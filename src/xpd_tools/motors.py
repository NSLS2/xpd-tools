"""Additional motor classes and helper functions for XPD beamline at NSLS-II."""

import asyncio

from ophyd_async.core import (
    DeviceMock,
    callback_on_mock_put,
    default_mock_class,
    derived_signal_r,
    set_mock_put_proceeds,
    set_mock_value,
)
from ophyd_async.epics.core import epics_signal_r
from ophyd_async.epics.motor import Motor as AsyncEpicsMotor


# TODO: Get this upstreamed to ophyd_async and remove it from here.
# It is a general utility that is not specific to XPD.
class VelocityRespectingMotorMock(DeviceMock[AsyncEpicsMotor]):
    """Mock behaviour that respects motor velocity and acceleration time."""

    async def connect(self, device: AsyncEpicsMotor) -> None:
        """Mock signals to simulate a move respecting velocity and acceleration."""
        set_mock_value(device.velocity, 10)
        set_mock_value(device.max_velocity, 100)
        set_mock_value(device.acceleration_time, 1)

        # Motor starts in "done" state (not moving)
        set_mock_value(device.motor_done_move, 1)

        async def _do_move(target: float):
            current = await device.user_readback.get_value()
            velocity = await device.velocity.get_value()
            acceleration_time = await device.acceleration_time.get_value()
            move_time = abs(target - current) / velocity + 2 * acceleration_time
            set_mock_value(device.motor_done_move, 0)
            elapsed = 0.0
            while elapsed < move_time:
                await asyncio.sleep(min(1.0, move_time - elapsed))
                elapsed += 1.0
                fraction = min(elapsed / move_time, 1.0)
                position = current + (target - current) * fraction
                set_mock_value(device.user_readback, position)
            set_mock_value(device.user_readback, target)
            set_mock_value(device.motor_done_move, 1)
            set_mock_put_proceeds(device.user_setpoint, True)

        def _on_setpoint_write(value):
            set_mock_put_proceeds(device.user_setpoint, False)
            asyncio.ensure_future(_do_move(value))

        callback_on_mock_put(device.user_setpoint, _on_setpoint_write)


@default_mock_class(VelocityRespectingMotorMock)
class RotationMotor(AsyncEpicsMotor):
    """A motor that can be used for rotation scans.

    This class is a subclass of the AsyncEpicsMotor class and is used to represent
    a motor that can be used for rotation scans. It has additional attributes and
    methods that are specific to rotation scans.

    Parameters
    ----------
    prefix : str
        PV prefix of the motor record.
    name : str, optional
        Name of the device.
    panda_encoder : int, default 1
        PandA encoder axis this motor's encoder is wired to. Flyscans use the
        INENC, CALC and PCOMP blocks with this number, and the shared PULSE1
        block to drive the capture.
    """

    def __init__(self, prefix: str, name: str = "", *, panda_encoder: int = 1):
        super().__init__(prefix, name=name)
        self.panda_encoder = panda_encoder
        self.encoder_counts_per_rev = derived_signal_r(
            self.get_encoder_counts_per_rev,
            derived_units="counts",
            derived_precision=0,
            encoder_resolution=self.encoder_resolution,
        )
        self.encoder_counts = epics_signal_r(int, prefix + ".REP")

    def get_encoder_counts_per_rev(self, encoder_resolution: float) -> int:
        """Calculate the number of encoder counts per revolution.

        Parameters
        ----------
        encoder_resolution : float
            The resolution of the encoder in degrees per count.

        Returns
        -------
        int
            The number of encoder counts per revolution.
        """
        return round(360.0 / encoder_resolution)
