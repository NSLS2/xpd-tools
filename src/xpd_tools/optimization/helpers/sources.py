"""Sources for flow control in the optimization workflow.

Each pump entry may set `loaded_ml` (volume filled into the syringe) to have the
plans check, before every trial, that it can supply the trial; `reserve_ml` is kept
back for dead volume. Without `loaded_ml` the pump isn't checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


def _check_volumes(item: Any) -> None:
    """Check loaded_ml fits the syringe and reserve_ml isn't negative.

    Checked at Load/Apply.
    """
    name = getattr(item.pump, "name", item.pump)
    if item.loaded_ml is not None and not 0 < item.loaded_ml <= item.syringe_ml:
        raise ValueError(
            f"{name}: loaded_ml must be > 0 and at most "
            f"syringe_ml ({item.syringe_ml}), "
            f"got {item.loaded_ml}"
        )
    if item.reserve_ml < 0:
        raise ValueError(f"{name}: reserve_ml can't be negative, got {item.reserve_ml}")


@dataclass(frozen=True, kw_only=True)
class FlowSource:
    """One precursor pump controlled by an optimization degree of freedom."""

    dof: str
    pump: Any
    precursor: str
    sample_label: str
    syringe_ml: float = 50.0
    material: str = "steel"
    target_ml: float = 30.0
    set_target: bool = True
    loaded_ml: float | None = None
    reserve_ml: float = 1.0

    def __post_init__(self) -> None:
        """Check the loaded and reserve volumes."""
        _check_volumes(self)


@dataclass(frozen=True, kw_only=True)
class DilutionStage:
    """A dilution pump started before or after mixer equilibration."""

    pump: Any
    ratio: float
    position: Literal["before_equilibrium", "after_equilibrium"]
    syringe_ml: float
    material: str
    target_ml: float
    set_target: bool = True
    wait_sec: float = 0.0
    loaded_ml: float | None = None
    reserve_ml: float = 1.0

    def __post_init__(self) -> None:
        """Check the loaded and reserve volumes."""
        _check_volumes(self)


@dataclass(frozen=True, kw_only=True)
class WashCycle:
    """One sequential wash-pump cycle."""

    pump: Any
    rate_ul_min: float = 500.0
    duration_sec: float = 60.0
    syringe_ml: float = 50.0
    material: str = "steel"
    target_ml: float = 30.0
    set_target: bool = False
    loaded_ml: float | None = None
    reserve_ml: float = 1.0

    def __post_init__(self) -> None:
        """Check the loaded and reserve volumes."""
        _check_volumes(self)
