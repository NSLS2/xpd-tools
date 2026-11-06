"""XPD-D Bluesky profile for the 28-ID-2 beamline at NSLS-II."""

import os
from pathlib import Path, PurePosixPath

from ophyd_async.epics.adkinetix import KinetixDetector
import bluesky.plan_stubs as bps
import bluesky.plans as bp
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.run_engine import RunEngine, autoawait_in_bluesky_event_loop, call_in_bluesky_event_loop
from bluesky.utils import ProgressBarManager
from bluesky_tiled_plugins import BufferingWrapper, TiledWriter
from IPython.core.getipython import get_ipython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from nslsii.ophyd_async.providers import NSLS2PathProvider
from nslsii.utils import print_version_info, print_proposal_info, ProposalIDPrompt
from nslsii.ophyd_async.devices import Xspress3Detector, Xspress3HDFWriterFactory, Xspress3TriggerMode
from ophyd_async.core import (
    init_devices,
    print_connection_status
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
from xpd_tools.utils import initialize_run_engine
from bluesky import preprocessors as bpp

# Use rich for pretty printing in the terminal
from rich import print

XPD_TOOLS_RUNNING_IN_CI = (
    os.environ.get("XPD_TOOLS_RUNNING_IN_CI", "false").lower() == "true"
)

TILED_BACKUP_ROOT = (
    "/nsls2/data/xpd-new/xpdd/shared/config/bluesky/tiled_backup"
    if not XPD_TOOLS_RUNNING_IN_CI else "/tmp/bluesky-docs"
)

os.environ["REDIS_HOST"] = "xf28id2-xpdd-redis1.nsls2.bnl.gov"
os.environ["OPHYD_ASYNC_PRESERVE_DETECTOR_STATE"] = "YES"


print_version_info(additional_packages=["xpd_tools"])

# Create our RunEngine instance
RE: RunEngine = initialize_run_engine()

# Set some metadata that never changes.
RE.md["facility"] = "NSLS-II"
RE.md["group"] = "XPD-D"
RE.md["beamline_id"] = "28-ID-2"

print("\nActive Proposal:\n")
print_proposal_info(RE.md)

# Setup progress bars
RE.waiting_hook = ProgressBarManager()  # type: ignore[assignment]

ipython = get_ipython()
if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
    ipython.prompts = ProposalIDPrompt(RE, ipython)
    autoawait_in_bluesky_event_loop()

        # Configure plots to work interactively
    from matplotlib import pyplot as plt

    plt.ion()

    # Make tracebacks minimal by default to reduce clutter.
    # Use %tb verbose to view full traceback.
    ipython.run_line_magic("xmode", "minimal")

if not XPD_TOOLS_RUNNING_IN_CI:
    tiled_writing_client = from_uri(
        "https://tiled.nsls2.bnl.gov",
        api_key=os.environ.get("TILED_BLUESKY_WRITING_API_KEY_XPDD", ""),
    )["xpdd"]["migration"]
    if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
        tiled_reading_client = c = from_uri("https://tiled.nsls2.bnl.gov")["xpdd"][
            "migration"
        ]
else:
    from tiled.client import simple

    tiled_reading_client = tiled_writing_client = c = simple(
        "/tmp/xpd_tools",
        api_key="xpdtools",
        readable_storage="/tmp/xpd_tools",
    )

tw = BufferingWrapper(
    TiledWriter(
        tiled_writing_client,
        backup_directory=f"{TILED_BACKUP_ROOT}/migration",
    )
)
RE.subscribe(tw)

# Setup BEC
bec = BestEffortCallback()
# Disable bec printout of baseline readings
# since it is kind of noisy
bec.disable_baseline()
RE.subscribe(bec)


path_provider = NSLS2PathProvider(
    RE.md,
    base_data_dir=PurePosixPath("/nsls2/data/xpd-new/proposals"),
)

print("Initializing devices...\n")

with init_devices(mock=XPD_TOOLS_RUNNING_IN_CI, timeout=2.0, on_connect_hook=print_connection_status):
    panda1 = HDFPanda("XF:28ID2-ES{PANDA:1}:", path_provider)
    amBaseX  = AsyncEpicsMotor("XF:28IDD-ES:2{Stg:Stack-Ax:Xbase}Mtr", name="amBaseX")
    amBaseY  = AsyncEpicsMotor("XF:28IDD-ES:2{Stg:Stack-Ax:Y}Mtr", name="amBaseY")
    amPhi    = RotationMotor("XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr", name="amPhi", panda_encoder=1)
    amTopX   = AsyncEpicsMotor("XF:28IDD-ES:2{Stg:Stack-Ax:Xfine}Mtr", name="amTopX") 
    amTopZ   = AsyncEpicsMotor("XF:28IDD-ES:2{Stg:Stack-Ax:Xfine}Mtr", name="amTopZ")
    step_motor = AsyncEpicsMotor("XF:28IDD-ES:2{Twister}Mtr", name="amazon_motor")

    pilatus_stats1 = NDStatsIO("XF:28ID2-ES{Pilatus4-Det:1}Stats1:")
    pilatus = Pilatus4Detector(
        "XF:28ID2-ES{Pilatus4-Det:1}",
        ADWriterFactory.hdf(path_provider, hinted=False),
        plugins={"stats1": pilatus_stats1},
        name="pilatus1"
    )
    pilatus.add_detector_logics(
        PluginSignalDataLogic(pilatus.driver, pilatus_stats1.total)
    )

    iris = KinetixDetector(
        "XF:28IDD-ES{Iris-Det:1}",
        ADWriterFactory.hdf(path_provider, hinted=False),
        name="iris1"
    )

    xspress3 = Xspress3Detector(
        "XF:28ID2-ES{Xsp:1}:",
        Xspress3HDFWriterFactory(path_provider, hinted_streams=()),
        name="xspress3-1"
    )


# Configure baseline supplemental data to include in the metadata of every run.
sd = bpp.SupplementalData(
    baseline=[
        amBaseX,
        amBaseY,
        amPhi,
        amTopX,
        amTopZ,
    ]
)
RE.preprocessors.append(sd)