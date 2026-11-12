"""Tests for the exposure-time helpers and detector config validation."""

import math
from dataclasses import dataclass

import pytest
from pydantic import ValidationError
from tomo_rig import TomoRig

import xpd_tools.utils as xpd_utils
from xpd_tools.tomo.utils import (
    ROTATION_STREAM,
    DetectorConfig,
    exposure_md,
    resolve_exposure_times,
    validate_detector_configs,
)


@dataclass(eq=False)  # hashed by identity, like a device
class FakeDetector:
    """Just the ``name`` that resolve_exposure_times reads."""

    name: str


PIL, IRIS = FakeDetector("pilatus1"), FakeDetector("iris1")
DETS: list = [PIL, IRIS]


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


# --- Detector configs ---------------------------------------------------------------


def _routes(routing) -> list[tuple[int, int, int | None]]:
    return [(r.ttl_out, r.divisor, r.div_block) for r in routing.routes]


def test_validate_routes_the_fastest_directly(tomo_rig: TomoRig):
    rig = tomo_rig
    routing = validate_detector_configs(
        [
            rig.config(rig.xs3, 4, 0.05),
            rig.config(rig.pilatus, 12, 0.002),
            rig.config(rig.iris, 6, 0.01),
        ]
    )
    assert routing.num_base_pulses == 13
    # In config order; the DIV blocks go to the next fastest first.
    assert _routes(routing) == [(2, 3, 2), (1, 1, None), (3, 2, 1)]


def test_validate_divides_the_intervals_not_the_images(tomo_rig: TomoRig):
    rig = tomo_rig
    # 100 and 200 projections are 101 and 201 images: the base pulses are 201 and
    # the slower detector fires every 2nd, pulses 0, 2, ... 200, 101 in all.
    routing = validate_detector_configs(
        [rig.config(rig.pilatus, 100, 0.002), rig.config(rig.xs3, 200, 0.002)]
    )
    assert routing.num_base_pulses == 201
    assert _routes(routing) == [(1, 2, 1), (2, 1, None)]
    assert rig.config(rig.pilatus, 100, 0.002).num_images == 101


def test_validate_averaged_frames_are_triggers(tomo_rig: TomoRig):
    rig = tomo_rig
    pilatus = rig.config(rig.pilatus, 22, 0.002)
    # nproj + 1 = 6 images of 2 averaged frames are 12 triggers, 11 gaps: half the
    # Pilatus's 22, so the Kinetix fires every 2nd base pulse.
    iris = rig.config(rig.iris, 5, 0.01).model_copy(update={"images_to_average": 2})
    object.__setattr__(rig.iris, "proc", object())  # pretend it has a Proc plugin
    assert iris.num_images == 6
    assert iris.num_triggers == 12
    routing = validate_detector_configs([pilatus, iris])
    assert routing.num_base_pulses == 23
    assert _routes(routing) == [(1, 1, None), (3, 2, 1)]


def test_config_looks_up_detector_name(tomo_rig: TomoRig, monkeypatch):
    class FakeIPython:
        user_ns = {"pilatus1": tomo_rig.pilatus}

    monkeypatch.setattr(xpd_utils, "get_ipython", lambda: FakeIPython())
    config = DetectorConfig.model_validate(
        {"detector": "pilatus1", "nproj": 10, "exposure_time": 0.1, "ttl_out": 1}
    )
    assert config.detector is tomo_rig.pilatus
    with pytest.raises(ValidationError, match="No detector is named 'iris9'"):
        DetectorConfig.model_validate(
            {"detector": "iris9", "nproj": 10, "exposure_time": 0.1, "ttl_out": 1}
        )


def _bad_sets(rig: TomoRig) -> list[tuple[list, str]]:
    pil = rig.config(rig.pilatus, 12, 0.002)
    return [
        ([], "1 to 3 detector configs, got 0"),
        ([pil, pil.model_copy(update={"ttl_out": 2})], "pilatus1 has more than one"),
        (
            [pil, rig.config(rig.iris, 6, 0.01).model_copy(update={"ttl_out": 1})],
            "iris1 and pilatus1 are both on TTL output 1",
        ),
        ([pil.model_copy(update={"ttl_out": 4})], "TTL output 4, which is not one"),
        (
            [pil, rig.config(rig.iris, 5, 0.01)],
            r"12, must be a multiple .* iris1 \(5\)",
        ),
        (
            [pil.model_copy(update={"images_to_average": 2})],
            "pilatus1 averages 2 frames",
        ),
    ]


@pytest.mark.parametrize("case", range(6))
def test_validate_rejects(tomo_rig: TomoRig, case: int):
    configs, match = _bad_sets(tomo_rig)[case]
    with pytest.raises(ValueError, match=match):
        validate_detector_configs(configs)


def test_validate_rejects_shared_and_reserved_names(tomo_rig: TomoRig):
    rig = tomo_rig
    pil = rig.config(rig.pilatus, 12, 0.002)
    iris = DetectorConfig(detector=rig.iris, nproj=6, exposure_time=0.01, ttl_out=2)
    rig.iris.set_name("pilatus1")
    with pytest.raises(ValueError, match="Two detectors are named 'pilatus1'"):
        validate_detector_configs([pil, iris])
    rig.iris.set_name(ROTATION_STREAM)
    with pytest.raises(ValueError, match="can't be named 'rotation'"):
        validate_detector_configs([pil, iris])


@pytest.mark.parametrize("exposure_time", [math.inf, math.nan])
def test_config_rejects_non_finite_exposure_time(tomo_rig: TomoRig, exposure_time):
    with pytest.raises(ValidationError):
        DetectorConfig(
            detector=tomo_rig.pilatus, nproj=10, exposure_time=exposure_time, ttl_out=1
        )
