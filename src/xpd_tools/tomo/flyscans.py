"""Tomography plans for the XPDD TOMO endstation.

Ported from hextools ``tomography/flyscans.py`` and adapted to XPDD: the Pilatus4,
Kinetix (``iris1``) and Xspress3 detectors, the per-axis PandA flyer in
:mod:`xpd_tools.flyers`, and a configurable sample-out motor for flats (the
xpdDacq ``run_XCT`` workflow).

Supporting devices (PandA, rotation motor, sample-out motor, shutters) default to
None and are then looked up in the IPython namespace under the names the XPDD
profile gives them, see :data:`DEFAULT_DEVICE_NAMES`.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from bluesky import plan_patterns
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.protocols import Collectable
from bluesky.utils import (
    FailedStatus,
    MsgGenerator,
    PlanHalt,
    RunEngineControlException,
)
from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.core import DetectorTrigger, TriggerInfo
from ophyd_async.epics.adcore import NDProcessFilterType
from ophyd_async.epics.motor import Motor as AsyncEpicsMotor
from ophyd_async.fastcs.panda import HDFPanda

from xpd_tools.detectors.utils import set_plugin_queue_sizes, set_proc_filter_type
from xpd_tools.flyers import SingleAxisFlyableLogic
from xpd_tools.motors import RotationMotor
from xpd_tools.plans.plans import compute_flyscan_models
from xpd_tools.shutter import Shutter, ensure_shutter_closed, ensure_shutter_open
from xpd_tools.specs import TOMO_DARK_FLAT_V1, TOMO_FLYSCAN_V1, spec_md
from xpd_tools.tomo.utils import (
    ExposureTime,
    ImagingDetector,
    TomoDetector,
    exposure_md,
    resolve_exposure_times,
)
from xpd_tools.utils import ensure_available

logger = logging.getLogger(__name__)

#: Most frames any detector may drop before a flyscan is treated as failed.
MAX_MISSING_FRAMES = 10

#: Time added to the detectors' deadtime when ``acquire_period`` is not given.
FRAME_PERIOD_MARGIN = 0.001

#: Name of the PandA dataset holding the captured rotation angle.
ROTATION_DATASET_NAME = "rotation_angle"

#: IPython namespace names that supporting devices are looked up under.
DEFAULT_DEVICE_NAMES = {
    "panda": "panda1",
    "rot_motor": "amPhi",
    "sample_out_motor": "amTopX",
    "fe_shutter": "fe_shutter",
    "photon_shutter": "photon_shutter",
}

#: A motor that can be stepped between tomograms.
Steppable = AsyncEpicsMotor


def _shutters(
    use_shutter: bool, fe_shutter: Shutter | None, photon_shutter: Shutter | None
) -> tuple[Shutter | None, Shutter | None]:
    """Resolve the shutters, or return None for both if shutters are not used."""
    if not use_shutter:
        return None, None
    return (
        ensure_available(Shutter, fe_shutter, DEFAULT_DEVICE_NAMES["fe_shutter"]),
        ensure_available(
            Shutter, photon_shutter, DEFAULT_DEVICE_NAMES["photon_shutter"]
        ),
    )


def tomo_dark_flat(
    detectors: list[ImagingDetector],
    exposure_time: ExposureTime,
    num_dark_images: int = 20,
    num_flat_images: int = 50,
    sample_out_offset: float = 2.0,
    sample_out_velocity: float | None = None,
    use_shutter: bool = True,
    sample_out_motor: AsyncEpicsMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
    md: dict[str, Any] | None = None,
) -> MsgGenerator[str]:
    """Take dark and flat images for tomography, in one run.

    The ``dark`` stream is taken with the photon shutter closed. For the ``flat``
    stream the sample is moved out of the beam by ``sample_out_offset`` and the
    photon shutter is opened. Afterwards the photon shutter is closed, then the
    sample is moved back and its motor's velocity and the detectors' exposure times
    are restored, even if the plan fails. The shutter always closes before the
    sample moves back into the beam.

    Take darks and flats with the same exposure time as the projections they
    correct. Later runs link to this one through ``last_dark_flat_uid``, see
    :class:`xpd_tools.tomo.utils.LastDarkFlatUIDTracker`.

    Parameters
    ----------
    detectors : list[ImagingDetector]
        The imaging detectors (Pilatus4 or Kinetix) to take darks and flats with.
    exposure_time : ExposureTime
        The exposure time of each image, in seconds: one number for every detector,
        or a mapping that gives every detector its own, keyed by detector or by its
        ophyd ``.name``, e.g. ``{pilatus1: 0.002, "iris1": 0.01}``.
    num_dark_images : int, default 20
        The number of dark images to acquire.
    num_flat_images : int, default 50
        The number of flat images to acquire.
    sample_out_offset : float, default 2.0
        The relative move of ``sample_out_motor`` that takes the sample out of the
        beam for the flats. Must not be zero.
    sample_out_velocity : float | None, default None
        The velocity to move the sample out and back at; None keeps the motor's
        current velocity. The original velocity is restored afterwards.
    use_shutter : bool, default True
        Whether to use the shutters. If False the plan never touches the shutters,
        so the darks are only as dark as the beam is.
    sample_out_motor : AsyncEpicsMotor | None, default None
        The motor that moves the sample out of the beam; None uses ``amTopX``.
    fe_shutter : Shutter | None, default None
        The front-end shutter, which must already be open; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter the plan opens and closes; None uses ``photon_shutter``.
    md : dict[str, Any] | None, default None
        Extra metadata for the run's start document.

    Returns
    -------
    str
        The uid of the run.

    Raises
    ------
    ValueError
        If a detector is not an imaging detector, ``sample_out_offset`` is zero, or
        ``exposure_time`` is invalid,
        see :func:`xpd_tools.tomo.utils.resolve_exposure_times`.
    """
    for det in detectors:
        if isinstance(det, Xspress3Detector):
            raise ValueError(
                f"{det.name} is an Xspress3 fluorescence detector, which takes no "
                "darks or flats. Pass only imaging detectors."
            )
    if sample_out_offset == 0:
        raise ValueError("sample_out_offset must be non-zero to move the sample out.")
    exposure_times = resolve_exposure_times(detectors, exposure_time)

    sample_out_motor = ensure_available(
        AsyncEpicsMotor, sample_out_motor, DEFAULT_DEVICE_NAMES["sample_out_motor"]
    )
    fe_shutter, photon_shutter = _shutters(use_shutter, fe_shutter, photon_shutter)
    if fe_shutter is not None:
        yield from ensure_shutter_open(fe_shutter)

    # Each detector's prepare sets its exposure, so put it back in the cleanup.
    initial_acquire_times = []
    for det in detectors:
        initial_acquire_times.append((yield from bps.rd(det.driver.acquire_time)))
    initial_velocity = yield from bps.rd(sample_out_motor.velocity)

    _md: dict[str, Any] = {
        "plan_name": "tomo_dark_flat",
        "detectors": [det.name for det in detectors],
        "exposure_time": exposure_md(exposure_times),
        "num_dark_images": num_dark_images,
        "num_flat_images": num_flat_images,
        "sample_out_motor": sample_out_motor.name,
        "sample_out_offset": sample_out_offset,
        "tiled_specs": [spec_md(TOMO_DARK_FLAT_V1)],
    }
    _md.update(md or {})

    @bpp.run_decorator(md=_md)
    @bpp.stage_decorator([*detectors])
    def _run() -> MsgGenerator[None]:
        if photon_shutter is not None:
            yield from ensure_shutter_closed(photon_shutter)

        streams = (("dark", num_dark_images), ("flat", num_flat_images))
        for stream, num_images in streams:
            if stream == "flat":
                if sample_out_velocity is not None:
                    yield from bps.mv(sample_out_motor.velocity, sample_out_velocity)
                yield from bps.mvr(sample_out_motor, sample_out_offset)
                if photon_shutter is not None:
                    yield from ensure_shutter_open(photon_shutter, allow_actuation=True)

            for det in detectors:
                yield from bps.prepare(
                    det,
                    TriggerInfo(
                        livetime=exposure_times[det], collections_per_event=num_images
                    ),
                    group=f"prepare-{stream}",
                )
            yield from bps.wait(group=f"prepare-{stream}")
            yield from bps.trigger_and_read(list(detectors), name=stream)

    def _close_shutter() -> MsgGenerator[None]:
        if photon_shutter is not None:
            yield from ensure_shutter_closed(photon_shutter)

    @bpp.reset_positions_decorator([sample_out_motor])
    def _body() -> MsgGenerator[None]:
        # Close the shutter before reset_positions moves the sample back into the
        # beam, also when the run fails.
        return (yield from bpp.finalize_wrapper(_run(), _close_shutter()))

    def _cleanup() -> MsgGenerator[None]:
        yield from bps.mv(sample_out_motor.velocity, initial_velocity)
        for det, acquire_time in zip(detectors, initial_acquire_times, strict=True):
            yield from bps.mv(det.driver.acquire_time, acquire_time)

    return (yield from bpp.finalize_wrapper(_body(), _cleanup()))


def tomo_flyscan(
    detectors: list[TomoDetector],
    num_projections: int,
    exposure_time: ExposureTime,
    acquire_period: float | None = None,
    images_to_average: int = 1,
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE,
    start: float = 0,
    stop: float = 180,
    use_shutter: bool = True,
    sample_name: str | None = None,
    time_based: bool = True,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
    md: dict[str, Any] | None = None,
) -> MsgGenerator[str]:
    """Take a single tomography fly scan, triggered by the PandA.

    The rotation motor moves to its run-up position while the photon shutter
    opens. Only then are the flyer and detectors armed, the PandA last, so no
    frames are taken before the motor reaches speed. The detectors and the PandA
    (which captures ``rotation_angle``) write to the ``primary`` stream.

    If a few frames are dropped (at most :data:`MAX_MISSING_FRAMES`) the scan
    keeps the frames it has and only logs a warning.

    Afterwards the photon shutter is closed, the motor stopped, and its velocity
    restored, even if the plan fails.

    Parameters
    ----------
    detectors : list[TomoDetector]
        The detectors (Pilatus4, Kinetix or Xspress3) to take projections with.
    num_projections : int
        The number of projections to acquire.
    exposure_time : ExposureTime
        The exposure time of each projection, in seconds: one number for every
        detector, or a mapping that gives every detector its own, keyed by detector
        or by its ophyd ``.name``, e.g. ``{pilatus1: 0.002, "iris1": 0.01}``.
    acquire_period : float | None, default None
        The period between projections, in seconds, shared by every detector. None
        uses the longest of each detector's exposure time plus its deadtime, plus
        :data:`FRAME_PERIOD_MARGIN`.
    images_to_average : int, default 1
        The number of images the Proc plugin combines into each projection. Only
        detectors with a ``proc`` plugin support more than 1.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged images.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    use_shutter : bool, default True
        Whether to open and close the photon shutter, after checking the front-end
        shutter is open. If False the plan never touches the shutters.
    sample_name : str | None, default None
        The name of the sample being scanned.
    time_based : bool, default True
        Whether the triggers are equally spaced in time, as opposed to in position.
    panda : HDFPanda | None, default None
        The PandA producing the triggers; None uses ``panda1``.
    rot_motor : RotationMotor | None, default None
        The rotation motor; None uses ``amPhi``.
    fe_shutter : Shutter | None, default None
        The front-end shutter, which must already be open; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter the plan opens and closes; None uses ``photon_shutter``.
    md : dict[str, Any] | None, default None
        Extra metadata for the run's start document.

    Returns
    -------
    str
        The uid of the run.

    Raises
    ------
    ValueError
        If ``acquire_period`` is shorter than a detector's exposure plus deadtime,
        or ``exposure_time`` is invalid, see
        :func:`xpd_tools.tomo.utils.resolve_exposure_times`.
    RuntimeError
        If a detector drops more than :data:`MAX_MISSING_FRAMES` frames.
    """
    if not detectors:
        raise ValueError("tomo_flyscan needs at least one detector.")
    filter_type = NDProcessFilterType(filter_type)
    exposure_times = resolve_exposure_times(detectors, exposure_time)
    # The trigger train and motor speed follow the longest exposure.
    longest_exposure = max(exposure_times.values())
    if images_to_average > 1:
        no_proc = [det.name for det in detectors if not hasattr(det, "proc")]
        if no_proc:
            raise ValueError(
                f"images_to_average={images_to_average} needs a Proc plugin, which "
                f"{no_proc} lack(s)."
            )
    panda = ensure_available(HDFPanda, panda, DEFAULT_DEVICE_NAMES["panda"])
    rot_motor = ensure_available(
        RotationMotor, rot_motor, DEFAULT_DEVICE_NAMES["rot_motor"]
    )
    fe_shutter, photon_shutter = _shutters(use_shutter, fe_shutter, photon_shutter)

    # The shortest period each detector can run at: its exposure plus deadtime.
    deadtime_results = cast(
        list[asyncio.Future[tuple[Any, float | None]]],
        (yield from bps.wait_for([det.get_trigger_deadtime for det in detectors])),
    )
    deadtimes = {
        det: result.result()[1] or 0.0
        for det, result in zip(detectors, deadtime_results, strict=True)
    }
    min_periods = {det: exposure_times[det] + deadtimes[det] for det in detectors}
    if acquire_period is None:
        acquire_period = max(min_periods.values()) + FRAME_PERIOD_MARGIN
    else:
        for det, min_period in min_periods.items():
            if acquire_period < min_period:
                raise ValueError(
                    f"acquire_period {acquire_period} s is shorter than {det.name}'s "
                    f"exposure time {exposure_times[det]} s plus its deadtime "
                    f"{deadtimes[det]} s."
                )

    _md: dict[str, Any] = {
        "plan_name": "tomo_flyscan",
        "detectors": [det.name for det in detectors],
        "num_points": num_projections,
        "exposure_time": exposure_md(exposure_times),
        "max_exposure_time": longest_exposure,
        "acquire_period": acquire_period,
        "time_based": time_based,
        "start_position": start,
        "stop_position": stop,
        "images_to_average": images_to_average,
        "filter_type": filter_type.value,
        "rotation_motor": rot_motor.name,
        "tiled_specs": [spec_md(TOMO_FLYSCAN_V1)],
    }
    if sample_name is not None:
        _md["sample_name"] = sample_name
    _md.update(md or {})

    if fe_shutter is not None:
        yield from ensure_shutter_open(fe_shutter)

    flyer_info, motor_info = yield from compute_flyscan_models(
        panda,
        rot_motor,
        num_pulses=num_projections * images_to_average,
        max_exposure_time=longest_exposure,
        start=start,
        stop=stop,
        acq_time_overhead=acquire_period - longest_exposure,
        time_based=time_based,
        position_dataset_name=ROTATION_DATASET_NAME,
    )

    # Every detector exposes for its own time on each trigger, and fills the rest
    # of the shared period with deadtime.
    det_trigger_infos = {
        det: TriggerInfo(
            number_of_events=num_projections,
            livetime=exposure_times[det],
            deadtime=acquire_period - exposure_times[det],
            trigger=DetectorTrigger.EXTERNAL_EDGE,
            exposures_per_collection=images_to_average,
        )
        for det in detectors
    }
    panda_trigger_info = TriggerInfo(
        number_of_events=num_projections,
        livetime=longest_exposure,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
        collections_per_event=images_to_average,
    )

    # The motor's fly prepare leaves VELO at the fly speed; restored in _cleanup.
    initial_velocity = yield from bps.rd(rot_motor.velocity)

    flyer = SingleAxisFlyableLogic(panda, rot_motor.panda_encoder).with_device()
    all_detectors = [*detectors, panda]
    all_devices = [*all_detectors, flyer, rot_motor]

    @bpp.stage_decorator(all_devices)
    @bpp.run_decorator(md=_md)
    def _body() -> MsgGenerator[None]:
        if photon_shutter is not None:
            yield from ensure_shutter_open(
                photon_shutter, allow_actuation=True, group="prepare", wait=False
            )
        yield from bps.prepare(rot_motor, motor_info, group="prepare")
        yield from bps.wait(group="prepare")

        # Arm the PandA and detectors only once the motor is at its run-up position,
        # so no frames are captured before kickoff.
        yield from set_proc_filter_type(detectors, filter_type)
        yield from bps.prepare(flyer, flyer_info, group="arm")
        for det in detectors:
            yield from bps.prepare(det, det_trigger_infos[det], group="arm")
        # Let the plugins buffer the whole scan.
        yield from set_plugin_queue_sizes(
            detectors, queue_size=num_projections * images_to_average
        )
        yield from bps.wait(group="arm")
        # Preparing the PandA arms PCAP, which enables position compare, so it must
        # come after position compare is configured.
        yield from bps.prepare(panda, panda_trigger_info, wait=True)

        try:
            yield from bps.fly_stub(
                all_devices,  # type: ignore[arg-type]
                collect_flush_period=max(1.0, acquire_period),
                stream_name="primary",
            )
        except FailedStatus as err:
            yield from _tolerate_missed_frames(
                detectors, all_devices, num_projections, err
            )

    def _cleanup() -> MsgGenerator[None]:
        if photon_shutter is not None:
            yield from ensure_shutter_closed(photon_shutter)
        yield from bps.abs_set(rot_motor.motor_stop, 1, wait=True)
        yield from bps.mv(rot_motor.velocity, initial_velocity)

    return (yield from bpp.finalize_wrapper(_body(), _cleanup()))


def _tolerate_missed_frames(
    detectors: Sequence[TomoDetector],
    all_devices: Sequence[Any],
    num_projections: int,
    err: FailedStatus,
) -> MsgGenerator[None]:
    """Keep a flyscan that dropped only a few frames, or re-raise its failure."""
    # bluesky annotates this as Status, but the RunEngine returns futures.
    indices = cast(
        list[asyncio.Future[int]],
        (yield from bps.wait_for([det.get_index for det in detectors])),
    )
    missing = {
        det.name: num_projections - index.result()
        for det, index in zip(detectors, indices, strict=True)
    }
    most_missing = max(missing.values(), default=0)
    if most_missing == 0:
        # The detectors got every frame, so something else failed.
        raise err
    if most_missing > MAX_MISSING_FRAMES:
        if most_missing >= num_projections / 2:
            # Losing half or more points is not a dropped-frame problem; re-raise the
            # original failure for more helpful debugging.
            raise err
        raise RuntimeError(
            f"Scan failed! Detector(s) missed up to {most_missing} frame(s). "
            f"Failure threshold: {MAX_MISSING_FRAMES}"
        ) from err
    for name, count in missing.items():
        logger.warning("Detector %s missed %d frame(s)", name, count)
    logger.warning(
        "Max missed frames %d is below failure threshold %d",
        most_missing,
        MAX_MISSING_FRAMES,
    )
    # Record the frames that every device did manage to write.
    yield from bps.collect(
        *[dev for dev in all_devices if isinstance(dev, Collectable)],
        name="primary",
    )


def _drop_xspress3_keys(
    exposure_time: ExposureTime, detectors: Sequence[TomoDetector]
) -> ExposureTime:
    """Remove the Xspress3 detectors' entries from an exposure-time mapping."""
    if not isinstance(exposure_time, Mapping):
        return exposure_time
    xspress3 = [d for d in detectors if isinstance(d, Xspress3Detector)]
    xspress3_names = {d.name for d in xspress3}
    return {
        key: value
        for key, value in exposure_time.items()
        if not (
            any(key is d for d in xspress3)
            or (isinstance(key, str) and key in xspress3_names)
        )
    }


