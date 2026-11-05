"""Weighted-profile R-factor (Rw) between measured and reference G(r) profiles."""

import numpy as np
from numpy.typing import ArrayLike

from xpd_tools.optimization.scoring._shared import _mask_and_interpolate


def weighted_profile_r(
    r_exp: ArrayLike,
    g_exp: ArrayLike,
    r_sim: ArrayLike,
    g_sim: ArrayLike,
    r_min: float = 2.0,
    r_max: float = 20.0,
) -> float:
    """
    Weighted-profile R-factor (Rw).

    Rw = sqrt( sum(w_i * (g_obs,i - g_calc,i)**2) / sum(w_i * g_obs,i**2) )

    Unweighted here (w_i = 1). Both curves are L2-normalized first, so the
    denominator sum(g_obs,i**2) is always 1.

    Args:
        - r_exp: Measured G(r) radial grid.
        - g_exp: Measured G(r) values.
        - r_sim: Reference G(r) radial grid.
        - g_sim: Reference G(r) values.
        - r_min: The minimum r value to include in the calculation.
        - r_max: The maximum r value to include in the calculation.

    Returns
    -------
        - The weighted-profile R-factor (Rw).
    """
    _, g_slice_exp, g_sim_i = _mask_and_interpolate(
        r_exp, g_exp, r_sim, g_sim, r_min, r_max, scorer="weighted_profile_r"
    )

    # Normalize each curve to unit L2 norm, independently, BEFORE comparing
    g_slice_exp = g_slice_exp / np.linalg.norm(g_slice_exp)
    g_sim_i = g_sim_i / np.linalg.norm(g_sim_i)

    residual = g_slice_exp - g_sim_i
    rw = np.sqrt(np.sum(residual**2) / np.sum(g_slice_exp**2))

    return float(rw)
