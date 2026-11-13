"""Try to break the tomo flyscans with many short, odd scans on the mock rig.

Each case is a rotation range plus one to three detectors with odd ``nproj`` and
exposure times. The expected outcome is worked out here, independently of the plans:

* the most projections must be a multiple of every other detector's, or the configs
  are rejected before anything moves;
* position-based triggers need the travel, in encoder counts, to be a multiple of the
  gaps between base pulses (and at least two counts per gap), or the models are
  rejected;
* anything else must run, and every detector must write ``nproj + 1`` images, at
  angles that start at ``start``, end at ``stop`` and are evenly spaced.

A case that fails for any other reason is a bug in the plans or in this test.
"""

import random
from dataclasses import dataclass
from typing import Any

import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import get_mock_put
from ophyd_async.fastcs.panda import PandaPcompDirection
from tomo_rig import TomoRig

import xpd_tools.tomo.flyscans as flyscans
from xpd_tools.shutter import ShutterStatus
from xpd_tools.tomo import ROTATION_STREAM, tomo_flyscan, tomo_flyscan_loop
from xpd_tools.tomo.flyscans import ROTATION_DATASET_NAME

#: Degrees per encoder count of the mock rotation motor.
RESOLUTION = 0.1

#: Rotation ranges (start, stop) with a mix of friendly and awkward counts. Negative
#: and reversed ranges, offsets that aren't a whole degree, tiny and full turns.
RANGES = [
    (0, 180),  # 1800 counts
    (0, 360),  # 3600
    (180, 0),  # reversed
    (-90, 90),  # through zero
    (-37.3, 212.9),  # 2502 counts, an awkward 2 * 3^2 * 139
    (359.9, 0),  # 3599 counts, prime-ish
    (10, 10.6),  # 6 counts
    (45.5, 46.5),  # 10 counts
    (0, 1.2),  # 12 counts
    (-0.3, 0.3),  # 6 counts, either side of zero
    (720, 1080),  # a second turn
    (0, 90.7),  # 907 counts, prime
]

#: Odd exposure times: very short and not round. The mock rig runs the fly move in real
#: time (frames x frame period), so they are kept short to keep the scans short.
EXPOSURES = [0.0001, 0.00123, 0.002, 0.007, 0.013]

#: nproj for each detector, fastest first, in sets that divide and sets that don't.
NPROJ_SETS = [
    (12,),
    (1,),
    (7,),
    (30,),
    (60, 30),
    (60, 20),
    (40, 10),
    (24, 8),
    (12, 4),
    (6, 3),
    (2, 1),
    (50, 7),  # 7 doesn't divide 50
    (10, 4),  # nor 4 divide 10
    (30, 20),  # nor 20 divide 30
    (60, 30, 10),
    (24, 12, 6),
    (12, 6, 4),
    (30, 10, 7),  # 7 doesn't divide 30
    (60, 20, 15),
    (40, 20, 10),
]


@dataclass(frozen=True)
class Case:
    start: float
    stop: float
    nprojs: tuple[int, ...]
    exposures: tuple[float, ...]
    #: Which rig detectors, in the order they get the nprojs.
    dets: tuple[str, ...]
    time_based: bool

    @property
    def id(self) -> str:
        n = "-".join(str(p) for p in self.nprojs)
        e = "-".join(f"{x:g}" for x in self.exposures)
        mode = "time" if self.time_based else "pos"
        return f"{self.start:g}to{self.stop:g}_n{n}_t{e}_{self.dets[0]}_{mode}"

    @property
    def gaps(self) -> int:
        return max(self.nprojs)

    @property
    def travel_counts(self) -> int:
        return abs(round((self.stop - self.start) / RESOLUTION))

    @property
    def expected_error(self) -> str | None:
        """The error the plan should raise, or None if the scan is valid."""
        if any(self.gaps % n for n in self.nprojs):
            return "must be a multiple"
        if self.time_based:
            return None
        if self.travel_counts % self.gaps:
            return "not evenly divisible"
        if self.travel_counts < 2 * self.gaps:
            return "less than the minimum"
        return None


