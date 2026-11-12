import pytest
from ophyd_async.core import Device
from pydantic import ValidationError

from xpd_tools.detectors.config import DetConfig, as_det_configs, check_det_configs


@pytest.fixture
def dets() -> tuple[Device, Device, Device]:
    return Device(name="pilatus1"), Device(name="iris1"), Device(name="xspress3-1")


def test_det_config_defaults_to_one_frame_per_point(dets) -> None:
    config = DetConfig(detector=dets[0], exposure=0.01)
    assert config.frames_per_point == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"exposure": 0},
        {"exposure": -0.1},
        {"exposure": 0.1, "frames_per_point": 0},
        {"exposure": 0.1, "frames_per_point": 1.5},
        {"exposure": 0.1, "threshold": 8.0},
    ],
)
def test_det_config_rejects_invalid_fields(dets, fields) -> None:
    with pytest.raises(ValidationError):
        DetConfig(detector=dets[0], **fields)


def test_det_config_rejects_a_name_for_the_detector() -> None:
    with pytest.raises(ValidationError):
        DetConfig.model_validate({"detector": "pilatus1", "exposure": 0.1})


def test_det_config_is_frozen(dets) -> None:
    config = DetConfig(detector=dets[0], exposure=0.01)
    with pytest.raises(ValidationError):
        config.exposure = 0.02  # pyright: ignore[reportAttributeAccessIssue]


def test_as_det_configs_accepts_a_mix_in_order(dets) -> None:
    pil, iris, xs3 = dets
    given = DetConfig(detector=iris, exposure=0.05, frames_per_point=2)
    configs = as_det_configs(
        [pil, given, {"detector": xs3, "exposure": 0.1}], default_exposure=0.01
    )
    assert configs == [
        DetConfig(detector=pil, exposure=0.01),
        given,
        DetConfig(detector=xs3, exposure=0.1),
    ]


def test_as_det_configs_needs_a_default_for_a_bare_detector(dets) -> None:
    with pytest.raises(ValueError, match="pilatus1"):
        as_det_configs([dets[0]])


def test_as_det_configs_rejects_an_invalid_dict(dets) -> None:
    with pytest.raises(ValueError):
        as_det_configs([{"detector": dets[0], "exposure": -1}])


def test_as_det_configs_rejects_other_types() -> None:
    with pytest.raises(TypeError, match="pilatus1"):
        as_det_configs(["pilatus1"])  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("counts", [(1,), (1, 1), (1, 2), (1, 2, 4), (2, 6), (1, 3, 6)])
def test_check_accepts_whole_multiples(dets, counts) -> None:
    check_det_configs(
        [
            DetConfig(detector=det, exposure=0.01, frames_per_point=count)
            for det, count in zip(dets, counts, strict=False)
        ]
    )


@pytest.mark.parametrize("counts", [(2, 3), (2, 4, 6), (3, 4)])
def test_check_rejects_counts_that_are_not_multiples(dets, counts) -> None:
    configs = [
        DetConfig(detector=det, exposure=0.01, frames_per_point=count)
        for det, count in zip(dets, counts, strict=False)
    ]
    with pytest.raises(ValueError, match="does not divide"):
        check_det_configs(configs)


def test_check_rejects_no_detectors() -> None:
    with pytest.raises(ValueError, match="no detectors"):
        check_det_configs([])


def test_check_rejects_too_many_detectors(dets) -> None:
    configs = [
        DetConfig(detector=Device(name=f"det{i}"), exposure=0.01) for i in range(4)
    ]
    with pytest.raises(ValueError, match="4 detectors are given; at most 3"):
        check_det_configs(configs)


def test_check_rejects_a_detector_given_twice(dets) -> None:
    config = DetConfig(detector=dets[0], exposure=0.01)
    with pytest.raises(ValueError, match="'pilatus1' is given more than once"):
        check_det_configs([config, config])


def test_check_rejects_two_detectors_with_one_name() -> None:
    configs = [
        DetConfig(detector=Device(name="pilatus1"), exposure=0.01) for _ in range(2)
    ]
    with pytest.raises(ValueError, match="two detectors are named 'pilatus1'"):
        check_det_configs(configs)


def test_check_accepts_frames_that_fit_the_period(dets) -> None:
    pil, iris, _ = dets
    configs = [
        DetConfig(detector=pil, exposure=0.009, frames_per_point=10),
        DetConfig(detector=iris, exposure=0.09),
    ]
    check_det_configs(configs, period=0.1, deadtimes={pil: 0.001, iris: 0.01})


def test_check_rejects_frames_that_do_not_fit_the_period(dets) -> None:
    pil = dets[0]
    configs = [DetConfig(detector=pil, exposure=0.01, frames_per_point=10)]
    with pytest.raises(ValueError, match="'pilatus1' needs 0.11 s per point"):
        check_det_configs(configs, period=0.1, deadtimes={pil: 0.001})


def test_check_reports_every_problem_at_once(dets) -> None:
    pil, iris, _ = dets
    configs = [
        DetConfig(detector=pil, exposure=0.2, frames_per_point=2),
        DetConfig(detector=iris, exposure=0.01, frames_per_point=3),
        DetConfig(detector=pil, exposure=0.01),
    ]
    with pytest.raises(ValueError) as error:
        check_det_configs(configs, period=0.1)
    message = str(error.value)
    assert "given more than once" in message
    assert "does not divide" in message
    assert "needs 0.4 s per point" in message


@pytest.mark.parametrize("exposure", [float("inf"), float("nan")])
def test_det_config_rejects_non_finite_exposure(dets, exposure) -> None:
    with pytest.raises(ValidationError):
        DetConfig(detector=dets[0], exposure=exposure)


@pytest.mark.parametrize("period", [0, -0.1, float("inf"), float("nan")])
def test_check_rejects_an_invalid_period(dets, period) -> None:
    configs = [DetConfig(detector=dets[0], exposure=0.01)]
    with pytest.raises(ValueError, match="period must be a finite number above 0"):
        check_det_configs(configs, period=period)


@pytest.mark.parametrize("deadtime", [-0.15, float("inf"), float("nan")])
def test_check_rejects_an_invalid_deadtime(dets, deadtime) -> None:
    pil = dets[0]
    configs = [DetConfig(detector=pil, exposure=0.2)]
    with pytest.raises(ValueError, match="'pilatus1' deadtime must be a finite"):
        check_det_configs(configs, period=0.1, deadtimes={pil: deadtime})
