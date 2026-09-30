"""Ophyd-async interface for QEPro spectrometers."""

import asyncio
from typing import Annotated as A

import numpy as np
from ophyd_async.core import (
    AsyncStatus,
    DeviceConnector,
    DeviceMock,
    SignalR,
    SignalRW,
    StandardReadable,
    callback_on_mock_put,
    default_mock_class,
    observe_value,
    set_mock_value,
)
from ophyd_async.core import StandardReadableFormat as Format
from ophyd_async.epics.core import EpicsDevice, PvSuffix


class QEProTEC(StandardReadable, EpicsDevice):
    """Thermoelectric cooler settings for a QEPro spectrometer."""

    tec: A[SignalRW[bool], PvSuffix.rbv("TEC")]
    tec_temp: A[SignalRW[float], PvSuffix.rbv("TEC_TEMP"), Format.CONFIG_SIGNAL]
    curr_tec_temp: A[SignalR[float], PvSuffix("CURR_TEC_TEMP_RBV")]

    def __init__(
        self,
        prefix: str = "",
        *,
        tolerance: float = 1.0,
        name: str = "",
        connector: DeviceConnector | None = None,
    ) -> None:
        self.tolerance = tolerance
        super().__init__(prefix, name=name, connector=connector)

    @AsyncStatus.wrap
    async def set(self, value: float) -> None:
        """Set the cooler temperature and wait for an in-tolerance update."""
        updates = observe_value(self.curr_tec_temp)
        try:
            await anext(updates)
            await self.tec_temp.set(value)
            await self.tec.set(True)
            async for current in updates:
                if abs(current - value) < self.tolerance:
                    return
        finally:
            await updates.aclose()


class QEProMock(DeviceMock["QEPro"]):
    """Deterministic QEPro behaviour for mock-mode tests and startup."""

    async def connect(self, device: "QEPro") -> None:
        """Initialize predictable spectra and complete mocked acquisitions."""
        x_axis = np.linspace(200.0, 1000.0, 8)
        sample = np.arange(8, dtype=float)

        set_mock_value(device.serial, "SIM-QEPRO")
        set_mock_value(device.model, "QEPro")
        set_mock_value(device.status, 0)
        set_mock_value(device.status_msg, "Idle")
        set_mock_value(device.device_connected, True)
        set_mock_value(device.check_status, False)
        set_mock_value(device.features, 63)
        set_mock_value(device.strobe, False)
        set_mock_value(device.electric_dark_correction, False)
        set_mock_value(device.non_linearity_correction, False)
        set_mock_value(device.shutter, False)
        set_mock_value(device.tec_device.tec, False)
        set_mock_value(device.tec_device.tec_temp, 20.0)
        set_mock_value(device.tec_device.curr_tec_temp, 20.0)
        set_mock_value(device.light_source, False)
        set_mock_value(device.light_source_intensity, 0.0)
        set_mock_value(device.light_source_count, 0)
        set_mock_value(device.num_spectra, 1)
        set_mock_value(device.spectra_collected, 0)
        set_mock_value(device.int_min_time, 1.0)
        set_mock_value(device.int_max_time, 10_000.0)
        set_mock_value(device.integration_time, 100.0)
        set_mock_value(device.buff_min_capacity, 1)
        set_mock_value(device.buff_max_capacity, 3)
        set_mock_value(device.buff_capacity, 3)
        set_mock_value(device.buff_element_count, 0)
        set_mock_value(device.output, sample)
        set_mock_value(device.sample, sample)
        set_mock_value(device.dark, np.zeros_like(sample))
        set_mock_value(device.reference, np.full_like(sample, 100.0))
        set_mock_value(device.formatted_spectrum_len, len(sample))
        set_mock_value(device.x_axis, x_axis)
        set_mock_value(device.x_axis_format, "Wavelength")
        set_mock_value(device.dark_available, True)
        set_mock_value(device.ref_available, True)
        set_mock_value(device.acquire, False)
        set_mock_value(device.collect_mode, "Average")
        set_mock_value(device.spectrum_type, "Absorbtion")
        set_mock_value(device.correction, "Reference")
        set_mock_value(device.trigger_mode, "Normal")

        async def _on_tec_enabled(enabled: bool) -> None:
            if enabled:
                set_mock_value(
                    device.tec_device.curr_tec_temp,
                    await device.tec_device.tec_temp.get_value(),
                )

        def _on_acquire_write(collect: bool) -> None:
            if collect:
                asyncio.get_running_loop().call_soon(
                    set_mock_value, device.acquire, False
                )

        callback_on_mock_put(device.tec_device.tec, _on_tec_enabled)
        callback_on_mock_put(device.acquire, _on_acquire_write)


