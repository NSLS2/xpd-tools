import pytest

from xpd_tools.motors import RotationMotor


@pytest.mark.parametrize(
    "encoder_resolution, expected",
    [
        (0.0009, 400_000),
        (0.01, 36_000),
    ],
)
def test_get_encoder_counts_per_rev(encoder_resolution: float, expected: int):
    motor = RotationMotor("MOT:")
    assert motor.get_encoder_counts_per_rev(encoder_resolution) == expected


def test_panda_encoder_defaults_to_axis_1():
    assert RotationMotor("MOT:").panda_encoder == 1
    assert RotationMotor("MOT:", panda_encoder=3).panda_encoder == 3
