"""RunEngine pump behavior without contacting EPICS hardware."""

from dataclasses import dataclass

import pytest
from bluesky import plan_stubs as bps
from ophyd_async.core import (
    callback_on_mock_execute,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)

from xpd_tools.pump_plans import (
    clear_pumps,
    configure_infusion,
    configure_withdrawal,
    enable_pumps,
    pump_session,
    read_pump_metadata,
    start_infusion,
    start_withdrawal,
    stop_pumps,
    wait_for_equilibrium,
)
from xpd_tools.pumps import DDSSyringeController, UltraSyringePump


@dataclass(frozen=True)
class PumpSet:
    dds: DDSSyringeController
    other_dds: DDSSyringeController
    ultra: UltraSyringePump


@pytest.fixture
async def pumps():
    async with init_devices(mock=True):
        dds = DDSSyringeController("DDS:1", name="DDS1")
        other_dds = DDSSyringeController("DDS:2", name="DDS2")
        ultra = UltraSyringePump("Ultra:1}:", name="Pump_Ultra1")

    for controller in (dds, other_dds):
        set_mock_value(controller.communication, "Disabled")
        set_mock_value(controller.update_pump, "1 second")
    set_mock_value(ultra.communication, "Disabled")
    set_mock_value(ultra.update_pump, "Passive")

    for pump in (dds.p1, dds.p2, other_dds.p1, other_dds.p2, ultra):
        set_mock_value(pump.read_infused, 1)
        set_mock_value(pump.read_withdrawn, 2)
    return PumpSet(dds, other_dds, ultra)


def test_enable_does_not_start_or_clear_pumps(RE, pumps):
    RE(enable_pumps([pumps.dds, pumps.ultra]))
    assert RE(bps.rd(pumps.dds.communication)).plan_result == "Enabled"
    assert RE(bps.rd(pumps.ultra.communication)).plan_result == "Enabled"
    for axis in (pumps.dds.p1, pumps.dds.p2, pumps.ultra):
        assert RE(bps.rd(axis.status)).plan_result == "Idle"
        assert RE(bps.rd(axis.read_infused)).plan_result == 1
        assert RE(bps.rd(axis.read_withdrawn)).plan_result == 2


def test_clear_selected_pumps_leaves_sibling_counters_and_flow_unchanged(RE, pumps):
    set_mock_value(pumps.dds.p1.status, "Infusing")
    set_mock_value(pumps.dds.p2.status, "Withdrawing")

    RE(clear_pumps([pumps.dds.p1, pumps.ultra]))

    for pump in (pumps.dds.p1, pumps.ultra):
        assert RE(bps.rd(pump.read_infused)).plan_result == 0
        assert RE(bps.rd(pump.read_withdrawn)).plan_result == 0
    for axis in (pumps.dds.p2, pumps.other_dds.p1, pumps.other_dds.p2):
        assert RE(bps.rd(axis.read_infused)).plan_result == 1
        assert RE(bps.rd(axis.read_withdrawn)).plan_result == 2
    assert RE(bps.rd(pumps.dds.p1.status)).plan_result == "Infusing"
    assert RE(bps.rd(pumps.dds.p2.status)).plan_result == "Withdrawing"
    assert RE(bps.rd(pumps.ultra.status)).plan_result == "Idle"
    assert RE(bps.rd(pumps.dds.communication)).plan_result == "Disabled"
    assert RE(bps.rd(pumps.ultra.communication)).plan_result == "Disabled"


def test_enable_deduplicates_controller_and_preserves_update_settings(RE, pumps):
    dds_enables = []
    callback_on_mock_put(
        pumps.dds.communication, lambda value: dds_enables.append(value)
    )

    RE(enable_pumps([pumps.dds, pumps.dds, pumps.ultra], update="2 second"))

    assert dds_enables == ["Enabled"]
    assert RE(bps.rd(pumps.dds.update_pump)).plan_result == "1 second"
    assert RE(bps.rd(pumps.ultra.update_pump)).plan_result == "2 second"


def test_configure_start_zero_and_metadata(RE, pumps):
    first, second = pumps.dds.p1, pumps.dds.p2
    RE(
        configure_infusion(
            [first, second],
            [50, 50],
            ["30 ml", "30 ml"],
            ["100 ul/min", "0 ul/min"],
            ["steel", "steel"],
            [True, True],
        )
    )
    assert RE(bps.rd(first.infuse_rate)).plan_result == 100
    assert RE(bps.rd(first.infuse_rate_unit)).plan_result == "ul/min"
    assert RE(bps.rd(first.target_vol)).plan_result == 30
    RE(start_infusion([first, second], [100, "0 ul/min"]))
    assert RE(read_pump_metadata([first, second])).plan_result == {
        "pumps": [first.name, second.name],
        "infuse_rate": [100, 0],
        "infuse_rate_unit": ["ul/min", ""],
        "pump_status": ["Infusing", "Idle"],
    }
    RE(stop_pumps([first, second]))
    assert RE(bps.rd(first.status)).plan_result == "Idle"


def test_preflight_rejects_wrong_syringe_and_limit(RE, pumps):
    first, second = pumps.dds.p1, pumps.dds.p2
    set_mock_value(second.read_min_infuse, 35.21)
    with pytest.raises(ValueError, match="selected DDS syringe"):
        RE(
            configure_infusion(
                [first, second],
                [50, 50],
                ["30 ml"] * 2,
                [100, 100],
                ["steel"] * 2,
                [True] * 2,
            )
        )
    assert RE(bps.rd(first.infuse_rate)).plan_result == 0
    with pytest.raises(ValueError, match="outside syringe range"):
        RE(configure_infusion([first], [50], ["30 ml"], [90000], ["steel"], [True]))