@default_mock_class(QEProMock)
class QEPro(StandardReadable, EpicsDevice):
    """Ophyd-async device interface for a QEPro spectrometer."""

    # Device information
    serial: A[SignalRW[str], PvSuffix("SERIAL")]
    model: A[SignalRW[str], PvSuffix("MODEL")]

    # Device status
    status: A[SignalRW[int], PvSuffix("STATUS")]
    status_msg: A[SignalRW[str], PvSuffix("STATUS_MSG")]
    device_connected: A[SignalR[bool], PvSuffix("CONNECTED_RBV")]
    check_status: A[SignalRW[bool], PvSuffix("CHECK_STATUS")]
    features: A[SignalR[int], PvSuffix("FEATURES_RBV")]

    # Togglable features
    strobe: A[SignalRW[bool], PvSuffix.rbv("STROBE")]
    electric_dark_correction: A[SignalRW[bool], PvSuffix.rbv("EDC")]
    non_linearity_correction: A[SignalRW[bool], PvSuffix.rbv("NLC")]
    shutter: A[SignalRW[bool], PvSuffix.rbv("SHUTTER")]

    # Thermal electric cooler
    tec_device: A[QEProTEC, PvSuffix(""), Format.CHILD]

    # Light source feature signals
    light_source: A[SignalRW[bool], PvSuffix.rbv("LIGHT_SOURCE")]
    light_source_intensity: A[SignalRW[float], PvSuffix.rbv("LIGHT_SOURCE_INTENSITY")]
    light_source_count: A[SignalR[int], PvSuffix("LIGHT_SOURCE_COUNT_RBV")]

    # Spectra averaging settings and current-scan count
    num_spectra: A[SignalRW[int], PvSuffix.rbv("NUM_SPECTRA"), Format.UNCACHED_SIGNAL]
    spectra_collected: A[SignalR[int], PvSuffix("SPECTRA_COLLECTED_RBV")]

    # Integration time settings in milliseconds
    int_min_time: A[SignalR[float], PvSuffix("INT_MIN_TIME_RBV")]
    int_max_time: A[SignalR[float], PvSuffix("INT_MAX_TIME_RBV")]
    integration_time: A[
        SignalRW[float], PvSuffix.rbv("INTEGRATION_TIME"), Format.UNCACHED_SIGNAL
    ]

    # Internal buffer settings
    buff_min_capacity: A[SignalR[int], PvSuffix("BUFF_MIN_CAPACITY_RBV")]
    buff_max_capacity: A[SignalR[int], PvSuffix("BUFF_MAX_CAPACITY_RBV")]
    buff_capacity: A[
        SignalRW[int], PvSuffix.rbv("BUFF_CAPACITY"), Format.UNCACHED_SIGNAL
    ]
    buff_element_count: A[SignalR[int], PvSuffix("BUFF_ELEMENT_COUNT_RBV")]

    # Formatted spectra
    output: A[SignalRW[np.ndarray], PvSuffix("OUTPUT"), Format.UNCACHED_SIGNAL]
    sample: A[SignalRW[np.ndarray], PvSuffix("SAMPLE"), Format.UNCACHED_SIGNAL]
    dark: A[SignalRW[np.ndarray], PvSuffix("DARK"), Format.UNCACHED_SIGNAL]
    reference: A[SignalRW[np.ndarray], PvSuffix("REFERENCE"), Format.UNCACHED_SIGNAL]
    formatted_spectrum_len: A[SignalR[int], PvSuffix("FORMATTED_SPECTRUM_LEN_RBV")]

    # X-axis settings
    x_axis: A[SignalRW[np.ndarray], PvSuffix("X_AXIS"), Format.UNCACHED_SIGNAL]
    x_axis_format: A[SignalRW[str], PvSuffix.rbv("X_AXIS_FORMAT")]

    # Dark/reference availability
    dark_available: A[SignalR[bool], PvSuffix("DARK_AVAILABLE_RBV")]
    ref_available: A[SignalR[bool], PvSuffix("REF_AVAILABLE_RBV")]

    # Collection settings and trigger
    acquire: A[SignalRW[bool], PvSuffix.rbv("COLLECT")]
    collect_mode: A[SignalRW[str], PvSuffix.rbv("COLLECT_MODE")]
    spectrum_type: A[
        SignalRW[str], PvSuffix.rbv("SPECTRUM_TYPE"), Format.UNCACHED_SIGNAL
    ]
    correction: A[SignalRW[str], PvSuffix.rbv("CORRECTION")]
    trigger_mode: A[SignalRW[str], PvSuffix.rbv("TRIGGER_MODE")]

    def __init__(self, prefix: str = "", *, name: str = "") -> None:
        super().__init__(prefix, name=name)
        self.set_name(name, child_name_separator="_")

    async def has_nlc_feature(self) -> int:
        """Return the non-linearity correction feature bit."""
        return await self.features.get_value() & 32

    async def has_lightsource_feature(self) -> int:
        """Return the light source feature bit."""
        return await self.features.get_value() & 16

    async def has_edc_feature(self) -> int:
        """Return the electric dark correction feature bit."""
        return await self.features.get_value() & 8

    async def has_buffer_feature(self) -> int:
        """Return the internal buffer feature bit."""
        return await self.features.get_value() & 4

    async def has_tec_feature(self) -> int:
        """Return the thermoelectric cooler feature bit."""
        return await self.features.get_value() & 2

    async def has_irrad_feature(self) -> int:
        """Return the irradiance calibration feature bit."""
        return await self.features.get_value() & 1

    @AsyncStatus.wrap
    async def trigger(self) -> None:
        """Collect one spectrum and wait for its acquire cycle to finish."""
        updates = observe_value(self.acquire)
        try:
            await anext(updates)
            await self.acquire.set(True)
            collecting = False
            async for acquired in updates:
                if acquired:
                    collecting = True
                elif collecting:
                    return
        finally:
            await updates.aclose()
