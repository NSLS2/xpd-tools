"""Acquisition plans for XPD beamline at NSLS-II."""

import logging
from collections.abc import Generator
from typing import Any

import bluesky.plan_stubs as bps
from bluesky.utils import Msg
from ophyd_async.core import (
    DetectorTrigger,
    FlyMotorInfo,
    TriggerInfo,
)
from ophyd_async.epics.adcore import AreaDetector
from ophyd_async.fastcs.panda import HDFPanda

from xpd_tools.detectors.utils import get_detector_acq_times

from ..flyers import (
    SingleAxisFlyableLogic,
    SingleAxisFlyscanInfo,
    construct_fly_info_models,
)
from ..motors import FlyscanMotor

logger = logging.getLogger(__name__)

#: Largest value a PandA position can hold.
PANDA_COUNTER_LIMIT = 2**31 - 1


def _fits_counter_range(
    current_encoder_value: int,
    current_position: float,
    encoder_resolution: float,
    positions: tuple[float, ...],
) -> bool:
    """Ensure the PandA counter doesn't overflow."""
    return all(
        abs(
            current_encoder_value
            + round((position - current_position) / encoder_resolution)
        )
        <= PANDA_COUNTER_LIMIT
        for position in (current_position, *positions)
    )


def compute_flyscan_models(
    panda: HDFPanda,
    motor: FlyscanMotor,
    num_pulses: int,
    max_exposure_time: float,
    start: float,
    stop: float,
    acq_time_overhead: float = 0.001,
    time_based: bool = False,
    position_dataset_name: str = "Angle",
    position_dataset_units: str = "deg",
) -> Generator[Msg, Any, tuple[SingleAxisFlyscanInfo, FlyMotorInfo]]:
    """Read the motor and PandA, and build the flyer and motor models for a flyscan.

    Refuses an encoder that resets on the index mark, and re-zeroes the PandA
    encoder counter if the scan would overflow its 32 bit range.

    Parameters
    ----------
    panda : HDFPanda
        PandA producing the triggers.
    motor : FlyscanMotor
        Motor to fly; its ``panda_encoder`` selects the INENC/CALC blocks.
    num_pulses : int
        Number of trigger pulses (exposures) in the scan.
    max_exposure_time : float
        Longest exposure time of the detectors, in seconds.
    start : float
        Start position of the scan.
    stop : float
        Stop position of the scan.
    acq_time_overhead : float, default 0.001
        Time added to each exposure, in seconds.
    time_based : bool, default False
        Triggers equally spaced in time if True, otherwise in position.
    position_dataset_name : str, default "Angle"
        Name of the PandA dataset holding the captured motor position.
    position_dataset_units : str, default "deg"
        Units of the captured motor position.

    Returns
    -------
    tuple[SingleAxisFlyscanInfo, FlyMotorInfo]
        The models to prepare the flyer and the motor with.

    Raises
    ------
    ValueError
        If the encoder resets on the index mark, or the scan is too long for the
        PandA counter.
    """
    panda_encoder = motor.panda_encoder
    inenc = panda.inenc[panda_encoder]  # type: ignore
    calc = panda.calc[panda_encoder]  # type: ignore

    reset_on_z = yield from bps.rd(inenc.rst_on_z)
    if reset_on_z == "1":
        raise ValueError(
            f"{panda.name} encoder resets on the index mark "
            f"(INENC{panda_encoder}.RST_ON_Z is {reset_on_z!r})"
            f"Turn RST_ON_Z off."
        )

    # Read the PandA encoder value and the EPICS motor position together
    encoder_res = yield from bps.rd(motor.encoder_resolution)
    current_motor_pos = yield from bps.rd(motor.user_readback)
    current_panda_encoder_value = yield from bps.rd(calc.out)
    current_panda_encoder_value = round(current_panda_encoder_value)

    max_velocity = yield from bps.rd(motor.max_velocity)
    acceleration_time = yield from bps.rd(motor.acceleration_time)

    def fly_info_models(current_encoder_value: int):
        return construct_fly_info_models(
            num_pulses=num_pulses,
            max_exposure_time=max_exposure_time,
            start_position=start,
            stop_position=stop,
            max_motor_velocity=max_velocity,
            encoder_resolution=encoder_res,
            current_position=current_motor_pos,
            current_encoder_value=current_encoder_value,
            acq_time_overhead=acq_time_overhead,
            time_based=time_based,
            position_dataset_name=position_dataset_name,
            position_dataset_units=position_dataset_units,
        )

    flyer_info, motor_info = fly_info_models(current_panda_encoder_value)

    move_extremes = (
        motor_info.ramp_up_start_pos(acceleration_time),
        motor_info.ramp_down_end_pos(acceleration_time),
    )

    def fits_counter_range(current_encoder_value: int) -> bool:
        return _fits_counter_range(
            current_encoder_value, current_motor_pos, encoder_res, move_extremes
        )

    if not fits_counter_range(current_panda_encoder_value):
        # To refuse the scan instead of re-zeroing the counter, use this instead:
        # raise ValueError(
        #     f"PandA counter is {current_panda_encoder_value}, and the scan would "
        #     f"take it past the 32 bit limit where it overflows. Re-zero the "
        #     f"encoder counter (INENC{panda_encoder}.SETP) and try again."
        # )
        logger.warning(
            "PandA counter is %d, and the scan would take it past the 32 bit "
            "limit where it overflows. Re-zeroing the encoder counter.",
            current_panda_encoder_value,
        )
        yield from bps.mv(inenc.setp, 0)
        current_panda_encoder_value = 0
        flyer_info, motor_info = fly_info_models(current_panda_encoder_value)
        if not fits_counter_range(current_panda_encoder_value):
            raise ValueError(
                f"Scan from {start} to {stop} is too long for the 32 bit PandA counter."
            )
    return flyer_info, motor_info


