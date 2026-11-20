"""Acquisition settings for XPD, held in a device that plans read and users move."""

from dataclasses import dataclass
from enum import StrEnum

from ophyd_async.core import StandardReadable, soft_signal_rw
from ophyd_async.core import StandardReadableFormat as Format


@dataclass(frozen=True)
class AcquisitionPreset:
    """Values that an `AcquisitionConfiguration` puts on `AcquisitionSettings`.

    Parameters
    ----------
    frame_acq_time : float
        Detector frame time in seconds.
    dark_window : float
        Minutes for which a dark frame stays valid.
    shutter_control : bool
        Whether plans open the fast shutter only while exposing.
    auto_load_calibration : bool
        Whether the current calibration is added to each run.
    """

    frame_acq_time: float
    dark_window: float
    shutter_control: bool
    auto_load_calibration: bool

    def __post_init__(self) -> None:
        """Refuse non-positive times."""
        if self.frame_acq_time <= 0:
            raise ValueError(f"frame_acq_time must be > 0 s, not {self.frame_acq_time}")
        if self.dark_window <= 0:
            raise ValueError(f"dark_window must be > 0 min, not {self.dark_window}")


class AcquisitionConfiguration(StrEnum):
    """Named acquisition presets; plans take the string value."""

    XPD = "xpd"

    @property
    def preset(self) -> AcquisitionPreset:
        """The values this configuration sets."""
        return _PRESETS[self]


# Defaults of qserver's set_glbl_qserver (xpd-profile-collection 141f81f).
_PRESETS: dict[AcquisitionConfiguration, AcquisitionPreset] = {
    AcquisitionConfiguration.XPD: AcquisitionPreset(
        frame_acq_time=0.5,
        dark_window=1000.0,
        shutter_control=True,
        auto_load_calibration=True,
    ),
}


class AcquisitionSettings(StandardReadable):
    """Acquisition settings that replace xpdAcq's ``glbl``.

    Change a value with ``bps.mv(settings.frame_acq_time, 0.1)``, or apply a
    preset with `xpd_tools.plans.use_acquisition_configuration`. The values are
    configuration signals, so a run that reads this device records them.

    Parameters
    ----------
    configuration : AcquisitionConfiguration, optional
        Preset for the starting values; default is ``XPD``.
    name : str, optional
        Device name propagated to its signals; default is empty.

    Attributes
    ----------
    frame_acq_time : SignalRW[float]
        Detector frame time in seconds.
    dark_window : SignalRW[float]
        Minutes for which a dark frame stays valid.
    shutter_control : SignalRW[bool]
        Whether plans open the fast shutter only while exposing.
    auto_load_calibration : SignalRW[bool]
        Whether the current calibration is added to each run.
    """

    def __init__(
        self,
        configuration: AcquisitionConfiguration = AcquisitionConfiguration.XPD,
        name: str = "",
    ) -> None:
        preset = configuration.preset
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.frame_acq_time = soft_signal_rw(
                float, preset.frame_acq_time, units="s"
            )
            self.dark_window = soft_signal_rw(float, preset.dark_window, units="min")
            self.shutter_control = soft_signal_rw(bool, preset.shutter_control)
            self.auto_load_calibration = soft_signal_rw(
                bool, preset.auto_load_calibration
            )
        super().__init__(name=name)
