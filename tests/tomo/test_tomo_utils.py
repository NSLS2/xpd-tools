"""Tests for resolve_exposure_times."""

import math
from dataclasses import dataclass

import pytest

from xpd_tools.tomo.utils import exposure_md, resolve_exposure_times


@dataclass(eq=False)  # hashed by identity, like a device
class FakeDetector:
    """Just the ``name`` that resolve_exposure_times reads."""

    name: str


PIL, IRIS = FakeDetector("pilatus1"), FakeDetector("iris1")
DETS: list = [PIL, IRIS]


def test_number_for_every_detector():
    assert resolve_exposure_times(DETS, 0.1) == {PIL: 0.1, IRIS: 0.1}
    assert resolve_exposure_times(DETS, 1) == {PIL: 1.0, IRIS: 1.0}


@pytest.mark.parametrize(
    "mapping",
    [
        {"pilatus1": 0.1, "iris1": 0.5},
        {PIL: 0.1, IRIS: 0.5},
        {"iris1": 0.5, PIL: 0.1},  # mixed keys, any order
    ],
)
def test_mapping_by_name_or_device(mapping):
    times = resolve_exposure_times(DETS, mapping)
    assert times == {PIL: 0.1, IRIS: 0.5}
    assert list(times) == DETS
    assert exposure_md(times) == {"pilatus1": 0.1, "iris1": 0.5}


@pytest.mark.parametrize(
    ("exposure_time", "match"),
    [
        ({"iris1": 0.5}, r"no exposure time for \['pilatus1'\]"),
        ({}, r"no exposure time for \['pilatus1', 'iris1'\]"),
        (
            {"pilatus1": 0.1, "iris1": 0.5, "pilatus1_fly": 0.5},
            r"'pilatus1_fly' .* not one of .*\['pilatus1', 'iris1'\]",
        ),
        (
            {PIL: 0.1, FakeDetector("iris1"): 0.5},
            "not one of",
        ),  # same name, other device
        ({"pilatus1": 0.1, "iris1": 0.5, IRIS: 0.6}, "'iris1' more than once"),
        ({"pilatus1": 0.1, "iris1": 0}, "iris1 must be a positive number"),
        ({"pilatus1": 0.1, "iris1": -1.0}, "positive"),
        ({"pilatus1": 0.1, "iris1": math.nan}, "positive"),
        ({"pilatus1": 0.1, "iris1": True}, "positive"),
        (0.0, "pilatus1 must be a positive number"),
        (True, "positive"),
        ("0.1", "positive"),
    ],
)
def test_rejects_bad_exposure_time(exposure_time, match: str):
    with pytest.raises(ValueError, match=match):
        resolve_exposure_times(DETS, exposure_time)


def test_rejects_duplicate_detector_names():
    with pytest.raises(ValueError, match="Two detectors are named 'iris1'"):
        resolve_exposure_times([IRIS, FakeDetector("iris1")], 0.1)  # type: ignore[list-item]
