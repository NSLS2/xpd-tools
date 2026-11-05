"""X-ray and UV-Vis optimization evaluation."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np

from xpd_tools.optimization.helpers.pdf import (
    _load_pdf_references,
    _PdfPhaseReference,
    _process_pdf,
    _read_pdfstream_data,
)
from xpd_tools.optimization.helpers.qepro import (
    PlqyReference,
    SpectraFitSettings,
    _compute_pl_outcomes,
    _filter_fl_to_good_batches,
    _read_tiled_data,
)
from xpd_tools.optimization.scoring import CnnScorer, EnsembleScorers, build_cnn_scorer


class XrayUvvisEvaluation:
    """Evaluate optical spectra and PDF data for one acquisition run."""

    def __init__(
        self,
        tiled_client: Any,
        sandbox_client: Any,
        pdf_references: str | Path,
        *,
        pdf_mode: Literal["raw", "fit", "raw_tracked"] = "fit",
        plqy: PlqyReference = PlqyReference(),  # ruff:ignore[function-call-in-default-argument]
        peak_target: float = 660,
        uvvis_max_retries: int = 10,
        uvvis_retry_delay: float = 2.0,
        xray_max_retries: int = 10,
        xray_retry_delay: float = 2.0,
        r_min: float = 2.0,
        r_max: float = 20.0,
        fit_settings: SpectraFitSettings = SpectraFitSettings(),  # ruff:ignore[function-call-in-default-argument]
        cnn_dataset_path: str | Path | None = None,
        cnn_weights_path: str | Path | None = None,
    ) -> None:
        if pdf_mode not in {"raw", "fit", "raw_tracked"}:
            raise ValueError("pdf_mode must be 'raw', 'fit', or 'raw_tracked'")
        if uvvis_max_retries < 1:
            raise ValueError("uvvis_max_retries must be at least one")
        if uvvis_retry_delay < 0:
            raise ValueError("uvvis_retry_delay cannot be negative")
        if xray_max_retries < 1:
            raise ValueError("xray_max_retries must be at least one")
        if xray_retry_delay < 0:
            raise ValueError("xray_retry_delay cannot be negative")

        phases = _load_pdf_references(pdf_references)
        if pdf_mode == "fit":
            for phase in phases:
                # Strict "fit" mode makes every phase's objective the
                # PDFfit2-refined correlation (pdf_fit_corr_{name}) -- cnn
                # has no refined curve to score, so there's no meaningful
                # objective value for it in this mode. raw_tracked is fine:
                # its objective stays the raw correlation regardless.
                if phase.scoring_function == "cnn":
                    raise ValueError(
                        f"phase {phase.name!r} uses scoring_function='cnn', which "
                        "isn't supported in PDF mode 'fit' (use 'raw' or 'raw_tracked')"
                    )
                if phase.cif_path is None:
                    raise ValueError(
                        f"phase {phase.name!r} requires cif_path for PDF mode 'fit'"
                    )
                if not phase.cif_path.is_file():
                    raise ValueError(
                        f"phase {phase.name!r} cif_path is not a file: {phase.cif_path}"
                    )

        # Build the cnn scorer once, eagerly, here at setup -- so a missing
        # or broken model fails now, not mid-experiment on the first
        # spectrum. See scoring.build_cnn_scorer.
        cnn_scorer: CnnScorer | None = None
        if any(phase.scoring_function == "cnn" for phase in phases):
            if cnn_dataset_path is None or cnn_weights_path is None:
                raise ValueError(
                    "phases include scoring_function='cnn' but cnn_dataset_path/"
                    "cnn_weights_path were not given"
                )
            cnn_scorer = build_cnn_scorer(str(cnn_dataset_path), str(cnn_weights_path))
        self._cnn_scorer = cnn_scorer

        self.tiled_client = tiled_client
        self.sandbox_client = sandbox_client
        self._pdf_mode: Literal["raw", "fit", "raw_tracked"] = pdf_mode
        self._phases = phases
        self._plqy = plqy
        self._peak_target = peak_target
        self._uvvis_max_retries = uvvis_max_retries
        self._uvvis_retry_delay = uvvis_retry_delay
        self._xray_max_retries = xray_max_retries
        self._xray_retry_delay = xray_retry_delay
        self._r_min = r_min
        self._r_max = r_max
        self._fit_settings = fit_settings
        # Persistent per-phase "ensemble" scorer state -- separate for raw
        # vs. fit mode since their score distributions differ. See
        # scoring._resolve_scorer.
        self._raw_ensemble_scorers: EnsembleScorers = {}
        self._fit_ensemble_scorers: EnsembleScorers = {}

    @property
    def pdf_mode(self) -> Literal["raw", "fit", "raw_tracked"]:
        """PDF metrics used as optimization objectives."""
        return self._pdf_mode

    @property
    def phases(self) -> tuple[_PdfPhaseReference, ...]:
        """Validated PDF phase references in configured order."""
        return self._phases

    @property
    def peak_target(self) -> float:
        """Target fluorescence peak wavelength in nanometers."""
        return self._peak_target

    def __call__(
        self,
        uid: Hashable,
        suggestions: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        """Evaluate a run and return finite outcomes for each suggestion."""
        # Single-suggestion restriction: outcomes are computed once per
        # acquisition, so >1 suggestion would silently receive identical
        # values. Revisit if batched suggestions per run are ever needed.
        if len(suggestions) > 1:
            raise RuntimeError(
                f"More than 1 suggestion is not supported, got: {len(suggestions)}"
            )

        # Read tiled data for the given uid
        fluorescence, absorbance, _metadata, batch_info = _read_tiled_data(
            self.tiled_client,
            uid,
            max_retries=self._uvvis_max_retries,
            retry_delay=self._uvvis_retry_delay,
        )

        # Filter fluorescence to good batches if batch_info is available
        if batch_info is not None:
            fluorescence = _filter_fl_to_good_batches(fluorescence, batch_info)

        # Compute Photoluminescence outcomes
        outcomes = _compute_pl_outcomes(
            fluorescence,
            absorbance,
            self._plqy,
            self._peak_target,
            uid=uid,
            fit_settings=self._fit_settings,
        )

        # Read PDF data from PDFsteam
        pdf_data = _read_pdfstream_data(
            self.sandbox_client,
            uid,
            max_retries=self._xray_max_retries,
            retry_delay=self._xray_retry_delay,
        )

        # Process PDF data and return the PDF scores
        # pdf_metrics: dict[str, float] for each PDF phase
        pdf_metrics = _process_pdf(
            self._phases,
            pdf_data,
            self._pdf_mode,
            uid=uid,
            r_min=self._r_min,
            r_max=self._r_max,
            raw_ensemble_scorers=self._raw_ensemble_scorers,
            fit_ensemble_scorers=self._fit_ensemble_scorers,
            cnn_scorer=self._cnn_scorer,
        )

        # Check that all PDF metrics are finite
        for name, value in pdf_metrics.items():
            if not np.isfinite(value):
                raise ValueError(f"PDF correlation {name!r} is not finite")

        # Update outcomes with PDF metrics
        outcomes.update(pdf_metrics)
        return [{**outcomes, "_id": suggestion["_id"]} for suggestion in suggestions]
