"""Orchestration tests for the tomography step and loop scans.

``tomo_flyscan`` and ``tomo_dark_flat`` are replaced by recorders, so these tests
check only the order, arguments and retries of the inner plans.
"""

from typing import Any

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from bluesky.utils import PlanHalt
from ophyd_async.core import init_devices, set_mock_value
from ophyd_async.epics.motor import Motor

import xpd_tools.tomo.flyscans as flyscans
from xpd_tools.tomo import (
    tomo_1d_step_scan,
    tomo_2d_step_scan,
    tomo_flyscan_loop,
    tomo_nd_step_scan,
)


class Recorder:
    """Stand-in for tomo_flyscan ("F") and tomo_dark_flat ("D")."""

    def __init__(self, failures: int = 0, failure: type[BaseException] = RuntimeError):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures = failures
        self._failure = failure

    @property
    def sequence(self) -> str:
        return "".join(kind for kind, _ in self.calls)

    @property
    def flyscans(self) -> list[dict[str, Any]]:
        return [kwargs for kind, kwargs in self.calls if kind == "F"]

    def tomo_flyscan(self, **kwargs):
        self.calls.append(("F", kwargs))
        yield from bps.null()
        if self._failures:
            self._failures -= 1
            raise self._failure("flyscan failed")
        return f"uid-{len(self.flyscans)}"

    def tomo_dark_flat(self, **kwargs):
        self.calls.append(("D", kwargs))
        yield from bps.null()
        return "dark-flat-uid"


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(flyscans, "tomo_flyscan", rec.tomo_flyscan)
    monkeypatch.setattr(flyscans, "tomo_dark_flat", rec.tomo_dark_flat)
    return rec


@pytest.fixture
async def stages() -> tuple[Motor, Motor]:
    async with init_devices(mock=True):
        stage_x = Motor("X:", name="stage_x")
        stage_y = Motor("Y:", name="stage_y")
    return stage_x, stage_y


DETS: list[Any] = []


@pytest.mark.parametrize(
    ("num_iterations", "dark_flat_offset", "dark_flat_frequency", "expected"),
    [
        (3, 0.0, 0, "FFF"),
        (3, 1.0, 0, "DFFFD"),
        (4, 1.0, 2, "DFFDFFD"),
        (3, 1.0, 1, "DFDFDFD"),
    ],
)
def test_loop_dark_flat_cadence(
    RE: RunEngine,
    recorder: Recorder,
    num_iterations: int,
    dark_flat_offset: float,
    dark_flat_frequency: int,
    expected: str,
):
    RE(
        tomo_flyscan_loop(
            DETS,
            num_projections=5,
            exposure_time=0.1,
            num_iterations=num_iterations,
            dark_flat_offset=dark_flat_offset,
            dark_flat_frequency=dark_flat_frequency,
        )
    )
    assert recorder.sequence == expected


def test_dark_flats_use_projection_exposure(RE: RunEngine, recorder: Recorder):
    RE(tomo_flyscan_loop(DETS, 5, 0.25, dark_flat_offset=1.5, sample_out_velocity=0.05))
    dark_flat = recorder.calls[0][1]
    assert dark_flat["exposure_time"] == 0.25
    assert dark_flat["sample_out_offset"] == 1.5
    assert dark_flat["sample_out_velocity"] == 0.05


def test_loop_md_chains_previous_uid(RE: RunEngine, recorder: Recorder):
    RE(tomo_flyscan_loop(DETS, 5, 0.1, num_iterations=3, alternate_flyscan_dir=True))
    mds = [call["md"] for call in recorder.flyscans]
    assert [md["grid_index"] for md in mds] == [0, 1, 2]
    assert all(md["num_grid_points"] == 3 for md in mds)
    assert "previous_grid_point_uid" not in mds[0]
    assert mds[1]["previous_grid_point_uid"] == "uid-1"
    assert mds[2]["previous_grid_point_uid"] == "uid-2"
    directions = [(c["start"], c["stop"]) for c in recorder.flyscans]
    assert directions == [(0, 180), (180, 0), (0, 180)]


async def test_1d_step_scan_moves_and_returns(
    RE: RunEngine, recorder: Recorder, stages
):
    stage_x, _ = stages
    RE(tomo_1d_step_scan(DETS, stage_x, 1.0, 3.0, 3, 5, 0.1))
    positions = [c["md"]["grid_positions"] for c in recorder.flyscans]
    assert positions == [{"stage_x": 1.0}, {"stage_x": 2.0}, {"stage_x": 3.0}]
    assert await stage_x.user_readback.get_value() == 0.0


def test_2d_step_scan_snakes_inner_axis(RE: RunEngine, recorder: Recorder, stages):
    stage_x, stage_y = stages
    RE(
        tomo_2d_step_scan(
            DETS, stage_y, 0, 1, 2, stage_x, 0, 1, 2, 5, 0.1, snake_axes=True
        )
    )
    positions = [
        (c["md"]["grid_positions"]["stage_y"], c["md"]["grid_positions"]["stage_x"])
        for c in recorder.flyscans
    ]
    assert positions == [(0, 0), (0, 1), (1, 1), (1, 0)]


