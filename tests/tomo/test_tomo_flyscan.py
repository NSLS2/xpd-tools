"""Tests for tomo_flyscan."""

import logging
from typing import Any, cast

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from bluesky.utils import FailedStatus
from ophyd_async.core import get_mock_put, set_mock_attr, set_mock_value
from tomo_rig import TomoRig

import xpd_tools.tomo.flyscans as flyscans
from xpd_tools.shutter import ShutterStatus
from xpd_tools.specs import TOMO_FLYSCAN_V1
from xpd_tools.tomo import (
    MAX_MISSING_FRAMES,
    ROTATION_STREAM,
    tomo_flyscan,
)
from xpd_tools.tomo.flyscans import FRAME_PERIOD_MARGIN, ROTATION_DATASET_NAME

NUM = 12
EXPOSURE = 0.001
#: Pilatus and Kinetix deadtime.
DEADTIME = 0.001


def _flyscan(rig: TomoRig, configs: list | None = None, **kwargs):
    if configs is None:
        configs = [rig.config(rig.pilatus, NUM, EXPOSURE)]
    # nproj intervals take nproj + 1 base pulses.
    rig.num_frames = (
        max(c.nproj if hasattr(c, "nproj") else c["nproj"] for c in configs) + 1
    )
    return tomo_flyscan(configs, **rig.device_kwargs(), **kwargs)


def _frames_written(docs, data_key: str) -> int:
    resource = next(r for r in docs["stream_resource"] if r["data_key"] == data_key)
    datums = [
        d for d in docs["stream_datum"] if d["stream_resource"] == resource["uid"]
    ]
    return datums[-1]["indices"]["stop"] if datums else 0


def _descriptor(docs, name: str) -> dict:
    (descriptor,) = [d for d in docs["descriptor"] if d["name"] == name]
    return descriptor


