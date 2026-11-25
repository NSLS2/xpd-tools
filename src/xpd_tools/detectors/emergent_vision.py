"""Emergent Vision (EVT) camera interface for XPD beamline at NSLS-II.

Targets the NSLS-II ADEmergentVision areaDetector driver:
https://github.com/NSLS2/ADEmergentVision

Driver quirks this module accounts for:

- Only internal (free-running) acquisition is supported; the driver configures
  the camera with ``TriggerMode=Off`` and exposes no trigger PVs.
- ``AcquireTime`` is interpreted in **milliseconds** (it is multiplied by 1000
  and sent to the camera as microseconds), unlike the areaDetector convention
  of seconds. `EmergentVisionTriggerLogic` converts from seconds for you.
- Writing ``ImageMode`` resets ``NumImages`` (to 100 for ``Multiple``), so
  ``ImageMode`` must be written before ``NumImages``.
- The frame rate is set via ``EVTFramerate`` (integer frames per second);
  ``AcquirePeriod`` is not used by the driver.
"""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated as A

from ophyd_async.core import (
    DetectorTriggerLogic,
    DeviceMock,
    EnableDisable,
    OnOff,
    SignalDict,
    SignalR,
    SignalRW,
    StandardReadable,
    StrictEnum,
    callback_on_mock_put,
    default_mock_class,
    set_mock_put_proceeds,
    set_mock_value,
    soft_signal_rw,
)
from ophyd_async.core import (
    StandardReadableFormat as Format,
)
from ophyd_async.epics.adcore import (
    ADAcquireLogic,
    ADBaseDataType,
    ADBaseIO,
    ADImageMode,
    ADWriterFactory,
    AreaDetector,
    NDFileHDF5IO,
    NDPluginBaseIO,
    trigger_info_from_driver,
)
from ophyd_async.epics.core import PvSuffix

# The driver's AcquireTime PV is in milliseconds.
EVT_ACQUIRE_TIME_SCALE = 1000.0

# Default minimum gap between frames (s); tune it at runtime through
# ``driver.readout_time``.
EVT_READOUT_TIME = 0.0


class EVTPixelFormat(StrictEnum):
    """Pixel bit depth; combined with ``ColorMode`` to select the camera format.

    See ``EVTPixelFormat`` in ADEmergentVision.template.
    """

    BIT_8 = "8 Bit"
    BIT_10 = "10 Bit"
    BIT_12 = "12 Bit"
    BIT_10_PACKED = "10 Bit Packed"
    BIT_12_PACKED = "12 Bit Packed"


class EmergentVisionDriverIO(StandardReadable, ADBaseIO):
    """Emergent Vision driver interface.

    PV names follow ``ADEmergentVision.template`` plus the standard
    ``ADBase.template`` records used by the driver.

    Note that ``acquire_time`` is in milliseconds on this driver.
    """

    # Standard driver parameters used by ADEmergentVision
    acquire_time: A[SignalRW[float], PvSuffix.rbv("AcquireTime"), Format.CONFIG_SIGNAL]
    gain: A[SignalRW[float], PvSuffix.rbv("Gain"), Format.CONFIG_SIGNAL]
    size_x: A[SignalRW[int], PvSuffix.rbv("SizeX"), Format.CONFIG_SIGNAL]
    size_y: A[SignalRW[int], PvSuffix.rbv("SizeY"), Format.CONFIG_SIGNAL]
    max_size_x: A[SignalR[int], PvSuffix("MaxSizeX_RBV")]
    max_size_y: A[SignalR[int], PvSuffix("MaxSizeY_RBV")]

    # EVT-specific parameters (ai/ao records, served as floats over CA)
    pixel_format: A[
        SignalRW[EVTPixelFormat],
        PvSuffix.rbv("EVTPixelFormat"),
        Format.CONFIG_SIGNAL,
    ]
    framerate: A[SignalRW[float], PvSuffix.rbv("EVTFramerate"), Format.CONFIG_SIGNAL]
    offset_x: A[SignalRW[float], PvSuffix.rbv("EVTOffsetX"), Format.CONFIG_SIGNAL]
    offset_y: A[SignalRW[float], PvSuffix.rbv("EVTOffsetY"), Format.CONFIG_SIGNAL]
    buffer_mode: A[SignalRW[OnOff], PvSuffix.rbv("EVTBuffMode")]
    buffer_num: A[SignalRW[float], PvSuffix.rbv("EVTBuffNum")]
    packet_size: A[SignalRW[float], PvSuffix.rbv("EVTPacketSize")]
    lut_enable: A[SignalRW[EnableDisable], PvSuffix.rbv("EVTLUTEnable")]
    auto_gain: A[
        SignalRW[EnableDisable], PvSuffix.rbv("EVTAutoGain"), Format.CONFIG_SIGNAL
    ]

    def __init__(self, prefix: str, name: str = ""):
        super().__init__(prefix, name=name)
        # Not a PV: the minimum gap between frames (s), used as the deadtime
        self.readout_time = soft_signal_rw(float, initial_value=EVT_READOUT_TIME)


