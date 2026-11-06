"""Acquisition plans for the Psylotech load frame at XPD."""

from collections.abc import Sequence

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from ophyd_async.epics.adcore import AreaDetector
from ophyd_async.fastcs.panda import HDFPanda

from ..psylotech import Psylotech
from .plans import single_axis_flyscan


def go_to_load(
    psylotech: Psylotech,
    target: float,
    tolerance: float | None = None,
    settle_time: float | None = None,
):
    """Move the load frame to a load and wait for the load to settle there.

    Parameters
    ----------
    psylotech : Psylotech
        The load frame, whose ``load`` is moved.
    target : float
        Load to move to. Tension is positive and compression negative.
    tolerance : float, optional
        Load the move is done within, by default what the load frame is set to.
    settle_time : float, optional
        Time in seconds the load must stay within tolerance, by default what the
        load frame is set to.

    Returns
    -------
    float
        The load once settled.
    """
    load = psylotech.load

    if tolerance is not None:
        yield from bps.mv(load.tolerance, tolerance)
    if settle_time is not None:
        yield from bps.mv(load.settle_time, settle_time)
    yield from bps.mv(load, target)
    return (yield from bps.rd(load.load))


def psylotech_flyscan(
    detectors: list[AreaDetector],
    panda: HDFPanda,
    psylo: Psylotech,
    num_images: int = 1801,
    start: float = 0.0,
    stop: float = 180.0,
    stream_name: str = "tomo",
    acq_time_overhead: float = 0.001,
    time_based: bool = False,
    md: dict | None = None,
):
    """Fly the Psylotech rotation axis, recording the load it was flown at.

    Parameters
    ----------
    detectors : list[AreaDetector]
        Detectors to trigger during the scan.
    panda : HDFPanda
        PandA triggering the detectors and capturing the encoders.
    psylo : Psylotech
        The load frame, whose rotation axis (``rot``) is flown. Its current load is
        added to the run's metadata as ``psylotech_load``.
    num_images, start, stop, stream_name, acq_time_overhead, time_based
        See :func:`xpd_tools.plans.single_axis_flyscan`.
    md : dict, optional
        Extra metadata for the run.
    """
    _md = dict(md or {})
    _md["psylotech_load"] = yield from bps.rd(psylo.load)
    return (
        yield from bpp.inject_md_wrapper(
            single_axis_flyscan(
                detectors=detectors,
                panda=panda,
                motor=psylo.rot,
                num_images=num_images,
                start=start,
                stop=stop,
                stream_name=stream_name,
                acq_time_overhead=acq_time_overhead,
                time_based=time_based,
            ),
            _md,
        )
    )


def load_series_flyscan(
    detectors: list[AreaDetector],
    panda: HDFPanda,
    rotation: Psylotech,
    load: Psylotech,
    loads: Sequence[float],
    num_images: int = 1801,
    start: float = 0.0,
    stop: float = 180.0,
    tolerance: float | None = None,
    settle_time: float | None = None,
    **flyscan_kwargs,
):
    """Fly the rotation axis at each of a series of loads.

    Parameters
    ----------
    detectors, panda
        See :func:`psylotech_flyscan`.
    rotation : Psylotech
        The load frame, whose rotation axis is flown at each load.
    load : Psylotech
        The load frame, whose load is moved. Normally the same object as
        ``rotation``.
    loads : Sequence[float]
        Loads to go to, in order. Tension is positive and compression negative.
    num_images, start, stop
        See :func:`xpd_tools.plans.single_axis_flyscan`.
    tolerance, settle_time
        See :func:`go_to_load`.
    **flyscan_kwargs
        Passed on to :func:`psylotech_flyscan`.
    """
    for target in loads:
        yield from go_to_load(load, target, tolerance, settle_time)
        yield from psylotech_flyscan(
            detectors,
            panda,
            rotation,
            num_images=num_images,
            start=start,
            stop=stop,
            **flyscan_kwargs,
        )
