"""Psylotech load frame devices for XPD beamline at NSLS-II.

The Psylotech IOC exposes a rotation axis, an axial (load cell) actuator and the
load cell readback. The rotation axis is a flyable motor for use with
:func:`xpd_tools.plans.single_axis_flyscan`; the axial actuator is a plain motor,
and :class:`PsylotechLoad` moves the axial actuator until a requested load is
reached.
"""

import asyncio
from dataclasses import dataclass
from functools import cached_property
from math import isclose

from bluesky.protocols import Location
from ophyd_async.core import (
    CALCULATE_TIMEOUT,
    DEFAULT_TIMEOUT,
    AsyncStatus,
    DeviceMock,
    EnabledDisabled,
    EnableDisable,
    FlyableLogic,
    FlyMotorInfo,
    MovableLogic,
    SignalR,
    SignalRW,
    SignalW,
    StandardFlyable,
    StandardMovable,
    StandardReadable,
    StrictEnum,
    TimeoutCalculator,
    callback_on_mock_put,
    default_mock_class,
    derived_signal_r,
    error_if_none,
    set_mock_value,
    soft_signal_r_and_setter,
    soft_signal_rw,
    wait_for_value,
)
from ophyd_async.core import StandardReadableFormat as Format
from ophyd_async.epics.core import epics_signal_r, epics_signal_rw, epics_signal_w
from ophyd_async.epics.motor import MotorFlyCtx, MotorLimitsError

RECONNECT_MESSAGE = (
    "Psylotech controller is not connected. Click the button in LabView to "
    "reconnect the TCP/IP link, or call staff."
)

#: Time allowed for the actuators to report enabled after they are asked to power up.
ACTUATOR_ENABLE_TIMEOUT = 5.0


class ConnectionState(StrictEnum):
    """State of the TCP/IP link between the IOC and the Psylotech controller."""

    CONNECTED = "Connected"
    DISCONNECTED = "Disconnected"


class PsylotechError(RuntimeError):
    """The Psylotech controller is not in a state where it can move."""


@dataclass(frozen=True)
class PsylotechSignals:
    """Signals shared by every axis, owned by the :class:`Psylotech` device.

    Each axis checks these before and around its moves but doesn't own them.
    """

    actuator_enable: SignalW[EnableDisable]
    actuator_enabled: SignalR[EnabledDisabled]
    connected: SignalR[ConnectionState]


