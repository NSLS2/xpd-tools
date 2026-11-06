"""Photoluminescence quality gating during UV-Vis acquisition."""

import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

import bluesky.plan_stubs as bps
import numpy as np
from bluesky.callbacks import CallbackBase
from bluesky.utils import MsgGenerator
from event_model import Event, EventDescriptor
from ophyd_async.core import SignalR, soft_signal_r_and_setter

from xpd_tools.detectors import QEPro
from xpd_tools.light_path import UVVisLightPath
from xpd_tools.optimization.analysis import classify_pl
from xpd_tools.plans.qepro_plans import measure_fluorescence

logger = logging.getLogger(__name__)

#: Peak wavelength recorded when a spectrum has no PL peak at or above 400 nm.
NO_PEAK_WAVELENGTH = -1.0


class PLQualityMonitor(CallbackBase):
    """Classify the last fluorescence spectrum of each batch as good or bad PL.

    Parameters
    ----------
    qepro : QEPro
        Spectrometer whose ``x_axis`` and ``output`` readings carry the spectrum.
    key_height : float, optional
        Smallest height of the strongest peak at or above 400 nm for a good
        spectrum; default is 200.
    height : float, optional
        Smallest height counted as a peak; default is 50.
    distance : int, optional
        Smallest separation between peaks, in samples; default is 100.

    Attributes
    ----------
    good_count, bad_count : int
        Number of batches classified good and bad so far.
    batch_index : int
        Index of the batch currently being recorded.
    batch_results : list of dict
        Result of every finalized batch, as returned by `finalize_batch`.

    Notes
    -----
    Subscribe the monitor to the run's ``"descriptor"`` and ``"event"``
    documents, for example with ``bpp.subs_decorator``, so the RunEngine calls it
    synchronously and a batch's events are recorded before the plan resumes.
    Only events in the ``"fluorescence"`` stream count. Only the last spectrum of
    a batch is classified, with `classify_pl`. The default thresholds equal the
    screening defaults of `analyze_pl_spectra`, so the gate and the PL evaluation
    agree on which spectra are good. Use a fresh monitor per run, because its
    counts accumulate.
    """

    def __init__(
        self,
        qepro: QEPro,
        *,
        key_height: float = 200.0,
        height: float = 50.0,
        distance: int = 100,
    ) -> None:
        super().__init__()
        self.good_count = 0
        self.bad_count = 0
        self.batch_index = 0
        self.batch_results: list[dict[str, Any]] = []
        self._x_field = qepro.x_axis.name
        self._y_field = qepro.output.name
        self._descriptors: set[str] = set()
        self._latest_spectrum: tuple[np.ndarray, np.ndarray] | None = None
        self._batch_event_count = 0
        self._key_height = key_height
        self._height = height
        self._distance = distance

    def descriptor(self, doc: EventDescriptor) -> None:
        if doc.get("name") == "fluorescence":
            self._descriptors.add(doc["uid"])

    def event(self, doc: Event) -> Event:
        data = doc["data"]
        if (
            doc["descriptor"] in self._descriptors
            and self._x_field in data
            and self._y_field in data
        ):
            self._latest_spectrum = (
                np.asarray(data[self._x_field]),
                np.asarray(data[self._y_field]),
            )
            self._batch_event_count += 1
        return doc

    def finalize_batch(self) -> dict[str, Any] | None:
        """Classify the batch's last spectrum and start a new batch.

        Returns
        -------
        dict or None
            ``batch_index``, ``verdict`` (``"good"`` or ``"bad"``),
            ``peak_wavelength_nm``, ``n_good_total``, ``n_bad_total`` and
            ``n_events_in_batch``. ``peak_wavelength_nm`` is
            `NO_PEAK_WAVELENGTH` when the spectrum has no PL peak. None when the
            batch recorded no spectrum.
        """
        if self._latest_spectrum is None:
            return None
        is_good, peak_wavelength = classify_pl(
            *self._latest_spectrum,
            key_height=self._key_height,
            height=self._height,
            distance=self._distance,
        )
        if is_good:
            self.good_count += 1
        else:
            self.bad_count += 1
        result = {
            "batch_index": self.batch_index,
            "verdict": "good" if is_good else "bad",
            "peak_wavelength_nm": (
                NO_PEAK_WAVELENGTH if math.isnan(peak_wavelength) else peak_wavelength
            ),
            "n_good_total": self.good_count,
            "n_bad_total": self.bad_count,
            "n_events_in_batch": self._batch_event_count,
        }
        self.batch_results.append(result)
        self.batch_index += 1
        self._latest_spectrum = None
        self._batch_event_count = 0
        return result


def _record_batch_quality(
    monitor: PLQualityMonitor,
    quality: Mapping[str, tuple[SignalR[Any], Callable[[Any], None]]],
) -> MsgGenerator[None]:
    result = monitor.finalize_batch()
    if result is None:
        raise RuntimeError(
            "Fluorescence batch recorded no spectrum; cannot classify PL quality"
        )
    for key, (_, set_value) in quality.items():
        set_value(result[key])
    yield from bps.create(name="fluorescence_quality")
    for signal, _ in quality.values():
        yield from bps.read(signal)
    yield from bps.save()


def fluorescence_quality_gate(
    qepro: QEPro,
    light_path: UVVisLightPath,
    monitor: PLQualityMonitor | None,
    num_spectra: int,
    *,
    good_target: int,
    max_bad: int,
) -> MsgGenerator[None]:
    """Repeat fluorescence batches until enough are good or too many are bad.

    Parameters
    ----------
    qepro : QEPro
        Spectrometer to read.
    light_path : UVVisLightPath
        LED and absorbance shutter of the UV-Vis setup.
    monitor : PLQualityMonitor or None
        Classifier subscribed to the run; None runs one ungated batch.
    num_spectra : int
        Spectra per batch, passed to `measure_fluorescence`.
    good_target : int
        Stop once this many batches are good.
    max_bad : int
        Stop once this many batches are bad; the caller proceeds anyway.

    Raises
    ------
    RuntimeError
        If a gated batch recorded no spectrum.

    Notes
    -----
    Call inside an open run, with ``monitor`` subscribed to that run's
    descriptor and event documents. The first batch always runs. With a monitor,
    each batch adds one ``"fluorescence_quality"`` event with the six fields
    returned by `PLQualityMonitor.finalize_batch`. Call at most once per run,
    with a fresh monitor.
    """
    yield from measure_fluorescence(qepro, light_path, num_spectra)
    if monitor is None:
        return
    quality = {
        "batch_index": soft_signal_r_and_setter(int, 0, name="batch_index"),
        "verdict": soft_signal_r_and_setter(str, "bad", name="verdict"),
        "peak_wavelength_nm": soft_signal_r_and_setter(
            float, NO_PEAK_WAVELENGTH, name="peak_wavelength_nm"
        ),
        "n_good_total": soft_signal_r_and_setter(int, 0, name="n_good_total"),
        "n_bad_total": soft_signal_r_and_setter(int, 0, name="n_bad_total"),
        "n_events_in_batch": soft_signal_r_and_setter(int, 0, name="n_events_in_batch"),
    }
    yield from _record_batch_quality(monitor, quality)
    while monitor.good_count < good_target and monitor.bad_count < max_bad:
        yield from measure_fluorescence(qepro, light_path, num_spectra)
        yield from _record_batch_quality(monitor, quality)
    if monitor.good_count >= good_target:
        logger.info("%d good PL batches, proceeding", monitor.good_count)
    else:
        logger.warning("%d bad PL batches, proceeding anyway", monitor.bad_count)
