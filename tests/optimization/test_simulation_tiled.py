from __future__ import annotations

from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from bluesky.run_engine import RunEngine
from ophyd import Signal

from xpd_tools.optimization.helpers.beamline import XrayPlanContext, XraySettings
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.helpers.qepro import PlqyReference
from xpd_tools.optimization.legacy.devices import FakeAreaDetector, FakePump
from xpd_tools.optimization.plans import (
    FlowSource,
    QualityPolicy,
    create_xray_plan,
    create_xray_uvvis_plan,
)
from xpd_tools.optimization.plugins import UvvisEvaluation
from xpd_tools.optimization.simulation.tiled import (
    _synthesize_absorbance_edge,
    _synthesize_pl_spectrum,
    build_simulated_tiled_clients,
    linear_dof_weights,
)


def test_linear_dof_weights_normalizes() -> None:
    start_doc = {
        "dof_names": ["infusion_rate_CsPb", "infusion_rate_Br"],
        "infuse_rates": [30.0, 90.0],
    }
    dof_for_phase = {"CsPbBr3": "infusion_rate_Br", "CsBr": "infusion_rate_CsPb"}
    weights = linear_dof_weights(start_doc, dof_for_phase)
    assert weights["CsPbBr3"] == pytest.approx(0.75)
    assert weights["CsBr"] == pytest.approx(0.25)


