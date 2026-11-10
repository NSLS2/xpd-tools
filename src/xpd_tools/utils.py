"""General utility functions for xpdtools."""

import os
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any, TypeVar

from bluesky.run_engine import RunEngine
from IPython.core.getipython import get_ipython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from IPython.terminal.prompts import Prompts
from nslsii.sync_experiment import sync_experiment
from nslsii.utils import open_redis_client
from pygments.token import Token
from redis_json_dict.redis_json_dict import RedisJSONDict
from rich import print as rprint


def print_version_info():
    """Print version information for bluesky, ophyd_async, tiled, and xpdtools."""
    from bluesky import __version__ as bluesky_version
    from ophyd_async import __version__ as ophyd_async_version
    from tiled import __version__ as tiled_version

    from xpd_tools import __version__ as xpd_tools_version

    rprint("\n[bold]Version Information[/bold]")
    rprint(f"  [bold]bluesky[/bold]: [blue]{bluesky_version}[/blue]")
    rprint(f"  [bold]ophyd_async[/bold]: [blue]{ophyd_async_version}[/blue]")
    rprint(f"  [bold]tiled[/bold]: [blue]{tiled_version}[/blue]")
    rprint(f"  [bold]xpd-tools[/bold]: [blue]{xpd_tools_version}[/blue]\n")


def show_docs(name: str, doc: dict[str, Any]):
    """Print out the bluesky documents in a readable format."""
    rprint(f"------- {name} ---------")
    rprint(doc)


class ProposalIDPrompt(Prompts):
    """Custom IPython prompt that shows the current proposal ID."""

    def __init__(self, RE: RunEngine, shell: TerminalInteractiveShell):
        super().__init__(shell)
        self._RE = RE

    def in_prompt_tokens(self, cli=None):
        return [
            (
                Token.Prompt,
                f"{self._RE.md.get('data_session', 'N/A')} [",
            ),
            (Token.PromptNum, str(self.shell.execution_count)),
            (Token.Prompt, "]: "),
        ]


def initialize_run_engine() -> RunEngine:
    """Initialize the bluesky RunEngine with appropriate metadata."""
    if os.environ.get("XPDTOOLS_RUNNING_IN_CI", "false").lower() == "true":
        return RunEngine(
            {
                "data_session": "pass-123456",
                "cycle": (
                    f"{datetime.today().year}-{int(datetime.today().month / 4) + 1}"
                ),
            }
        )
    return RunEngine(
        RedisJSONDict(open_redis_client(redis_ssl=True), "")  # type: ignore (TODO: Loosen type of RE.md to Mapping from dict)
    )


def start_beamtime(proposal_id: int, verbose: bool = True) -> None:
    """Start a beamtime for the given proposal ID."""
    with open(os.devnull, "w") as f, redirect_stdout(f):
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


NSVarT = TypeVar("NSVarT")


def get_obj_from_ipython_ns(var_name: str, var_type: type[NSVarT]) -> NSVarT | None:
    """Get an object of a given type from the IPython namespace, if it exists.

    Parameters
    ----------
    var_name : str
        The name of the object in the IPython namespace.
    var_type : type[NSVarT]
        The expected type of the object.

    Returns
    -------
    NSVarT | None
        The object if it exists in the IPython namespace and is of the correct
        type, otherwise None.
    """
    ipython = get_ipython()
    if ipython is not None:
        obj = ipython.user_ns.get(var_name)
        if isinstance(obj, var_type):
            return obj
    return None


def ensure_available(
    var_type: type[NSVarT], value: NSVarT | None, ns_name: str
) -> NSVarT:
    """Return a device passed to a plan, or find it in the IPython namespace.

    Plans take their supporting devices (PandA, rotation motor, shutters) as
    optional arguments that default to None. A None argument is looked up in the
    IPython namespace under ``ns_name``, the name the profile gives the device
    (e.g. ``panda1`` or ``amPhi``).

    Parameters
    ----------
    var_type : type[NSVarT]
        The expected type of the object.
    value : NSVarT | None
        The object passed to the plan, or None to look it up.
    ns_name : str
        The name to look up in the IPython namespace.

    Returns
    -------
    NSVarT
        The object if it is available locally or in the IPython namespace.

    Raises
    ------
    ValueError
        If the object is not available locally or in the IPython namespace.
    TypeError
        If the provided value is not of the expected type or None.
    """
    if isinstance(value, var_type):
        return value
    if value is not None:
        raise TypeError(
            f"{ns_name} must be of type {var_type.__name__} or None, "
            f"is {type(value).__name__}"
        )
    found = get_obj_from_ipython_ns(ns_name, var_type)
    if found is not None:
        return found
    raise ValueError(
        f"No {var_type.__name__} was passed, and none is named {ns_name!r} in the "
        "IPython namespace."
    )