@dataclass
class PsylotechMovableLogic(MovableLogic[float]):
    """Move logic for a Psylotech axis.

    The IOC only has an absolute move and a readback. Every move powers the
    actuators up, writes the setpoint and waits for the readback to get within
    ``tolerance``, then powers the actuators back down. A stop re-targets a moving
    axis at its current readback and powers the actuators down.
    """

    connected: SignalR[ConnectionState]
    actuator_enable: SignalW[EnableDisable]
    actuator_enabled: SignalR[EnabledDisabled]
    #: Jog rate of the axis, in axis units per second.
    velocity: SignalRW[float]
    #: Fastest jog rate the axis may be given, in axis units per second.
    max_velocity: SignalR[float]
    #: Distance from the setpoint the readback may be at for a move to be done.
    tolerance: float
    #: Time taken to reach the jog rate, in seconds.
    acceleration_time: float

    async def enable_actuators(self) -> None:
        """Power the actuators and wait for them to report enabled."""
        await self.actuator_enable.set(EnableDisable.ENABLE)
        await wait_for_value(
            self.actuator_enabled,
            EnabledDisabled.ENABLED,
            timeout=ACTUATOR_ENABLE_TIMEOUT,
        )

    async def disable_actuators(self) -> None:
        await self.actuator_enable.set(EnableDisable.DISABLE)

    async def stop(self) -> None:
        """Stop the axis, but only if it is moving.

        Stage and unstage also call this, so it must not touch an idle axis,
        except to power down actuators a prepared fly scan left enabled.
        """
        setpoint, readback = await asyncio.gather(
            self.setpoint.get_value(), self.readback.get_value()
        )
        if not isclose(setpoint, readback, rel_tol=0.0, abs_tol=self.tolerance):
            await self.setpoint.set(readback)
            await self.disable_actuators()
        elif await self.actuator_enabled.get_value() == EnabledDisabled.ENABLED:
            # Left powered by a fly scan that was prepared but never finished
            await self.disable_actuators()

    async def check_move(self, new_position: float) -> None:
        if await self.connected.get_value() != ConnectionState.CONNECTED:
            raise PsylotechError(RECONNECT_MESSAGE)
        velocity, max_velocity = await asyncio.gather(
            self.velocity.get_value(), self.max_velocity.get_value()
        )
        if velocity > max_velocity:
            raise PsylotechError(
                f"{self.readback.name} jog rate {velocity} is above the maximum of "
                f"{max_velocity}."
            )

    async def calculate_timeout(
        self, old_position: float, new_position: float
    ) -> float:
        velocity = await self.velocity.get_value()
        try:
            return (
                abs((new_position - old_position) / velocity)
                + 2 * self.acceleration_time
                + DEFAULT_TIMEOUT
            )
        except ZeroDivisionError as error:
            msg = f"{self.readback.name} has a jog rate of zero."
            raise ValueError(msg) from error

    async def go_to(self, new_position: float, timeout: TimeoutCalculator) -> None:
        """Write the setpoint and wait for the readback to reach it.

        The actuators must already be powered.
        """
        await self.setpoint.set(new_position)
        await wait_for_value(
            self.readback,
            lambda value: isclose(
                value, new_position, rel_tol=0.0, abs_tol=self.tolerance
            ),
            timeout=timeout(),
        )

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        try:
            await self.enable_actuators()
            await self.go_to(new_position, timeout)
        finally:
            await self.disable_actuators()


@dataclass
class PsylotechAxialLogic(PsylotechMovableLogic):
    """Move logic for the axial actuator, which has limits to its travel."""

    high_limit: SignalR[float]
    low_limit: SignalR[float]

    async def check_move(self, new_position: float) -> None:
        await super().check_move(new_position)
        high, low = await asyncio.gather(
            self.high_limit.get_value(), self.low_limit.get_value()
        )
        if not low <= new_position <= high:
            raise PsylotechError(
                f"{self.readback.name} position {new_position} is outside the "
                f"travel limits {low} to {high}."
            )


@dataclass
class PsylotechRotationLogic(
    PsylotechMovableLogic, FlyableLogic[FlyMotorInfo, MotorFlyCtx]
):
    """Combined move + fly logic for the Psylotech rotation axis."""

    async def on_prepare(self, value: FlyMotorInfo) -> MotorFlyCtx:
        """Move to the beginning of the run-up distance ready for a fly scan."""
        max_velocity = await self.max_velocity.get_value()
        if value.speed > max_velocity:
            raise MotorLimitsError(
                f"Speed {value.speed} deg/s was requested for {self.readback.name} "
                f"with max speed of {max_velocity} deg/s."
            )
        ramp_up_start_pos = value.ramp_up_start_pos(self.acceleration_time)
        ramp_down_end_pos = value.ramp_down_end_pos(self.acceleration_time)
        await asyncio.gather(
            self.check_move(ramp_up_start_pos), self.check_move(ramp_down_end_pos)
        )
        # Move to the run-up start at maximum velocity. The actuators stay powered
        # from here until the fly move is complete, so kickoff starts at once.
        try:
            await self.velocity.set(max_velocity)
            old_position = await self.readback.get_value()
            timeout = await self.calculate_timeout(old_position, ramp_up_start_pos)
            await self.enable_actuators()
            await self.go_to(ramp_up_start_pos, lambda: timeout)
            # Set the velocity we will use for the fly scan
            await self.velocity.set(value.speed)
        except BaseException:
            await self.disable_actuators()
            raise
        return MotorFlyCtx(fly_info=value)

    async def _fly_move(self, target: float, timeout: float) -> None:
        """Run the fly move with the actuators already powered, then power down."""
        try:
            await self.check_move(target)
            await self.go_to(target, lambda: timeout)
        finally:
            await self.disable_actuators()

    async def on_kickoff(self, ctx: MotorFlyCtx) -> MotorFlyCtx:
        """Begin moving the axis from the prepared position to the final position."""
        target = ctx.fly_info.ramp_down_end_pos(self.acceleration_time)
        try:
            # The fly move bypasses the move checks, so repeat them here: the
            # controller may have disconnected since prepare succeeded.
            await self.check_move(target)
            if await self.actuator_enabled.get_value() != EnabledDisabled.ENABLED:
                await self.enable_actuators()
            if ctx.fly_info.timeout == CALCULATE_TIMEOUT:
                initial = await self.readback.get_value()
                timeout = await self.calculate_timeout(initial, target)
            else:
                timeout = ctx.fly_info.timeout
        except BaseException:
            await self.disable_actuators()
            raise
        ctx.status = AsyncStatus(self._fly_move(target, timeout))  # pyright: ignore[reportArgumentType]
        # Wait out the run-up so the axis is at constant velocity before kickoff()
        # returns, as ophyd_async's Motor does.
        await asyncio.sleep(self.acceleration_time)
        return ctx

    async def on_complete(self, ctx: MotorFlyCtx) -> None:
        """Block until the axis reaches the fly-scan end position."""
        status = error_if_none(
            ctx.status, f"kickoff for {self.readback.name} not called."
        )
        await status


