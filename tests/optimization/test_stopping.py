from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from xpd_tools.optimization.agent import BuildAgent
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.stopping import _correlation_metric_names, watch_and_stop


def test_correlation_metric_names_only_includes_wanted_phases() -> None:
    agent = BuildAgent(evaluation_method="xray")
    agent.set_xray_objectives(
        phases=[
            Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False),
            Phase(name="Impurity", gr="b.gr", cif="b.cif", minimize=True),
        ]
    )
    assert _correlation_metric_names(agent) == ("corr_Wanted",)


def test_correlation_metric_names_uses_fit_prefix_in_fit_mode() -> None:
    agent = BuildAgent(evaluation_method="xray", pdf_mode="fit")
    agent.set_xray_objectives(
        phases=[Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False)]
    )
    assert _correlation_metric_names(agent) == ("pdf_fit_corr_Wanted",)


def test_correlation_metric_names_empty_without_phases() -> None:
    agent = BuildAgent(evaluation_method="xray")
    assert _correlation_metric_names(agent) == ()


class _FakeFuture:
    def __init__(self) -> None:
        self._done = False

    def done(self) -> bool:
        return self._done

    def resolve(self) -> None:
        self._done = True


class _FakeAxClient:
    """Returns the next canned DataFrame on each summarize() call, sticking
    on the last one once exhausted (so a slow/late poll still sees it)."""

    def __init__(self, frames: list[pd.DataFrame]) -> None:
        self._frames = frames
        self.calls = 0

    def summarize(self, trial_statuses: Any = None) -> pd.DataFrame:
        frame = self._frames[min(self.calls, len(self._frames) - 1)]
        self.calls += 1
        return frame


class _FakeAgent:
    def __init__(self, ax_client: _FakeAxClient) -> None:
        self.ax_client = ax_client
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def test_watch_and_stop_requires_configured_criteria() -> None:
    agent = BuildAgent(evaluation_method="xray")
    agent.set_xray_objectives(
        phases=[Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False)]
    )
    with pytest.raises(ValueError, match="set_success_criteria"):
        watch_and_stop(
            _FakeAgent(_FakeAxClient([pd.DataFrame()])),
            cast("Future[Any]", _FakeFuture()),
            agent,
        )


def test_watch_and_stop_stops_on_correlation_threshold() -> None:
    build_agent = BuildAgent(evaluation_method="xray")
    build_agent.set_xray_objectives(
        phases=[Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False)]
    )
    build_agent.set_success_criteria(min_correlation=0.9, poll_interval=0.01)

    frames = [
        pd.DataFrame({"corr_Wanted": [0.5, 0.6]}),
        pd.DataFrame({"corr_Wanted": [0.5, 0.95]}),
    ]
    fake_agent = _FakeAgent(_FakeAxClient(frames))
    future = _FakeFuture()

    thread = watch_and_stop(fake_agent, cast("Future[Any]", future), build_agent)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert fake_agent.stop_calls == 1


def test_watch_and_stop_stops_on_fwhm_plqy_pairing() -> None:
    build_agent = BuildAgent(evaluation_method="uvvis")
    build_agent.set_success_criteria(max_fwhm=30.0, min_plqy=0.5, poll_interval=0.01)

    frames = [
        pd.DataFrame({"log_FWHM": [np.log(40.0)], "log_PLQY": [np.log(0.3)]}),
        pd.DataFrame({"log_FWHM": [np.log(25.0)], "log_PLQY": [np.log(0.6)]}),
    ]
    fake_agent = _FakeAgent(_FakeAxClient(frames))
    future = _FakeFuture()

    thread = watch_and_stop(fake_agent, cast("Future[Any]", future), build_agent)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert fake_agent.stop_calls == 1


def test_watch_and_stop_treats_both_criteria_as_independent_or_not_and() -> None:
    """Both criteria configured at once: satisfying EITHER one stops the
    campaign, even though the other never crosses its own threshold --
    proving they're independent success paths, not a combined AND.
    """
    build_agent = BuildAgent(evaluation_method="xray-uvvis")
    build_agent.set_xray_objectives(
        phases=[Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False)]
    )
    build_agent.set_uvvis_objectives()
    build_agent.set_success_criteria(
        min_correlation=0.9, max_fwhm=30.0, min_plqy=0.5, poll_interval=0.01
    )

    # Correlation crosses its threshold; FWHM/PLQY never do.
    frame = pd.DataFrame(
        {
            "corr_Wanted": [0.95],
            "log_FWHM": [np.log(999.0)],
            "log_PLQY": [np.log(1e-6)],
        }
    )
    fake_agent = _FakeAgent(_FakeAxClient([frame]))
    future = _FakeFuture()

    thread = watch_and_stop(fake_agent, cast("Future[Any]", future), build_agent)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert fake_agent.stop_calls == 1


def test_watch_and_stop_exits_cleanly_when_future_resolves_without_success() -> None:
    build_agent = BuildAgent(evaluation_method="xray")
    build_agent.set_xray_objectives(
        phases=[Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=False)]
    )
    build_agent.set_success_criteria(min_correlation=0.99, poll_interval=0.01)

    fake_agent = _FakeAgent(_FakeAxClient([pd.DataFrame({"corr_Wanted": [0.5]})]))
    future = _FakeFuture()

    thread = watch_and_stop(fake_agent, cast("Future[Any]", future), build_agent)
    time.sleep(0.05)  # let it poll a few times without ever succeeding
    future.resolve()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert fake_agent.stop_calls == 0
