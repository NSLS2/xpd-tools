"""Offline checks of the Psylotech devices."""

import asyncio
from pathlib import Path

import bluesky.plan_stubs as bps
import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    EnabledDisabled,
    EnableDisable,
    FlyMotorInfo,
    StaticPathProvider,
    UUIDFilenameProvider,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)
from ophyd_async.core._mock_signal_utils import (  # noqa: PLC2701
    _get_mock_signal_backend,
)
from ophyd_async.epics.adcore import ADWriterFactory
from test_plans import (
    SCAN_START,
    SCAN_STOP,
    HDFPandaWithCalc,
    _add_pcomp_inp,  # noqa: PLC2701
    _setup_flyscan_coordination,  # noqa: PLC2701
)

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.plans import go_to_load, psylotech_flyscan, single_axis_flyscan
from xpd_tools.psylotech import (
    ConnectionState,
    Psylotech,
    PsylotechAxial,
    PsylotechError,
    PsylotechLoad,
    PsylotechRotation,
)

PREFIX = "PSYLO:"
NUM_IMAGES = 11
#: Load per meter of axial travel, so a probe step gives a load that can be seen
SPRING = 1e6


def _record_actuator_writes(device) -> list[EnableDisable]:
    """Record the writes to the actuator enable, keeping the mock's behaviour."""
    writes: list[EnableDisable] = []
    original = _get_mock_signal_backend(device.actuator_enable)._mock_put_callback

    def _record(value):
        writes.append(value)
        if original:
            return original(value)

    callback_on_mock_put(device.actuator_enable, _record)
    return writes


def _attach_spring(axial: PsylotechAxial, load: PsylotechLoad, stiffness: float):
    """Make the axial actuator arrive instantly and load a spring of `stiffness`."""

    def _move(position: float):
        set_mock_value(axial.user_readback, position)
        set_mock_value(load.load, stiffness * position)

    callback_on_mock_put(axial.user_setpoint, _move)


@pytest.fixture
async def psylo() -> Psylotech:
    async with init_devices(mock=True):
        psylo = Psylotech(PREFIX, name="psylo")
    return psylo


@pytest.fixture
def rotation(psylo: Psylotech) -> PsylotechRotation:
    return psylo.rot


@pytest.fixture
def load_frame(psylo: Psylotech) -> tuple[PsylotechAxial, PsylotechLoad]:
    axial, load = psylo.load.axial, psylo.load
    _attach_spring(axial, load, SPRING)
    set_mock_value(load.settle_time, 0.01)
    return axial, load


async def test_move_powers_the_actuators_for_the_move_only(
    psylo: Psylotech, rotation: PsylotechRotation
):
    writes = _record_actuator_writes(psylo)
    await rotation.set(12.0)
    assert await rotation.user_readback.get_value() == 12.0
    assert writes == [EnableDisable.ENABLE, EnableDisable.DISABLE]
    assert await psylo.actuator_enabled.get_value() == EnabledDisabled.DISABLED


async def test_move_refused_when_controller_disconnected(
    psylo: Psylotech, rotation: PsylotechRotation
):
    set_mock_value(psylo.connected, ConnectionState.DISCONNECTED)
    writes = _record_actuator_writes(psylo)
    with pytest.raises(PsylotechError, match="reconnect"):
        await rotation.set(1.0)
    assert writes == []


async def test_kickoff_refused_when_controller_disconnects_after_prepare(
    psylo: Psylotech, rotation: PsylotechRotation
):
    writes = _record_actuator_writes(psylo)
    await rotation.prepare(
        FlyMotorInfo(start_position=0.0, end_position=10.0, time_for_move=10.0)
    )
    assert await psylo.actuator_enabled.get_value() == EnabledDisabled.ENABLED
    set_mock_value(psylo.connected, ConnectionState.DISCONNECTED)
    setpoint = await rotation.user_setpoint.get_value()
    with pytest.raises(PsylotechError, match="reconnect"):
        await rotation.kickoff()
    assert await rotation.user_setpoint.get_value() == setpoint
    assert writes == [EnableDisable.ENABLE, EnableDisable.DISABLE]


async def test_unstage_powers_down_a_prepared_axis(
    psylo: Psylotech, rotation: PsylotechRotation
):
    await rotation.prepare(
        FlyMotorInfo(start_position=0.0, end_position=10.0, time_for_move=10.0)
    )
    assert await psylo.actuator_enabled.get_value() == EnabledDisabled.ENABLED
    await rotation.unstage()
    assert await psylo.actuator_enabled.get_value() == EnabledDisabled.DISABLED


async def test_stage_does_not_touch_an_idle_axis(
    psylo: Psylotech, rotation: PsylotechRotation
):
    writes = _record_actuator_writes(psylo)
    await rotation.stage()
    await rotation.unstage()
    assert writes == []


