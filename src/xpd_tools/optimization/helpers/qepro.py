"""QEPro fluorescence/absorbance evaluation: stream schema and PL/PLQY scoring."""

from __future__ import annotations

import logging
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from xpd_tools.optimization.analysis import (
    analyze_pl_spectra,
    calculate_plqy,
    correct_absorbance,
)
from xpd_tools.optimization.helpers.common import (
    _read_stream_dataset,
    _retry_access,
    _TiledAccessError,
)

logger = logging.getLogger(__name__)

_QEPRO_FIELDS = ("QEPro_x_axis", "QEPro_output")


@dataclass(frozen=True)
class PlqyReference:
    """Reference values used for relative PLQY calculation."""

    reference_type: str = "quinine"
    excitation_wavelength_nm: float = 365.0
    absorbance: float = 0.06
    pl_integral: float = 1.2e6
    refractive_index: float = 1.33
    plqy: float = 0.546
    solvent_refractive_index: float = 1.506


@dataclass(frozen=True, kw_only=True)
class SpectraFitSettings:
    """Screening, data-selection, and fitting windows for PL/absorbance spectra."""

    pl_screen_key_height: float = 200.0
    pl_screen_peak_height: float = 50.0
    pl_screen_peak_distance: int = 100
    pl_percent_range: tuple[float, float] = (40.0, 100.0)
    pl_wavelength_range: tuple[float, float] = (400.0, 800.0)
    pl_fit_maxfev: int = 100000
    pl_fit_r2_window_sigma: float = 3.0
    absorbance_percent_range: tuple[float, float] = (10.0, 70.0)
    absorbance_wavelength_range: tuple[float, float] = (210.0, 700.0)

    def __post_init__(self) -> None:
        """Store the range fields as tuples."""
        # Normalize to tuples regardless of what sequence type the caller
        # passed in -- see Pump.__post_init__'s comment for why this matters.
        for field in (
            "pl_percent_range",
            "pl_wavelength_range",
            "absorbance_percent_range",
            "absorbance_wavelength_range",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))


@dataclass(frozen=True, kw_only=True)
class QualityPolicy:
    """Fluorescence quality-gating and UV-Vis shot policy."""

    enabled: bool = True
    good_batches: int = 3
    max_bad_batches: int = 3
    absorbance_shots: int = 10
    fluorescence_shots: int = 10


def _read_qepro_stream(
    client: Any,
    uid: Hashable,
    stream_name: str,
) -> tuple[dict[str, np.ndarray], Mapping[str, Any]]:
    """Read a QEPro stream from Tiled, with retries on failure."""
    dataset, metadata = _read_stream_dataset(client, uid, stream_name)
    missing = [field for field in _QEPRO_FIELDS if field not in dataset]
    if missing:
        raise ValueError(
            f"QEPro stream {stream_name!r} is missing fields: {', '.join(missing)}"
        )
    return (
        {field: np.asarray(dataset[field].values) for field in _QEPRO_FIELDS},
        metadata,
    )


def _read_quality_stream(client: Any, uid: Hashable) -> list[dict[str, Any]]:
    """Read the fluorescence_quality stream from Tiled, with retries on failure."""
    dataset, _ = _read_stream_dataset(client, uid, "fluorescence_quality")
    required = ("verdict", "n_events_in_batch")
    missing = [field for field in required if field not in dataset]
    if missing:
        raise ValueError(
            "fluorescence_quality stream is missing fields: " + ", ".join(missing)
        )
    verdicts = np.asarray(dataset["verdict"].values)
    counts = np.asarray(dataset["n_events_in_batch"].values)
    if verdicts.shape != counts.shape:
        raise ValueError("fluorescence_quality verdict and event-count shapes differ")
    return [
        {"verdict": str(verdict), "n_events_in_batch": int(count)}
        for verdict, count in zip(verdicts, counts, strict=True)
    ]


def _filter_fl_to_good_batches(
    fluorescence: dict[str, np.ndarray],
    batch_info: Sequence[Mapping[str, Any]],
) -> dict[str, np.ndarray]:
    """
    Filter fluorescence events to good batches based on QEPro output and batch info.

    Args:
        - fluorescence: The fluorescence data to filter.
        - batch_info: The batch info to use for filtering.

    Returns
    -------
        - The filtered fluorescence events.
    """
    output = np.asarray(fluorescence["QEPro_output"])
    event_count = 1 if output.ndim == 1 else output.shape[0]
    counts = [int(batch["n_events_in_batch"]) for batch in batch_info]
    if any(count < 0 for count in counts) or sum(counts) != event_count:
        raise ValueError(
            "fluorescence quality batch counts must exactly partition "
            f"{event_count} events; received {counts}"
        )

    # Select indices of good batches
    selected_indices: list[int] = []
    cursor = 0
    for batch, count in zip(batch_info, counts, strict=True):
        if batch["verdict"] == "good":
            selected_indices.extend(range(cursor, cursor + count))
        cursor += count

    # If no good batches are found, use all events
    if not selected_indices:
        logger.warning(
            "No good PL batches found; using all %d fluorescence events",
            event_count,
        )
        return fluorescence

    # Return the filtered fluorescence events
    indices = np.asarray(selected_indices, dtype=np.intp)
    logger.info(
        "Keeping %d/%d fluorescence events from good batches",
        indices.size,
        event_count,
    )
    return {
        field: array[indices]
        if (array := np.asarray(values)).ndim >= 1 and array.shape[0] == event_count
        else array
        for field, values in fluorescence.items()
    }


