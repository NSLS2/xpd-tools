"""Utilities shared by the tomography plans."""

from collections.abc import MutableMapping
from typing import Any

from nslsii.ophyd_async.devices import Xspress3Detector
from ophyd_async.epics.adkinetix import KinetixDetector

from xpd_tools.detectors.pilatus4 import Pilatus4Detector

#: Detectors that can take tomography projections.
TomoDetector = Pilatus4Detector | KinetixDetector | Xspress3Detector

#: Detectors that take images, and so need darks and flats.
ImagingDetector = Pilatus4Detector | KinetixDetector

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
