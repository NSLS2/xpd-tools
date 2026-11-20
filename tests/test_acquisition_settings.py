"""Tests for the acquisition settings device, its presets and its plan."""

from collections.abc import Generator
from typing import Any, cast

import bluesky.plan_stubs as bps
import pytest
from bluesky import Msg
from bluesky.plans import count
from bluesky.run_engine import RunEngine
from ophyd_async.core import init_devices

from xpd_tools.acquisition_settings import (
    AcquisitionConfiguration,
    AcquisitionPreset,
    AcquisitionSettings,
)
from xpd_tools.plans import use_acquisition_configuration

XPD_VALUES = {
    "settings-frame_acq_time": 0.5,
    "settings-dark_window": 1000.0,
    "settings-shutter_control": True,
    "settings-auto_load_calibration": True,
}


@pytest.fixture
async def settings() -> AcquisitionSettings:
    async with init_devices():
        device = AcquisitionSettings(name="settings")
    return device


def _read_all(settings: AcquisitionSettings) -> Generator[Msg, None, dict[str, Any]]:
    signals = (
        settings.frame_acq_time,
        settings.dark_window,
        settings.shutter_control,
        settings.auto_load_calibration,
    )
    values = {}
    for signal in signals:
        values[signal.name] = yield from bps.rd(signal)
    return values


def _values(RE: RunEngine, settings: AcquisitionSettings) -> dict[str, Any]:
    return cast(Any, RE(_read_all(settings))).plan_result


def test_every_configuration_has_a_preset():
    for configuration in AcquisitionConfiguration:
        assert isinstance(configuration.preset, AcquisitionPreset)


def test_starts_with_the_xpd_preset(RE: RunEngine, settings: AcquisitionSettings):
    assert _values(RE, settings) == XPD_VALUES


def test_a_value_is_changed_with_mv(RE: RunEngine, settings: AcquisitionSettings):
    RE(bps.mv(settings.frame_acq_time, 0.1, settings.dark_window, 10.0))
    values = _values(RE, settings)
    assert values["settings-frame_acq_time"] == 0.1
    assert values["settings-dark_window"] == 10.0


def test_use_acquisition_configuration_restores_the_preset(
    RE: RunEngine, settings: AcquisitionSettings
):
    RE(bps.mv(settings.frame_acq_time, 0.1, settings.shutter_control, False))
    RE(use_acquisition_configuration(settings, "xpd"))
    assert _values(RE, settings) == XPD_VALUES


def test_unknown_configuration_is_refused(RE: RunEngine, settings: AcquisitionSettings):
    with pytest.raises(ValueError, match="bogus"):
        RE(use_acquisition_configuration(settings, "bogus"))
    assert _values(RE, settings) == XPD_VALUES


def test_a_run_reading_the_device_records_the_settings_and_units(
    RE: RunEngine, settings: AcquisitionSettings
):
    descriptors: list[Any] = []
    RE(bps.mv(settings.frame_acq_time, 0.1))
    RE(
        count([settings]),
        lambda name, doc: descriptors.append(doc) if name == "descriptor" else None,
    )
    configuration = descriptors[0]["configuration"]["settings"]
    assert configuration["data"]["settings-frame_acq_time"] == 0.1
    assert configuration["data"]["settings-dark_window"] == 1000.0
    assert configuration["data_keys"]["settings-frame_acq_time"]["units"] == "s"
    assert configuration["data_keys"]["settings-dark_window"]["units"] == "min"


@pytest.mark.parametrize(
    "frame_acq_time, dark_window", [(0.0, 1.0), (-0.1, 1.0), (0.5, 0.0)]
)
def test_preset_refuses_non_positive_times(frame_acq_time: float, dark_window: float):
    with pytest.raises(ValueError, match="must be > 0"):
        AcquisitionPreset(
            frame_acq_time=frame_acq_time,
            dark_window=dark_window,
            shutter_control=True,
            auto_load_calibration=True,
        )
