"""Ensemble PDF goodness-of-fit scorer."""

import numpy as np
from numpy.typing import ArrayLike

from xpd_tools.optimization.scoring.cross_correlation_pdf import cross_correlation
from xpd_tools.optimization.scoring.nn_matrix_pdf import nn_matrix
from xpd_tools.optimization.scoring.pearson_pdf import pearson
from xpd_tools.optimization.scoring.weighted_profile_r_pdf import weighted_profile_r


class EnsembleGoodnessOfFitScorer:
    """Ensemble PDF goodness-of-fit scorer.

    Composite of pearson_pdf, nn_matrix_pdf, weighted_profile_r_pdf, and
    cross_correlation_similarity_pdf.
    """

    _LOWER_IS_BETTER = frozenset({"weighted_profile_r", "nn_matrix"})

    def __init__(self, weights: dict[str, float] | None = None):
        metric_names = (
            "pearson",
            "cross_correlation",
            "weighted_profile_r",
            "nn_matrix",
        )
        if weights:
            unknown = sorted(set(weights) - set(metric_names))
            if unknown:
                raise ValueError(
                    f"weights has unknown metric names: {', '.join(unknown)}"
                )
        # Merge over the defaults rather than replacing outright -- an
        # empty dict must mean "no overrides" (all-1.0), and a partial
        # dict must override only the named metrics, not drop the rest.
        self.weights: dict[str, float] = {
            **dict.fromkeys(metric_names, 1.0),
            **(weights or {}),
        }
        self._n: dict[str, int] = dict.fromkeys(metric_names, 0)
        self._mean: dict[str, float] = dict.fromkeys(metric_names, 0.0)
        # Welford's running sum-of-squared-deviations
        self._m2: dict[str, float] = dict.fromkeys(metric_names, 0.0)

    def _update_and_normalize(self, name: str, value: float) -> float:
        n = self._n[name] + 1
        delta = value - self._mean[name]
        mean = self._mean[name] + delta / n
        m2 = self._m2[name] + delta * (value - mean)
        self._n[name], self._mean[name], self._m2[name] = n, mean, m2

        if n < 2:
            return value  # not enough history yet to normalize against
        std = np.sqrt(m2 / (n - 1))
        return (value - mean) / std if std > 0 else 0.0

    def score(
        self,
        r_exp: ArrayLike,
        g_exp: ArrayLike,
        r_sim: ArrayLike,
        g_sim: ArrayLike,
        r_min: float = 2.0,
        r_max: float = 20.0,
    ) -> float:
        raw = {
            "pearson": pearson(r_exp, g_exp, r_sim, g_sim, r_min=r_min, r_max=r_max),
            "cross_correlation": cross_correlation(
                r_exp, g_exp, r_sim, g_sim, r_min=r_min, r_max=r_max
            ),
            "weighted_profile_r": weighted_profile_r(
                r_exp, g_exp, r_sim, g_sim, r_min=r_min, r_max=r_max
            ),
            "nn_matrix": nn_matrix(
                r_exp, g_exp, r_sim, g_sim, r_min=r_min, r_max=r_max
            ),
        }
        total = 0.0
        for name, value in raw.items():
            z = self._update_and_normalize(name, value)
            sign = -1.0 if name in self._LOWER_IS_BETTER else 1.0
            total += self.weights[name] * sign * z
        return float(total)
