from __future__ import annotations

import numpy as np
import pytest

from xpd_tools.optimization.analysis import (
    analyze_pl_spectra,
    calculate_plqy,
    classify_pl,
    correct_absorbance,
    pdf_profile,
)
from xpd_tools.optimization.scoring import weighted_profile_r


def test_classify_pl_suppresses_led_and_accepts_height_boundary() -> None:
    wavelength = np.arange(300.0, 801.0)
    intensity = np.zeros_like(wavelength)
    intensity[65] = 100_000
    intensity[360] = 2_000

    is_good, peak_wavelength = classify_pl(wavelength, intensity)

    assert is_good
    assert peak_wavelength == 660


def test_classify_pl_reports_no_peak() -> None:
    wavelength = np.arange(300.0, 801.0)

    is_good, peak_wavelength = classify_pl(wavelength, np.zeros_like(wavelength))

    assert not is_good
    assert np.isnan(peak_wavelength)


def test_analyze_pl_spectra_ranks_only_pl_window(wavelength: np.ndarray) -> None:
    sigma = 20.0
    gaussian = np.exp(-((wavelength - 660) ** 2) / (2 * sigma**2))
    spectra = np.vstack([1000 * gaussian, 2000 * gaussian, 3000 * gaussian])
    spectra[0, np.abs(wavelength - 365).argmin()] = 100_000
    spectra[1, 0] = np.nan
    repeated_wavelength = np.broadcast_to(wavelength, spectra.shape)

    result = analyze_pl_spectra(
        repeated_wavelength,
        spectra,
        percent_range=(0, 50),
    )

    assert result is not None
    peak, fwhm, integral, r_squared = result
    assert peak == pytest.approx(660, abs=1e-6)
    assert fwhm == pytest.approx(2.355 * sigma, rel=1e-6)
    assert integral == pytest.approx(1500 * np.sqrt(2 * np.pi) * sigma, rel=1e-3)
    assert r_squared == pytest.approx(1.0)


def test_correct_absorbance_selects_configured_percentile(
    wavelength: np.ndarray,
) -> None:
    baseline = 0.001 * wavelength + 0.2
    outlier = baseline.copy()
    outlier[(wavelength >= 210) & (wavelength <= 700)] += 10

    corrected_x, corrected = correct_absorbance(
        wavelength,
        np.vstack([baseline, baseline, outlier]),
        percent_range=(0, 50),
    )

    np.testing.assert_array_equal(corrected_x, wavelength)
    np.testing.assert_allclose(corrected, 0, atol=1e-10)


def test_correct_absorbance_wavelength_range_controls_outlier_detection(
    wavelength: np.ndarray,
) -> None:
    baseline = 0.001 * wavelength + 0.2
    bump = baseline.copy()
    bump_mask = (wavelength >= 340) & (wavelength <= 360)
    bump[bump_mask] += 5.0
    spectra = np.vstack([baseline, baseline, bump])

    # Scoring window covers the bump -> it's detected as an outlier and excluded.
    _, excluded = correct_absorbance(
        wavelength, spectra, percent_range=(0, 50), wavelength_range=(210, 700)
    )
    # Scoring window misses the bump -> all three spectra look alike and are
    # averaged in.
    _, included = correct_absorbance(
        wavelength, spectra, percent_range=(0, 50), wavelength_range=(400, 700)
    )

    np.testing.assert_allclose(excluded[bump_mask], 0, atol=1e-6)
    assert np.all(included[bump_mask] > 1.0)


def test_analyze_pl_spectra_fits_single_gaussian(wavelength: np.ndarray) -> None:
    sigma = 20.0
    intensity = 5000 * np.exp(-((wavelength - 660) ** 2) / (2 * sigma**2))

    result = analyze_pl_spectra(wavelength, intensity)

    assert result is not None
    peak, fwhm, integral, r_squared = result
    assert peak == pytest.approx(660, abs=1e-6)
    assert fwhm == pytest.approx(2.355 * sigma, rel=1e-6)
    assert integral == pytest.approx(5000 * np.sqrt(2 * np.pi) * sigma, rel=1e-3)
    assert r_squared == pytest.approx(1.0)


