from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from blop.ax.agent import Agent
from bluesky.run_engine import RunEngine

from xpd_tools.optimization.agent import BuildAgent, _load_historical_data
from xpd_tools.optimization.helpers.dofs import Pump
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.helpers.qepro import PlqyReference, SpectraFitSettings
from xpd_tools.optimization.helpers.run import RunSettings
from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle


@pytest.fixture
def phase_factory(tmp_path: Path) -> Callable[..., Phase]:
    """Write a real minimal .gr/.cif pair and return a matching Phase."""
    radial = np.linspace(1.0, 25.0, 200)

    def factory(name: str = "A", *, minimize: bool = False) -> Phase:
        gr_path = tmp_path / f"{name}.gr"
        np.savetxt(gr_path, np.column_stack((radial, np.sin(radial))))
        cif_path = tmp_path / f"{name}.cif"
        cif_path.write_text(
            "data_test\n_symmetry_space_group_name_H-M 'P 1'\n"
            "_cell_length_a 6\n_cell_length_b 6\n_cell_length_c 6\n"
            "_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n"
        )
        return Phase(name=name, gr=str(gr_path), cif=str(cif_path), minimize=minimize)

    return factory


@pytest.fixture
def mocked_queueserver() -> None:
    """Patch every network/queueserver call BuildAgent.build() can make."""
    targets = [
        "xpd_tools.optimization.agent.from_uri",
        "xpd_tools.optimization.agent.from_profile",
        "xpd_tools.optimization.agent.REManagerAPI",
        "xpd_tools.optimization.agent.RemoteDispatcher",
        "blop.ax.queueserver_agent.QueueserverClient",
        "blop.ax.queueserver_agent.QueueserverOptimizationRunner",
    ]
    patchers = [patch(target) for target in targets]
    for patcher in patchers:
        patcher.start()
    yield
    for patcher in patchers:
        patcher.stop()


@pytest.fixture
def mocked_local() -> None:
    """Patch the Tiled calls build_local() can still make for evaluation --
    no queue server or ZMQ dispatcher involved on this path at all."""
    targets = [
        "xpd_tools.optimization.agent.from_uri",
        "xpd_tools.optimization.agent.from_profile",
    ]
    patchers = [patch(target) for target in targets]
    for patcher in patchers:
        patcher.start()
    yield
    for patcher in patchers:
        patcher.stop()


def _pump(name: str = "CsPb", *, bounds: tuple[float, float] = (10, 200)) -> Pump:
    return Pump(name=name, id=f"dds-{name}", bounds=bounds)


def _source(name: str = "CsPb") -> FlowSource:
    return FlowSource(
        dof=f"infusion_rate_{name}",
        pump=f"dds-{name}",
        precursor=f"precursor-{name}",
        sample_label=name,
    )


def _uvvis_agent(**kwargs: Any) -> BuildAgent:
    agent = BuildAgent(
        evaluation_method="uvvis",
        http_server_uri="https://example.invalid",
        **kwargs,
    )
    agent.set_dofs([_pump()])
    agent.experiment(sources=[_source()])
    return agent


