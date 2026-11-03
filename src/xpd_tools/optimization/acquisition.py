"""Reusable RunEngine factory with NSLS-II Tiled write wiring.

This mirrors the Tiled / RunEngine setup in the beamline's 00-startup.py so
both the queue-server worker and the local-fallback path use identical wiring.

Typical use::

    from xpd_tools.optimization.acquisition import build_acquisition_run_engine
    from tiled.client import from_profile
    import os

    writing_client = from_profile(
        "nsls2", api_key=os.environ["TILED_BLUESKY_WRITING_API_KEY_XPD"]
    )["xpd"]["raw"]
    writing_client.context.http_client.headers["tiled-qos"] = "acquisition"

    RE = build_acquisition_run_engine(
        data_session="pass-123456",
        writing_client=writing_client,
    )
"""

from __future__ import annotations

import logging
from typing import Any

from bluesky.run_engine import RunEngine
from bluesky_tiled_plugins import TiledWriter

logger = logging.getLogger(__name__)


def build_acquisition_run_engine(
    *,
    data_session: str,
    writing_client: Any,
    extra_md: dict | None = None,
) -> Any:
    """Build a Bluesky RunEngine wired for NSLS-II Tiled data writing.

    Sets the standard NSLS-II RunEngine metadata (``facility``, ``group``,
    ``beamline_id``, ``data_session``) and subscribes a ``TiledWriter`` backed
    by ``writing_client``, mirroring ``00-startup.py`` lines 67-112.

    Parameters
    ----------
    data_session:
        Active proposal identifier, e.g. ``"pass-314159"``.  Required by the
        NSLS-II Tiled write policy; raises ``ValueError`` when falsy.
    writing_client:
        An authenticated, QoS-tagged Tiled catalog client pointing at the
        write node (e.g. ``from_profile("nsls2", api_key=...)["xpd"]["raw"]``
        with ``headers["tiled-qos"] = "acquisition"`` already set).
    extra_md:
        Optional additional key/value pairs merged into ``RE.md`` after the
        standard NSLS-II entries.

    Returns
    -------
    bluesky.run_engine.RunEngine
        Ready-to-use RunEngine with the TiledWriter subscribed.

    Raises
    ------
    ValueError
        If ``data_session`` is empty or None.
    """
    if not data_session:
        raise ValueError(
            "data_session is required for NSLS-II Tiled writes. "
            "Pass the active proposal identifier, e.g. 'pass-314159'."
        )

    RE = RunEngine()

    # Standard NSLS-II metadata (mirrors 00-startup.py:110-112)
    RE.md["facility"] = "NSLS-II"
    RE.md["group"] = "XPD"
    RE.md["beamline_id"] = "28-ID-2"
    RE.md["data_session"] = data_session

    if extra_md:
        RE.md.update(extra_md)

    RE.subscribe(TiledWriter(writing_client))
    logger.info(
        "RunEngine wired: data_session=%s, writing to %r",
        data_session,
        writing_client,
    )
    return RE
