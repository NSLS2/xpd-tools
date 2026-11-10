"""Utility functions for xpd_tools detectors."""

from collections.abc import Generator, Sequence
from typing import Any

import bluesky.plan_stubs as bps
from bluesky.utils import Msg
from ophyd_async.epics.adcore import (
    AreaDetector,
    NDPluginBaseIO,
    NDProcessFilterType,
    NDProcessIO,
)


def get_detector_acq_times(
    detectors: list[AreaDetector],
) -> Generator[Msg, Any, list[float]]:
    """Get the acquisition times and periods for a list of detectors.

    Parameters
    ----------
    detectors : list[AreaDetector]
        List of area detectors to query.

    Returns
    -------
    list[float]
        List of tuples containing the acquisition time and period
        for each detector. If the acquisition period is less than
        the acquisition time, it will be set to the acquisition time.
    """
    acquisition_periods: list[float] = []
    for det in detectors:
        acq_time: float = yield from bps.rd(det.driver.acquire_time)
        acq_period: float = yield from bps.rd(det.driver.acquire_period)
        if acq_period < acq_time:
            acq_period = acq_time
        acquisition_periods.append(acq_period)
    return acquisition_periods


def set_proc_filter_type(
    dets: Sequence[AreaDetector], filter_type: NDProcessFilterType | str
) -> Generator[Msg, Any, None]:
    """Set the Proc plugin's filter type on every detector that has one.

    ophyd-async's prepare leaves the filter type to the IOC, so without this an
    averaged acquisition uses whatever the Proc plugin was last left on.

    Parameters
    ----------
    dets : Sequence[AreaDetector]
        The detectors; those without a ``proc`` plugin are skipped.
    filter_type : NDProcessFilterType | str
        The filter type, or its EPICS name (e.g. ``"Average"``).
    """
    filter_type = NDProcessFilterType(filter_type)
    sigs: list[Any] = []
    for det in dets:
        proc = getattr(det, "proc", None)
        if isinstance(proc, NDProcessIO):
            sigs.extend([proc.filter_type, filter_type])
    if sigs:
        yield from bps.mv(*sigs)


def set_plugin_queue_sizes(
    dets: AreaDetector | Sequence[AreaDetector], queue_size: int
) -> Generator[Msg, Any, None]:
    """Set the queue size of every AreaDetector plugin to a given value.

    A flyscan sets this to the number of exposures, so the plugins can buffer the
    whole scan without dropping frames.

    Parameters
    ----------
    dets : AreaDetector | Sequence[AreaDetector]
        The detector or list of detectors whose plugin queue sizes are to be set.
    queue_size : int
        The desired queue size for all plugins.
    """
    if isinstance(dets, AreaDetector):
        dets = [dets]
    sigs: list[Any] = []
    for det in dets:
        for _, child in det.children():
            if isinstance(child, NDPluginBaseIO):
                sigs.extend([child.queue_size, queue_size])
    if sigs:
        yield from bps.mv(*sigs)