def tomo_nd_step_scan(
    detectors: list[TomoDetector],
    *args: Any,
    num_projections: int,
    exposure_time: ExposureTime,
    acquire_period: float | None = None,
    images_to_average: int = 1,
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE,
    start: float = 0,
    stop: float = 180,
    num_repeats: int = 1,
    delay_between_points: float = 0.0,
    use_shutter: bool = True,
    alternate_flyscan_dir: bool = False,
    dark_flat_offset: float = 0.0,
    dark_flat_exposure_time: ExposureTime | None = None,
    num_dark_images: int = 20,
    num_flat_images: int = 50,
    dark_flat_frequency: int = 0,
    sample_out_velocity: float | None = None,
    sample_name: str | None = None,
    time_based: bool = True,
    snake_axes: bool | Sequence[Steppable] = False,
    relative: bool = False,
    num_retries: int = 0,
    sample_out_motor: AsyncEpicsMotor | None = None,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
) -> MsgGenerator[None]:
    """Take a tomography flyscan at each point of an N-D grid of step motors.

    Each tomogram is its own :func:`tomo_flyscan` run, whose start document records
    ``grid_index``, ``num_grid_points``, ``grid_positions`` and the previous
    point's ``previous_grid_point_uid``, plus ``grid_reference_positions`` for a
    relative scan. Step motors are returned to their starting positions when the
    scan ends, whether the axes are absolute or relative.

    Parameters
    ----------
    detectors : list[TomoDetector]
        The detectors to use for each tomo flyscan.
    *args : Any
        Step axes patterned like ``(motor1, start1, stop1, num1, motor2, start2,
        stop2, num2, ...)``, outermost (slowest) axis first, as in
        :func:`bluesky.plans.grid_scan`. With no axes, the flyscan is repeated
        in place.
    num_projections : int
        The number of projections to acquire at each grid point.
    exposure_time : ExposureTime
        The exposure time of each projection, in seconds: one number for every
        detector, or a mapping that gives every detector its own, keyed by detector
        or by its ophyd ``.name``, e.g. ``{pilatus1: 0.002, "iris1": 0.01}``.
    acquire_period : float | None, default None
        The period between projections. See :func:`tomo_flyscan`.
    images_to_average : int, default 1
        The number of images to average for each projection.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged images.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    num_repeats : int, default 1
        The number of times to run the whole grid.
    delay_between_points : float, default 0.0
        Seconds to wait between consecutive flyscans.
    use_shutter : bool, default True
        Whether to use the shutters.
    alternate_flyscan_dir : bool, default False
        Whether to reverse the rotation direction at every other grid point.
    dark_flat_offset : float, default 0.0
        The ``sample_out_offset`` for flats. If non-zero, a :func:`tomo_dark_flat`
        run is taken before and after the scan; if 0, no darks/flats are taken.
    dark_flat_exposure_time : ExposureTime | None, default None
        The exposure time of the darks/flats: a number for every imaging detector,
        or a mapping with every imaging detector's own (keys for Xspress3 detectors
        are ignored). None gives each detector the same exposure time as its
        projections.
    num_dark_images : int, default 20
        Number of dark images per dark/flat.
    num_flat_images : int, default 50
        Number of flat images per dark/flat.
    dark_flat_frequency : int, default 0
        If greater than 0, also take a dark/flat after every this many grid points.
    sample_out_velocity : float | None, default None
        Velocity to move the sample out for flats at; None keeps the current one.
    sample_name : str | None, default None
        The name of the sample being scanned.
    time_based : bool, default True
        Whether the triggers are equally spaced in time, as opposed to in position.
    snake_axes : bool | Sequence[Steppable], default False
        If True, snake every axis but the outermost; otherwise, the motors to snake.
    relative : bool, default False
        Whether each step axis's start and stop are offsets from that motor's
        position when the scan begins, as in :func:`bluesky.plans.rel_grid_scan`.
        Each run's ``grid_positions`` are still absolute, and
        ``grid_reference_positions`` records the starting positions. The rotation
        range (``start``, ``stop``) is always absolute.
    num_retries : int, default 0
        How many times to retry a grid point's flyscan if it fails; 0 never retries.
        A user's abort, stop or halt is never retried.
    sample_out_motor : AsyncEpicsMotor | None, default None
        The motor that moves the sample out for flats; None uses ``amTopX``.
    panda : HDFPanda | None, default None
        The PandA producing the triggers; None uses ``panda1``.
    rot_motor : RotationMotor | None, default None
        The rotation motor; None uses ``amPhi``.
    fe_shutter : Shutter | None, default None
        The front-end shutter; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter; None uses ``photon_shutter``.

    Raises
    ------
    ValueError
        If the step axes are malformed, the outermost axis is snaked, or
        ``exposure_time`` or ``dark_flat_exposure_time`` is invalid.
    RuntimeError
        If a grid point still fails after ``num_retries`` retries.
    """
    num_retries = max(num_retries, 0)
    # Checked here so a bad key fails before anything moves.
    exposure_times: dict[TomoDetector | str, float] = {
        **resolve_exposure_times(detectors, exposure_time)
    }
    # Darks/flats are only for the imaging detectors, and by default match each
    # detector's projection exposure time.
    imaging_detectors = [d for d in detectors if not isinstance(d, Xspress3Detector)]
    if dark_flat_exposure_time is None:
        dark_flat_exposure_times: dict[TomoDetector | str, float] = {
            det: exposure_times[det] for det in imaging_detectors
        }
    else:
        dark_flat_exposure_times = {
            **resolve_exposure_times(
                imaging_detectors,
                _drop_xspress3_keys(dark_flat_exposure_time, detectors),
            )
        }

    if len(args) % 4:
        raise ValueError("Step axes must be given as (motor, start, stop, num) groups.")
    axes = [args[i : i + 4] for i in range(0, len(args), 4)]
    motors = [axis[0] for axis in axes]

    if isinstance(snake_axes, bool):
        snaked = motors[1:] if snake_axes else []
    else:
        snaked = list(snake_axes)
    if motors and motors[0] in snaked:
        raise ValueError("The outermost axis cannot be snaked.")

    if axes:
        pattern_args: list[Any] = [*axes[0]]
        for axis in axes[1:]:
            pattern_args += [*axis, axis[0] in snaked]
        grid = list(plan_patterns.outer_product(pattern_args))
    else:
        grid = [{}]

    # Flats need the sample moved out of the beam, so a zero offset means no
    # darks/flats.
    take_dark_flats = dark_flat_offset != 0

    def _dark_flat() -> MsgGenerator[None]:
        yield from bps.checkpoint()
        yield from tomo_dark_flat(
            detectors=imaging_detectors,
            exposure_time=dark_flat_exposure_times,
            num_dark_images=num_dark_images,
            num_flat_images=num_flat_images,
            sample_out_offset=dark_flat_offset,
            sample_out_velocity=sample_out_velocity,
            use_shutter=use_shutter,
            sample_out_motor=sample_out_motor,
            fe_shutter=fe_shutter,
            photon_shutter=photon_shutter,
        )

    @bpp.reset_positions_decorator(motors)
    def _body() -> MsgGenerator[None]:
        # Relative axes are offsets from where their motors are as the scan starts.
        origins: dict[Any, float] = {}
        if relative:
            for motor in motors:
                origins[motor] = cast(float, (yield from bps.rd(motor)))
        trajectory = [
            {motor: pos + origins.get(motor, 0.0) for motor, pos in point.items()}
            for point in grid
        ] * num_repeats

        if take_dark_flats:
            yield from _dark_flat()

        num_points = len(trajectory)
        pos_cache: defaultdict[Any, Any] = defaultdict(lambda: None)
        previous_uid: str | None = None
        for i, point in enumerate(trajectory):
            # A deferred pause stops here; resuming starts this point afresh.
            yield from bps.checkpoint()
            if i > 0 and delay_between_points > 0:
                yield from bps.sleep(delay_between_points)

            # Only moves the axes whose position changed since the last point.
            yield from bps.move_per_step(point, pos_cache)

            reverse = alternate_flyscan_dir and i % 2 == 1
            point_md: dict[str, Any] = {
                "grid_index": i,
                "num_grid_points": num_points,
                "grid_positions": {motor.name: pos for motor, pos in point.items()},
            }
            if previous_uid is not None:
                point_md["previous_grid_point_uid"] = previous_uid
            if origins:
                point_md["grid_reference_positions"] = {
                    motor.name: pos for motor, pos in origins.items()
                }

            for attempt in range(1, num_retries + 2):
                try:
                    previous_uid = yield from tomo_flyscan(
                        detectors=detectors,
                        num_projections=num_projections,
                        exposure_time=exposure_times,
                        acquire_period=acquire_period,
                        images_to_average=images_to_average,
                        filter_type=filter_type,
                        start=stop if reverse else start,
                        stop=start if reverse else stop,
                        use_shutter=use_shutter,
                        sample_name=sample_name,
                        time_based=time_based,
                        panda=panda,
                        rot_motor=rot_motor,
                        fe_shutter=fe_shutter,
                        photon_shutter=photon_shutter,
                        md=point_md,
                    )
                    break
                except (RunEngineControlException, PlanHalt):
                    # Never retry a user's abort, stop or halt request.
                    raise
                except Exception as err:
                    if attempt > num_retries:
                        raise RuntimeError(
                            f"Flyscan at point {i} failed after {attempt} "
                            f"attempt(s): {err}"
                        ) from err
                    logger.error(
                        "Flyscan at point %d failed (%s), retrying (attempt %d/%d).",
                        i,
                        err,
                        attempt,
                        num_retries,
                    )

            # The final dark/flat below covers the last point.
            is_last = i == num_points - 1
            if (
                take_dark_flats
                and dark_flat_frequency > 0
                and not is_last
                and (i + 1) % dark_flat_frequency == 0
            ):
                yield from _dark_flat()

        if take_dark_flats:
            yield from _dark_flat()

    return (yield from _body())