class PsylotechMock(DeviceMock["Psylotech"]):
    """Mock that reports the controller connected and the actuators as asked."""

    async def connect(self, device: "Psylotech") -> None:
        set_mock_value(device.connected, ConnectionState.CONNECTED)
        set_mock_value(device.actuator_enabled, EnabledDisabled.DISABLED)

        def _enable(value: EnableDisable):
            set_mock_value(
                device.actuator_enabled,
                EnabledDisabled.ENABLED
                if value == EnableDisable.ENABLE
                else EnabledDisabled.DISABLED,
            )

        callback_on_mock_put(device.actuator_enable, _enable)


class PsylotechAxisMock(DeviceMock["_PsylotechAxis"]):
    """Mock that moves instantly."""

    async def connect(self, device: "_PsylotechAxis") -> None:
        set_mock_value(device.velocity, device.MAX_VELOCITY)
        callback_on_mock_put(
            device.user_setpoint,
            lambda value: set_mock_value(device.user_readback, value),
        )


@default_mock_class(PsylotechAxisMock)
class _PsylotechAxis(StandardMovable[float], StandardReadable):
    """Signals shared by the Psylotech axes."""

    #: Distance from the setpoint the readback may be at for a move to be done.
    POSITION_TOLERANCE: float
    #: Fastest jog rate, in the axis units per second. Will become a PV.
    MAX_VELOCITY: float
    VELOCITY_UNITS: str
    #: Time taken to reach the jog rate, in seconds.
    ACCELERATION_TIME: float

    # Created by the subclasses, which differ in their PVs
    user_setpoint: SignalRW[float]
    user_readback: SignalR[float]

    def __init__(
        self, prefix: str, shared: PsylotechSignals, jog_rate_pv: str, name: str = ""
    ):
        # Not a Device, so the parent keeps ownership of the shared signals
        self._shared = shared
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.velocity = epics_signal_rw(float, prefix + jog_rate_pv)
            self.max_velocity, _ = soft_signal_r_and_setter(
                float, self.MAX_VELOCITY, units=self.VELOCITY_UNITS
            )
        super().__init__(name)

    async def locate(self) -> Location[float]:
        """Report where the axis is, for relative moves.

        The setpoint PV holds the last commanded position, which is stale after a
        stop or a move from elsewhere, so relative moves start from the readback.
        """
        readback = await self.user_readback.get_value()
        return Location(setpoint=readback, readback=readback)

    def _movable_logic_args(self) -> dict:
        return {
            "setpoint": self.user_setpoint,
            "readback": self.user_readback,
            "connected": self._shared.connected,
            "actuator_enable": self._shared.actuator_enable,
            "actuator_enabled": self._shared.actuator_enabled,
            "velocity": self.velocity,
            "max_velocity": self.max_velocity,
            "tolerance": self.POSITION_TOLERANCE,
            "acceleration_time": self.ACCELERATION_TIME,
        }


