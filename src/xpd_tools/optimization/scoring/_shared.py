"""Shared masking/interpolation/validation for the plain G(r) scorers."""

import numpy as np
from numpy.typing import ArrayLike


def _mask_and_interpolate(
    r_exp: ArrayLike,
    g_exp: ArrayLike,
    r_sim: ArrayLike,
    g_sim: ArrayLike,
    r_min: float,
    r_max: float,
    *,
    scorer: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Mask to [r_min, r_max] and interpolate the reference onto that grid.

    Args:
        - r_exp: Experimental r values.
        - g_exp: Experimental G(r) values.
        - r_sim: Simulated r values.
        - g_sim: Simulated G(r) values.
        - r_min: Minimum r value for masking.
        - r_max: Maximum r value for masking.
        - scorer: Name of the scorer calling this function.

    Returns
    -------
        - r_slice: Masked r values.
        - g_slice: Masked G(r) values.
        - g_sim_i: Interpolated G(r) values.
    """
    r_exp = np.asarray(r_exp, dtype=float)
    g_exp = np.asarray(g_exp, dtype=float)
    r_sim = np.asarray(r_sim, dtype=float)
    g_sim = np.asarray(g_sim, dtype=float)

    # Mask, by default between 2 and 20 A
    mask = (r_exp >= r_min) & (r_exp <= r_max)
    r_slice = r_exp[mask]
    g_slice = g_exp[mask]

    # Interpolate the reference onto the masked grid
    g_sim_i = np.interp(r_slice, r_sim, g_sim)

    if np.ptp(g_slice) == 0 or np.ptp(g_sim_i) == 0:
        raise ValueError(
            f"{scorer}: measured or reference G(r) is constant within "
            f"r_min={r_min}, r_max={r_max} -- correlation is undefined"
        )
    return r_slice, g_slice, g_sim_i
