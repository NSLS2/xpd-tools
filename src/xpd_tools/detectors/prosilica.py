"""Prosilica (Allied Vision) camera interface for XPD beamline at NSLS-II."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated as A

from ophyd_async.core import (
    DetectorTriggerLogic,
    DeviceMock,
    SignalDict,
    SignalR,
    SignalRW,
    StandardReadable,
    StrictEnum,
    callback_on_mock_put,
    default_mock_class,
    set_mock_put_proceeds,
    set_mock_value,
)
from ophyd_async.core import (
    StandardReadableFormat as Format,
)
from ophyd_async.epics.adcore import (
    ADAcquireLogic,
    ADBaseDataType,
    ADBaseIO,
    ADWriterFactory,
    AreaDetector,
    NDFileHDF5IO,
    NDPluginBaseIO,
    prepare_exposures,
    trigger_info_from_driver,
)
from ophyd_async.epics.core import (
    PvSuffix,
)

# Approximate frame readout time; the actual value depends on camera model,
# ROI, binning and network bandwidth.
PROSILICA_READOUT_TIME = 0.01


class ProsilicaTriggerMode(StrictEnum):
    """Trigger modes for the Prosilica camera.

    ``SYNC_IN_*`` select the external trigger input line.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#implementation-of-standard-driver-parameters
    """

    FREE_RUN = "Free Run"
    SYNC_IN_1 = "Sync In 1"
    SYNC_IN_2 = "Sync In 2"
    SYNC_IN_3 = "Sync In 3"
    SYNC_IN_4 = "Sync In 4"
    FIXED_RATE = "Fixed Rate"
    SOFTWARE = "Software"


SYNC_IN_TRIGGER_MODES = (
    ProsilicaTriggerMode.SYNC_IN_1,
    ProsilicaTriggerMode.SYNC_IN_2,
    ProsilicaTriggerMode.SYNC_IN_3,
    ProsilicaTriggerMode.SYNC_IN_4,
)


class ProsilicaBayerConvert(StrictEnum):
    """Color conversion applied when the color mode is Bayer.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    NONE = "None"
    RGB1 = "RGB1"
    RGB2 = "RGB2"
    RGB3 = "RGB3"


class ProsilicaAutoMode(StrictEnum):
    """Gain modes for the Prosilica camera.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    MANUAL = "Manual"
    AUTO_ONCE = "AutoOnce"
    AUTO = "Auto"


class ProsilicaExposureMode(StrictEnum):
    """Exposure modes for the Prosilica camera.

    ``EXTERNAL`` lets the external trigger pulse width set the exposure time.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    MANUAL = "Manual"
    AUTO_ONCE = "AutoOnce"
    AUTO = "Auto"
    EXTERNAL = "External"


class ProsilicaTriggerEvent(StrictEnum):
    """Edge or level of the external trigger signal for ``Sync In`` modes.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    RISING_EDGE = "Rising edge"
    FALLING_EDGE = "Falling edge"
    ANY_EDGE = "Any edge"
    HIGH_LEVEL = "High level"
    LOW_LEVEL = "Low level"


class ProsilicaTriggerOverlap(StrictEnum):
    """Behaviour when a trigger arrives before the camera is ready.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    OFF = "Off"
    PREVIOUS_FRAME = "Previous frame"


class ProsilicaSyncOutMode(StrictEnum):
    """Modes of the ``Sync Out`` signals.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    GPO = "GPO"
    ACQ_TRIG_READY = "AcqTrigReady"
    FRAME_TRIG_READY = "FrameTrigReady"
    FRAME_TRIGGER = "FrameTrigger"
    EXPOSING = "Exposing"
    FRAME_READOUT = "FrameReadout"
    IMAGING = "Imaging"
    ACQUIRING = "Acquiring"
    SYNC_IN_1 = "SyncIn1"
    SYNC_IN_2 = "SyncIn2"
    SYNC_IN_3 = "SyncIn3"
    SYNC_IN_4 = "SyncIn4"
    STROBE_1 = "Strobe1"
    STROBE_2 = "Strobe2"
    STROBE_3 = "Strobe3"
    STROBE_4 = "Strobe4"


class ProsilicaStrobeMode(StrictEnum):
    """Modes of the ``Strobe 1`` signal.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    ACQ_TRIG_READY = "AcqTrigReady"
    FRAME_TRIG_READY = "FrameTrigReady"
    FRAME_TRIGGER = "FrameTrigger"
    EXPOSING = "Exposing"
    FRAME_READOUT = "FrameReadout"
    ACQUIRING = "Acquiring"
    SYNC_IN_1 = "SyncIn1"
    SYNC_IN_2 = "SyncIn2"
    SYNC_IN_3 = "SyncIn3"
    SYNC_IN_4 = "SyncIn4"


