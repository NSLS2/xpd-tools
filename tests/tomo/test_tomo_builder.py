"""Tests for the interactive detector config builder."""

from collections.abc import Iterator

import pytest
from tomo_rig import TomoRig

from xpd_tools.tomo import (
    BuilderCancelledError,
    DetectorConfig,
    build_detector_configs,
    image_count_options,
    suggest_base_images,
    validate_detector_configs,
)
from xpd_tools.tomo.builder import discover_detectors, parse_selection


def _run(rig: TomoRig, answers: list[str], **kwargs):
    """Run the builder with scripted answers; returns (configs, printed text)."""
    replies: Iterator[str] = iter(answers)
    printed: list[str] = []

    def fake_input(prompt: str) -> str:
        printed.append(prompt)
        return next(replies)

    configs = build_detector_configs(
        detectors={"pilatus1": rig.pilatus, "iris1": rig.iris, "xs3": rig.xs3},
        namespace=kwargs.pop("namespace", None),
        input_fn=fake_input,
        print_fn=lambda *a: printed.append(" ".join(map(str, a))),
        color=False,
        emoji=False,
        **kwargs,
    )
    return configs, "\n".join(printed)


def test_image_count_options_spacing():
    options = {o.nproj: o for o in image_count_options(100, 0, 360)}
    assert set(options) == {1, 2, 4, 5, 10, 20, 25, 50, 100}
    full = options[100]
    assert (full.divisor, full.step, full.first_angle, full.last_angle) == (
        1,
        3.6,
        0,
        360,
    )
    assert full.num_images == 101
    # Every 2nd pulse is 50 projections 7.2 degrees apart, 51 images, the last at
    # the end of the range too.
    half = options[50]
    assert (half.divisor, half.num_images) == (2, 51)
    assert half.step == pytest.approx(7.2)
    assert half.last_angle == 360
    # One projection is the first and last image only.
    assert options[1].divisor == 100
    assert options[1].step == 360


def test_suggest_base_images():
    assert suggest_base_images(1000, [3, 8], how_many=3) == [1008, 984, 1032]
    assert all(n % 3 == 0 and n % 8 == 0 for n in suggest_base_images(1000, [3, 8]))
    assert suggest_base_images(5, [1])[0] == 5
    with pytest.raises(ValueError, match="positive"):
        suggest_base_images(0, [1])


def test_parse_selection():
    names = ["pilatus1", "iris1", "xs3"]
    assert parse_selection("1 3", names) == [0, 2]
    assert parse_selection("1,2", names) == [0, 1]
    assert parse_selection("1-3", names) == [0, 1, 2]
    assert parse_selection("all", names) == [0, 1, 2]
    assert parse_selection("IRIS1 pilatus1", names) == [1, 0]
    assert parse_selection("2 2", names) == [1]
    for bad in ("", "4", "0", "nope"):
        with pytest.raises(ValueError):  # noqa: PT011
            parse_selection(bad, names)


def test_discover_detectors_finds_each_detector_once(tomo_rig: TomoRig):
    namespace = {
        "pilatus1_fly": tomo_rig.pilatus,
        "also_pilatus": tomo_rig.pilatus,
        "iris1": tomo_rig.iris,
        "_hidden": tomo_rig.xs3,
        "not_a_detector": 3,
    }
    assert discover_detectors(namespace) == {
        "pilatus1_fly": tomo_rig.pilatus,
        "iris1": tomo_rig.iris,
    }


def iris_images(configs) -> int:
    return configs[1].num_images


def test_builds_configs_ready_for_the_flyscan(tomo_rig: TomoRig):
    rig = tomo_rig
    configs, shown = _run(
        rig,
        # detectors, then pilatus (exposure, nproj), iris (exposure, nproj)
        ["1 2", "0.1", "100", "0.05", "200", "y"],
    )
    assert all(isinstance(c, DetectorConfig) for c in configs)
    pil, iris = configs
    assert (pil.detector, pil.ttl_out, pil.exposure_time, pil.nproj) == (
        rig.pilatus,
        1,
        0.1,
        100,
    )
    assert (pil.num_images, iris_images(configs)) == (101, 201)
    assert (iris.detector, iris.ttl_out, iris.nproj) == (rig.iris, 3, 200)
    validate_detector_configs(configs)
    assert "RE(tomo_flyscan(configs, start=0, stop=360))" in shown


def test_confirmed_configs_are_saved_as_detconfig(tomo_rig: TomoRig):
    namespace: dict = {"DETCONFIG": "old"}
    configs, shown = _run(
        tomo_rig, ["1", "0.1", "10", "y"], namespace=namespace, start=0, stop=360
    )
    assert namespace["DETCONFIG"] == configs
    assert "Replaced the previous DETCONFIG" in shown
    assert "Saved as DETCONFIG" in shown
    assert "RE(tomo_flyscan(DETCONFIG, start=0, stop=360))" in shown


def test_cancelled_configs_are_not_saved(tomo_rig: TomoRig):
    namespace: dict = {}
    with pytest.raises(BuilderCancelledError):
        _run(tomo_rig, ["1", "0.1", "10", "n"], namespace=namespace)
    assert namespace == {}


def test_bad_answers_are_asked_again(tomo_rig: TomoRig):
    configs, shown = _run(
        tomo_rig,
        ["", "9", "1", "abc", "-1", "0.1", "0", "10", "y"],
    )
    (config,) = configs
    assert (config.exposure_time, config.nproj) == (0.1, 10)
    assert "'abc' is not a number" in shown
    assert "greater than 0" in shown
    assert "Pick at least one detector" in shown
    assert "no detector number 9" in shown


def test_uneven_projection_counts_are_offered_options_and_fixed(tomo_rig: TomoRig):
    configs, shown = _run(
        tomo_rig,
        # pilatus 100 and iris 150 do not divide; re-enter 100 -> 150? keep iris, fix
        # the pilatus to 75 (divides 150).
        ["1 2", "0.1", "100", "0.1", "150", "y", "75", "150", "y"],
    )
    assert [c.nproj for c in configs] == [75, 150]
    assert "must be a multiple of each detector's" in shown
    assert "Or raise the most projections" in shown


def test_cancelling(tomo_rig: TomoRig):
    with pytest.raises(BuilderCancelledError):
        _run(tomo_rig, ["1", "q"])
    with pytest.raises(BuilderCancelledError):
        _run(tomo_rig, ["1", "0.1", "10", "n"])
    with pytest.raises(BuilderCancelledError, match="No detectors"):
        build_detector_configs(detectors={}, input_fn=lambda _: "", print_fn=print)
