"""Campaign management helpers for halide perovskite optimization runs.

Backing module — import these from the notebook or test scripts rather than
calling them from a shell.  Every function is unit-testable without live
hardware.

Typical import in the notebook or a script::

    from xpd_tools.optimization.campaign import (
        save_campaign_config,
        load_campaign_config,
        assert_plan_registered,
        summarize_campaign,
    )
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

    from xpd_tools.optimization.agent import BuildAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def save_campaign_config(
    build_agent: BuildAgent,
    name: str,
    directory: str | Path = "configs",
) -> Path:
    """Archive the campaign config as a timestamped JSON file.

    Writes two copies:

    * ``{directory}/{name}_{timestamp}.json`` — timestamped archive, never
      overwritten, so every run's exact config is preserved for later reload.
    * ``{directory}/{name}.json`` — stable symlink / copy for convenience;
      always points at the most-recent archive.

    Parameters
    ----------
    build_agent:
        Configured ``BuildAgent`` whose ``to_config()`` will be serialised.
    name:
        Base filename (no extension), e.g. ``"halide_beamline"``.
    directory:
        Directory to write into.  Created automatically if it does not exist.

    Returns
    -------
    Path
        Absolute path of the timestamped archive file.

    Example
    -------
    ::

        from xpd_tools.optimization.campaign import save_campaign_config
        cfg_path = save_campaign_config(build_agent, "halide_beamline")
        print("archived to", cfg_path)
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = directory / f"{name}_{timestamp}.json"
    stable_path = directory / f"{name}.json"

    config = build_agent.to_config()  # returns dict when no filename given
    _write_json(config, archive_path)
    _write_json(config, stable_path)

    logger.info("Campaign config saved: %s (stable copy: %s)",
        archive_path,
        stable_path
    )
    print(f"Config archived → {archive_path}")
    print(f"Stable copy     → {stable_path}")
    return archive_path.resolve()


def load_campaign_config(path: str | Path) -> BuildAgent:
    """Rebuild a ``BuildAgent`` from a JSON config saved by ``save_campaign_config``.

    Parameters
    ----------
    path:
        Path to any JSON produced by ``BuildAgent.to_config()`` or
        ``save_campaign_config()``.

    Returns
    -------
    BuildAgent
        Fully reconstructed agent, ready for ``build()`` or
        ``register_acquisition_plans()``.

    Example
    -------
    ::

        from xpd_tools.optimization.campaign import load_campaign_config
        build_agent = load_campaign_config("configs/halide_beamline.json")
    """
    from xpd_tools.optimization.agent import BuildAgent

    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, encoding="utf-8") as f:
        config = json.load(f)

    build_agent = BuildAgent.from_config(config)
    logger.info("Campaign config loaded from %s", path)
    print(f"Config loaded ← {path}")
    return build_agent


def _write_json(data: Any, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# ---------------------------------------------------------------------------
# Queue server connectivity
# ---------------------------------------------------------------------------

def assert_plan_registered(
    run_agent: Any,
    plan_name: str = "uvvis_acquire",
) -> None:
    """Confirm that *plan_name* is registered on the queue-server worker.

    Calls the RE Manager API to list all available plans, prints them, and
    raises ``RuntimeError`` if the expected plan is absent (meaning the
    worker's startup module did not load ``profiles/halide`` correctly).

    Parameters
    ----------
    run_agent:
        ``QueueserverAgent`` returned by ``build_agent.build()``.
    plan_name:
        Plan name to assert; defaults to ``"uvvis_acquire"``.

    Raises
    ------
    RuntimeError
        If *plan_name* is not in the list of available plans.

    Example
    -------
    ::

        from xpd_tools.optimization.campaign import assert_plan_registered
        assert_plan_registered(run_agent, "uvvis_acquire")
    """
    # Access the underlying RE Manager API via the runner's client
    try:
        client = run_agent._runner._client._re_manager_api
        response = client.plans_allowed(user_group="admin")
        available: dict = response.get("plans_allowed", {})
    except Exception as exc:
        raise RuntimeError(
            f"Could not query queue-server plan list: {exc}\n"
            "Is the RE Manager running and reachable at the configured http_server_uri?"
        ) from exc

    plan_names = sorted(available.keys())
    print("Plans registered on the queue-server worker:")
    for name in plan_names:
        marker = " ✓" if name == plan_name else ""
        print(f"  {name}{marker}")

    if plan_name not in available:
        raise RuntimeError(
            f"Plan '{plan_name}' is NOT registered on the queue-server worker.\n"
            "Check that the worker started with:\n"
            "  start-re-manager --startup-module \
            xpd_tools.optimization.profiles.halide\n"
            "and that XPD_HALIDE_CONFIG points at a valid config JSON."
        )

    print(f"\n✓  '{plan_name}' is registered and ready.")


# ---------------------------------------------------------------------------
# Results / export
# ---------------------------------------------------------------------------

def summarize_campaign(run_agent: Any) -> pd.DataFrame:
    """Return a tidy DataFrame of all completed trials from *run_agent*.

    Wraps ``run_agent.ax_client.summarize()`` and exports to
    ``tmp/output/agent_halide_data_beamline.csv``.

    Parameters
    ----------
    run_agent:
        ``QueueserverAgent`` (or ``Agent``) returned by ``build()`` /
        ``build_local()``.

    Returns
    -------
    pd.DataFrame
        All trial results as a flat DataFrame.

    Example
    -------
    ::

        from xpd_tools.optimization.campaign import summarize_campaign
        df = summarize_campaign(run_agent)
        df.head()
    """
    from pathlib import Path

    df = run_agent.ax_client.summarize()

    output_dir = Path("tmp/output")
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "agent_halide_data_beamline.csv"
    df.to_csv(csv_path, index=False)
    print(f"Results exported → {csv_path}  ({len(df)} trials)")
    return df