class ProsilicaTimestampType(StrictEnum):
    """Source of the NDArray timestamp.

    The template defines the fourth menu choice twice ("EPICS" then "IOC"),
    so only "IOC" is served by the IOC.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst#prosilica-specific-parameters
    """

    NATIVE_TICKS = "NativeTicks"
    NATIVE_SECONDS = "NativeSeconds"
    POSIX = "POSIX"
    IOC = "IOC"


class ProsilicaLegacyDriverIO(StandardReadable, ADBaseIO):
    """Prosilica driver interface for older ADProsilica IOCs (e.g. 2.5.0).

    These IOCs lack the ``GainMode``, ``ExposureMode`` and
    ``TemperatureMainboard`` PVs, which are added by `ProsilicaDriverIO`.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst
    """

    # Standard Driver Parameters
    trigger_mode: A[SignalRW[ProsilicaTriggerMode], PvSuffix.rbv("TriggerMode")]
    num_images: A[SignalRW[int], PvSuffix.rbv("NumImages")]
    num_images_counter: A[SignalR[int], PvSuffix("NumImagesCounter_RBV")]
    acquire_time: A[SignalRW[float], PvSuffix.rbv("AcquireTime"), Format.CONFIG_SIGNAL]
    acquire_period: A[
        SignalRW[float], PvSuffix.rbv("AcquirePeriod"), Format.CONFIG_SIGNAL
    ]
    gain: A[SignalRW[float], PvSuffix.rbv("Gain"), Format.CONFIG_SIGNAL]
    bin_x: A[SignalRW[int], PvSuffix.rbv("BinX"), Format.CONFIG_SIGNAL]
    bin_y: A[SignalRW[int], PvSuffix.rbv("BinY"), Format.CONFIG_SIGNAL]
    min_x: A[SignalRW[int], PvSuffix.rbv("MinX"), Format.CONFIG_SIGNAL]
    min_y: A[SignalRW[int], PvSuffix.rbv("MinY"), Format.CONFIG_SIGNAL]
    size_x: A[SignalRW[int], PvSuffix.rbv("SizeX"), Format.CONFIG_SIGNAL]
    size_y: A[SignalRW[int], PvSuffix.rbv("SizeY"), Format.CONFIG_SIGNAL]
    max_size_x: A[SignalR[int], PvSuffix("MaxSizeX_RBV")]
    max_size_y: A[SignalR[int], PvSuffix("MaxSizeY_RBV")]
    manufacturer: A[SignalR[str], PvSuffix("Manufacturer_RBV"), Format.CONFIG_SIGNAL]
    model: A[SignalR[str], PvSuffix("Model_RBV"), Format.CONFIG_SIGNAL]
    serial_number: A[SignalR[str], PvSuffix("SerialNumber_RBV"), Format.CONFIG_SIGNAL]
    firmware_version: A[
        SignalR[str], PvSuffix("FirmwareVersion_RBV"), Format.CONFIG_SIGNAL
    ]
    sdk_version: A[SignalR[str], PvSuffix("SDKVersion_RBV"), Format.CONFIG_SIGNAL]
    driver_version: A[SignalR[str], PvSuffix("DriverVersion_RBV"), Format.CONFIG_SIGNAL]

    # Bayer Color Conversion
    bayer_convert: A[
        SignalRW[ProsilicaBayerConvert],
        PvSuffix.rbv("BayerConvert"),
        Format.CONFIG_SIGNAL,
    ]

    # Trigger and I/O Control
    trigger_event: A[
        SignalRW[ProsilicaTriggerEvent],
        PvSuffix.rbv("TriggerEvent"),
        Format.CONFIG_SIGNAL,
    ]
    trigger_delay: A[
        SignalRW[float], PvSuffix.rbv("TriggerDelay"), Format.CONFIG_SIGNAL
    ]
    trigger_overlap: A[
        SignalRW[ProsilicaTriggerOverlap], PvSuffix.rbv("TriggerOverlap")
    ]
    trigger_software: A[SignalRW[bool], PvSuffix("TriggerSoftware")]
    sync_in1_level: A[SignalR[bool], PvSuffix("SyncIn1Level_RBV")]
    sync_in2_level: A[SignalR[bool], PvSuffix("SyncIn2Level_RBV")]
    sync_out1_mode: A[SignalRW[ProsilicaSyncOutMode], PvSuffix.rbv("SyncOut1Mode")]
    sync_out1_level: A[SignalRW[bool], PvSuffix.rbv("SyncOut1Level")]
    sync_out1_invert: A[SignalRW[bool], PvSuffix.rbv("SyncOut1Invert")]
    sync_out2_mode: A[SignalRW[ProsilicaSyncOutMode], PvSuffix.rbv("SyncOut2Mode")]
    sync_out2_level: A[SignalRW[bool], PvSuffix.rbv("SyncOut2Level")]
    sync_out2_invert: A[SignalRW[bool], PvSuffix.rbv("SyncOut2Invert")]
    sync_out3_mode: A[SignalRW[ProsilicaSyncOutMode], PvSuffix.rbv("SyncOut3Mode")]
    sync_out3_level: A[SignalRW[bool], PvSuffix.rbv("SyncOut3Level")]
    sync_out3_invert: A[SignalRW[bool], PvSuffix.rbv("SyncOut3Invert")]
    strobe1_mode: A[SignalRW[ProsilicaStrobeMode], PvSuffix.rbv("Strobe1Mode")]
    strobe1_ctl_duration: A[SignalRW[bool], PvSuffix.rbv("Strobe1CtlDuration")]
    strobe1_delay: A[SignalRW[float], PvSuffix.rbv("Strobe1Delay")]
    strobe1_duration: A[SignalRW[float], PvSuffix.rbv("Strobe1Duration")]

    # Timestamp Control
    timestamp_type: A[SignalRW[ProsilicaTimestampType], PvSuffix.rbv("PSTimestampType")]
    reset_timer: A[SignalRW[int], PvSuffix("PSResetTimer")]

    # Statistics Information
    read_statistics: A[SignalRW[int], PvSuffix("PSReadStatistics")]
    ps_driver_type: A[SignalR[str], PvSuffix("PSDriverType_RBV")]
    filter_version: A[SignalR[str], PvSuffix("PSFilterVersion_RBV")]
    frame_rate: A[SignalR[float], PvSuffix("PSFrameRate_RBV")]
    byte_rate: A[SignalRW[int], PvSuffix.rbv("PSByteRate")]
    packet_size: A[SignalR[int], PvSuffix("PSPacketSize_RBV")]
    frames_completed: A[SignalR[int], PvSuffix("PSFramesCompleted_RBV")]
    frames_dropped: A[SignalR[int], PvSuffix("PSFramesDropped_RBV")]
    packets_erroneous: A[SignalR[int], PvSuffix("PSPacketsErroneous_RBV")]
    packets_missed: A[SignalR[int], PvSuffix("PSPacketsMissed_RBV")]
    packets_received: A[SignalR[int], PvSuffix("PSPacketsReceived_RBV")]
    packets_requested: A[SignalR[int], PvSuffix("PSPacketsRequested_RBV")]
    packets_resent: A[SignalR[int], PvSuffix("PSPacketsResent_RBV")]
    bad_frame_counter: A[SignalR[int], PvSuffix("PSBadFrameCounter_RBV")]


