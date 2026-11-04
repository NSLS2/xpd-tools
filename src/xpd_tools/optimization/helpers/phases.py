"""Phase objective or constraint descriptions for BLOP optimization."""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ax.api.protocols import IMetric
from blop.ax import Objective, OutcomeConstraint


@dataclass(frozen=True, kw_only=True)
class Phase:
    """Phase structure to score against using PDF.

    role
    ----
    "objective"  (default) -- the phase metric becomes a Pareto objective.
                              `minimize` sets direction (False = maximize).
                              `objective_threshold` optionally pins the Pareto
                              reference point (worst value still worth counting).
    "constraint"           -- the metric is a feasibility gate only; no Pareto
                              dimension is added.  `bound` is required.
                              `minimize=True`  -> metric <= bound  (cap)
                              `minimize=False` -> metric >= bound  (floor)

    target_fraction (cnn sugar)
    ---------------------------
    Kept for backward compatibility.  When set, expands at build time to:
      - objective  frac_dist_{name}  (minimize squared distance to target)
      - constraint frac_{name} in [target - fraction_tolerance,
                                   target + fraction_tolerance]
    Incompatible with role="constraint" or bound/objective_threshold.
    """

    name: str
    gr: str
    cif: str
    minimize: bool = False
    # cnn only: full name as it appears in the trained CNN dictionary
    # (e.g. "CsPbBr3_Pnma_mp-567681"). When set, used for the CNN lookup
    # instead of name. Metric keys (frac_{name}) still use the short name.
    cnn_name: str | None = None
    # role / bound / objective_threshold -- see docstring above.
    role: Literal["objective", "constraint"] = "objective"
    bound: float | None = None
    objective_threshold: float | None = None
    # cnn sugar -- see docstring above.
    target_fraction: float | None = None
    fraction_tolerance: float = 0.05

    def __post_init__(self) -> None:
        """Error checking on BLOP objectives and constraints."""
        if self.fraction_tolerance <= 0:
            raise ValueError(f"phase {self.name!r}: fraction_tolerance must be > 0")

        if self.role == "constraint":
            if self.bound is None:
                raise ValueError(
                    f"phase {self.name!r}: role='constraint' requires bound to be set"
                )
            if self.objective_threshold is not None:
                raise ValueError(
                    f"phase {self.name!r}: \
                        objective_threshold is only valid for role='objective'"
                )
            if self.target_fraction is not None:
                raise ValueError(
                    f"phase {self.name!r}: \
                    target_fraction cannot be used with role='constraint'"
                )
        else:
            if self.bound is not None:
                raise ValueError(
                    f"phase {self.name!r}: bound is only valid for role='constraint'"
                )

        if self.target_fraction is not None:
            if not 0 <= self.target_fraction <= 1:
                raise ValueError(
                    f"phase {self.name!r}: target_fraction must be in [0, 1]"
                )
            if self.minimize:
                raise ValueError(
                    f"phase {self.name!r}: target_fraction sets its own direction; "
                    "drop minimize=True"
                )
            if self.objective_threshold is not None:
                raise ValueError(
                    f"phase {self.name!r}: \
                    objective_threshold cannot be combined with target_fraction"
                )


def _create_phase(phase: Phase, *, metric_prefix: str) -> Objective | None:
    """Build the Ax objective for one phase's PDF metric.

    Returns None for constraint-role phases (they produce no objective).

    `metric_prefix` must match the evaluator's actual output key
    (`"corr_"` for raw mode, `"pdf_fit_corr_"` for fit mode, `"frac_"` for
    cnn) -- the objective name has to match a real evaluation outcome exactly.
    A cnn phase with a target minimizes `frac_dist_{name}` instead.
    """
    if phase.role == "constraint":
        return None
    if metric_prefix == "frac_" and phase.target_fraction is not None:
        return Objective(name=f"frac_dist_{phase.name}", minimize=True)
    return Objective(
        name=f"{metric_prefix}{phase.name}",
        minimize=phase.minimize,
    )


def _phase_constraints(
    phase: Phase, *, metric_prefix: str
) -> tuple[OutcomeConstraint, ...]:
    """Build Ax outcome constraints for one phase.

    Covers three cases:
    - role='constraint'       -> one-sided gate (bound required)
    - role='objective' + objective_threshold -> Pareto ref-point constraint
    - target_fraction set     -> two-sided band (sugar for frac_dist path)
    """
    constraints: list[OutcomeConstraint] = []

    if phase.role == "constraint":
        op = "<=" if phase.minimize else ">="
        metric_name = f"{metric_prefix}{phase.name}"
        constraints.append(
            OutcomeConstraint(
                f"m {op} {phase.bound:g}",
                m=IMetric(name=metric_name),
            )
        )

    elif phase.objective_threshold is not None:
        # Pareto reference-point threshold written as an outcome constraint
        # on the objective metric; Ax interprets it as the HV ref point.
        op = "<=" if phase.minimize else ">="
        metric_name = f"{metric_prefix}{phase.name}"
        constraints.append(
            OutcomeConstraint(
                f"m {op} {phase.objective_threshold:g}",
                m=IMetric(name=metric_name),
            )
        )

    if metric_prefix == "frac_" and phase.target_fraction is not None:
        tol = phase.fraction_tolerance
        frac_metric = IMetric(name=f"frac_{phase.name}")
        constraints.append(
            OutcomeConstraint(
                f"f >= {phase.target_fraction - tol:g}", f=frac_metric
            )
        )
        constraints.append(
            OutcomeConstraint(
                f"f <= {phase.target_fraction + tol:g}", f=frac_metric
            )
        )

    return tuple(constraints)


def _phases_to_pdf_schema(
    phases: list[Phase], *, scoring_function: str | None = None
) -> dict[str, Any]:
    """Build a PDF reference schema dict from configured phases.

    Matches the JSON schema `helpers.pdf._load_pdf_references` parses.

    `gr`/`cif` are resolved to absolute paths.
    `scoring_function` is applied uniformly to every phase.
    """
    return {
        "schema_version": 1,
        "phases": [
            {
                "name": phase.name,
                "gr_path": str(Path(phase.gr).expanduser().resolve()),
                "cif_path": str(Path(phase.cif).expanduser().resolve()),
                "minimize": phase.minimize,
                **(
                    {}
                    if scoring_function is None
                    else {"scoring_function": scoring_function}
                ),
                **(
                    {}
                    if phase.cnn_name is None
                    else {"cnn_name": phase.cnn_name}
                ),
                **(
                    {}
                    if phase.role == "objective"
                    else {"role": phase.role}
                ),
                **(
                    {}
                    if phase.bound is None
                    else {"bound": phase.bound}
                ),
                **(
                    {}
                    if phase.objective_threshold is None
                    else {"objective_threshold": phase.objective_threshold}
                ),
                **(
                    {}
                    if phase.target_fraction is None
                    else {"target_fraction": phase.target_fraction}
                ),
            }
            for phase in phases
        ],
    }


def _write_pdf_references(
    phases: list[Phase], directory: Path, *, scoring_function: str | None = None
) -> Path:
    """Write `_phases_to_pdf_schema(phases)` as a JSON file inside `directory`."""
    path = directory / "pdf_references.json"
    path.write_text(
        json.dumps(_phases_to_pdf_schema(phases, scoring_function=scoring_function))
    )
    return path