class PsylotechRotation(_PsylotechAxis, StandardFlyable[FlyMotorInfo, MotorFlyCtx]):
    """The Psylotech rotation axis.

    It provides the same signals as an EPICS motor record that
    :func:`xpd_tools.plans.single_axis_flyscan` reads, with values that don't come
    from the IOC held as literals on the class.

    Parameters
    ----------
    prefix : str
        PV prefix of the Psylotech IOC, e.g. ``XF:28ID2-ES{Psylotech:1}``.
    shared : PsylotechSignals
        Signals shared by all of the axes.
    name : str, optional
        Name of the device.
    panda_encoder : int, default 1
        PandA encoder axis the rotation encoder is wired to.
    """

    # TODO: Confirm these against the Psylotech controller.
    #: Rotation encoder counts per revolution.
    ENCODER_COUNTS_PER_REV = 288000
    POSITION_TOLERANCE = 0.01
    MAX_VELOCITY = 3600
    VELOCITY_UNITS = "deg/s"
    ACCELERATION_TIME = 0.5

    def __init__(
        self,
        prefix: str,
        shared: PsylotechSignals,
        name: str = "",
        *,
        panda_encoder: int = 1,
    ):
        self.panda_encoder = panda_encoder
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.user_readback = epics_signal_r(float, prefix + "RotationA2Angle_RBV")
        self.user_setpoint = epics_signal_rw(
            float, prefix + "RotationMoveAbsolute", wait=False
        )

        # Soft signals with the names single_axis_flyscan reads from a motor record
        self.encoder_counts_per_rev, _ = soft_signal_r_and_setter(
            int, self.ENCODER_COUNTS_PER_REV, units="counts"
        )
        self.encoder_resolution, _ = soft_signal_r_and_setter(
            float, 360.0 / self.ENCODER_COUNTS_PER_REV, units="deg/count"
        )
        self.acceleration_time, _ = soft_signal_r_and_setter(
            float, self.ACCELERATION_TIME, units="s"
        )
        super().__init__(prefix, shared, "RotationJogRate", name)

    @cached_property
    def _logic(self) -> PsylotechRotationLogic:
        """The combined move + fly logic, shared by movable_logic and flyable_logic."""
        return PsylotechRotationLogic(**self._movable_logic_args())

    @cached_property
    def movable_logic(self) -> PsylotechRotationLogic:
        return self._logic

    @cached_property
    def flyable_logic(self) -> PsylotechRotationLogic:
        return self._logic


class PsylotechAxial(_PsylotechAxis):
    """The Psylotech axial actuator, which moves the load cell.

    Parameters
    ----------
    prefix : str
        PV prefix of the Psylotech IOC, e.g. ``XF:28ID2-ES{Psylotech:1}``.
    shared : PsylotechSignals
        Signals shared by all of the axes.
    name : str, optional
        Name of the device.
    panda_encoder : int, default 2
        PandA encoder axis the axial encoder is wired to.
    """

    # TODO: Confirm these against the Psylotech controller.
    # The IOC currently reports the axial actuator in meters, not mm.
    #: Axial encoder counts per mm.
    ENCODER_COUNTS_PER_MM = 720000 * 4
    #: m
    POSITION_TOLERANCE = 1e-6
    #: m/s
    MAX_VELOCITY = 1e-3
    VELOCITY_UNITS = "m/s"
    ACCELERATION_TIME = 0.1
    #: Travel limits in m. These will become PVs.
    HIGH_LIMIT = 0.025
    LOW_LIMIT = -0.025

    def __init__(
        self,
        prefix: str,
        shared: PsylotechSignals,
        name: str = "",
        *,
        panda_encoder: int = 2,
    ):
        self.panda_encoder = panda_encoder
        with self.add_children_as_readables(Format.HINTED_SIGNAL):
            self.user_readback = epics_signal_r(float, prefix + "AxialPosition_RBV")
        self.user_setpoint = epics_signal_rw(
            float, prefix + "AxialMoveAbsolute", wait=False
        )
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.high_limit_travel, _ = soft_signal_r_and_setter(
                float, self.HIGH_LIMIT, units="m"
            )
            self.low_limit_travel, _ = soft_signal_r_and_setter(
                float, self.LOW_LIMIT, units="m"
            )
        super().__init__(prefix, shared, "AxialJogRate", name)

    @cached_property
    def movable_logic(self) -> PsylotechAxialLogic:
        return PsylotechAxialLogic(
            **self._movable_logic_args(),
            high_limit=self.high_limit_travel,
            low_limit=self.low_limit_travel,
        )


