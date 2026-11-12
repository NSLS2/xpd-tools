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
    rig = tomo_rig
    rig.num_frames = 6
    RE(
        tomo_flyscan(
            [rig.config(rig.pilatus, 6, 0.001), rig.config(rig.iris, 3, 0.001)],
            **rig.device_kwargs(),
        )
    )
    validate_run(_run_from_docs(docs), TOMO_FLYSCAN_V1)


def test_tomo_dark_flat_matches_spec(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    RE(
        tomo_dark_flat(
            [tomo_rig.config(tomo_rig.pilatus, 1, 0.001)],
            num_dark_images=1,
            num_flat_images=1,
            sample_out_motor=tomo_rig.sample_out_motor,
            fe_shutter=tomo_rig.fe_shutter,
            photon_shutter=tomo_rig.photon_shutter,
        )
    )
    validate_run(_run_from_docs(docs), TOMO_DARK_FLAT_V1)


def _det_config(divisor: int) -> dict[str, Any]:
    return {
        "nproj": 6 // divisor,
        "num_images": 6 // divisor + 1,
        "exposure_time": 0.1,
        "images_to_average": 1,
        "filter_type": "Average",
        "ttl_out": divisor,
        "trigger_divisor": divisor,
    }


FLYSCAN_START = {
    "plan_name": "tomo_flyscan",
    "detectors": ["pilatus1", "iris1"],
    "detector_configs": {"pilatus1": _det_config(1), "iris1": _det_config(2)},
    "num_base_pulses": 6,
    "trigger_period": 0.11,
    "time_based": True,
    "start_position": 0,
    "stop_position": 180,
    "rotation_motor": "amPhi",
}
FLYSCAN_STREAMS = {
    "pilatus1": ["pilatus1"],
    "iris1": ["iris1"],
    "rotation": ["rotation_angle"],
}


def _without(mapping: dict, key: str) -> dict:
    return {k: v for k, v in mapping.items() if k != key}


@pytest.mark.parametrize(
    ("start", "streams", "match"),
    [
        ({**FLYSCAN_START, "plan_name": "count"}, {}, "plan_name"),
        (_without(FLYSCAN_START, "detector_configs"), {}, "detector_configs"),
        (_without(FLYSCAN_START, "trigger_period"), {}, "trigger_period"),
        (
            {**FLYSCAN_START, "detector_configs": [1, 2]},
            FLYSCAN_STREAMS,
            "must be a mapping",
        ),
        (
            {
                **FLYSCAN_START,
                "detector_configs": {"pilatus1": _det_config(1)},
            },
            FLYSCAN_STREAMS,
            "no entry for 'iris1'",
        ),
        (
            {
                **FLYSCAN_START,
                "detector_configs": {
                    "pilatus1": _det_config(1),
                    "iris1": _without(_det_config(2), "ttl_out"),
                },
            },
            FLYSCAN_STREAMS,
            r"'iris1' is missing \['ttl_out'\]",
        ),
        (FLYSCAN_START, _without(FLYSCAN_STREAMS, "iris1"), "no 'iris1' stream"),
        (
            FLYSCAN_START,
            {**FLYSCAN_STREAMS, "iris1": ["iris1-stats"]},
            r"'iris1' is missing \['iris1'\]",
        ),
        (FLYSCAN_START, _without(FLYSCAN_STREAMS, "rotation"), "no 'rotation' stream"),
        (
            FLYSCAN_START,
            {**FLYSCAN_STREAMS, "rotation": []},
            "rotation_angle",
        ),
        (
            {**FLYSCAN_START, "last_dark_flat_uid": 5},
            FLYSCAN_STREAMS,
            "must be a string",
        ),
        ({**FLYSCAN_START, "detectors": "iris1"}, FLYSCAN_STREAMS, "list of names"),
    ],
)
def test_validate_tomo_flyscan_rejects(start, streams, match: str):
    with pytest.raises(SpecValidationError, match=match):
        validate_run(FakeRun(start, streams), TOMO_FLYSCAN_V1)


def test_validate_rejects_unknown_version():
    with pytest.raises(SpecValidationError, match="Unknown TomoFlyscan version '2'"):
        validate_run(
            FakeRun(FLYSCAN_START, FLYSCAN_STREAMS), Spec("TomoFlyscan", version="2")
        )


DARK_FLAT_START = {
    "plan_name": "tomo_dark_flat",
    "detectors": ["iris1"],
    "exposure_time": {"iris1": 0.1},
    "num_dark_images": 1,
    "num_flat_images": 1,
    "sample_out_motor": "amTopX",
    "sample_out_offset": 2.0,
}


@pytest.mark.parametrize(
    ("start", "streams", "match"),
    [
        (DARK_FLAT_START, {"dark": ["iris1"]}, "no 'flat' stream"),
        ({**DARK_FLAT_START, "exposure_time": 0.1}, {}, "map detector names"),
    ],
)
def test_validate_tomo_dark_flat_rejects(start, streams, match: str):
    with pytest.raises(SpecValidationError, match=match):
        validate_run(FakeRun(start, streams), TOMO_DARK_FLAT_V1)


def test_validate_run_unknown_spec():
    with pytest.raises(KeyError, match="No validator"):
        validate_run(FakeRun({}, {}), Spec("Unknown"))
