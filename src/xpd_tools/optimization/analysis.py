"""Numerical analysis for X-ray and UV-Vis optimization."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import integrate
from scipy.optimize import curve_fit
from scipy.signal import find_peaks

from xpd_tools.optimization.scoring import pearson


def _nearest_index(values: NDArray[np.float64], target: float) -> int:
    return int(np.abs(values - target).argmin())


def classify_pl(
    wavelength: ArrayLike,
    intensity: ArrayLike,
    *,
    key_height: float = 2000,
    height: float = 30,
    distance: int = 30,
) -> tuple[bool, float]:
    """
    Return whether a spectrum has a strong non-LED peak and its wavelength.

    Args:
        - wavelength: The wavelength values of the spectrum.
        - intensity: The intensity values of the spectrum.
        - key_height: The minimum height of a peak to be considered a strong non-LED
          peak.
        - height: The minimum height of a peak to be considered a peak.
        - distance: The minimum distance between peaks to be considered separate peaks.

    Returns:
        - has_peak: bool - Whether the spectrum has a strong non-LED peak.
        - wavelength: float - The wavelength of the strong non-LED peak.
    """
    x = np.asarray(wavelength, dtype=float)
    y = np.asarray(intensity, dtype=float)
    if x.ndim != 1 or y.ndim != 1 or x.shape != y.shape:
        raise ValueError("wavelength and intensity must be equal-length 1D arrays")

    peaks, properties = find_peaks(y, height=height, distance=distance)
    pl_mask = x[peaks] >= 400
    pl_peaks = peaks[pl_mask]
    if pl_peaks.size == 0:
        return False, float("nan")

    pl_heights = properties["peak_heights"][pl_mask]
    strongest = int(np.argmax(pl_heights))
    return bool(pl_heights[strongest] >= key_height), float(x[pl_peaks[strongest]])


def _prepare_spectra(
    wavelength: ArrayLike,
    spectra: ArrayLike,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """
    Prepare the wavelength and spectra for analysis.

    Args:
        - wavelength: The wavelength values of the spectrum.
        - spectra: The intensity values of the spectrum.

    Returns:
        - The wavelength and spectra values
    """
    values = np.asarray(spectra, dtype=float)
    if values.ndim == 1:
        values = values[np.newaxis, :]
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise ValueError("spectra must contain at least one one-dimensional event")

    wavelengths = np.asarray(wavelength, dtype=float)
    if wavelengths.ndim == 1:
        if wavelengths.shape[0] != values.shape[1]:
            raise ValueError("wavelength and spectra lengths do not match")
        wavelengths = np.broadcast_to(wavelengths, values.shape)
    elif wavelengths.ndim == 2:
        if wavelengths.shape != values.shape:
            raise ValueError("wavelength and spectra shapes do not match")
        if not np.allclose(wavelengths, wavelengths[0], equal_nan=False):
            raise ValueError("all wavelength rows must use the same grid")
    else:
        raise ValueError("wavelength must be one- or two-dimensional")

    return wavelengths, np.nan_to_num(values, nan=0.0)


def _select_spectra(
    wavelengths: NDArray[np.float64],
    spectra: NDArray[np.float64],
    wavelength_range: tuple[float, float],
    percent_range: tuple[float, float],
    *,
    weighted: bool,
) -> NDArray[np.bool_]:
    """
    Generate a boolean mask of selected spectra.

    Args:
        - wavelengths: The wavelength values of the spectrum.
        - spectra: The intensity values of the spectrum.
        - wavelength_range: The wavelength range to use for fitting.
        - percent_range: The percent range to use for fitting.
        - weighted: Whether to use weighted fitting.

    Returns:
        - A boolean mask indicating which spectra are selected.
    """
    low_wavelength, high_wavelength = wavelength_range
    low_percent, high_percent = percent_range

    # Check that the wavelength range and percent range are valid
    if low_wavelength > high_wavelength:
        raise ValueError("wavelength_range must be ordered")
    if not 0 <= low_percent <= high_percent <= 100:
        raise ValueError("percent_range must be ordered within [0, 100]")

    scores = np.empty(spectra.shape[0], dtype=float)
    for index, (x_row, y_row) in enumerate(zip(wavelengths, spectra, strict=True)):
        mask = (x_row >= low_wavelength) & (x_row <= high_wavelength)
        if not np.any(mask):
            raise ValueError("wavelength_range contains no spectrum samples")
        window = y_row[mask]
        scores[index] = (
            float(np.mean(window * x_row[mask])) if weighted else float(np.max(window))
        )

    lower, upper = np.percentile(scores, percent_range)
    return (scores >= lower) & (scores <= upper)


def _gaussian(
    x: NDArray[np.float64], amplitude: float, center: float, sigma: float
) -> NDArray[np.float64]:
    return amplitude * np.exp(-((x - center) ** 2) / (2 * sigma**2))


def _fit_pl_spectrum(
    wavelength: NDArray[np.float64],
    intensity: NDArray[np.float64],
    peak_wavelength: float,
    *,
    wavelength_range: tuple[float, float] = (400.0, 800.0),
    maxfev: int = 100000,
    r2_window_sigma: float = 3.0,
) -> tuple[float, float, float, float]:
    """
    Fit a Gaussian to the PL spectrum within a given wavelength range.

    Args:
        - wavelength: The wavelength values of the spectrum.
        - intensity: The intensity values of the spectrum.
        - peak_wavelength: The wavelength of the peak to fit.
        - wavelength_range: The wavelength range to use for fitting.
        - maxfev: The maximum number of function evaluations for the fit.
        - r2_window_sigma: The sigma value for the R-squared window.

    Returns:
        - The fitted peak parameters (peak, fwhm, pl_integral, r-squared).
    """
    low, high = wavelength_range
    fit_mask = (wavelength >= low) & (wavelength <= high)
    x = wavelength[fit_mask]
    y = intensity[fit_mask]
    if x.size < 3:
        raise ValueError("PL fit window must contain at least three samples")

    total = float(np.sum(y))
    if total == 0 or not np.isfinite(total):
        raise ValueError("PL fit window has no finite signal")
    mean = float(np.sum(x * y) / total)
    sigma = float(np.sqrt(np.sum(np.abs(y) * (x - mean) ** 2) / total))

    # Find the peak index and initial guess for the fit
    peak_index = _nearest_index(wavelength, peak_wavelength)
    initial_guess = [
        float(intensity[peak_index]),
        float(wavelength[peak_index]),
        sigma,
    ]

    # Attempt to fit the Gaussian to the data
    try:
        fitted, _ = curve_fit(
            _gaussian,
            x,
            y,
            p0=initial_guess,
            bounds=((0, float(x[0]), 0), (float(np.max(y) * 1.15), 1000, np.inf)),
            maxfev=maxfev,
        )
    except (RuntimeError, ValueError):
        fitted, _ = curve_fit(
            _gaussian,
            x,
            y,
            p0=initial_guess,
            bounds=(-np.inf, np.inf),
            maxfev=maxfev * 10,
        )

    peak = float(fitted[1])
    fitted_sigma = abs(float(fitted[2]))
    fitted_y = _gaussian(x, *fitted)

    # Compute the R-squared value for the fit (diagnostic only; never fatal)
    r2_mask = (x >= peak - r2_window_sigma * fitted_sigma) & (
        x <= peak + r2_window_sigma * fitted_sigma
    )
    observed = y[r2_mask]
    predicted = fitted_y[r2_mask]

    if observed.size < 2:
        r_squared = float("nan")
    else:
        residual_sum = float(np.sum((observed - predicted) ** 2))
        total_sum = float(np.sum((observed - np.mean(observed)) ** 2))
        r_squared = float("nan") if total_sum == 0 else 1 - residual_sum / total_sum
    pl_integral = float(integrate.simpson(y, x=x))

    # Return the peak, FWHM, integral, and R-squared value
    return peak, 2.355 * fitted_sigma, pl_integral, r_squared


def analyze_pl_spectra(
    wavelength: ArrayLike,
    spectra: ArrayLike,
    *,
    key_height: float = 200,
    height: float = 50,
    distance: int = 100,
    percent_range: tuple[float, float] = (40, 100),
    wavelength_range: tuple[float, float] = (400.0, 800.0),
    maxfev: int = 100000,
    r2_window_sigma: float = 3.0,
) -> tuple[float, float, float, float] | None:
    """
    Select valid PL events, average them, and fit their strongest peak.

    Args:
        - wavelength: The wavelength values of the spectra.
        - spectra: The PL intensity values of the spectra.
        - key_height: The height of the key peak.
        - height: The height of the peaks to fit.
        - distance: The distance between peaks to consider.
        - percent_range: The percentile range to use for filtering.
        - wavelength_range: The wavelength range to use for filtering.
        - maxfev: The maximum number of function evaluations for the fit.
        - r2_window_sigma: The sigma value for the R-squared window.

    Returns:
        - The fitted peak parameters (peak, fwhm, pl_integral, r-squared), or None if
          no valid spectra or strong non-LED peak.
    """
    wavelengths, values = _prepare_spectra(wavelength, spectra)
    good = np.fromiter(
        (
            classify_pl(
                x_row,
                y_row,
                key_height=key_height,
                height=height,
                distance=distance,
            )[0]
            for x_row, y_row in zip(wavelengths, values, strict=True)
        ),
        dtype=bool,
        count=values.shape[0],
    )
    if not np.any(good):
        return None

    good_wavelengths = wavelengths[good]
    good_spectra = values[good]
    selected = _select_spectra(
        good_wavelengths,
        good_spectra,
        wavelength_range,
        percent_range,
        weighted=False,
    )
    fit_wavelength = np.asarray(good_wavelengths[selected][0], dtype=float)
    averaged = np.mean(good_spectra[selected], axis=0)
    is_good, peak_wavelength = classify_pl(
        fit_wavelength,
        averaged,
        key_height=key_height,
        height=height,
        distance=distance,
    )
    if not is_good:
        return None

    # Returns the fitted peak parameters (peak, fwhm, pl_integral, r-squared)
    return _fit_pl_spectrum(
        fit_wavelength,
        averaged,
        peak_wavelength,
        wavelength_range=wavelength_range,
        maxfev=maxfev,
        r2_window_sigma=r2_window_sigma,
    )


def _fit_baseline(
    wavelength: NDArray[np.float64],
    absorbance: NDArray[np.float64],
    wavelength_range: tuple[float, float],
) -> NDArray[np.float64]:
    """
    Fit a baseline to the absorbance spectrum within a given wavelength range.

    Args:
        - wavelength: The wavelength values of the spectrum.
        - absorbance: The absorbance values of the spectrum.
        - wavelength_range: The wavelength range to use for fitting.

    Returns:
        - coefficients: The coefficients of the fitted baseline.
    """
    start = _nearest_index(wavelength, wavelength_range[0])
    stop = _nearest_index(wavelength, wavelength_range[1])
    if start > stop:
        start, stop = stop, start
    x = wavelength[start:stop+1]
    y = absorbance[start:stop+1]
    if x.size < 2 or x[0] == x[-1]:
        raise ValueError("absorbance baseline range must contain two samples")
    return np.asarray(np.polyfit(x, y, 1), dtype=float)


def correct_absorbance(
    wavelength: ArrayLike,
    spectra: ArrayLike,
    *,
    percent_range: tuple[float, float] = (10, 70),
    wavelength_range: tuple[float, float] = (210.0, 700.0),
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """
    Percentile-filter, average, and baseline-correct absorbance spectra.

    Args:
        - wavelength: The wavelength values of the spectra.
        - spectra: The absorbance values of the spectra.
        - percent_range: The percentile range to use for filtering.
        - wavelength_range: The wavelength range to use for filtering.

    Returns:
        - x: The wavelength values of the filtered spectra.
        - corrected: The baseline-corrected absorbance values of the filtered spectra.
    """
    wavelengths, values = _prepare_spectra(wavelength, spectra)
    selected = _select_spectra(
        wavelengths,
        values,
        wavelength_range,
        percent_range,
        weighted=True,
    )
    averaged = np.mean(values[selected], axis=0)
    x = np.asarray(wavelengths[selected][0], dtype=float)
    short_wavelength = _fit_baseline(x, averaged, (205, 240))
    long_wavelength = _fit_baseline(x, averaged, (750, 950))
    baseline = (
        long_wavelength
        if abs(short_wavelength[0]) >= abs(long_wavelength[0])
        else short_wavelength
    )
    return x, averaged - (baseline[0] * x + baseline[1])


def calculate_plqy(
    absorbance_sample: float,
    pl_integral_sample: float,
    refractive_index_solvent: float,
    *,
    reference_type: str,
    absorbance_reference: float,
    pl_integral_reference: float,
    refractive_index_reference: float,
    plqy_reference: float,
) -> float:
    """
    Calculate PLQY relative to a fluorescein or quinine reference.

    Args:
        - absorbance_sample: The absorbance value of the sample.
        - pl_integral_sample: The PL integral value of the sample.
        - refractive_index_solvent: The refractive index of the solvent.
        - reference_type: The type of reference to use, either 'fluorescein' or
          'quinine'.
        - absorbance_reference: The absorbance value of the reference.
        - pl_integral_reference: The PL integral value of the reference.
        - refractive_index_reference: The refractive index of the reference.
        - plqy_reference: The PLQY value of the reference.

    Returns:
        - plqy: float - The calculated PLQY value relative to the reference.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        integral_ratio = np.divide(pl_integral_sample, pl_integral_reference)
        refractive_index_ratio = (
            np.divide(refractive_index_solvent, refractive_index_reference) ** 2
        )
        if reference_type == "fluorescein":
            absorbance_ratio = np.divide(
                1 - 10 ** (-absorbance_reference),
                1 - 10 ** (-absorbance_sample),
            )
        elif reference_type == "quinine":
            absorbance_ratio = np.divide(absorbance_reference, absorbance_sample)
        else:
            raise ValueError("reference_type must be either 'fluorescein' or 'quinine'")
        return float(
            plqy_reference * integral_ratio * absorbance_ratio * refractive_index_ratio
        )