def test_linear_dof_weights_falls_back_to_uniform_when_all_zero() -> None:
    start_doc = {"dof_names": ["infusion_rate_CsPb"], "infuse_rates": [0.0]}
    dof_for_phase = {"CsPbBr3": "infusion_rate_CsPb", "CsBr": "infusion_rate_Br"}
    assert linear_dof_weights(start_doc, dof_for_phase) == {"CsPbBr3": 0.5, "CsBr": 0.5}


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_mixes_phase_pdfs(
    mock_mp_user_cls: Any,
    RE: RunEngine,
) -> None:
    """The point of build_simulated_tiled_clients(): different suggestions
    should score against different, DOF-weighted mixtures of real (here,
    faked-out) Materials-Project PDFs, instead of the same static G(r)
    every time.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    gr_wanted = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    gr_unwanted = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([gr_wanted, gr_unwanted]),
    )

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    tiled_client, sandbox_client = build_simulated_tiled_clients(
        RE, phases=phases, dof_for_phase=dof_for_phase, noise_level=0.0
    )
    mock_mp_user_cls.assert_called_once()
    call_kwargs = mock_mp_user_cls.call_args.kwargs
    assert call_kwargs["formulas"] == ["Wanted", "Unwanted"]
    # Defaults should be the cheaper ones, not MPAPIUser's own qstep=0.01/
    # rstep=0.01/r=(1.0, 30.0) -- see chat re: _debye_sum's O(n_atoms**2 *
    # n_q_points) cost.
    assert call_kwargs["qstep"] == 0.05
    assert call_kwargs["rstep"] == 0.05
    assert call_kwargs["r"] == (1.0, 22.0)

    pump = FakePump(name="dds2_p1")
    context = XrayPlanContext(
        led=Signal(name="led", value="Low"),
        fast_shutter=Signal(name="fast_shutter", value=20),
        xray_detector=FakeAreaDetector(name="xray_detector"),
        wrap_xray_run=lambda plan, no_dark: plan,
        sources=(
            FlowSource(
                dof="infusion_rate_CsPb",
                pump=pump,
                precursor="CsPbOA",
                sample_label="CsPb",
            ),
        ),
        dilutions=(),
        wash_cycles=(),
        mixer_lengths_cm=(0.0,),
        residence_time_ratio=0.0,
        xray=XraySettings(exposure=0.1, frame_acq_time=0.1),
    )
    plan = create_xray_plan(context)

    result = RE(plan([{"_id": 1, "infusion_rate_CsPb": 25.0}], []))
    run_uid = result.plan_result

    # Only infusion_rate_CsPb was suggested -- Br's weight is 0, so the
    # mixture should be exactly the "Wanted" phase's own PDF.
    stored = sandbox_client[run_uid]["scattering"].read()
    np.testing.assert_allclose(stored["gr_G"].values, gr_wanted)
    np.testing.assert_allclose(stored["gr_r"].values, r_ref)
    assert run_uid in tiled_client.runs


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_synthesizes_pl_peak_from_band_gap(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """The band-gap extension: tiled_client's fluorescence spectrum should
    be a synthetic Gaussian at 1240/band_gap_eV of the phase present, not
    whatever the (zero-valued) simulated qepro actually produced.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 2.0,
        "Unwanted": 4.0,
    }

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    tiled_client, _sandbox_client = build_simulated_tiled_clients(
        RE, phases=phases, dof_for_phase=dof_for_phase, noise_level=0.0
    )

    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    plan = create_xray_uvvis_plan(context)
    result = RE(
        plan(
            [
                {
                    "_id": 1,
                    "infusion_rate_CsPb": 30.0,
                    "infusion_rate_Br": 0.0,
                    "infusion_rate_I2": 0.0,
                }
            ],
            [],
        )
    )
    run_uid = result.plan_result

    # Only infusion_rate_CsPb was suggested -- Br's weight is 0, so the
    # spectrum should be "Wanted"'s own peak (2.0 eV) at full intensity.
    fl = tiled_client[run_uid]["fluorescence"].read()
    x_axis = fl["QEPro_x_axis"].values
    expected = _synthesize_pl_spectrum(x_axis, 1240.0 / 2.0, 20.0, 5000.0)
    np.testing.assert_allclose(fl["QEPro_output"].values, expected)

    # Absorbance is also synthesized -- a sigmoid edge plateauing at
    # absorbance_value, not a flat line (see _synthesize_absorbance_edge).
    abs_ = tiled_client[run_uid]["absorbance"].read()
    expected_absorbance = _synthesize_absorbance_edge(
        abs_["QEPro_x_axis"].values, 0.3, 350.0, 30.0
    )
    np.testing.assert_allclose(abs_["QEPro_output"].values, expected_absorbance)


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_makes_plqy_finite_end_to_end(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """Before absorbance synthesis, log_PLQY always hit helpers.qepro's
    1e-10 failure floor (ln(1e-10) ~= -23.0259) because the simulated
    qepro's real absorbance output was flat zero. With a real (if
    synthetic) absorbance spectrum, UvvisEvaluation's actual PLQY fit
    should now produce a real, sane (not just finite) value.

    A flat (constant) absorbance spectrum isn't enough on its own:
    helpers.analysis.correct_absorbance's linear baseline fit reproduces
    a flat line almost exactly, leaving a machine-epsilon residual whose
    *sign* is float noise -- calculate_plqy's division by that either
    blows up to a physically nonsensical PLQY (e.g. ~1e15, log~34) or
    goes negative and re-triggers the same 1e-10 floor. Bounding
    log_PLQY to a sane range here catches that regression; `!= 1e-10`
    alone would not have.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 2.0,
        "Unwanted": 4.0,
    }

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    # Noise off: absorbance noise at the default 0.05 can drive the corrected
    # absorbance at the excitation wavelength <= 0 and hit the 1e-10 floor.
    tiled_client, _sandbox_client = build_simulated_tiled_clients(
        RE, phases=phases, dof_for_phase=dof_for_phase, noise_level=0.0
    )

    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    plan = create_xray_uvvis_plan(context)
    result = RE(
        plan(
            [
                {
                    "_id": 1,
                    "infusion_rate_CsPb": 30.0,
                    "infusion_rate_Br": 0.0,
                    "infusion_rate_I2": 0.0,
                }
            ],
            [],
        )
    )
    run_uid = result.plan_result

    evaluation = UvvisEvaluation(
        tiled_client,
        plqy=PlqyReference(
            excitation_wavelength_nm=365,
            absorbance=0.361,
            pl_integral=952628,
            refractive_index=1.337,
            plqy=0.546,
            solvent_refractive_index=1.506,
        ),
        peak_target=1240.0 / 2.0,
    )
    outcomes = evaluation(run_uid, [{"_id": 1}])

    log_plqy = outcomes[0]["log_PLQY"]
    assert np.isfinite(log_plqy)
    # A real PLQY is in (0, 1], i.e. log_PLQY <= 0 -- generously bounded
    # below too, to catch a division-by-near-zero blowup either way.
    assert -20.0 < log_plqy <= 5.0


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_mixes_per_phase_fwhm_and_absorbance(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """pl_fwhm_nm as a {phase_name: value} mapping gives each emitter its own
    peak width; absorbance_value as a mapping mixes by the phase weights.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 2.0,
        "Unwanted": 4.0,
    }

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    tiled_client, _sandbox_client = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase=dof_for_phase,
        pl_fwhm_nm={"Wanted": 10.0, "Unwanted": 30.0},
        absorbance_value={"Wanted": 0.2, "Unwanted": 0.6},
        noise_level=0.0,
    )

    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    plan = create_xray_uvvis_plan(context)
    result = RE(
        plan(
            [
                {
                    "_id": 1,
                    # Equal weight this time -- 0.5/0.5 mix.
                    "infusion_rate_CsPb": 10.0,
                    "infusion_rate_Br": 10.0,
                    "infusion_rate_I2": 0.0,
                }
            ],
            [],
        )
    )
    run_uid = result.plan_result

    fl = tiled_client[run_uid]["fluorescence"].read()
    x_axis = fl["QEPro_x_axis"].values
    # Both phases emit (no pl_phases): each its own peak at half intensity;
    # Unwanted's 310 nm is clipped to peak_nm_range's 410 nm.
    expected_fl = _synthesize_pl_spectrum(
        x_axis, 1240.0 / 2.0, 10.0, 2500.0
    ) + _synthesize_pl_spectrum(x_axis, 410.0, 30.0, 2500.0)
    np.testing.assert_allclose(fl["QEPro_output"].values, expected_fl)

    abs_ = tiled_client[run_uid]["absorbance"].read()
    expected_absorbance = _synthesize_absorbance_edge(
        abs_["QEPro_x_axis"].values, 0.5 * 0.2 + 0.5 * 0.6, 350.0, 30.0
    )
    np.testing.assert_allclose(abs_["QEPro_output"].values, expected_absorbance)


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_clamps_extreme_band_gap_to_a_fittable_peak(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """A large band gap (e.g. an ionic salt mixed in alongside a real
    semiconductor) can put 1240/mixed_eg well below 400nm, which
    helpers.qepro.classify_pl hardcodes as "not real PL" and discards --
    silently reproducing the same Peak=0.0 failure floor this whole
    feature was built to avoid. The clamp must keep it fittable.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    # 10 eV -> 124nm unclamped -- far below classify_pl's 400nm floor.
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 10.0,
        "Unwanted": 10.0,
    }

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    tiled_client, _sandbox_client = build_simulated_tiled_clients(
        RE, phases=phases, dof_for_phase=dof_for_phase
    )

    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    plan = create_xray_uvvis_plan(context)
    result = RE(
        plan(
            [
                {
                    "_id": 1,
                    "infusion_rate_CsPb": 30.0,
                    "infusion_rate_Br": 0.0,
                    "infusion_rate_I2": 0.0,
                }
            ],
            [],
        )
    )
    run_uid = result.plan_result

    fl = tiled_client[run_uid]["fluorescence"].read()
    x_axis = fl["QEPro_x_axis"].values.reshape(-1)
    output = fl["QEPro_output"].values.reshape(-1)
    assert x_axis[int(np.argmax(output))] >= 400.0

    evaluation = UvvisEvaluation(
        tiled_client,
        plqy=PlqyReference(
            excitation_wavelength_nm=365,
            absorbance=0.361,
            pl_integral=952628,
            refractive_index=1.337,
            plqy=0.546,
            solvent_refractive_index=1.506,
        ),
        peak_target=450,
    )
    outcomes = evaluation(run_uid, [{"_id": 1}])
    assert outcomes[0]["Peak"] != 0.0


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_pl_phases_restricts_pl_mix(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """pl_phases=("Wanted",): only Wanted emits, at its own band gap/FWHM;
    Unwanted is dark, so at 0.5/0.5 it halves the PL instead of shifting it.
    Absorbance still mixes over all phases.
    """
    r_ref = np.linspace(1.0, 25.0, 5)
    mock_mp_user_cls.return_value.load.return_value = (
        r_ref,
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 2.0,
        "Unwanted": 10.0,
    }

    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}

    tiled_client, _sandbox_client = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase=dof_for_phase,
        pl_phases=("Wanted",),
        pl_fwhm_nm={"Wanted": 10.0, "Unwanted": 30.0},
        absorbance_value={"Wanted": 0.2, "Unwanted": 0.6},
        noise_level=0.0,
    )

    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    plan = create_xray_uvvis_plan(context)
    result = RE(
        plan(
            [
                {
                    "_id": 1,
                    # Equal weight -- Unwanted's 10 eV gap would emit at the
                    # 410 nm clip if it weren't excluded by pl_phases.
                    "infusion_rate_CsPb": 10.0,
                    "infusion_rate_Br": 10.0,
                    "infusion_rate_I2": 0.0,
                }
            ],
            [],
        )
    )
    run_uid = result.plan_result

    fl = tiled_client[run_uid]["fluorescence"].read()
    expected_fl = _synthesize_pl_spectrum(
        fl["QEPro_x_axis"].values, 1240.0 / 2.0, 10.0, 2500.0
    )
    np.testing.assert_allclose(fl["QEPro_output"].values, expected_fl)

    # Absorbance still mixes over all phases -- unaffected by pl_phases.
    abs_ = tiled_client[run_uid]["absorbance"].read()
    expected_absorbance = _synthesize_absorbance_edge(
        abs_["QEPro_x_axis"].values, 0.5 * 0.2 + 0.5 * 0.6, 350.0, 30.0
    )
    np.testing.assert_allclose(abs_["QEPro_output"].values, expected_absorbance)


def _pl_peak_nm(
    tiled_client: Any, RE: RunEngine, plan_context_factory: Any, rates: dict
) -> float:
    """Run one xray+uvvis point at these rates; return the PL maximum (nm)."""
    context = plan_context_factory(
        quality=QualityPolicy(enabled=False, absorbance_shots=1, fluorescence_shots=1)
    )
    run_uid = RE(create_xray_uvvis_plan(context)([{"_id": 1, **rates}], [])).plan_result
    fl = tiled_client[run_uid]["fluorescence"].read()
    x_axis = fl["QEPro_x_axis"].values.reshape(-1)
    return float(x_axis[int(np.argmax(fl["QEPro_output"].values.reshape(-1)))])


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_pl_peak_follows_the_dominant_emitter(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """With two emitters, the PL maximum sits at whichever one the DOFs make
    dominant -- each emits at its own band gap, so the peak moves between
    their positions instead of averaging the gaps into one."""
    mock_mp_user_cls.return_value.load.return_value = (
        np.linspace(1.0, 25.0, 5),
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {"Red": 2.0, "Blue": 2.5}
    phases = [
        Phase(name="Red", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Blue", gr="unused.gr", cif="unused.cif", minimize=False),
    ]
    tiled_client, _ = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase={"Red": "infusion_rate_CsPb", "Blue": "infusion_rate_Br"},
        noise_level=0.0,
    )

    red = _pl_peak_nm(
        tiled_client,
        RE,
        plan_context_factory,
        {"infusion_rate_CsPb": 30.0, "infusion_rate_Br": 10.0, "infusion_rate_I2": 0.0},
    )
    blue = _pl_peak_nm(
        tiled_client,
        RE,
        plan_context_factory,
        {"infusion_rate_CsPb": 10.0, "infusion_rate_Br": 30.0, "infusion_rate_I2": 0.0},
    )
    assert red == pytest.approx(620.0, abs=1.0)
    assert blue == pytest.approx(496.0, abs=1.0)


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_pl_alloy_emits_one_interpolated_peak(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """pl_alloy phases are one solid solution (mixed-halide perovskite): a single
    peak at the weight-averaged gap, moving continuously with the ratio, not two
    end-member peaks."""
    mock_mp_user_cls.return_value.load.return_value = (
        np.linspace(1.0, 25.0, 5),
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {"Red": 2.0, "Blue": 2.5}
    phases = [
        Phase(name="Red", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Blue", gr="unused.gr", cif="unused.cif", minimize=False),
    ]
    tiled_client, _ = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase={"Red": "infusion_rate_CsPb", "Blue": "infusion_rate_Br"},
        pl_alloy=("Red", "Blue"),
        noise_level=0.0,
    )

    rates = {
        "infusion_rate_CsPb": 30.0,
        "infusion_rate_Br": 10.0,
        "infusion_rate_I2": 0.0,
    }
    peak = _pl_peak_nm(tiled_client, RE, plan_context_factory, rates)
    assert peak == pytest.approx(1240.0 / (0.75 * 2.0 + 0.25 * 2.5), abs=1.0)  # 584 nm


@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_band_gap_ev_overrides_mp(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
) -> None:
    """band_gap_ev replaces MP's DFT gap for the phases it lists (here the
    emitter: 2.8824 eV DFT -> 2.38 eV measured), and leaves the others."""
    mock_mp_user_cls.return_value.load.return_value = (
        np.linspace(1.0, 25.0, 5),
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {
        "Wanted": 2.8824,
        "Unwanted": 2.0,
    }
    phases = [
        Phase(name="Wanted", gr="unused.gr", cif="unused.cif", minimize=False),
        Phase(name="Unwanted", gr="unused.gr", cif="unused.cif", minimize=True),
    ]
    dof_for_phase = {"Wanted": "infusion_rate_CsPb", "Unwanted": "infusion_rate_Br"}
    tiled_client, _ = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase=dof_for_phase,
        band_gap_ev={"Wanted": 2.38},
        noise_level=0.0,
    )

    wanted = _pl_peak_nm(
        tiled_client,
        RE,
        plan_context_factory,
        {"infusion_rate_CsPb": 30.0, "infusion_rate_Br": 0.0, "infusion_rate_I2": 0.0},
    )
    unwanted = _pl_peak_nm(
        tiled_client,
        RE,
        plan_context_factory,
        {"infusion_rate_CsPb": 0.0, "infusion_rate_Br": 30.0, "infusion_rate_I2": 0.0},
    )
    assert wanted == pytest.approx(1240.0 / 2.38, abs=1.0)  # 521 nm, not MP's 430
    assert unwanted == pytest.approx(1240.0 / 2.0, abs=1.0)  # MP's own gap


@pytest.mark.parametrize("phases", [None, ()])
@patch("xpd_tools.optimization.simulation.tiled.MPAPIUser")
def test_build_simulated_tiled_clients_without_phases_uses_dof_for_phase(
    mock_mp_user_cls: Any,
    RE: RunEngine,
    plan_context_factory: Any,
    phases: Any,
) -> None:
    """A UV-Vis-only agent has no phases (only set_xray_objectives sets them):
    notebooks pass build_agent.phases (None), the GUI phases=(). Either way the
    phases named in dof_for_phase are simulated."""
    mock_mp_user_cls.return_value.load.return_value = (
        np.linspace(1.0, 25.0, 5),
        np.column_stack([np.ones(5), np.ones(5)]),
    )
    mock_mp_user_cls.return_value.band_gaps.return_value = {"Red": 2.0, "Blue": 2.5}
    tiled_client, _ = build_simulated_tiled_clients(
        RE,
        phases=phases,
        dof_for_phase={"Red": "infusion_rate_CsPb", "Blue": "infusion_rate_Br"},
        noise_level=0.0,
    )
    assert mock_mp_user_cls.call_args.kwargs["formulas"] == ["Red", "Blue"]

    rates = {
        "infusion_rate_CsPb": 30.0,
        "infusion_rate_Br": 0.0,
        "infusion_rate_I2": 0.0,
    }
    assert _pl_peak_nm(tiled_client, RE, plan_context_factory, rates) == (
        pytest.approx(620.0, abs=1.0)
    )


def test_missing_mp_api_key_says_where_to_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xpd_tools.optimization.simulation.structures import MPAPIUser

    monkeypatch.delenv("MP_API_KEY", raising=False)
    with pytest.raises(ValueError, match=r"MP_API_KEY isn't set.*\.env"):
        MPAPIUser(q=(0.5, 25.0), r=(1.0, 22.0), formulas=["CsPbBr3"])
