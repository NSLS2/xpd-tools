"""Plans that change XPD's acquisition settings."""

from collections.abc import Callable, Generator
from typing import Any

import bluesky.plan_stubs as bps

from xpd_tools.acquisition_settings import PersistentAcquisitionSettings


def acquisition_settings_plans(
    settings: PersistentAcquisitionSettings,
) -> tuple[Callable[..., Generator], Callable[..., Generator]]:
    """Make the plans that change ``settings``, for bsui and the Queue Server.

    The Queue Server passes only devices by name, so a profile binds the plans
    to its settings once::

        use_acquisition_configuration, set_acquisition_settings = (
            acquisition_settings_plans(settings)
        )

    Parameters
    ----------
    settings : PersistentAcquisitionSettings
        The settings the plans change.

    Returns
    -------
    tuple
        ``use_acquisition_configuration`` and ``set_acquisition_settings``.
    """

    def use_acquisition_configuration(configuration: str = "xpd") -> Generator:
        """Set every acquisition setting from a named preset.

        Parameters
        ----------
        configuration : str, optional
            Value of an `AcquisitionConfiguration` member; default ``"xpd"``.
            A string, because the Queue Server refuses a plan whose default is
            an Enum member.

        Raises
        ------
        ValueError
            If ``configuration`` names no preset.
        """
        settings.use(configuration)
        yield from bps.null()

    def set_acquisition_settings(
        frame_acq_time: float | None = None,
        dark_window: float | None = None,
        shutter_control: bool | None = None,
        auto_load_calibration: bool | None = None,
    ) -> Generator:
        """Change the acquisition settings that are given; keep the others.

        Parameters
        ----------
        frame_acq_time : float, optional
            Detector frame time in seconds.
        dark_window : float, optional
            Minutes for which a dark frame stays valid.
        shutter_control : bool, optional
            Whether plans open the fast shutter only while exposing.
        auto_load_calibration : bool, optional
            Whether the current calibration is added to each run.

        Raises
        ------
        TypeError, ValueError
            If a value has the wrong type or a time is not positive; nothing
            is changed.
        """
        given: dict[str, Any] = {
            "frame_acq_time": frame_acq_time,
            "dark_window": dark_window,
            "shutter_control": shutter_control,
            "auto_load_calibration": auto_load_calibration,
        }
        settings.update(
            **{name: value for name, value in given.items() if value is not None}
        )
        yield from bps.null()

    return use_acquisition_configuration, set_acquisition_settings
