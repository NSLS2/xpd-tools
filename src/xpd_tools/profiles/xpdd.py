"""XPD-D Bluesky profile for the 28-ID-2 beamline at NSLS-II."""

import os

# Remove PYEPICS_LIBCA set by the conda epics-base activation script.
# It points to conda's libca.so which conflicts with epicscorelibs' libca.so
# (used by aioca/ophyd-async), causing PV connections to fail and the process
# to hang on exit with "double free or corruption".
os.environ.pop("PYEPICS_LIBCA", None)

from datetime import datetime

import bluesky.plan_stubs as bps
import bluesky.plans as bp
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.run_engine import RunEngine, call_in_bluesky_event_loop
from bluesky.utils import ProgressBarManager
from bluesky_tiled_plugins import BufferingWrapper, TiledWriter
from IPython.core.getipython import get_ipython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from nslsii.ophyd_async.providers import NSLS2PathProvider
from nslsii.sync_experiment import sync_experiment
from ophyd_async.core import (
    init_devices,
)
from ophyd_async.epics.adcore import ADWriterFactory, NDStatsIO, PluginSignalDataLogic
from ophyd_async.epics.adkinetix import KinetixDetector
from ophyd_async.epics.motor import Motor as AsyncEpicsMotor
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.plan_stubs import retrieve_settings, store_settings
from rich import print as rprint
from tiled.client import from_uri

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.motors import RotationMotor
from xpd_tools.plans import *
from xpd_tools.psylotech import Psylotech
from xpd_tools.utils import (
    ProposalIDPrompt,
    initialize_run_engine,
    show_docs,
)


def start_beamtime(proposal_id: int, verbose: bool = True) -> None:
    """Start a beamtime for the given proposal ID."""
    md = sync_experiment(proposal_id, "XPD", redis_ssl=True)

    rprint(
        f"Started beamtime for proposal ID [bold][blue]{proposal_id}[/blue][/bold].\n"
    )
    rprint(
        "Current time: [italic]"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/italic]\n"
    )
    proposal_md = md.get("proposal", {})
    if proposal_md and verbose:
        rprint(f"Proposal title: [italic]{proposal_md['title']}[/italic]\n")
        rprint(
            f"Proposal type: [italic]{proposal_md['type']}[/italic], "
            f"Proposal PI: [italic]{proposal_md['pi_name']}[/italic]\n"
        )


# Allow for using env var to run in simulation mode
XPDTOOLS_RUNNING_IN_CI = (
    os.environ.get("XPDTOOLS_RUNNING_IN_CI", "false").lower() == "true"
)

TILED_BACKUP_ROOT = "/nsls2/data/xpd/xpdd/shared/config/bluesky/tiled_backup"
TILED_LOCAL_BACKUP_ROOT = "/tmp/bluesky-docs"

os.environ["REDIS_HOST"] = "xf28id2-xpdd-redis1.nsls2.bnl.gov"
os.environ["OPHYD_ASYNC_PRESERVE_DETECTOR_STATE"] = "YES"

rprint("\n[bold][blue]Starting the XPDD profile collection...[/blue][/bold]")

# Create our RunEngine instance
RE: RunEngine = initialize_run_engine()
rprint("Initialzied RunEngine...\n")

# Set some metadata that never changes.
RE.md["facility"] = "NSLS-II"
RE.md["group"] = "XPD-D"
RE.md["beamline_id"] = "28-ID-2"

# Display current proposal information, if set
rprint("[bold][underline]Current Proposal Information[/underline][/bold]\n")
_proposal_md = RE.md.get("proposal", {})
if _proposal_md:
    rprint(
        "[bold]  - Title: [green]"
        f"[italic]{_proposal_md['title']}[/italic][/green][/bold]"
    )
    rprint(f"[bold]  - Type:  [italic]{_proposal_md['type']}[/italic][/bold]")
    rprint(f"[bold]  - PI:    [italic]{_proposal_md['pi_name']}[/italic][/bold]\n")
else:
    rprint("[bold][red]No active proposal![/red][/bold]\n")
rprint(
    "[bold]To switch proposals, use start_beamtime(123456) replacing the number "
    "with your proposal ID.[/bold]\n"
)
del _proposal_md

# Setup terminal progress bars
RE.waiting_hook = ProgressBarManager()  # type: ignore[assignment]

if not XPDTOOLS_RUNNING_IN_CI:
    # Initialize our tiled writing client
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
    # When testing, spin up an in-process tiled server, and use that instead
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

# Setup BEC for live table, header, and live plots
bec = BestEffortCallback()
RE.subscribe(bec)

# Setup the proposal ID prompt, as well as the autoawait magic
# Allows for calling `await coro()` directly in the IPython shell
ipython = get_ipython()
if ipython is not None and isinstance(ipython, TerminalInteractiveShell):
    ipython.prompts = ProposalIDPrompt(RE, ipython)
    ipython.run_line_magic("autoawait", "call_in_bluesky_event_loop")
    # ipython.run_line_magic("matplotlib", "")
    if not XPDTOOLS_RUNNING_IN_CI:
        tiled_reading_client = c = from_uri("https://tiled.nsls2.bnl.gov")["xpdd"][
            "migration"
        ]


path_provider = NSLS2PathProvider(
    RE.md,  # type: ignore (nslsii types metadata_dict as dict; RE.md is a RedisJSONDict)
    beamline_tla="xpd",
    beamline_tla_suffix="-new",
)

# Created before the devices are, so it keeps the name pilatus1 gives its plugins
# (pilatus1-stats1) rather than a variable name, and is typed for the logic below
pilatus1_stats1 = NDStatsIO("XF:28ID2-ES{Pilatus4-Det:1}Stats1:")

with init_devices(mock=XPDTOOLS_RUNNING_IN_CI):
    # Create a PandABox ophyd object
    panda1 = HDFPanda("XF:28ID2-ES{PANDA:1}:", path_provider)

    # Create the rotation motor ophyd object for flyscanning
    # amPhi = RotationMotor(  # noqa: N816
    #     "XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr", name="amPhi", panda_encoder=1
    # )

    # For non-rotation motors (i.e. any other motors) use the `AsyncEpicsMotor` class
    # step_motor = AsyncEpicsMotor(  # noqa: N816
    #     "XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr", name="mPhi2"
    # )

    # The beamline names these motors in mixedCase, which the users rely on
    amBaseX = AsyncEpicsMotor(  # noqa: N816
        "XF:28IDD-ES:2{Stg:Stack-Ax:Xbase}Mtr", name="amBaseX"
    )
    amBaseY = AsyncEpicsMotor(  # noqa: N816
        "XF:28IDD-ES:2{Stg:Stack-Ax:Y}Mtr", name="amBaseY"
    )
    amPhi = RotationMotor(  # noqa: N816
        "XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr", name="amPhi", panda_encoder=1
    )
    amTopX = AsyncEpicsMotor(  # noqa: N816
        "XF:28IDD-ES:2{Stg:Stack-Ax:Xfine}Mtr", name="amTopX"
    )
    amTopZ = AsyncEpicsMotor(  # noqa: N816
        "XF:28IDD-ES:2{Stg:Stack-Ax:Z}Mtr", name="amTopZ"
    )

    # Initialize the Pilatus4 detector object, with an HDF file writer and the
    # stats plugin
    pilatus1 = Pilatus4Detector(
        "XF:28ID2-ES{Pilatus4-Det:1}",
        ADWriterFactory.hdf(path_provider),
        plugins={"stats1": pilatus1_stats1},
    )
    # Tell the detector to include stats plugin total reading in step scans
    pilatus1.add_detector_logics(
        PluginSignalDataLogic(pilatus1.driver, pilatus1_stats1.total)
    )

    # Fly-scan Pilatus  no stats plugin / data logic (practical at ~450 Hz)
    # TODO: Remove this once we get new ophyd-async release. Flyscan will auto-disable
    # stepscan only data logic
    pilatus1_fly = Pilatus4Detector(
        "XF:28ID2-ES{Pilatus4-Det:1}", ADWriterFactory.hdf(path_provider)
    )
    pilatus1_fly.set_name("pilatus1")  # reuse assets/pilatus1/ per DE

    iris1 = KinetixDetector(
        "XF:28IDD-ES{Iris-Det:1}",
        ADWriterFactory.hdf(path_provider),
    )

    psylo = Psylotech("XF:28ID2-ES{Psylotech:1}", name="psylo")
