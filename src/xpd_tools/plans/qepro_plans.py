"""Hardware-independent Bluesky plans for QEPro spectrometers."""

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp

from xpd_tools.detectors import QEPro


def configure_qepro(
    qepro: QEPro,
    *,
    integration_time: float = 100,
    num_spectra_to_average: int = 10,
    buffer: int = 3,
    spectrum_type: str = "Absorbtion",
    correction_type: str = "Reference",
    electric_dark_correction: bool = True,
):
    """Apply the standard QEPro collection configuration."""
    yield from bps.abs_set(qepro.integration_time, integration_time, wait=True)
    yield from bps.abs_set(qepro.num_spectra, num_spectra_to_average, wait=True)
    yield from bps.abs_set(qepro.buff_capacity, buffer, wait=True)
    yield from bps.abs_set(
        qepro.collect_mode,
        "Average" if num_spectra_to_average > 1 else "Single",
        wait=True,
    )
    if electric_dark_correction:
        yield from bps.abs_set(qepro.electric_dark_correction, True, wait=True)
    yield from bps.abs_set(qepro.correction, correction_type, wait=True)
    yield from bps.abs_set(qepro.spectrum_type, spectrum_type, wait=True)


def _capture_qepro_frame(qepro: QEPro, spectrum_type: str, settle_time: float):
    yield from bps.abs_set(qepro.spectrum_type, spectrum_type, wait=True)
    yield from bps.trigger(qepro, wait=True)
    yield from bps.sleep(settle_time)


def _take_qepro_frame(qepro: QEPro, spectrum_type: str, settle_time: float):
    previous_spectrum_type = yield from bps.rd(qepro.spectrum_type)
    yield from bpp.finalize_wrapper(
        _capture_qepro_frame(qepro, spectrum_type, settle_time),
        bps.abs_set(qepro.spectrum_type, previous_spectrum_type, wait=True),
    )


def take_qepro_dark_frame(qepro: QEPro, *, settle_time: float = 1.0):
    """Capture a dark frame and restore the prior spectrum type."""
    yield from _take_qepro_frame(qepro, "Dark", settle_time)


def take_qepro_reference_frame(qepro: QEPro, *, settle_time: float = 1.0):
    """Capture a reference frame and restore the prior spectrum type."""
    yield from _take_qepro_frame(qepro, "Reference", settle_time)


def set_qepro_temperature(qepro: QEPro, temperature: float):
    """Set QEPro's thermoelectric cooler and wait for convergence."""
    yield from bps.abs_set(qepro.tec_device, temperature, wait=True)
