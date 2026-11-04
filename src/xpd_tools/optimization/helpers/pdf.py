"""PDF (G(r)) evaluation: schema, config loading, and pdffit2 scoring."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from tiled.queries import Eq

from xpd_tools.optimization.analysis import pdf_profile
from xpd_tools.optimization.helpers.common import _retry_access, _TiledAccessError
from xpd_tools.optimization.pdffit import fit_pdf_correlations
from xpd_tools.optimization.scoring import (
    _ALL_SCORING_NAMES,
    CnnScorer,
    EnsembleScorers,
    _resolve_scorer,
)

logger = logging.getLogger(__name__)

_PHASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ROOT_FIELDS = frozenset({"schema_version", "phases"})
_PHASE_FIELDS = frozenset(
    {
        "name",
        "gr_path",
        "cif_path",
        "minimize",
        "constraint_profile",
        "scoring_function",
        "cnn_name",
        "role",
        "bound",
        "objective_threshold",
        "target_fraction",
    }
)
# gr_path is NOT in here -- it's required unless scoring_function == "cnn"
# (a cnn-scored phase has no reference .gr; it's scored by the trained
# model's dictionary instead). See the per-phase check in _load_pdf_references.
_PHASE_REQUIRED_FIELDS = frozenset({"name", "minimize"})


@dataclass(frozen=True)
class _PdfPhaseReference:
    """Validated external PDF inputs for one phase."""

    name: str
    minimize: bool
    gr_path: Path | None = None
    cif_path: Path | None = None
    constraint_profile: Literal["none", "cs_pb_br3"] = "none"
    scoring_function: Literal[
        "pearson",
        "cross_correlation",
        "nn_matrix",
        "weighted_profile_r",
        "ensemble",
        "cnn"
    ] = "pearson"
    cnn_name: str | None = None
    role: Literal["objective", "constraint"] = "objective"
    bound: float | None = None
    objective_threshold: float | None = None
    target_fraction: float | None = None


def _require_exact_fields(
    value: dict[str, Any],
    allowed: frozenset[str],
    required: frozenset[str],
    field: str,
) -> None:
    """Raise a ValueError if the value has unknown or missing fields."""
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ValueError(f"{field} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{field} is missing fields: {', '.join(missing)}")


def _reference_path(
    value: object,
    config_directory: Path,
    field: str,
    *,
    require_file: bool,
) -> Path:
    """Return the resolved path for the given value."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path string")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_directory / candidate
    if require_file and not candidate.is_file():
        raise ValueError(f"{field} is not a file: {candidate}")
    return candidate


