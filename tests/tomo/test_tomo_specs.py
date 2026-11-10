"""Tests for the tomography Tiled specs and their validators."""

from collections.abc import Mapping
from typing import Any

import pytest
from bluesky.run_engine import RunEngine
from tiled.structures.core import Spec
from tomo_rig import TomoRig

from xpd_tools.specs import (
    TOMO_DARK_FLAT_V1,
    TOMO_FLYSCAN_V1,
    SpecValidationError,
    validate_run,
)
from xpd_tools.tomo import tomo_dark_flat, tomo_flyscan


class FakeRun:
    """A RunLike built from a start document and stream field names."""

    def __init__(self, start: Mapping[str, Any], streams: dict[str, list[str]]):
        self.start = start
        self._streams = streams

    def __contains__(self, key: str) -> bool:
        return key in self._streams

    def __getitem__(self, key: str) -> list[str]:
        return self._streams[key]


def _run_from_docs(docs: dict[str, list[dict]]) -> FakeRun:
    return FakeRun(
        docs["start"][0],
        {d["name"]: list(d["data_keys"]) for d in docs["descriptor"]},
    )


def test_tomo_flyscan_matches_spec(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    tomo_rig.num_frames = 5
    RE(tomo_flyscan([tomo_rig.pilatus], 5, 0.001, **tomo_rig.device_kwargs()))
    validate_run(_run_from_docs(docs), TOMO_FLYSCAN_V1)


def test_tomo_dark_flat_matches_spec(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    RE(
        tomo_dark_flat(
            [tomo_rig.pilatus],
            0.001,
            num_dark_images=1,
            num_flat_images=1,
            sample_out_motor=tomo_rig.sample_out_motor,
            fe_shutter=tomo_rig.fe_shutter,
            photon_shutter=tomo_rig.photon_shutter,
        )
    )
    validate_run(_run_from_docs(docs), TOMO_DARK_FLAT_V1)


FLYSCAN_START = {
    "plan_name": "tomo_flyscan",
    "detectors": ["iris1"],
    "num_points": 3,
    "exposure_time": 0.1,
    "acquire_period": 0.11,
    "time_based": True,
    "start_position": 0,
    "stop_position": 180,
    "images_to_average": 1,
    "rotation_motor": "amPhi",
}


@pytest.mark.parametrize(
    ("start", "streams", "match"),
    [
        ({**FLYSCAN_START, "plan_name": "count"}, {}, "plan_name"),
        (
            {k: v for k, v in FLYSCAN_START.items() if k != "exposure_time"},
            {},
            "exposure_time",
        ),
        (FLYSCAN_START, {}, "no 'primary' stream"),
        (FLYSCAN_START, {"primary": ["iris1"]}, "rotation_angle"),
        (
            {**FLYSCAN_START, "last_dark_flat_uid": 5},
            {"primary": ["iris1", "rotation_angle"]},
            "must be a string",
        ),
        ({**FLYSCAN_START, "detectors": "iris1"}, {}, "list of names"),
    ],
)
def test_validate_tomo_flyscan_rejects(start, streams, match: str):
    with pytest.raises(SpecValidationError, match=match):
        validate_run(FakeRun(start, streams), TOMO_FLYSCAN_V1)


def test_validate_tomo_dark_flat_needs_flat_stream():
    start = {
        "plan_name": "tomo_dark_flat",
        "detectors": ["iris1"],
        "exposure_time": 0.1,
        "num_dark_images": 1,
        "num_flat_images": 1,
        "sample_out_motor": "amTopX",
        "sample_out_offset": 2.0,
    }
    with pytest.raises(SpecValidationError, match="no 'flat' stream"):
        validate_run(FakeRun(start, {"dark": ["iris1"]}), TOMO_DARK_FLAT_V1)


def test_validate_run_unknown_spec():
    with pytest.raises(KeyError, match="No validator"):
        validate_run(FakeRun({}, {}), Spec("Unknown"))