async def test_tomo_flyscan_documents(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    # A dict of the config fields is as good as a DetectorConfig.
    config = {
        "detector": tomo_rig.pilatus,
        "nproj": NUM,
        "exposure_time": EXPOSURE,
        "ttl_out": 1,
    }
    result = cast(
        Any,
        RE(_flyscan(tomo_rig, [config], sample_name="rock", md={"operator": "me"})),
    )

    (start,) = docs["start"]
    assert result.plan_result == start["uid"]
    assert start["plan_name"] == "tomo_flyscan"
    assert start["detectors"] == ["pilatus1"]
    assert start["detector_configs"] == {
        "pilatus1": {
            "nproj": NUM,
            "num_images": NUM + 1,
            "exposure_time": EXPOSURE,
            "images_to_average": 1,
            "filter_type": "Average",
            "ttl_out": 1,
            "trigger_divisor": 1,
        }
    }
    assert start["num_base_pulses"] == NUM + 1
    assert start["trigger_period"] == pytest.approx(
        EXPOSURE + DEADTIME + FRAME_PERIOD_MARGIN
    )
    assert start["rotation_motor"] == "amPhi"
    assert start["sample_name"] == "rock"
    assert start["operator"] == "me"
    assert start["tiled_specs"] == [
        {"name": TOMO_FLYSCAN_V1.name, "version": TOMO_FLYSCAN_V1.version}
    ]
    assert docs["stop"][0]["exit_status"] == "success"

    # One stream per detector, and the PandA's in its own.
    assert "pilatus1" in _descriptor(docs, "pilatus1")["data_keys"]
    assert ROTATION_DATASET_NAME in _descriptor(docs, ROTATION_STREAM)["data_keys"]
    assert _frames_written(docs, "pilatus1") == NUM + 1
    assert _frames_written(docs, ROTATION_DATASET_NAME) == NUM + 1

    # The detector is told its exposure and image count; unused TTLs are off.
    driver = tomo_rig.pilatus.driver
    assert get_mock_put(driver.acquire_time).call_args_list[-1].args[0] == EXPOSURE
    assert get_mock_put(driver.num_triggers).call_args_list[-1].args[0] == NUM + 1
    panda = tomo_rig.panda
    assert await panda.ttlout[1].val.get_value() == "PULSE1.OUT"
    assert await panda.ttlout[2].val.get_value() == "ZERO"
    assert await panda.ttlout[3].val.get_value() == "ZERO"


async def test_tomo_flyscan_routes_each_detector_its_rate(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps
):
    rig = tomo_rig
    configs = [
        rig.config(rig.pilatus, 12, 0.002),
        rig.config(rig.iris, 6, 0.002),
        rig.config(rig.xs3, 4, 0.002),
    ]
    RE(_flyscan(rig, configs))

    assert docs["stop"][0]["exit_status"] == "success"
    (start,) = docs["start"]
    assert start["num_base_pulses"] == 13
    divisors = {
        name: config["trigger_divisor"]
        for name, config in start["detector_configs"].items()
    }
    assert divisors == {"pilatus1": 1, "iris1": 2, "xs3": 3}

    # The fastest detector gets the base pulses; the slower ones a DIV block each,
    # the next fastest first, passing the first pulse.
    panda = rig.panda
    assert await panda.ttlout[1].val.get_value() == "PULSE1.OUT"
    # The iris is on TTL3 and the Xspress3 on TTL2.
    assert await panda.ttlout[3].val.get_value() == "DIV1.OUTD"
    assert await panda.ttlout[2].val.get_value() == "DIV2.OUTD"
    assert await panda.div[1].divisor.get_value() == 2
    assert await panda.div[2].divisor.get_value() == 3
    assert await panda.div[1].first_pulse.get_value() == "OutD"

    # So each detector writes its own number of frames, to its own stream.
    # (nproj + 1 each: the divided detectors' last trigger is the last base pulse.)
    assert _frames_written(docs, "pilatus1") == 13
    assert _frames_written(docs, "iris1") == 7
    assert _frames_written(docs, "xs3") == 5
    assert _frames_written(docs, ROTATION_DATASET_NAME) == 13
    assert {"pilatus1", "iris1", "xs3", ROTATION_STREAM} <= {
        d["name"] for d in docs["descriptor"]
    }


def test_tomo_flyscan_period_fits_slowest_detector(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps, monkeypatch
):
    calls: list[dict] = []
    original = flyscans.compute_flyscan_models

    def _spy(*args, **kwargs):
        calls.append(kwargs)
        return (yield from original(*args, **kwargs))

    monkeypatch.setattr(flyscans, "compute_flyscan_models", _spy)
    rig = tomo_rig
    # The Kinetix fires every 2nd pulse, so it needs (20 ms + 1 ms) / 2 per base
    # pulse, more than the Pilatus's 2 ms + 1 ms.
    RE(
        _flyscan(
            rig, [rig.config(rig.pilatus, 12, 0.002), rig.config(rig.iris, 6, 0.02)]
        )
    )

    period = (0.02 + DEADTIME) / 2 + FRAME_PERIOD_MARGIN
    assert docs["start"][0]["trigger_period"] == pytest.approx(period)
    (kwargs,) = calls
    assert kwargs["num_pulses"] == 13
    assert kwargs["max_exposure_time"] == pytest.approx(period)
    # Each detector exposes for its own time.
    pilatus_puts = get_mock_put(rig.pilatus.driver.acquire_time).call_args_list
    iris_puts = get_mock_put(rig.iris.driver.acquire_time).call_args_list
    assert pilatus_puts[-1].args[0] == 0.002
    assert iris_puts[-1].args[0] == 0.02


def test_tomo_flyscan_short_period_names_detector(RE: RunEngine, tomo_rig: TomoRig):
    rig = tomo_rig
    configs = [rig.config(rig.pilatus, 12, 0.002), rig.config(rig.iris, 6, 0.02)]
    # Long enough for the Pilatus, too short for the Kinetix every 2nd pulse.
    with pytest.raises(ValueError, match="too short for iris1"):
        RE(_flyscan(rig, configs, trigger_period=0.005))


def test_tomo_flyscan_rejects_bad_configs_before_moving(
    RE: RunEngine, tomo_rig: TomoRig, docs
):
    rig = tomo_rig
    configs = [rig.config(rig.pilatus, 12, 0.002), rig.config(rig.iris, 5, 0.002)]
    with pytest.raises(ValueError, match="must be a multiple"):
        RE(_flyscan(rig, configs))
    assert "start" not in docs
    get_mock_put(rig.rot_motor.user_setpoint).assert_not_called()


def test_tomo_flyscan_rejects_averaging(RE: RunEngine, tomo_rig: TomoRig):
    config = tomo_rig.config(tomo_rig.pilatus, NUM, EXPOSURE, images_to_average=2)
    with pytest.raises(ValueError, match="averages 2 frames"):
        RE(_flyscan(tomo_rig, [config]))


def test_tomo_flyscan_operates_shutter_and_restores_motor(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    RE(_flyscan(tomo_rig))
    assert tomo_rig.shutter_log == ["photon_shutter:open", "photon_shutter:close"]
    get_mock_put(tomo_rig.rot_motor.motor_stop).assert_called()
    assert get_mock_put(tomo_rig.rot_motor.velocity).call_args_list[-1].args[0] == 5.0


async def test_tomo_flyscan_cleans_up_on_failure(
    RE: RunEngine, tomo_rig: TomoRig, sleeps, monkeypatch
):
    def _fail(*args, **kwargs):
        yield from bps.null()
        raise RuntimeError("boom")

    monkeypatch.setattr(flyscans, "_fly_to_streams", _fail)
    with pytest.raises(RuntimeError, match="boom"):
        RE(_flyscan(tomo_rig))
    assert await tomo_rig.photon_shutter.status.get_value() == ShutterStatus.CLOSED
    assert await tomo_rig.rot_motor.velocity.get_value() == 5.0
    get_mock_put(tomo_rig.rot_motor.motor_stop).assert_called()


def test_tomo_flyscan_requires_open_fe_shutter(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    set_mock_value(tomo_rig.fe_shutter.status, ShutterStatus.CLOSED)
    with pytest.raises(ValueError, match="fe_shutter is closed"):
        RE(_flyscan(tomo_rig))


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        (0, FailedStatus),
        (2, None),
        (MAX_MISSING_FRAMES + 1, RuntimeError),
    ],
)
def test_tomo_flyscan_missed_frames(
    RE: RunEngine,
    tomo_rig: TomoRig,
    sleeps,
    monkeypatch,
    caplog,
    missing: int,
    expected: type[Exception] | None,
):
    num = 40
    collected: list[list[str]] = []

    def _fly_dropping_frames(*args, **kwargs):
        yield from bps.null()
        raise FailedStatus("complete timed out")

    def _collect_streams(streams):
        collected.append(list(streams))
        yield from bps.null()

    async def _pilatus_index() -> int:
        return num + 1 - missing

    async def _panda_index() -> int:
        return num + 1

    monkeypatch.setattr(flyscans, "_fly_to_streams", _fly_dropping_frames)
    monkeypatch.setattr(flyscans, "_collect_streams", _collect_streams)
    set_mock_attr(tomo_rig.pilatus, "get_index", _pilatus_index)
    set_mock_attr(tomo_rig.panda, "get_index", _panda_index)

    plan = _flyscan(tomo_rig, [tomo_rig.config(tomo_rig.pilatus, num, EXPOSURE)])
    if expected is None:
        with caplog.at_level(logging.WARNING):
            RE(plan)
        assert collected == [["pilatus1", ROTATION_STREAM]]
        assert f"pilatus1 missed {missing} frame(s)" in caplog.text
    else:
        with pytest.raises(expected):
            RE(plan)
        assert collected == []
