"""Acquisition settings for XPD, kept in Redis so every session shares them."""

import math
from collections.abc import MutableMapping
from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class AcquisitionSettings:
    """The acquisition settings that replace xpdAcq's ``glbl`` keys.

    Parameters
    ----------
    frame_acq_time : float
        Detector frame time in seconds (``glbl['frame_acq_time']``).
    dark_window : float
        Minutes for which a dark frame stays valid (``glbl['dk_window']``).
    shutter_control : bool
        Whether plans open the fast shutter only while exposing.
    auto_load_calibration : bool
        Whether the current calibration is added to each run
        (``glbl['auto_load_calib']``).
    """

    frame_acq_time: float
    dark_window: float
    shutter_control: bool
    auto_load_calibration: bool

    def __post_init__(self) -> None:
        """Refuse times that are not positive and finite, and wrong types."""
        for name in ("frame_acq_time", "dark_window"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be a number, not {value!r}")
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be finite and > 0, not {value}")
            object.__setattr__(self, name, float(value))
        for name in ("shutter_control", "auto_load_calibration"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be True or False, not {value!r}")


class AcquisitionConfiguration(StrEnum):
    """Named acquisition presets; plans take the string value."""

    XPD = "xpd"

    @property
    def settings(self) -> AcquisitionSettings:
        """The settings this configuration applies."""
        return _PRESETS[self]


# Defaults of qserver's set_glbl_qserver (xpd-profile-collection 141f81f).
_PRESETS: dict[AcquisitionConfiguration, AcquisitionSettings] = {
    AcquisitionConfiguration.XPD: AcquisitionSettings(
        frame_acq_time=0.5,
        dark_window=1000.0,
        shutter_control=True,
        auto_load_calibration=True,
    ),
}

_NAMES = tuple(field.name for field in fields(AcquisitionSettings))


class PersistentAcquisitionSettings:
    """`AcquisitionSettings` kept in a mapping, one key per setting.

    In a profile the mapping is a ``RedisJSONDict`` on the xpd-tools Redis DB,
    so bsui and the Queue Server share the settings and they survive a restart.
    The mapping is read on every access, so no session holds a stale copy.

    Do not give it the DB that holds ``RE.md``: ``RE.md`` is a ``RedisJSONDict``
    with an empty prefix, so it reads every key in its DB and these settings
    would appear in every start document.

    Parameters
    ----------
    store : MutableMapping[str, Any]
        Where the settings are kept.
    configuration : str, optional
        Preset for any setting the store does not hold yet; default ``"xpd"``.
        Settings already in the store are kept.

    Examples
    --------
    >>> settings = PersistentAcquisitionSettings({})
    >>> settings.update(dark_window=10).dark_window
    10.0
    >>> settings.get().frame_acq_time
    0.5
    """

    def __init__(
        self,
        store: MutableMapping[str, Any],
        configuration: str = AcquisitionConfiguration.XPD,
    ) -> None:
        self._store = store
        preset = asdict(AcquisitionConfiguration(configuration).settings)
        missing = {name: value for name, value in preset.items() if name not in store}
        if missing:
            store.update(missing)

    def get(self) -> AcquisitionSettings:
        """Read the current settings from the store."""
        return AcquisitionSettings(**{name: self._store[name] for name in _NAMES})

    def update(self, **changes: Any) -> AcquisitionSettings:
        """Change the given settings, keep the others, and return them all.

        Raises
        ------
        TypeError
            If a name is not a setting, or a value has the wrong type.
        ValueError
            If a time is not positive.
        """
        settings = replace(self.get(), **changes)
        # Write only what changed, so a setting another session changed since
        # the read above is not put back.
        self._store.update({name: getattr(settings, name) for name in changes})
        return settings

    def use(self, configuration: str) -> AcquisitionSettings:
        """Apply a named preset and return its settings.

        Raises
        ------
        ValueError
            If ``configuration`` names no preset.
        """
        settings = AcquisitionConfiguration(configuration).settings
        self._store.update(asdict(settings))
        return settings

    def __repr__(self) -> str:
        """Show the current settings, as printing ``glbl`` did."""
        return f"{type(self).__name__}({self.get()!r})"
