"""Tests for the shutter devices and plan stubs."""

import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    callback_on_mock_execute,
    get_mock_put,
    init_devices,
    set_mock_value,
)

from xpd_tools.shutter import (
    PPSShutter,
    Shutter,
    ShutterStatus,
    ensure_shutter_closed,
    ensure_shutter_open,
)


@pytest.fixture
async def shutter() -> Shutter:
    async with init_devices(mock=True):
        shutter = Shutter("SH:", name="shutter")
    callback_on_mock_execute(
        shutter.open_cmd, lambda: set_mock_value(shutter.status, ShutterStatus.OPEN)
    )
    callback_on_mock_execute(
        shutter.close_cmd,
        lambda: set_mock_value(shutter.status, ShutterStatus.CLOSED),
    )
    return shutter


async def test_shutter_set_opens_and_closes(shutter: Shutter):
    await shutter.set(True)
    assert await shutter.status.get_value() == ShutterStatus.OPEN
    await shutter.set(False)
    assert await shutter.status.get_value() == ShutterStatus.CLOSED


async def test_shutter_subscription_reports_bool(shutter: Shutter):
    readings: list[bool] = []

    def callback(reading):
        readings.append(reading["shutter"]["value"])

    shutter.subscribe_reading(callback)
    set_mock_value(shutter.status, ShutterStatus.OPEN)
    shutter.clear_sub(callback)
    set_mock_value(shutter.status, ShutterStatus.CLOSED)
    assert readings == [False, True]


def test_ensure_open_refuses_to_actuate_by_default(RE: RunEngine, shutter: Shutter):
    with pytest.raises(ValueError, match="shutter is closed"):
        RE(ensure_shutter_open(shutter))


async def test_ensure_open_and_closed_actuate(
    RE: RunEngine, shutter: Shutter, sleeps: list[float]
):
    RE(ensure_shutter_open(shutter, allow_actuation=True))
    assert await shutter.status.get_value() == ShutterStatus.OPEN
    RE(ensure_shutter_closed(shutter))
    assert await shutter.status.get_value() == ShutterStatus.CLOSED
    assert sleeps == [4.0, 4.0]


def test_ensure_closed_skips_closed_shutter(
    RE: RunEngine, shutter: Shutter, sleeps: list[float]
):
    RE(ensure_shutter_closed(shutter))
    assert sleeps == []


async def test_pps_interlock_acknowledge():
    async with init_devices(mock=True):
        pps = PPSShutter("PPS:", name="pps")
    set_mock_value(pps.interlock_ack.acknowledged, False)
    await pps.interlock_ack.set(True)
    get_mock_put(pps.interlock_ack.request).assert_called_once()
    assert await pps.interlock_ack.acknowledged.get_value()