def _load_pdf_references(path: str | Path) -> tuple[_PdfPhaseReference, ...]:
    """Load and validate version-1 external PDF phase references."""
    config_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {config_path}: {exc.msg}") from exc

    if not isinstance(document, dict):
        raise ValueError(f"configuration root must be an object: {config_path}")
    _require_exact_fields(document, _ROOT_FIELDS, _ROOT_FIELDS, "configuration")
    if type(document["schema_version"]) is not int:
        raise ValueError("schema_version must be the integer 1")
    if document["schema_version"] != 1:
        raise ValueError(
            f"schema_version is unsupported: {document['schema_version']!r}"
        )

    raw_phases = document["phases"]
    if not isinstance(raw_phases, list) or not raw_phases:
        raise ValueError("phases must be a non-empty list")

    names: set[str] = set()
    phases: list[_PdfPhaseReference] = []
    for index, raw_phase in enumerate(raw_phases):

        # Validate the phase object and extract its fields.
        field = f"phases[{index}]"
        if not isinstance(raw_phase, dict):
            raise ValueError(f"{field} must be an object")
        _require_exact_fields(
            raw_phase,
            _PHASE_FIELDS,
            _PHASE_REQUIRED_FIELDS,
            field,
        )

        # Extract and validate the phase name.
        name = raw_phase["name"]
        if not isinstance(name, str):
            raise ValueError(f"{field}.name must be a string")
        if not _PHASE_NAME.fullmatch(name):
            raise ValueError(
                f"{field}.name must match {_PHASE_NAME.pattern!r}: {name!r}"
            )
        if name in names:
            raise ValueError(f"{field}.name is duplicated: {name!r}")
        names.add(name)

        # Extract and validate the minimize flag.
        minimize = raw_phase["minimize"]
        if not isinstance(minimize, bool):
            raise ValueError(f"{field}.minimize must be a boolean")

        # Extract and validate the constraint profile.
        constraint_profile = raw_phase.get("constraint_profile", "none")
        if constraint_profile not in {"none", "cs_pb_br3"}:
            raise ValueError(
                f"{field}.constraint_profile is unsupported: {constraint_profile!r}"
            )

        # Extract and validate the scoring function.
        scoring_function = raw_phase.get("scoring_function", "pearson")
        if scoring_function not in _ALL_SCORING_NAMES:
            raise ValueError(
                f"{field}.scoring_function is unsupported: {scoring_function!r}"
            )

        # Extract and validate the GR path -- required unless this phase is
        # scored by "cnn", which has no reference .gr (see CnnScorer).
        if scoring_function == "cnn":
            gr_path = None
        else:
            if "gr_path" not in raw_phase:
                raise ValueError(
                    f"{field}.gr_path is required unless scoring_function is 'cnn'"
                )
            gr_path = _reference_path(
                raw_phase["gr_path"],
                config_path.parent,
                f"{field}.gr_path",
                require_file=True,
            )

        # Extract and validate the cnn_name (cnn only -- overrides name for
        # CNN dictionary lookup; metric keys still use the short name).
        cnn_name = raw_phase.get("cnn_name")
        if cnn_name is not None:
            if scoring_function != "cnn":
                raise ValueError(f"{field}.cnn_name requires scoring_function 'cnn'")
            if not isinstance(cnn_name, str) or not cnn_name:
                raise ValueError(f"{field}.cnn_name must be a non-empty string")

        # Extract and validate role / bound / objective_threshold.
        role = raw_phase.get("role", "objective")
        if role not in ("objective", "constraint"):
            raise ValueError(
                f"{field}.role must be 'objective' or 'constraint', got {role!r}"
            )
        bound = raw_phase.get("bound")
        if bound is not None and not isinstance(bound, (int, float)):
            raise ValueError(f"{field}.bound must be a number")
        objective_threshold = raw_phase.get("objective_threshold")
        if objective_threshold is not None and \
            not isinstance(objective_threshold, (int, float)):
            raise ValueError(f"{field}.objective_threshold must be a number")
        if role == "constraint":
            if bound is None:
                raise ValueError(f"{field}: role='constraint' requires bound to be set")
            if objective_threshold is not None:
                raise ValueError(
                    f"{field}: objective_threshold is only valid for role='objective'"
                )
        else:
            if bound is not None:
                raise ValueError(f"{field}: bound is only valid for role='constraint'")

        # Extract and validate the target fraction (cnn only -- it's the
        # only scorer that predicts phase fractions).
        target_fraction = raw_phase.get("target_fraction")
        if target_fraction is not None:
            if scoring_function != "cnn":
                raise ValueError(
                    f"{field}.target_fraction requires scoring_function 'cnn'"
                )
            if (
                isinstance(target_fraction, bool)
                or not isinstance(target_fraction, (int, float))
                or not 0 <= target_fraction <= 1
            ):
                raise ValueError(f"{field}.target_fraction must be a number in [0, 1]")
            if minimize:
                raise ValueError(
                    f"{field}.target_fraction sets its own direction; \
                    minimize must be false"
                )

        # Extract and validate the CIF path.
        cif_value = raw_phase.get("cif_path")
        cif_path = (
            None
            if cif_value is None
            else _reference_path(
                cif_value,
                config_path.parent,
                f"{field}.cif_path",
                require_file=False,
            )
        )

        # Append the phase reference to the list.
        phases.append(
            _PdfPhaseReference(
                name=name,
                gr_path=gr_path,
                minimize=minimize,
                cif_path=cif_path,
                constraint_profile=cast(
                    Literal["none", "cs_pb_br3"], constraint_profile
                ),
                scoring_function=cast(
                    Literal[
                        "pearson",
                        "cross_correlation",
                        "nn_matrix",
                        "weighted_profile_r",
                        "ensemble",
                        "cnn",
                    ],
                    scoring_function,
                ),
                cnn_name=cnn_name,
                role=cast(Literal["objective", "constraint"], role),
                bound=bound,
                objective_threshold=objective_threshold,
                target_fraction=target_fraction,
            )
        )

    # The cnn's softmax spans its whole dictionary, so targets can sum to
    # less than 1 (the rest is untargeted phases) but never more.
    total = sum(phase.target_fraction or 0.0 for phase in phases)
    if total > 1 + 1e-6:
        raise ValueError(f"target_fraction values sum to {total:g}; must be <= 1")
    return tuple(phases)


