"""Utility functions for xpd_tools detectors."""

from collections.abc import Generator
from typing import Any

import bluesky.plan_stubs as bps
from bluesky.utils import Msg
from ophyd_async.epics.adcore import AreaDetector


def get_detector_acq_times(
    detectors: list[AreaDetector],
) -> Generator[Msg, Any, list[float]]:
    """
    Get the acquisition times and periods for a list of detectors.

    Args:
        - detectors: list[AreaDetector] - List of area detectors to query.

    Returns
    -------
        - list[float] - Acquisition period for each detector. If the acquisition
          period is less than the acquisition time, the acquisition time is used.
    """
    acquisition_periods: list[float] = []
    for det in detectors:
        acq_time: float = yield from bps.rd(det.driver.acquire_time)
        acq_period: float = yield from bps.rd(det.driver.acquire_period)
        if acq_period < acq_time:
            acq_period = acq_time
        acquisition_periods.append(acq_period)
    return acquisition_periods
