from __future__ import annotations

from pathlib import Path

from xpd_tools.optimization.helpers.phases import (
    Phase,
    _create_phase,
    _phases_to_pdf_schema,
)


def test_create_phase_applies_metric_prefix_and_minimize() -> None:
    phase = Phase(name="Wanted", gr="a.gr", cif="a.cif", minimize=True)

    raw = _create_phase(phase, metric_prefix="corr_")
    assert raw is not None
    assert raw.name == "corr_Wanted"
    assert raw.minimize is True

    fit = _create_phase(phase, metric_prefix="pdf_fit_corr_")
    assert fit is not None
    assert fit.name == "pdf_fit_corr_Wanted"


def test_phases_to_pdf_schema_resolves_paths_and_matches_load_schema() -> None:
    phases = [
        Phase(name="Wanted", gr="refdata/Wanted.gr", cif="refdata/Wanted.cif"),
        Phase(
            name="Impurity",
            gr="refdata/Impurity.gr",
            cif="refdata/Impurity.cif",
            minimize=True,
        ),
    ]

    schema = _phases_to_pdf_schema(phases)

    assert schema["schema_version"] == 1
    assert [phase["name"] for phase in schema["phases"]] == ["Wanted", "Impurity"]
    assert schema["phases"][1]["minimize"] is True
    for phase, raw in zip(phases, schema["phases"], strict=True):
        assert raw["gr_path"] == str(Path(phase.gr).resolve())
        assert raw["cif_path"] == str(Path(phase.cif).resolve())
        assert Path(raw["gr_path"]).is_absolute()
        assert Path(raw["cif_path"]).is_absolute()
        assert "constraint_profile" not in raw
        assert "scoring_function" not in raw


def test_phases_to_pdf_schema_applies_scoring_function_uniformly() -> None:
    phases = [
        Phase(name="Wanted", gr="a.gr", cif="a.cif"),
        Phase(name="Impurity", gr="b.gr", cif="b.cif", minimize=True),
    ]

    schema = _phases_to_pdf_schema(phases, scoring_function="cross_correlation")

    assert all(
        phase["scoring_function"] == "cross_correlation" for phase in schema["phases"]
    )
