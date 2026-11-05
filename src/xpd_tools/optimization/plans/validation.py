"""Static configuration validation, run once when a plan is constructed."""

from __future__ import annotations

import numpy as np

from xpd_tools.optimization.helpers.beamline import (
    XrayPlanContext,
    XrayUvvisPlanContext,
)


def _require_finite_nonnegative(value: float, field: str) -> None:
    if not np.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


def _validate_context(context: XrayUvvisPlanContext | XrayPlanContext) -> None:
    """Validate static process configuration before constructing a plan."""
    if context.xray_detector is None:
        raise ValueError("xray_detector is required")
    if context.wrap_xray_run is None:
        raise ValueError("wrap_xray_run is required")
    if not context.sources:
        raise ValueError("sources must contain at least one flow source")

    dofs = [source.dof for source in context.sources]
    if any(not dof or not dof.startswith("infusion_rate_") for dof in dofs):
        raise ValueError("source DOFs must be nonempty infusion_rate_* names")
    if len(set(dofs)) != len(dofs):
        raise ValueError("source DOFs must be unique")

    if len(context.mixer_lengths_cm) not in {1, 2}:
        raise ValueError("mixer_lengths_cm must contain one or two values")
    for index, length in enumerate(context.mixer_lengths_cm):
        _require_finite_nonnegative(length, f"mixer_lengths_cm[{index}]")
    _require_finite_nonnegative(context.residence_time_ratio, "residence_time_ratio")

    for index, stage in enumerate(context.dilutions):
        if stage.position not in {"before_equilibrium", "after_equilibrium"}:
            raise ValueError(f"dilutions[{index}].position is unsupported")
        _require_finite_nonnegative(stage.ratio, f"dilutions[{index}].ratio")
        _require_finite_nonnegative(stage.wait_sec, f"dilutions[{index}].wait_sec")
    for index, cycle in enumerate(context.wash_cycles):
        _require_finite_nonnegative(
            cycle.rate_ul_min, f"wash_cycles[{index}].rate_ul_min"
        )
        _require_finite_nonnegative(
            cycle.duration_sec, f"wash_cycles[{index}].duration_sec"
        )

    if context.quality.good_batches < 0 or context.quality.max_bad_batches < 0:
        raise ValueError("quality batch limits must be non-negative")
    if context.quality.absorbance_shots < 1 or context.quality.fluorescence_shots < 1:
        raise ValueError("quality shot counts must be positive")
    if not np.isfinite(context.xray.exposure) or context.xray.exposure <= 0:
        raise ValueError("xray.exposure must be positive and finite")
    if not np.isfinite(context.xray.frame_acq_time) or context.xray.frame_acq_time <= 0:
        raise ValueError("xray.frame_acq_time must be positive and finite")
