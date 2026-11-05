"""UV-Vis-only optimization evaluation: PL/absorbance spectra, no PDF data."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import Any

from xpd_tools.optimization.helpers.qepro import (
    PlqyReference,
    SpectraFitSettings,
    _compute_pl_outcomes,
    _filter_fl_to_good_batches,
    _read_tiled_data,
)


class UvvisEvaluation:
    """Evaluate optical (PL/absorbance) spectra for one acquisition run."""

    def __init__(
        self,
        tiled_client: Any,
        *,
        plqy: PlqyReference = PlqyReference(),  # ruff:ignore[function-call-in-default-argument]
        peak_target: float = 660,
        max_retries: int = 10,
        retry_delay: float = 2.0,
        fit_settings: SpectraFitSettings = SpectraFitSettings(),  # ruff:ignore[function-call-in-default-argument]
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least one")
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")

        self.tiled_client = tiled_client
        self._plqy = plqy
        self._peak_target = peak_target
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._fit_settings = fit_settings

    @property
    def peak_target(self) -> float:
        """Target fluorescence peak wavelength in nanometers."""
        return self._peak_target

    def __call__(
        self,
        uid: Hashable,
        suggestions: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        """Evaluate a run's optical spectra and return outcomes per suggestion."""
        # Read tiled data for the given uid for UVVis
        fluorescence, absorbance, _metadata, batch_info = _read_tiled_data(
            self.tiled_client,
            uid,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
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
        return [{**outcomes, "_id": suggestion["_id"]} for suggestion in suggestions]
