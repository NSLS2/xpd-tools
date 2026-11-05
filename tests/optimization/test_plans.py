from __future__ import annotations

import threading
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from bluesky.run_engine import RunEngine
from bluesky.utils import FailedStatus, Msg, RequestStop, RunEngineInterrupted
from ophyd import Signal

from xpd_tools.optimization.plans import (
    DilutionStage,
    FlowSource,
    QualityPolicy,
    SpectraFitSettings,
    UvvisPlanContext,
    WashCycle,
    XrayPlanContext,
    XraySettings,
    create_uvvis_plan,
    create_xray_plan,
    create_xray_screened_plan,
    create_xray_uvvis_plan,
)
from xpd_tools.optimization.plans.runtime import _cleanup_devices, _with_safe_cleanup


def _source(name: str, pump: Any) -> FlowSource:
    return FlowSource(
        dof=f"infusion_rate_{name}",
        pump=pump,
        precursor=f"precursor-{name}",
        sample_label=name,
    )


def test_quality_gate_xray_and_canonical_metadata(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_qepro: Any,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    fake_area_detector: Any,
    wavelength: np.ndarray,
    bad_spectrum: np.ndarray,
    good_spectrum: np.ndarray,
    plan_context_factory: Any,
) -> None:
    absorbance = (
        0.0001 * wavelength
        + 0.2
        + 0.25 * np.exp(-((wavelength - 365) ** 2) / (2 * 12**2))
    )
    fake_qepro.spectra = [bad_spectrum, good_spectrum, absorbance]
    wrapped: list[bool] = []

    def wrap(plan: Any, no_dark: bool):
        wrapped.append(no_dark)
        return plan

    pump = fake_pumps["dds2_p1"]
    context = plan_context_factory(
        sources=(_source("CsPb", pump),),
        quality=QualityPolicy(
            enabled=True,
            good_batches=1,
            max_bad_batches=2,
            absorbance_shots=1,
            fluorescence_shots=1,
        ),
        xray=XraySettings(exposure=0.25, frame_acq_time=0.1, no_dark=False),
        wrap_xray_run=wrap,
    )
    plan = create_xray_uvvis_plan(context)
    metadata = {
        "sample_name": "caller-value",
        "flow_config": "obsolete",
        "blop_correlation_uid": "correlation",
        "blop_suggestions": [{"_id": 7}],
    }
    safe_at_stop: list[tuple[str, str, int]] = []

    def record_stop(name: str, doc: dict[str, Any]) -> None:
        if name == "stop":
            safe_at_stop.append(
                cast(
                    tuple[str, str, int],
                    tuple(signal.get() for signal in optical_signals),
                )
            )

    token = RE.subscribe(record_stop)
    try:
        result = RE(
            plan(
                [{"_id": 7, "infusion_rate_CsPb": 25}],
                [],
                md=metadata,
            )
        )
    finally:
        RE.unsubscribe(token)

    start = next(doc for name, doc in documents if name == "start")
    descriptors = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    quality_events = [
        doc
        for name, doc in documents
        if name == "event" and descriptors[doc["descriptor"]] == "fluorescence_quality"
    ]
    stream_counts = {
        stream: sum(
            name == "event" and descriptors.get(doc["descriptor"]) == stream
            for name, doc in documents
            if name == "event"
        )
        for stream in ("fluorescence", "absorbance", "scattering")
    }

    assert plan.__name__ == "xray_uvvis_acquire"
    assert cast(Any, result).plan_result == start["uid"]
    assert [event["data"]["verdict"] for event in quality_events] == ["bad", "good"]
    assert [event["data"]["n_events_in_batch"] for event in quality_events] == [1, 1]
    assert stream_counts == {"fluorescence": 2, "absorbance": 1, "scattering": 1}
    assert wrapped == [False]
    assert fake_area_detector.images_per_set.get() == 3
    assert start["sp_num_frames"] == 3
    assert start["sample_name"] == "CsPb_025"
    assert start["sample_type"] == "CsPb_025"
    assert start["blop_correlation_uid"] == "correlation"
    assert start["blop_suggestions"] == [{"_id": 7}]
    assert start["dof_names"] == ["infusion_rate_CsPb"]
    assert start["precursors"] == ["precursor-CsPb"]
    assert start["pumps"] == ["dds2_p1"]
    assert start["detectors"] == ["QEPro", "xray_detector"]
    assert start["use_good_bad"] is True
    assert start["xray_uvvis_config"]["sources"][0]["pump"] == "dds2_p1"
    assert start["xray_uvvis_config"]["xray"]["exposure"] == 0.25
    assert "flow_config" not in start
    assert pump.status.get() == "Stopped"
    assert safe_at_stop == [("Low", "Low", 20)]