def test_2d_step_scan_forwards_dark_flats(RE: RunEngine, recorder: Recorder, stages):
    stage_x, stage_y = stages
    RE(
        tomo_2d_step_scan(
            DETS, stage_y, 0, 1, 2, stage_x, 0, 1, 2, 5, 0.1, dark_flat_offset=1.0
        )
    )
    assert recorder.sequence == "DFFFFD"


async def test_relative_1d_step_scan(RE: RunEngine, recorder: Recorder, stages):
    stage_x, _ = stages
    set_mock_value(stage_x.user_setpoint, 10.0)
    set_mock_value(stage_x.user_readback, 10.0)
    RE(tomo_1d_step_scan(DETS, stage_x, -1.0, 1.0, 3, 5, 0.1, relative=True))

    mds = [c["md"] for c in recorder.flyscans]
    assert [md["grid_positions"] for md in mds] == [
        {"stage_x": 9.0},
        {"stage_x": 10.0},
        {"stage_x": 11.0},
    ]
    assert all(md["grid_reference_positions"] == {"stage_x": 10.0} for md in mds)
    # The rotation range is untouched by relative.
    assert [(c["start"], c["stop"]) for c in recorder.flyscans] == [(0, 180)] * 3
    assert await stage_x.user_readback.get_value() == 10.0


def test_relative_2d_step_scan_offsets_each_axis(
    RE: RunEngine, recorder: Recorder, stages
):
    stage_x, stage_y = stages
    for motor, pos in ((stage_x, 5.0), (stage_y, -2.0)):
        set_mock_value(motor.user_setpoint, pos)
        set_mock_value(motor.user_readback, pos)
    RE(
        tomo_2d_step_scan(
            DETS, stage_y, 0, 1, 2, stage_x, 0, 1, 2, 5, 0.1, relative=True
        )
    )
    positions = [
        (c["md"]["grid_positions"]["stage_y"], c["md"]["grid_positions"]["stage_x"])
        for c in recorder.flyscans
    ]
    assert positions == [(-2.0, 5.0), (-2.0, 6.0), (-1.0, 5.0), (-1.0, 6.0)]


def test_relative_repeats_use_one_reference(RE: RunEngine, recorder: Recorder, stages):
    stage_x, _ = stages
    set_mock_value(stage_x.user_setpoint, 3.0)
    set_mock_value(stage_x.user_readback, 3.0)
    RE(
        tomo_nd_step_scan(
            DETS,
            stage_x,
            0,
            1,
            2,
            num_projections=5,
            exposure_time=0.1,
            num_repeats=2,
            relative=True,
        )
    )
    positions = [c["md"]["grid_positions"]["stage_x"] for c in recorder.flyscans]
    assert positions == [3.0, 4.0, 3.0, 4.0]


def test_absolute_step_scan_has_no_reference(RE: RunEngine, recorder: Recorder, stages):
    stage_x, _ = stages
    set_mock_value(stage_x.user_readback, 10.0)
    RE(tomo_1d_step_scan(DETS, stage_x, 1.0, 2.0, 2, 5, 0.1))
    mds = [c["md"] for c in recorder.flyscans]
    assert [md["grid_positions"] for md in mds] == [{"stage_x": 1.0}, {"stage_x": 2.0}]
    assert all("grid_reference_positions" not in md for md in mds)


def test_retries_failed_point(RE: RunEngine, recorder: Recorder):
    recorder._failures = 2
    RE(tomo_flyscan_loop(DETS, 5, 0.1, num_iterations=2, num_retries=2))
    assert recorder.sequence == "FFFF"


def test_gives_up_after_retries(RE: RunEngine, recorder: Recorder):
    recorder._failures = 5
    with pytest.raises(RuntimeError, match="failed after 2 attempt"):
        RE(tomo_flyscan_loop(DETS, 5, 0.1, num_retries=1))


def test_halt_is_never_retried(RE: RunEngine, recorder: Recorder):
    recorder._failures = 1
    recorder._failure = PlanHalt
    # The RunEngine ends the run on a halt instead of raising it.
    RE(tomo_flyscan_loop(DETS, 5, 0.1, num_retries=3))
    assert recorder.sequence == "F"


def test_rejects_malformed_axes(RE: RunEngine, recorder: Recorder, stages):
    stage_x, stage_y = stages
    with pytest.raises(ValueError, match="groups"):
        RE(tomo_nd_step_scan(DETS, stage_x, 0, 1, num_projections=5, exposure_time=1))
    with pytest.raises(ValueError, match="outermost"):
        RE(
            tomo_nd_step_scan(
                DETS,
                stage_y,
                0,
                1,
                2,
                stage_x,
                0,
                1,
                2,
                num_projections=5,
                exposure_time=1,
                snake_axes=[stage_y],
            )
        )
