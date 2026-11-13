"""Utilities shared by the tomography plans."""

import math
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.epics.adcore import AreaDetector, NDProcessFilterType
from ophyd_async.epics.adkinetix import KinetixDetector
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.flyers import (
    PANDA_DIV_BLOCKS,
    PANDA_TTL_OUTPUTS,
    TtlRoute,
    check_position_triggers,
    default_ttl_out,
)
from xpd_tools.utils import get_obj_from_ipython_ns

#: Detectors that can take tomography projections.
TomoDetector = Pilatus4Detector | KinetixDetector | Xspress3Detector

#: Detectors that take images, and so need darks and flats.
ImagingDetector = Pilatus4Detector | KinetixDetector

#: An exposure time in seconds: one number for every detector, or a mapping that
#: gives every detector its own, keyed by detector or by its ``.name``.
ExposureTime = float | Mapping[TomoDetector | str, float]


def resolve_exposure_times(
    detectors: Sequence[TomoDetector], exposure_time: ExposureTime
) -> dict[TomoDetector, float]:
    """Return each detector's exposure time.

    String keys match a detector's ophyd ``.name`` (its data key in Tiled), not the
    variable it is bound to in the IPython namespace.

    Parameters
    ----------
    detectors : Sequence[TomoDetector]
        The detectors of the scan.
    exposure_time : ExposureTime
        A number, in seconds, for every detector, or a mapping that gives every
        detector its exposure time, keyed by detector or detector name.

    Returns
    -------
    dict[TomoDetector, float]
        An exposure time for every detector, in the order of ``detectors``.

    Raises
    ------
    ValueError
        If two detectors share a name, a key matches no detector, two keys match
        the same detector, a detector has no exposure time, or an exposure time is
        not a positive number.

    Examples
    --------
    >>> class Det:  # stands in for a detector; only its name is read
    ...     def __init__(self, name):
    ...         self.name = name
    >>> pil, iris = Det("pilatus1"), Det("iris1")
    >>> times = resolve_exposure_times([pil, iris], {pil: 0.002, "iris1": 0.01})
    >>> [times[pil], times[iris]]
    [0.002, 0.01]
    >>> list(resolve_exposure_times([pil, iris], 0.1).values())
    [0.1, 0.1]
    """
    by_name: dict[str, TomoDetector] = {}
    for det in detectors:
        if det.name in by_name:
            raise ValueError(f"Two detectors are named {det.name!r}.")
        by_name[det.name] = det

    if not isinstance(exposure_time, Mapping):
        times: dict[TomoDetector, Any] = dict.fromkeys(detectors, exposure_time)
    else:
        given: dict[str, Any] = {}
        for key, value in exposure_time.items():
            name = key if isinstance(key, str) else getattr(key, "name", None)
            det = by_name.get(name) if isinstance(name, str) else None
            if det is None or (not isinstance(key, str) and key is not det):
                raise ValueError(
                    f"{name or key!r} in exposure_time is not one of the detectors' "
                    f"names: {list(by_name)}."
                )
            if det.name in given:
                raise ValueError(f"exposure_time gives {det.name!r} more than once.")
            given[det.name] = value
        missing = [name for name in by_name if name not in given]
        if missing:
            raise ValueError(f"exposure_time has no exposure time for {missing}.")
        times = {det: given[det.name] for det in detectors}

    for det, value in times.items():
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        if not (is_number and math.isfinite(value) and value > 0):
            raise ValueError(
                f"Exposure time of {det.name} must be a positive number, got {value!r}."
            )
    return {det: float(value) for det, value in times.items()}


def exposure_md(times: Mapping[TomoDetector, float]) -> dict[str, float]:
    """Return ``times`` keyed by detector name, for a start document.

    Parameters
    ----------
    times : Mapping[TomoDetector, float]
        Exposure times, from :func:`resolve_exposure_times`.

    Returns
    -------
    dict[str, float]
        Exposure time in seconds by detector name.
    """
    return {det.name: float(value) for det, value in times.items()}


