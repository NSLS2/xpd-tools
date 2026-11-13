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
from bluesky.utils import (
    FailedStatus,
    MsgGenerator,
    PlanHalt,
    RunEngineControlException,
    short_uid,
)
from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.core import DetectorTrigger, TriggerInfo
from ophyd_async.epics.motor import Motor as AsyncEpicsMotor
from ophyd_async.fastcs.panda import HDFPanda

from xpd_tools.detectors.utils import set_plugin_queue_sizes, set_proc_filter_type
from xpd_tools.flyers import SingleAxisFlyableLogic
from xpd_tools.motors import RotationMotor
from xpd_tools.plans.plans import compute_flyscan_models
from xpd_tools.shutter import Shutter, ensure_shutter_closed, ensure_shutter_open
from xpd_tools.specs import TOMO_DARK_FLAT_V1, TOMO_FLYSCAN_V1, spec_md
from xpd_tools.tomo.utils import (
    ROTATION_STREAM,
    DetectorConfig,
    DetectorConfigLike,
    ExposureTime,
    TomoDetector,
    TriggerRouting,
    exposure_md,
    resolve_exposure_times,
    to_detector_configs,
    validate_detector_configs,
)
from xpd_tools.utils import ensure_available

logger = logging.getLogger(__name__)

#: Most frames any detector may drop before a flyscan is treated as failed.
MAX_MISSING_FRAMES = 10

