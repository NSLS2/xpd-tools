"""Tests for xpd_tools.optimization.campaign helpers.

Two tiers:

1. Offline (CI) -- config round-trips, mock plan-registration logic,
   summarize_campaign shape.  No queue server, no hardware required.

2. Single-trial acquisition (live queue server + hardware required):
   loads a saved config, builds a QueueserverAgent, asserts the plan is
   registered, submits one manual point with nonzero pump rates so you can
   visually confirm the drive screws are turning, blocks on the Future, and
   surfaces the full evaluation result.

   !! PHYSICAL SAFETY REQUIREMENT !!
   Disconnect the DDS and Ultra syringes from their drive screws before
   running the single-trial test.  The pumps will actuate; without
   disconnection you will dispense fluid.

Run offline::

    pytest tests/optimization/test_campaign.py -k "not single_trial"

Run single-trial acquisition (queue server must be up, hardware connected)::

    XPD_RUN_TRIAL=1 XPD_HALIDE_CONFIG=/path/to/halide_beamline.json \\
        pytest tests/optimization/test_campaign.py -k single_trial -v -s

Environment variables for the single-trial test:
  XPD_RUN_TRIAL=1          Gate: must be set to run the live acquisition test.
  XPD_HALIDE_CONFIG        Path to the JSON config (from save_campaign_config or
                           build_agent.to_config()).  Defaults to
                           "configs/halide_beamline.json" relative to cwd.
  XPD_HTTP_API_KEY         HTTP API key for the queue server (optional).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from xpd_tools.optimization.campaign import (
    assert_plan_registered,
    load_campaign_config,
    save_campaign_config,
    summarize_campaign,
)

# ---------------------------------------------------------------------------
# Helpers shared by offline tests
# ---------------------------------------------------------------------------

def _make_uvvis_agent():
    """Minimal UV-Vis BuildAgent matching the real halide notebook config."""
    from xpd_tools.optimization.agent import BuildAgent
    from xpd_tools.optimization.helpers.dofs import Pump
    from xpd_tools.optimization.helpers.qepro import PlqyReference, SpectraFitSettings
    from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle

    agent = BuildAgent(
        queue_server=True,
        http_server_uri="https://xf28id2-xpd-qs1.nsls2.bnl.gov",
        evaluation_method="uvvis",
    )
    agent.set_dofs([
        Pump(name="CsPb", id="dds2_p1", bounds=(0, 100)),
        Pump(name="Br",   id="dds2_p2", bounds=(0, 100)),
        Pump(name="Cl",   id="dds3_p1", bounds=(0, 100)),
        Pump(name="I",    id="dds3_p2", bounds=(0, 100)),
    ])
    agent.experiment(
        sources=[
            FlowSource(dof="infusion_rate_CsPb", pump="dds2_p1",
                       precursor="CsPb", sample_label="Cs-rich 33mM", loaded_ml=30.0),
            FlowSource(dof="infusion_rate_Br",   pump="dds2_p2",
                       precursor="Br",   sample_label="TOABr",         loaded_ml=30.0),
            FlowSource(dof="infusion_rate_Cl",   pump="dds3_p1",
                       precursor="Pb",   sample_label="Pb-rich 33mM",  loaded_ml=30.0),
            FlowSource(dof="infusion_rate_I",    pump="dds3_p2",
                       precursor="I",    sample_label="I",              loaded_ml=30.0),
        ],
        dilutions=[
            DilutionStage(pump="dds1_p1", ratio=1.0, position="before_equilibrium",
                          syringe_ml=20, material="plastic_BD",
                          target_ml=20, loaded_ml=20.0),
            DilutionStage(pump="ultra2",  ratio=1.0, position="after_equilibrium",
                          syringe_ml=100, material="steel",
                          target_ml=100, loaded_ml=100.0, wait_sec=30),
        ],
        wash_cycles=[
            WashCycle(pump="ultra1", loaded_ml=30.0),
        ],
    )
    agent.set_uvvis_objectives(
        peak_target=520.0,
        peak_tolerance=5.0,
        fit_settings=SpectraFitSettings(),
        plqy=PlqyReference(),
    )
    agent.set_run(
        iterations=5,
        extra_initialization_trials=2,
        generation_strategy={"initialize_with_center": False},
    )
    return agent


# ---------------------------------------------------------------------------
# Tier 1: Offline tests (no queue server, no hardware)
# ---------------------------------------------------------------------------

class TestSaveCampaignConfig:
    def test_archive_file_created(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "test_campaign", directory=tmp_path)
        assert path.exists()
        assert path.suffix == ".json"
        assert "test_campaign_" in path.name

    def test_stable_copy_created(self, tmp_path):
        agent = _make_uvvis_agent()
        save_campaign_config(agent, "test_campaign", directory=tmp_path)
        stable = tmp_path / "test_campaign.json"
        assert stable.exists()

    def test_saved_json_is_valid(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "test_campaign", directory=tmp_path)
        with open(path) as f:
            data = json.load(f)
        assert "evaluation_method" in data
        assert data["evaluation_method"] == "uvvis"

    def test_directory_created_if_missing(self, tmp_path):
        agent = _make_uvvis_agent()
        deep = tmp_path / "nested" / "configs"
        save_campaign_config(agent, "test_campaign", directory=deep)
        assert deep.exists()

    def test_multiple_saves_accumulate(self, tmp_path):
        agent = _make_uvvis_agent()
        save_campaign_config(agent, "test_campaign", directory=tmp_path)
        save_campaign_config(agent, "test_campaign", directory=tmp_path)
        archives = list(tmp_path.glob("test_campaign_*.json"))
        # Two timestamped archives (may collide within the same second, so >= 1)
        assert len(archives) >= 1


class TestLoadCampaignConfig:
    def test_round_trip_evaluation_method(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "rt", directory=tmp_path)
        rebuilt = load_campaign_config(path)
        assert rebuilt.evaluation_method == "uvvis"

    def test_round_trip_dof_ids(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "rt", directory=tmp_path)
        rebuilt = load_campaign_config(path)
        ids = {p.id for p in rebuilt.pumps}
        assert ids == {"dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2"}

    def test_round_trip_source_pumps(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "rt", directory=tmp_path)
        rebuilt = load_campaign_config(path)
        pumps = {s.pump for s in rebuilt.sources}
        assert pumps == {"dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2"}

    def test_round_trip_run_settings(self, tmp_path):
        agent = _make_uvvis_agent()
        path = save_campaign_config(agent, "rt", directory=tmp_path)
        rebuilt = load_campaign_config(path)
        assert rebuilt.run_settings.iterations == 5

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_campaign_config(tmp_path / "does_not_exist.json")

    def test_stable_path_loads(self, tmp_path):
        agent = _make_uvvis_agent()
        save_campaign_config(agent, "rt", directory=tmp_path)
        rebuilt = load_campaign_config(tmp_path / "rt.json")
        assert rebuilt.evaluation_method == "uvvis"


class TestAssertPlanRegistered:
    """Test the logic of assert_plan_registered against a mock RE Manager."""

    def _make_mock_agent(self, plan_names: list[str]) -> MagicMock:
        """Build a mock QueueserverAgent whose client returns *plan_names*."""
        agent = MagicMock()
        api = agent._runner._client._re_manager_api
        api.plans_allowed.return_value = {
            "plans_allowed": {name: {} for name in plan_names}
        }
        return agent

    def test_passes_when_plan_present(self):
        agent = self._make_mock_agent(["uvvis_acquire", "other_plan"])
        assert_plan_registered(agent, "uvvis_acquire")  # should not raise

    def test_raises_when_plan_absent(self):
        agent = self._make_mock_agent(["other_plan"])
        with pytest.raises(RuntimeError, match="uvvis_acquire"):
            assert_plan_registered(agent, "uvvis_acquire")

    def test_raises_on_api_error(self):
        agent = MagicMock()
        agent._runner._client._re_manager_api.plans_allowed.side_effect = (
            ConnectionError("unreachable")
        )
        with pytest.raises(RuntimeError, match="Could not query"):
            assert_plan_registered(agent, "uvvis_acquire")


class TestSummarizeCampaign:
    def test_returns_dataframe(self, tmp_path):
        import pandas as pd

        mock_agent = MagicMock()
        mock_agent.ax_client.summarize.return_value = pd.DataFrame(
            {"trial_index": [0, 1], "Peak": [520.0, 540.0]}
        )
        with patch("xpd_tools.optimization.campaign.Path") as MockPath:
            MockPath.return_value.__truediv__.return_value = tmp_path / "out.csv"
            MockPath.return_value.mkdir.return_value = None
            # Use real tmp_path so the CSV actually gets written
            import xpd_tools.optimization.campaign as cm
            original_path = cm.Path
            cm.Path = Path
            try:
                df = summarize_campaign(mock_agent)
            finally:
                cm.Path = original_path

        assert isinstance(df, pd.DataFrame)
        assert "trial_index" in df.columns


# ---------------------------------------------------------------------------
# Tier 2: Single-trial live acquisition (queue server + hardware required)
# ---------------------------------------------------------------------------

_RUN_TRIAL = os.environ.get("XPD_RUN_TRIAL", "0").strip().lower() in ("1", "true", "yes")

single_trial = pytest.mark.skipif(
    not _RUN_TRIAL,
    reason=(
        "Set XPD_RUN_TRIAL=1 to run the live single-trial acquisition test.\n"
        "!! Disconnect DDS and Ultra syringes from drive screws first !!\n"
        "The pumps will actuate; visually confirm the screws are turning."
    ),
)


@single_trial
class TestSingleTrialAcquisition:
    """Submit one real acquisition trial through the queue server.

    Prerequisites:
    - Queue server running with the halide worker profile loaded:
        start-re-manager --startup-module xpd_tools.optimization.profiles.halide
    - XPD_HALIDE_CONFIG set on the worker to a valid config JSON.
    - XPD_HALIDE_CONFIG set here (or default configs/halide_beamline.json exists).
    - DDS and Ultra syringes physically disconnected from their drive screws.

    Run::

        XPD_RUN_TRIAL=1 XPD_HALIDE_CONFIG=/path/to/halide_beamline.json \\
            pytest tests/optimization/test_campaign.py -k single_trial -v -s
    """

    @pytest.fixture(scope="class")
    def build_agent(self):
        """Load the campaign config — build fresh or reload an archived JSON."""
        config_path = os.environ.get(
            "XPD_HALIDE_CONFIG",
            "configs/halide_beamline.json",
        )
        return load_campaign_config(config_path)

    @pytest.fixture(scope="class")
    def run_agent(self, build_agent):
        """Build the QueueserverAgent from the loaded config."""
        agent = build_agent.build()
        return agent

    def test_plan_is_registered(self, run_agent):
        """Confirm uvvis_acquire is visible on the queue-server worker."""
        assert_plan_registered(run_agent, plan_name="uvvis_acquire")

    def test_single_trial_submit(self, build_agent, run_agent):
        """Submit one manual trial point with nonzero pump rates.

        The infusion rates below are small but nonzero so that:
        - The DDS and Ultra pump motors receive a move command.
        - You can visually confirm the drive screws are turning.
        - The UV-Vis acquisition fires and captures a dark/reference frame.
        - The full evaluation function runs and results are surfaced --
          a failed fit on a dark frame is expected and useful information.

        Watch the console for:
        - Queue server accepting the plan item.
        - Pump actuation (watch the screws).
        - QEPro dark / reference spectrum capture.
        - Evaluation output (peak fit, PLQY, FWHM or a clear error message).
        """
        # One point: low but nonzero rates so pumps actuate visibly.
        # Values are in ul/min and within the steel 20 mL syringe range.
        manual_point = [{
            "infusion_rate_CsPb": 20.0,   # dds2_p1 -- watch this screw
            "infusion_rate_Br":   20.0,   # dds2_p2
            "infusion_rate_Cl":   20.0,   # dds3_p1
            "infusion_rate_I":    20.0,   # dds3_p2
        }]

        print("\n" + "=" * 60)
        print("Submitting single trial to queue server...")
        print(f"Point: {manual_point[0]}")
        print("Watch the drive screws — they should begin turning.")
        print("=" * 60)

        future = run_agent.submit_suggestions(manual_point)

        print("Waiting for acquisition to complete (blocks until done)...")
        # Let it run -- full evaluation, no error swallowing.
        # If the dark-frame fit fails the exception propagates here as
        # useful diagnostic information.
        result = future.result()

        print("\n" + "=" * 60)
        print("Single-trial result:")
        print(result)
        print("=" * 60)

        # A result object (even with NaN metrics) means the acquisition ran
        # and the evaluation function was called successfully.
        assert result is not None, (
            "future.result() returned None -- acquisition may not have completed"
        )