def _make_cases() -> list[Case]:
    rng = random.Random(28)  # fixed, so a failure can be rerun
    names = ["pilatus1", "iris1", "xs3"]
    cases = []
    for start, stop in RANGES:
        # Each range gets a mix of valid and invalid nproj sets.
        for nprojs in rng.sample(NPROJ_SETS, 3):
            dets = tuple(rng.sample(names, len(nprojs)))
            exposures = tuple(rng.choice(EXPOSURES) for _ in nprojs)
            cases.append(Case(start, stop, nprojs, exposures, dets, False))
    # A few time-based scans too, which don't need the counts to divide.
    for start, stop in [(0, 180), (-37.3, 212.9), (359.9, 0)]:
        nprojs = rng.choice([p for p in NPROJ_SETS if len(p) > 1 and p[0] % p[1] == 0])
        dets = tuple(rng.sample(names, len(nprojs)))
        exposures = tuple(rng.choice(EXPOSURES) for _ in nprojs)
        cases.append(Case(start, stop, nprojs, exposures, dets, True))
    return cases


CASES = _make_cases()


def test_the_cases_cover_valid_and_invalid_scans():
    """Guard the generator: plenty of scans must run, and plenty must be rejected."""
    errors = [case.expected_error for case in CASES]
    assert errors.count(None) >= 15
    assert errors.count("must be a multiple") >= 5
    assert errors.count("not evenly divisible") >= 5
    assert {len(case.nprojs) for case in CASES} == {1, 2, 3}


def _frames_written(docs, data_key: str) -> int:
    resource = next(r for r in docs["stream_resource"] if r["data_key"] == data_key)
    datums = [
        d for d in docs["stream_datum"] if d["stream_resource"] == resource["uid"]
    ]
    return datums[-1]["indices"]["stop"] if datums else 0


def _detectors(rig: TomoRig) -> dict[str, Any]:
    return {"pilatus1": rig.pilatus, "iris1": rig.iris, "xs3": rig.xs3}


def _configs(rig: TomoRig, case: Case) -> list:
    dets = _detectors(rig)
    return [
        rig.config(dets[name], nproj, exposure)
        for name, nproj, exposure in zip(
            case.dets, case.nprojs, case.exposures, strict=True
        )
    ]


@pytest.fixture
def flyer_models(monkeypatch) -> list[tuple]:
    """The (flyer_info, motor_info) the flyscan computed, to check the angles."""
    models: list[tuple] = []
    original = flyscans.compute_flyscan_models

    def _spy(*args, **kwargs):
        result = yield from original(*args, **kwargs)
        models.append(result)
        return result

    monkeypatch.setattr(flyscans, "compute_flyscan_models", _spy)
    return models