def test_analyze_pl_spectra_reports_no_valid_event(wavelength: np.ndarray) -> None:
    assert analyze_pl_spectra(wavelength, np.zeros_like(wavelength)) is None


def test_analyze_pl_spectra_key_height_screens_weak_peaks(
    wavelength: np.ndarray,
) -> None:
    weak = 60 * np.exp(-((wavelength - 660) ** 2) / (2 * 20.0**2))

    assert analyze_pl_spectra(wavelength, weak) is None
    assert analyze_pl_spectra(wavelength, weak, key_height=50) is not None


def test_analyze_pl_spectra_wavelength_range_restricts_fit_window(
    wavelength: np.ndarray,
) -> None:
    sigma = 20.0
    gaussian = 5000 * np.exp(-((wavelength - 660) ** 2) / (2 * sigma**2))

    full = analyze_pl_spectra(wavelength, gaussian, wavelength_range=(400.0, 800.0))
    narrow = analyze_pl_spectra(wavelength, gaussian, wavelength_range=(600.0, 700.0))

    assert full is not None and narrow is not None
    # Narrower window captures less of the Gaussian's tails -> smaller integral.
    assert narrow[2] < full[2]


def test_analyze_pl_spectra_r2_window_sigma_controls_scoring_range(
    wavelength: np.ndarray,
) -> None:
    sigma = 20.0
    spiked = 5000 * np.exp(-((wavelength - 660) ** 2) / (2 * sigma**2))
    spiked[np.abs(wavelength - 750).argmin()] += 200  # outlier 4.5 sigma from peak

    narrow_window = analyze_pl_spectra(wavelength, spiked, r2_window_sigma=3.0)
    wide_window = analyze_pl_spectra(wavelength, spiked, r2_window_sigma=6.0)

    assert narrow_window is not None and wide_window is not None
    # Widening the R^2 window pulls the outlier into the fit-quality score.
    assert wide_window[3] < narrow_window[3]


def test_calculate_plqy_formulas() -> None:
    common = {
        "absorbance_sample": 0.2,
        "pl_integral_sample": 10.0,
        "refractive_index_solvent": 1.5,
        "absorbance_reference": 0.1,
        "pl_integral_reference": 5.0,
        "refractive_index_reference": 1.0,
        "plqy_reference": 0.5,
    }

    assert calculate_plqy(reference_type="quinine", **common) == pytest.approx(1.125)
    assert calculate_plqy(reference_type="fluorescein", **common) == pytest.approx(
        0.5 * 2 * ((1 - 10**-0.1) / (1 - 10**-0.2)) * 1.5**2
    )
    with pytest.raises(ValueError, match="reference_type"):
        calculate_plqy(reference_type="unknown", **common)


def test_pdf_profile_is_finite_and_validates_inputs() -> None:
    radial = np.linspace(0, 25, 251)
    profile = np.sin(radial)

    assert pdf_profile(radial, profile, radial, profile) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="constant"):
        pdf_profile(radial, np.ones_like(radial), radial, profile)
    with pytest.raises(ValueError, match="finite"):
        pdf_profile(radial, np.where(radial == 5, np.nan, profile), radial, profile)
    with pytest.raises(ValueError, match="not enough"):
        pdf_profile(np.array([1.0]), np.array([1.0]), radial, profile)


def test_pdf_profile_dispatches_to_the_given_scoring_function() -> None:
    radial = np.linspace(0, 25, 251)
    profile = np.sin(radial)

    assert pdf_profile(radial, profile, radial, profile) == pytest.approx(1.0)
    assert pdf_profile(
        radial, profile, radial, profile, function=weighted_profile_r
    ) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError, match="not enough"):
        pdf_profile(np.array([1.0]), np.array([1.0]), radial, profile)