def tomo_flyscan_loop(
    detectors: list[TomoDetector],
    num_projections: int,
    exposure_time: ExposureTime,
    acquire_period: float | None = None,
    images_to_average: int = 1,
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE,
    start: float = 0,
    stop: float = 180,
    num_iterations: int = 1,
    delay_between_tomograms: float = 0.0,
    alternate_flyscan_dir: bool = False,
    dark_flat_offset: float = 0.0,
    num_dark_images: int = 20,
    num_flat_images: int = 50,
    dark_flat_frequency: int = 0,
    dark_flat_exposure_time: ExposureTime | None = None,
    sample_out_velocity: float | None = None,
    use_shutter: bool = True,
    sample_name: str | None = None,
    time_based: bool = True,
    num_retries: int = 0,
    sample_out_motor: AsyncEpicsMotor | None = None,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
) -> MsgGenerator[None]:
    """Take repeated tomography flyscans in place, with darks/flats around them.

    A :func:`tomo_nd_step_scan` with no step axes. See it for the parameters not
    listed here.

    Parameters
    ----------
    detectors : list[TomoDetector]
        The detectors to use for each tomo flyscan.
    num_projections : int
        The number of projections per tomogram.
    exposure_time : ExposureTime
        The exposure time of each projection, in seconds: a number, or a mapping
        with every detector's own. See :func:`tomo_flyscan`.
    acquire_period : float | None, default None
        The period between projections.
    images_to_average : int, default 1
        The number of images to average for each projection.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged images.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    num_iterations : int, default 1
        The number of tomograms to take.
    delay_between_tomograms : float, default 0.0
        Seconds to wait between consecutive tomograms.
    alternate_flyscan_dir : bool, default False
        Whether to reverse the rotation direction every other tomogram.
    dark_flat_offset : float, default 0.0
        The sample-out offset for flats; 0 takes no darks/flats.
    num_dark_images : int, default 20
        Number of dark images per dark/flat.
    num_flat_images : int, default 50
        Number of flat images per dark/flat.
    dark_flat_frequency : int, default 0
        If greater than 0, also take a dark/flat after every this many tomograms.
    dark_flat_exposure_time : ExposureTime | None, default None
        The exposure time of the darks/flats, a number or a mapping; None gives
        each detector the same exposure time as its projections. See
        :func:`tomo_nd_step_scan`.
    sample_out_velocity : float | None, default None
        Velocity to move the sample out for flats at.
    use_shutter : bool, default True
        Whether to use the shutters.
    sample_name : str | None, default None
        The name of the sample being scanned.
    time_based : bool, default True
        Whether the triggers are equally spaced in time.
    num_retries : int, default 0
        How many times to retry a failed tomogram.
    sample_out_motor : AsyncEpicsMotor | None, default None
        The motor that moves the sample out for flats; None uses ``amTopX``.
    panda : HDFPanda | None, default None
        The PandA; None uses ``panda1``.
    rot_motor : RotationMotor | None, default None
        The rotation motor; None uses ``amPhi``.
    fe_shutter : Shutter | None, default None
        The front-end shutter; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter; None uses ``photon_shutter``.
    """
    yield from tomo_nd_step_scan(
        detectors,
        num_projections=num_projections,
        exposure_time=exposure_time,
        acquire_period=acquire_period,
        images_to_average=images_to_average,
        filter_type=filter_type,
        start=start,
        stop=stop,
        num_repeats=num_iterations,
        delay_between_points=delay_between_tomograms,
        use_shutter=use_shutter,
        alternate_flyscan_dir=alternate_flyscan_dir,
        dark_flat_offset=dark_flat_offset,
        dark_flat_exposure_time=dark_flat_exposure_time,
        num_dark_images=num_dark_images,
        num_flat_images=num_flat_images,
        dark_flat_frequency=dark_flat_frequency,
        sample_out_velocity=sample_out_velocity,
        sample_name=sample_name,
        time_based=time_based,
        num_retries=num_retries,
        sample_out_motor=sample_out_motor,
        panda=panda,
        rot_motor=rot_motor,
        fe_shutter=fe_shutter,
        photon_shutter=photon_shutter,
    )