def _trigger_angles(flyer_info, step_per_pulse: int, divisor: int) -> list[float]:
    """Angles of a detector's triggers, from the PandA position compare settings.

    Base pulse ``j`` fires at ``start + j * pulse_step`` counts, going the direction
    of the move, and a detector with divisor ``D`` fires on pulses 0, D, 2D, ...
    """
    sign = 1 if flyer_info.direction == PandaPcompDirection.POSITIVE else -1
    angles = []
    for pulse in range(0, flyer_info.num_pulses, divisor):
        counts = flyer_info.start + sign * pulse * step_per_pulse
        angles.append(counts * flyer_info.position_scale + flyer_info.position_offset)
    return angles


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_flyscan_combinations(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps, flyer_models, case: Case
):
    rig = tomo_rig
    configs = _configs(rig, case)
    rig.num_frames = case.gaps + 1

    def run():
        return RE(
            tomo_flyscan(
                configs,
                start=case.start,
                stop=case.stop,
                time_based=case.time_based,
                **rig.device_kwargs(),
            )
        )

    if case.expected_error:
        with pytest.raises(ValueError, match=case.expected_error):
            run()
        # Rejected before the motor or a detector moved.
        get_mock_put(rig.rot_motor.user_setpoint).assert_not_called()
        assert rig.shutter_log == [] or rig.shutter_log[-1].endswith("close")
        return

    run()
    assert docs["stop"][0]["exit_status"] == "success"
    (start_doc,) = docs["start"]

    # Every detector wrote nproj + 1 images, the PandA one angle per base pulse.
    for name, nproj in zip(case.dets, case.nprojs, strict=True):
        assert _frames_written(docs, name) == nproj + 1, name
        config = start_doc["detector_configs"][name]
        assert config["nproj"] == nproj
        assert config["num_images"] == nproj + 1
        assert config["trigger_divisor"] == case.gaps // nproj
    assert _frames_written(docs, ROTATION_DATASET_NAME) == case.gaps + 1
    assert ROTATION_STREAM in {d["name"] for d in docs["descriptor"]}

    # Each detector is told its own exposure and image count.
    for config, exposure in zip(configs, case.exposures, strict=True):
        driver = config.detector.driver
        if hasattr(driver, "acquire_time"):
            puts = get_mock_put(driver.acquire_time).call_args_list
            assert puts[-1].args[0] == exposure

    # The angles: the first image at start, the last at stop, evenly spaced.
    ((flyer_info, motor_info),) = flyer_models
    assert flyer_info.num_pulses == case.gaps + 1
    assert motor_info.start_position == case.start
    assert motor_info.end_position == case.stop
    if not case.time_based:
        step_counts = case.travel_counts // case.gaps
        assert flyer_info.pulse_step == step_counts
        step = (case.stop - case.start) / case.gaps
        for name, nproj in zip(case.dets, case.nprojs, strict=True):
            divisor = case.gaps // nproj
            angles = _trigger_angles(flyer_info, step_counts, divisor)
            assert len(angles) == nproj + 1, name
            assert angles[0] == pytest.approx(case.start, abs=RESOLUTION), name
            assert angles[-1] == pytest.approx(case.stop, abs=RESOLUTION), name
            gaps = [b - a for a, b in zip(angles, angles[1:], strict=False)]
            assert gaps == pytest.approx([step * divisor] * nproj, abs=1e-6), name

    # Always cleaned up: shutter closed, motor stopped, speed restored.
    assert await rig.photon_shutter.status.get_value() == ShutterStatus.CLOSED
    assert await rig.rot_motor.velocity.get_value() == 5.0
    get_mock_put(rig.rot_motor.motor_stop).assert_called()


async def test_flyscans_back_to_back_with_a_failure_between(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps
):
    """A rejected scan leaves nothing behind that breaks the next one."""
    rig = tomo_rig
    good = [rig.config(rig.pilatus, 20, 0.002), rig.config(rig.iris, 10, 0.002)]
    bad = [rig.config(rig.pilatus, 20, 0.002), rig.config(rig.iris, 7, 0.002)]
    rig.num_frames = 21

    def scan(configs):
        return tomo_flyscan(configs, start=0, stop=40, **rig.device_kwargs())

    RE(scan(good))
    with pytest.raises(ValueError, match="must be a multiple"):
        RE(scan(bad))
    RE(scan(good))
    assert [d["exit_status"] for d in docs["stop"]] == ["success", "success"]


async def test_flyscan_loop_alternates_direction(
    RE: RunEngine, tomo_rig: TomoRig, docs, sleeps, flyer_models
):
    """The loop plan, for real: three tomograms, the rotation reversed every other.

    Darks and flats are left out: the mock detectors keep their frame counters
    between runs, which the real ones reset, so a dark/flat after a flyscan would
    see the flyscan's frames. The dark/flat cadence is tested in test_tomo_step_scans.
    """
    rig = tomo_rig
    rig.num_frames = 11
    RE(
        tomo_flyscan_loop(
            [rig.config(rig.pilatus, 10, 0.002), rig.config(rig.iris, 5, 0.002)],
            start=0,
            stop=90,
            num_iterations=3,
            alternate_flyscan_dir=True,
            **rig.device_kwargs(),
        )
    )
    plans = [s["plan_name"] for s in docs["start"]]
    assert plans == ["tomo_flyscan"] * 3
    assert {s["exit_status"] for s in docs["stop"]} == {"success"}
    # The rotation alternates 0 -> 90, 90 -> 0, 0 -> 90.
    spans = [(m.start_position, m.end_position) for _, m in flyer_models]
    assert spans == [(0, 90), (90, 0), (0, 90)]
