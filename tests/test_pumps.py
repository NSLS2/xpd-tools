"""Offline pump behavior checks."""

import math

import pytest
from ophyd_async.core import init_devices, set_mock_value

from xpd_tools.pumps import (
    DDSSyringeController,
    UltraSyringePump,
    rate_limits,
    rate_unit_converter,
    validate_pump_settings,
    vol_unit_converter,
)


@pytest.mark.asyncio
async def test_pump_axes_and_ultra_are_readable():
    async with init_devices(mock=True):
        controller = DDSSyringeController("XF:28IDC-ES:1{Pump:Syrng-DDS:1", name="DDS1")
        ultra = UltraSyringePump(
            "XF:28IDC-ES:1{Pump:Syrng-Ultra:1}:", name="Pump_Ultra1"
        )
    for pump in (controller.p1, controller.p2, ultra):
        set_mock_value(pump.status, "Idle")
        set_mock_value(pump.read_infuse_rate, 100.0)
        set_mock_value(pump.read_infuse_rate_unit, "ul/min")
        readings = await pump.read()
        assert readings[pump.status.name]["value"] == "Idle"
        assert readings[pump.read_infuse_rate.name]["value"] == 100.0
        assert pump.infuse_rate.name in await pump.read_configuration()
        assert pump.pump_infuse.name not in readings


def test_syringe_policy():
    pump = UltraSyringePump("mock:", name="ultra")
    assert rate_limits(pump, "steel", 50) == (118.038, 122.578)
    assert math.isclose(rate_unit_converter("ml/hr", "ul/min"), 1000 / 60)
    assert math.isclose(vol_unit_converter("nl", "ml"), 1e-6)
    validate_pump_settings(pump, "steel", 50, 30, 100)
    for rate in (-1, float("nan"), 200000):
        with pytest.raises(ValueError):
            validate_pump_settings(pump, "steel", 50, 30, rate)
    with pytest.raises(ValueError):
        validate_pump_settings(pump, "steel", 50, 51, 100)