class ProsilicaDriverIO(ProsilicaLegacyDriverIO):
    """Prosilica driver interface.

    PV names follow ``prosilica.template`` from the ADProsilica repository.

    See https://github.com/areaDetector/ADProsilica/blob/master/docs/ADProsilica/ADProsilica.rst
    """

    # Gain and Exposure Modes
    gain_mode: A[
        SignalRW[ProsilicaAutoMode], PvSuffix.rbv("GainMode"), Format.CONFIG_SIGNAL
    ]
    exposure_mode: A[
        SignalRW[ProsilicaExposureMode],
        PvSuffix.rbv("ExposureMode"),
        Format.CONFIG_SIGNAL,
    ]

    # Temperature
    temperature_mainboard: A[SignalR[float], PvSuffix("TemperatureMainboard_RBV")]


@dataclass
class ProsilicaLegacyTriggerLogic(DetectorTriggerLogic):
    """Trigger logic for older Prosilica IOCs without an ``ExposureMode`` PV.

    Level (pulse-width) triggering is not supported because it requires
    ``ExposureMode=External``.

    Parameters
    ----------
    driver : ProsilicaLegacyDriverIO
        The camera driver.
    trigger_input : ProsilicaTriggerMode, optional
        ``Sync In`` line used for external triggers, by default ``SYNC_IN_1``.
    """

    driver: ProsilicaLegacyDriverIO
    trigger_input: ProsilicaTriggerMode = ProsilicaTriggerMode.SYNC_IN_1

    def __post_init__(self):
        """Validate that ``trigger_input`` is a ``Sync In`` line."""
        if self.trigger_input not in SYNC_IN_TRIGGER_MODES:
            allowed = [m.value for m in SYNC_IN_TRIGGER_MODES]
            raise ValueError(
                f"trigger_input must be one of {allowed}, got {self.trigger_input!r}"
            )

    def get_deadtime(self, config_values: SignalDict) -> float:
        return PROSILICA_READOUT_TIME

    def _set_exposure_mode(self, mode: ProsilicaExposureMode) -> list:
        """Coroutines that set the exposure mode; legacy IOCs have none."""
        return []

    async def prepare_internal(self, num: int, livetime: float, deadtime: float):
        """Prepare the camera for internally triggered acquisition.

        With no requested deadtime the camera free-runs as fast as the
        exposure and readout allow; otherwise the frame period is fixed.
        """
        trigger_mode = (
            ProsilicaTriggerMode.FIXED_RATE
            if deadtime
            else ProsilicaTriggerMode.FREE_RUN
        )
        await asyncio.gather(
            self.driver.trigger_mode.set(trigger_mode),
            *self._set_exposure_mode(ProsilicaExposureMode.MANUAL),
            prepare_exposures(self.driver, num, livetime, deadtime),
        )

    async def prepare_edge(self, num: int, livetime: float):
        """Prepare the camera to take one frame per rising trigger edge."""
        await asyncio.gather(
            self.driver.trigger_mode.set(self.trigger_input),
            self.driver.trigger_event.set(ProsilicaTriggerEvent.RISING_EDGE),
            *self._set_exposure_mode(ProsilicaExposureMode.MANUAL),
            prepare_exposures(self.driver, num, livetime),
        )

    async def default_trigger_info(self):
        """Get default trigger info based on the number of images to acquire."""
        return await trigger_info_from_driver(self.driver.num_images)


