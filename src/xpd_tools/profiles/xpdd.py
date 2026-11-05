"""XPD-D Bluesky profile for the 28-ID-2 beamline at NSLS-II."""

import os

import bluesky.plan_stubs as bps
import bluesky.plans as bp
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.run_engine import RunEngine, call_in_bluesky_event_loop
from bluesky.utils import ProgressBarManager
from bluesky_tiled_plugins import BufferingWrapper, TiledWriter
from IPython.core.getipython import get_ipython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from nslsii.ophyd_async.providers import NSLS2PathProvider
from ophyd_async.core import (
    init_devices,
)
from ophyd_async.epics.adcore import ADWriterFactory, NDStatsIO, PluginSignalDataLogic
from ophyd_async.epics.motor import Motor as AsyncEpicsMotor
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.plan_stubs import store_settings
from tiled.client import from_uri

from xpd_tools.detectors.panda import PandAConfiguration, switch_panda_configuration
from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.motors import RotationMotor
from xpd_tools.plans import single_axis_flyscan
from xpd_tools.utils import ProposalIDPrompt, initialize_run_engine

XPD_TOOLS_RUNNING_IN_CI = (
    os.environ.get("XPD_TOOLS_RUNNING_IN_CI", "false").lower() == "true"
)

TILED_BACKUP_ROOT = "/nsls2/data/xpd/xpdd/shared/config/bluesky/tiled_backup"
TILED_LOCAL_BACKUP_ROOT = "/tmp/bluesky-docs"

os.environ["REDIS_HOST"] = "xf28id2-xpdd-redis1.nsls2.bnl.gov"
os.environ["OPHYD_ASYNC_PRESERVE_DETECTOR_STATE"] = "YES"

# Create our RunEngine instance
RE: RunEngine = initialize_run_engine()

# Set some metadata that never changes.
RE.md["facility"] = "NSLS-II"
RE.md["group"] = "XPD-D"
RE.md["beamline_id"] = "28-ID-2"

# Setup progress bars
RE.waiting_hook = ProgressBarManager()  # type: ignore[assignment]


if not XPD_TOOLS_RUNNING_IN_CI:
    tiled_writing_client = from_uri(
        "https://tiled.nsls2.bnl.gov",
        api_key=os.environ.get("TILED_BLUESKY_WRITING_API_KEY_XPDD", ""),
    )["xpdd"]["migration"]
    tw = BufferingWrapper(
        TiledWriter(
            tiled_writing_client, backup_directory=f"{TILED_BACKUP_ROOT}/migration"
        )
    )
else:
    from tiled.client import simple

    tiled_reading_client = tiled_writing_client = c = simple(
        # Tiled refuses API keys that are not alphanumeric.
        "/tmp/xpd_tools",
        api_key="xpdtools",
        readable_storage="/tmp/xpd_tools",
    )
    tw = BufferingWrapper(
        TiledWriter(
            tiled_writing_client,
            backup_directory=f"{TILED_LOCAL_BACKUP_ROOT}/migration",
        )
    )
RE.subscribe(tw)

# Setup BEC
bec = BestEffortCallback()
RE.subscribe(bec)

ipython = get_ipython()
if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
    ipython.prompts = ProposalIDPrompt(RE, ipython)
    ipython.run_line_magic("autoawait", "call_in_bluesky_event_loop")
    if not XPD_TOOLS_RUNNING_IN_CI:
        tiled_reading_client = c = from_uri("https://tiled.nsls2.bnl.gov")["xpdd"][
            "migration"
        ]


path_provider = NSLS2PathProvider(
    RE.md,  # type: ignore (nslsii types metadata_dict as dict; RE.md is a RedisJSONDict)
    beamline_tla="xpd",
    beamline_tla_suffix="-new",
)

with init_devices(mock=XPD_TOOLS_RUNNING_IN_CI):
    panda1 = HDFPanda("XF:28ID2-ES{PANDA:1}:", path_provider)
    rot_motor = RotationMotor("XF:28IDD-ES:2{Twister}Mtr")
    step_motor = AsyncEpicsMotor("XF:28IDD-ES:2{Twister}Mtr", name="amazon_motor")
    pilatus1_stats1 = NDStatsIO("XF:28ID2-ES{Pilatus4-Det:1}Stats1:")
    pilatus1 = Pilatus4Detector(
        "XF:28ID2-ES{Pilatus4-Det:1}",
        ADWriterFactory.hdf(path_provider),
        plugins={"stats1": pilatus1_stats1},
    )
    pilatus1.add_detector_logics(
        PluginSignalDataLogic(pilatus1.driver, pilatus1.stats1.total)  # type: ignore
    )
