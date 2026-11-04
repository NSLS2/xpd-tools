"""Light sources and shutters of the in-situ UV-Vis spectroscopy setup."""

from typing import Annotated as A

from ophyd_async.core import DeviceMock, SignalRW, default_mock_class, set_mock_value
from ophyd_async.epics.core import EpicsDevice, PvSuffix


class UVVisLightPathMock(DeviceMock["UVVisLightPath"]):
    """Mock-mode light path that starts with the LED off and the shutter closed."""

    async def connect(self, device: "UVVisLightPath") -> None:
        """Switch the LED off and close the absorbance shutter."""
        set_mock_value(device.led, "Low")
        set_mock_value(device.uv_shutter, "Low")


@default_mock_class(UVVisLightPathMock)
class UVVisLightPath(EpicsDevice):
    """Represent the excitation LED and absorbance shutter of the UV-Vis setup.

    Parameters
    ----------
    prefix : str, optional
        Light PV prefix; ``"XF:28IDC-ES:1{Light:"`` in hutch C.
    with_pvi : bool, optional
        Use PVI discovery instead of static suffixes; default is False.
    name : str, optional
        Device name propagated to its signals; default is empty.
    connector : DeviceConnector, optional
        Prebuilt connector, mutually exclusive with a nonempty prefix.

    Attributes
    ----------
    led : SignalRW[str]
        Fluorescence-excitation LED: ``"High"`` is on, ``"Low"`` is off.
    uv_shutter : SignalRW[str]
        Absorbance light shutter: ``"High"`` is open, ``"Low"`` is closed.
    """

    led: A[SignalRW[str], PvSuffix("Flu-LED:1}Cmd")]
    uv_shutter: A[SignalRW[str], PvSuffix("Abs-Sht:1}Cmd")]
