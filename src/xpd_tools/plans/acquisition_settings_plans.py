"""Plans that change XPD's acquisition settings."""

import bluesky.plan_stubs as bps

from xpd_tools.acquisition_settings import AcquisitionConfiguration, AcquisitionSettings


def use_acquisition_configuration(
    settings: AcquisitionSettings, configuration: str = "xpd"
):
    """Set every acquisition setting from a named preset.

    Parameters
    ----------
    settings : AcquisitionSettings
        The settings device to change.
    configuration : str, optional
        Value of an `AcquisitionConfiguration` member; default is ``"xpd"``.
        A string, because the Queue Server refuses a plan whose default is an
        Enum member.

    Raises
    ------
    ValueError
        If ``configuration`` names no preset.
    """
    preset = AcquisitionConfiguration(configuration).preset
    yield from bps.mv(
        settings.frame_acq_time,
        preset.frame_acq_time,
        settings.dark_window,
        preset.dark_window,
        settings.shutter_control,
        preset.shutter_control,
        settings.auto_load_calibration,
        preset.auto_load_calibration,
    )
