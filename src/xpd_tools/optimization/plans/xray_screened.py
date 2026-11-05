"""Bound Queue Server acquisition plan for UV-Vis-screened X-ray runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp

from xpd_tools.optimization.helpers.beamline import XrayUvvisPlanContext
from xpd_tools.optimization.plans.metadata import _build_run_metadata
from xpd_tools.optimization.plans.preflight import _preflight
from xpd_tools.optimization.plans.runtime import (
    _cleanup_devices,
    _measure_pl_with_quality_gate,
    _measure_scattering,
    _new_quality_signals,
    _prepare_xray_detector,
    _run_pump_sequence_and_measure,
    _unique_devices,
    _with_safe_cleanup,
)
from xpd_tools.optimization.plans.validation import _validate_context
from xpd_tools.optimization.plans.volumes import VolumeTracker


def create_xray_screened_plan(context: XrayUvvisPlanContext) -> Callable[..., Any]:
    """Bind validated hardware once and return the Queue Server acquisition plan.

    Runs the UV-Vis fluorescence quality gate to decide whether the sample is
    worth committing X-ray beamtime to, but does not measure absorbance or
    record UV-Vis data as an optimization objective.
    """
    _validate_context(context)
    volumes = VolumeTracker(context)  # checks loaded_ml; state kept across trials
    quality_signals = _new_quality_signals()

    def xray_screened_acquire(
        suggestions: Sequence[Mapping[str, Any]],
        actuators: Sequence[Any],
        sensors: Sequence[Any] | None = None,
        md: Mapping[str, Any] | None = None,
    ):
        """Acquire one UV-Vis-screened X-ray optimization run."""
        del actuators, sensors
        rates = _preflight(context, suggestions, plan_name="xray_screened_acquire")
        detector_metadata = yield from _prepare_xray_detector(context)
        run_metadata = _build_run_metadata(
            context,
            rates,
            md,
            detector_metadata,
        )
        started: list[Any] = []

        def cleanup():
            yield from _cleanup_devices(context, started)

        def measure():
            yield from _measure_pl_with_quality_gate(context, quality_signals)
            yield from bps.mv(context.led, "Low", context.uv_shutter, "Low")
            yield from context.wrap_xray_run(
                _measure_scattering(context),
                context.xray.no_dark,
            )

        def acquisition():
            yield from _run_pump_sequence_and_measure(
                context, rates, started, measure, volumes
            )

        plan = bpp.stage_wrapper(acquisition(), [context.qepro, context.xray_detector])
        plan = bpp.baseline_wrapper(
            plan, _unique_devices([source.pump for source in context.sources])
        )
        plan = _with_safe_cleanup(plan, cleanup)
        plan = bpp.run_wrapper(plan, md=run_metadata)
        plan = bpp.set_run_key_wrapper(plan, "xray_screened_acquire")
        return (yield from plan)

    return xray_screened_acquire
