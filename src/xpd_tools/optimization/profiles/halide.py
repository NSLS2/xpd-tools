"""Queue Server worker startup module for halide perovskite UV-Vis campaigns.

Load with::

    start-re-manager --startup-module xpd_tools.optimization.profiles.halide

This module:
1. Reads the experiment config from the JSON written by ``BuildAgent.to_config()``,
   whose path is given by the ``XPD_HALIDE_CONFIG`` environment variable.
2. Instantiates all real ophyd devices via ``build_halide_devices()``.
3. Calls ``BuildAgent.register_acquisition_plans(devices, namespace=globals())``
   which injects the bound plan(s) (e.g. ``uvvis_acquire``) into this module's
   global namespace where the Queue Server process discovers them automatically.

Environment variables
---------------------
XPD_HALIDE_CONFIG  (required)
    Absolute path to the JSON config file produced by ``build_agent.to_config(path)``.

XPD_HALIDE_MOCK  (optional, default "0")
    Set to "1" to instantiate simulated devices instead of live EPICS connections.
    Useful for testing the worker startup without hardware.

XPD_HTTP_API_KEY  (optional)
    HTTP API key for the Queue Server, forwarded to ``BuildAgent.from_config``.

Queue Server permissions
------------------------
Add the following to ``user_group_permissions.yaml`` so the agent may submit
these plans::

    user_groups:
      admin:
        allowed_plans:
          - ":.*_acquire"   # matches uvvis_acquire, xray_acquire, etc.

Or list the specific plans::

    allowed_plans:
      - ":uvvis_acquire"
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Load config
# ---------------------------------------------------------------------------

_config_path = os.environ.get("XPD_HALIDE_CONFIG")
if not _config_path:
    raise RuntimeError(
        "XPD_HALIDE_CONFIG environment variable is not set. "
        "Point it at the JSON file written by BuildAgent.to_config()."
    )

_config_path = Path(_config_path).expanduser().resolve()
if not _config_path.exists():
    raise FileNotFoundError(
        f"XPD_HALIDE_CONFIG={_config_path} does not exist."
    )

logger.info("Loading halide experiment config from %s", _config_path)
with open(_config_path, encoding="utf-8") as _f:
    _config = json.load(_f)

# ---------------------------------------------------------------------------
# 2. Rebuild the BuildAgent from the config (single source of truth)
# ---------------------------------------------------------------------------

from xpd_tools.optimization.agent import BuildAgent  # noqa: E402

_http_api_key = os.environ.get("XPD_HTTP_API_KEY")
build_agent = BuildAgent.from_config(_config, http_api_key=_http_api_key)

# ---------------------------------------------------------------------------
# 3. Instantiate devices
# ---------------------------------------------------------------------------

from xpd_tools.optimization.devices import build_halide_devices  # noqa: E402

_mock = os.environ.get("XPD_HALIDE_MOCK", "0").strip().lower() in ("1", "true", "yes")
if _mock:
    logger.warning("XPD_HALIDE_MOCK is set -- \
        using simulated devices, NOT real hardware.")

devices = build_halide_devices(mock=_mock)

# ---------------------------------------------------------------------------
# 4. Register acquisition plan(s) into this module's globals
# ---------------------------------------------------------------------------

_registered = build_agent.register_acquisition_plans(devices, namespace=globals())

logger.info(
    "Registered acquisition plans: %s",
    ", ".join(_registered.keys()),
)

# ---------------------------------------------------------------------------
# Convenience: also expose individual devices as module-level names so that
# interactive users can import or inspect them directly if needed.
# ---------------------------------------------------------------------------

qepro = devices["qepro"]
led = devices["led"]
uv_shutter = devices["uv_shutter"]
fast_shutter = devices["fast_shutter"]

dds1_p1 = devices.get("dds1_p1")
dds1_p2 = devices.get("dds1_p2")
dds2_p1 = devices.get("dds2_p1")
dds2_p2 = devices.get("dds2_p2")
dds3_p1 = devices.get("dds3_p1")
dds3_p2 = devices.get("dds3_p2")
ultra1 = devices.get("ultra1")
ultra2 = devices.get("ultra2")
