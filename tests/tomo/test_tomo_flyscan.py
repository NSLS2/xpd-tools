"""Tests for tomo_flyscan."""

import logging
from typing import Any, cast

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from bluesky.utils import FailedStatus
from ophyd_async.core import get_mock_put, set_mock_attr, set_mock_value
from tomo_rig import TomoRig

from xpd_tools.shutter import ShutterStatus
from xpd_tools.specs import TOMO_FLYSCAN_V1
from xpd_tools.tomo import MAX_MISSING_FRAMES, LastDarkFlatUIDTracker, tomo_flyscan
from xpd_tools.tomo.flyscans import FRAME_PERIOD_MARGIN, ROTATION_DATASET_NAME

NUM = 11
EXPOSURE = 0.001


def _flyscan(rig: TomoRig, **kwargs):
    rig.num_frames = kwargs.get("num_projections", NUM)
    kwargs.setdefault("num_projections", NUM)
    kwargs.setdefault("exposure_time", EXPOSURE)
    return tomo_flyscan([rig.pilatus], **rig.device_kwargs(), **kwargs)


def test_tomo_flyscan_documents(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    result = cast(
        Any, RE(_flyscan(tomo_rig, sample_name="rock", md={"operator": "me"}))
    )

    (start,) = docs["start"]
    assert result.plan_result == start["uid"]
    assert start["plan_name"] == "tomo_flyscan"
    assert start["detectors"] == ["pilatus1"]
    assert start["num_points"] == NUM
    assert start["rotation_motor"] == "amPhi"
    assert start["sample_name"] == "rock"
    assert start["operator"] == "me"
    assert start["tiled_specs"] == [
        {"name": TOMO_FLYSCAN_V1.name, "version": TOMO_FLYSCAN_V1.version}
    ]
    # Pilatus deadtime is 1 ms
    assert start["acquire_period"] == pytest.approx(
        EXPOSURE + 0.001 + FRAME_PERIOD_MARGIN
    )
    assert docs["stop"][0]["exit_status"] == "success"

    (descriptor,) = [d for d in docs["descriptor"] if d["name"] == "primary"]
    assert {"pilatus1", ROTATION_DATASET_NAME} <= set(descriptor["data_keys"])
    for key in ("pilatus1", ROTATION_DATASET_NAME):
        resource = next(r for r in docs["stream_resource"] if r["data_key"] == key)
        datums = [
            d for d in docs["stream_datum"] if d["stream_resource"] == resource["uid"]
        ]
        assert datums[-1]["indices"]["stop"] == NUM


def test_tomo_flyscan_sets_exposure_and_deadtime(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    RE(_flyscan(tomo_rig, exposure_time=0.002, acquire_period=0.01))
    driver = tomo_rig.pilatus.driver
    assert get_mock_put(driver.acquire_time).call_args_list[-1].args[0] == 0.002
    assert get_mock_put(driver.num_triggers).call_args_list[-1].args[0] == NUM


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

    monkeypatch.setattr(bps, "fly_stub", _fail)
    with pytest.raises(RuntimeError, match="boom"):
        RE(_flyscan(tomo_rig))
    assert await tomo_rig.photon_shutter.status.get_value() == ShutterStatus.CLOSED
    assert await tomo_rig.rot_motor.velocity.get_value() == 5.0
    get_mock_put(tomo_rig.rot_motor.motor_stop).assert_called()


def test_tomo_flyscan_without_shutter(RE: RunEngine, tomo_rig: TomoRig, sleeps):
    RE(
        tomo_flyscan(
            [tomo_rig.pilatus],
            NUM,
            EXPOSURE,
            use_shutter=False,
            panda=tomo_rig.panda,
            rot_motor=tomo_rig.rot_motor,
        )
    )
    assert tomo_rig.shutter_log == []


def test_tomo_flyscan_requires_open_fe_shutter(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    set_mock_value(tomo_rig.fe_shutter.status, ShutterStatus.CLOSED)
    with pytest.raises(ValueError, match="fe_shutter is closed"):
        RE(_flyscan(tomo_rig))


def test_tomo_flyscan_rejects_short_acquire_period(RE: RunEngine, tomo_rig: TomoRig):
    with pytest.raises(ValueError, match="shorter than the exposure"):
        RE(_flyscan(tomo_rig, exposure_time=0.01, acquire_period=0.0105))


def test_tomo_flyscan_averaging_needs_proc(RE: RunEngine, tomo_rig: TomoRig, sleeps):
    with pytest.raises(ValueError, match="needs a Proc plugin"):
        RE(_flyscan(tomo_rig, images_to_average=2))


def test_tomo_flyscan_links_last_dark_flat(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps
):
    RE.subscribe(LastDarkFlatUIDTracker(RE.md))
    RE.md["last_dark_flat_uid"] = "dark-flat-uid"
    RE(_flyscan(tomo_rig))
    assert docs["start"][0]["last_dark_flat_uid"] == "dark-flat-uid"


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
    collected: list[str | None] = []

    def _fly_stub_dropping_frames(*args, **kwargs):
        yield from bps.null()
        raise FailedStatus("complete timed out")

    def _collect(*devices, name=None, **kwargs):
        collected.append(name)
        yield from bps.null()

    async def _get_index() -> int:
        return num - missing

    monkeypatch.setattr(bps, "fly_stub", _fly_stub_dropping_frames)
    monkeypatch.setattr(bps, "collect", _collect)
    set_mock_attr(tomo_rig.pilatus, "get_index", _get_index)

    plan = _flyscan(tomo_rig, num_projections=num)
    if expected is None:
        with caplog.at_level(logging.WARNING):
            RE(plan)
        assert collected == ["primary"]
        assert f"missed {missing} frame(s)" in caplog.text
    else:
        with pytest.raises(expected):
            RE(plan)
        assert collected == []
