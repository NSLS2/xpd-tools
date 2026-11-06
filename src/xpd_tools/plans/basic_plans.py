"""Basic acquisition helpers for XPD beamline at NSLS-II."""

from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.protocols import Readable
from ophyd_async.core import StandardDetector, TriggerInfo


def count(
    detectors: list[Readable],
    num: int = 1,
    exposure_time: float | None = None,
    delay: float = 0.0,
    sample_name: str | None = None,
    md: dict[str, Any] | None = None,
):
    """Take ``num`` readings of the detectors, optionally setting the exposure time.

    Like ``bluesky.plans.count``, with an ``exposure_time`` for the area detectors
    (Pilatus4, IRIS/Kinetix, Xspress3, ...) and a ``sample_name``. Each detector
    gets its own shortest acquire period for that exposure.

    Parameters
    ----------
    detectors : list[Readable]
        Detectors to read. Area detectors are set to ``exposure_time``; other
        readables are just read.
    num : int, optional
        Number of readings, by default 1.
    exposure_time : float | None, optional
        Exposure time per reading in seconds. ``None`` keeps each detector's
        current exposure time.
    delay : float, optional
        Time to wait between readings in seconds, by default 0.
    sample_name : str | None, optional
        Name of the sample, recorded in the run's start document.
    md : dict[str, Any] | None, optional
        Extra metadata for the run's start document.

    Examples
    --------
    >>> RE(count([pilatus1], num=5, exposure_time=0.1, sample_name="LaB6"))
    """
    area_detectors = [det for det in detectors if isinstance(det, StandardDetector)]

    _md: dict[str, Any] = {
        "plan_name": "count",
        "detectors": [det.name for det in detectors],
        "num_points": num,
        "exposure_time": exposure_time,
        "delay": delay,
        "hints": {"dimensions": [(("time",), "primary")]},
    }
    if sample_name is not None:
        _md["sample_name"] = sample_name
    _md.update(md or {})

    @bpp.stage_decorator(detectors)
    @bpp.run_decorator(md=_md)
    def _inner():
        # Preparing after staging is what sets the exposure: staging clears it.
        if exposure_time is not None:
            for det in area_detectors:
                (deadtime,) = yield from bps.wait_for([det.get_trigger_deadtime])
                trigger_info = TriggerInfo(
                    livetime=exposure_time, deadtime=deadtime.result()[1] or 0.0
                )
                yield from bps.prepare(det, trigger_info, group="prepare")
            yield from bps.wait(group="prepare")

        for i in range(num):
            if i and delay:
                yield from bps.sleep(delay)
            yield from bps.checkpoint()
            yield from bps.trigger_and_read(detectors)

    return (yield from _inner())
