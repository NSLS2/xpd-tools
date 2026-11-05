"""Bound Queue Server acquisition plan for X-ray runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp

from xpd_tools.optimization.helpers.beamline import XrayPlanContext
from xpd_tools.optimization.plans.metadata import (
    _config_metadata,
    _device_name,
    _sample_name,
)
from xpd_tools.optimization.plans.preflight import _preflight
from xpd_tools.optimization.plans.runtime import (
    _cleanup_devices,
    _measure_scattering,
    _prepare_xray_detector,
    _run_pump_sequence_and_measure,
    _unique_devices,
    _with_safe_cleanup,
)
from xpd_tools.optimization.plans.validation import _validate_context
from xpd_tools.optimization.plans.volumes import VolumeTracker


def _serialized_xray_config(context: XrayPlanContext) -> dict[str, Any]:
    return {
        "devices": {
            "led": _device_name(context.led),
            "fast_shutter": _device_name(context.fast_shutter),
            "xray_detector": _device_name(context.xray_detector),
        },
        "sources": [_config_metadata(source) for source in context.sources],
        "dilutions": [_config_metadata(stage) for stage in context.dilutions],
        "wash_cycles": [_config_metadata(cycle) for cycle in context.wash_cycles],
        "mixer_lengths_cm": list(context.mixer_lengths_cm),
        "residence_time_ratio": context.residence_time_ratio,
        "xray": _config_metadata(context.xray),
    }


def _build_xray_run_metadata(
    context: XrayPlanContext,
    rates: tuple[float, ...],
    supplied: Mapping[str, Any] | None,
    detector_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge caller metadata before authoritative derived metadata."""
    metadata = deepcopy(dict(supplied or {}))
    metadata.update(detector_metadata)
    sample_name = _sample_name(
        rates, tuple(source.sample_label for source in context.sources)
    )
    metadata.update(
        {
            "sample_type": sample_name,
            "sample_name": sample_name,
            "infuse_rates": list(rates),
            "dof_names": [source.dof for source in context.sources],
            "precursors": [source.precursor for source in context.sources],
            "pumps": [_device_name(source.pump) for source in context.sources],
            "detectors": [_device_name(context.xray_detector)],
            "xray_config": _serialized_xray_config(context),
        }
    )
    return metadata


def create_xray_plan(context: XrayPlanContext) -> Callable[..., Any]:
    """Bind validated hardware once and return the Queue Server acquisition plan."""
    _validate_context(context)
    volumes = VolumeTracker(context)  # checks loaded_ml; state kept across trials

    def xray_acquire(
        suggestions: Sequence[Mapping[str, Any]],
        actuators: Sequence[Any],
        sensors: Sequence[Any] | None = None,
        md: Mapping[str, Any] | None = None,
    ):
        """Acquire one X-ray optimization run."""
        del actuators, sensors
        rates = _preflight(context, suggestions, plan_name="xray_acquire")
        detector_metadata = yield from _prepare_xray_detector(context)
        run_metadata = _build_xray_run_metadata(
            context,
            rates,
            md,
            detector_metadata,
        )
        started: list[Any] = []

        def cleanup():
            yield from _cleanup_devices(context, started)

        def measure():
            yield from bps.mv(context.led, "Low")
            yield from context.wrap_xray_run(
                _measure_scattering(context),
                context.xray.no_dark,
            )

        def acquisition():
            yield from _run_pump_sequence_and_measure(
                context, rates, started, measure, volumes
            )

        plan = bpp.stage_wrapper(acquisition(), [context.xray_detector])
        plan = bpp.baseline_wrapper(
            plan, _unique_devices([source.pump for source in context.sources])
        )
        plan = _with_safe_cleanup(plan, cleanup)
        plan = bpp.run_wrapper(plan, md=run_metadata)
        plan = bpp.set_run_key_wrapper(plan, "xray_acquire")
        return (yield from plan)

    return xray_acquire