def _in_tolerance(
    readback: SignalR[float], target: SignalR[float], tolerance: SignalR[float]
) -> SignalR[bool]:
    """Report whether `readback` is within `tolerance` of `target`."""

    def in_tolerance(readback: float, target: float, tolerance: float) -> bool:
        return isclose(readback, target, rel_tol=0.0, abs_tol=tolerance)

    return derived_signal_r(
        in_tolerance, readback=readback, target=target, tolerance=tolerance
    )


@dataclass
class PsylotechLoadLogic(MovableLogic[float]):
    """Move the axial actuator until the load cell settles at the requested load.

    The load can be in tension (positive) or compression (negative), so there is a
    limit on each side. The axial actuator is stepped towards the target a little
    at a time, with the actuators powered for the whole move. The ratio of load
    change to position change is learnt from the steps taken, so it works whichever
    way the load changes with position, and is kept through a stretch where the
    load doesn't respond, such as when the load passes through zero. The first step
    is the small ``probe_step``, which is repeated until the load responds to it.
    Every step after that is sized to cover the remaining load error but never
    more than ``max_step``, so a lower ``max_step`` takes more, gentler steps. The
    move is done once `in_tolerance` has held for ``settle_time`` seconds.
    """

    axial: PsylotechAxialLogic
    in_tolerance: SignalR[bool]
    settle_time: SignalR[float]
    #: Highest load, in tension, that may be requested or reached.
    high_limit: SignalR[float]
    #: Lowest load, in compression, that may be requested or reached.
    low_limit: SignalR[float]
    #: Axial step used until the load response to a step is known.
    probe_step: SignalR[float]
    #: Axial direction (+1 or -1) that increases the load, used until it is known.
    load_direction: float
    #: Largest axial step in a single move.
    max_step: SignalR[float]
    #: Smallest load change that is taken as a response to a step.
    noise_floor: float
    #: A load change against the direction being driven bigger than this is a break.
    break_drop: float
    #: Loads below this are not judged for slipping, as the grips take up slack.
    slip_min_load: float
    #: A step whose load response is below this fraction of expected is slipping.
    slip_fraction: float
    #: Number of slipping steps in a row that stop the move.
    slip_steps: int
    #: Longest the whole move may take, in seconds.
    move_timeout: float | None

    async def stop(self) -> None:
        await self.axial.stop()

    async def _check_load(self, load: float, what: str) -> None:
        high, low = await asyncio.gather(
            self.high_limit.get_value(), self.low_limit.get_value()
        )
        if not low <= load <= high:
            raise PsylotechError(
                f"{what} {load} is outside the load limits {low} to {high}."
            )

    async def check_move(self, new_position: float) -> None:
        await self._check_load(new_position, "Requested load")
        await self.axial.check_move(await self.axial.readback.get_value())

    async def calculate_timeout(
        self, old_position: float, new_position: float
    ) -> float | None:
        return self.move_timeout

    async def _stays_in_tolerance(self, settle_time: float) -> bool:
        """Report whether `in_tolerance` holds for the whole of `settle_time`."""
        try:
            await wait_for_value(self.in_tolerance, False, timeout=settle_time)
        except TimeoutError:
            return True
        return False

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        """Step the axial actuator until the load settles at `new_position`."""
        settle_time, probe_step, max_step = await asyncio.gather(
            self.settle_time.get_value(),
            self.probe_step.get_value(),
            self.max_step.get_value(),
        )
        if not (0 < probe_step < float("inf") and 0 < max_step < float("inf")):
            raise ValueError(
                "probe_step and max_step must be finite and greater than zero."
            )
        # Target first, so in_tolerance judges the new target before anything moves.
        await self.setpoint.set(new_position)

        gain: float | None = None  # load change per axial position change
        # (position, load, direction the load was driven) before a step
        previous: tuple[float, float, float] | None = None
        slipping = 0  # steps in a row where the load hardly responded
        try:
            await self.axial.enable_actuators()
            async with asyncio.timeout(timeout()):
                while True:
                    if await self.in_tolerance.get_value():
                        await self._check_load(await self.readback.get_value(), "Load")
                        previous = None
                        slipping = 0
                        if await self._stays_in_tolerance(settle_time):
                            await self._check_load(
                                await self.readback.get_value(), "Load"
                            )
                            return
                        continue

                    load, position = await asyncio.gather(
                        self.readback.get_value(), self.axial.readback.get_value()
                    )
                    await self._check_load(load, "Load")
                    error = new_position - load
                    if previous is not None:
                        position_change = position - previous[0]
                        load_change = load - previous[1]
                        if load_change * previous[2] < -self.break_drop:
                            raise PsylotechError(
                                f"Load fell by {abs(load_change)} while it was being "
                                f"driven the other way, from {previous[1]} to {load}. "
                                f"The sample may have broken/slipped."
                            )
                        if (
                            gain is not None
                            and abs(previous[1]) > self.slip_min_load
                            and abs(load_change)
                            < self.slip_fraction * abs(gain * position_change)
                        ):
                            slipping += 1
                            if slipping >= self.slip_steps:
                                raise PsylotechError(
                                    f"Load stopped responding to the axial actuator "
                                    f"at {load} after {slipping} steps. The sample "
                                    f"may be slipping in the grips."
                                )
                        else:
                            slipping = 0
                        if position_change and abs(load_change) > self.noise_floor:
                            gain = load_change / position_change

                    if gain is None:
                        direction = 1.0 if error > 0 else -1.0
                        step = direction * self.load_direction * probe_step
                    else:
                        step = max(-max_step, min(max_step, error / gain))
                    previous = (position, load, 1.0 if error > 0 else -1.0)
                    await self.axial.check_move(position + step)
                    step_timeout = await self.axial.calculate_timeout(
                        position, position + step
                    )
                    await self.axial.go_to(
                        position + step, lambda timeout=step_timeout: timeout
                    )
                    # Give the load time to relax before judging the step
                    await asyncio.sleep(settle_time)
        finally:
            await self.axial.disable_actuators()


