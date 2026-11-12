"""Top phase-match confidence from the trained autoencoder-pdf encoder."""

from __future__ import annotations

import os
import warnings
from typing import TYPE_CHECKING

import numpy as np
import torch
from numpy.typing import ArrayLike

from xpd_tools.optimization.helpers.common import pdfstream_str_to_1d

if TYPE_CHECKING:
    # Only needed by type checkers; autoencoder-pdf is not yet on PyPI so we
    # never import it at module load time.  The real import is deferred to
    # build_cnn_scorer() where the package is actually required.
    from autoencoder_pdf.training.conc_encoder import ConcEncoder


class CnnScorer:
    """A loaded autoencoder-pdf encoder scorer."""

    def __init__(
        self,
        encoder: ConcEncoder,
        r_dict: np.ndarray,
        names: list[str]
    ) -> None:
        self._encoder = encoder
        self._r_dict = r_dict
        self._names = names

    def score(
        self,
        r_exp: ArrayLike,
        g_exp: ArrayLike,
        phase_names: list[str],
        r_min: float = 2.0,
        r_max: float = 20.0,
    ) -> dict[str, float]:
        """
        Predicted fraction for each requested phase name.

        Args:
            - r_exp: Measured G(r) radial grid.
            - g_exp: Measured G(r) values.
            - phase_names: Which of the model's dictionary phases to return scores for.
            - r_min: Minimum r value the measured data should cover (warns if not).
            - r_max: Maximum r value the measured data should cover (warns if not).

        Returns
        -------
            - {phase_name: predicted fraction} for each requested name.
        """
        r_exp = pdfstream_str_to_1d(r_exp)
        g_exp = pdfstream_str_to_1d(g_exp)

        if r_exp.min() > r_min or r_exp.max() < r_max:
            warnings.warn(
                f"cnn: measured r-range {r_exp.min():.2f}-{r_exp.max():.2f} \
                    does not cover "
                f"r_min={r_min}, r_max={r_max}; out-of-range r is zero-filled.",
                stacklevel=2,
            )

        unknown = sorted(set(phase_names) - set(self._names))
        if unknown:
            raise ValueError(
                f"cnn: phase names not in the trained dictionary: {', '.join(unknown)}"
            )

        interp_g = np.interp(
            self._r_dict,
            r_exp,
            g_exp,
            left=0.0,
            right=0.0).astype("float32")

        x = torch.tensor(interp_g)[None]
        x = x / (x.norm(dim=-1, keepdim=True) + 1e-6)
        with torch.no_grad():
            pred = self._encoder(x)[0].numpy()

        return {name: float(pred[self._names.index(name)]) for name in phase_names}


def build_cnn_scorer(dataset_path: str, weights_path: str) -> CnnScorer:
    """
    Load and validate the trained dataset + encoder weights.

    Args:
        - dataset_path: Path to the trained dataset_pc.npz.
        - weights_path: Path to the trained amortized_encoder.pt.

    Returns
    -------
        - A CnnScorer ready for repeated .score(...) calls.
    """
    # Deferred import: autoencoder-pdf is not on PyPI yet, so we only require
    # it here (at call time) rather than at module import time.
    try:
        from autoencoder_pdf.training.conc_encoder import ConcEncoder
    except ImportError as exc:
        raise ImportError(
            "objective_function='cnn' requires the 'autoencoder-pdf' package, "
            "which is not yet available on PyPI.  Install it manually or via the "
            "'cnn' optional-dependency group: pip install 'xpd-tools[cnn]'."
        ) from exc

    if not dataset_path or not os.path.isfile(dataset_path):
        raise ValueError(f"cnn scorer: dataset_path not set \
                or not found: {dataset_path!r}")
    if not weights_path or not os.path.isfile(weights_path):
        raise ValueError(f"cnn scorer: weights_path not set \
                or not found: {weights_path!r}")

    try:
        data = np.load(dataset_path, allow_pickle=True)
        r_dict = data["r"].astype("float32")
    except Exception as exc:
        raise ValueError(f"cnn scorer: failed to \
            load dataset_path {dataset_path!r}: {exc}") from exc

    try:
        ckpt = torch.load(weights_path, map_location="cpu", weights_only=False)
        names = list(ckpt["names"])
    except Exception as exc:
        raise ValueError(f"cnn scorer: failed to \
            load weights_path {weights_path!r}: {exc}") from exc

    try:
        encoder = ConcEncoder.from_checkpoint(ckpt, n_r=len(r_dict))
    except Exception as exc:
        raise ValueError(
            f"cnn scorer: weights at {weights_path!r} don't match \
                the ConcEncoder architecture: {exc}"
        ) from exc

    return CnnScorer(encoder, r_dict, names)