#: Name of the flyscan stream holding the PandA's captures, ``rotation_angle``.
ROTATION_STREAM = "rotation"


class DetectorConfig(BaseModel):
    """How one detector takes part in a tomography scan.

    Pass a list of these to the tomo plans. A plain dict with the same keys works
    too, which is what a GUI or the Queue Server sends.

    Attributes
    ----------
    detector : TomoDetector
        The detector. A string is looked up by that name in the IPython namespace.
    nproj : int
        The number of projections: the angular intervals the detector covers over
        the scan range. A flyscan also takes an image at the end of the range (0 and
        360 degrees are the same view), so it writes ``nproj + 1`` images, see
        :attr:`num_images`.
    exposure_time : float
        The exposure time of each frame, in seconds.
    ttl_out : int, optional
        The PandA TTL output wired to the detector's trigger input, one of
        :data:`xpd_tools.flyers.PANDA_TTL_OUTPUTS`. Defaults to the output its kind
        of detector is wired to, :data:`~xpd_tools.flyers.DEFAULT_TTL_OUT`; pass it
        to override.
    images_to_average : int, default 1
        The number of frames the Proc plugin combines into each image. Only
        detectors with a ``proc`` plugin support more than 1.
    filter_type : NDProcessFilterType, default NDProcessFilterType.AVERAGE
        How the Proc plugin combines the averaged frames.

    Examples
    --------
    ``DetectorConfig(detector=pilatus1, nproj=1800, exposure_time=0.002)``, or the
    same as a dict: ``{"detector": "pilatus1", "nproj": 1800, "exposure_time":
    0.002}``. Add ``ttl_out=2`` to override the default TTL output.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra="forbid")

    detector: TomoDetector
    nproj: PositiveInt
    exposure_time: PositiveFloat
    # _default_ttl_out always replaces this placeholder; 0 is not a wired output, so
    # validate_detector_configs would refuse it if it ever leaked through.
    ttl_out: int = Field(default=0)
    images_to_average: PositiveInt = 1
    filter_type: NDProcessFilterType = NDProcessFilterType.AVERAGE

    @staticmethod
    def _find_detector(value: Any) -> Any:
        """Find a detector given by name in the IPython namespace."""
        if not isinstance(value, str):
            return value
        found = get_obj_from_ipython_ns(value, AreaDetector)
        if found is None:
            raise ValueError(
                f"No detector is named {value!r} in the IPython namespace."
            )
        return found

    @field_validator("detector", mode="before")
    @classmethod
    def _look_up_detector(cls, value: Any) -> Any:
        return cls._find_detector(value)

    @model_validator(mode="before")
    @classmethod
    def _default_ttl_out(cls, data: Any) -> Any:
        """Fill a missing ``ttl_out`` from the detector's default wiring."""
        if not isinstance(data, Mapping) or data.get("ttl_out") is not None:
            return data
        ttl_out = default_ttl_out(cls._find_detector(data.get("detector")))
        if ttl_out is None:
            raise ValueError(
                f"{type(data.get('detector')).__name__} has no default TTL output; "
                "pass ttl_out."
            )
        return {**data, "ttl_out": ttl_out}

    @property
    def num_images(self) -> int:
        """The images a flyscan writes: ``nproj`` plus one at the end of the range."""
        return self.nproj + 1

    @property
    def num_triggers(self) -> int:
        """The triggers the detector needs: its images times frames averaged."""
        return self.num_images * self.images_to_average

    @property
    def num_intervals(self) -> int:
        """The gaps between the detector's triggers, one fewer than its triggers."""
        return self.num_triggers - 1

    @property
    def name(self) -> str:
        """The detector's name."""
        return self.detector.name


#: A detector config, or a dict of its fields.
DetectorConfigLike = DetectorConfig | Mapping[str, Any]


