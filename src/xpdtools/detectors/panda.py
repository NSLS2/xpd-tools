"""PandA settings provider for XPD beamline flyscans."""

from collections.abc import Generator
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from bluesky import Msg
from ophyd_async.core import YamlSettingsProvider
from ophyd_async.fastcs.panda import HDFPanda, apply_panda_settings
from ophyd_async.plan_stubs import apply_settings_if_different, retrieve_settings

PANDA_CONFIG_PATH = Path(str(files("xpdtools.panda_configurations")))

# Built dynamically from the packaged yaml files, so member names are only known
# at runtime. cast to type[Enum] so the checker treats it as a class (issubclass,
# subscript/call lookup); reference members via PandAConfiguration["NAME"].
PandAConfiguration = cast(
    type[Enum],
    Enum(
        "PandAConfiguration",
        {p.stem.upper(): p.stem for p in PANDA_CONFIG_PATH.glob("*.yaml")},
    ),
)


class PandASettingsProvider(YamlSettingsProvider):
    """A read-only YamlSettingsProvider backed by configs shipped with this package.

    This provider is intended for use with the PandABox in XPD beamline flyscans.
    """

    def __init__(self) -> None:
        super().__init__(PANDA_CONFIG_PATH)

    async def store(self, name: str, data: dict[str, Any]) -> None:
        """Not supported for packaged configs.

        Raises
        ------
        NotImplementedError
            Always, since packaged configs are read-only.
        """
        raise NotImplementedError(
            "Cannot store settings in a packaged provider. "
            "Use ophyd_async.core.YamlSettingsProvider for writable configs."
        )


def switch_panda_configuration(
    panda: HDFPanda, configuration_name: str
) -> Generator[Msg, None, None]:
    """Switch the PandA configuration to a new one.

    Parameters
    ----------
    panda : HDFPanda
        The PandA device to configure.
    configuration_name : str
        The name of the configuration to apply (without .yaml extension).

    Raises
    ------
    FileNotFoundError
        If the specified configuration does not exist.
    """
    provider = PandASettingsProvider()
    config_data = yield from retrieve_settings(provider, configuration_name, panda)
    yield from apply_settings_if_different(config_data, apply_panda_settings)