class PsylotechLoadMock(DeviceMock["PsylotechLoad"]):
    """Mock that leaves the load readback to the axial actuator, not the setpoint."""

    async def connect(self, device: "PsylotechLoad") -> None:
        pass


@default_mock_class(PsylotechLoadMock)
class PsylotechLoad(StandardMovable[float], StandardReadable):
    """Move the Psylotech load frame to a load.

    Setting this device moves the axial actuator slowly while monitoring the load
    cell until the load settles at the requested value. Tension is positive and
    compression negative. ``tolerance`` and ``settle_time`` are signals that can be
    set, and ``in_tolerance`` can be read at any time to see whether the load is
    currently at the target.

    Parameters
    ----------
    prefix : str
        PV prefix of the Psylotech IOC, e.g. ``XF:28ID2-ES{Psylotech:1}``.
    shared : PsylotechSignals
        Signals shared by all of the axes.
    name : str, optional
        Name of the device.
    panda_encoder : int, default 3
        PandA encoder axis the axial encoder is wired to.
    """

    # TODO: Confirm these against the Psylotech controller.
    #: Load the move is done within.
    LOAD_TOLERANCE = 1.0
    #: Load limits, in tension and in compression. These will become PVs.
    HIGH_LIMIT = 4_000.0
    LOW_LIMIT = -4_000.0
    #: s
    SETTLE_TIME = 2.0
    #: m
    PROBE_STEP = 0.000005
    #: Axial direction that increases the load: +1 if moving up puts it in tension.
    LOAD_DIRECTION = 1.0
    #: m
    MAX_STEP = 0.00002
    NOISE_FLOOR = 1
    #: A load change against the direction being driven this big is a break.
    BREAK_LOAD_DROP = 100.0
    #: Loads below this are not judged for slipping.
    SLIP_MIN_LOAD = 50.0
    #: A step giving less than this fraction of the expected load change is slipping.
    SLIP_RESPONSE_FRACTION = 0.2
    #: Number of slipping steps in a row that stop the move.
    SLIP_STEPS = 3
    #: s, for the whole move. Each step also has its own timeout.
    MOVE_TIMEOUT = 300.0

    def __init__(
        self,
        prefix: str,
        shared: PsylotechSignals,
        name: str = "",
        *,
        panda_encoder: int = 3,
    ):
        with self.add_children_as_readables():
            self.axial = PsylotechAxial(
                prefix, shared, name="axial", panda_encoder=panda_encoder
            )
        with self.add_children_as_readables(Format.HINTED_SIGNAL):
            self.load = epics_signal_r(float, prefix + "LoadCellFullScale_RBV")
        self.target_load: SignalRW[float] = soft_signal_rw(float, 0.0)
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.tolerance = soft_signal_rw(float, self.LOAD_TOLERANCE)
            self.settle_time = soft_signal_rw(float, self.SETTLE_TIME, units="s")
            self.high_limit, _ = soft_signal_r_and_setter(float, self.HIGH_LIMIT)
            self.low_limit, _ = soft_signal_r_and_setter(float, self.LOW_LIMIT)
            self.probe_step = soft_signal_rw(float, self.PROBE_STEP, units="m")
            self.max_step = soft_signal_rw(float, self.MAX_STEP, units="m")
        self.in_tolerance = _in_tolerance(self.load, self.target_load, self.tolerance)
        super().__init__(name)

    async def locate(self) -> Location[float]:
        """Report the load, for relative moves, rather than the last target."""
        load = await self.load.get_value()
        return Location(setpoint=load, readback=load)

    @cached_property
    def movable_logic(self) -> PsylotechLoadLogic:
        return PsylotechLoadLogic(
            setpoint=self.target_load,
            readback=self.load,
            axial=self.axial.movable_logic,
            in_tolerance=self.in_tolerance,
            settle_time=self.settle_time,
            high_limit=self.high_limit,
            low_limit=self.low_limit,
            probe_step=self.probe_step,
            load_direction=self.LOAD_DIRECTION,
            max_step=self.max_step,
            noise_floor=self.NOISE_FLOOR,
            break_drop=self.BREAK_LOAD_DROP,
            slip_min_load=self.SLIP_MIN_LOAD,
            slip_fraction=self.SLIP_RESPONSE_FRACTION,
            slip_steps=self.SLIP_STEPS,
            move_timeout=self.MOVE_TIMEOUT,
        )


@default_mock_class(PsylotechMock)
class Psylotech(StandardReadable):
    """The Psylotech load frame.

    Owns the signals shared by every axis: the actuator power and the link to the
    controller. The axes check them before and around each move.

    Parameters
    ----------
    prefix : str
        PV prefix of the Psylotech IOC, e.g. ``XF:28ID2-ES{Psylotech:1}``.
    name : str, optional
        Name of the device.
    """

    def __init__(self, prefix: str, name: str = ""):
        self.actuator_enable = epics_signal_w(EnableDisable, prefix + "ActuatorEnable")
        self.actuator_enabled = epics_signal_r(
            EnabledDisabled, prefix + "ActuatorEnabled_RBV"
        )
        self.connected = epics_signal_r(ConnectionState, prefix + "Connected_RBV")
        shared = PsylotechSignals(
            self.actuator_enable, self.actuator_enabled, self.connected
        )
        with self.add_children_as_readables():
            self.rot = PsylotechRotation(prefix, shared, name="rot", panda_encoder=2)
            self.load = PsylotechLoad(prefix, shared, name="load")

        super().__init__(name=name)
