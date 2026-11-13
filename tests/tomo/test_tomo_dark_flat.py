"""Tests for tomo_dark_flat and the dark/flat UID tracker."""

from typing import Any, cast

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from ophyd_async.core import get_mock_put
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
        [rig.config(rig.pilatus, 1, EXPOSURE)],
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
    assert start["exposure_time"] == {"pilatus1": EXPOSURE}
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


async def test_tomo_dark_flat_per_detector_exposure(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps
):
    RE(
        tomo_dark_flat(
            [
                tomo_rig.config(tomo_rig.pilatus, 1, 0.002),
                # A dict works too; only the detector and exposure time are used.
                {
                    "detector": tomo_rig.iris,
                    "nproj": 9,
                    "exposure_time": 0.005,
                    "ttl_out": 2,
                },
            ],
            num_dark_images=NUM_DARK,
            num_flat_images=NUM_FLAT,
            sample_out_motor=tomo_rig.sample_out_motor,
            fe_shutter=tomo_rig.fe_shutter,
            photon_shutter=tomo_rig.photon_shutter,
        )
    )
    pilatus_puts = get_mock_put(tomo_rig.pilatus.driver.acquire_time).call_args_list
    iris_puts = get_mock_put(tomo_rig.iris.driver.acquire_time).call_args_list
    # Each detector takes darks and flats at its own exposure, then is restored.
    assert [c.args[0] for c in pilatus_puts] == [0.002, 0.002, 0.5]
    assert [c.args[0] for c in iris_puts] == [0.005, 0.005, 0.4]
    assert docs["start"][0]["exposure_time"] == {"pilatus1": 0.002, "iris1": 0.005}


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
            [tomo_rig.config(tomo_rig.pilatus, 1, EXPOSURE)],
            num_dark_images=NUM_DARK,
            num_flat_images=NUM_FLAT,
            use_shutter=False,
            sample_out_motor=tomo_rig.sample_out_motor,
        )
    )
    assert tomo_rig.shutter_log == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sample_out_offset": 0}, "non-zero"),
        ({"det": "xs3"}, "xs3 is an Xspress3"),
        ({"no_motor": True}, "'amTopX'"),
    ],
)
def test_tomo_dark_flat_rejects(RE: RunEngine, tomo_rig: TomoRig, kwargs, match):
    det = getattr(tomo_rig, kwargs.pop("det", "pilatus"))
    if kwargs.pop("no_motor", False):
        plan = tomo_dark_flat([tomo_rig.config(det, 1, EXPOSURE)])
    else:
        plan = tomo_dark_flat(
            [tomo_rig.config(det, 1, EXPOSURE)],
            sample_out_motor=tomo_rig.sample_out_motor,
            **kwargs,
        )
    with pytest.raises(ValueError, match=match):
        RE(plan)


def test_tracker_records_dark_flat_uid(RE: RunEngine, tomo_rig: TomoRig, sleeps):
    RE.subscribe(LastDarkFlatUIDTracker(RE.md))
    result = cast(Any, RE(_dark_flat(tomo_rig)))
    assert RE.md["last_dark_flat_uid"] == result.plan_result
    assert RE.md["last_dark_flat_scan_id"] == 1
