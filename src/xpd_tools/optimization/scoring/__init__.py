"""PDF scoring functions."""

from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any

from xpd_tools.optimization.scoring.cross_correlation_pdf import cross_correlation
from xpd_tools.optimization.scoring.pearson_pdf import pearson
from xpd_tools.optimization.scoring.weighted_profile_r_pdf import weighted_profile_r

if TYPE_CHECKING:
    from xpd_tools.optimization.scoring.cnn_pdf import CnnScorer, build_cnn_scorer
    from xpd_tools.optimization.scoring.ensemble_pdf import EnsembleGoodnessOfFitScorer
    from xpd_tools.optimization.scoring.nn_matrix_pdf import nn_matrix

# Loaded on first use, so the Queue Server worker can import the plans without
# torch (cnn) or scikit-learn (nn_matrix, ensemble).
_LAZY = {
    "CnnScorer": "cnn_pdf",
    "build_cnn_scorer": "cnn_pdf",
    "EnsembleGoodnessOfFitScorer": "ensemble_pdf",
    "nn_matrix": "nn_matrix_pdf",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        module = import_module(f"{__name__}.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _nn_matrix(*args: Any, **kwargs: Any) -> float:
    return __getattr__("nn_matrix")(*args, **kwargs)


# scoring function lookup table; all take the same input structures
_SCORING_FUNCTIONS = {
    "pearson": pearson,
    "cross_correlation": cross_correlation,
    "nn_matrix": _nn_matrix,
    "weighted_profile_r": weighted_profile_r,
}
# ensemble and cnn are scored separately, not through _SCORING_FUNCTIONS
_ALL_SCORING_NAMES = frozenset({*_SCORING_FUNCTIONS, "ensemble", "cnn"})

EnsembleScorers = dict[str, "EnsembleGoodnessOfFitScorer"]


def _resolve_scorer(
    scoring_function: str, phase_name: str, ensemble_scorers: EnsembleScorers
) -> Callable[..., float]:
    """Return the callable to score one phase's G(r) profile."""
    if scoring_function == "ensemble":
        if phase_name not in ensemble_scorers:
            ensemble_scorers[phase_name] = __getattr__("EnsembleGoodnessOfFitScorer")()
        return ensemble_scorers[phase_name].score
    return _SCORING_FUNCTIONS[scoring_function]


__all__ = [
    "CnnScorer",
    "EnsembleGoodnessOfFitScorer",
    "EnsembleScorers",
    "build_cnn_scorer",
    "cross_correlation",
    "nn_matrix",
    "pearson",
    "weighted_profile_r",
]
