"""Materials-Project-backed simulated sandbox/tiled data for build_local()."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import numpy as np

from xpd_tools.optimization.legacy.tiled import (
    FakeTiledCatalog,
    FakeTiledRun,
    FakeTiledStream,
    run_from_documents,
)
from xpd_tools.optimization.simulation.structures import MPAPIUser

if TYPE_CHECKING:
    from bluesky.run_engine import RunEngine

    from xpd_tools.optimization.helpers.phases import Phase

PhaseWeightFn = Callable[[Mapping[str, Any], Mapping[str, str]], Mapping[str, float]]


def linear_dof_weights(
    start_doc: Mapping[str, Any],
    dof_for_phase: Mapping[str, str],
) -> dict[str, float]:
    """Toy composition model - Fake linear interpolation between DOF values."""
    rates = dict(zip(start_doc["dof_names"], start_doc["infuse_rates"], strict=True))
    raw = {name: max(0.0, rates.get(dof, 0.0)) for name, dof in dof_for_phase.items()}
    total = sum(raw.values())
    if total <= 0:
        return {name: 1.0 / len(raw) for name in raw}
    return {name: v / total for name, v in raw.items()}


def _synthesize_pl_spectrum(
    x_axis: np.ndarray, peak_nm: float, fwhm_nm: float, intensity: float
) -> np.ndarray:
    """Compute a Gaussian PL spectrum centered at peak_nm over x_axis."""
    sigma_nm = fwhm_nm / 2.3548  # FWHM -> Gaussian sigma
    return intensity * np.exp(-((x_axis - peak_nm) ** 2) / (2 * sigma_nm**2))


def _synthesize_absorbance_edge(
    x_axis: np.ndarray, plateau: float, edge_nm: float, edge_width_nm: float
) -> np.ndarray:
    """Compute a sigmoid absorption edge.

    Baseline correction needs a non-flat edge to avoid flat-spectrum artifacts.
    """
    return plateau / (1.0 + np.exp((x_axis - edge_nm) / edge_width_nm))


def _mix_or_constant(
    value: float | Mapping[str, float], weights: Mapping[str, float]
) -> float:
    """Mix a per-phase value by weights, or pass a single constant through."""
    if isinstance(value, Mapping):
        return sum(weights[name] * value[name] for name in weights)
    return value


def build_simulated_tiled_clients(
    RE: RunEngine,
    *,
    phases: Sequence[Phase] | None,
    dof_for_phase: Mapping[str, str],
    weight_fn: PhaseWeightFn = linear_dof_weights,
    q: tuple[float, float] = (0.5, 25.0),
    r: tuple[float, float] = (1.0, 22.0),
    qstep: float = 0.05,
    rstep: float = 0.05,
    pl_fwhm_nm: float | Mapping[str, float] = 20.0,
    pl_intensity: float = 5000.0,
    pl_phases: Sequence[str] | None = None,
    band_gap_ev: Mapping[str, float] | None = None,
    pl_alloy: Sequence[str] | None = None,
    peak_nm_range: tuple[float, float] = (410.0, 780.0),
    absorbance_value: float | Mapping[str, float] = 0.3,
    absorbance_edge_nm: float = 350.0,
    absorbance_edge_width_nm: float = 30.0,
    noise_level: float = 0.05,
    mp_api_key: str | None = None,
) -> tuple[FakeTiledCatalog, FakeTiledCatalog]:
    """
    Build and subscribe a Materials-Project-backed (tiled_client, sandbox_client).

    Args:
        - RE: the RunEngine to use for simulation.
        - phases: the list of `Phase` objects to simulate; None or empty (a UV-Vis-only
          agent has no phases) simulates the phases named in dof_for_phase.
        - dof_for_phase: a mapping of phase names to DOF names.
        - weight_fn: the function to use for DOF weight normalization.
        - q: the range of q values to simulate.
        - r: the range of r values to simulate
        - qstep, rstep: interpolation step size for q/r values.
        - pl_fwhm_nm, pl_intensity: each emitting phase's PL peak width (one value, or
          per phase) and the height of a pure emitter's peak.
        - pl_phases: the phases that emit (default: all). Each emits its own Gaussian
          at 1240/band_gap nm, scaled by its fraction of the whole mixture, so the
          other (dark) phases dim the PL instead of shifting it.
        - band_gap_ev: per-phase band gaps (eV) that replace Materials Project's DFT
          values, e.g. measured ones; phases not listed keep MP's.
        - pl_alloy: emitters that form one solid solution (e.g. CsPbCl3, CsPbBr3,
          CsPbI3 as mixed-halide CsPb(Cl/Br/I)3): they emit one peak at their
          weight-averaged gap (Vegard), so it moves continuously with the halide
          ratio instead of showing each end member's own peak.
        - peak_nm_range: clamp for each emitter's PL peak.
        - absorbance_value: the plateau height of a synthesized sigmoid absorption edge.
        - absorbance_edge_nm, absorbance_edge_width_nm: shape of the sigmoid edge.
        - noise_level: stddev of Gaussian noise for the signal.
        - mp_api_key: the Materials-Project API key to use for sandbox client.

    Returns
    -------
        - tiled_client: the simulated tiled client catalog.
        - sandbox_client: the simulated sandbox client catalog.
    """
    tiled_client = FakeTiledCatalog()
    sandbox_client = FakeTiledCatalog()

    names = [phase.name for phase in phases or ()] or list(dof_for_phase)
    mp_user = MPAPIUser(
        q=q,
        r=r,
        qstep=qstep,
        rstep=rstep,
        formulas=names,
        api_key=mp_api_key,
    )
    r_ref, gs = mp_user.load()
    phase_grs = dict(zip(names, gs.T, strict=True))
    phase_band_gaps = {**mp_user.band_gaps(), **(band_gap_ev or {})}

    documents: list[tuple[str, Mapping[str, Any]]] = []

    def _on_document(name: str, doc: Mapping[str, Any]) -> None:
        documents.append((name, doc))
        if name != "stop":
            return
        uid = doc["run_start"]
        run = run_from_documents(documents, uid)

        relevant_streams = {"scattering", "fluorescence", "absorbance"}
        needs_weights = bool(run.streams.keys() & relevant_streams)
        weights = (
            weight_fn(run.metadata["start"], dof_for_phase) if needs_weights else {}
        )

        if "scattering" in run.streams:
            """ Calculate the scattering GR_G and insert into sandbox client.

                1) Take G(r) from the scattering stream.
                2) Sum over all phases corrected for phase weights to get GR_G.
                3) Add any prescribed noise to disrupt the system.
            """
            gr_g = sum(weights[name] * phase_grs[name] for name in weights)
            if noise_level:
                gr_g = gr_g + np.random.normal(0.0, noise_level, size=gr_g.shape)
            sandbox_client.insert(
                uid,
                FakeTiledRun(
                    {"scattering": FakeTiledStream({"gr_r": r_ref, "gr_G": gr_g})},
                    metadata={"original_run_uid": uid},
                ),
            )

        if "fluorescence" in run.streams:
            """ Calculate the fluorescence and insert into tiled client.

                1) Each emitting phase (pl_phases) adds a Gaussian at its own
                    1240/band_gap nm (clipped to peak_nm_range), with height
                    pl_intensity x its fraction of the whole mixture: dark
                    phases dim the PL, they don't shift it. The pl_alloy
                    phases emit as one, at their weight-averaged gap.
                2) No emitters present gives a dark spectrum, which the PL fit
                    scores as no PL.
                3) Add any prescribed noise to disrupt the system.
            """
            x_axis = run.streams["fluorescence"].data["QEPro_x_axis"].values
            emitters = {
                name: weight
                for name, weight in weights.items()
                if (pl_phases is None or name in pl_phases)
                and weight > 0
                and phase_band_gaps[name] > 0
            }
            alloy = [name for name in emitters if name in (pl_alloy or ())]
            groups = [[name] for name in emitters if name not in alloy]
            groups += [alloy] if alloy else []
            synthetic = np.zeros_like(x_axis, dtype=float)
            for group in groups:
                group_weights = {name: emitters[name] for name in group}
                total = sum(group_weights.values())
                mix = {name: w / total for name, w in group_weights.items()}
                gap = _mix_or_constant(phase_band_gaps, mix)
                peak_nm = float(np.clip(1240.0 / gap, *peak_nm_range))
                synthetic = synthetic + _synthesize_pl_spectrum(
                    x_axis,
                    peak_nm,
                    _mix_or_constant(pl_fwhm_nm, mix),
                    pl_intensity * total,
                )
            if noise_level:
                synthetic = synthetic + np.random.normal(
                    0.0, noise_level, size=synthetic.shape
                )
            run.streams["fluorescence"] = FakeTiledStream(
                {"QEPro_x_axis": x_axis, "QEPro_output": synthetic}
            )

        if "absorbance" in run.streams:
            """ Calculate the absorbance and insert into sandbox client.

                1) Take absorbance from the simulated qe pro.
                2) Sum over all phases corrected for phase weights to get
                    the mixed absorbance.
                3) Simulate a synthetic edge to fake a baseline correction
                    and keep that process happy.
                4) Add any prescribed noise to disrupt the system.
            """
            x_axis = run.streams["absorbance"].data["QEPro_x_axis"].values
            mixed_absorbance = _mix_or_constant(absorbance_value, weights)
            synthetic_absorbance = _synthesize_absorbance_edge(
                x_axis, mixed_absorbance, absorbance_edge_nm, absorbance_edge_width_nm
            )
            if noise_level:
                synthetic_absorbance = synthetic_absorbance + np.random.normal(
                    0.0, noise_level, size=synthetic_absorbance.shape
                )
            run.streams["absorbance"] = FakeTiledStream(
                {"QEPro_x_axis": x_axis, "QEPro_output": synthetic_absorbance}
            )

        tiled_client.insert(uid, run)

    RE.subscribe(_on_document)
    return tiled_client, sandbox_client
