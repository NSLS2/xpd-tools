from enum import Enum

import pytest

from xpdtools.detectors.panda import (
    PandAConfiguration,
    PandASettingsProvider,
)


def test_dynamic_config_enum_populated() -> None:
    assert issubclass(PandAConfiguration, Enum)
    assert PandAConfiguration["SINGLE_AXIS_FLYSCAN"].name == "SINGLE_AXIS_FLYSCAN"
    assert PandAConfiguration["SINGLE_AXIS_FLYSCAN"].value == "single_axis_flyscan"


async def test_packaged_settings_provider_store() -> None:
    """Test that the PandASettingsProvider raises NotImplementedError on store."""

    provider = PandASettingsProvider()

    with pytest.raises(NotImplementedError):
        await provider.store("single_axis_flyscan", {"key": "value"})


async def test_packaged_settings_provider_retrieve() -> None:
    """Test that the PandASettingsProvider retrieves a config correctly."""

    provider = PandASettingsProvider()
    config_data = await provider.retrieve("single_axis_flyscan")

    # Check that the retrieved data is a dictionary and contains expected keys
    assert isinstance(config_data, dict)
    assert config_data["pcomp.1.enable"] == "PCAP.ACTIVE"
    assert config_data["pulse.1.trig"] == "PCOMP1.OUT"