def test_ultra_and_withdrawal(RE, pumps):
    first, second, ultra = pumps.dds.p1, pumps.dds.p2, pumps.ultra
    with pytest.warns(UserWarning, match="Ultra panel"):
        RE(configure_infusion([ultra], [50], ["30 ml"], [100], ["steel"], [True]))
    RE(configure_withdrawal([first], [50], ["30 ml"], [100], ["steel"], [True]))
    RE(start_withdrawal([first], [100]))
    assert RE(bps.rd(first.status)).plan_result == "Withdrawing"
    assert RE(bps.rd(first.read_withdraw_rate)).plan_result == 100
    assert RE(bps.rd(second.status)).plan_result == "Idle"
    RE(stop_pumps([first]))


def test_session_stops_on_success_failure_and_cleanup_failure(RE, pumps):
    first, second = pumps.dds.p1, pumps.dds.p2

    def setup():
        yield from bps.null()

    def body():
        yield from start_infusion([first], [100])
        yield from bps.null()

    RE(pump_session(body, setup=setup(), pumps=[first]))
    assert RE(bps.rd(first.status)).plan_result == "Idle"

    def failing():
        yield from start_infusion([first, second], [100, 100])
        raise RuntimeError("acquisition failed")

    with pytest.raises(RuntimeError, match="acquisition failed"):
        RE(pump_session(failing, setup=setup(), pumps=[first, second]))
    assert RE(bps.rd(second.status)).plan_result == "Idle"
    tracked = []

    def failing_setup():
        tracked.append(first)
        yield from start_infusion([first], [100])
        raise RuntimeError("setup failed")

    with pytest.raises(RuntimeError, match="setup failed"):
        RE(pump_session(body, setup=failing_setup(), pumps=tracked))
    assert RE(bps.rd(first.status)).plan_result == "Idle"

    def failed_stop():
        raise RuntimeError("STOP failed")

    callback_on_mock_execute(first.pump_stop, failed_stop)
    with pytest.raises(ExceptionGroup, match="Pump stop failures"):
        RE(stop_pumps([first, second]))
    assert RE(bps.rd(second.status)).plan_result == "Idle"
    with pytest.raises(ExceptionGroup, match="Pump session and cleanup failures"):
        RE(pump_session(failing, setup=setup(), pumps=[first, second]))


def test_stop_dispatches_all_pumps_before_waiting_for_status(RE, pumps, monkeypatch):
    first, second = pumps.dds.p1, pumps.dds.p2
    events = []
    callback_on_mock_execute(first.pump_stop, lambda: events.append("first STOP"))
    callback_on_mock_execute(second.pump_stop, lambda: events.append("second STOP"))

    async def stalled_status(*args, **kwargs):
        events.append("status wait")
        raise TimeoutError("pump status did not change")

    monkeypatch.setattr("xpd_tools.pump_plans.wait_for_value", stalled_status)

    with pytest.raises(ExceptionGroup, match="Pump stop failures"):
        RE(stop_pumps([first, second]))

    assert events == [
        "first STOP",
        "second STOP",
        "status wait",
        "status wait",
    ]


def test_equilibrium_segment_flow(RE, pumps):
    first, second = pumps.dds.p1, pumps.dds.p2
    set_mock_value(first.status, "Infusing")
    set_mock_value(first.read_infuse_rate, 100)
    set_mock_value(first.read_infuse_rate_unit, "ul/min")
    duration = RE(wait_for_equilibrium([["0 cm", first]], ratio=1)).plan_result
    assert duration == 0
    with pytest.raises(ValueError, match="no positive infusing flow"):
        RE(wait_for_equilibrium([["10 cm", second]], ratio=1))


def test_tubing_diameters(RE, pumps):
    rates = (100, 200)  # ul/min
    height = 1
    import numpy as np

    first = pumps.dds.p1
    set_mock_value(first.status, "Infusing")
    set_mock_value(first.read_infuse_rate_unit, "ul/min")

    def vol_calc_time(diameter, height, rate):
        volume = np.pi * (diameter / 2.0) ** 2.0 * (height * 10)  # cm to mm, mm³ to uL
        return volume / rate * 60

    diameters = {
        "positive": 1.0,  # area = 0.78539816339
        "negative": -1.0,
        "NaN": np.nan,
        "string": "1",
    }

    for condition, val in diameters.items():
        if condition == "positive":
            durations = []
            for rate in rates:
                set_mock_value(first.read_infuse_rate, rate)
                duration = RE(
                    wait_for_equilibrium(
                        mixer_pump_list=[[f"{height} cm", first]],
                        ratio=1,
                        tubing_ID_mm=val,
                    )
                ).plan_result
                assert duration == vol_calc_time(val, height, rate)
                durations.append(duration)
            assert durations[0] == 2 * durations[1]
        elif condition == "negative":
            with pytest.raises(
                ValueError,
                match="Nonempty mixer and nonnegative residence geometry required",
            ):
                duration = RE(
                    wait_for_equilibrium(
                        mixer_pump_list=[[f"{height} cm", first]],
                        ratio=1,
                        tubing_ID_mm=val,
                    )
                ).plan_result
        elif condition == "NaN":
            with pytest.raises(
                ValueError,
                match="Nonempty mixer and nonnegative residence geometry required",
            ):
                duration = RE(
                    wait_for_equilibrium(
                        mixer_pump_list=[[f"{height} cm", first]],
                        ratio=1,
                        tubing_ID_mm=val,
                    )
                ).plan_result
        elif condition == "string":
            with pytest.raises(TypeError, match="must be real number, not str"):
                duration = RE(
                    wait_for_equilibrium(
                        mixer_pump_list=[[f"{height} cm", first]],
                        ratio=1,
                        tubing_ID_mm=val,
                    )
                ).plan_result
