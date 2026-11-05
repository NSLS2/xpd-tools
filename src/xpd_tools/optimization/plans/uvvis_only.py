"""Bound Queue Server acquisition plan for UV-Vis runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp

from xpd_tools.optimization.helpers.beamline import UvvisPlanContext
from xpd_tools.optimization.plans.metadata import (
    _config_metadata,
    _device_name,
    _sample_name,
)
from xpd_tools.optimization.plans.preflight import _preflight
from xpd_tools.optimization.plans.runtime import (
    _cleanup_devices,
    _measure_pl_with_quality_gate,
    _measure_uvvis,
    _new_quality_signals,
    _run_pump_sequence_and_measure,
    _unique_devices,
    _with_safe_cleanup,
)
from xpd_tools.optimization.plans.validation import _require_finite_nonnegative
from xpd_tools.optimization.plans.volumes import VolumeTracker


def _validate_uvvis_context(context: UvvisPlanContext) -> None:
    """Validate static process configuration before constructing a plan."""
    if not context.sources:
        raise ValueError("sources must contain at least one flow source")

    # Validate source DOFs
    dofs = [source.dof for source in context.sources]
    if any(not dof or not dof.startswith("infusion_rate_") for dof in dofs):
        raise ValueError("source DOFs must be nonempty infusion_rate_* names")
    if len(set(dofs)) != len(dofs):
        raise ValueError("source DOFs must be unique")

    # Validate mixer lengths
    if len(context.mixer_lengths_cm) not in {1, 2}:
        raise ValueError("mixer_lengths_cm must contain one or two values")
    for index, length in enumerate(context.mixer_lengths_cm):
        _require_finite_nonnegative(length, f"mixer_lengths_cm[{index}]")
    _require_finite_nonnegative(context.residence_time_ratio, "residence_time_ratio")

    # Validate dilutions
    for index, stage in enumerate(context.dilutions):
        if stage.position not in {"before_equilibrium", "after_equilibrium"}:
            raise ValueError(f"dilutions[{index}].position is unsupported")
        _require_finite_nonnegative(stage.ratio, f"dilutions[{index}].ratio")
        _require_finite_nonnegative(stage.wait_sec, f"dilutions[{index}].wait_sec")

    # Validate wash cycles
    for index, cycle in enumerate(context.wash_cycles):
        _require_finite_nonnegative(
            cycle.rate_ul_min, f"wash_cycles[{index}].rate_ul_min"
        )
        _require_finite_nonnegative(
            cycle.duration_sec, f"wash_cycles[{index}].duration_sec"
        )

    # Validate quality limits
    if context.quality.good_batches < 0 or context.quality.max_bad_batches < 0:
        raise ValueError("quality batch limits must be non-negative")
    if context.quality.absorbance_shots < 1 or context.quality.fluorescence_shots < 1:
        raise ValueError("quality shot counts must be positive")


def _serialized_uvvis_config(context: UvvisPlanContext) -> dict[str, Any]:
    return {
        "devices": {
            "qepro": _device_name(context.qepro),
            "led": _device_name(context.led),
            "uv_shutter": _device_name(context.uv_shutter),
            "fast_shutter": _device_name(context.fast_shutter),
        },
        "sources": [_config_metadata(source) for source in context.sources],
        "dilutions": [_config_metadata(stage) for stage in context.dilutions],
        "wash_cycles": [_config_metadata(cycle) for cycle in context.wash_cycles],
        "mixer_lengths_cm": list(context.mixer_lengths_cm),
        "residence_time_ratio": context.residence_time_ratio,
        "quality": _config_metadata(context.quality),
        "fit_settings": _config_metadata(context.fit_settings),
    }


def _build_uvvis_run_metadata(
    context: UvvisPlanContext,
    rates: tuple[float, ...],
    supplied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge caller metadata before authoritative derived metadata."""
    metadata = deepcopy(dict(supplied or {}))
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
            "detectors": [_device_name(context.qepro)],
            "uvvis_config": _serialized_uvvis_config(context),
            "use_good_bad": context.quality.enabled,
        }
    )
    return metadata


def create_uvvis_plan(context: UvvisPlanContext) -> Callable[..., Any]:
    """Bind validated hardware once and return the Queue Server acquisition plan."""
    _validate_uvvis_context(context)
    volumes = VolumeTracker(context)  # checks loaded_ml; state kept across trials
    quality_signals = _new_quality_signals()

    def uvvis_acquire(
        suggestions: Sequence[Mapping[str, Any]],
        actuators: Sequence[Any],
        sensors: Sequence[Any] | None = None,
        md: Mapping[str, Any] | None = None,
    ):
        """Acquire one UV-Vis optimization run."""
        del actuators, sensors
        rates = _preflight(context, suggestions, plan_name="uvvis_acquire")
        run_metadata = _build_uvvis_run_metadata(context, rates, md)
        started: list[Any] = []

        def cleanup():
            yield from _cleanup_devices(context, started)

        def measure():
            yield from _measure_pl_with_quality_gate(context, quality_signals)
            yield from _measure_uvvis(
                context,
                "absorbance",
                context.quality.absorbance_shots,
            )
            yield from bps.mv(context.led, "Low", context.uv_shutter, "Low")

        def acquisition():
            yield from _run_pump_sequence_and_measure(
                context, rates, started, measure, volumes
            )

        plan = bpp.stage_wrapper(acquisition(), [context.qepro])
        plan = bpp.baseline_wrapper(
            plan, _unique_devices([source.pump for source in context.sources])
        )
        plan = _with_safe_cleanup(plan, cleanup)
        plan = bpp.run_wrapper(plan, md=run_metadata)
        plan = bpp.set_run_key_wrapper(plan, "uvvis_acquire")
        return (yield from plan)

    return uvvis_acquire
