"""Tests for tomo_dark_flat and the dark/flat UID tracker."""

from typing import Any, cast

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.core import get_mock_put, init_devices
from tomo_rig import TomoRig

from xpd_tools.shutter import ShutterStatus
from xpd_tools.specs import TOMO_DARK_FLAT_V1
from xpd_tools.tomo import LastDarkFlatUIDTracker, tomo_dark_flat

EXPOSURE = 0.001
NUM_DARK = 3
NUM_FLAT = 4


def _dark_flat(rig: TomoRig, **kwargs):
    kwargs.setdefault("num_dark_images", NUM_DARK)
    kwargs.setdefault("num_flat_images", NUM_FLAT)
    return tomo_dark_flat(
        [rig.pilatus],
        EXPOSURE,
        sample_out_motor=rig.sample_out_motor,
        fe_shutter=rig.fe_shutter,
        photon_shutter=rig.photon_shutter,
        **kwargs,
    )


def test_tomo_dark_flat_documents(RE: RunEngine, tomo_rig: TomoRig, docs, sleeps):
    result = cast(
        Any, RE(_dark_flat(tomo_rig, sample_out_offset=1.5, md={"note": "x"}))
    )

    (start,) = docs["start"]
    assert result.plan_result == start["uid"]
    assert start["plan_name"] == "tomo_dark_flat"
    assert start["detectors"] == ["pilatus1"]
    assert start["exposure_time"] == EXPOSURE
    assert start["num_dark_images"] == NUM_DARK
    assert start["num_flat_images"] == NUM_FLAT
    assert start["sample_out_motor"] == "amTopX"
    assert start["sample_out_offset"] == 1.5
    assert start["note"] == "x"
    assert start["tiled_specs"] == [
        {"name": TOMO_DARK_FLAT_V1.name, "version": TOMO_DARK_FLAT_V1.version}
    ]
    assert [d["name"] for d in docs["descriptor"]] == ["dark", "flat"]
    assert docs["stop"][0]["exit_status"] == "success"


async def test_tomo_dark_flat_moves_sample_and_shutter(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    events: list[str] = []
    setpoint = tomo_rig.sample_out_motor.user_setpoint
    get_mock_put(setpoint).side_effect = lambda v, **_: events.append(f"x={v}")
    tomo_rig.shutter_log = events
    for shutter in (tomo_rig.fe_shutter, tomo_rig.photon_shutter):
        from tomo_rig import _make_shutter_instant  # noqa: PLC2701

        _make_shutter_instant(shutter, events)

    RE(_dark_flat(tomo_rig, sample_out_offset=2.0, sample_out_velocity=0.1))

    # Sample out (from 3.0), shutter open for the flats, shutter closed before the
    # sample moves back in.
    assert events == [
        "x=5.0",
        "photon_shutter:open",
        "photon_shutter:close",
        "x=3.0",
    ]
    velocity_puts = get_mock_put(tomo_rig.sample_out_motor.velocity).call_args_list
    assert [c.args[0] for c in velocity_puts] == [0.1, 1.0]
    assert await tomo_rig.photon_shutter.status.get_value() == ShutterStatus.CLOSED
    assert await tomo_rig.sample_out_motor.user_readback.get_value() == 3.0


async def test_tomo_dark_flat_restores_exposure_time(
    RE: RunEngine, tomo_rig: TomoRig, sleeps
):
    RE(_dark_flat(tomo_rig))
    assert await tomo_rig.pilatus.driver.acquire_time.get_value() == 0.5


async def test_tomo_dark_flat_cleans_up_on_failure(
    RE: RunEngine, tomo_rig: TomoRig, sleeps, monkeypatch
):
    original = bps.trigger_and_read

    def _fail_flats(devices, name="primary"):
        if name == "flat":
            raise RuntimeError("boom")
        return (yield from original(devices, name=name))

    monkeypatch.setattr(bps, "trigger_and_read", _fail_flats)
    with pytest.raises(RuntimeError, match="boom"):
        RE(_dark_flat(tomo_rig, sample_out_velocity=0.1))
    assert await tomo_rig.photon_shutter.status.get_value() == ShutterStatus.CLOSED
    assert await tomo_rig.sample_out_motor.user_readback.get_value() == 3.0
    assert await tomo_rig.sample_out_motor.velocity.get_value() == 1.0
    assert await tomo_rig.pilatus.driver.acquire_time.get_value() == 0.5


def test_tomo_dark_flat_closes_shutter_before_return_on_failure(
    RE: RunEngine, tomo_rig: TomoRig, sleeps, monkeypatch
):
    from tomo_rig import _make_shutter_instant  # noqa: PLC2701

    events: list[str] = []
    setpoint = tomo_rig.sample_out_motor.user_setpoint
    get_mock_put(setpoint).side_effect = lambda v, **_: events.append(f"x={v}")
    _make_shutter_instant(tomo_rig.photon_shutter, events)

    original = bps.trigger_and_read

    def _fail_flats(devices, name="primary"):
        if name == "flat":
            raise RuntimeError("boom")
        return (yield from original(devices, name=name))

    monkeypatch.setattr(bps, "trigger_and_read", _fail_flats)
    with pytest.raises(RuntimeError, match="boom"):
        RE(_dark_flat(tomo_rig))
    # The sample only moves back into the beam once the shutter is closed.
    assert events == [
        "x=5.0",
        "photon_shutter:open",
        "photon_shutter:close",
        "x=3.0",
    ]


def test_tomo_dark_flat_without_shutter(RE: RunEngine, tomo_rig: TomoRig, sleeps):
    RE(
        tomo_dark_flat(
            [tomo_rig.pilatus],
            EXPOSURE,
            num_dark_images=NUM_DARK,
            num_flat_images=NUM_FLAT,
            use_shutter=False,
            sample_out_motor=tomo_rig.sample_out_motor,
        )
    )
    assert tomo_rig.shutter_log == []


def test_tomo_dark_flat_rejects_zero_offset(RE: RunEngine, tomo_rig: TomoRig):
    with pytest.raises(ValueError, match="non-zero"):
        RE(_dark_flat(tomo_rig, sample_out_offset=0))


async def test_tomo_dark_flat_rejects_xspress3(RE: RunEngine, tomo_rig: TomoRig):
    async with init_devices(mock=True):
        xs3 = Xspress3Detector("XSP:", name="xs3")
    with pytest.raises(ValueError, match="xs3 is an Xspress3"):
        RE(tomo_dark_flat([xs3], EXPOSURE))  # type: ignore[list-item]


def test_tomo_dark_flat_requires_a_sample_out_motor(RE: RunEngine, tomo_rig: TomoRig):
    with pytest.raises(ValueError, match="'amTopX'"):
        RE(tomo_dark_flat([tomo_rig.pilatus], EXPOSURE))


def test_tracker_records_dark_flat_uid(RE: RunEngine, tomo_rig: TomoRig, sleeps):
    RE.subscribe(LastDarkFlatUIDTracker(RE.md))
    result = cast(Any, RE(_dark_flat(tomo_rig)))
    assert RE.md["last_dark_flat_uid"] == result.plan_result
    assert RE.md["last_dark_flat_scan_id"] == 1