async def test_load_move_settles_in_tolerance(
    psylo: Psylotech, load_frame: tuple[PsylotechAxial, PsylotechLoad]
):
    _, load = load_frame
    writes = _record_actuator_writes(psylo)
    await load.set(50.0)
    assert await load.in_tolerance.get_value()
    assert await load.load.get_value() == pytest.approx(50.0, abs=1.0)
    # One power cycle for the whole move, not one per step
    assert writes == [EnableDisable.ENABLE, EnableDisable.DISABLE]


async def test_load_move_follows_a_load_that_falls_with_position(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    axial, load = load_frame
    _attach_spring(axial, load, -SPRING)
    await load.set(30.0)
    assert await load.load.get_value() == pytest.approx(30.0, abs=1.0)


@pytest.mark.parametrize("sign", [1, -1])
async def test_load_move_refused_beyond_either_load_limit(
    load_frame: tuple[PsylotechAxial, PsylotechLoad], sign: int
):
    _, load = load_frame
    limit = load.HIGH_LIMIT if sign > 0 else load.LOW_LIMIT
    with pytest.raises(PsylotechError, match="load limits"):
        await load.set(limit + sign)


async def test_load_limits_are_independent(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    _, load = load_frame
    set_mock_value(load.low_limit, -20.0)
    await load.set(60.0)  # tension is still allowed
    with pytest.raises(PsylotechError, match="load limits"):
        await load.set(-30.0)


async def test_load_move_crosses_zero(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    _, load = load_frame
    await load.set(40.0)
    await load.set(-40.0)
    assert await load.load.get_value() == pytest.approx(-40.0, abs=1.0)
    await load.set(25.0)
    assert await load.load.get_value() == pytest.approx(25.0, abs=1.0)


async def test_load_move_crosses_zero_through_a_dead_zone(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    """The load doesn't respond while the grips take up slack around zero."""
    axial, load = load_frame
    slack, spring = 1e-4, SPRING

    def _move(position: float):
        set_mock_value(axial.user_readback, position)
        if abs(position) <= slack:
            response = 0.0
        else:
            response = spring * (position - slack * (1 if position > 0 else -1))
        set_mock_value(load.load, response)

    callback_on_mock_put(axial.user_setpoint, _move)
    await load.set(30.0)
    await load.set(-30.0)
    assert await load.load.get_value() == pytest.approx(-30.0, abs=1.0)


async def test_axial_refused_beyond_travel_limits(
    psylo: Psylotech, load_frame: tuple[PsylotechAxial, PsylotechLoad]
):
    axial, _ = load_frame
    writes = _record_actuator_writes(psylo)
    with pytest.raises(PsylotechError, match="travel limits"):
        await axial.set(axial.HIGH_LIMIT + 1)
    with pytest.raises(PsylotechError, match="travel limits"):
        await axial.set(axial.LOW_LIMIT - 1)
    assert writes == []


async def test_move_refused_above_max_jog_rate(rotation: PsylotechRotation):
    set_mock_value(rotation.velocity, rotation.MAX_VELOCITY * 2)
    with pytest.raises(PsylotechError, match="maximum"):
        await rotation.set(1.0)


async def test_in_tolerance_follows_tolerance_signal(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    _, load = load_frame
    set_mock_value(load.load, 10.0)
    await load.target_load.set(12.0)
    set_mock_value(load.tolerance, 1.0)
    assert not await load.in_tolerance.get_value()
    set_mock_value(load.tolerance, 3.0)
    assert await load.in_tolerance.get_value()


@pytest.fixture
async def flyscan_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(PsylotechRotation, "ACCELERATION_TIME", 0.0)
    path_provider = StaticPathProvider(
        UUIDFilenameProvider(), tmp_path, create_dir_depth=-2
    )
    async with init_devices(mock=True):
        panda = HDFPandaWithCalc("PANDA:", path_provider)
    await _add_pcomp_inp(panda)
    async with init_devices(mock=True):
        psylo = Psylotech(PREFIX, name="psylo")
        pilatus1 = Pilatus4Detector(
            "DET1:", ADWriterFactory.hdf(path_provider), name="pilatus1"
        )
    set_mock_value(pilatus1.driver.acquire_time, 0.001)
    set_mock_value(pilatus1.driver.acquire_period, 0.002)
    motor = psylo.rot
    motor.panda_encoder = 1
    _setup_flyscan_coordination(panda, motor, pilatus1, NUM_IMAGES)
    return pilatus1, panda, motor, psylo


def test_rotation_through_single_axis_flyscan(RE: RunEngine, flyscan_devices):
    """The rotation axis can be flown by the single axis flyscan plan."""
    pilatus1, panda, motor, psylo = flyscan_devices
    writes = _record_actuator_writes(psylo)
    docs: dict[str, list] = {}
    RE.subscribe(lambda name, doc: docs.setdefault(name, []).append(doc))

    RE(
        single_axis_flyscan(
            detectors=[pilatus1],
            panda=panda,
            motor=motor,
            num_images=NUM_IMAGES,
            start=SCAN_START,
            stop=SCAN_STOP,
        )
    )

    assert docs["stop"][0]["exit_status"] == "success"
    # The actuators stay powered from prepare until the fly move completes
    assert writes == [EnableDisable.ENABLE, EnableDisable.DISABLE]
    scale = asyncio.run(panda.calc[1].out_scale.get_value())
    assert scale == pytest.approx(360.0 / PsylotechRotation.ENCODER_COUNTS_PER_REV)
    # The jog rate was left at the fly speed
    velocity = asyncio.run(motor.velocity.get_value())
    assert velocity == pytest.approx(PsylotechRotation.MAX_VELOCITY)


async def test_go_to_load_plan(RE: RunEngine, psylo: Psylotech, load_frame):
    _, load = load_frame
    results = []

    def plan():
        results.append((yield from go_to_load(psylo, 35.0, tolerance=2.0)))

    RE(plan())
    assert results[0] == pytest.approx(35.0, abs=2.0)
    assert await load.tolerance.get_value() == 2.0


def test_psylotech_flyscan_plan_records_the_load(RE: RunEngine, flyscan_devices):
    pilatus1, panda, motor, psylo = flyscan_devices
    load = psylo.load
    set_mock_value(load.load, 42.0)
    starts: list[dict] = []
    RE.subscribe(lambda name, doc: starts.append(doc) if name == "start" else None)

    RE(
        psylotech_flyscan(
            [pilatus1],
            panda,
            psylo,
            num_images=NUM_IMAGES,
            start=SCAN_START,
            stop=SCAN_STOP,
            md={"sample": "steel"},
        )
    )

    assert starts[0]["psylotech_load"] == 42.0
    assert starts[0]["sample"] == "steel"
    assert starts[0]["plan_name"] == "single_axis_flyscan"


async def test_relative_move_starts_from_the_readback(
    RE: RunEngine, psylo: Psylotech, rotation: PsylotechRotation
):
    """The setpoint PV is stale, so mvr must start from where the axis is."""
    set_mock_value(rotation.user_readback, 30.0)
    set_mock_value(rotation.user_setpoint, 0.0)
    RE(bps.mvr(rotation, 5.0))
    assert await rotation.user_readback.get_value() == pytest.approx(35.0)


async def test_tiny_relative_axial_move_actually_moves(
    RE: RunEngine, load_frame: tuple[PsylotechAxial, PsylotechLoad]
):
    axial, _ = load_frame
    RE(bps.mvr(axial, 1e-4))
    assert await axial.user_readback.get_value() == pytest.approx(1e-4)


async def test_relative_load_move_starts_from_the_load(
    RE: RunEngine, load_frame: tuple[PsylotechAxial, PsylotechLoad]
):
    _, load = load_frame
    set_mock_value(load.load, 20.0)
    assert (await load.locate())["setpoint"] == 20.0


async def test_load_move_stops_when_the_sample_breaks(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    axial, load = load_frame
    spring = SPRING

    def _move(position: float):
        set_mock_value(axial.user_readback, position)
        # The sample lets go once it has been loaded past 200
        broken = position * spring > 200.0
        set_mock_value(load.load, 0.0 if broken else position * spring)

    callback_on_mock_put(axial.user_setpoint, _move)
    set_mock_value(load.low_limit, -4000.0)
    with pytest.raises(PsylotechError, match="broken"):
        await load.set(400.0)


async def test_load_move_stops_when_the_sample_slips(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    axial, load = load_frame
    spring = SPRING

    def _move(position: float):
        set_mock_value(axial.user_readback, position)
        # Past a load of 100 the grips slip and the load stops rising
        set_mock_value(load.load, min(position * spring, 100.0))

    callback_on_mock_put(axial.user_setpoint, _move)
    with pytest.raises(PsylotechError, match="slipping"):
        await load.set(300.0)


async def test_load_move_probes_first_then_caps_each_step(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    axial, load = load_frame
    set_mock_value(load.probe_step, 5e-6)
    set_mock_value(load.max_step, 1e-5)
    positions: list[float] = [0.0]
    original = _get_mock_signal_backend(axial.user_setpoint)._mock_put_callback

    def _record(value):
        positions.append(value)
        if original:
            return original(value)

    callback_on_mock_put(axial.user_setpoint, _record)
    await load.set(40.0)
    steps = [b - a for a, b in zip(positions, positions[1:], strict=False)]
    assert steps[0] == pytest.approx(5e-6)
    assert all(abs(step) <= 1e-5 + 1e-12 for step in steps[1:])
    assert len(steps) > 3


async def test_load_move_times_out_when_the_load_never_settles(
    load_frame: tuple[PsylotechAxial, PsylotechLoad],
):
    axial, load = load_frame
    load.movable_logic.move_timeout = 0.3  # built when the device was named
    # The load never responds to the actuator, so the move can't finish
    callback_on_mock_put(
        axial.user_setpoint, lambda v: set_mock_value(axial.user_readback, v)
    )
    with pytest.raises(TimeoutError):
        await load.set(500.0)
