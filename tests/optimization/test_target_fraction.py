from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from xpd_tools.optimization.agent import BuildAgent, _load_historical_data
from xpd_tools.optimization.helpers.pdf import (
    _load_pdf_references,
    _raw_pdf_correlations,
)
from xpd_tools.optimization.helpers.phases import Phase, _write_pdf_references


class _FakeCnn:
    """Stands in for CnnScorer: a fixed 0.7 CsPbBr3 / 0.3 Cs4PbBr6 prediction."""

    def score(self, r, g, phase_names, r_min, r_max):
        pred = {"CsPbBr3": 0.7, "Cs4PbBr6": 0.3}
        return {name: pred[name] for name in phase_names}


def test_cnn_reports_fraction_and_squared_distance(tmp_path: Path) -> None:
    phases = [
        Phase(name="CsPbBr3", gr="a.gr", cif="a.cif", target_fraction=0.6),
        Phase(name="Cs4PbBr6", gr="b.gr", cif="b.cif"),
    ]
    refs = _load_pdf_references(
        _write_pdf_references(phases, tmp_path, scoring_function="cnn")
    )
    assert [p.target_fraction for p in refs] == [0.6, None]

    r = np.linspace(1, 30, 100)
    scores = _raw_pdf_correlations(
        refs, {"gr_r": r, "gr_G": np.sin(r)}, ensemble_scorers={}, cnn_scorer=_FakeCnn()
    )
    assert scores == pytest.approx(
        {"frac_CsPbBr3": 0.7, "frac_dist_CsPbBr3": 0.01, "frac_Cs4PbBr6": 0.3}
    )


def _cnn_agent(phases: list[Phase], fraction_mode: bool = True) -> BuildAgent:
    agent = BuildAgent(evaluation_method="xray")
    agent.set_xray_objectives(
        objective_function="cnn",
        phases=phases,
        cnn_dataset_path="dataset_pc.npz",
        cnn_weights_path="amortized_encoder.pt",
        fraction_mode=fraction_mode,
    )
    # Objectives are assembled at build time (58034a9); tests read them directly.
    agent._assemble_objectives()
    return agent


def test_fraction_mode_targets_with_constraints() -> None:
    agent = _cnn_agent(
        [
            Phase(name="A", gr="", cif="", target_fraction=0.6, fraction_tolerance=0.1),
            Phase(name="B", gr="", cif="", target_fraction=0.2),
        ]
    )
    assert [(o.name, o.minimize) for o in agent.objectives] == [
        ("frac_dist_A", True),
        ("frac_dist_B", True),
    ]
    assert [str(c) for c in agent._outcome_constraints(needs_plqy=False)] == [
        "frac_A >= 0.5",
        "frac_A <= 0.7",
        "frac_B >= 0.15",
        "frac_B <= 0.25",
    ]
    assert BuildAgent.from_config(agent.to_config()).fraction_mode is True


def test_without_fraction_mode_raw_fractions_are_optimized() -> None:
    agent = _cnn_agent(
        [Phase(name="A", gr="", cif=""), Phase(name="B", gr="", cif="", minimize=True)],
        fraction_mode=False,
    )
    assert [(o.name, o.minimize) for o in agent.objectives] == [
        ("frac_A", False),
        ("frac_B", True),
    ]
    assert agent._outcome_constraints(needs_plqy=False) == ()


@pytest.mark.parametrize(
    ("target", "fraction_mode", "match"),
    [(None, True, "missing: A"), (0.5, False, "set on: A")],
)
def test_fraction_mode_must_match_targets(target, fraction_mode, match) -> None:
    with pytest.raises(ValueError, match=match):
        _cnn_agent(
            [Phase(name="A", gr="", cif="", target_fraction=target)], fraction_mode
        )


def test_fraction_mode_requires_cnn() -> None:
    with pytest.raises(ValueError, match="requires objective_function='cnn'"):
        BuildAgent(evaluation_method="xray").set_xray_objectives(fraction_mode=True)


def test_cnn_history_recomputes_distance_and_ignores_correlations(
    tmp_path: Path,
) -> None:
    agent = _cnn_agent([Phase(name="A", gr="", cif="", target_fraction=0.6)])
    objectives = [o.name for o in agent.objectives]
    csv = tmp_path / "history.csv"
    # frac_dist_A saved against an old target; recomputed for the current one.
    pd.DataFrame({"x": [1.0], "frac_A": [0.4], "frac_dist_A": [9.0]}).to_csv(csv)
    rows = _load_historical_data(
        csv, ["x"], objectives, optional_names=["frac_A"], fraction_targets={"A": 0.6}
    )
    assert rows == [pytest.approx({"x": 1.0, "frac_dist_A": 0.04, "frac_A": 0.4})]

    pd.DataFrame({"x": [1.0], "corr_A": [0.9]}).to_csv(csv)
    with pytest.raises(ValueError, match="none of the configured objectives"):
        _load_historical_data(csv, ["x"], objectives, fraction_targets={"A": 0.6})


@pytest.mark.parametrize(
    ("phase", "match"),
    [
        (
            {"scoring_function": "pearson", "gr_path": "x.gr", "target_fraction": 0.5},
            "requires scoring_function",
        ),
        ({"scoring_function": "cnn", "target_fraction": 1.5}, r"in \[0, 1\]"),
        (
            {"scoring_function": "cnn", "target_fraction": 0.5, "minimize": True},
            "own direction",
        ),
    ],
)
def test_target_fraction_rejects_bad_config(
    tmp_path: Path, phase: dict, match: str
) -> None:
    (tmp_path / "x.gr").write_text("1 0\n2 1\n")
    config = tmp_path / "c.json"
    config.write_text(
        json.dumps(
            {"schema_version": 1, "phases": [{"name": "A", "minimize": False, **phase}]}
        )
    )
    with pytest.raises(ValueError, match=match):
        _load_pdf_references(config)


def test_target_fractions_must_not_sum_above_one(tmp_path: Path) -> None:
    config = tmp_path / "c.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phases": [
                    {
                        "name": n,
                        "minimize": False,
                        "scoring_function": "cnn",
                        "target_fraction": 0.6,
                    }
                    for n in "AB"
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="sum to 1.2"):
        _load_pdf_references(config)
