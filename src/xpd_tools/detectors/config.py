"""Per-detector settings for a scan, and the checks they must pass together.

A scan's detectors are given as one list whose entries are a bare detector, a
:class:`DetConfig`, or a dict with the same fields. A dict is what the Queue
Server and the GUI send: the Queue Server converts device names to devices in
dict values, but not in dict keys, so the detector is a value.
"""

from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from math import isclose, isfinite
from typing import Any

from ophyd_async.core import Device
from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt

#: Most detectors a scan can use.
MAX_DETECTORS = 3


class DetConfig(BaseModel):
    """What a user chooses for one detector in a scan.

    The plan builds the detector's full trigger settings from these, so a user
    cannot enter values that contradict the scan.

    Parameters
    ----------
    detector : Device
        The detector.
    exposure : float
        Exposure time of each frame, in seconds.
    collections_per_event : int, optional
        Frames the detector takes per event of the scan, that is per frame of
        the slowest detector; ophyd-async's ``TriggerInfo.collections_per_event``.
        Default 1.

    Examples
    --------
    >>> from ophyd_async.core import Device
    >>> config = DetConfig(detector=Device(name="pilatus1"), exposure=0.01)
    >>> config.exposure, config.collections_per_event
    (0.01, 1)
    """

    model_config = ConfigDict(
        allow_inf_nan=False,
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
    )

    detector: Device
    exposure: PositiveFloat
    collections_per_event: PositiveInt = 1


def as_det_configs(
    entries: Iterable[Device | DetConfig | Mapping[str, Any]],
    default_exposure: float | None = None,
) -> list[DetConfig]:
    """Turn each entry into a :class:`DetConfig`, in order.

    Parameters
    ----------
    entries : Iterable[Device | DetConfig | Mapping[str, Any]]
        Bare detectors, :class:`DetConfig` objects, or dicts with the fields of
        :class:`DetConfig`.
    default_exposure : float, optional
        Exposure time, in seconds, for each bare detector.

    Returns
    -------
    list[DetConfig]
        One :class:`DetConfig` per entry.

    Raises
    ------
    ValueError
        If a bare detector is given without ``default_exposure``, or an entry
        is not a valid :class:`DetConfig` (pydantic's ``ValidationError`` is a
        ``ValueError``).
    TypeError
        If an entry is none of the accepted types.

    Examples
    --------
    >>> from ophyd_async.core import Device
    >>> pil, xs3 = Device(name="pilatus1"), Device(name="xspress3-1")
    >>> configs = as_det_configs([pil, {"detector": xs3, "exposure": 0.1}], 0.01)
    >>> [(c.detector.name, c.exposure) for c in configs]
    [('pilatus1', 0.01), ('xspress3-1', 0.1)]
    """
    configs = []
    for entry in entries:
        if isinstance(entry, DetConfig):
            configs.append(entry)
        elif isinstance(entry, Mapping):
            configs.append(DetConfig.model_validate(entry))
        elif isinstance(entry, Device):
            if default_exposure is None:
                raise ValueError(
                    f"{entry.name!r} is given without settings and there is no "
                    "default exposure."
                )
            configs.append(DetConfig(detector=entry, exposure=default_exposure))
        else:
            raise TypeError(
                f"{entry!r} is not a detector, a DetConfig or a dict of its fields."
            )
    return configs


def check_det_configs(
    configs: Sequence[DetConfig],
    period: float | None = None,
    deadtimes: Mapping[Device, float] | None = None,
    max_detectors: int = MAX_DETECTORS,
) -> None:
    """Check that the detectors' settings work together in one scan.

    Every problem found is reported at once, so a form can show them all.

    Parameters
    ----------
    configs : Sequence[DetConfig]
        The scan's detectors and their settings.
    period : float, optional
        Time between events of the scan, in seconds; finite and above 0. If
        given, each detector's frames must fit in it: ``collections_per_event *
        (exposure + deadtime) <= period``.
    deadtimes : Mapping[Device, float], optional
        Each detector's deadtime in seconds, finite and 0 or more, used with
        ``period``; a missing detector counts as 0.
    max_detectors : int, optional
        Most detectors allowed. Default :data:`MAX_DETECTORS`.

    Raises
    ------
    ValueError
        Listing every problem: no detectors, too many, a detector given twice
        (or two with the same name), frame counts that are not whole multiples
        of each other, an invalid period or deadtime, or frames that do not fit
        the period.

    Examples
    --------
    >>> from ophyd_async.core import Device
    >>> pil, xs3 = Device(name="pilatus1"), Device(name="xspress3-1")
    >>> check_det_configs(  # doctest: +ELLIPSIS
    ...     [
    ...         DetConfig(detector=pil, exposure=0.01, collections_per_event=2),
    ...         DetConfig(detector=xs3, exposure=0.01, collections_per_event=3),
    ...     ]
    ... )
    Traceback (most recent call last):
    ...
    ValueError: Detector settings do not work together:
    - collections_per_event 2 does not divide 3; ...
    """
    problems = []
    if not configs:
        problems.append("no detectors are given.")
    if len(configs) > max_detectors:
        problems.append(f"{len(configs)} detectors are given; at most {max_detectors}.")

    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    for config in configs:
        det = config.detector
        if id(det) in seen_ids:
            problems.append(f"{det.name!r} is given more than once.")
        elif det.name in seen_names:
            problems.append(f"two detectors are named {det.name!r}.")
        seen_ids.add(id(det))
        seen_names.add(det.name)

    # Each smaller count dividing the next makes every count a multiple of every
    # smaller one.
    counts = sorted({config.collections_per_event for config in configs})
    for smaller, larger in pairwise(counts):
        if larger % smaller:
            problems.append(
                f"collections_per_event {smaller} does not divide {larger}; each "
                "detector's frame count must be a whole multiple of the others'."
            )

    if period is not None and not (isfinite(period) and period > 0):
        problems.append(f"period must be a finite number above 0, not {period!r}.")
    elif period is not None:
        deadtimes = deadtimes or {}
        for config in configs:
            deadtime = deadtimes.get(config.detector, 0.0)
            if not (isfinite(deadtime) and deadtime >= 0):
                problems.append(
                    f"{config.detector.name!r} deadtime must be a finite number of "
                    f"0 or more, not {deadtime!r}."
                )
                continue
            needed = config.collections_per_event * (config.exposure + deadtime)
            if needed > period and not isclose(needed, period):
                problems.append(
                    f"{config.detector.name!r} needs {needed:g} s per event "
                    f"({config.collections_per_event} x ({config.exposure:g} s "
                    f"exposure + {deadtime:g} s deadtime)) but the period is "
                    f"{period:g} s."
                )

    if problems:
        raise ValueError(
            "Detector settings do not work together:\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
