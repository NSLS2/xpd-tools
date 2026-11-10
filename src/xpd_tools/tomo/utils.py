"""Utilities shared by the tomography plans."""

import math
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.epics.adkinetix import KinetixDetector

from xpd_tools.detectors.pilatus4 import Pilatus4Detector

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
