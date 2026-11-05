"""Per-suggestion checks run before any device message is emitted."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from xpd_tools.optimization.helpers.beamline import (
    UvvisPlanContext,
    XrayPlanContext,
    XrayUvvisPlanContext,
)


def _preflight(
    context: XrayUvvisPlanContext | XrayPlanContext | UvvisPlanContext,
    suggestions: Sequence[Mapping[str, Any]],
    *,
    plan_name: str,
) -> tuple[float, ...]:
    """
    Validate one complete suggestion before emitting any device message.

    Args:
        - context: the plan context
        - suggestions: the suggestions to validate
        - plan_name: the name of the plan

    Returns
    -------
        - the validated suggestion as a tuple of rates
    """
    if len(suggestions) != 1 or not suggestions[0]:
        raise ValueError(f"{plan_name} requires exactly one nonempty suggestion")

    # Take only the first suggestion and validate it
    # At some point, we will support more than one suggestion
    suggestion = suggestions[0]
    configured_dofs = tuple(source.dof for source in context.sources)
    configured_set = set(configured_dofs)
    supplied_dofs = {name for name in suggestion if name.startswith("infusion_rate_")}
    if supplied_dofs != configured_set:
        raise ValueError(
            "suggestion infusion DOFs must match sources: "
            f"expected {sorted(configured_set)}, got {sorted(supplied_dofs)}"
        )

    # Check for unknown fields
    unknown_fields = sorted(set(suggestion) - configured_set - {"_id"})
    if unknown_fields:
        raise ValueError(f"suggestion has unknown fields: {', '.join(unknown_fields)}")

    rates = tuple(float(suggestion[dof]) for dof in configured_dofs)
    if any(not np.isfinite(rate) or rate < 0 for rate in rates):
        raise ValueError("infusion rates must be finite and non-negative")
    if not any(rate > 0 for rate in rates):
        raise ValueError("at least one infusion rate must be greater than zero")
    return rates