#: Time added to the base trigger period when ``trigger_period`` is not given.
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
    detector_configs: Sequence[DetectorConfigLike],
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

    Each detector takes its darks and flats with the exposure time of its config,
    the same as the projections they correct. Later runs link to this one through
    ``last_dark_flat_uid``, see :class:`xpd_tools.tomo.utils.LastDarkFlatUIDTracker`.

    Parameters
    ----------
    detector_configs : Sequence[DetectorConfigLike]
        One detector config (or dict) per imaging detector (Pilatus4 or Kinetix).
        Only each config's detector and exposure time are used.
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
        If a detector is not an imaging detector, appears twice or shares a name,
        or ``sample_out_offset`` is zero.
    """
    configs = to_detector_configs(detector_configs)
    for config in configs:
        if isinstance(config.detector, Xspress3Detector):
            raise ValueError(
                f"{config.name} is an Xspress3 fluorescence detector, which takes no "
                "darks or flats. Pass only imaging detectors."
            )
    if sample_out_offset == 0:
        raise ValueError("sample_out_offset must be non-zero to move the sample out.")
    detectors = [config.detector for config in configs]
    exposure_times = resolve_exposure_times(
        detectors, {config.detector: config.exposure_time for config in configs}
    )

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
    detector_configs: Sequence[DetectorConfigLike],
    trigger_period: float | None = None,
    start: float = 0,
    stop: float = 180,
    use_shutter: bool = True,
    sample_name: str | None = None,
    time_based: bool = False,
    panda: HDFPanda | None = None,
    rot_motor: RotationMotor | None = None,
    fe_shutter: Shutter | None = None,
    photon_shutter: Shutter | None = None,
    md: dict[str, Any] | None = None,
) -> MsgGenerator[str]:
    """Take a single tomography fly scan, triggered by the PandA.

    ``nproj`` projections are ``nproj`` angular intervals over ``start`` to ``stop``,
    and the scan also takes an image at ``stop`` (0 and 360 degrees are the same view),
    so a detector writes ``nproj + 1`` images, ``(stop - start) / nproj`` apart.

    Each detector runs at its own rate. The PandA produces one base pulse
    (``PULSE1.OUT``) per image of the detector with the most projections, and
    captures ``rotation_angle`` on every base pulse. A detector needing fewer
    triggers is fed every D-th base pulse through a PandA DIV block, starting with
    the first, so trigger ``k`` of a detector with divisor D comes at base pulse
    ``k * D``. Each detector writes to a stream named after it, and the PandA to
    the ``rotation`` stream.

    The rotation motor moves to its run-up position while the photon shutter
    opens. Only then are the flyer and detectors armed, the PandA last, so no
    frames are taken before the motor reaches speed.

    If a few frames are dropped (at most :data:`MAX_MISSING_FRAMES` per device) the
    scan keeps the frames it has and only logs a warning.

    Afterwards the photon shutter is closed, the motor stopped, and its velocity
    restored, even if the plan fails.

    Parameters
    ----------
    detector_configs : Sequence[DetectorConfigLike]
        One :class:`~xpd_tools.tomo.utils.DetectorConfig` (or a dict of its
        fields) per detector: the detector, its number of projections ``nproj``,
        exposure time, PandA TTL output and filter type. At most three, and the
        most projections must be a multiple of every detector's, see
        :func:`~xpd_tools.tomo.utils.validate_detector_configs`.
    trigger_period : float | None, default None
        The time between base pulses, in seconds: the frame period of the fastest
        detector. A detector with divisor D fires every D base pulses. None uses
        the shortest period every detector can keep up with, plus
        :data:`FRAME_PERIOD_MARGIN`.
    start : float, default 0
        The starting angle of the rotation, in degrees.
    stop : float, default 180
        The stopping angle of the rotation, in degrees.
    use_shutter : bool, default True
        Whether to open and close the photon shutter, after checking the front-end
        shutter is open. If False the plan never touches the shutters.
    sample_name : str | None, default None
        The name of the sample being scanned.
    time_based : bool, default False
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
        If the detector configs don't work together, see
        :func:`~xpd_tools.tomo.utils.validate_detector_configs`, or
        ``trigger_period`` is too short for a detector's exposure plus deadtime.
    RuntimeError
        If a device drops more than :data:`MAX_MISSING_FRAMES` frames.
    """
    configs = to_detector_configs(detector_configs)
    panda = ensure_available(HDFPanda, panda, DEFAULT_DEVICE_NAMES["panda"])
    rot_motor = ensure_available(
        RotationMotor, rot_motor, DEFAULT_DEVICE_NAMES["rot_motor"]
    )
    # Check the base pulses fit the encoder counts of the scan before touching
    # any hardware.
    encoder_resolution = yield from bps.rd(rot_motor.encoder_resolution)
    counts = (
        {}
        if time_based
        else {"start": start, "stop": stop, "encoder_resolution": encoder_resolution}
    )
    routing = validate_detector_configs(configs, **counts)
    detectors = [config.detector for config in configs]
    divisors = {
        config.detector: route.divisor
        for config, route in zip(configs, routing.routes, strict=True)
    }
    fe_shutter, photon_shutter = _shutters(use_shutter, fe_shutter, photon_shutter)

    # A detector fired every D base pulses needs D base periods for its exposure
    # plus deadtime, so the base period must be at least (exposure + deadtime) / D.
    deadtime_results = cast(
        list[asyncio.Future[tuple[Any, float | None]]],
        (yield from bps.wait_for([det.get_trigger_deadtime for det in detectors])),
    )
    deadtimes = {
        det: result.result()[1] or 0.0
        for det, result in zip(detectors, deadtime_results, strict=True)
    }
    min_periods = {
        config.detector: (config.exposure_time + deadtimes[config.detector])
        / divisors[config.detector]
        for config in configs
    }
    if trigger_period is None:
        trigger_period = max(min_periods.values()) + FRAME_PERIOD_MARGIN
    else:
        for config in configs:
            det = config.detector
            if trigger_period < min_periods[det]:
                raise ValueError(
                    f"trigger_period {trigger_period} s is too short for "
                    f"{config.name}: fired every {divisors[det]} base pulse(s), it "
                    f"needs {min_periods[det] * divisors[det]} s for its exposure "
                    f"time {config.exposure_time} s plus its deadtime "
                    f"{deadtimes[det]} s."
                )

    _md: dict[str, Any] = {
        "plan_name": "tomo_flyscan",
        "detectors": [config.name for config in configs],
        "detector_configs": {
            config.name: {
                "nproj": config.nproj,
                "num_images": config.num_images,
                "exposure_time": config.exposure_time,
                "images_to_average": config.images_to_average,
                "filter_type": config.filter_type.value,
                "ttl_out": config.ttl_out,
                "trigger_divisor": divisors[config.detector],
            }
            for config in configs
        },
        "num_base_pulses": routing.num_base_pulses,
        "trigger_period": trigger_period,
        "time_based": time_based,
        "start_position": start,
        "stop_position": stop,
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
        num_pulses=routing.num_base_pulses,
        max_exposure_time=trigger_period,
        start=start,
        stop=stop,
        acq_time_overhead=0.0,
        time_based=time_based,
        position_dataset_name=ROTATION_DATASET_NAME,
    )
    flyer_info = flyer_info.model_copy(update={"ttl_routes": routing.routes})

    # Each detector exposes for its own time on each of its triggers, and fills the
    # rest of its D base periods with deadtime.
    det_trigger_infos = {
        config.detector: TriggerInfo(
            number_of_events=config.num_images,
            livetime=config.exposure_time,
            deadtime=divisors[config.detector] * trigger_period - config.exposure_time,
            trigger=DetectorTrigger.EXTERNAL_EDGE,
            exposures_per_collection=config.images_to_average,
        )
        for config in configs
    }
    panda_trigger_info = TriggerInfo(
        number_of_events=routing.num_base_pulses,
        livetime=trigger_period,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
    )

    # The motor's fly prepare leaves VELO at the fly speed; restored in _cleanup.
    initial_velocity = yield from bps.rd(rot_motor.velocity)

    flyer = SingleAxisFlyableLogic(panda, rot_motor.panda_encoder).with_device()
    all_devices = [*detectors, panda, flyer, rot_motor]
    streams: dict[str, list[Any]] = {
        **{config.name: [config.detector] for config in configs},
        ROTATION_STREAM: [panda],
    }

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
        for config in configs:
            yield from set_proc_filter_type([config.detector], config.filter_type)
            # Let the plugins buffer the whole scan.
            yield from set_plugin_queue_sizes(config.detector, config.num_triggers)
        yield from bps.prepare(flyer, flyer_info, group="arm")
        for det in detectors:
            yield from bps.prepare(det, det_trigger_infos[det], group="arm")
        yield from bps.wait(group="arm")
        # Preparing the PandA arms PCAP, which enables position compare, so it must
        # come after position compare is configured.
        yield from bps.prepare(panda, panda_trigger_info, wait=True)

        try:
            yield from _fly_to_streams(
                all_devices, streams, flush_period=max(1.0, trigger_period)
            )
        except FailedStatus as err:
            yield from _tolerate_missed_frames(configs, panda, routing, streams, err)

    def _cleanup() -> MsgGenerator[None]:
        if photon_shutter is not None:
            yield from ensure_shutter_closed(photon_shutter)
        yield from bps.abs_set(rot_motor.motor_stop, 1, wait=True)
        yield from bps.mv(rot_motor.velocity, initial_velocity)

    return (yield from bpp.finalize_wrapper(_body(), _cleanup()))


def _collect_streams(streams: Mapping[str, Sequence[Any]]) -> MsgGenerator[None]:
    """Collect each stream's devices into that stream."""
    for name, devices in streams.items():
        yield from bps.collect_all(*devices, name=name)