class TriggerRouting(BaseModel):
    """The PandA triggers a set of detector configs needs.

    Attributes
    ----------
    num_base_pulses : int
        The number of base pulses (``PULSE1.OUT``): one more than the most projections
        any detector takes, as the last pulse comes at the end of the range. The
        PandA captures the rotation angle on every one.
    routes : list[TtlRoute]
        The TTL route of each detector config, in the configs' order.
    """

    model_config = ConfigDict(frozen=True)

    num_base_pulses: int
    routes: list[TtlRoute]


def to_detector_configs(
    configs: Sequence[DetectorConfigLike],
) -> list[DetectorConfig]:
    """Convert dicts to :class:`DetectorConfig`, keeping configs as they are.

    Parameters
    ----------
    configs : Sequence[DetectorConfigLike]
        Detector configs or dicts of their fields.

    Returns
    -------
    list[DetectorConfig]
        The configs, in order.
    """
    return [
        config
        if isinstance(config, DetectorConfig)
        else DetectorConfig.model_validate(config)
        for config in configs
    ]


def validate_detector_configs(
    configs: Sequence[DetectorConfigLike],
    start: float | None = None,
    stop: float | None = None,
    encoder_resolution: float | None = None,
) -> TriggerRouting:
    """Check that detector configs work together, and route their triggers.

    The detector(s) needing the most triggers get the base pulses directly. Every
    other detector gets its own PandA DIV block, dividing the base pulses by the
    ratio of the projection counts, so each detector gets exactly the ``nproj + 1``
    triggers it needs, all on base pulses. Changes no hardware.

    Parameters
    ----------
    configs : Sequence[DetectorConfigLike]
        The detector configs of one scan.
    start : float | None, default None
        The starting angle of the scan. With ``stop`` and ``encoder_resolution``,
        also checks the scan against the encoder counts (see Raises).
    stop : float | None, default None
        The stopping angle of the scan.
    encoder_resolution : float | None, default None
        The degrees per encoder count of the rotation motor.

    Returns
    -------
    TriggerRouting
        The base pulse count and each config's TTL route.

    Raises
    ------
    ValueError
        If there are no configs or more than the PandA has TTL outputs for, a
        detector appears twice, two detectors share a name or one is named
        :data:`ROTATION_STREAM`, two configs share a TTL output or one is not wired,
        the most projections are not a multiple of every detector's projections, there
        are more divided detectors than DIV blocks, or a detector averages frames
        without a Proc plugin. If ``start``, ``stop`` and ``encoder_resolution`` are
        given, also if the base pulses don't fit the encoder counts of the scan: the
        travel is not a whole number of counts per base step, or is under two counts
        per step. Position based scans only; ``start`` equal to ``stop`` is refused.
    """
    given = [v is not None for v in (start, stop, encoder_resolution)]
    if any(given) and not all(given):
        raise ValueError("start, stop and encoder_resolution must be given together.")
    configs = to_detector_configs(configs)
    if not 1 <= len(configs) <= len(PANDA_TTL_OUTPUTS):
        raise ValueError(
            f"A flyscan takes 1 to {len(PANDA_TTL_OUTPUTS)} detector configs, got "
            f"{len(configs)}."
        )

    names: list[str] = []
    ttls: list[int] = []
    for i, config in enumerate(configs):
        if any(config.detector is other.detector for other in configs[:i]):
            raise ValueError(f"{config.name} has more than one detector config.")
        if config.name in names:
            raise ValueError(f"Two detectors are named {config.name!r}.")
        if config.name == ROTATION_STREAM:
            raise ValueError(
                f"A detector can't be named {ROTATION_STREAM!r}, the name of the "
                "rotation stream."
            )
        if config.ttl_out not in PANDA_TTL_OUTPUTS:
            raise ValueError(
                f"{config.name} is on TTL output {config.ttl_out}, which is not one "
                f"of the wired outputs {list(PANDA_TTL_OUTPUTS)}."
            )
        if config.ttl_out in ttls:
            other = configs[ttls.index(config.ttl_out)].name
            raise ValueError(
                f"{config.name} and {other} are both on TTL output {config.ttl_out}."
            )
        if config.images_to_average > 1 and not hasattr(config.detector, "proc"):
            raise ValueError(
                f"{config.name} averages {config.images_to_average} frames, which "
                "needs a Proc plugin it lacks."
            )
        names.append(config.name)
        ttls.append(config.ttl_out)

    # Triggers are fence posts: N triggers are N - 1 gaps, and every detector's
    # gaps must tile the fastest one's, so all land on base pulses.
    max_nproj = max(config.num_intervals for config in configs)
    num_base_pulses = max_nproj + 1
    uneven = [
        f"{config.name} ({config.num_intervals})"
        for config in configs
        if max_nproj % config.num_intervals
    ]
    if uneven:
        raise ValueError(
            f"The most projections, {max_nproj}, must be a multiple of every "
            f"detector's projections (the gaps between its triggers); "
            f"{', '.join(uneven)} are not."
        )

    if all(given):
        travel_counts = abs(round((stop - start) / encoder_resolution))  # type: ignore
        check_position_triggers(travel_counts, num_base_pulses)

    # DIV blocks go to the divided detectors, the most triggers first.
    divided = sorted(
        (i for i, config in enumerate(configs) if config.num_intervals < max_nproj),
        key=lambda i: -configs[i].num_intervals,
    )
    if len(divided) > len(PANDA_DIV_BLOCKS):
        raise ValueError(
            f"{len(divided)} detectors need divided triggers, but the PandA has only "
            f"{len(PANDA_DIV_BLOCKS)} DIV blocks."
        )
    div_blocks = dict(zip(divided, PANDA_DIV_BLOCKS, strict=False))
    routes = [
        TtlRoute(
            ttl_out=config.ttl_out,
            divisor=max_nproj // config.num_intervals,
            div_block=div_blocks.get(i),
        )
        for i, config in enumerate(configs)
    ]
    return TriggerRouting(num_base_pulses=num_base_pulses, routes=routes)


