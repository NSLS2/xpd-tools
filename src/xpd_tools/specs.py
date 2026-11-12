"""Tiled specs for the datasets produced by xpdtools tomography plans.

Ported from hextools. Each spec has a validator that checks a run (e.g. a
``BlueskyRunV3`` read back from Tiled) against the contract documented beside it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from tiled.structures.core import Spec


class RunLike(Protocol):
    """What the validators read from a run, e.g. a ``BlueskyRunV3``."""

    @property
    def start(self) -> Mapping[str, Any]:
        """The run's start document."""
        ...

    def __contains__(self, key: str, /) -> bool:
        """Return whether the run has a stream named ``key``."""
        ...

    def __getitem__(self, key: str, /) -> Any:
        """Return the stream named ``key``."""
        ...


class SpecValidationError(ValueError):
    """A run does not match the spec it is validated against."""


def _require_plan_name(run: RunLike, *names: str) -> None:
    plan_name = run.start.get("plan_name")
    if plan_name not in names:
        raise SpecValidationError(
            f"plan_name is {plan_name!r}, expected one of {list(names)}."
        )


def _require_start_keys(run: RunLike, *keys: str) -> None:
    missing = [key for key in keys if key not in run.start]
    if missing:
        raise SpecValidationError(f"Start document is missing {missing}.")


def _names(run: RunLike, key: str) -> list[str]:
    """Return the start document's list of names under ``key``, e.g. detectors."""
    names: Any = run.start.get(key)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise SpecValidationError(
            f"Start document '{key}' must be a list of names, got {names!r}."
        )
    return names


def _require_stream(run: RunLike, stream: str, *fields: str) -> None:
    if stream not in run:
        raise SpecValidationError(f"Run has no '{stream}' stream.")
    missing = [field for field in fields if field not in run[stream]]
    if missing:
        raise SpecValidationError(f"Stream '{stream}' is missing {missing}.")


def _require_optional_str(run: RunLike, key: str) -> None:
    value = run.start.get(key)
    if value is not None and not isinstance(value, str):
        raise SpecValidationError(
            f"Start document '{key}' must be a string, got {value!r}."
        )


def _require_exposure_time(run: RunLike) -> None:
    """Check ``exposure_time`` maps every detector's name to seconds."""
    value = run.start.get("exposure_time")
    if not isinstance(value, Mapping):
        raise SpecValidationError(
            "Start document 'exposure_time' must map detector names to seconds, "
            f"got {value!r}."
        )
    missing = [name for name in _names(run, "detectors") if name not in value]
    if missing:
        raise SpecValidationError(f"'exposure_time' has no entry for {missing}.")


def _check_version(spec_name: str, version: str | None) -> None:
    if version != "1":
        raise SpecValidationError(f"Unknown {spec_name} version {version!r}.")


def spec_md(spec: Spec) -> dict[str, str | None]:
    """Return the ``tiled_specs`` start document entry for ``spec``.

    Parameters
    ----------
    spec : Spec
        The spec the run follows.

    Returns
    -------
    dict[str, str | None]
        ``{"name": ..., "version": ...}``, the form every plan uses.
    """
    return {"name": spec.name, "version": spec.version}


# ``<det>`` is a detector's ``name`` (e.g. ``iris1``). Every stream name and every
# other dataset name is fixed.
#
# Tomography dark/flat convention: darks and flats are never part of a tomography
# run. They are taken by a separate ``tomo_dark_flat`` run (TOMO_DARK_FLAT_V1).
# The profile's LastDarkFlatUIDTracker copies that run's uid and scan_id into
# ``RE.md``, so every later run's start document carries ``last_dark_flat_uid``
# and ``last_dark_flat_scan_id`` until the next dark/flat replaces them. Both keys
# are absent if no dark/flat has been taken in the session. This replaces the
# xpdDacq "latest dark/flat file with the same exposure" lookup.


# tomo_dark_flat
#
# Start document:
#   plan_name: "tomo_dark_flat"
#   detectors: list[str]
#   exposure_time: dict[str, float] detector name to seconds, an entry for every
#                                    detector; by default the same as the
#                                    projections it serves
#   num_dark_images: int
#   num_flat_images: int
#   sample_out_motor: str            motor moved to take the sample out for flats
#   sample_out_offset: float         relative move of sample_out_motor for flats
# Streams:
#   dark: a single event, photon shutter closed
#     <det>                     (num_dark_images, height, width) image stack, external
#   flat: a single event, sample moved out of the beam, shutter open
#     <det>                     (num_flat_images, height, width) image stack, external
TOMO_DARK_FLAT_V1 = Spec("TomoDarkFlat", version="1")


def validate_tomo_dark_flat(run: RunLike, version: str | None = "1") -> None:
    """Validate ``run`` against :data:`TOMO_DARK_FLAT_V1`.

    Parameters
    ----------
    run : RunLike
        The run to validate.
    version : str | None, default "1"
        The spec version; only "1" exists.

    Raises
    ------
    SpecValidationError
        If the run does not match the spec.
    """
    _check_version("TomoDarkFlat", version)
    _require_plan_name(run, "tomo_dark_flat")
    _require_start_keys(
        run,
        "exposure_time",
        "num_dark_images",
        "num_flat_images",
        "sample_out_motor",
        "sample_out_offset",
    )
    _require_exposure_time(run)
    dets = _names(run, "detectors")
    _require_stream(run, "dark", *dets)
    _require_stream(run, "flat", *dets)


