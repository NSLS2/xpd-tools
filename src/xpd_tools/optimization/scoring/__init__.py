"""PDF scoring functions."""

from collections.abc import Callable

from xpd_tools.optimization.scoring.cnn_pdf import CnnScorer, build_cnn_scorer
from xpd_tools.optimization.scoring.cross_correlation_pdf import cross_correlation
from xpd_tools.optimization.scoring.ensemble_pdf import EnsembleGoodnessOfFitScorer
from xpd_tools.optimization.scoring.nn_matrix_pdf import nn_matrix
from xpd_tools.optimization.scoring.pearson_pdf import pearson
from xpd_tools.optimization.scoring.weighted_profile_r_pdf import weighted_profile_r

# scoring function lookup table; all take the same input structures
_SCORING_FUNCTIONS = {
    "pearson": pearson,
    "cross_correlation": cross_correlation,
    "nn_matrix": nn_matrix,
    "weighted_profile_r": weighted_profile_r,
}
# ensemble and cnn are scored separately, not through _SCORING_FUNCTIONS
_ALL_SCORING_NAMES = frozenset({*_SCORING_FUNCTIONS, "ensemble", "cnn"})

EnsembleScorers = dict[str, EnsembleGoodnessOfFitScorer]


def _resolve_scorer(
    scoring_function: str,
    phase_name: str,
    ensemble_scorers: EnsembleScorers
) -> Callable[[], float]:
    """Return the callable to score one phase's G(r) profile."""
    if scoring_function == "ensemble":
        return ensemble_scorers.setdefault(
            phase_name, EnsembleGoodnessOfFitScorer()
        ).score
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
