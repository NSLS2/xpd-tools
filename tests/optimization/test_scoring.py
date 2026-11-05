from __future__ import annotations

import numpy as np
import pytest

from xpd_tools.optimization.scoring import (
    cross_correlation,
    nn_matrix,
    pearson,
    weighted_profile_r,
)
from xpd_tools.optimization.scoring.ensemble_pdf import EnsembleGoodnessOfFitScorer

RADIAL = np.linspace(1.0, 25.0, 241)
REFERENCE = np.sin(RADIAL) + 2.0  # keep strictly positive for NMF


def _diverge_beyond(
    radial: np.ndarray, profile: np.ndarray, cutoff: float
) -> np.ndarray:
    return np.where(radial <= cutoff, profile, profile + 5.0)


@pytest.mark.parametrize(
    "scorer",
    [pearson, cross_correlation, nn_matrix, weighted_profile_r],
)
def test_constant_reference_within_masked_window_raises(scorer) -> None:
    """A reference that's constant only within [r_min, r_max] (but not
    over its full range) must raise a clear error naming the window --
    not silently divide-by-zero into NaN. analysis.pdf_profile's own
    constant-profile guard only checks the full, unmasked reference, so
    this can't rely on that catching it first when called directly.
    """
    reference = np.where(RADIAL < 10, 0.0, REFERENCE)
    with pytest.raises(ValueError, match="constant"):
        scorer(RADIAL, REFERENCE, RADIAL, reference, r_min=2.0, r_max=8.0)


@pytest.mark.parametrize(
    "scorer",
    [pearson, cross_correlation],
)
def test_identical_profiles_score_perfectly(scorer) -> None:
    score = scorer(RADIAL, REFERENCE, RADIAL, REFERENCE)
    assert score == pytest.approx(1.0)


@pytest.mark.parametrize(
    "scorer",
    [nn_matrix, weighted_profile_r],
)
def test_identical_profiles_have_near_zero_dissimilarity(scorer) -> None:
    score = scorer(RADIAL, REFERENCE, RADIAL, REFERENCE)
    assert score == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    "scorer",
    [pearson, cross_correlation, nn_matrix, weighted_profile_r],
)
def test_r_min_r_max_mask_the_comparison_window(scorer) -> None:
    experimental = _diverge_beyond(RADIAL, REFERENCE, cutoff=12.0)

    narrow = scorer(RADIAL, experimental, RADIAL, REFERENCE, r_min=2.0, r_max=10.0)
    full = scorer(RADIAL, experimental, RADIAL, REFERENCE, r_min=2.0, r_max=20.0)

    # Narrow window only sees the matching region; full window sees the divergence too.
    if scorer in (pearson, cross_correlation):
        assert narrow == pytest.approx(1.0)
        assert full < narrow
    else:
        assert narrow == pytest.approx(0.0, abs=1e-6)
        assert full > narrow


def test_weighted_profile_r_grows_with_larger_residuals() -> None:
    small_offset = weighted_profile_r(RADIAL, REFERENCE + 0.01, RADIAL, REFERENCE)
    large_offset = weighted_profile_r(RADIAL, REFERENCE + 1.0, RADIAL, REFERENCE)
    assert small_offset < large_offset


def test_cross_correlation_penalizes_lag_shift() -> None:
    shift = int(0.25 * len(RADIAL) / (RADIAL[-1] - RADIAL[0]))
    shifted = np.roll(REFERENCE, shift)
    score = cross_correlation(RADIAL, shifted, RADIAL, REFERENCE)
    assert score < 1.0


def test_ensemble_first_call_returns_raw_unnormalized_values() -> None:
    scorer = EnsembleGoodnessOfFitScorer()
    total = scorer.score(RADIAL, REFERENCE, RADIAL, REFERENCE)

    expected = (
        pearson(RADIAL, REFERENCE, RADIAL, REFERENCE)
        + cross_correlation(RADIAL, REFERENCE, RADIAL, REFERENCE)
        + weighted_profile_r(RADIAL, REFERENCE, RADIAL, REFERENCE)
        + nn_matrix(RADIAL, REFERENCE, RADIAL, REFERENCE)
    )
    assert total == pytest.approx(expected)


def test_ensemble_normalizes_and_flips_lower_is_better_metrics_after_history() -> None:
    scorer = EnsembleGoodnessOfFitScorer()
    good = _diverge_beyond(RADIAL, REFERENCE, cutoff=100.0)  # never diverges
    bad = _diverge_beyond(RADIAL, REFERENCE, cutoff=5.0)  # diverges early, badly

    scorer.score(RADIAL, good, RADIAL, REFERENCE)
    good_total = scorer.score(RADIAL, good, RADIAL, REFERENCE)
    bad_total = scorer.score(RADIAL, bad, RADIAL, REFERENCE)

    # A better-matching profile should score higher once normalization kicks in.
    assert good_total > bad_total


def test_ensemble_custom_weights_are_applied() -> None:
    equal = EnsembleGoodnessOfFitScorer().score(RADIAL, REFERENCE, RADIAL, REFERENCE)
    weighted = EnsembleGoodnessOfFitScorer(
        weights={
            "pearson": 1.0,
            "cross_correlation": 0.0,
            "weighted_profile_r": 0.0,
            "nn_matrix": 0.0,
        }
    ).score(RADIAL, REFERENCE, RADIAL, REFERENCE)

    # First call is raw (unnormalized), so weighting only pearson isolates its raw
    # value.
    assert weighted == pytest.approx(pearson(RADIAL, REFERENCE, RADIAL, REFERENCE))
    assert weighted != pytest.approx(equal)


def test_ensemble_partial_weights_override_only_named_metrics() -> None:
    """A partial weights dict must override only the metrics it names --
    not raise KeyError for the rest, and not silently drop them.
    """
    scorer = EnsembleGoodnessOfFitScorer(weights={"pearson": 2.0})
    assert scorer.weights == {
        "pearson": 2.0,
        "cross_correlation": 1.0,
        "weighted_profile_r": 1.0,
        "nn_matrix": 1.0,
    }
    scorer.score(RADIAL, REFERENCE, RADIAL, REFERENCE)  # must not raise


def test_ensemble_empty_weights_dict_means_all_defaults() -> None:
    """An explicit empty dict is a real 'no overrides', not the same as
    None falling through to a from-scratch default -- both must land on
    all-1.0, but via the same merge path, not a falsy-dict special case.
    """
    assert (
        EnsembleGoodnessOfFitScorer(weights={}).weights
        == EnsembleGoodnessOfFitScorer().weights
    )


def test_ensemble_rejects_unknown_weight_names() -> None:
    with pytest.raises(ValueError, match="unknown metric names"):
        EnsembleGoodnessOfFitScorer(weights={"pearson": 1.0, "typo": 5.0})