def test_xray_stream_name_is_used_for_the_scattering_event(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_pumps: Mapping[str, Any],
    plan_context_factory: Any,
) -> None:
    pump = fake_pumps["dds2_p1"]
    context = plan_context_factory(
        sources=(_source("CsPb", pump),),
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1),
        xray=XraySettings(
            exposure=0.1, frame_acq_time=0.1, no_dark=True, stream_name="custom_stream"
        ),
    )
    plan = create_xray_uvvis_plan(context)

    RE(plan([{"_id": 1, "infusion_rate_CsPb": 25}], [], md={}))

    descriptor_names = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    event_streams = {
        descriptor_names[doc["descriptor"]]
        for name, doc in documents
        if name == "event"
    }
    assert "custom_stream" in event_streams
    assert "scattering" not in event_streams


@pytest.mark.parametrize(
    "suggestions",
    [
        [],
        [{"infusion_rate_CsPb": 10}, {"infusion_rate_CsPb": 20}],
        [{"_id": 1}],
        [{"infusion_rate_unknown": 10}],
        [{"infusion_rate_CsPb": -1}],
        [{"infusion_rate_CsPb": np.nan}],
        [{"infusion_rate_CsPb": 0}],
        [{"infusion_rate_CsPb": 10, "extra": 1}],
    ],
)
def test_preflight_errors_before_device_messages(
    suggestions: list[dict[str, Any]],
    fake_pumps: Mapping[str, Any],
    fake_area_detector: Any,
    plan_context_factory: Any,
) -> None:
    pump = fake_pumps["dds2_p1"]
    plan = create_xray_uvvis_plan(
        plan_context_factory(
            sources=(_source("CsPb", pump),),
        )
    )

    with pytest.raises(ValueError):
        next(plan(suggestions, []))
    assert pump.stop_count == 0
    assert fake_area_detector.images_per_set.get() == 1