def framerate_for_period(period: float) -> int:
    """Largest integer frame rate whose frame period is at least ``period`` s."""
    if period <= 0:
        raise ValueError(f"Frame period must be positive, got {period}")
    framerate = math.floor(1 / period)
    if framerate < 1:
        raise ValueError(
            f"Frame period {period} s exceeds 1 s, the longest the integer "
            "EVTFramerate (frames per second) can represent"
        )
    return framerate


@dataclass
class EmergentVisionTriggerLogic(DetectorTriggerLogic):
    """Internal-trigger logic for Emergent Vision cameras.

    Parameters
    ----------
    driver : EmergentVisionDriverIO
        The camera driver.
    """

    driver: EmergentVisionDriverIO

    def config_sigs(self) -> set[SignalR]:
        return {self.driver.readout_time}

    def get_deadtime(self, config_values: SignalDict) -> float:
        return config_values[self.driver.readout_time]

    async def prepare_internal(self, num: int, livetime: float, deadtime: float):
        """Prepare the camera for internally triggered acquisition.

        ``EVTFramerate`` is set to the largest integer rate whose period
        covers ``livetime + deadtime``. With no requested deadtime,
        ``driver.readout_time`` is used, giving the fastest rate the exposure
        allows.
        """
        image_mode = ADImageMode.CONTINUOUS if num == 0 else ADImageMode.MULTIPLE
        # The driver overwrites NumImages when ImageMode is written.
        await self.driver.image_mode.set(image_mode)
        coros = [self.driver.num_images.set(num)]
        if livetime:
            if deadtime:
                framerate = framerate_for_period(livetime + deadtime)
            else:
                # Fastest rate the exposure allows; 1 fps is the slowest the
                # integer EVTFramerate can go.
                readout = await self.driver.readout_time.get_value()
                framerate = max(1, math.floor(1 / (livetime + readout)))
            coros += [
                self.driver.acquire_time.set(livetime * EVT_ACQUIRE_TIME_SCALE),
                self.driver.framerate.set(framerate),
            ]
        await asyncio.gather(*coros)

    async def default_trigger_info(self):
        """Get default trigger info based on the number of images to acquire."""
        return await trigger_info_from_driver(self.driver.num_images)


class EmergentVisionDetectorMock(DeviceMock["EmergentVisionDetector"]):
    """Mock behaviour that simulates Emergent Vision internal acquisition."""

    async def connect(self, device: "EmergentVisionDetector") -> None:
        """Mock signals to simulate Emergent Vision camera acquisition."""
        set_mock_value(device.driver.array_size_x, 2048)
        set_mock_value(device.driver.array_size_y, 1536)
        set_mock_value(device.driver.data_type, ADBaseDataType.UINT8)

        try:
            hdf = device.get_plugin("hdf", NDFileHDF5IO)
            set_mock_value(hdf.array_size0, 1536)
            set_mock_value(hdf.array_size1, 2048)
            set_mock_value(hdf.file_path_exists, True)
        except (AttributeError, TypeError):
            hdf = None

        set_mock_value(device.driver.acquire_time, 10.0)
        set_mock_value(device.driver.framerate, 30.0)
        set_mock_value(device.driver.num_images, 1)

        async def _do_acquisition():
            num_images = await device.driver.num_images.get_value()
            acquire_time = (
                await device.driver.acquire_time.get_value() / EVT_ACQUIRE_TIME_SCALE
            )
            for _ in range(num_images):
                await asyncio.sleep(acquire_time)
                counter = await device.driver.array_counter.get_value()
                set_mock_value(device.driver.array_counter, counter + 1)
                if hdf is not None:
                    num_captured = await hdf.num_captured.get_value()
                    set_mock_value(hdf.num_captured, num_captured + 1)

            set_mock_value(device.driver.acquire, False)
            set_mock_put_proceeds(device.driver.acquire, True)

        def _on_acquire_write(value: bool) -> None:
            if value:
                set_mock_put_proceeds(device.driver.acquire, False)
                asyncio.ensure_future(_do_acquisition())

        callback_on_mock_put(device.driver.acquire, _on_acquire_write)


@default_mock_class(EmergentVisionDetectorMock)
class EmergentVisionDetector(AreaDetector[EmergentVisionDriverIO]):
    """Create an Emergent Vision AreaDetector instance.

    Only internal triggering is supported by the ADEmergentVision driver.

    Parameters
    ----------
    prefix : str
        EPICS PV prefix for the camera
    writer_factories : ADWriterFactory
        Factories for file writer plugins and their data logics
    driver_suffix : str, optional
        Suffix for the driver PV, by default "cam1:"
    plugins : dict[str, NDPluginBaseIO], optional
        Additional areaDetector plugins to include, by default None
    config_sigs : Sequence[SignalR], optional
        Additional signals to include in configuration, by default ()
    name : str, optional
        Name for the camera device, by default ""
    """

    def __init__(
        self,
        prefix: str,
        *writer_factories: ADWriterFactory,
        driver_suffix="cam1:",
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = EmergentVisionDriverIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            *writer_factories,
            acquire_logic=ADAcquireLogic(driver),
            trigger_logic=EmergentVisionTriggerLogic(driver),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )
