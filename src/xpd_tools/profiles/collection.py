"""Bluesky profile for the 28-ID-2 XPD beamline at NSLS-II."""

import argparse
import os
from pathlib import (
    Path,  # noqa: F401 - operator namespace
    PureWindowsPath,
)

from bluesky import plan_stubs as bps  # noqa: F401 - operator namespace
from bluesky import plans as bp  # noqa: F401 - operator namespace
from bluesky import preprocessors as bpp
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.plan_stubs import mv, mvr  # noqa: F401 - operator namespace
from bluesky.run_engine import (
    RunEngine,
    autoawait_in_bluesky_event_loop,
    call_in_bluesky_event_loop,  # noqa: F401 - operator namespace
)
from bluesky.utils import ProgressBarManager  # noqa: F401 - operator namespace
from bluesky_tiled_plugins import BufferingWrapper, TiledWriter
from IPython.core.getipython import get_ipython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from nslsii.ophyd_async.providers import NSLS2PathProvider
from nslsii.utils import ProposalIDPrompt, print_proposal_info, print_version_info
from ophyd_async.core import UUIDFilenameProvider, YMDPathProvider, init_devices, logger, print_connection_status
from ophyd_async.epics.adcore import (
    ADWriterFactory,
    NDStatsIO,
    PluginSignalDataLogic,
)
from ophyd_async.epics.adkinetix import (
    KinetixDetector,  # noqa: F401 - operator namespace
)
from ophyd_async.epics.advimba import VimbaDetector
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.sim import SimMotor
from tiled.client import from_uri, simple

from xpd_tools.utils import initialize_run_engine

parser = argparse.ArgumentParser(description="HEX Tools Collection Profile")
parser.add_argument(
  "--mock", action="store_true", help="Mock all beamline devices"
)

MOCK = parser.parse_args().mock

# Where mock detectors write their files, instead of the proposal directories.
MOCK_DETECTOR_DATA_DIR = "/tmp/xpd_tools_dets"

# Where TiledWriter backs up documents it could not write to Tiled.
if MOCK:
    TILED_BACKUP_ROOT = "/tmp/bluesky-docs"
else:
    TILED_BACKUP_ROOT = "/nsls2/data/xpd-new/shared/config/bluesky/tiled_backup"

# Environment variables for Redis host and ophyd_async detector state preservation
os.environ["REDIS_HOST"] = "xf28id2-xpd-redis1.nsls2.bnl.gov"
os.environ["OPHYD_ASYNC_PRESERVE_DETECTOR_STATE"] = "YES"

# Print version information for bluesky, ophyd_async, tiled, and hextools.
print_version_info(additional_packages=["xpd_tools"])


# Setup the RunEngine and its metadata.
if MOCK:
    from datetime import datetime

    RE: RunEngine = RunEngine(
        {
            "data_session": "pass-123456",
            "cycle": (
                f"{datetime.today().year}-{int(datetime.today().month / 4) + 1}"
            ),
        }
    )
else:
    RE = initialize_run_engine()
RE.md["facility"] = "NSLS-II"
RE.md["group"] = "XPD"
RE.md["beamline_id"] = "28-ID-2"

# Uncomment if running in the terminal to get progress bars.
RE.waiting_hook = ProgressBarManager()  # type: ignore[assignment]

# Display active proposal information in the console.
print_proposal_info(RE.md)

# Get the IPython shell and set up the custom prompt to show the current proposal ID.
ipython = get_ipython()
if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
    ipython.prompts = ProposalIDPrompt(RE, ipython)
    ipython.xmode = "Minimal"
    autoawait_in_bluesky_event_loop()

    # Configure plots to work interactively
    from matplotlib import pyplot as plt

    plt.ion()

    # Make tracebacks minimal by default to reduce clutter.
    # Use %tb verbose to view full traceback.
    ipython.run_line_magic("xmode", "minimal")


# Construct our tiled clients for writing and (in an interactive session) reading.
# If we're running in CI, we use a simple client for both.
if not MOCK:
    tiled_writing_client = from_uri(
        "https://tiled.nsls2.bnl.gov",
        api_key=os.environ.get("TILED_BLUESKY_WRITING_API_KEY_HEX", ""),
    )["hex"]["raw"]
    if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
        tiled_reading_client = c = from_uri("https://tiled.nsls2.bnl.gov")["hex"]["raw"]
else:
    tiled_writing_client = tiled_reading_client = c = simple(
        readable_storage=MOCK_DETECTOR_DATA_DIR
    )

tw = BufferingWrapper(
    TiledWriter(tiled_writing_client, backup_directory=f"{TILED_BACKUP_ROOT}/sql")
)

RE.subscribe(tw)

# Subscribe the best effort callback, for its plots. The GUI's live table and plan
# narration already cover what its table and heading would print.
bec = BestEffortCallback()
RE.subscribe(bec)

# Define our global default path provider for the beamline
if MOCK:
    path_provider = YMDPathProvider(UUIDFilenameProvider(), MOCK_DETECTOR_DATA_DIR)
else:
    path_provider = NSLS2PathProvider(RE.md)

# For the Perkin-Elmer detector, we use a separate path provider
# Since we need to specify the write directory as a windows path.
pe_path_provider = (
    path_provider
    if MOCK
    else NSLS2PathProvider(
        RE.md,
        base_write_dir=PureWindowsPath("J:\\proposals"),
    )
)

print("\nInitializing devices...\n")

with init_devices(
    mock=MOCK, timeout=2.0, on_connect_hook=print_connection_status
):

    perkin_elmer = PerkinElmerDetector(
        "XF:27ID1-ES{PE-Det:1}",
        pe_path_provider,
        name="perkin-elmer",
        proc_suffix="Proc1:",
    )

    # A few simulated motors, useful for testing
    sim_x = SimMotor(name="sim_x", instant=False)
    sim_y = SimMotor(name="sim_y", instant=False)
    sim_z = SimMotor(name="sim_z", instant=False)


# Pause the RunEngine if the beam current drops below 100 mA and resume above
# 390 mA. Always installed; it ignores low beam while the ring is in maintenance
# or shutdown, checking the mode each time rather than once at startup.
RE.install_suspender(
    SuspendFloorUnlessOpsMode(
        storage_ring.beam_current,
        100,
        resume_thresh=390,
        ops_mode=storage_ring.operating_mode,
        skip_modes=(NSLS2OpsMode.MAINTENANCE, NSLS2OpsMode.SHUTDOWN),
    )
)

# Configure baseline supplemental data to include in the metadata of every run.
sd = bpp.SupplementalData(
    baseline=[
        storage_ring.beam_current,
    ]
)
RE.preprocessors.append(sd)

# Disable bec printout of baseline readings
# since it is kind of noisy
# bec.disable_baseline()