#: Start document keys that link a run to the last dark/flat, set by
#: :class:`LastDarkFlatUIDTracker`.
DARK_FLAT_MD_KEYS = ("last_dark_flat_uid", "last_dark_flat_scan_id")


class LastDarkFlatUIDTracker:
    """RunEngine subscription that remembers the last dark/flat run.

    On the start document of a ``tomo_dark_flat`` run, it copies the run's uid and
    scan_id into the metadata dict (normally ``RE.md``), so every later run's start
    document links to it.

    Parameters
    ----------
    metadata_dict : MutableMapping[str, Any]
        The metadata to write into, normally ``RE.md``.

    Examples
    --------
    >>> md = {}
    >>> tracker = LastDarkFlatUIDTracker(md)
    >>> tracker("start", {"uid": "abc", "scan_id": 7, "plan_name": "tomo_dark_flat"})
    >>> md
    {'last_dark_flat_uid': 'abc', 'last_dark_flat_scan_id': 7}
    """

    def __init__(self, metadata_dict: MutableMapping[str, Any]):
        self._metadata_dict = metadata_dict

    def __call__(self, name: str, doc: dict[str, Any]) -> None:
        """Record the uid and scan_id of a ``tomo_dark_flat`` start document."""
        if (
            name == "start"
            and doc.get("uid")
            and doc.get("plan_name") == "tomo_dark_flat"
        ):
            uid_key, scan_id_key = DARK_FLAT_MD_KEYS
            self._metadata_dict[uid_key] = doc["uid"]
            self._metadata_dict[scan_id_key] = doc.get("scan_id")


def clear_last_dark_flat(metadata_dict: MutableMapping[str, Any]) -> None:
    """Forget the last dark/flat, so later runs link none until a new one is taken.

    Parameters
    ----------
    metadata_dict : MutableMapping[str, Any]
        The metadata the tracker writes into, normally ``RE.md``.

    Examples
    --------
    >>> md = {"last_dark_flat_uid": "abc", "last_dark_flat_scan_id": 7, "x": 1}
    >>> clear_last_dark_flat(md)
    >>> md
    {'x': 1}
    """
    for key in DARK_FLAT_MD_KEYS:
        metadata_dict.pop(key, None)