class TestDofSourceAlignment:
    def test_set_dofs_and_experiment_normalize_tuples_to_lists(self) -> None:
        """set_dofs/experiment/set_xray_objectives accept any sequence type
        (tuple, list, ...) and normalize to a list -- so a hand-built agent
        matches a from_config()-rebuilt one regardless of which the caller
        used (from_config always rebuilds lists).
        """
        agent = BuildAgent(evaluation_method="xray")
        agent.set_dofs((_pump(),))
        agent.experiment(
            sources=(_source(),), dilutions=(), wash_cycles=()
        )
        assert isinstance(agent.pumps, list)
        assert isinstance(agent.sources, list)
        assert isinstance(agent.dilutions, list)
        assert isinstance(agent.wash_cycles, list)

    def test_dofs_then_matching_sources_ok(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        assert agent.dofs[0].name == "infusion_rate_CsPb"

    def test_sources_then_matching_dofs_ok(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.experiment(sources=[_source()])
        agent.set_dofs([_pump()])
        assert agent.dofs[0].name == "infusion_rate_CsPb"

    def test_mismatch_raises_when_second_call_is_experiment(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_dofs([_pump()])
        with pytest.raises(ValueError, match="must reference the same names"):
            agent.experiment(sources=[_source("Br")])

    def test_mismatch_raises_when_second_call_is_set_dofs(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.experiment(sources=[_source("Br")])
        with pytest.raises(ValueError, match="must reference the same names"):
            agent.set_dofs([_pump()])

    def test_build_requires_set_dofs(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.experiment(sources=[_source()])
        with pytest.raises(ValueError, match="set_dofs"):
            agent.build()

    def test_build_requires_experiment(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_dofs([_pump()])
        with pytest.raises(ValueError, match="experiment"):
            agent.build()


class TestUvvisObjectives:
    def test_builds_plqy_reference(self) -> None:
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(
            plqy=PlqyReference(
                excitation_wavelength_nm=400, solvent_refractive_index=1.5
            )
        )
        assert agent.plqy.reference_type == "quinine"
        assert agent.plqy.excitation_wavelength_nm == 400
        assert agent.plqy.solvent_refractive_index == 1.5

    def test_builds_spectra_fit_settings(self) -> None:
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(
            plqy=PlqyReference(),
            fit_settings=SpectraFitSettings(pl_screen_key_height=50),
        )
        assert agent.fit_settings.pl_screen_key_height == 50
        assert agent.fit_settings.pl_wavelength_range == (400.0, 800.0)

    def test_build_requires_set_uvvis_objectives(self) -> None:
        agent = _uvvis_agent()
        with pytest.raises(ValueError, match="set_uvvis_objectives"):
            agent.build()


class TestSuccessCriteria:
    def test_requires_at_least_one_real_target(self) -> None:
        agent = BuildAgent(evaluation_method="xray")
        with pytest.raises(ValueError, match="at least one real target"):
            agent.set_success_criteria()
        with pytest.raises(ValueError, match="at least one real target"):
            agent.set_success_criteria(max_fwhm=30.0)  # min_plqy missing
        with pytest.raises(ValueError, match="at least one real target"):
            agent.set_success_criteria(min_plqy=0.5)  # max_fwhm missing

    def test_stores_min_correlation(self) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_success_criteria(min_correlation=0.9, poll_interval=1.0)
        assert agent.success_criteria.min_correlation == 0.9
        assert agent.success_criteria.max_fwhm is None
        assert agent.success_criteria.min_plqy is None
        assert agent.success_criteria.poll_interval == 1.0

    def test_stores_fwhm_plqy_pair(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_success_criteria(max_fwhm=30.0, min_plqy=0.5)
        assert agent.success_criteria.min_correlation is None
        assert agent.success_criteria.max_fwhm == 30.0
        assert agent.success_criteria.min_plqy == 0.5
        assert agent.success_criteria.poll_interval == 5.0  # default


class TestXrayObjectives:
    def test_builds_xray_settings(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            exposure=300.0,
            frame_acq_time=1.0,
            no_dark=True,
            stream_name="custom",
            phases=[phase_factory()],
        )
        assert agent.xray_settings.exposure == 300.0
        assert agent.xray_settings.frame_acq_time == 1.0
        assert agent.xray_settings.no_dark is True
        assert agent.xray_settings.stream_name == "custom"

    def test_objective_function_rejects_unknown_choices(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        with pytest.raises(ValueError, match="Invalid objective function"):
            agent.set_xray_objectives(
                objective_function="unknown", phases=[phase_factory()]
            )

    def test_cnn_requires_model_paths(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        with pytest.raises(ValueError, match="cnn_dataset_path and cnn_weights_path"):
            agent.set_xray_objectives(objective_function="cnn", phases=[phase_factory()])

    def test_cnn_paths_reach_evaluator_and_round_trip(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            objective_function="cnn",
            phases=[phase_factory()],
            cnn_dataset_path=Path("model/dataset_pc.npz"),
            cnn_weights_path="model/amortized_encoder.pt",
        )
        captured: dict[str, Any] = {}
        with patch.dict(
            "xpd_tools.optimization.agent._EVALUATORS",
            {"xray": lambda **kwargs: captured.update(kwargs)},
        ):
            agent._build_evaluator(True, sandbox_client=object())
        assert captured["cnn_dataset_path"] == "model/dataset_pc.npz"
        assert captured["cnn_weights_path"] == "model/amortized_encoder.pt"

        xray = agent.to_config()["xray"]
        assert xray["cnn_dataset_path"] == "model/dataset_pc.npz"
        assert xray["cnn_weights_path"] == "model/amortized_encoder.pt"

    def test_objective_function_accepts_ensemble(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            objective_function="ensemble", phases=[phase_factory()]
        )
        assert agent.objective_function == "ensemble"

    def test_phases_normalizes_tuples_to_lists(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(phases=(phase_factory(),))
        assert isinstance(agent.phases, list)

    def test_unscreened_has_no_quality_policy(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(screening="unscreened", phases=[phase_factory()])
        assert agent.quality_policy is None

    @pytest.mark.parametrize("screening", ["screen_only", "screen_and_record"])
    def test_screened_modes_build_quality_policy(
        self, screening: str, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            screening=screening,
            use_good_bad=False,
            good_target=5,
            max_bad=1,
            num_abs=8,
            num_flu=12,
            phases=[phase_factory()],
        )
        assert agent.quality_policy.enabled is False
        assert agent.quality_policy.good_batches == 5
        assert agent.quality_policy.max_bad_batches == 1
        assert agent.quality_policy.absorbance_shots == 8
        assert agent.quality_policy.fluorescence_shots == 12

    def test_stores_min_max_radius(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            min_radius=3.0, max_radius=15.0, phases=[phase_factory()]
        )
        assert agent.min_radius == 3.0
        assert agent.max_radius == 15.0

    def test_stores_phases_and_applies_metric_prefix(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        phases = [phase_factory("Wanted"), phase_factory("Impurity", minimize=True)]

        raw_agent = BuildAgent(evaluation_method="xray", pdf_mode="raw")
        raw_agent.set_xray_objectives(phases=phases)
        assert raw_agent.phases == phases
        assert {obj.name for obj in raw_agent.objectives} == {
            "corr_Wanted",
            "corr_Impurity",
        }

        fit_agent = BuildAgent(evaluation_method="xray", pdf_mode="fit")
        fit_agent.set_xray_objectives(phases=phases)
        assert {obj.name for obj in fit_agent.objectives} == {
            "pdf_fit_corr_Wanted",
            "pdf_fit_corr_Impurity",
        }

        raw_tracked_agent = BuildAgent(evaluation_method="xray", pdf_mode="raw_tracked")
        raw_tracked_agent.set_xray_objectives(phases=phases)
        assert {obj.name for obj in raw_tracked_agent.objectives} == {
            "corr_Wanted",
            "corr_Impurity",
        }

    def test_build_requires_phases(self) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        with pytest.raises(ValueError, match="requires phases"):
            agent.build()


class TestPeakOutcomeConstraints:
    def test_values_match_target_and_tolerance(self) -> None:
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(
            peak_target=650, peak_tolerance=4, plqy=PlqyReference()
        )
        constraints = agent._peak_outcome_constraints()
        assert [str(c) for c in constraints] == ["Peak >= 646", "Peak <= 654"]

    def test_objective_thresholds_become_constraints_on_the_objectives(self) -> None:
        """Ax turns constraints on objective metrics into objective thresholds."""
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(
            peak_target=650, peak_tolerance=4, plqy=PlqyReference(),
            max_fwhm_nm=60.0, min_plqy=1e-3, max_peak_distance_nm=50.0,
        )
        thresholds = [str(c) for c in agent._uvvis_objective_thresholds()]
        assert thresholds == [
            "log_FWHM <= 4.09434", "log_PLQY >= -6.90776", "peak_distance <= 50"
        ]
        assert [str(c) for c in agent._outcome_constraints(needs_plqy=True)] == [
            "Peak >= 646", "Peak <= 654", *thresholds
        ]

    def test_objective_thresholds_default_to_none_and_must_be_positive(self) -> None:
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(plqy=PlqyReference())
        assert agent._uvvis_objective_thresholds() == ()
        with pytest.raises(ValueError, match="min_plqy must be positive"):
            agent.set_uvvis_objectives(plqy=PlqyReference(), min_plqy=0.0)


class TestAcquisitionPlanSelection:
    def test_uvvis(self) -> None:
        agent = _uvvis_agent()
        assert agent._acquisition_plan_name() == "uvvis_acquire"

    def test_xray_unscreened(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(screening="unscreened", phases=[phase_factory()])
        assert agent._acquisition_plan_name() == "xray_acquire"

    def test_xray_screen_and_record(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(
            screening="screen_and_record", phases=[phase_factory()]
        )
        assert agent._acquisition_plan_name() == "xray_uvvis_acquire"

    def test_xray_screen_only(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray")
        agent.set_xray_objectives(screening="screen_only", phases=[phase_factory()])
        assert agent._acquisition_plan_name() == "xray_screened_acquire"

    def test_xray_uvvis(self, phase_factory: Callable[..., Phase]) -> None:
        agent = BuildAgent(evaluation_method="xray-uvvis")
        agent.set_xray_objectives(phases=[phase_factory()])
        assert agent._acquisition_plan_name() == "xray_uvvis_acquire"


class TestLoadHistoricalData:
    def test_returns_matching_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,log_FWHM\n50.0,3.1\n75.0,3.0\n")
        rows = _load_historical_data(path, ["infusion_rate_CsPb"], ["log_FWHM"])
        assert rows == [
            {"infusion_rate_CsPb": 50.0, "log_FWHM": 3.1},
            {"infusion_rate_CsPb": 75.0, "log_FWHM": 3.0},
        ]

    def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb\n50.0\n")
        with pytest.raises(ValueError, match="missing columns: infusion_rate_Br"):
            _load_historical_data(
                path, ["infusion_rate_CsPb", "infusion_rate_Br"], []
            )

    def test_optional_column_included_when_present(self, tmp_path: Path) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,Peak\n50.0,648.0\n")
        rows = _load_historical_data(
            path, ["infusion_rate_CsPb"], [], optional_names=("Peak",)
        )
        assert rows == [{"infusion_rate_CsPb": 50.0, "Peak": 648.0}]

    def test_optional_column_omitted_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb\n50.0\n")
        rows = _load_historical_data(
            path, ["infusion_rate_CsPb"], [], optional_names=("Peak",)
        )
        assert rows == [{"infusion_rate_CsPb": 50.0}]

    def test_partial_objectives_load_with_only_the_present_ones(
        self, tmp_path: Path
    ) -> None:
        """UV-Vis-only historical data seeding an xray-uvvis agent -- the
        CSV never recorded PDF phase correlations, and that's fine: Ax's
        own complete_trial explicitly supports partial per-trial outcome
        data, so this should load with just the columns that exist rather
        than failing the whole file over the missing ones.
        """
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,log_FWHM,log_PLQY\n50.0,3.1,0.5\n")
        rows = _load_historical_data(
            path,
            ["infusion_rate_CsPb"],
            ["log_FWHM", "log_PLQY", "corr_CsPbBr3"],
        )
        assert rows == [{"infusion_rate_CsPb": 50.0, "log_FWHM": 3.1, "log_PLQY": 0.5}]

    def test_peak_distance_recomputed_from_peak_and_target(self, tmp_path: Path) -> None:
        """A saved peak_distance is relative to the target it was exported with;
        the current target wins."""
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,Peak,peak_distance\n50.0,460.0,999.0\n")
        rows = _load_historical_data(
            path,
            ["infusion_rate_CsPb"],
            ["peak_distance"],
            optional_names=("Peak",),
            peak_target=450.0,
        )
        assert rows == [{"infusion_rate_CsPb": 50.0, "peak_distance": 10.0, "Peak": 460.0}]

    def test_peak_distance_kept_without_a_target(self, tmp_path: Path) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,Peak,peak_distance\n50.0,460.0,7.0\n")
        rows = _load_historical_data(path, ["infusion_rate_CsPb"], ["peak_distance"])
        assert rows == [{"infusion_rate_CsPb": 50.0, "peak_distance": 7.0}]

    def test_rows_with_nan_or_inf_in_loaded_columns_are_dropped(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "history.csv"
        path.write_text(
            "infusion_rate_CsPb,log_FWHM,unused\n"
            "50.0,3.1,nan\n"  # NaN only in a column that isn't loaded: kept
            "60.0,inf,1.0\n"
            ",3.0,1.0\n"
            "75.0,-inf,1.0\n"
            "80.0,2.9,1.0\n"
        )
        rows = _load_historical_data(path, ["infusion_rate_CsPb"], ["log_FWHM"])
        assert rows == [
            {"infusion_rate_CsPb": 50.0, "log_FWHM": 3.1},
            {"infusion_rate_CsPb": 80.0, "log_FWHM": 2.9},
        ]

    def test_dropped_rows_name_the_columns_responsible(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,log_FWHM\n50.0,3.1\n60.0,inf\n")
        rows = _load_historical_data(path, ["infusion_rate_CsPb"], ["log_FWHM"])
        assert rows == [{"infusion_rate_CsPb": 50.0, "log_FWHM": 3.1}]
        assert "dropping 1 of 2" in caplog.text
        assert "log_FWHM: 1" in caplog.text

    def test_every_row_dropped_raises_naming_the_column(self, tmp_path: Path) -> None:
        """A DOF column present only in the header (e.g. a pump the old runs never
        had) empties every row: that's a data/config mismatch, so fail loudly."""
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb,log_FWHM,infusion_rate_I\n50.0,3.1\n60.0,3.0\n")
        with pytest.raises(
            ValueError, match=r"every one of 2 .*\(infusion_rate_I: 2\)"
        ):
            _load_historical_data(
                path, ["infusion_rate_CsPb", "infusion_rate_I"], ["log_FWHM"]
            )

    def test_no_configured_objectives_present_raises(self, tmp_path: Path) -> None:
        """Distinguishes "some objectives missing" (fine, see above) from
        "none of them are here at all" -- the latter is much more likely to
        be the wrong file than an intentional partial subset.
        """
        path = tmp_path / "history.csv"
        path.write_text("infusion_rate_CsPb\n50.0\n")
        with pytest.raises(ValueError, match="none of the configured objectives"):
            _load_historical_data(
                path, ["infusion_rate_CsPb"], ["log_FWHM", "corr_CsPbBr3"]
            )


def test_pump_bounds_normalizes_to_tuple() -> None:
    """Pump.bounds is typed tuple[float, float] but nothing enforced it --
    from_config() always rebuilds it as a tuple, so a hand-built Pump using
    a list would silently mismatch. See Pump.__post_init__.
    """
    assert Pump(name="CsPb", id="dds2_p1", bounds=[10, 200]).bounds == (10, 200)
    assert isinstance(Pump(name="CsPb", id="dds2_p1", bounds=[10, 200]).bounds, tuple)


def test_spectra_fit_settings_ranges_normalize_to_tuple() -> None:
    """Same fragility as Pump.bounds, for SpectraFitSettings's four range
    fields -- see SpectraFitSettings.__post_init__.
    """
    settings = SpectraFitSettings(
        pl_percent_range=[40, 100],
        pl_wavelength_range=[400, 800],
        absorbance_percent_range=[10, 70],
        absorbance_wavelength_range=[210, 700],
    )
    assert settings.pl_percent_range == (40, 100)
    assert settings.pl_wavelength_range == (400, 800)
    assert settings.absorbance_percent_range == (10, 70)
    assert settings.absorbance_wavelength_range == (210, 700)
    assert all(
        isinstance(getattr(settings, field), tuple)
        for field in (
            "pl_percent_range",
            "pl_wavelength_range",
            "absorbance_percent_range",
            "absorbance_wavelength_range",
        )
    )


class TestConfigRoundTrip:
    def _full_agent(self, phase: Phase) -> BuildAgent:
        agent = BuildAgent(
            evaluation_method="xray-uvvis",
            http_server_uri="https://example.invalid",
            http_api_key="TOP-SECRET",
            zmq_consumer_address="ipc:///tmp/fake",
            tiled_profile="xpd",
            checkpoint_path="/tmp/ckpt.json",
        )
        agent.set_metadata({"beamline": "28id2", "comment": "test"})
        agent.set_dofs([_pump()])
        agent.experiment(
            sources=[_source()],
            dilutions=[
                DilutionStage(
                    pump="dds1_p1",
                    ratio=1.0,
                    position="before_equilibrium",
                    syringe_ml=20,
                    material="plastic_BD",
                    target_ml=20,
                )
            ],
            wash_cycles=[WashCycle(pump="ultra1")],
        )
        agent.set_xray_objectives(
            exposure=300.0,
            screening="screen_and_record",
            good_target=5,
            min_radius=3.0,
            max_radius=15.0,
            phases=[phase],
        )
        agent.set_uvvis_objectives(
            peak_target=650,
            peak_tolerance=4,
            plqy=PlqyReference(),
            fit_settings=SpectraFitSettings(),
            max_fwhm_nm=60.0,
            min_plqy=1e-3,
            max_peak_distance_nm=50.0,
        )
        agent.set_success_criteria(min_correlation=0.9, max_fwhm=30.0, min_plqy=0.5, poll_interval=2.0)
        agent.set_run(
            iterations=12,
            extra_initialization_trials=5,
            generation_strategy={"initialize_with_center": False},
            local={
                "mixer_lengths_cm": (0.0, 5.0),
                "residence_time_ratio": 0.5,
                "simulated": {"dof_for_phase": {"A": "infusion_rate_CsPb"}, "noise_level": 0.01},
            },
        )
        return agent

    def test_to_config_excludes_api_key(self, phase_factory: Callable[..., Phase]) -> None:
        agent = self._full_agent(phase_factory())
        agent.http_api_key = "TOP-SECRET"
        text = json.dumps(agent.to_config())
        assert "TOP-SECRET" not in text

    def test_round_trip_through_json_is_identical(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        agent = self._full_agent(phase_factory())
        config = agent.to_config()
        reloaded = json.loads(json.dumps(config))

        rebuilt = BuildAgent.from_config(reloaded, http_api_key="TOP-SECRET")

        assert rebuilt.to_config() == config

    def test_rebuilt_agent_builds(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        agent = self._full_agent(phase_factory())
        rebuilt = BuildAgent.from_config(agent.to_config())
        built = rebuilt.build()
        assert built.acquisition_plan == "xray_uvvis_acquire"

    def test_live_object_state_matches_after_round_trip(
        self, phase_factory: Callable[..., Phase]
    ) -> None:
        """`from_config` must rebuild an agent whose live state (`__dict__`)
        matches the original -- a strictly stronger guarantee than the
        `to_config()`-output equality the other round-trip tests check.
        `to_config()` output can't distinguish a `list` from a `tuple`
        (both serialize to the same JSON array), so a to_config()-only
        check would miss a real regression class: e.g. `from_config`
        silently rebuilding `Pump.bounds` as a list when the original was
        a tuple (as happened in the example notebook -- see
        `test_dofs.py`/`helpers/dofs.py`'s `bounds: tuple[float, float]`).
        """
        agent = self._full_agent(phase_factory())
        config = json.loads(json.dumps(agent.to_config()))

        rebuilt = BuildAgent.from_config(config, http_api_key=agent.http_api_key)

        assert rebuilt.__dict__ == agent.__dict__

    def test_run_settings_round_trip(self, phase_factory: Callable[..., Phase]) -> None:
        config = json.loads(json.dumps(self._full_agent(phase_factory()).to_config()))
        assert config["run"]["iterations"] == 12
        assert config["run"]["local"]["mixer_lengths_cm"] == [0.0, 5.0]
        rebuilt = BuildAgent.from_config(config)
        assert rebuilt.run_settings.local.mixer_lengths_cm == (0.0, 5.0)
        assert rebuilt.run_settings.local.simulated == {
            "dof_for_phase": {"A": "infusion_rate_CsPb"},
            "noise_level": 0.01,
        }

    def test_config_without_run_gets_default_run_settings(self) -> None:
        """Configs saved before "run" existed still load."""
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_uvvis_objectives(plqy=PlqyReference())
        config = agent.to_config()
        del config["run"]
        assert BuildAgent.from_config(config).run_settings == RunSettings()

    def test_partial_config_round_trips_without_spurious_defaults(self) -> None:
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_uvvis_objectives(plqy=PlqyReference())

        config = agent.to_config()
        rebuilt = BuildAgent.from_config(config)

        assert rebuilt.to_config() == config
        assert rebuilt.dofs is None
        assert rebuilt.xray_settings is None


class TestBuildEndToEnd:
    def test_uvvis_succeeds(self, mocked_queueserver: None) -> None:
        agent = _uvvis_agent()
        agent.set_uvvis_objectives(plqy=PlqyReference())
        built = agent.build()
        assert built.acquisition_plan == "uvvis_acquire"

    def test_xray_succeeds(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        agent = BuildAgent(evaluation_method="xray", http_server_uri="https://example.invalid")
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(
            screening="screen_and_record", phases=[phase_factory()]
        )
        built = agent.build()
        assert built.acquisition_plan == "xray_uvvis_acquire"

    def test_pdf_mode_reaches_the_built_evaluator(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        agent = BuildAgent(
            evaluation_method="xray",
            http_server_uri="https://example.invalid",
            pdf_mode="raw_tracked",
        )
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(screening="unscreened", phases=[phase_factory()])
        assert agent.to_config()["pdf_mode"] == "raw_tracked"

        built = agent.build()
        assert built.evaluation_function.pdf_mode == "raw_tracked"
        # raw_tracked optimizes against the raw correlation, like "raw".
        assert {obj.name for obj in agent.objectives} == {
            f"corr_{phase.name}" for phase in agent.phases
        }

    def test_objective_function_reaches_the_built_evaluator(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        """objective_function must actually control phase scoring, not just
        be stored -- see the objective_function dead-wiring bug found in
        review (fixed by threading it through _write_pdf_references).
        """
        agent = BuildAgent(evaluation_method="xray", http_server_uri="https://example.invalid")
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(
            objective_function="cross_correlation",
            screening="unscreened",
            phases=[phase_factory()],
        )
        built = agent.build()
        assert all(
            phase.scoring_function == "cross_correlation"
            for phase in built.evaluation_function.phases
        )

    def test_xray_screen_only_succeeds(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        agent = BuildAgent(evaluation_method="xray", http_server_uri="https://example.invalid")
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(screening="screen_only", phases=[phase_factory()])
        built = agent.build()
        assert built.acquisition_plan == "xray_screened_acquire"

    def test_xray_uvvis_succeeds(
        self, phase_factory: Callable[..., Phase], mocked_queueserver: None
    ) -> None:
        agent = BuildAgent(
            evaluation_method="xray-uvvis", http_server_uri="https://example.invalid"
        )
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(phases=[phase_factory()])
        agent.set_uvvis_objectives(plqy=PlqyReference())
        built = agent.build()
        assert built.acquisition_plan == "xray_uvvis_acquire"

    def test_queue_server_false_not_implemented(self) -> None:
        agent = _uvvis_agent(queue_server=False)
        with pytest.raises(NotImplementedError, match="queue_server=False"):
            agent.build()


class TestBuildLocal:
    """build_local() -- the no-queue-server path, driving a local RunEngine
    directly against real/simulated devices instead of dispatching plans by
    name through a Queue Server."""

    def test_requires_queue_server_false(self) -> None:
        agent = _uvvis_agent()  # queue_server=True by default
        with pytest.raises(ValueError, match="queue_server=True"):
            agent.build_local(devices={}, wrap_xray_run=lambda plan, no_dark: plan)

    def test_uvvis_builds_local_agent(
        self,
        mocked_local: None,
        fake_pumps: Mapping[str, Any],
        optical_signals: tuple[Any, Any, Any],
        fake_qepro: Any,
    ) -> None:
        led, uv_shutter, fast_shutter = optical_signals
        agent = _uvvis_agent(queue_server=False)
        agent.set_uvvis_objectives(plqy=PlqyReference())
        built = agent.build_local(
            devices={
                "dds-CsPb": fake_pumps["dds2_p1"],
                "led": led,
                "uv_shutter": uv_shutter,
                "fast_shutter": fast_shutter,
                "qepro": fake_qepro,
            },
            wrap_xray_run=lambda plan, no_dark: plan,
        )
        assert isinstance(built, Agent)
        assert built.acquisition_plan.__name__ == "uvvis_acquire"

    def test_xray_unscreened_builds_local_agent(
        self,
        phase_factory: Callable[..., Phase],
        mocked_local: None,
        fake_pumps: Mapping[str, Any],
        optical_signals: tuple[Any, Any, Any],
        fake_area_detector: Any,
    ) -> None:
        led, _uv_shutter, fast_shutter = optical_signals
        agent = BuildAgent(evaluation_method="xray", queue_server=False)
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(screening="unscreened", phases=[phase_factory()])
        built = agent.build_local(
            devices={
                "dds-CsPb": fake_pumps["dds2_p1"],
                "led": led,
                "fast_shutter": fast_shutter,
                "xray_detector": fake_area_detector,
            },
            wrap_xray_run=lambda plan, no_dark: plan,
        )
        assert built.acquisition_plan.__name__ == "xray_acquire"

    def test_xray_screen_only_builds_local_agent(
        self,
        phase_factory: Callable[..., Phase],
        mocked_local: None,
        fake_pumps: Mapping[str, Any],
        optical_signals: tuple[Any, Any, Any],
        fake_area_detector: Any,
        fake_qepro: Any,
    ) -> None:
        led, uv_shutter, fast_shutter = optical_signals
        agent = BuildAgent(evaluation_method="xray", queue_server=False)
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(screening="screen_only", phases=[phase_factory()])
        built = agent.build_local(
            devices={
                "dds-CsPb": fake_pumps["dds2_p1"],
                "led": led,
                "uv_shutter": uv_shutter,
                "fast_shutter": fast_shutter,
                "xray_detector": fake_area_detector,
                "qepro": fake_qepro,
            },
            wrap_xray_run=lambda plan, no_dark: plan,
        )
        assert built.acquisition_plan.__name__ == "xray_screened_acquire"

    def test_xray_uvvis_builds_local_agent(
        self,
        phase_factory: Callable[..., Phase],
        mocked_local: None,
        fake_pumps: Mapping[str, Any],
        optical_signals: tuple[Any, Any, Any],
        fake_area_detector: Any,
        fake_qepro: Any,
    ) -> None:
        led, uv_shutter, fast_shutter = optical_signals
        agent = BuildAgent(evaluation_method="xray-uvvis", queue_server=False)
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(phases=[phase_factory()])
        agent.set_uvvis_objectives(plqy=PlqyReference())
        built = agent.build_local(
            devices={
                "dds-CsPb": fake_pumps["dds2_p1"],
                "led": led,
                "uv_shutter": uv_shutter,
                "fast_shutter": fast_shutter,
                "xray_detector": fake_area_detector,
                "qepro": fake_qepro,
            },
            wrap_xray_run=lambda plan, no_dark: plan,
        )
        assert built.acquisition_plan.__name__ == "xray_uvvis_acquire"

    def test_acquisition_plan_actually_runs_with_a_real_run_engine(
        self,
        phase_factory: Callable[..., Phase],
        mocked_local: None,
        fake_pumps: Mapping[str, Any],
        optical_signals: tuple[Any, Any, Any],
        fake_area_detector: Any,
        RE: RunEngine,
        documents: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """build_local()'s plan isn't just structurally correct -- it's a
        genuine local Bluesky plan, runnable end to end with a real
        RunEngine and no queue server involved anywhere."""
        led, _uv_shutter, fast_shutter = optical_signals
        pump = fake_pumps["dds2_p1"]
        agent = BuildAgent(evaluation_method="xray", queue_server=False)
        agent.set_dofs([_pump()])
        agent.experiment(sources=[_source()])
        agent.set_xray_objectives(
            screening="unscreened",
            exposure=0.1,
            frame_acq_time=0.1,
            phases=[phase_factory()],
        )
        built = agent.build_local(
            devices={
                "dds-CsPb": pump,
                "led": led,
                "fast_shutter": fast_shutter,
                "xray_detector": fake_area_detector,
            },
            wrap_xray_run=lambda plan, no_dark: plan,
            # Real hardware defaults (30cm mixer, ratio=1.0) compute a real
            # multi-minute equilibrium wait from the pump rate -- zero it
            # out so this test actually finishes.
            mixer_lengths_cm=(0.0,),
            residence_time_ratio=0.0,
        )

        RE(built.acquisition_plan([{"_id": 1, "infusion_rate_CsPb": 25}], []))

        assert any(name == "start" for name, _ in documents)
        assert any(name == "stop" for name, _ in documents)
        assert pump.status.get() == "Stopped"


class TestRunSettings:
    def test_n_points_must_be_one(self) -> None:
        with pytest.raises(ValueError, match="n_points must be 1"):
            BuildAgent(evaluation_method="uvvis").set_run(n_points=5)

    def test_budget_cannot_be_set_twice(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            BuildAgent(evaluation_method="uvvis").set_run(
                extra_initialization_trials=5,
                generation_strategy={"initialization_budget": 10},
            )

    def test_configure_generation_strategy_adds_history_to_the_budget(self) -> None:
        calls: list[dict] = []
        client = SimpleNamespace(
            summarize=lambda: pd.DataFrame({"trial_index": range(154)}),
            configure_generation_strategy=lambda **kwargs: calls.append(kwargs),
        )
        agent = BuildAgent(evaluation_method="uvvis")
        agent.set_run(
            extra_initialization_trials=5,
            generation_strategy={"allow_exceeding_initialization_budget": True},
        )
        assert agent.configure_generation_strategy(SimpleNamespace(ax_client=client)) == 154
        assert calls == [
            {"allow_exceeding_initialization_budget": True, "initialization_budget": 159}
        ]

    def test_configure_generation_strategy_leaves_ax_defaults_when_unset(self) -> None:
        calls: list[dict] = []
        client = SimpleNamespace(
            summarize=lambda: pd.DataFrame({"trial_index": []}),
            configure_generation_strategy=lambda **kwargs: calls.append(kwargs),
        )
        agent = BuildAgent(evaluation_method="uvvis")
        assert agent.configure_generation_strategy(SimpleNamespace(ax_client=client)) == 0
        assert calls == []



def test_old_config_with_phase_simulated_still_loads(
    phase_factory: Callable[..., Phase],
) -> None:
    """Phases used to carry an unused "simulated" flag; from_config drops it."""
    config = TestConfigRoundTrip()._full_agent(phase_factory()).to_config()
    config["xray"]["phases"] = [
        {**phase, "simulated": False} for phase in config["xray"]["phases"]
    ]
    agent = BuildAgent.from_config(config)
    assert all(not hasattr(phase, "simulated") for phase in agent.phases)


class TestPumpIds:
    """Each DOF drives its own pump, matching its flow source's; sources aren't dilutions."""

    @staticmethod
    def _agent(pumps: list[tuple[str, str]], sources: list[tuple[str, str]], dilution=None):
        agent = BuildAgent(evaluation_method="uvvis", queue_server=False)
        agent.set_dofs([Pump(name=name, id=pump_id, bounds=(0, 200)) for name, pump_id in pumps])
        agent.experiment(
            sources=[
                FlowSource(dof=f"infusion_rate_{name}", pump=pump, precursor=name, sample_label=name)
                for name, pump in sources
            ],
            dilutions=[
                DilutionStage(pump=dilution, ratio=1.0, position="before_equilibrium",
                              syringe_ml=20, material="plastic_BD", target_ml=20)
            ] if dilution else None,
        )
        return agent

    def test_valid_setup_passes(self) -> None:
        self._agent([("Br", "dds2_p2"), ("I", "dds3_p2")], [("Br", "dds2_p2"), ("I", "dds3_p2")], "dds1_p1")

    def test_duplicate_pump_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="pump id dds2_p2 is used by DOFs Br, I"):
            BuildAgent(evaluation_method="uvvis", queue_server=False).set_dofs(
                [Pump(name="Br", id="dds2_p2", bounds=(0, 200)),
                 Pump(name="I", id="dds2_p2", bounds=(0, 200))]
            )

    def test_dof_pump_must_match_its_source(self) -> None:
        with pytest.raises(ValueError, match="infusion_rate_I: DOF pump id dds3_p2, flow source pump dds3_p1"):
            self._agent([("Br", "dds2_p2"), ("I", "dds3_p2")], [("Br", "dds2_p2"), ("I", "dds3_p1")])

    def test_source_cannot_also_be_a_dilution(self) -> None:
        with pytest.raises(ValueError, match="pump dds2_p2 is both a flow source and a dilution"):
            self._agent([("Br", "dds2_p2")], [("Br", "dds2_p2")], "dds2_p2")


@pytest.mark.parametrize("iterations", [0, -3, 2.5, None, True])
def test_run_iterations_must_be_a_positive_whole_number(iterations: Any) -> None:
    with pytest.raises(ValueError, match="run.iterations must be a whole number"):
        RunSettings(iterations=iterations)


@pytest.mark.parametrize("extra", [-1, 1.5, "3"])
def test_extra_initialization_trials_must_be_a_whole_number(extra: Any) -> None:
    with pytest.raises(ValueError, match="run.extra_initialization_trials"):
        RunSettings(extra_initialization_trials=extra)


@pytest.mark.parametrize(
    "bounds", [(300, 200), (10, 10), (-5, 200), (0, float("inf")), (0,), (0, 100, 200), (0, "200")]
)
def test_pump_bounds_must_be_an_increasing_pair_of_rates(bounds: Any) -> None:
    with pytest.raises(ValueError, match="bounds must be"):
        Pump(name="Br", id="dds2_p2", bounds=bounds)