@dataclass
class ProsilicaTriggerLogic(ProsilicaLegacyTriggerLogic):
    """Trigger logic for Prosilica cameras.

    Parameters
    ----------
    driver : ProsilicaDriverIO
        The camera driver.
    trigger_input : ProsilicaTriggerMode, optional
        ``Sync In`` line used for external triggers, by default ``SYNC_IN_1``.
    """

    driver: ProsilicaDriverIO  # pyright: ignore[reportIncompatibleVariableOverride]

    def _set_exposure_mode(self, mode: ProsilicaExposureMode) -> list:
        return [self.driver.exposure_mode.set(mode)]

    async def prepare_level(self, num: int):
        """Prepare the camera to expose while the trigger input is high."""
        await asyncio.gather(
            self.driver.trigger_mode.set(self.trigger_input),
            self.driver.trigger_event.set(ProsilicaTriggerEvent.HIGH_LEVEL),
            *self._set_exposure_mode(ProsilicaExposureMode.EXTERNAL),
            prepare_exposures(self.driver, num),
        )


class ProsilicaDetectorMock(DeviceMock["ProsilicaDetector | ProsilicaLegacyDetector"]):
    """Mock behaviour that simulates Prosilica internally triggered acquisition."""

    async def connect(
        self, device: "ProsilicaDetector | ProsilicaLegacyDetector"
    ) -> None:
        """Mock signals to simulate Prosilica camera acquisition."""
        set_mock_value(device.driver.array_size_x, 2448)
        set_mock_value(device.driver.array_size_y, 2050)
        set_mock_value(device.driver.data_type, ADBaseDataType.UINT16)

        try:
            hdf = device.get_plugin("hdf", NDFileHDF5IO)
            set_mock_value(hdf.array_size0, 2050)
            set_mock_value(hdf.array_size1, 2448)
            set_mock_value(hdf.file_path_exists, True)
        except (AttributeError, TypeError):
            hdf = None

        set_mock_value(device.driver.acquire_time, 1.0)
        set_mock_value(device.driver.acquire_period, 1.0 + PROSILICA_READOUT_TIME)
        set_mock_value(device.driver.num_images, 1)
        set_mock_value(device.driver.num_images_counter, 0)
        set_mock_value(device.driver.trigger_mode, ProsilicaTriggerMode.FREE_RUN)

        async def _do_acquisition():
            trigger_mode = await device.driver.trigger_mode.get_value()
            if trigger_mode not in (
                ProsilicaTriggerMode.FREE_RUN,
                ProsilicaTriggerMode.FIXED_RATE,
            ):
                set_mock_put_proceeds(device.driver.acquire, True)
                return

            num_images = await device.driver.num_images.get_value()
            acquire_time = await device.driver.acquire_time.get_value()
            if trigger_mode == ProsilicaTriggerMode.FIXED_RATE:
                acquire_period = await device.driver.acquire_period.get_value()
            else:
                acquire_period = acquire_time + PROSILICA_READOUT_TIME

            for i in range(num_images):
                await asyncio.sleep(acquire_time)
                set_mock_value(device.driver.num_images_counter, i + 1)
                if hdf is not None:
                    num_captured = await hdf.num_captured.get_value()
                    set_mock_value(hdf.num_captured, num_captured + 1)
                if i < num_images - 1:
                    await asyncio.sleep(max(acquire_period - acquire_time, 0))

            set_mock_value(device.driver.acquire, False)
            set_mock_put_proceeds(device.driver.acquire, True)

        def _on_acquire_write(value: bool) -> None:
            if value:
                set_mock_put_proceeds(device.driver.acquire, False)
                asyncio.ensure_future(_do_acquisition())

        callback_on_mock_put(device.driver.acquire, _on_acquire_write)


