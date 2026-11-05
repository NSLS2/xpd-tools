"""Pearson correlation coefficient between measured and reference G(r)."""

import numpy as np
from numpy.typing import ArrayLike

from xpd_tools.optimization.scoring._shared import _mask_and_interpolate


def pearson(
    r_exp: ArrayLike,
    g_exp: ArrayLike,
    r_sim: ArrayLike,
    g_sim: ArrayLike,
    r_min: float = 2.0,
    r_max: float = 20.0,
) -> float:
    """
    Pearson correlation coefficient between the measured and reference G(r).

    Args:
        - r_exp: Measured G(r) radial grid.
        - g_exp: Measured G(r) values.
        - r_sim: Reference G(r) radial grid.
        - g_sim: Reference G(r) values.
        - r_min: Minimum r value to consider.
        - r_max: Maximum r value to consider.

    Returns
    -------
        - Pearson correlation coefficient between the measured and reference G(r).
    """
    _, g_slice, g_sim_i = _mask_and_interpolate(
        r_exp, g_exp, r_sim, g_sim, r_min, r_max, scorer="pearson"
    )
    pearson_result = np.corrcoef(g_slice, g_sim_i)[0, 1]

    return float(pearson_result)