def tomo_1d_step_scan(
    detectors: list[TomoDetector],
    step_motor: Steppable,
    step_start: float,
    step_stop: float,
    num_tomograms: int,
    num_projections: int,
    exposure_time: ExposureTime,
    acquire_period: float | None = None,
    images_to_average: int = 1,
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE,
    start: float = 0,
    stop: float = 180,
    use_shutter: bool = True,
    sample_name: str | None = None,
    alternate_flyscan_dir: bool = False,
    relative: bool = False,
    num_retries: int = 3,
    dark_flat_offset: float = 0.0,
    num_dark_images: int = 20,
    num_flat_images: int = 50,
    dark_flat_frequency: int = 0,
    dark_flat_exposure_time: ExposureTime | None = None,
    sample_out_velocity: float | None = None,
    time_based: bool = True,
    sample_out_motor: AsyncEpicsMotor | None = None,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
) -> MsgGenerator[None]:
    """Take a tomography flyscan at each point along one step motor.

    A :func:`tomo_nd_step_scan` with one step axis. See it for the parameters not
    listed here.

    Parameters
    ----------
    detectors : list[TomoDetector]
        The detectors to use for each tomo flyscan.
    step_motor : Steppable
        The motor to step between tomograms.
    step_start : float
        The first position of ``step_motor``.
    step_stop : float
        The last position of ``step_motor``.
    num_tomograms : int
        The number of points (tomograms) along ``step_motor``.
    num_projections : int
        The number of projections per tomogram.
    exposure_time : ExposureTime
        The exposure time of each projection, in seconds: a number, or a mapping
        with every detector's own. See :func:`tomo_flyscan`.
    acquire_period : float | None, default None
        The period between projections.
    images_to_average : int, default 1
        The number of images to average for each projection.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged images.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    use_shutter : bool, default True
        Whether to use the shutters.
    sample_name : str | None, default None
        The name of the sample being scanned.
    alternate_flyscan_dir : bool, default False
        Whether to reverse the rotation direction every other tomogram.
    relative : bool, default False
        Whether ``step_start`` and ``step_stop`` are offsets from ``step_motor``'s
        position when the scan begins. See :func:`tomo_nd_step_scan`.
    num_retries : int, default 3
        How many times to retry a failed tomogram.
    dark_flat_offset : float, default 0.0
        The sample-out offset for flats; 0 takes no darks/flats.
    num_dark_images : int, default 20
        Number of dark images per dark/flat.
    num_flat_images : int, default 50
        Number of flat images per dark/flat.
    dark_flat_frequency : int, default 0
        If greater than 0, also take a dark/flat after every this many tomograms.
    dark_flat_exposure_time : ExposureTime | None, default None
        The exposure time of the darks/flats, a number or a mapping; None gives
        each detector the same exposure time as its projections. See
        :func:`tomo_nd_step_scan`.
    sample_out_velocity : float | None, default None
        Velocity to move the sample out for flats at.
    time_based : bool, default True
        Whether the triggers are equally spaced in time.
    sample_out_motor : AsyncEpicsMotor | None, default None
        The motor that moves the sample out for flats; None uses ``amTopX``.
    panda : HDFPanda | None, default None
        The PandA; None uses ``panda1``.
    rot_motor : RotationMotor | None, default None
        The rotation motor; None uses ``amPhi``.
    fe_shutter : Shutter | None, default None
        The front-end shutter; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter; None uses ``photon_shutter``.
    """
    yield from tomo_nd_step_scan(
        detectors,
        step_motor,
        step_start,
        step_stop,
        num_tomograms,
        num_projections=num_projections,
        exposure_time=exposure_time,
        acquire_period=acquire_period,
        images_to_average=images_to_average,
        filter_type=filter_type,
        start=start,
        stop=stop,
        use_shutter=use_shutter,
        alternate_flyscan_dir=alternate_flyscan_dir,
        dark_flat_offset=dark_flat_offset,
        dark_flat_exposure_time=dark_flat_exposure_time,
        num_dark_images=num_dark_images,
        num_flat_images=num_flat_images,
        dark_flat_frequency=dark_flat_frequency,
        sample_out_velocity=sample_out_velocity,
        sample_name=sample_name,
        time_based=time_based,
        relative=relative,
        num_retries=num_retries,
        sample_out_motor=sample_out_motor,
        panda=panda,
        rot_motor=rot_motor,
        fe_shutter=fe_shutter,
        photon_shutter=photon_shutter,
    )