@default_mock_class(ProsilicaDetectorMock)
class ProsilicaDetector(AreaDetector[ProsilicaDriverIO]):
    """Create a Prosilica AreaDetector instance.

    Parameters
    ----------
    prefix : str
        EPICS PV prefix for the camera
    writer_factories : ADWriterFactory
        Factories for file writer plugins and their data logics
    driver_suffix : str, optional
        Suffix for the driver PV, by default "cam1:"
    trigger_input : ProsilicaTriggerMode, optional
        ``Sync In`` line used for external triggers, by default ``SYNC_IN_1``
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
        trigger_input: ProsilicaTriggerMode = ProsilicaTriggerMode.SYNC_IN_1,
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = ProsilicaDriverIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            *writer_factories,
            acquire_logic=ADAcquireLogic(driver),
            trigger_logic=ProsilicaTriggerLogic(driver, trigger_input),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )


@default_mock_class(ProsilicaDetectorMock)
class ProsilicaLegacyDetector(AreaDetector[ProsilicaLegacyDriverIO]):
    """Create a Prosilica AreaDetector for older ADProsilica IOCs (e.g. 2.5.0).

    Use this when the IOC lacks the ``GainMode``, ``ExposureMode`` and
    ``TemperatureMainboard`` PVs. Internal and edge triggering are supported;
    level (pulse-width) triggering is not.

    Parameters are the same as for `ProsilicaDetector`.
    """

    def __init__(
        self,
        prefix: str,
        *writer_factories: ADWriterFactory,
        driver_suffix="cam1:",
        trigger_input: ProsilicaTriggerMode = ProsilicaTriggerMode.SYNC_IN_1,
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = ProsilicaLegacyDriverIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            *writer_factories,
            acquire_logic=ADAcquireLogic(driver),
            trigger_logic=ProsilicaLegacyTriggerLogic(driver, trigger_input),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )
