import pytest
from ophyd_async.core import init_devices

from xpd_tools.motors import RotationMotor, get_encoder_value_from_pos


@pytest.mark.parametrize(
    "position, encoder_resolution, encoder_pos_at_zero, expected",
    [
        (0.0, 10.0, 1000, 1000),
        (90.0, 10.0, 1000, 1009),
        (180.0, 10.0, 1000, 1018),
        (360.0, 10.0, 1000, 1036),
        (-90.0, 10.0, 1000, 991),
    ],
)
def test_get_encoder_value_from_pos(
    position: float, encoder_resolution: float, encoder_pos_at_zero: int, expected: int
):
    actual = get_encoder_value_from_pos(
        position, encoder_resolution, encoder_pos_at_zero
    )
    assert actual == expected, f"Expected {expected}, got {actual}"


@pytest.mark.parametrize(
    "encoder_resolution",
    (
        0.0009,  # the d-hutch spinner
        0.0001,
        0.01,
        1.0,
    ),
)
def test_rotation_motor_counts_per_rev(RE, encoder_resolution: float):
    """A revolution's worth of counts, times the size of a count, is 360 degrees.

    Asserted as that INVARIANT rather than as specific outputs, deliberately.
    encoder_resolution is the motor record's ERES - "Encoder Step Size (EGU)",
    the size of one count in degrees - so counts per revolution divides by it.
    Until 2026-09-21 this multiplied instead, which is dimensionally
    degrees-squared per count and truncates to 0 for any encoder finer than
    about 0.0028 deg/count, the spinner included. A test asserting output
    values would pass that formula happily; this one cannot, whatever the
    numbers are.

    The tolerance is one count, which is what int() truncation can cost.
    """
    with init_devices(mock=True):
        motor = RotationMotor("TEST:ROT:")

    counts_per_rev = motor.get_encoder_counts_per_rev(encoder_resolution)

    assert counts_per_rev > 0
    assert abs(counts_per_rev * encoder_resolution - 360.0) <= encoder_resolution