def _read_tiled_data(
    tiled_client: Any,
    uid: Hashable,
    *,
    max_retries: int,
    retry_delay: float,
) -> tuple[
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    Mapping[str, Any],
    list[dict[str, Any]] | None,
]:
    """
    Read required raw QEPro streams, retaining successes between retries.

    Args:
        - tiled_client: The Tiled client to use for reading.
        - uid: The uid of the run to read.
        - max_retries: The maximum number of retries to perform.
        - retry_delay: The delay between retries, in seconds.

    Returns
    -------
        - A tuple of the read data, state, metadata, and batch info.
    """
    state: dict[str, Any] = {}

    def read() -> (
        tuple[
            dict[str, np.ndarray],
            dict[str, np.ndarray],
            Mapping[str, Any],
            list[dict[str, Any]] | None,
        ]
        | None
    ):
        """Read the fluorescence and absorbance streams from Tiled, with retries."""
        errors: list[_TiledAccessError] = []
        for key, stream_name in (
            ("fluorescence", "fluorescence"),
            ("absorbance", "absorbance"),
        ):
            if key in state:
                continue
            try:
                values, metadata = _read_qepro_stream(tiled_client, uid, stream_name)
            except _TiledAccessError as exc:
                errors.append(exc)
            else:
                state[key] = values
                state.setdefault("metadata", metadata)

        metadata = state.get("metadata")
        if metadata is not None:
            state["use_good_bad"] = bool(metadata.get("use_good_bad", False))
        if state.get("use_good_bad") and "quality" not in state:
            try:
                state["quality"] = _read_quality_stream(tiled_client, uid)
            except _TiledAccessError as exc:
                errors.append(exc)

        if (
            "fluorescence" in state
            and "absorbance" in state
            and "metadata" in state
            and "use_good_bad" in state
            and (not state["use_good_bad"] or "quality" in state)
        ):
            return (
                state["fluorescence"],
                state["absorbance"],
                state["metadata"],
                state.get("quality"),
            )
        if errors:
            raise errors[-1]
        return None

    def missing() -> list[str]:
        names: list[str] = []
        if "metadata" not in state:
            names.append("run metadata")
        if "fluorescence" not in state:
            names.append("fluorescence stream")
        if "absorbance" not in state:
            names.append("absorbance stream")
        if state.get("use_good_bad") and "quality" not in state:
            names.append("fluorescence_quality stream")
        return names

    return _retry_access(
        uid,
        "required Tiled data",
        read,
        missing,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )


def _compute_pl_outcomes(
    fluorescence: Mapping[str, np.ndarray],
    absorbance: Mapping[str, np.ndarray],
    plqy: PlqyReference,
    peak_target: float,
    *,
    uid: Hashable,
    fit_settings: SpectraFitSettings = SpectraFitSettings(),  # ruff:ignore[function-call-in-default-argument]
) -> dict[str, float]:
    """
    Fit PL/absorbance spectra and derive Peak/FWHM/PLQY outcomes.

    Args:
        - fluorescence: The fluorescence data to fit.
        - absorbance: The absorbance data to fit.
        - plqy: The PLQY reference to use.
        - peak_target: The target peak value to use for fitting.
        - uid: The uid of the run to use for logging.
        - fit_settings: The fit settings to use for fitting.

    Returns
    -------
        - A dictionary of the fit results.
    """
    # Fit the fluorescence spectrum
    pl_result = analyze_pl_spectra(
        fluorescence["QEPro_x_axis"],
        fluorescence["QEPro_output"],
        key_height=fit_settings.pl_screen_key_height,
        height=fit_settings.pl_screen_peak_height,
        distance=fit_settings.pl_screen_peak_distance,
        percent_range=fit_settings.pl_percent_range,
        wavelength_range=fit_settings.pl_wavelength_range,
        maxfev=fit_settings.pl_fit_maxfev,
        r2_window_sigma=fit_settings.pl_fit_r2_window_sigma,
    )
    # Fit the absorbance spectrum
    wavelength, corrected_absorbance = correct_absorbance(
        absorbance["QEPro_x_axis"],
        absorbance["QEPro_output"],
        percent_range=fit_settings.absorbance_percent_range,
        wavelength_range=fit_settings.absorbance_wavelength_range,
    )

    # If the PL fit fails, use penalty values to steer BLOP away
    if pl_result is None:
        peak = 0.0
        peak_distance = peak_target
        fwhm = 1000.0
        plqy_value = 1e-10
    else:
        # Fit the PLQY value if successful
        peak, fwhm, pl_integral, _r_squared = pl_result
        if not np.isfinite(peak):
            raise ValueError(f"fitted Peak is not finite for uid={uid!r}")
        if not np.isfinite(fwhm) or fwhm <= 0:
            raise ValueError(f"fitted FWHM is not positive and finite for uid={uid!r}")
        excitation_index = int(
            np.abs(wavelength - plqy.excitation_wavelength_nm).argmin()
        )
        # Calculate the PLQY value
        plqy_value = calculate_plqy(
            float(corrected_absorbance[excitation_index]),
            pl_integral,
            plqy.solvent_refractive_index,
            reference_type=plqy.reference_type,
            absorbance_reference=plqy.absorbance,
            pl_integral_reference=plqy.pl_integral,
            refractive_index_reference=plqy.refractive_index,
            plqy_reference=plqy.plqy,
        )
        peak_distance = abs(peak_target - peak)

    # If the PLQY fit fails, use penalty values to steer BLOP away
    if not np.isfinite(plqy_value) or plqy_value <= 0:
        plqy_value = 1e-10

    return {
        "Peak": float(peak),
        "peak_distance": float(peak_distance),
        "log_FWHM": float(np.log(fwhm)),
        "log_PLQY": float(np.log(plqy_value)),
    }