def single_axis_flyscan(
    detectors: list[AreaDetector],
    panda: HDFPanda,
    motor: FlyscanMotor,
    num_images: int = 1801,
    start: float = 0.0,
    stop: float = 180.0,
    stream_name: str = "tomo",
    acq_time_overhead: float = 0.001,
    time_based: bool = False,
):
    """Perform a single axis flyscan with the given detectors, PandABox, and motor.

    Parameters
    ----------
    detectors : list[AreaDetector]
        list of standard detectors to use in the scan
    panda : HDFPanda
        HDFPanda to use for triggering the detectors and motor
    motor : FlyscanMotor
        Flyable motor, such as a RotationMotor, to use for the scan
    num_images : int, optional
        Number of images to acquire, by default 1801
    start : float, optional
        Start motor position of the scan, by default 0.0
    stop : float, optional
        Stop motor position of the scan, by default 180.0
    stream_name : str, optional
        Name of the data stream, by default "tomo"
    acq_time_overhead : float, optional
        Overhead time for acquisition for each frame in seconds, by default 0.001
    flyscan_type : SingleAxisFlyscanType, default SingleAxisFlyscanType.POSITION_BASED
        Type of flyscan to perform, by default SingleAxisFlyscanType.POSITION_BASED
    """
    all_detectors = [*detectors, panda]

    # Construct ephemeral flyer for the single axis flyscan
    single_axis_panda_flyer = SingleAxisFlyableLogic(
        panda, motor.panda_encoder
    ).with_device()
    all_devices = [*all_detectors, single_axis_panda_flyer, motor]

    acquisition_periods = yield from get_detector_acq_times(detectors)
    max_acq_period = max(acquisition_periods)

    det_trigger_info = TriggerInfo(
        number_of_events=num_images,
        trigger=DetectorTrigger.EXTERNAL_EDGE,
    )

    panda_trigger_info = TriggerInfo(
        number_of_events=num_images,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
    )

    flyer_info, motor_info = yield from compute_flyscan_models(
        panda,
        motor,
        num_pulses=num_images,
        max_exposure_time=max_acq_period,
        start=start,
        stop=stop,
        acq_time_overhead=acq_time_overhead,
        time_based=time_based,
    )

    _md = {
        "detectors": [det.name for det in detectors],
        "num_points": num_images,
        "plan_name": "single_axis_flyscan",
        "hints": {},
    }
    yield from bps.open_run(md=_md)

    yield from bps.stage_all(*all_detectors)

    yield from bps.prepare(motor, motor_info, wait=True)

    # Configure position compare and arm the detectors only once the motor is at
    # its run-up position, so no triggers are produced on the way there.
    yield from bps.prepare(single_axis_panda_flyer, flyer_info, group="prepare")
    for det in detectors:
        yield from bps.prepare(det, det_trigger_info, group="prepare")
    yield from bps.wait(group="prepare")

    # Preparing the PandA arms PCAP, which enables position compare, so it must
    # come after position compare is configured.
    yield from bps.prepare(panda, panda_trigger_info, wait=True)

    yield from bps.declare_stream(*all_detectors, name=stream_name)

    yield from bps.kickoff_all(*all_devices, wait=True)

    flush_period = max(1, max_acq_period)
    yield from bps.collect_while_completing(
        all_devices,
        all_detectors,
        flush_period=flush_period,
        stream_name=stream_name,
    )
    yield from bps.unstage_all(*all_detectors)

    yield from bps.close_run()
