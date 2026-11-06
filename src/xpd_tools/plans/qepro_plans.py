"""Hardware-independent Bluesky plans for QEPro and the UV-Vis light path."""

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp

from xpd_tools.detectors import QEPro
from xpd_tools.light_path import UVVisLightPath


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


def _set_optics(
    qepro: QEPro,
    light_path: UVVisLightPath,
    *,
    correction: str,
    spectrum_type: str,
    led: str,
    uv_shutter: str,
    settle_time: float,
):
    current = (
        (yield from bps.rd(qepro.correction)),
        (yield from bps.rd(qepro.spectrum_type)),
        (yield from bps.rd(light_path.led)),
        (yield from bps.rd(light_path.uv_shutter)),
    )
    if current == (correction, spectrum_type, led, uv_shutter):
        return
    yield from bps.mv(qepro.correction, correction, qepro.spectrum_type, spectrum_type)
    yield from bps.mv(light_path.led, led, light_path.uv_shutter, uv_shutter)
    yield from bps.sleep(settle_time)


def measure_absorbance(
    qepro: QEPro,
    light_path: UVVisLightPath,
    num_spectra: int,
    *,
    stream_name: str = "absorbance",
    settle_time: float = 2.0,
):
    """Switch to absorbance optics and read ``num_spectra`` QEPro spectra.

    Parameters
    ----------
    qepro : QEPro
        Spectrometer to read.
    light_path : UVVisLightPath
        LED and absorbance shutter of the UV-Vis setup.
    num_spectra : int
        Number of spectra; each is one ``trigger_and_read`` event.
    stream_name : str, optional
        Event stream name; default is ``"absorbance"``.
    settle_time : float, optional
        Seconds to wait after switching optics; default is 2.0.

    Notes
    -----
    The caller opens the run and stages the QEPro. Absorbance uses the
    ``"Reference"`` correction with the LED off and the shutter open;
    ``"Absorbtion"`` is the IOC's own spelling of the spectrum type. The plan
    skips the writes and the settle when the optics are already in this state.
    """
    yield from _set_optics(
        qepro,
        light_path,
        correction="Reference",
        spectrum_type="Absorbtion",
        led="Low",
        uv_shutter="High",
        settle_time=settle_time,
    )
    for _ in range(num_spectra):
        yield from bps.trigger_and_read([qepro], name=stream_name)


def measure_fluorescence(
    qepro: QEPro,
    light_path: UVVisLightPath,
    num_spectra: int,
    *,
    stream_name: str = "fluorescence",
    settle_time: float = 2.0,
):
    """Switch to fluorescence optics and read ``num_spectra`` QEPro spectra.

    Parameters
    ----------
    qepro : QEPro
        Spectrometer to read.
    light_path : UVVisLightPath
        LED and absorbance shutter of the UV-Vis setup.
    num_spectra : int
        Number of spectra; each is one ``trigger_and_read`` event.
    stream_name : str, optional
        Event stream name; default is ``"fluorescence"``.
    settle_time : float, optional
        Seconds to wait after switching optics; default is 2.0.

    Notes
    -----
    The caller opens the run and stages the QEPro. Fluorescence uses the
    ``"Dark"`` correction with the LED on and the shutter closed. The plan skips
    the writes and the settle when the optics are already in this state, which
    keeps repeated fluorescence batches cheap.
    """
    yield from _set_optics(
        qepro,
        light_path,
        correction="Dark",
        spectrum_type="Corrected Sample",
        led="High",
        uv_shutter="Low",
        settle_time=settle_time,
    )
    for _ in range(num_spectra):
        yield from bps.trigger_and_read([qepro], name=stream_name)
