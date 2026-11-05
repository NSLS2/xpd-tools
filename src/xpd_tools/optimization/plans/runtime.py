"""Device-level acquisition steps shared by Queue Server plans."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from math import ceil, pi
from typing import Any, Literal, cast
from uuid import uuid4

import numpy as np
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import RunEngineControlException
from ophyd import Signal

from xpd_tools.optimization.analysis import classify_pl
from xpd_tools.optimization.helpers.beamline import XrayUvvisPlanContext
from xpd_tools.optimization.helpers.sources import DilutionStage, FlowSource, WashCycle
from xpd_tools.optimization.plans.metadata import _device_name
from xpd_tools.optimization.plans.volumes import VolumeTracker

logger = logging.getLogger(__name__)


def _new_quality_signals() -> dict[str, Signal]:
    return {
        name: Signal(name=name, value=value)
        for name, value in (
            ("batch_index", 0),
            ("verdict", "bad"),
            ("peak_wavelength_nm", -1.0),
            ("n_good_total", 0),
            ("n_bad_total", 0),
            ("n_events_in_batch", 0),
        )
    }


def _unique_devices(devices: Sequence[Any]) -> list[Any]:
    seen: set[int] = set()
    unique: list[Any] = []
    for device in devices:
        identity = id(device)
        if identity not in seen:
            seen.add(identity)
            unique.append(device)
    return unique


def _configure_and_start(
    settings: Sequence[tuple[FlowSource | DilutionStage | WashCycle, float]],
    started: list[Any],
):
    """Configure every nonzero pump before starting each one in order."""
    for config, rate in settings:
        if rate == 0:
            continue
        yield from config.pump.set_infuse2(
            config.syringe_ml,
            syringe_material=config.material,
            set_target=config.set_target,
            target_vol=config.target_ml,
            target_unit="ml",
            infuse_rate=rate,
            infuse_unit="ul/min",
        )
    for config, rate in settings:
        if rate == 0:
            continue
        yield from config.pump.infuse_pump2()
        started.append(config.pump)


def _stop_running(pumps: Sequence[Any], started: list[Any]):
    for pump in _unique_devices(pumps):
        yield from pump.stop_pump2()
        started[:] = [running for running in started if running is not pump]


def _wait_for_equilibrium(
    pumps: Sequence[Any],
    mixer_lengths_cm: Sequence[float],
    *,
    ratio: float,
    tubing_id_mm: float = 1.016,
):
    """Wait for configured mixer residence time using live pump readbacks."""
    stages = (
        (
            (float(mixer_lengths_cm[0]), pumps[:2]),
            (float(mixer_lengths_cm[1]), pumps),
        )
        if len(mixer_lengths_cm) == 2
        else ((float(mixer_lengths_cm[0]), pumps),)
    )
    residence_seconds = 0.0
    for length_cm, stage_pumps in stages:
        total_rate = 0.0
        for pump in stage_pumps:
            rate = yield from bps.rd(pump.read_infuse_rate)
            unit = yield from bps.rd(pump.read_infuse_rate_unit)
            status = yield from bps.rd(pump.status)
            if status == "Infusing":
                if str(unit).lower() != "ul/min":
                    raise ValueError(
                        f"pump {pump.name!r} rate unit must be 'ul/min': {unit!r}"
                    )
                total_rate += float(rate)
        if total_rate <= 0:
            raise RuntimeError("active mixer flow must be greater than zero")
        mixer_volume_ul = pi * (tubing_id_mm / 2) ** 2 * length_cm * 10
        residence_seconds += 60 * mixer_volume_ul / total_rate
    yield from bps.sleep(residence_seconds * ratio)


def _run_pump_sequence_and_measure(
    context: Any,
    rates: tuple[float, ...],
    started: list[Any],
    measure: Callable[[], Any],
    volumes: VolumeTracker,
):
    """
    Stop, configure, and start pumps/dilutions around one measurement step.

    Args:
        - context: The optimization context.
        - rates: The pump rates to apply.
        - started: A list to track started pumps.
        - measure: A callable to perform the measurement.
        - volumes: Checks the syringes can supply this trial (before any pump
          starts) and times the measurement for the next check.

    Returns
    -------
        - Physical pump movements
    """
    all_pumps = _unique_devices(
        [
            *(source.pump for source in context.sources),
            *(stage.pump for stage in context.dilutions),
            *(cycle.pump for cycle in context.wash_cycles),
        ]
    )
    yield from volumes.check(rates)  # RefillRequired before any pump moves
    for pump in all_pumps:
        yield from pump.stop_pump2()

    yield from _configure_and_start(
        tuple(zip(context.sources, rates, strict=True)),
        started,
    )
    total_rate = sum(rates)
    dilution_settings = tuple(
        (stage, total_rate * stage.ratio) for stage in context.dilutions
    )
    before = tuple(
        item for item in dilution_settings if item[0].position == "before_equilibrium"
    )
    after = tuple(
        item for item in dilution_settings if item[0].position == "after_equilibrium"
    )
    yield from _configure_and_start(before, started)
    for stage, rate in before:
        if rate > 0 and stage.wait_sec:
            yield from bps.sleep(stage.wait_sec)

    yield from _wait_for_equilibrium(
        tuple(source.pump for source in context.sources),
        context.mixer_lengths_cm,
        ratio=context.residence_time_ratio,
        tubing_id_mm=context.tubing_id_mm,
    )

    yield from _configure_and_start(after, started)
    for stage, rate in after:
        if rate > 0 and stage.wait_sec:
            yield from bps.sleep(stage.wait_sec)

    measure_start = time.monotonic()
    yield from measure()
    volumes.record_measurement(time.monotonic() - measure_start)

    yield from _stop_running(tuple(started), started)
    for cycle in context.wash_cycles:
        yield from _configure_and_start(
            ((cycle, cycle.rate_ul_min),),
            started,
        )
        if cycle.rate_ul_min > 0 and cycle.duration_sec:
            yield from bps.sleep(cycle.duration_sec)
        yield from _stop_running((cycle.pump,), started)


def _measure_uvvis(
    context: XrayUvvisPlanContext,
    stream: Literal["fluorescence", "absorbance"],
    shots: int,
    *,
    settle_sec: float = 2,
):
    """
    Configure one optical mode and return its final event reading.

    Args:
        - context: The optimization context.
        - stream: The optical stream to configure.
        - shots: The number of shots to take.
        - settle_sec: The number of seconds to wait after configuring.

    Returns
    -------
        - The final event reading from the QEPRO.
    """
    modes = {
        "fluorescence": ("High", "Low", "Dark", "Corrected Sample"),
        "absorbance": ("Low", "High", "Reference", "Absorbtion"),
    }
    led_value, shutter_value, correction, spectrum_type = modes[stream]
    state = (
        (yield from bps.rd(context.led)),
        (yield from bps.rd(context.uv_shutter)),
        (yield from bps.rd(context.qepro.correction)),
        (yield from bps.rd(context.qepro.spectrum_type)),
    )
    desired = (led_value, shutter_value, correction, spectrum_type)
    if state != desired:
        yield from bps.mv(
            context.qepro.correction,
            correction,
            context.qepro.spectrum_type,
            spectrum_type,
            context.led,
            led_value,
            context.uv_shutter,
            shutter_value,
        )
        yield from bps.sleep(settle_sec)

    reading: Mapping[str, Any] | None = None
    for _ in range(shots):
        reading = yield from bps.trigger_and_read([context.qepro], name=stream)
    return reading


def _emit_quality_event(signals: Mapping[str, Signal], result: Mapping[str, Any]):
    """Emit one fluorescence-quality event."""
    values = dict(result)
    peak_wavelength = float(values["peak_wavelength_nm"])
    values["peak_wavelength_nm"] = (
        peak_wavelength if np.isfinite(peak_wavelength) else -1.0
    )
    moves = [item for name, value in values.items() for item in (signals[name], value)]
    yield from bps.mv(*moves)
    yield from bps.create(name="fluorescence_quality")
    for signal in signals.values():
        yield from bps.read(cast(Any, signal))
    yield from bps.save()


def _measure_pl_with_quality_gate(
    context: XrayUvvisPlanContext,
    signals: Mapping[str, Signal],
):
    """
    Collect fluorescence batches until the configured quality limit.

    Args:
        - context: The optimization context.
        - signals: The signals to emit quality events on.

    Returns
    -------
        - None. Stops after `good_batches` good or `max_bad_batches` bad batches.
    """
    good_count = 0
    bad_count = 0
    batch_index = 0
    while True:
        reading = yield from _measure_uvvis(
            context,
            "fluorescence",
            context.quality.fluorescence_shots,
        )
        if not context.quality.enabled:
            return

        x_field = context.qepro.x_axis.name
        y_field = context.qepro.output.name
        if reading is None or x_field not in reading or y_field not in reading:
            raise RuntimeError("fluorescence batch produced no matching QEPro event")
        wavelength = np.asarray(reading[x_field]["value"])
        intensity = np.asarray(reading[y_field]["value"])

        # Determine whether the measurement is good, and find the peak wavelength
        is_good, peak_wavelength = classify_pl(
            wavelength,
            intensity,
            key_height=context.fit_settings.pl_screen_key_height,
            height=context.fit_settings.pl_screen_peak_height,
            distance=context.fit_settings.pl_screen_peak_distance,
        )
        if is_good:
            good_count += 1
        else:
            bad_count += 1
        yield from _emit_quality_event(
            signals,
            {
                "batch_index": batch_index,
                "verdict": "good" if is_good else "bad",
                "peak_wavelength_nm": peak_wavelength,
                "n_good_total": good_count,
                "n_bad_total": bad_count,
                "n_events_in_batch": context.quality.fluorescence_shots,
            },
        )
        batch_index += 1
        if (
            good_count >= context.quality.good_batches
            or bad_count >= context.quality.max_bad_batches
        ):
            return


def _prepare_xray_detector(context: XrayUvvisPlanContext):
    """Configure the area detector and return scan-plan metadata."""
    yield from bps.mv(
        context.xray_detector.cam.acquire_time,
        context.xray.frame_acq_time,
    )
    acquisition_time = float(
        (yield from bps.rd(context.xray_detector.cam.acquire_time))
    )
    if acquisition_time <= 0:
        raise ValueError("detector acquisition time must be positive")
    frame_count = max(1, int(ceil(context.xray.exposure / acquisition_time)))
    if hasattr(context.xray_detector, "images_per_set"):
        yield from bps.mv(context.xray_detector.images_per_set, frame_count)
    computed_exposure = frame_count * acquisition_time
    plan_metadata = {
        "time_per_frame": acquisition_time,
        "num_frames": frame_count,
        "requested_exposure": context.xray.exposure,
        "computed_exposure": computed_exposure,
        "type": "generator",
        "uid": str(uuid4()),
        "plan_name": "trigger",
    }
    return {
        "sp_time_per_frame": acquisition_time,
        "sp_num_frames": frame_count,
        "sp_requested_exposure": context.xray.exposure,
        "sp_computed_exposure": computed_exposure,
        "sp_type": "bps.trigger",
        "sp_uid": str(uuid4()),
        "sp_plan_name": "trigger",
        "sp_detector": context.xray_detector.name,
        "sp": plan_metadata,
    }


def _measure_scattering(context: XrayUvvisPlanContext):
    """Collect one scattering event and always close the fast shutter."""

    def acquire():
        yield from bps.mv(context.fast_shutter, -20)
        yield from bps.trigger_and_read(
            [context.xray_detector], name=context.xray.stream_name
        )

    return (
        yield from bpp.finalize_wrapper(acquire(), bps.mv(context.fast_shutter, 20))
    )


def _cleanup_devices(context: XrayUvvisPlanContext, started: Sequence[Any]):
    """Attempt every pump and optical safe-state action before raising failures."""
    errors: list[Exception] = []
    for pump in reversed(_unique_devices(started)):
        try:
            yield from pump.stop_pump2()
        except RunEngineControlException as exc:  # stop/abort: keep cleaning up
            errors.append(exc)
        except Exception as exc:
            logger.exception("Failed to stop pump %s", _device_name(pump))
            errors.append(exc)
    for signal, value, label in (
        (getattr(context, "led", None), "Low", "LED"),
        (getattr(context, "uv_shutter", None), "Low", "UV shutter"),
        (context.fast_shutter, 20, "fast shutter"),
    ):
        if signal is None:
            continue
        try:
            yield from bps.abs_set(signal, value, wait=True)
        except RunEngineControlException as exc:  # stop/abort: keep cleaning up
            errors.append(exc)
        except Exception as exc:
            logger.exception("Failed to place %s in its safe state", label)
            errors.append(exc)
    control = [e for e in errors if isinstance(e, RunEngineControlException)]
    failures = [e for e in errors if not isinstance(e, RunEngineControlException)]
    group = ExceptionGroup("acquisition cleanup failed", failures) if failures else None
    if control:
        # Every safe-state action was still attempted; report the stop/abort itself,
        # with any real cleanup failures attached as its cause.
        raise control[0] from group
    if group:
        raise group


def _with_safe_cleanup(plan: Any, cleanup: Callable[[], Any]):
    """Run cleanup and explicitly chain failures from acquisition errors."""
    try:
        result = yield from plan
    except Exception as primary_error:
        try:
            yield from cleanup()
        except Exception as cleanup_error:
            if isinstance(primary_error, RunEngineControlException):
                raise primary_error from cleanup_error  # stop/abort stays the outcome
            raise cleanup_error from primary_error
        raise
    else:
        yield from cleanup()
        return result
