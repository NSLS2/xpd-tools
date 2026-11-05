"""What the data-acquisition GUI shows at XPD-D (28-ID-2).

Everything XPD-D-specific about the GUI lives here; ``xpd_tools.gui`` is generic.
Launch with ``python -m xpd_tools.gui`` (or the ``xpdd-gui`` pixi task).
"""

from __future__ import annotations

import os
from pathlib import Path

from ophyd_async.epics.adcore import NDStatsIO
from ophyd_async.epics.motor import Motor
from ophyd_async.fastcs.panda import HDFPanda

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.gui.spec import DetectorView, GuiSpec, Site, Technique
from xpd_tools.motors import RotationMotor
from xpd_tools.plans import single_axis_flyscan

MOCK_ENV = "XPD_TOOLS_RUNNING_IN_CI"


def _is_mock() -> bool:
    return os.environ.get(MOCK_ENV, "false").lower() == "true"


def _default_plan_history_file() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    )
    filename = "plan_history_mock.json" if _is_mock() else "plan_history.json"
    return state_home / "xpd_tools" / filename


XPDD_GUI = GuiSpec(
    name="XPD-D",
    techniques=[
        Technique(
            "Flyscan",
            plans=[("Single Axis Flyscan", single_axis_flyscan)],
            detectors=[
                DetectorView(
                    "pilatus1",
                    "XF:28ID2-ES{Pilatus4-Det:1}",
                    {"show_profile_lines": True, "preserve_aspect_ratio": True},
                ),
            ],
        ),
    ],
    # Names must match those defined in the profile / Queue Server namespace.
    expected_devices=[
        ("panda1", HDFPanda),
        ("rot_motor", RotationMotor),
        ("step_motor", Motor),
        ("pilatus1", Pilatus4Detector),
        ("pilatus1_stats1", NDStatsIO),
    ],
    logger_name="xpd_tools",
    site=Site(
        "Upton, NY", latitude=40.8677, longitude=-72.8814, timezone="America/New_York"
    ),
    profile_dir=Path(__file__).resolve().parent / "profiles",
    default_profile="xpdd",
    mock_environment={MOCK_ENV: "true"},
    plan_history_env="XPD_TOOLS_PLAN_HISTORY_FILE",
    default_plan_history_file=_default_plan_history_file,
    about_packages=(
        "xpd-tools",
        "bluesky",
        "ophyd-async",
        "tiled",
        "bluesky-tiled-plugins",
    ),
    settings_scope=("NSLS2", "xpd-tools-gui"),
)