# tomo_flyscan (also the per-tomogram run of tomo_flyscan_loop and the
# tomo_1d/2d/nd_step_scan plans, which open one run per tomogram)
#
# A detector's nproj projections are nproj angular intervals over the rotation range,
# and the scan also images the end of the range, so it writes nproj + 1 images.
# Each detector runs at its own rate. The PandA makes one base pulse per image of the
# detector with the most projections, and captures the rotation angle on every
# base pulse. A detector with trigger_divisor D gets every D-th base pulse, from the
# first, so its trigger k comes at base pulse k * D and its angles are
# rotation_angle[::D]. With images_to_average A, image j averages its triggers
# j * A to (j + 1) * A - 1, and a detector writes nproj + 1 images of A triggers.
#
# Start document:
#   plan_name: "tomo_flyscan"
#   detectors: list[str]
#   detector_configs: dict[str, dict]  detector name to its config, an entry for
#                                    every detector, each with:
#     nproj: int                     projections: angular intervals over the range
#     num_images: int                images written, nproj + 1
#     exposure_time: float           seconds per frame
#     images_to_average: int         frames the Proc plugin combines per image
#     filter_type: str               Proc plugin filter that combined them
#     ttl_out: int                   PandA TTL output that triggered it
#     trigger_divisor: int           base pulses per trigger
#   num_base_pulses: int             base pulses, most projections + 1
#   trigger_period: float            seconds between base pulses
#   time_based: bool
#   start_position, stop_position: float   rotation range, degrees
#   rotation_motor: str
#   sample_name: str                 optional
#   last_dark_flat_uid: str          optional, see the dark/flat convention
#   last_dark_flat_scan_id: int      optional, see the dark/flat convention
#   grid_index: int                  optional, from the step scans: 0-based point index
#   num_grid_points: int             optional, from the step scans: points in the scan
#   grid_positions: dict[str, float] optional, from the step scans: step motor name to
#                                    setpoint; empty when the scan has no step axes
#   grid_reference_positions: dict[str, float]  optional, from relative step scans:
#                                    step motor name to its position when the scan
#                                    started; grid_positions are still absolute
#   previous_grid_point_uid: str     optional, from the step scans: start uid of the
#                                    previous point's successful run; absent on first
# Streams:
#   <det>: one per detector, named after it, collected while flying
#     <det>                     (num_images, ...) images, external
#   rotation: collected while flying
#     rotation_angle            (num_base_pulses,) rotation angle from the PandA,
#                               degrees, external. Other PandA captures enabled in its
#                               settings also appear here.
TOMO_FLYSCAN_V1 = Spec("TomoFlyscan", version="1")

_DETECTOR_CONFIG_KEYS = (
    "nproj",
    "num_images",
    "exposure_time",
    "images_to_average",
    "filter_type",
    "ttl_out",
    "trigger_divisor",
)


def validate_tomo_flyscan(run: RunLike, version: str | None = "1") -> None:
    """Validate ``run`` against :data:`TOMO_FLYSCAN_V1`.

    Parameters
    ----------
    run : RunLike
        The run to validate.
    version : str | None, default "1"
        The spec version; only "1" exists.

    Raises
    ------
    SpecValidationError
        If the run does not match the spec.
    """
    _check_version("TomoFlyscan", version)
    _require_plan_name(run, "tomo_flyscan")
    _require_start_keys(
        run,
        "detector_configs",
        "num_base_pulses",
        "trigger_period",
        "time_based",
        "start_position",
        "stop_position",
        "rotation_motor",
    )
    _require_optional_str(run, "last_dark_flat_uid")
    dets = _names(run, "detectors")
    configs = run.start["detector_configs"]
    if not isinstance(configs, Mapping):
        raise SpecValidationError(
            f"Start document 'detector_configs' must be a mapping, got {configs!r}."
        )
    for det in dets:
        config = configs.get(det)
        if not isinstance(config, Mapping):
            raise SpecValidationError(f"'detector_configs' has no entry for {det!r}.")
        missing = [key for key in _DETECTOR_CONFIG_KEYS if key not in config]
        if missing:
            raise SpecValidationError(
                f"'detector_configs' entry for {det!r} is missing {missing}."
            )
        _require_stream(run, det, det)
    _require_stream(run, "rotation", "rotation_angle")


_VALIDATORS: dict[str, Callable[[RunLike, str | None], None]] = {
    TOMO_DARK_FLAT_V1.name: validate_tomo_dark_flat,
    TOMO_FLYSCAN_V1.name: validate_tomo_flyscan,
}

#: Every spec xpdtools plans write.
SPECS = [TOMO_DARK_FLAT_V1, TOMO_FLYSCAN_V1]


def validate_run(run: RunLike, spec: Spec) -> None:
    """Validate ``run`` against ``spec``.

    Parameters
    ----------
    run : RunLike
        The run to validate.
    spec : Spec
        The spec to validate against.

    Raises
    ------
    SpecValidationError
        If the run does not match the spec.
    KeyError
        If there is no validator for ``spec``'s name.
    """
    try:
        validator = _VALIDATORS[spec.name]
    except KeyError:
        raise KeyError(f"No validator for spec {spec.name!r}.") from None
    validator(run, spec.version)
