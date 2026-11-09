"""Background success-threshold watcher for a running bluesky-adaptive campaign.

Layered on top of the existing iterations= stopping mode.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from xpd_tools.optimization.agent import BuildAgent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SuccessCriteria:
    """Optional early-stop thresholds for a running campaign."""

    min_correlation: float | None = None
    max_fwhm: float | None = None
    min_plqy: float | None = None
    poll_interval: float = 5.0


def _correlation_metric_names(build_agent: "BuildAgent") -> tuple[str, ...]:
    """Name of the "wanted" (minimize=False) phase correlation metrics."""
    # cnn phases report fractions (frac_), not correlations.
    if not build_agent.phases or build_agent.objective_function == "cnn":
        return ()
    prefix = "pdf_fit_corr_" if build_agent.pdf_mode == "fit" else "corr_"
    return tuple(
        f"{prefix}{phase.name}" for phase in build_agent.phases if not phase.minimize
    )


def watch_and_stop(
    agent: Any,
    future: "Future[Any]",
    build_agent: "BuildAgent",
) -> threading.Thread:
    """Start a background thread that stops `agent`'s campaign on success."""
    if build_agent.success_criteria is None:
        raise ValueError(
            "build_agent.set_success_criteria(...) must be called before watch_and_stop()"
        )
    correlation_metrics = _correlation_metric_names(build_agent)

    def _watch() -> None:
        while not future.done():
            # Re-read each poll so criteria changed mid-run take effect.
            criteria = build_agent.success_criteria
            df = agent.ax_client.summarize(trial_statuses=["completed"])

            if criteria.min_correlation is not None:
                for metric in correlation_metrics:
                    if metric in df.columns and (df[metric] >= criteria.min_correlation).any():
                        logger.info(
                            "Success threshold reached: %s >= %s -- stopping campaign.",
                            metric,
                            criteria.min_correlation,
                        )
                        agent.stop()
                        return

            if criteria.max_fwhm is not None and criteria.min_plqy is not None:
                if {"log_FWHM", "log_PLQY"} <= set(df.columns):
                    fwhm = np.exp(df["log_FWHM"])
                    plqy = np.exp(df["log_PLQY"])
                    if ((fwhm <= criteria.max_fwhm) & (plqy >= criteria.min_plqy)).any():
                        logger.info(
                            "Success threshold reached: FWHM <= %s and PLQY >= %s -- "
                            "stopping campaign.",
                            criteria.max_fwhm,
                            criteria.min_plqy,
                        )
                        agent.stop()
                        return

            time.sleep(criteria.poll_interval)

    thread = threading.Thread(target=_watch, daemon=True, name="xpd-success-watcher")
    thread.start()
    return thread
