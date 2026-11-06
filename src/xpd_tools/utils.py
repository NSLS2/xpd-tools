"""General utility functions for xpdtools."""

import os
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any

from bluesky.run_engine import RunEngine
from nslsii.sync_experiment import sync_experiment
from nslsii.utils import open_redis_client, print_proposal_info
from redis_json_dict.redis_json_dict import RedisJSONDict

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

    print(
        f"Started beamtime for proposal ID [bold][blue]{proposal_id}[/blue][/bold].\n"
    )
    print(
        "Current time: [italic]"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/italic]\n"
    )
    print_proposal_info(md)