def _fly_to_streams(
    flyers: Sequence[Any],
    streams: Mapping[str, Sequence[Any]],
    flush_period: float,
) -> MsgGenerator[None]:
    """Fly ``flyers``, collecting each stream's devices into its own stream.

    :func:`bluesky.plan_stubs.fly_stub` collects every device into one stream,
    which needs them all to produce the same number of frames. Detectors running
    at different rates each get a stream instead.
    """
    for name, devices in streams.items():
        yield from bps.declare_stream(*devices, name=name)
    yield from bps.kickoff_all(*flyers, wait=True)
    group = short_uid(label="complete")
    yield from bps.complete_all(*flyers, group=group, wait=False)
    done = False
    while not done:
        done = yield from bps.wait(
            group=group, timeout=flush_period, error_on_timeout=False
        )
        yield from _collect_streams(streams)


def _tolerate_missed_frames(
    configs: Sequence[DetectorConfig],
    panda: HDFPanda,
    routing: TriggerRouting,
    streams: Mapping[str, Sequence[Any]],
    err: FailedStatus,
) -> MsgGenerator[None]:
    """Keep a flyscan that dropped only a few frames, or re-raise its failure."""
    expected: dict[Any, int] = {
        **{config.detector: config.num_images for config in configs},
        panda: routing.num_base_pulses,
    }
    devices = list(expected)
    # bluesky annotates this as Status, but the RunEngine returns futures.
    indices = cast(
        list[asyncio.Future[int]],
        (yield from bps.wait_for([dev.get_index for dev in devices])),
    )
    missing = {
        dev: expected[dev] - index.result()
        for dev, index in zip(devices, indices, strict=True)
    }
    most_missing = max(missing.values(), default=0)
    if most_missing == 0:
        # Every device got every frame, so something else failed.
        raise err
    if most_missing > MAX_MISSING_FRAMES:
        if any(count >= expected[dev] / 2 for dev, count in missing.items()):
            # Losing half or more of a device's points is not a dropped-frame
            # problem; re-raise the original failure for more helpful debugging.
            raise err
        raise RuntimeError(
            f"Scan failed! Device(s) missed up to {most_missing} frame(s). "
            f"Failure threshold: {MAX_MISSING_FRAMES}"
        ) from err
    for dev, count in missing.items():
        if count:
            logger.warning("%s missed %d frame(s)", dev.name, count)
    logger.warning(
        "Max missed frames %d is below failure threshold %d",
        most_missing,
        MAX_MISSING_FRAMES,
    )
    # Record the frames that every device did manage to write.
    yield from _collect_streams(streams)


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
    detector_configs: Sequence[DetectorConfigLike],
    *args: Any,
    trigger_period: float | None = None,
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
    time_based: bool = False,
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
    detector_configs : Sequence[DetectorConfigLike]
        One :class:`~xpd_tools.tomo.utils.DetectorConfig` (or a dict of its
        fields) per detector: the detector, its number of projections ``nproj``,
        exposure time, PandA TTL output and filter type. At most three, and the
        most projections must be a multiple of every detector's, see
        :func:`~xpd_tools.tomo.utils.validate_detector_configs`.
    *args : Any
        Step axes patterned like ``(motor1, start1, stop1, num1, motor2, start2,
        stop2, num2, ...)``, outermost (slowest) axis first, as in
        :func:`bluesky.plans.grid_scan`. With no axes, the flyscan is repeated
        in place.
    trigger_period : float | None, default None
        The time between base pulses. See :func:`tomo_flyscan`.
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
        are ignored). None gives each detector the exposure time of its config.
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
    time_based : bool, default False
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
        If the step axes are malformed, the outermost axis is snaked, the detector
        configs don't work together, or ``dark_flat_exposure_time`` is invalid.
    RuntimeError
        If a grid point still fails after ``num_retries`` retries.
    """
    num_retries = max(num_retries, 0)
    # Checked here so bad configs fail before anything moves.
    configs = to_detector_configs(detector_configs)
    validate_detector_configs(configs)
    # Darks/flats are only for the imaging detectors, and by default use each
    # detector's projection exposure time.
    dark_flat_configs = [
        config
        for config in configs
        if not isinstance(config.detector, Xspress3Detector)
    ]
    if dark_flat_exposure_time is not None:
        times = resolve_exposure_times(
            [config.detector for config in dark_flat_configs],
            _drop_xspress3_keys(
                dark_flat_exposure_time, [config.detector for config in configs]
            ),
        )
        dark_flat_configs = [
            config.model_copy(update={"exposure_time": times[config.detector]})
            for config in dark_flat_configs
        ]

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
            detector_configs=dark_flat_configs,
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
                        detector_configs=configs,
                        trigger_period=trigger_period,
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
    detector_configs: Sequence[DetectorConfigLike],
    trigger_period: float | None = None,
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
    time_based: bool = False,
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
    detector_configs : Sequence[DetectorConfigLike]
        One detector config (or dict) per detector. See :func:`tomo_flyscan`.
    trigger_period : float | None, default None
        The time between base pulses. See :func:`tomo_flyscan`.
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
    time_based : bool, default False
        Whether the triggers are equally spaced in time.
    num_retries : int, default 0
        How many times to retry a failed tomogram.
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
    """
    yield from tomo_nd_step_scan(
        detector_configs,
        trigger_period=trigger_period,
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
    detector_configs: Sequence[DetectorConfigLike],
    step_motor: Steppable,
    step_start: float,
    step_stop: float,
    num_tomograms: int,
    trigger_period: float | None = None,
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
    time_based: bool = False,
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
    detector_configs : Sequence[DetectorConfigLike]
        One detector config (or dict) per detector. See :func:`tomo_flyscan`.
    step_motor : Steppable
        The motor to step between tomograms.
    step_start : float
        The first position of ``step_motor``.
    step_stop : float
        The last position of ``step_motor``.
    num_tomograms : int
        The number of points (tomograms) along ``step_motor``.
    trigger_period : float | None, default None
        The time between base pulses. See :func:`tomo_flyscan`.
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
    time_based : bool, default False
        Whether the triggers are equally spaced in time.
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
    """
    yield from tomo_nd_step_scan(
        detector_configs,
        step_motor,
        step_start,
        step_stop,
        num_tomograms,
        trigger_period=trigger_period,
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
    detector_configs: Sequence[DetectorConfigLike],
    outer_motor: Steppable,
    outer_start: float,
    outer_stop: float,
    outer_num: int,
    inner_motor: Steppable,
    inner_start: float,
    inner_stop: float,
    inner_num: int,
    trigger_period: float | None = None,
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
    time_based: bool = False,
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
    detector_configs : Sequence[DetectorConfigLike]
        One detector config (or dict) per detector. See :func:`tomo_flyscan`.
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
    trigger_period : float | None, default None
        The time between base pulses. See :func:`tomo_flyscan`.
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
    time_based : bool, default False
        Whether the triggers are equally spaced in time.
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
    """
    yield from tomo_nd_step_scan(
        detector_configs,
        outer_motor,
        outer_start,
        outer_stop,
        outer_num,
        inner_motor,
        inner_start,
        inner_stop,
        inner_num,
        trigger_period=trigger_period,
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
