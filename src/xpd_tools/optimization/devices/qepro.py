"""Regular-ophyd QEPro spectrometer device class for 28-ID-2.

Ported from xpd-profile-collection startup/25-QEPro.py, retaining only the
device definition.  The tiled/CSV export helpers and interactive plotting
methods from the original startup script are omitted; all data export is
handled by the Bluesky RunEngine + TiledWriter stack.

The signals exposed here match what ``plans/runtime.py`` and the evaluator
(``helpers/qepro.py``) expect:

    qepro.correction     -- SignalWithRBV: 'Dark' | 'Reference' | 'None'
    qepro.spectrum_type  -- SignalWithRBV: 'Corrected Sample' | ...
    qepro.x_axis         -- EpicsSignal: wavelength array
    qepro.output         -- EpicsSignal: processed spectrum array

Bluesky read names (the keys that appear in events) are:
    QEPro_x_axis, QEPro_output   (device name "QEPro" + signal name)
"""

from __future__ import annotations

from ophyd import Component as Cpt
from ophyd import Device, EpicsSignal, EpicsSignalRO
from ophyd.areadetector import EpicsSignalWithRBV as SignalWithRBV
from ophyd.status import DeviceStatus, SubscriptionStatus


class QEProTEC(Device):
    """Thermal electric cooler sub-device of the QEPro."""

    tec = Cpt(SignalWithRBV, "TEC")
    tec_temp = Cpt(SignalWithRBV, "TEC_TEMP", kind="config")
    curr_tec_temp = Cpt(EpicsSignalRO, "CURR_TEC_TEMP_RBV")

    def __init__(self, *args, tolerance: float = 1.0, **kwargs):
        self.tolerance = tolerance
        super().__init__(*args, **kwargs)

    def set(self, value: float) -> DeviceStatus:
        """Set TEC temperature setpoint; complete when within tolerance."""

        def check_setpoint(value, old_value, **kwargs):
            return abs(value - self.tec_temp.get()) < self.tolerance

        self.tec_temp.put(value)
        return SubscriptionStatus(self.curr_tec_temp, check_setpoint)


class QEPro(Device):
    """Ocean Optics QEPro spectrometer.

    Named ``QEPro`` so that Bluesky event field names are
    ``QEPro_x_axis`` and ``QEPro_output``, matching the evaluator.
    """

    serial = Cpt(EpicsSignal, "SERIAL")
    model = Cpt(EpicsSignal, "MODEL")
    status = Cpt(EpicsSignal, "STATUS")
    status_msg = Cpt(EpicsSignal, "STATUS_MSG", string=True)
    device_connected = Cpt(EpicsSignalRO, "CONNECTED_RBV")
    features = Cpt(EpicsSignalRO, "FEATURES_RBV")

    strobe = Cpt(SignalWithRBV, "STROBE")
    electric_dark_correction = Cpt(SignalWithRBV, "EDC", kind="normal")
    non_linearity_correction = Cpt(SignalWithRBV, "NLC", kind="normal")
    shutter = Cpt(SignalWithRBV, "SHUTTER")

    tec_device = Cpt(QEProTEC, "")

    light_source = Cpt(SignalWithRBV, "LIGHT_SOURCE")
    light_source_intensity = Cpt(SignalWithRBV, "LIGHT_SOURCE_INTENSITY")
    light_source_count = Cpt(EpicsSignalRO, "LIGHT_SOURCE_COUNT_RBV")

    num_spectra = Cpt(SignalWithRBV, "NUM_SPECTRA", kind="normal")
    spectra_collected = Cpt(EpicsSignalRO, "SPECTRA_COLLECTED_RBV")

    int_min_time = Cpt(EpicsSignalRO, "INT_MIN_TIME_RBV")
    int_max_time = Cpt(EpicsSignalRO, "INT_MAX_TIME_RBV")
    integration_time = Cpt(SignalWithRBV, "INTEGRATION_TIME", kind="normal")

    buff_min_capacity = Cpt(EpicsSignalRO, "BUFF_MIN_CAPACITY_RBV")
    buff_max_capacity = Cpt(EpicsSignalRO, "BUFF_MAX_CAPACITY_RBV")
    buff_capacity = Cpt(SignalWithRBV, "BUFF_CAPACITY")
    buff_element_count = Cpt(EpicsSignalRO, "BUFF_ELEMENT_COUNT_RBV")

    # Spectra signals -- these are what plans/runtime.py reads
    output = Cpt(EpicsSignal, "OUTPUT", kind="normal")
    sample = Cpt(EpicsSignal, "SAMPLE", kind="normal")
    dark = Cpt(EpicsSignal, "DARK", kind="normal")
    reference = Cpt(EpicsSignal, "REFERENCE", kind="normal")

    formatted_spectrum_len = Cpt(EpicsSignalRO, "FORMATTED_SPECTRUM_LEN_RBV")
    x_axis = Cpt(EpicsSignal, "X_AXIS", kind="normal")
    x_axis_format = Cpt(SignalWithRBV, "X_AXIS_FORMAT")

    dark_available = Cpt(EpicsSignalRO, "DARK_AVAILABLE_RBV")
    ref_available = Cpt(EpicsSignalRO, "REF_AVAILABLE_RBV")

    acquire = Cpt(SignalWithRBV, "COLLECT")
    collect_mode = Cpt(SignalWithRBV, "COLLECT_MODE", kind="normal")
    spectrum_type = Cpt(SignalWithRBV, "SPECTRUM_TYPE", kind="normal")
    correction = Cpt(SignalWithRBV, "CORRECTION", kind="normal")
    trigger_mode = Cpt(SignalWithRBV, "TRIGGER_MODE")

    def trigger(self) -> DeviceStatus:
        """Trigger a single acquisition and return a status object."""

        def acquisition_done(value, old_value, **kwargs):
            # COLLECT:RBV goes 1 → 0 when acquisition completes
            return int(old_value) == 1 and int(value) == 0

        self.acquire.set(1)
        return SubscriptionStatus(self.acquire._readback, acquisition_done)
