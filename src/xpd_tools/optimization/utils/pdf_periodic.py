"""Periodic G(r) calculator."""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from pymatgen.core import Structure as PymatgenStructure

DR, DQ = 0.005, 0.01   # pair-histogram and Fourier-filter steps


@lru_cache(maxsize=2)
def _sine_transforms(
    rmin: float, rmax: float, rstep: float,
    qmin: float, qmax: float,
    r_pairs: float
):
    """r-grids and the G(r_fine) -> F(Q) -> G(r) matrices, cached."""
    r_fine = np.arange(DR / 2, r_pairs, DR)
    q = np.arange(qmin, qmax + 1e-9, DQ)
    r = np.arange(rmin, rmax + rstep / 2, rstep)
    to_q = np.sin(np.outer(q, r_fine)) * DR
    to_r = (2 / np.pi) * np.sin(np.outer(r, q)) * (q[1] - q[0])
    return r, r_fine, to_q, to_r


def _g(
    vecs: np.ndarray,
    w: np.ndarray,
    n_atoms: int,
    volume: float,
    eps: np.ndarray | None,
    rmin: float = 1.5,
    rmax: float = 30.0,
    rstep: float = 0.05,
    qmax: float = 18.0,
    qdamp: float = 0.032,
    u: float = 0.04,
    qmin: float = 0.0,
    pad: float = 10.0,
) -> tuple[np.ndarray, np.ndarray]:
    """G(r) from pair vectors; see pdf_periodic for the settings."""
    if eps is not None:
        deform = np.eye(3) + eps
        vecs, volume = vecs @ deform.T, volume * np.linalg.det(deform)
    r_pairs = rmax + pad
    pair_hist, _ = np.histogram(
        np.linalg.norm(vecs, axis=1),
        bins=np.arange(0.0, r_pairs + DR / 2, DR),
        weights=w
    )

    # pair width from two independent displacements
    sigma = np.sqrt(2 * u) if u > 0 else DR
    k = np.arange(-int(5 * sigma / DR), int(5 * sigma / DR) + 1) * DR
    kernel = np.exp(-0.5 * (k / sigma) ** 2)
    kernel /= kernel.sum()

    r, r_fine, to_q, to_r = _sine_transforms(rmin, rmax, rstep, qmin, qmax, r_pairs)

    g_fine = np.convolve(pair_hist, kernel, mode="same") / (n_atoms * DR) / r_fine \
        - 4 * np.pi * n_atoms / volume * r_fine
    g_fine *= np.clip((r_pairs - r_fine) / 5.0, 0.0, 1.0)

    # Fourier-filter to [qmin, qmax]: Qmax termination ripples
    g = to_r @ (to_q @ g_fine)
    return r, g * np.exp(-0.5 * (r * qdamp) ** 2)


def _pairs(structure: PymatgenStructure, r_max: float) -> tuple[np.ndarray, np.ndarray]:
    """Cartesian pair vectors within r_max and their Z_i Z_j / <Z>^2 X-ray weights."""
    z = np.array([site.specie.Z for site in structure], dtype=float)
    i, j, images, _ = structure.get_neighbor_list(r_max)
    vecs = structure.cart_coords[j] \
        + images @ structure.lattice.matrix \
        - structure.cart_coords[i]
    return vecs, z[i] * z[j] / z.mean() ** 2


def pdf_periodic(
    structure: PymatgenStructure,
    rmin: float = 1.5,
    rmax: float = 30.0,
    rstep: float = 0.05,
    qmax: float = 18.0,
    qdamp: float = 0.032,
    u: float = 0.04,
    qmin: float = 0.0,
    pad: float = 10.0,
    strain: float | np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute a periodic crystal's X-ray G(r) in pure numpy.

    Args:
        - struct: structure to compute PDF for
        - rmin: Minimum radius (A)
        - rmax: Maximum radius (A)
        - rstep: Radius step (A)
        - qmax: Maximum q for the termination filter (1/A)
        - qdamp: Q damping factor (instrument resolution envelope)
        - U: isotropic atomic displacement for every atom (A^2)
        - qmin: Minimum q for the termination filter (1/A)
        - pad: pairs are summed to rmax + pad (A); the last 5 A are
            tapered to limit truncation ripple
        - strain: small-strain tensor eps applied to the lattice
                as F = I + eps (cartesian x, y, z):
                    a float is isotropic,
                    a (3,) array the diagonal (eps_xx, eps_yy, eps_zz),
                    a (3, 3) array the full tensor
    Returns:
        - r: radial values, rmin to rmax inclusive
        - g: PDF values
    """
    eps = None
    if strain is not None:
        eps = np.asarray(strain, dtype=float)
        eps = eps if eps.shape == (3, 3) else np.diag(np.broadcast_to(eps, (3,)))
    # strain shortens a distance by at most shrink
    shrink = np.linalg.norm(eps, 2) if eps is not None else 0.0
    vecs, w = _pairs(structure, (rmax + pad) / (1 - shrink))
    return _g(vecs, w, len(structure), structure.volume, eps,
              rmin=rmin, rmax=rmax, rstep=rstep,
              qmin=qmin, qmax=qmax, qdamp=qdamp,
              u=u, pad=pad)