def pdf_profile(
    r_exp: ArrayLike,
    g_exp: ArrayLike,
    r_ref: ArrayLike,
    g_ref: ArrayLike,
    r_min: float = 2.0,
    r_max: float = 20.0,
    *,
    function: Callable[..., float] = pearson,
) -> float:
    """
    Score a reference PDF profile against the experimental radial grid.

    Args:
        - r_exp: The radial values of the experimental PDF profile.
        - g_exp: The intensity values of the experimental PDF profile.
        - r_ref: The radial values of the reference PDF profile.
        - g_ref: The intensity values of the reference PDF profile.
        - r_min: The minimum radial value to consider.
        - r_max: The maximum radial value to consider.
        - function: The scoring function to use.

    Returns:
        - score: float - The similarity/dissimilarity score of the reference PDF profile
          against the experimental radial grid.
    """
    # Experimental
    experimental_r = np.asarray(r_exp, dtype=float)
    experimental_g = np.asarray(g_exp, dtype=float)

    # Simulated Reference
    reference_r = np.asarray(r_ref, dtype=float)
    reference_g = np.asarray(g_ref, dtype=float)
    arrays = (experimental_r, experimental_g, reference_r, reference_g)

    # Validate inputs
    if any(array.ndim != 1 for array in arrays):
        raise ValueError("PDF profile inputs must be one-dimensional")
    if experimental_r.shape != experimental_g.shape:
        raise ValueError("experimental PDF arrays must have equal lengths")
    if reference_r.shape != reference_g.shape:
        raise ValueError("reference PDF arrays must have equal lengths")
    if any(not np.all(np.isfinite(array)) for array in arrays):
        raise ValueError("PDF profile inputs must contain only finite values")

    mask = (experimental_r >= r_min) & (experimental_r <= r_max)
    if np.count_nonzero(mask) < 2 or reference_r.size < 2:
        raise ValueError("not enough PDF points to compute a PDF profile score")
    if np.ptp(experimental_g[mask]) == 0 or np.ptp(reference_g) == 0:
        raise ValueError("constant PDF profiles have undefined correlation")

    score = function(
        experimental_r,
        experimental_g,
        reference_r,
        reference_g,
        r_min=r_min,
        r_max=r_max,
    )
    if not np.isfinite(score):
        raise ValueError("PDF profile score is not finite")
    return float(score)
