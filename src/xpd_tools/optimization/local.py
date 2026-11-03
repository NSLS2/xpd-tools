"""Local-RunEngine fallback for halide perovskite UV-Vis campaigns.

Provides ``LocalCampaign``: an in-process twin of ``build()`` +
``profiles/halide.py`` that runs acquisition plans directly against a local
Bluesky RunEngine instead of dispatching them through the queue server.

Use this when the queue-server HTTP frontend is unavailable (e.g. expired TLS
certificate).  Switch back to ``build()`` / ``profiles/halide.py`` once the
queue server is healthy.

Typical use::

    import json, os
    from xpd_tools.optimization.local import LocalCampaign

    cfg = json.load(open(os.environ["XPD_HALIDE_CONFIG"]))
    campaign = LocalCampaign.from_config(
        cfg,
        data_session="pass-314159",  # active NSLS-II proposal
    )

    # Validate hardware before moving anything
    report = campaign.check_connections()
    print(report)

    # !! Disconnect DDS/Ultra syringes from drive screws first !!
    campaign.single_trial({
        "infusion_rate_CsPb": 20.0,
        "infusion_rate_Br":   20.0,
        "infusion_rate_Cl":   20.0,
        "infusion_rate_I":    20.0,
    })

    # Full campaign
    campaign.run()
    df = campaign.summarize()

Environment variables
---------------------
TILED_BLUESKY_WRITING_API_KEY_XPD
    Tiled write API key.  Required when not passed explicitly via
    ``LocalCampaign.from_config(..., tiled_api_key=...)``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tiled.client import from_profile, from_uri

from xpd_tools.optimization.acquisition import build_acquisition_run_engine
from xpd_tools.optimization.campaign import summarize_campaign
from xpd_tools.optimization.devices import build_halide_devices, check_halide_connections

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


class LocalCampaign:
    """In-process twin of ``build()`` + ``profiles/halide.py``.

    Wires a local Bluesky RunEngine against live (or mocked) halide devices,
    writing acquired documents to Tiled and reading them back for evaluation —
    all without a queue server.

    Attributes
    ----------
    RE : bluesky.run_engine.RunEngine
        RunEngine with NSLS-II metadata and TiledWriter subscribed.
    run_agent : blop.ax.agent.Agent
        The optimiser agent returned by ``BuildAgent.build_local()``.
    devices : dict[str, Any]
        Live (or mocked) ophyd device instances keyed by pump-id / role.
    build_agent : BuildAgent
        The ``BuildAgent`` used to construct this campaign.
    writing_client : Any
        Authenticated Tiled write node (``["xpd"]["raw"]`` or configured path).
    reading_client : Any
        Tiled read node — the same catalog node as ``writing_client`` so the
        evaluator can look up runs by UID after they are written.
    """

    def __init__(
        self,
        build_agent: Any,
        *,
        data_session: str,
        mock: bool = False,
        tiled_api_key: str | None = None,
    ) -> None:
        if build_agent.queue_server:
            raise ValueError(
                "LocalCampaign requires queue_server=False on the BuildAgent. "
                "Use BuildAgent.build() for the queue-server path."
            )
        if not data_session:
            raise ValueError(
                "data_session is required (e.g. 'pass-314159'). "
                "NSLS-II Tiled write policy rejects writes without it."
            )

        self.build_agent = build_agent
        self._data_session = data_session
        self._mock = mock

        # -- Tiled wiring -----------------------------------------------------
        # Resolve the API key: explicit arg > env var.
        key = tiled_api_key or build_agent.tiled_api_key

        # Writing client: authenticated, QoS-tagged, same node the evaluator reads.
        if build_agent.tiled_uri:
            _write_root = from_uri(build_agent.tiled_uri, api_key=key)
        else:
            from xpd_tools.optimization.cli import DEFAULT_TILED_PROFILE
            _write_root = from_profile(
                build_agent.tiled_profile or DEFAULT_TILED_PROFILE, api_key=key
            )
        writing_client = _write_root
        for segment in build_agent.tiled_catalog_path:
            writing_client = writing_client[segment]
        writing_client.context.http_client.headers["tiled-qos"] = "acquisition"
        self.writing_client = writing_client

        # Reading client: same catalog node, unauthenticated read (or keyed if needed).
        reading_client = build_agent._tiled_client()
        self.reading_client = reading_client

        # -- RunEngine --------------------------------------------------------
        self.RE = build_acquisition_run_engine(
            data_session=data_session,
            writing_client=writing_client,
        )

        # -- Devices ----------------------------------------------------------
        self.devices = build_halide_devices(mock=mock)

        # -- run_agent --------------------------------------------------------
        from xpd_tools.optimization.legacy.devices import identity_wrap_xray_run

        local_cfg = build_agent.run_settings.local
        self.run_agent = build_agent.build_local(
            devices=self.devices,
            wrap_xray_run=identity_wrap_xray_run,
            mixer_lengths_cm=local_cfg.mixer_lengths_cm,
            residence_time_ratio=local_cfg.residence_time_ratio,
            tiled_client=reading_client,
            sandbox_client=None,  # UV-Vis only; pdfstream not needed
        )
        build_agent.configure_generation_strategy(self.run_agent)

        logger.info(
            "LocalCampaign ready: data_session=%s, mock=%s, "
            "tiled_catalog_path=%s",
            data_session,
            mock,
            build_agent.tiled_catalog_path,
        )

    # -------------------------------------------------------------------------
    # Constructors
    # -------------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        data_session: str,
        mock: bool = False,
        tiled_api_key: str | None = None,
    ) -> "LocalCampaign":
        """Build a ``LocalCampaign`` from a ``to_config()``-shaped dict.

        Parameters
        ----------
        config:
            Dict as produced by ``BuildAgent.to_config()`` or loaded from its
            JSON file.  ``connection.queue_server`` is overridden to ``False``
            so the config can be shared with the queue-server path without
            editing.
        data_session:
            Active NSLS-II proposal identifier (e.g. ``"pass-314159"``).
        mock:
            When ``True`` use simulated devices; real EPICS connections otherwise.
        tiled_api_key:
            Override the ``TILED_BLUESKY_WRITING_API_KEY_XPD`` env var.
        """
        import copy

        from xpd_tools.optimization.agent import BuildAgent

        cfg = copy.deepcopy(dict(config))
        cfg["connection"]["queue_server"] = False  # force local path

        build_agent = BuildAgent.from_config(cfg, tiled_api_key=tiled_api_key)
        return cls(
            build_agent,
            data_session=data_session,
            mock=mock,
            tiled_api_key=tiled_api_key,
        )

    # -------------------------------------------------------------------------
    # Hardware
    # -------------------------------------------------------------------------

    def check_connections(self, *, require_alive: bool = True) -> Any:
        """Check device liveness before any hardware motion.

        Wraps ``check_halide_connections`` using the already-instantiated
        ``self.devices`` dict.

        Parameters
        ----------
        require_alive:
            When ``True`` (default), raise ``RuntimeError`` if any device
            with a liveness probe is not alive.

        Returns
        -------
        list[ConnectionReport]
        """
        return check_halide_connections(self.devices, require_alive=require_alive)

    # -------------------------------------------------------------------------
    # Acquisition
    # -------------------------------------------------------------------------

    def single_trial(self, point: Mapping[str, float]) -> Any:
        """Submit one manual trial point to the local RunEngine.

        Drives ``run_agent.sample_suggestions([point])`` directly through
        ``self.RE`` — the local twin of the queue-server ``submit_suggestions``
        call in the single-trial test.

        !! PHYSICAL SAFETY !!
        Disconnect DDS and Ultra syringes from their drive screws before
        calling this method.  The pumps will actuate; confirm the screws
        are turning visually before reconnecting syringes.

        Parameters
        ----------
        point:
            Mapping of DOF name → value, e.g.::

                {"infusion_rate_CsPb": 20.0, "infusion_rate_Br": 20.0, ...}

        Returns
        -------
        Any
            Whatever ``run_agent.sample_suggestions`` yields via the RE.
        """
        logger.info("LocalCampaign.single_trial: submitting point %s", point)
        return self.RE(self.run_agent.sample_suggestions([dict(point)]))

    def run(self) -> Any:
        """Run the full optimisation campaign.

        Drives ``run_agent.optimize(iterations, n_points)`` through ``self.RE``
        using the run settings from the BuildAgent config.

        Returns
        -------
        Any
            Final RE return value.
        """
        rs = self.build_agent.run_settings
        logger.info(
            "LocalCampaign.run: iterations=%d, n_points=%d",
            rs.iterations,
            rs.n_points,
        )
        return self.RE(self.run_agent.optimize(rs.iterations, rs.n_points))

    def summarize(self) -> "pd.DataFrame":
        """Summarise the campaign results as a DataFrame.

        Wraps ``summarize_campaign(self.run_agent)``.

        Returns
        -------
        pandas.DataFrame
        """
        return summarize_campaign(self.run_agent)