def _read_pdfstream_data(
    sandbox_client: Any,
    uid: Hashable,
    *,
    max_retries: int,
    retry_delay: float,
) -> dict[str, np.ndarray]:
    """Read the correlated pdfstream G(r) stream with bounded retries."""

    def read() -> dict[str, np.ndarray]:
        try:
            matches = sandbox_client.search(Eq("start.original_run_uid", uid))
            key = matches.keys().last()
            dataset = matches[key]["scattering"].read()
        except Exception as exc:
            raise _TiledAccessError(
                f"failed to read pdfstream data for uid={uid!r}"
            ) from exc
        missing = [field for field in ("gr_r", "gr_G") if field not in dataset]
        if missing:
            raise ValueError(
                "scattering stream is missing fields: " + ", ".join(missing)
            )
        return {
            field: np.asarray(dataset[field].values).squeeze()
            for field in ("gr_r", "gr_G")
        }

    return _retry_access(
        uid,
        "pdfstream scattering data",
        read,
        lambda: ("scattering stream",),
        max_retries=max_retries,
        retry_delay=retry_delay,
    )


@cache
def _load_reference_gr(gr_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load and cache one phase's reference G(r).

    Reference files are validated once up front and held in
    cache to prevent repeated I/O for data.
    """
    r, g = np.loadtxt(gr_path, usecols=(0, 1), unpack=True)
    return r, g


def _raw_pdf_correlations(
    phases: Sequence[_PdfPhaseReference],
    pdf_data: Mapping[str, np.ndarray],
    *,
    r_min: float = 2.0,
    r_max: float = 20.0,
    ensemble_scorers: EnsembleScorers,
    cnn_scorer: CnnScorer | None = None,
) -> dict[str, float]:
    """
    Compute the raw PDF correlations for each phase.

    Args:
        - phases: Sequence of _pdfreference objects.
        - pdf_data: Mapping of phase names to PDF data.
        - r_min: Minimum radius for PDF correlation.
        - r_max: Maximum radius for PDF correlation.
        - ensemble_scorers: Mapping of ensemble scorers.
        - cnn_scorer: Built CnnScorer.

    Returns
    -------
        - results: corr_{name} per correlation-scored phase; for cnn phases
          frac_{name} (predicted fraction) and, with a target,
          frac_dist_{name} (squared distance to it).
    """
    results: dict[str, float] = {}

    cnn_phases = [phase for phase in phases if phase.scoring_function == "cnn"]
    if cnn_phases:
        if cnn_scorer is None:
            raise ValueError(
                "phases include scoring_function='cnn' but no cnn_scorer was built "
                "(pass dataset_path/weights_path when constructing the evaluator)"
            )
        cnn_scores = cnn_scorer.score(
            pdf_data["gr_r"],
            pdf_data["gr_G"],
            phase_names=[phase.cnn_name or phase.name for phase in cnn_phases],
            r_min=r_min,
            r_max=r_max,
        )
        for phase in cnn_phases:
            fraction = cnn_scores[phase.cnn_name or phase.name]
            results[f"frac_{phase.name}"] = fraction
            # Squared, not abs: smooth at the target, so the GP models it well.
            if phase.target_fraction is not None:
                results[f"frac_dist_{phase.name}"] = \
                (fraction - phase.target_fraction) ** 2

    for phase in phases:
        if phase.scoring_function == "cnn":
            continue
        reference_r, reference_g = _load_reference_gr(phase.gr_path)
        results[f"corr_{phase.name}"] = pdf_profile(
            pdf_data["gr_r"],
            pdf_data["gr_G"],
            reference_r,
            reference_g,
            r_min=r_min,
            r_max=r_max,
            function=_resolve_scorer(
                phase.scoring_function,
                phase.name,
                ensemble_scorers
            ),
        )
    return results


def _process_pdf(
    phases: Sequence[_PdfPhaseReference],
    pdf_data: Mapping[str, np.ndarray],
    pdf_mode: Literal["raw", "fit", "raw_tracked"],
    *,
    uid: Hashable,
    r_min: float = 2.0,
    r_max: float = 20.0,
    raw_ensemble_scorers: EnsembleScorers,
    fit_ensemble_scorers: EnsembleScorers,
    cnn_scorer: CnnScorer | None = None,
) -> dict[str, float]:
    """
    Process the PDF and return the raw or fit correlation results.

    Args:
        - phases: Sequence of `_PdfPhaseReference` objects.
        - pdf_data: Mapping of phase names to PDF data.
        - pdf_mode: Mode of PDF processing, either "raw", "fit", or "raw_tracked".
        - uid: Hashable identifier for the PDF processing.
        - r_min: Minimum radius for PDF correlation.
        - r_max: Maximum radius for PDF correlation.
        - raw_ensemble_scorers: Mapping of raw ensemble scorers.
        - fit_ensemble_scorers: Mapping of fit ensemble scorers.
        - cnn_scorer: Built CnnScorer, required if any phase uses scoring_function
          "cnn".

    Returns
    -------
        - results: Dictionary of phase names to raw or fit PDF correlation values.
    """
    results = _raw_pdf_correlations(
        phases, pdf_data, r_min=r_min, r_max=r_max,
        ensemble_scorers=raw_ensemble_scorers, cnn_scorer=cnn_scorer,
    )

    # Do fit and processing on the Raw PDF from the beamline
    if pdf_mode == "raw":
        return results

    # Do fit and processing on the Raw PDF from the beamline, with tracking
    # In this case the fit is not an objective; the fit does not block
    # evaluation; fall back to raw-only silently (besides the warning).
    if pdf_mode == "raw_tracked":
        try:
            results.update(
                fit_pdf_correlations(
                    phases,
                    pdf_data,
                    r_min=r_min,
                    r_max=r_max,
                    ensemble_scorers=fit_ensemble_scorers,
                )
            )
        except Exception:
            logger.warning(
                "PDF fitting failed for uid=%r; continuing with raw PDF "
                "correlations only.",
                uid,
                exc_info=True,
            )
        return results

    # Do a fit. Does not consider raw information on correlations.
    # The refined correlation is the objective, so a
    # refinement failure must be fatal rather than silently degraded.
    try:
        results.update(
            fit_pdf_correlations(
                phases,
                pdf_data,
                r_min=r_min,
                r_max=r_max,
                ensemble_scorers=fit_ensemble_scorers,
            )
        )
    except Exception as exc:
        raise RuntimeError(f"PDF fitting failed for uid={uid!r}") from exc
    return results
