import pytest
from ophyd_async.core import FlyMotorInfo

from xpd_tools.flyers import (
    PandaPcompDirection,
    SingleAxisFlyscanInfo,
    calculate_move_time_for_flyscan,
    construct_fly_info_models,
    get_zero_encoder_position,
)


@pytest.mark.parametrize(
    "travel_distance, max_motor_velocity, num_images, acquire_period, expected_time",
    [
        (10.0, 2.0, 3, 0.2001, 5.0),
        (5.0, 1.0, 2, 0.1002, 5.0),
        (20.0, 5.0, 3, 0.3003, 4.0),
        (15.0, 3.0, 2, 0.25004, 5.0),
        (8.0, 4.0, 10, 0.5005, 5.005),
    ],
)
def test_calculate_move_time_for_flyscan(
    travel_distance: float,
    max_motor_velocity: float,
    num_images: int,
    acquire_period: float,
    expected_time: float,
):
    result = calculate_move_time_for_flyscan(
        travel_distance,
        max_motor_velocity,
        num_images,
        acquire_period,
    )
    assert pytest.approx(result, rel=1e-3) == expected_time


@pytest.mark.parametrize(
    "num_pulses, max_exposure_time, start_position,"
    " stop_position, encoder_resolution, max_motor_velocity,"
    " encoder_pos_at_zero, time_based,"
    " expected_flyer_info, expected_motor_info",
    [
        (
            11,
            0.1,
            0.0,
            100.0,
            0.1,
            50.0,
            0,
            False,
            SingleAxisFlyscanInfo(
                start=0,
                num_pulses=11,
                direction=PandaPcompDirection.POSITIVE,
                pulse_width=1,
                pulse_step=100,
                time_based=False,
                position_scale=0.1,
                position_offset=0.0,
            ),
            FlyMotorInfo(start_position=0.0, end_position=100.0, time_for_move=2.0),
        ),
        (
            5,
            0.2,
            90.0,
            0.0,
            0.1,
            30.0,
            0,
            False,
            SingleAxisFlyscanInfo(
                start=900,
                num_pulses=5,
                direction=PandaPcompDirection.NEGATIVE,
                pulse_width=1,
                pulse_step=225,
                time_based=False,
                position_scale=0.1,
                position_offset=0.0,
            ),
            FlyMotorInfo(start_position=90.0, end_position=0.0, time_for_move=3.0),
        ),
        (
            11,
            0.1,
            0.0,
            50.0,
            10.0,
            25.0,
            0,
            True,
            SingleAxisFlyscanInfo(
                start=0,
                num_pulses=11,
                direction=PandaPcompDirection.POSITIVE,
                pulse_width=0.101,
                pulse_step=0.2,
                time_based=True,
                position_scale=10.0,
                position_offset=0.0,
            ),
            FlyMotorInfo(start_position=0.0, end_position=50.0, time_for_move=2.0),
        ),
        (
            1801,
            0.05,
            0.0,
            180.0,
            360 / 70000,
            60.0,
            39240,
            True,
            SingleAxisFlyscanInfo(
                start=39240,
                num_pulses=1801,
                direction=PandaPcompDirection.POSITIVE,
                pulse_width=0.051,
                pulse_step=0.0510283,
                time_based=True,
                position_scale=360 / 70000,
                position_offset=-39240 * (360 / 70000),
            ),
            FlyMotorInfo(start_position=0.0, end_position=180.0, time_for_move=91.851),
        ),
    ],
)
def test_construct_fly_info_models(
    num_pulses: int,
    max_exposure_time: float,
    start_position: float,
    stop_position: float,
    encoder_resolution: float,
    max_motor_velocity: float,
    encoder_pos_at_zero: int,
    time_based: bool,
    expected_flyer_info: SingleAxisFlyscanInfo,
    expected_motor_info: FlyMotorInfo,
):
    flyer_info, motor_info = construct_fly_info_models(
        num_pulses,
        max_exposure_time,
        start_position,
        stop_position,
        encoder_resolution,
        max_motor_velocity,
        encoder_pos_at_zero,
        time_based=time_based,
    )
    assert flyer_info.start == expected_flyer_info.start
    assert flyer_info.num_pulses == expected_flyer_info.num_pulses
    assert flyer_info.direction == expected_flyer_info.direction
    assert flyer_info.pulse_width == pytest.approx(expected_flyer_info.pulse_width)
    assert flyer_info.pulse_step == pytest.approx(expected_flyer_info.pulse_step)
    assert flyer_info.time_based == expected_flyer_info.time_based
    assert flyer_info.position_scale == pytest.approx(
        expected_flyer_info.position_scale
    )
    assert flyer_info.position_offset == pytest.approx(
        expected_flyer_info.position_offset
    )
    assert motor_info.start_position == pytest.approx(
        expected_motor_info.start_position
    )
    assert motor_info.end_position == pytest.approx(expected_motor_info.end_position)
    assert motor_info.time_for_move == pytest.approx(expected_motor_info.time_for_move)


@pytest.mark.parametrize(
    "num_pulses, max_exposure_time, start_position,"
    " stop_position, encoder_resolution, max_motor_velocity,"
    " encoder_pos_at_zero, expected_match",
    [
        # travel_counts=1000, num_pulses-1=6 -> 1000 % 6 != 0
        (7, 0.1, 0.0, 100.0, 0.1, 50.0, 0, "not evenly divisible"),
        # travel_counts=10, num_pulses-1=10 -> 10 < 10*2=20
        (11, 0.1, 0.0, 1.0, 0.1, 50.0, 0, "less than the minimum required"),
    ],
)
def test_construct_fly_info_models_raises(
    num_pulses: int,
    max_exposure_time: float,
    start_position: float,
    stop_position: float,
    encoder_resolution: float,
    max_motor_velocity: float,
    encoder_pos_at_zero: int,
    expected_match: str,
):
    with pytest.raises(ValueError, match=expected_match):
        construct_fly_info_models(
            num_pulses,
            max_exposure_time,
            start_position,
            stop_position,
            encoder_resolution,
            max_motor_velocity,
            encoder_pos_at_zero,
        )


@pytest.mark.parametrize(
    "current_position, start_position, encoder_resolution, current_encoder_value,"
    "expected_zero_encoder_position",
    [
        (10.0, 0.0, 0.1, 100, 0),
        (5.0, 2.0, 0.2, 50, 35),
        (20.0, 10.0, 0.5, 200, 180),
        (15.0, 5.0, 0.1, 150, 50),
        (8.0, 4.0, 0.2, 80, 60),
        (183, 0.0, 0.0009, 198353, -4980),
    ],
)
def test_get_zero_encoder_position(
    current_position: float,
    start_position: float,
    encoder_resolution: float,
    current_encoder_value: int,
    expected_zero_encoder_position: int,
):
    zero_encoder_position = get_zero_encoder_position(
        current_position=current_position,
        start_position=start_position,
        encoder_resolution=encoder_resolution,
        current_encoder_value=current_encoder_value,
    )
    assert zero_encoder_position == expected_zero_encoder_position


# Encoder scale and anchor used by the composition test below. The resolution is
# the d-hutch spinner's, in degrees per count.
ENCODER_RESOLUTION = 0.0009
ENCODER_COUNT_AT_ZERO = 1_000_000
RUN_UP_DEGREES = 18.0


def _encoder_count_at(position: float) -> int:
    """Ground truth: what the encoder reads at a given motor position."""
    return int(position / ENCODER_RESOLUTION + ENCODER_COUNT_AT_ZERO)


@pytest.mark.parametrize(
    "start_position, stop_position",
    [
        (0.0, 360.0),  # ascending - passed before this test existed
        (360.0, 0.0),  # descending - the case that fails at the beamline
    ],
)
def test_flyscan_arms_pcomp_where_the_encoder_actually_is(
    start_position: float, stop_position: float
):
    """The position compare must be armed at the encoder count of the scan start.

    This composes get_zero_encoder_position with construct_fly_info_models the way
    single_axis_flyscan does. Each is correct in isolation and each has its own
    passing unit test; only in composition is the start offset applied twice, which
    arms the pcomp at a count the axis never reaches and means no trigger ever fires.

    Asserted against the swept range as well as the exact count, because "off by the
    start offset" is only a bug when it puts the trigger outside the travel.
    """
    # The plan reads these live, with the motor parked at the scan start.
    anchor = get_zero_encoder_position(
        current_position=start_position,
        start_position=start_position,
        encoder_resolution=ENCODER_RESOLUTION,
        current_encoder_value=_encoder_count_at(start_position),
    )
    assert anchor == ENCODER_COUNT_AT_ZERO, (
        "the anchor must be the encoder count at ZERO degrees, not at the scan start"
    )

    flyer_info, _ = construct_fly_info_models(
        num_pulses=101,
        max_exposure_time=0.004,
        start_position=start_position,
        stop_position=stop_position,
        encoder_resolution=ENCODER_RESOLUTION,
        max_motor_velocity=147.0,
        encoder_pos_at_zero=anchor,
    )

    assert flyer_info.start == _encoder_count_at(start_position)

    low, high = sorted(
        (_encoder_count_at(start_position), _encoder_count_at(stop_position))
    )
    run_up = int(RUN_UP_DEGREES / ENCODER_RESOLUTION)
    assert low - run_up <= flyer_info.start <= high + run_up, (
        f"pcomp armed at {flyer_info.start}, outside the swept encoder range "
        f"[{low - run_up}..{high + run_up}] - no trigger can ever fire"
    )