def test_preflight_error_names_the_actual_plan(
    fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    """_preflight is shared by all four acquisition plans -- its error must
    name whichever plan actually raised it, not always xray_uvvis_acquire.
    """
    context = plan_context_factory(sources=(_source("CsPb", fake_pumps["dds2_p1"]),))

    with pytest.raises(ValueError, match="^uvvis_acquire requires"):
        next(create_uvvis_plan(context)([], []))
    with pytest.raises(ValueError, match="^xray_acquire requires"):
        next(create_xray_plan(context)([], []))
    with pytest.raises(ValueError, match="^xray_screened_acquire requires"):
        next(create_xray_screened_plan(context)([], []))
    with pytest.raises(ValueError, match="^xray_uvvis_acquire requires"):
        next(create_xray_uvvis_plan(context)([], []))


def test_live_quality_gate_uses_configured_fit_settings(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_qepro: Any,
    wavelength: np.ndarray,
    fake_pumps: Mapping[str, Any],
    plan_context_factory: Any,
) -> None:
    """_measure_pl_with_quality_gate must classify against context.fit_settings,
    not classify_pl's own hardcoded defaults (key_height=2000) -- a peak
    below 2000 but above SpectraFitSettings' default (200) must be "good".
    """
    moderate_peak = 1000 * np.exp(-((wavelength - 660) ** 2) / (2 * 20**2))
    fake_qepro.spectra = [moderate_peak]
    pump = fake_pumps["dds2_p1"]
    context = plan_context_factory(
        sources=(_source("CsPb", pump),),
        quality=QualityPolicy(
            enabled=True, good_batches=1, max_bad_batches=1, fluorescence_shots=1
        ),
        fit_settings=SpectraFitSettings(),  # default pl_screen_key_height=200
    )

    RE(create_xray_uvvis_plan(context)([{"_id": 1, "infusion_rate_CsPb": 25}], []))

    descriptors = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    quality_events = [
        doc["data"]
        for name, doc in documents
        if name == "event" and descriptors[doc["descriptor"]] == "fluorescence_quality"
    ]
    assert quality_events[0]["verdict"] == "good"


def test_context_normalizes_sequence_fields_to_tuples(
    fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    """sources/dilutions/wash_cycles/mixer_lengths_cm are typed tuple[...]
    but nothing else enforced it -- a caller passing lists (e.g. a real
    worker startup script) would build a frozen dataclass with an
    unhashable field. See beamline.py's _normalize_context_sequences.
    """
    context = plan_context_factory(
        sources=[_source("CsPb", fake_pumps["dds2_p1"])],
        dilutions=[],
        wash_cycles=[],
        mixer_lengths_cm=[10.0, 20.0],
    )
    assert isinstance(context.sources, tuple)
    assert isinstance(context.dilutions, tuple)
    assert isinstance(context.wash_cycles, tuple)
    assert isinstance(context.mixer_lengths_cm, tuple)
    hash(context)  # would raise TypeError if any field were still a list


def test_factory_validates_static_context(
    fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    source = _source("CsPb", fake_pumps["dds2_p1"])
    with pytest.raises(ValueError, match="unique"):
        create_xray_uvvis_plan(plan_context_factory(sources=(source, source)))


def test_both_dilution_positions_use_total_source_rate(
    RE: RunEngine,
    fake_pumps: Mapping[str, Any],
    plan_context_factory: Any,
) -> None:
    source = fake_pumps["dds2_p1"]
    before = fake_pumps["dds1_p1"]
    after = fake_pumps["ultra2"]
    context = plan_context_factory(
        sources=(_source("CsPb", source),),
        dilutions=(
            DilutionStage(
                pump=before,
                ratio=2,
                position="before_equilibrium",
                syringe_ml=20,
                material="plastic_BD",
                target_ml=20,
            ),
            DilutionStage(
                pump=after,
                ratio=3,
                position="after_equilibrium",
                syringe_ml=100,
                material="steel",
                target_ml=100,
            ),
        ),
    )

    RE(create_xray_uvvis_plan(context)([{"infusion_rate_CsPb": 20}], []))

    assert source.configurations[0]["infuse_rate"] == 20
    assert before.configurations[0]["infuse_rate"] == 40
    assert after.configurations[0]["infuse_rate"] == 60
    assert before.configurations[0]["infuse_unit"] == "ul/min"
    assert after.configurations[0]["target_unit"] == "ml"
    assert (source.status.get(), before.status.get(), after.status.get()) == (
        "Stopped",
        "Stopped",
        "Stopped",
    )


def test_partial_start_failure_cleans_only_successfully_started_pumps(
    RE: RunEngine,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    plan_context_factory: Any,
) -> None:
    first = fake_pumps["dds2_p1"]
    second = fake_pumps["dds2_p2"]
    second.fail_start = True
    context = plan_context_factory(
        sources=(_source("A", first), _source("B", second)),
    )

    with pytest.raises(RuntimeError, match="failed to start"):
        RE(
            create_xray_uvvis_plan(context)(
                [{"infusion_rate_A": 10, "infusion_rate_B": 20}], []
            )
        )

    assert first.start_count == 1
    assert first.stop_count == 2
    assert first.status.get() == "Stopped"
    assert second.start_count == 1
    assert second.stop_count == 1
    assert tuple(signal.get() for signal in optical_signals) == ("Low", "Low", 20)


def test_wash_failure_closes_devices_safely(
    RE: RunEngine,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    plan_context_factory: Any,
) -> None:
    source = fake_pumps["dds2_p1"]
    wash = fake_pumps["ultra1"]
    wash.fail_start = True
    context = plan_context_factory(
        sources=(_source("CsPb", source),),
        wash_cycles=(WashCycle(pump=wash, rate_ul_min=500, duration_sec=0),),
    )

    with pytest.raises(RuntimeError, match="failed to start"):
        RE(create_xray_uvvis_plan(context)([{"infusion_rate_CsPb": 10}], []))

    assert source.status.get() == "Stopped"
    assert wash.start_count == 1
    assert tuple(signal.get() for signal in optical_signals) == ("Low", "Low", 20)


def test_scattering_failure_closes_shutter_and_pumps(
    RE: RunEngine,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    fake_area_detector: Any,
    plan_context_factory: Any,
) -> None:
    source = fake_pumps["dds2_p1"]
    fake_area_detector.fail_trigger = True
    wrapped: list[bool] = []

    def wrap(plan: Any, no_dark: bool):
        wrapped.append(no_dark)
        return plan

    context = plan_context_factory(
        sources=(_source("CsPb", source),),
        wrap_xray_run=wrap,
    )

    with pytest.raises(FailedStatus):
        RE(create_xray_uvvis_plan(context)([{"infusion_rate_CsPb": 10}], []))

    assert wrapped == [True]
    assert source.status.get() == "Stopped"
    assert optical_signals[2].get() == 20


def test_cleanup_aggregates_failures_and_continues_safe_actions(
    RE: RunEngine,
    fake_qepro: Any,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    plan_context_factory: Any,
) -> None:
    first = fake_pumps["dds2_p1"]
    second = fake_pumps["dds2_p2"]
    first.fail_stop_call = 2
    fake_qepro.fail_on_trigger = 1
    context = plan_context_factory(
        sources=(_source("A", first), _source("B", second)),
    )

    with pytest.raises(ExceptionGroup, match="acquisition cleanup failed") as exc:
        RE(
            create_xray_uvvis_plan(context)(
                [{"infusion_rate_A": 10, "infusion_rate_B": 20}], []
            )
        )

    assert isinstance(exc.value.__cause__, FailedStatus)
    assert first.stop_count == 2
    assert second.stop_count == 2
    assert second.status.get() == "Stopped"
    assert tuple(signal.get() for signal in optical_signals) == ("Low", "Low", 20)


def test_cleanup_runs_when_plan_is_cancelled(
    RE: RunEngine,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    plan_context_factory: Any,
) -> None:
    pump = fake_pumps["dds2_p1"]
    context = plan_context_factory(
        sources=(_source("CsPb", pump),),
        mixer_lengths_cm=(1.0,),
        residence_time_ratio=1.0,
    )
    pause = threading.Timer(0.1, RE.request_pause)
    pause.start()
    try:
        with pytest.raises(RunEngineInterrupted):
            RE(create_xray_uvvis_plan(context)([{"infusion_rate_CsPb": 10}], []))
        RE.abort()
    finally:
        pause.cancel()

    assert pump.status.get() == "Stopped"
    assert tuple(signal.get() for signal in optical_signals) == ("Low", "Low", 20)


def test_create_xray_screened_plan_gates_without_recording_absorbance(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_pumps: Mapping[str, Any],
    plan_context_factory: Any,
) -> None:
    pump = fake_pumps["dds2_p1"]
    context = plan_context_factory(
        sources=(_source("CsPb", pump),),
        quality=QualityPolicy(
            enabled=True, good_batches=1, max_bad_batches=2, fluorescence_shots=1
        ),
        xray=XraySettings(exposure=0.1, frame_acq_time=0.1),
    )
    plan = create_xray_screened_plan(context)

    result = RE(plan([{"_id": 1, "infusion_rate_CsPb": 25}], []))

    start = next(doc for name, doc in documents if name == "start")
    descriptor_names = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    event_streams = {
        descriptor_names[doc["descriptor"]]
        for name, doc in documents
        if name == "event"
    }

    assert plan.__name__ == "xray_screened_acquire"
    assert cast(Any, result).plan_result == start["uid"]
    assert event_streams == {"fluorescence", "fluorescence_quality", "scattering"}
    assert "absorbance" not in event_streams
    assert start["detectors"] == ["QEPro", "xray_detector"]
    assert pump.status.get() == "Stopped"


def test_create_xray_plan_runs_without_uvvis_devices(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    fake_area_detector: Any,
) -> None:
    _led, _uv_shutter, fast_shutter = optical_signals
    pump = fake_pumps["dds2_p1"]
    context = XrayPlanContext(
        led=_led,
        fast_shutter=fast_shutter,
        xray_detector=fake_area_detector,
        wrap_xray_run=lambda plan, no_dark: plan,
        sources=(_source("CsPb", pump),),
        dilutions=(),
        wash_cycles=(),
        mixer_lengths_cm=(0.0,),
        residence_time_ratio=0.0,
        xray=XraySettings(exposure=0.1, frame_acq_time=0.1),
    )
    plan = create_xray_plan(context)

    result = RE(plan([{"_id": 1, "infusion_rate_CsPb": 25}], []))

    start = next(doc for name, doc in documents if name == "start")
    descriptor_names = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    event_streams = {
        descriptor_names[doc["descriptor"]]
        for name, doc in documents
        if name == "event"
    }

    assert plan.__name__ == "xray_acquire"
    assert cast(Any, result).plan_result == start["uid"]
    assert event_streams == {"scattering"}
    assert start["detectors"] == ["xray_detector"]
    assert "xray_config" in start
    assert "xray_uvvis_config" not in start
    assert pump.status.get() == "Stopped"


def test_create_uvvis_plan_runs_without_xray_devices(
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
    fake_qepro: Any,
    fake_pumps: Mapping[str, Any],
    optical_signals: tuple[Signal, Signal, Signal],
    good_spectrum: np.ndarray,
) -> None:
    led, uv_shutter, fast_shutter = optical_signals
    pump = fake_pumps["dds2_p1"]
    fake_qepro.spectra = [good_spectrum, good_spectrum]
    context = UvvisPlanContext(
        qepro=fake_qepro,
        led=led,
        uv_shutter=uv_shutter,
        fast_shutter=fast_shutter,
        sources=(_source("CsPb", pump),),
        dilutions=(),
        wash_cycles=(),
        mixer_lengths_cm=(0.0,),
        residence_time_ratio=0.0,
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1),
    )
    plan = create_uvvis_plan(context)

    result = RE(plan([{"_id": 1, "infusion_rate_CsPb": 25}], []))

    start = next(doc for name, doc in documents if name == "start")
    descriptor_names = {
        doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"
    }
    event_streams = {
        descriptor_names[doc["descriptor"]]
        for name, doc in documents
        if name == "event"
    }

    assert plan.__name__ == "uvvis_acquire"
    assert cast(Any, result).plan_result == start["uid"]
    assert event_streams == {"fluorescence", "absorbance"}
    assert start["detectors"] == ["QEPro"]
    assert start["use_good_bad"] is False
    assert "uvvis_config" in start
    assert "xray_uvvis_config" not in start
    assert pump.status.get() == "Stopped"


class _CleanupPump:
    """A pump whose stop_pump2 plan yields one message, optionally failing."""

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail

    def stop_pump2(self):
        yield Msg("stop_pump", self)
        if self.fail:
            raise RuntimeError(f"{self.name} did not stop")


def _drive(plan: Any, throw_at: int | None = None, exc: BaseException | None = None):
    """Run a plan by hand, throwing `exc` in at message index `throw_at`, as
    RE.stop() does. Returns (messages seen, exception the plan ended with)."""
    messages, index = [], 0
    try:
        msg = next(plan)
        while True:
            messages.append(msg)
            msg = plan.throw(exc) if index == throw_at else plan.send(None)
            index += 1
    except StopIteration:
        return messages, None
    except BaseException as error:  # noqa: BLE001 -- the outcome under test
        return messages, error


def _cleanup_context() -> Any:
    return SimpleNamespace(led="led", uv_shutter="uv_shutter", fast_shutter="shutter")


def test_stop_during_cleanup_finishes_cleanup_and_stays_a_stop() -> None:
    """A stop landing mid-cleanup must not become 'acquisition cleanup failed'."""
    pumps = [_CleanupPump("A"), _CleanupPump("B")]
    messages, outcome = _drive(
        _cleanup_devices(_cleanup_context(), pumps), throw_at=0, exc=RequestStop()
    )
    stopped = [m.obj.name for m in messages if m.command == "stop_pump"]
    moved = [m.obj for m in messages if m.command == "set"]
    assert stopped == ["B", "A"]  # both pumps still stopped (reverse order)
    assert moved == ["led", "uv_shutter", "shutter"]  # every safe state still set
    assert isinstance(outcome, RequestStop)
    assert outcome.__cause__ is None


def test_stop_during_cleanup_keeps_real_failures_as_its_cause() -> None:
    pumps = [_CleanupPump("A", fail=True), _CleanupPump("B")]
    _, outcome = _drive(
        _cleanup_devices(_cleanup_context(), pumps), throw_at=0, exc=RequestStop()
    )
    assert isinstance(outcome, RequestStop)
    assert isinstance(outcome.__cause__, ExceptionGroup)
    assert [str(e) for e in outcome.__cause__.exceptions] == ["A did not stop"]


def test_stop_during_acquisition_stays_a_stop_when_cleanup_fails() -> None:
    def acquisition():
        yield Msg("acquire")

    def failing_cleanup():
        yield Msg("cleanup")
        raise ExceptionGroup("acquisition cleanup failed", [RuntimeError("pump")])

    plan = _with_safe_cleanup(acquisition(), failing_cleanup)
    _, outcome = _drive(plan, throw_at=0, exc=RequestStop())
    assert isinstance(outcome, RequestStop)
    assert isinstance(outcome.__cause__, ExceptionGroup)


def test_real_acquisition_error_still_reports_the_cleanup_failure() -> None:
    """Unchanged behaviour for genuine errors: cleanup failure, chained from it."""

    def acquisition():
        yield Msg("acquire")
        raise ValueError("detector")

    def failing_cleanup():
        yield Msg("cleanup")
        raise ExceptionGroup("acquisition cleanup failed", [RuntimeError("pump")])

    _, outcome = _drive(_with_safe_cleanup(acquisition(), failing_cleanup))
    assert isinstance(outcome, ExceptionGroup)
    assert isinstance(outcome.__cause__, ValueError)
