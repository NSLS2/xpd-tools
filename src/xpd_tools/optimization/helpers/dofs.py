"""Degrees of freedom (pumps) the optimizer varies."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from bluesky_adaptive.ax import RangeDOF


@dataclass(frozen=True, kw_only=True)
class Pump:
    """Pump infuser for experimental process."""

    name: str
    id: str
    bounds: tuple[float, float]
    parameter_type: Literal["float", "int"] = "float"

    def __post_init__(self) -> None:
        """Store bounds as a tuple."""
        # Normalize to a tuple regardless of what sequence type the caller
        # passed in -- keeps a hand-built Pump and a from_config()-rebuilt
        # one equal regardless of which the caller used.
        object.__setattr__(self, "bounds", tuple(self.bounds))
        # Infusion rates: two finite, non-negative numbers, lower below upper.
        numbers = all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
            for v in self.bounds
        )
        if not (
            numbers and len(self.bounds) == 2 and 0 <= self.bounds[0] < self.bounds[1]
        ):
            raise ValueError(
                f"pump {self.name!r}: bounds must be [lower, upper] rates with "
                f"0 <= lower < upper, got {list(self.bounds)}"
            )


def _create_pump(pump: Pump) -> RangeDOF:
    return RangeDOF(
        name=f"infusion_rate_{pump.name}",
        bounds=pump.bounds,
        parameter_type=pump.parameter_type,
    )