def tomo_2d_step_scan(
    detectors: list[TomoDetector],
    outer_motor: Steppable,
    outer_start: float,
    outer_stop: float,
    outer_num: int,
    inner_motor: Steppable,
    inner_start: float,
    inner_stop: float,
    inner_num: int,
    num_projections: int,
    exposure_time: ExposureTime,
    acquire_period: float | None = None,
    images_to_average: int = 1,
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE,
    start: float = 0,
    stop: float = 180,
    use_shutter: bool = True,
    sample_name: str | None = None,
    alternate_flyscan_dir: bool = False,
    snake_axes: bool = False,
    relative: bool = False,
    num_retries: int = 3,
    dark_flat_offset: float = 0.0,
    num_dark_images: int = 20,
    num_flat_images: int = 50,
    dark_flat_frequency: int = 0,
    dark_flat_exposure_time: ExposureTime | None = None,
    sample_out_velocity: float | None = None,
    time_based: bool = True,
    sample_out_motor: AsyncEpicsMotor | None = None,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
) -> MsgGenerator[None]:
    """Take a tomography flyscan at each point of a 2-D grid of step motors.

    A :func:`tomo_nd_step_scan` with two step axes. See it for the parameters not
    listed here.

    Parameters
    ----------
    detectors : list[TomoDetector]
        The detectors to use for each tomo flyscan.
    outer_motor : Steppable
        The slow (outer) step motor.
    outer_start : float
        The first position of ``outer_motor``.
    outer_stop : float
        The last position of ``outer_motor``.
    outer_num : int
        The number of points along ``outer_motor``.
    inner_motor : Steppable
        The fast (inner) step motor.
    inner_start : float
        The first position of ``inner_motor``.
    inner_stop : float
        The last position of ``inner_motor``.
    inner_num : int
        The number of points along ``inner_motor``.
    num_projections : int
        The number of projections per tomogram.
    exposure_time : ExposureTime
        The exposure time of each projection, in seconds: a number, or a mapping
        with every detector's own. See :func:`tomo_flyscan`.
    acquire_period : float | None, default None
        The period between projections.
    images_to_average : int, default 1
        The number of images to average for each projection.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged images.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    use_shutter : bool, default True
        Whether to use the shutters.
    sample_name : str | None, default None
        The name of the sample being scanned.
    alternate_flyscan_dir : bool, default False
        Whether to reverse the rotation direction every other tomogram.
    snake_axes : bool, default False
        Whether to snake the inner axis.
    relative : bool, default False
        Whether the outer and inner starts and stops are offsets from their
        motors' positions when the scan begins. See :func:`tomo_nd_step_scan`.
    num_retries : int, default 3
        How many times to retry a failed tomogram.
    dark_flat_offset : float, default 0.0
        The sample-out offset for flats; 0 takes no darks/flats.
    num_dark_images : int, default 20
        Number of dark images per dark/flat.
    num_flat_images : int, default 50
        Number of flat images per dark/flat.
    dark_flat_frequency : int, default 0
        If greater than 0, also take a dark/flat after every this many tomograms.
    dark_flat_exposure_time : ExposureTime | None, default None
        The exposure time of the darks/flats, a number or a mapping; None gives
        each detector the same exposure time as its projections. See
        :func:`tomo_nd_step_scan`.
    sample_out_velocity : float | None, default None
        Velocity to move the sample out for flats at.
    time_based : bool, default True
        Whether the triggers are equally spaced in time.
    sample_out_motor : AsyncEpicsMotor | None, default None
        The motor that moves the sample out for flats; None uses ``amTopX``.
    panda : HDFPanda | None, default None
        The PandA; None uses ``panda1``.
    rot_motor : RotationMotor | None, default None
        The rotation motor; None uses ``amPhi``.
    fe_shutter : Shutter | None, default None
        The front-end shutter; None uses ``fe_shutter``.
    photon_shutter : Shutter | None, default None
        The photon shutter; None uses ``photon_shutter``.
    """
    yield from tomo_nd_step_scan(
        detectors,
        outer_motor,
        outer_start,
        outer_stop,
        outer_num,
        inner_motor,
        inner_start,
        inner_stop,
        inner_num,
        num_projections=num_projections,
        exposure_time=exposure_time,
        acquire_period=acquire_period,
        images_to_average=images_to_average,
        filter_type=filter_type,
        start=start,
        stop=stop,
        use_shutter=use_shutter,
        alternate_flyscan_dir=alternate_flyscan_dir,
        dark_flat_offset=dark_flat_offset,
        dark_flat_exposure_time=dark_flat_exposure_time,
        num_dark_images=num_dark_images,
        num_flat_images=num_flat_images,
        dark_flat_frequency=dark_flat_frequency,
        sample_out_velocity=sample_out_velocity,
        sample_name=sample_name,
        time_based=time_based,
        snake_axes=snake_axes,
        relative=relative,
        num_retries=num_retries,
        sample_out_motor=sample_out_motor,
        panda=panda,
        rot_motor=rot_motor,
        fe_shutter=fe_shutter,
        photon_shutter=photon_shutter,
    )
