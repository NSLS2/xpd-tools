"""Blop optimizer and Queue Server integration."""

import json
import logging
import re
import tempfile
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from ax.api.protocols import IMetric
from blop.ax import Objective, OutcomeConstraint, RangeDOF
from blop.ax.agent import Agent
from blop.ax.queueserver_agent import QueueserverAgent
from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.http import REManagerAPI
from tiled.client import from_profile, from_uri

from xpd_tools.optimization import plugins
from xpd_tools.optimization.cli import (
    DEFAULT_SANDBOX_URI,
    DEFAULT_TILED_PROFILE,
    SANDBOX_CATALOG,
)
from xpd_tools.optimization.helpers.beamline import (
    UvvisPlanContext,
    XrayPlanContext,
    XraySettings,
    XrayUvvisPlanContext,
)
from xpd_tools.optimization.helpers.dofs import Pump, _create_pump
from xpd_tools.optimization.helpers.phases import (
    Phase,
    _create_phase,
    _phase_constraints,
    _write_pdf_references,
)
from xpd_tools.optimization.helpers.qepro import (
    PlqyReference,
    QualityPolicy,
    SpectraFitSettings,
)
from xpd_tools.optimization.helpers.run import LocalRunSettings, RunSettings
from xpd_tools.optimization.plans import (
    DilutionStage,
    FlowSource,
    WashCycle,
    create_uvvis_plan,
    create_xray_plan,
    create_xray_screened_plan,
    create_xray_uvvis_plan,
)
from xpd_tools.optimization.scoring import _ALL_SCORING_NAMES
from xpd_tools.optimization.stopping import SuccessCriteria

# qNEHVI hypervolume acquisition degrades past this many objectives: the Pareto
# front saturates (nearly every trial becomes non-dominated) and per-objective
# improvement signal stalls. Emit an advisory warning when exceeded.
_MANY_OBJECTIVES_WARN = 4

logger = logging.getLogger(__name__)

_EVALUATORS = {
    "uvvis": plugins.UvvisEvaluation,
    "xray": plugins.XrayEvaluation,
    "xray-uvvis": plugins.XrayUvvisEvaluation,
}

# build_local()'s counterpart to build()'s _acquisition_plan_name(): the
# same plan-name dispatch, but resolved to the real local callable instead
# of a Queue Server plan-name string.
_LOCAL_PLAN_FACTORIES = {
    "uvvis_acquire": create_uvvis_plan,
    "xray_acquire": create_xray_plan,
    "xray_screened_acquire": create_xray_screened_plan,
    "xray_uvvis_acquire": create_xray_uvvis_plan,
}


def _load_historical_data(
    path: str | Path,
    dof_names: Sequence[str],
    objective_names: Sequence[str],
    *,
    optional_names: Sequence[str] = (),
    peak_target: float | None = None,
    fraction_targets: Mapping[str, float] | None = None,
) -> list[dict[str, float]]:
    """Load historical observations from a CSV for seeding the optimizer.

    Expects columns named exactly like the configured DOFs (e.g. an export
    from `ax_client.summarize()`).

    Only `dof_names` are required as they anchor each point in parameter space
    for `AxOptimizer.ingest`'s `attach_trial`.

    `peak_distance` is recomputed from `Peak` and `peak_target` when both are
    available, since a saved value is relative to whatever target the CSV was
    exported with. `frac_dist_{name}` is likewise recomputed from `frac_{name}`
    and `fraction_targets[name]`. Rows with NaN/inf in any loaded column are
    dropped.
    """
    frame = pd.read_csv(path)
    for name, target in (fraction_targets or {}).items():
        if f"frac_{name}" in frame.columns and f"frac_dist_{name}" in objective_names:
            frame[f"frac_dist_{name}"] = (frame[f"frac_{name}"] - target) ** 2
    if (
        peak_target is not None
        and "Peak" in frame.columns
        and "peak_distance" in objective_names
    ):
        frame["peak_distance"] = (frame["Peak"] - peak_target).abs()
    missing_dofs = [name for name in dof_names if name not in frame.columns]
    if missing_dofs:
        raise ValueError(
            f"{path}: historical data is missing columns: {', '.join(missing_dofs)}"
        )

    present_objectives = [name for name in objective_names if name in frame.columns]
    if objective_names and not present_objectives:
        raise ValueError(
            f"{path}: historical data has none of the configured objectives "
            f"({', '.join(objective_names)}) -- likely the wrong file"
        )

    columns = [
        *dof_names,
        *present_objectives,
        *(name for name in optional_names if name in frame.columns),
    ]
    values = frame[columns].astype(float)
    finite = np.isfinite(values)
    keep = finite.all(axis=1)
    if not keep.all():
        # Which columns caused it, e.g. "infusion_rate_I: 154"
        detail = ", ".join(
            f"{column}: {int((~finite[column]).sum())}"
            for column in columns
            if not finite[column].all()
        )
        if not keep.any():
            # Nothing usable is a config/data mismatch, not intended filtering.
            raise ValueError(
                f"{path}: every one of {len(values)} historical rows has NaN/inf in a "
                f"loaded column ({detail}) -- check the CSV matches the configured "
                "DOFs and objectives"
            )
        logger.warning(
            "%s: dropping %d of %d historical rows with NaN/inf values (%s)",
            path,
            int((~keep).sum()),
            len(values),
            detail,
        )
    return values[keep].to_dict(orient="records")


# ---------------------------------------------------------------------------
# Feasibility helpers
# ---------------------------------------------------------------------------

_CONSTRAINT_RE = re.compile(
    r"^\s*(?P<metric>\S+)\s*(?P<op><=|>=|<|>)\s*(?P<bound>[+-]?\S+)\s*$"
)


def _parse_outcome_constraint(ax_str: str) -> tuple[str, str, float] | None:
    """Parse an Ax outcome-constraint string into (metric, op, bound).

    Returns None when the string doesn't match the expected pattern.
    """
    m = _CONSTRAINT_RE.match(ax_str)
    if m is None:
        return None
    try:
        bound = float(m.group("bound"))
    except ValueError:
        return None
    return m.group("metric"), m.group("op"), bound


def _check_feasibility(
    df: pd.DataFrame,
    constraints: Sequence[Any],
) -> dict[str, Any]:
    """Evaluate feasibility of completed trials against outcome constraints.

    Returns a dict with keys:
        - parsed: list of (metric, op, bound) tuples from constraints
        - feasible_count: int, rows satisfying all constraints (no NaNs)
        - total_count: int, rows in df
        - per_constraint: list of dicts with metric/op/bound/satisfied\
            /observed_min/observed_max
    """
    ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
           "<": lambda a, b: a < b, ">": lambda a, b: a > b}

    parsed = []
    for c in constraints:
        result = _parse_outcome_constraint(str(c))
        if result is not None:
            parsed.append(result)

    per_constraint = []
    feasible_mask = pd.Series([True] * len(df), index=df.index)

    for metric, op, bound in parsed:
        if metric not in df.columns:
            continue
        col = df[metric]
        finite_mask = col.notna() & np.isfinite(col)
        satisfies = finite_mask & ops[op](col, bound)
        feasible_mask = feasible_mask & satisfies
        per_constraint.append({
            "metric": metric,
            "op": op,
            "bound": bound,
            "satisfied": int(satisfies.sum()),
            "observed_min": float(col[finite_mask].min()) if finite_mask.any()
                else float("nan"),
            "observed_max": float(col[finite_mask].max()) if finite_mask.any()
                else float("nan"),
        })

    return {
        "parsed": parsed,
        "feasible_count": int(feasible_mask.sum()),
        "total_count": len(df),
        "per_constraint": per_constraint,
    }


def _infeasibility_message(report: dict[str, Any]) -> str:
    """Build a human-readable infeasibility error from a _check_feasibility report."""
    total = report["total_count"]
    lines = [
        f"Multi-objective threshold inference requires ≥1 observation satisfying "
        f"all outcome constraints, but all {total} completed trial(s) are infeasible.",
        "Constraint summary (rows satisfied / total):",
    ]
    for c in report["per_constraint"]:
        satisfied = c["satisfied"]
        if np.isnan(c["observed_min"]):
            obs = "no finite observations"
        else:
            obs = f"observed {c['observed_min']:.4g}–{c['observed_max']:.4g}"
        flag = "" if satisfied > 0 else " ← zero satisfied"
        lines.append(
            f"  {c['metric']} {c['op']} {c['bound']:.6g}: "
            f"{satisfied}/{total} ({obs}){flag}"
        )
    lines.append(
        "Relax the bounds above (or seed a feasible historical observation) "
        "to let MBM start with a non-empty Pareto front."
    )
    return "\n".join(lines)


# Error signatures produced by Ax/BoTorch when MBM runs with zero feasible
# observations.  We catch only these exact strings so unrelated errors still
# propagate unchanged.
_INFEASIBILITY_SIGNATURES = (
    "cannot convert float NaN to integer",
    "probability tensor contains either `inf`, `nan` or element < 0",
)


class BuildAgent:
    """Agent to screen and build the run agent."""

    def __init__(self,
                # Queue Server
                queue_server: bool = True,
                # HTTP Client
                http_server_uri: str | None = None,
                http_api_key: str | None = None,
                # ZMQ
                zmq_consumer_address: str | None = None,
                # Tiled (tiled_uri takes precedence over tiled_profile when set)
                tiled_profile: str | None = None,
                tiled_uri: str | None = None,
                # Tiled sandbox catalog where pdfstream writes analysis results
                sandbox_uri: str = DEFAULT_SANDBOX_URI,
                # PDF correlation mode:
                #   "raw"         -- raw measured-G(r) correlation is the
                #                    objective; pdffit2 refinement never runs.
                #   "fit"         -- pdffit2-refined correlation is the
                #                    objective; a refinement failure is fatal.
                #   "raw_tracked" -- raw correlation is the objective (like
                #                    "raw"), but pdffit2 refinement is also
                #                    attempted every evaluation purely for
                #                    comparison/tracking -- a refinement
                #                    failure is logged and evaluation
                #                    continues with the raw result only.
                pdf_mode: Literal["raw", "fit", "raw_tracked"] = "raw",
                # Evaluation Method
                evaluation_method: str | None = None,
                # Agent Historical Data
                agent_data_path: str | None = None,
                # Optimizer checkpoint (distinct from agent_data_path: this is
                # blop's own state file, not historical observations to ingest)
                checkpoint_path: str | Path | None = None,
        ) -> None:
        # Queue Server
        self.queue_server = queue_server
        # HTTP Client
        self.http_server_uri = http_server_uri
        self.http_api_key = http_api_key
        # ZMQ
        self.zmq_consumer_address = zmq_consumer_address
        # Tiled
        self.tiled_profile = tiled_profile
        self.tiled_uri = tiled_uri
        self.sandbox_uri = sandbox_uri
        # Optimizer checkpoint
        self.checkpoint_path = checkpoint_path
        # Historical data CSV to seed the optimizer with (distinct from
        # checkpoint_path -- see above)
        self.agent_data_path = agent_data_path

        # Evaluation Function
        if evaluation_method not in [
            "uvvis",
            "xray",
            "xray-uvvis",
            None]:
            raise ValueError(
                "Evaluation method must be 'uvvis', 'xray', or 'xray-uvvis' (or None)."
            )
        self.evaluation_method = evaluation_method

        # PDF correlation mode -- see the docstring above pdf_mode's parameter.
        self.pdf_mode = pdf_mode

        # DOFs and flow sources: either may be configured first; see
        # _check_dof_source_alignment for the cross-check applied once both exist.
        self.dofs: list[RangeDOF] | None = None
        # Retained alongside self.dofs -- RangeDOF drops Pump.id (the
        # hardware address), which a config round-trip needs back.
        self.pumps: list[Pump] | None = None
        self.sources: list[FlowSource] | None = None
        self.dilutions: list[DilutionStage] | None = None
        self.wash_cycles: list[WashCycle] | None = None
        self.phases: list[Phase] | None = None
        self.plqy: PlqyReference | None = None
        self.fit_settings: SpectraFitSettings | None = None
        self.xray_settings: XraySettings | None = None
        self.screening: Literal[
            "unscreened",
            "screen_only",
            "screen_and_record"
        ] | None = None
        self.quality_policy: QualityPolicy | None = None
        self.xray_max_retries: int | None = None
        self.xray_retry_delay: float | None = None
        self.min_radius: float | None = None
        self.max_radius: float | None = None
        self.objective_function: str | None = None
        self.cnn_dataset_path: str | None = None
        self.cnn_weights_path: str | None = None
        self.fraction_mode: bool = False
        self.peak_target: float | None = None
        self.peak_tolerance: float | None = None
        # UV-Vis objective/constraint configuration -- set by set_uvvis_objectives.
        # Each metric can be an "objective" (Pareto dim) or a "constraint" (gate).
        # Bounds/thresholds are the worst acceptable values (used as Pareto ref
        # points for objectives, or as constraint limits for constraints).
        self.fwhm_role: Literal["objective", "constraint"] = "objective"
        self.plqy_role: Literal["objective", "constraint"] = "objective"
        self.peak_distance_role: Literal["objective", "constraint"] = "objective"
        self.max_fwhm_nm: float | None = None
        self.min_plqy: float | None = None
        self.max_peak_distance_nm: float | None = None
        self.uvvis_max_retries: int | None = None
        self.uvvis_retry_delay: float | None = None
        self.metadata: dict[str, Any] | None = None
        self.metadata_string: str | None = None
        # Optional early-stop thresholds for build()'s campaign -- see
        # set_success_criteria/stopping.watch_and_stop. Doesn't change
        # build()/run() at all on its own; the existing iterations=
        # stopping mode is unaffected whether or not this is configured.
        self.success_criteria: SuccessCriteria | None = None
        # How the campaign runs (iterations, generation strategy, local-run settings);
        # always set, so to_config() always writes "run". See set_run().
        self.run_settings: RunSettings = RunSettings()

        # Objectives -- assembled lazily by _assemble_objectives() at build time
        # so that UV-Vis and phase roles are both known before the list is built.
        self.objectives: list[Objective] = []

    def _check_dof_source_alignment(self) -> None:
        """Cross-check configured DOFs, their pump ids, and the flow sources.

        Pump ids must be unique across DOFs as soon as `set_dofs` is called.
        """
        if self.pumps is not None:
            by_id: dict[str, list[str]] = {}
            for pump in self.pumps:
                by_id.setdefault(pump.id, []).append(pump.name)
            shared = {pump_id: names for pump_id, names in by_id.items()
                    if len(names) > 1}
            if shared:
                raise ValueError(
                    "each DOF needs its own pump: "
                    + "; ".join(
                        f"pump id {pump_id} is used by DOFs {', '.join(names)}"
                        for pump_id, names in shared.items()
                    )
                )
        if self.dofs is None or self.sources is None:
            return
        dof_names = {dof.name for dof in self.dofs}
        source_names = {source.dof for source in self.sources}
        if dof_names != source_names:
            raise ValueError(
                "DOFs and flow sources must reference the same names: "
                f"sources missing a DOF: {sorted(source_names - dof_names)}; "
                f"DOFs missing a source: {sorted(dof_names - source_names)}"
            )
        if self.pumps is not None:
            pump_for_dof = {_create_pump(pump).name: pump.id for pump in self.pumps}
            mismatched = [
                f"{source.dof}: DOF pump id {pump_for_dof[source.dof]}, "
                f"flow source pump {source.pump}"
                for source in self.sources
                if source.dof in pump_for_dof and
                    pump_for_dof[source.dof] != source.pump
            ]
            if mismatched:
                raise ValueError(
                    "each DOF's pump id must be its flow source's pump: "
                    + "; ".join(mismatched)
                )
        both = sorted(
            {str(source.pump) for source in self.sources}
            & {str(stage.pump) for stage in self.dilutions or ()}
        )
        if both:
            raise ValueError(
                f"pump {', '.join(both)} is both a flow source and a dilution; "
                "they run at the same time, so each needs its own pump"
            )

    def set_dofs(self,
        pumps: Sequence[Pump]) -> None:
        # Normalized to a list regardless of what sequence type the caller
        # passed in (tuple, list, ...) -- from_config() always rebuilds
        # this as a list, so a hand-built and a config-rebuilt agent would
        # otherwise disagree on `self.pumps`'s type despite equal contents.
        self.pumps = list(pumps)
        self.dofs = [_create_pump(pump) for pump in pumps]
        self._check_dof_source_alignment()

    def set_xray_objectives(self,
        # Measurement
        max_retries: int = 10,
        retry_delay: float = 2.0,
        # Configuration
        exposure: float = 600.0,
        frame_acq_time: float = 1.5,
        no_dark: bool = False,
        stream_name: str = "scattering",
        # Screening: whether/how a UV-Vis fluorescence quality gate runs
        # during X-ray acquisition.
        #   "unscreened"        -- no UV-Vis hardware touched at all.
        #   "screen_only"       -- gate runs (accept/reject before spending
        #                          X-ray beamtime), but the UV-Vis
        #                          measurement isn't reported as evaluation
        #                          data (PDF-only objectives).
        #   "screen_and_record" -- gate runs and its data is also used for
        #                          evaluation (the "xray-uvvis" case).
        screening: Literal[
            "unscreened",
            "screen_only",
            "screen_and_record"
        ] = "screen_only",
        use_good_bad: bool = True,
        good_target: int = 2,
        max_bad: int = 3,
        num_abs: int = 16,
        num_flu: int = 16,
        # Phase Fitting
        objective_function: str = "pearson",
        # PDF correlation masking window (radial distance, Angstroms) --
        # how much of each phase's G(r) contributes to its correlation
        # score. See analysis.pdf_profile's r_min/r_max.
        min_radius: float = 2.0,
        max_radius: float = 20.0,
        phases: Sequence[Phase] = (),
        # Trained autoencoder-pdf files, required when objective_function="cnn":
        # dataset_pc.npz (the r-grid) and amortized_encoder.pt (the weights).
        cnn_dataset_path: str | Path | None = None,
        cnn_weights_path: str | Path | None = None,
        # cnn only: True steers every phase to its target_fraction (each must
        # set one); False optimizes each raw fraction (none may set one).
        fraction_mode: bool = False,
    ) -> None:

        self.xray_max_retries = max_retries
        self.xray_retry_delay = retry_delay
        self.min_radius = min_radius
        self.max_radius = max_radius

        # Detector acquisition settings
        self.xray_settings = XraySettings(
            exposure=exposure,
            frame_acq_time=frame_acq_time,
            no_dark=no_dark,
            stream_name=stream_name,
        )

        # Screening / fluorescence quality-gating and shot-count policy.
        # Only "unscreened" means no UV-Vis hardware is involved; the other
        # two both need a real QualityPolicy for the acquisition-side gate,
        # they just differ in whether the resulting data is later used for
        # evaluation (a plans.py/evaluation_method concern, not this one).
        self.screening = screening
        self.quality_policy = (
            None
            if screening == "unscreened"
            else QualityPolicy(
                enabled=use_good_bad,
                good_batches=good_target,
                max_bad_batches=max_bad,
                absorbance_shots=num_abs,
                fluorescence_shots=num_flu,
            )
        )

        if objective_function not in _ALL_SCORING_NAMES:
            raise ValueError(f"Invalid objective function: {objective_function}")
        self.objective_function = objective_function
        if objective_function == "cnn" and \
            (cnn_dataset_path is None or cnn_weights_path is None):
            raise ValueError(
                "objective_function='cnn' requires \
                cnn_dataset_path and cnn_weights_path"
            )
        # Stored as str so to_config() stays JSON-serializable.
        self.cnn_dataset_path = None if cnn_dataset_path is None \
            else str(cnn_dataset_path)
        self.cnn_weights_path = None if cnn_weights_path is None \
            else str(cnn_weights_path)

        # Handle phases -- normalized to a list regardless of what sequence
        # type the caller passed in (see set_dofs's comment).
        self.phases = list(phases)

        if fraction_mode and objective_function != "cnn":
            raise ValueError("fraction_mode requires objective_function='cnn'")
        untargeted = [p.name for p in self.phases if p.target_fraction is None]
        targeted = [p.name for p in self.phases if p.target_fraction is not None]
        if fraction_mode and untargeted:
            raise ValueError(
                f"fraction_mode needs a target_fraction on every phase; missing: "
                f"{', '.join(untargeted)}"
            )
        if not fraction_mode and targeted:
            raise ValueError(
                f"target_fraction is only used with fraction_mode=True; set on: "
                f"{', '.join(targeted)}"
            )
        self.fraction_mode = fraction_mode
        # Objectives and constraints are assembled at build time by
        # _assemble_objectives(), which reads self.phases and all role/bound
        # fields set here and in set_uvvis_objectives().

    def set_uvvis_objectives(self,
        # Objective Target
        peak_target: float = 450.0,
        peak_tolerance: float = 5.0,
        max_retries: int = 10,
        retry_delay: float = 2.0,
        # Screening, data selection, and fitting windows
        fit_settings: SpectraFitSettings = SpectraFitSettings(),  # ruff:ignore[function-call-in-default-argument]
        # Calibration Standard Reference
        plqy: PlqyReference = PlqyReference(),  # ruff:ignore[function-call-in-default-argument]
        # Per-metric role: "objective" adds a Pareto dimension; "constraint"
        # turns the metric into a feasibility gate using the bound below.
        # Defaults to "objective" for all three (backward-compatible).
        fwhm_role: Literal["objective", "constraint"] = "objective",
        plqy_role: Literal["objective", "constraint"] = "objective",
        peak_distance_role: Literal["objective", "constraint"] = "objective",
        # Bounds: for objectives these become Pareto reference points (worst
        # value still worth counting in the hypervolume); for constraints they
        # become the hard feasibility limit.  A constraint role requires its
        # bound to be set.
        max_fwhm_nm: float | None = None,
        min_plqy: float | None = None,
        max_peak_distance_nm: float | None = None,
    ) -> None:

        # Setting Objective Targets
        self.peak_target = peak_target
        self.peak_tolerance = peak_tolerance
        for name, value in (("max_fwhm_nm", max_fwhm_nm), ("min_plqy", min_plqy),
                            ("max_peak_distance_nm", max_peak_distance_nm)):
            if value is not None and not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")

        # Validate constraint roles have their bounds set
        if fwhm_role == "constraint" and max_fwhm_nm is None:
            raise ValueError("fwhm_role='constraint' requires max_fwhm_nm to be set")
        if plqy_role == "constraint" and min_plqy is None:
            raise ValueError("plqy_role='constraint' requires min_plqy to be set")
        if peak_distance_role == "constraint" and max_peak_distance_nm is None:
            raise ValueError(
                "peak_distance_role='constraint' \
                requires max_peak_distance_nm to be set"
            )

        self.fwhm_role = fwhm_role
        self.plqy_role = plqy_role
        self.peak_distance_role = peak_distance_role
        self.max_fwhm_nm = max_fwhm_nm
        self.min_plqy = min_plqy
        self.max_peak_distance_nm = max_peak_distance_nm
        self.uvvis_max_retries = max_retries
        self.uvvis_retry_delay = retry_delay

        # Screening, data selection, and fitting windows
        self.fit_settings = fit_settings

        # Calibration Standard Reference
        self.plqy = plqy

    def set_metadata(self, metadata: dict) -> None:
        self.metadata = metadata
        # Place holder for storing metadata as a string
        # We should probably grab UUIDs and version info, as well as run info
        self.metadata_string = ""
        for key, value in metadata.items():
            self.metadata_string += f"{key}: {value}\n"

    def set_success_criteria(
        self,
        *,
        min_correlation: float | None = None,
        max_fwhm: float | None = None,
        min_plqy: float | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        """Configure optional early-stop thresholds for build()'s campaign.

        Runs in parallel as a worker thread watching the output of the main
        queueserver thread. This might be deprecated in favor of the implementation
        Tom and others are working on, since that seems more formal than this process.

        `min_correlation` and the `max_fwhm`/`min_plqy` are mutually exclusive.
        """
        if min_correlation is None and (max_fwhm is None or min_plqy is None):
            raise ValueError(
                "set_success_criteria needs at least one real target: "
                "min_correlation, or both max_fwhm and min_plqy together"
            )
        self.success_criteria = SuccessCriteria(
            min_correlation=min_correlation,
            max_fwhm=max_fwhm,
            min_plqy=min_plqy,
            poll_interval=poll_interval,
        )

    def set_run(
        self,
        *,
        iterations: int = 10,
        n_points: int = 1,
        extra_initialization_trials: int | None = None,
        generation_strategy: Mapping[str, Any] | None = None,
        local: LocalRunSettings | Mapping[str, Any] | None = None,
    ) -> None:
        """
        Set how the campaign runs; saved in to_config()'s "run" section.

        Args:
            - iterations: Optimization iterations to run.
            - n_points: Points per iteration; must be 1 (one suggestion per plan).
            - extra_initialization_trials: If set, initialization_budget = historical
              trials + this; see configure_generation_strategy().
            - generation_strategy: kwargs for ax_client.configure_generation_strategy().
            - local: build_local() settings for simulated runs (LocalRunSettings or
              its fields as a dict).
        """
        if isinstance(local, Mapping):
            local = LocalRunSettings(**local)
        self.run_settings = RunSettings(
            iterations=iterations,
            n_points=n_points,
            extra_initialization_trials=extra_initialization_trials,
            generation_strategy=dict(generation_strategy or {}),
            local=local or LocalRunSettings(),
        )

    def configure_generation_strategy(self, agent: Any) -> int:
        """
        Apply run_settings' generation strategy to a built agent's Ax client.

        Call after build()/build_local() and before the first new trial: every
        trial the client has then is ingested historical data.

        Args:
            - agent: The agent returned by build() or build_local().

        Returns
        -------
            - int - How many historical trials were ingested.
        """
        historical = len(agent.ax_client.summarize())
        strategy = dict(self.run_settings.generation_strategy)
        extra = self.run_settings.extra_initialization_trials
        if extra is not None:
            strategy["initialization_budget"] = historical + extra
        if strategy:
            agent.ax_client.configure_generation_strategy(**strategy)
        return historical

    def experiment(
        self,
        sources: Sequence[FlowSource] | None = None,
        dilutions: Sequence[DilutionStage] | None = None,
        wash_cycles: Sequence[WashCycle] | None = None,
    ) -> None:
        # Normalized to lists regardless of what sequence type the caller
        # passed in -- see set_dofs's comment on why this matters for
        # to_config()/from_config() round-trips.
        self.sources = None if sources is None else list(sources)
        self.dilutions = None if dilutions is None else list(dilutions)
        self.wash_cycles = None if wash_cycles is None else list(wash_cycles)
        self._check_dof_source_alignment()

    def _metric_prefix(self) -> str:
        """Metric prefix for phase scoring: frac_, corr_, or pdf_fit_corr_."""
        if self.objective_function == "cnn":
            return "frac_"
        if self.pdf_mode == "fit":
            return "pdf_fit_corr_"
        return "corr_"

    def _assemble_objectives(self) -> None:
        """Build self.objectives from UV-Vis and phase role configuration.

        Called once at the start of _validate_before_build(), so the list is
        always complete and in a fixed order before any consumer reads it.
        Idempotent: safe to call on repeated build() invocations.

        Order (preserved for backward-compatible historical-data column mapping):
          1. UV-Vis objective-role metrics: log_FWHM, log_PLQY, peak_distance
          2. Phase objective-role metrics (in declaration order)
        """
        objectives: list[Objective] = []

        needs_uvvis = self.evaluation_method in ("uvvis", "xray-uvvis")
        if needs_uvvis:
            uvvis_metrics = [
                ("log_FWHM", True, self.fwhm_role),
                ("log_PLQY", False, self.plqy_role),
                ("peak_distance", True, self.peak_distance_role),
            ]
            for name, minimize, role in uvvis_metrics:
                if role == "objective":
                    objectives.append(Objective(name=name, minimize=minimize))

        prefix = self._metric_prefix()
        for phase in self.phases or ():
            obj = _create_phase(phase, metric_prefix=prefix)
            if obj is not None:
                objectives.append(obj)

        self.objectives = objectives

        n = len(objectives)
        if n > _MANY_OBJECTIVES_WARN:
            warnings.warn(
                f"{n} objectives configured "
                f"({', '.join(o.name for o in objectives)}). "
                f"Multi-objective optimization (qNEHVI) degrades past "
                f"~{_MANY_OBJECTIVES_WARN}: the Pareto front saturates and "
                "per-objective progress stalls. Consider moving secondary "
                "phases or UV-Vis metrics to role='constraint' with a bound "
                "so they gate feasibility instead of adding Pareto dimensions.",
                stacklevel=3,
            )

    def _tiled_client(self) -> Any:
        """Resolve the raw-data Tiled client; `tiled_uri` wins when set."""
        if self.tiled_uri:
            return from_uri(self.tiled_uri)
        return from_profile(self.tiled_profile or DEFAULT_TILED_PROFILE)

    def _sandbox_client(self) -> Any:
        """Resolve the pdfstream sandbox catalog client."""
        return from_uri(self.sandbox_uri)[SANDBOX_CATALOG]

    def _peak_outcome_constraints(self) -> tuple[OutcomeConstraint, ...]:
        """Constrain the fitted PL peak to `peak_target` +/- `peak_tolerance`.

        Only relevant for UV-Vis and xray-UVVis evaluation methods.
        """
        peak = IMetric(name="Peak")
        return (
            OutcomeConstraint(f"p >= {self.peak_target - self.peak_tolerance:g}",
                p=peak),
            OutcomeConstraint(f"p <= {self.peak_target + self.peak_tolerance:g}",
                p=peak),
        )

    def _uvvis_objective_thresholds(self) -> tuple[OutcomeConstraint, ...]:
        """Thresholds and constraints for UV-Vis metrics.

        For objective-role metrics: written as outcome constraints on the
        objective metric, which Ax interprets as the Pareto reference point
        (worst value still counted in the hypervolume).

        For constraint-role metrics: written as a hard feasibility gate.
        The bound is required for constraints (validated in set_uvvis_objectives).
        """
        # (metric_name, op, raw_value, transform, role)
        configs = (
            ("log_FWHM", "<=", self.max_fwhm_nm, np.log, self.fwhm_role),
            ("log_PLQY", ">=", self.min_plqy, np.log, self.plqy_role),
            ("peak_distance", "<=", self.max_peak_distance_nm, float,
                self.peak_distance_role),
        )
        result = []
        for metric, op, value, transform, _role in configs:
            if value is None:
                continue
            result.append(
                OutcomeConstraint(
                    f"m {op} {transform(value):.6g}",
                    m=IMetric(name=metric),
                )
            )
        return tuple(result)

    def _fraction_targets(self) -> dict[str, float]:
        """Target fraction by phase name in fraction_mode, else empty."""
        if not self.fraction_mode:
            return {}
        return {
            phase.name: phase.target_fraction
            for phase in self.phases or ()
            if phase.target_fraction is not None
        }

    def _outcome_constraints(self, needs_plqy: bool) -> tuple[OutcomeConstraint, ...]:
        """Assemble all outcome constraints for the optimizer.

        Covers three sources:
        - UV-Vis: hard Peak band (always), plus per-metric threshold/constraint
          for log_FWHM / log_PLQY / peak_distance based on their roles.
        - Phases: role='constraint' gates, objective_threshold ref points, and
          target_fraction bands (sugar for the frac_dist_ path). Works for all
          scoring functions (cnn frac_, pearson corr_, fit pdf_fit_corr_).
        """
        uvvis: tuple[OutcomeConstraint, ...] = ()
        if needs_plqy:
            uvvis = (
                *self._peak_outcome_constraints(),
                *self._uvvis_objective_thresholds()
            )

        prefix = self._metric_prefix()
        phase_constraints: list[OutcomeConstraint] = []
        for phase in self.phases or ():
            phase_constraints.extend(_phase_constraints(phase, metric_prefix=prefix))

        return (*uvvis, *phase_constraints)

    def _acquisition_plan_name(self) -> str:
        """Select the registered acquisition-plan name for the current config.

        BuildAgent can only choose a plan, not build the
        actual plan callable: `create_xray_plan`/`create_uvvis_plan`/
        `create_xray_uvvis_plan` all need live ophyd device objects that
        exist only in the queue server's worker environment.
        """
        if self.evaluation_method == "uvvis":
            return "uvvis_acquire"
        if self.evaluation_method == "xray-uvvis":
            return "xray_uvvis_acquire"
        # evaluation_method == "xray": the plan depends on self.screening.
        if self.screening == "unscreened":
            return "xray_acquire"
        if self.screening == "screen_only":
            return "xray_screened_acquire"
        return "xray_uvvis_acquire"  # screening == "screen_and_record"

    def _validate_before_build(self) -> tuple[bool, bool]:
        """Shared build()/build_local() preconditions.

        Returns (needs_pdf, needs_plqy).
        """
        self._assemble_objectives()

        if len(self.objectives) == 0:
            raise ValueError(
                "No objectives configured. At least one phase or UV-Vis metric "
                "must have role='objective'."
            )

        if self.dofs is None:
            raise ValueError(
                "set_dofs(...) must be called before build()/build_local()"
            )
        if self.sources is None:
            raise ValueError(
                "experiment(...) must be called before build()/build_local()"
            )
        self._check_dof_source_alignment()

        needs_pdf = self.evaluation_method in ("xray", "xray-uvvis")
        if needs_pdf and not self.phases:
            raise ValueError(
                f"evaluation_method={self.evaluation_method!r} requires phases; "
                "call set_xray_objectives(phases=...) first"
            )
        needs_plqy = self.evaluation_method in ("uvvis", "xray-uvvis")
        if needs_plqy and self.plqy is None:
            raise ValueError(
                f"evaluation_method={self.evaluation_method!r} requires "
                "set_uvvis_objectives(...) to be called before build()/build_local()"
            )
        return needs_pdf, needs_plqy

    def _build_evaluator(
        self,
        needs_pdf: bool,
        *,
        tiled_client: Any | None = None,
        sandbox_client: Any | None = None,
    ) -> Any:
        """Build the evaluation function.

        `tiled_client`/`sandbox_client` override the real
        `_tiled_client()`/`_sandbox_client()` connections when given --
        `build_local()`'s way of substituting
        `legacy.build_fake_tiled_clients(...)` for genuinely offline runs;
        `build()` never passes these, so its behavior is unchanged.
        """
        with tempfile.TemporaryDirectory() as directory:
            evaluator_kwargs: dict[str, Any] = {}
            if needs_pdf:
                evaluator_kwargs["sandbox_client"] = (
                    sandbox_client
                    if sandbox_client is not None
                    else self._sandbox_client()
                )
                evaluator_kwargs["pdf_references"] = _write_pdf_references(
                    self.phases,
                    Path(directory),
                    scoring_function=self.objective_function
                )
                evaluator_kwargs["pdf_mode"] = self.pdf_mode
                evaluator_kwargs["r_min"] = self.min_radius
                evaluator_kwargs["r_max"] = self.max_radius
                evaluator_kwargs["cnn_dataset_path"] = self.cnn_dataset_path
                evaluator_kwargs["cnn_weights_path"] = self.cnn_weights_path
            if self.evaluation_method == "uvvis":
                evaluator_kwargs["tiled_client"] = (
                    tiled_client if tiled_client is not None else self._tiled_client()
                )
                evaluator_kwargs["plqy"] = self.plqy
                evaluator_kwargs["peak_target"] = self.peak_target
                evaluator_kwargs["max_retries"] = self.uvvis_max_retries
                evaluator_kwargs["retry_delay"] = self.uvvis_retry_delay
                evaluator_kwargs["fit_settings"] = self.fit_settings
            elif self.evaluation_method == "xray":
                evaluator_kwargs["max_retries"] = self.xray_max_retries
                evaluator_kwargs["retry_delay"] = self.xray_retry_delay
            elif self.evaluation_method == "xray-uvvis":
                # X-ray and UV-Vis are separate hardware with independent
                # retry policies -- kept as two distinct pairs rather than
                # sharing one, even though the reference implementation this
                # was ported from used a single shared cadence for both.
                evaluator_kwargs["tiled_client"] = (
                    tiled_client if tiled_client is not None else self._tiled_client()
                )
                evaluator_kwargs["plqy"] = self.plqy
                evaluator_kwargs["peak_target"] = self.peak_target
                evaluator_kwargs["uvvis_max_retries"] = self.uvvis_max_retries
                evaluator_kwargs["uvvis_retry_delay"] = self.uvvis_retry_delay
                evaluator_kwargs["xray_max_retries"] = self.xray_max_retries
                evaluator_kwargs["xray_retry_delay"] = self.xray_retry_delay
                evaluator_kwargs["fit_settings"] = self.fit_settings

            return _EVALUATORS[self.evaluation_method](**evaluator_kwargs)

    def _seed_historical_data(self, agent: Any, needs_plqy: bool) -> None:
        if self.agent_data_path is None:
            return
        historical = _load_historical_data(
            self.agent_data_path,
            [dof.name for dof in self.dofs],
            [objective.name for objective in self.objectives],
            optional_names=(
                *(("Peak",) if needs_plqy else ()),
                *(f"frac_{name}" for name in self._fraction_targets()),
            ),
            peak_target=self.peak_target,
            fraction_targets=self._fraction_targets(),
        )
        if historical:
            agent.ingest(historical)

    def _is_moo_with_constraints(self, constraints: Sequence[Any]) -> bool:
        """Return true for a multi-objective campaign with outcome constraints."""
        return len(self.objectives) > 1 and bool(constraints)

    def _build_time_feasibility_check(
        self, agent: Any, constraints: Sequence[Any]
    ) -> None:
        """Raise ValueError if seeded historical trials are all infeasible.

        Only fires for multi-objective campaigns with outcome constraints and
        at least one completed trial, so a fresh campaign (no history) is
        always allowed through.
        """
        if not self._is_moo_with_constraints(constraints):
            return
        try:
            df = agent.ax_client.summarize()
        except Exception:
            return
        if df is None or len(df) == 0:
            return
        report = _check_feasibility(df, constraints)
        if report["feasible_count"] == 0 and report["total_count"] > 0:
            raise ValueError(_infeasibility_message(report))

    def _install_feasibility_guard(
        self, agent: Any, constraints: Sequence[Any]
    ) -> None:
        """Wrap agent._optimizer.suggest to translate MBM infeasibility crashes.

        When MBM engages with zero feasible observations it raises one of two
        opaque errors (NaN→int or probability-tensor).  This wrapper catches
        exactly those signatures and re-raises as a clear ValueError naming
        which constraints are unsatisfied and by how much.

        Only installed for multi-objective campaigns with outcome constraints.
        """
        if not self._is_moo_with_constraints(constraints):
            return

        original_suggest = agent._optimizer.suggest

        def _guarded_suggest(num_points: int | None = None) -> Any:
            try:
                return original_suggest(num_points)
            except (ValueError, RuntimeError) as exc:
                msg = str(exc)
                if any(sig in msg for sig in _INFEASIBILITY_SIGNATURES):
                    try:
                        df = agent.ax_client.summarize()
                        report = _check_feasibility(df, constraints)
                        if report["feasible_count"] == 0 and report["total_count"] > 0:
                            raise ValueError(_infeasibility_message(report)) from None
                    except ValueError:
                        raise
                    except Exception:
                        pass
                raise

        agent._optimizer.suggest = _guarded_suggest

    def build(self, ) -> None:
        """Build the agent for use in a queueserver environment.

        This method should only be called when `queue_server=True`,
        and needs the local environment of the beamline to function
        properly.
        """
        if not self.queue_server:
            raise NotImplementedError(
                "queue_server=False -- call build_local(...) instead of build()"
            )
        needs_pdf, needs_plqy = self._validate_before_build()
        acquisition_plan = self._acquisition_plan_name()
        _evaluator = self._build_evaluator(needs_pdf)

        re_manager_api = REManagerAPI(http_server_uri=self.http_server_uri)
        if self.http_api_key:
            re_manager_api.set_authorization_key(api_key=self.http_api_key)
        document_dispatcher = RemoteDispatcher(self.zmq_consumer_address)

        constraints = self._outcome_constraints(needs_plqy)
        agent = QueueserverAgent(
            re_manager_api,
            document_dispatcher,
            sensors=(),
            dofs=self.dofs,
            objectives=self.objectives,
            evaluation_function=_evaluator,
            acquisition_plan=acquisition_plan,
            outcome_constraints=constraints,
            checkpoint_path=(
                None if self.checkpoint_path is None else str(self.checkpoint_path)
            ),
        )
        self._seed_historical_data(agent, needs_plqy)
        self._build_time_feasibility_check(agent, constraints)
        self._install_feasibility_guard(agent, constraints)
        return agent

    def _build_plan_context(
        self,
        devices: Mapping[str, Any],
        wrap_xray_run: Callable[[Any, bool], Any],
        mixer_lengths_cm: tuple[float, ...],
        residence_time_ratio: float,
    ) -> XrayPlanContext | UvvisPlanContext | XrayUvvisPlanContext:
        """Bind sources/dilutions/wash_cycles' pump names to real devices.

        Builds the plan context matching `_acquisition_plan_name()`'s
        dispatch -- the same context shape the Queue Server's worker builds
        today (see `helpers.beamline`), just constructed here instead of in
        a separate worker process.
        """

        def _bind_pump(item: Any) -> Any:
            return replace(item, pump=devices[item.pump])

        common = {
            "sources": tuple(_bind_pump(source) for source in self.sources),
            "dilutions": tuple(_bind_pump(stage) for stage in self.dilutions or ()),
            "wash_cycles": tuple(_bind_pump(cycle) for cycle in self.wash_cycles or ()),
            "quality": self.quality_policy or QualityPolicy(enabled=False),
            "mixer_lengths_cm": mixer_lengths_cm,
            "residence_time_ratio": residence_time_ratio,
        }
        fit_settings = self.fit_settings or SpectraFitSettings()

        plan_name = self._acquisition_plan_name()
        if plan_name == "uvvis_acquire":
            return UvvisPlanContext(
                qepro=devices["qepro"],
                led=devices["led"],
                uv_shutter=devices["uv_shutter"],
                fast_shutter=devices["fast_shutter"],
                fit_settings=fit_settings,
                **common,
            )
        if plan_name == "xray_acquire":
            return XrayPlanContext(
                led=devices["led"],
                fast_shutter=devices["fast_shutter"],
                xray_detector=devices["xray_detector"],
                wrap_xray_run=wrap_xray_run,
                xray=self.xray_settings,
                **common,
            )
        # "xray_screened_acquire" or "xray_uvvis_acquire" -- both need the
        # full XrayUvvisPlanContext; they differ only in whether the plan
        # itself reports the UV-Vis measurement as evaluation data.
        return XrayUvvisPlanContext(
            qepro=devices["qepro"],
            led=devices["led"],
            uv_shutter=devices["uv_shutter"],
            fast_shutter=devices["fast_shutter"],
            xray_detector=devices["xray_detector"],
            wrap_xray_run=wrap_xray_run,
            xray=self.xray_settings,
            fit_settings=fit_settings,
            **common,
        )

    def build_local(
        self,
        devices: Mapping[str, Any],
        wrap_xray_run: Callable[[Any, bool], Any],
        *,
        # Same meaning/defaults as helpers.beamline's *PlanContext dataclass
        # fields -- on the queue-server path these are set directly on the
        # context by the worker's own startup script (never seen by
        # xpd_tools at all); build_local() has to build the context itself,
        # so it needs its own way to override them for real tubing, or (as
        # in tests) to make simulated runs fast.
        mixer_lengths_cm: tuple[float, ...] = (30.0,),
        residence_time_ratio: float = 1.0,
        # Real Tiled/sandbox connections by default -- same as build().
        # Override with legacy.build_fake_tiled_clients(...) to run fully
        # offline: there's no real pdfstream service to reduce a
        # simulated detector's data anyway, so a real sandbox_client can
        # never produce real results for a simulated campaign.
        tiled_client: Any | None = None,
        sandbox_client: Any | None = None,
    ) -> Agent:
        """No-queue-server build path.

        Drives a local Bluesky RunEngine directly against `devices` (real
        hardware or simulated), instead of dispatching acquisition plans by
        name through a Queue Server.

        Returns a `blop.ax.agent.Agent`.

        Can be used both as a fake atmosphere to test building of the agent
        and loading historical data, and as a local environment to run simulation
        driven bayesian optimization of the chemical space.
        """
        if self.queue_server:
            raise ValueError(
                "queue_server=True -- call build() instead of build_local()"
            )
        needs_pdf, needs_plqy = self._validate_before_build()
        _evaluator = self._build_evaluator(
            needs_pdf, tiled_client=tiled_client, sandbox_client=sandbox_client
        )

        context = self._build_plan_context(
            devices, wrap_xray_run, mixer_lengths_cm, residence_time_ratio
        )
        acquisition_plan = _LOCAL_PLAN_FACTORIES[self._acquisition_plan_name()](context)

        constraints = self._outcome_constraints(needs_plqy)
        agent = Agent(
            sensors=(),
            dofs=self.dofs,
            objectives=self.objectives,
            evaluation_function=_evaluator,
            acquisition_plan=acquisition_plan,
            outcome_constraints=constraints,
            checkpoint_path=(
                None if self.checkpoint_path is None else str(self.checkpoint_path)
            ),
        )
        self._seed_historical_data(agent, needs_plqy)
        self._build_time_feasibility_check(agent, constraints)
        self._install_feasibility_guard(agent, constraints)
        return agent

    def register_acquisition_plans(
        self,
        devices: Mapping[str, Any],
        *,
        namespace: dict[str, Any] | None = None,
        wrap_xray_run: Callable[[Any, bool], Any] | None = None,
        mixer_lengths_cm: tuple[float, ...] = (30.0,),
        residence_time_ratio: float = 1.0,
    ) -> dict[str, Callable]:
        """Build bound acquisition plan(s) and optionally inject them into a namespace.

        This is the Queue Server worker twin of ``build_local()``.  Call it from
        a worker startup module to register the plan(s) that the agent will
        dispatch by name::

            # In xpd_tools/optimization/profiles/halide.py (loaded by
            # `start-re-manager --startup-module ...`)
            build_agent = BuildAgent.from_config(json.load(open(config_path)))
            devices = build_halide_devices()
            build_agent.register_acquisition_plans(devices, namespace=globals())
            # → ``uvvis_acquire`` (and/or xray variants) are now in globals(),
            #   and qserver discovers them automatically.

        Parameters
        ----------
        devices:
            Dict mapping pump-id strings and device-role strings to real ophyd
            objects, as returned by `build_halide_devices()`.
        namespace:
            If given, each `{plan_name: callable}` pair is injected here.
            Pass `globals()` from the startup module to expose the plans to
            the Queue Server process.
        wrap_xray_run:
            Required only when the selected `evaluation_method` is `xray`,
            `xray-uvvis`, or `xray-screened`.  Ignored for UV-Vis-only runs.
        mixer_lengths_cm:
            Tube lengths (cm) between injection tees.  Passed to `_build_plan_context`.
        residence_time_ratio:
            Fraction of computed residence time to wait.

        Returns
        -------
        dict[str, Callable]
            `{plan_name: bound_plan}` -- e.g. `{"uvvis_acquire": <callable>}`.
        """
        self._validate_before_build()

        plan_name = self._acquisition_plan_name()

        # X-ray plans require wrap_xray_run; UV-Vis does not.
        _xray_plans = {"xray_acquire", "xray_screened_acquire", "xray_uvvis_acquire"}
        if plan_name in _xray_plans and wrap_xray_run is None:
            raise ValueError(
                f"evaluation_method={self.evaluation_method!r} requires wrap_xray_run "
                "but none was supplied to register_acquisition_plans()."
            )

        context = self._build_plan_context(
            devices,
            wrap_xray_run,  # type: ignore[arg-type]
            mixer_lengths_cm,
            residence_time_ratio,
        )
        bound_plan = _LOCAL_PLAN_FACTORIES[plan_name](context)
        plans = {plan_name: bound_plan}

        if namespace is not None:
            namespace.update(plans)

        return plans

    def to_config(self, filename: str | None = None) -> dict[str, Any] | None:
        """Compile the current configuration into a JSON-able dict.

        TODO: Expand to include local meta-data on the beamline. We should be
        able to reproduce the beamline state (devices, configuration) to
        recreate the agent if experimental data is weird.
        """
        _json = {
            "connection": {
                "queue_server": self.queue_server,
                "http_server_uri": self.http_server_uri,
                "zmq_consumer_address": self.zmq_consumer_address,
                "tiled_profile": self.tiled_profile,
                "tiled_uri": self.tiled_uri,
                "sandbox_uri": self.sandbox_uri,
            },
            "evaluation_method": self.evaluation_method,
            "pdf_mode": self.pdf_mode,
            "agent_data_path": self.agent_data_path,
            "checkpoint_path": (
                None if self.checkpoint_path is None else str(self.checkpoint_path)
            ),
            "metadata": self.metadata,
            "success_criteria": (
                None if self.success_criteria is None else asdict(self.success_criteria)
            ),
            "pumps": (
                None if self.pumps is None else [asdict(pump) for pump in self.pumps]
            ),
            "experiment": {
                "sources": (
                    None
                    if self.sources is None
                    else [asdict(source) for source in self.sources]
                ),
                "dilutions": (
                    None
                    if self.dilutions is None
                    else [asdict(stage) for stage in self.dilutions]
                ),
                "wash_cycles": (
                    None
                    if self.wash_cycles is None
                    else [asdict(cycle) for cycle in self.wash_cycles]
                ),
            },
            "xray": {
                "max_retries": self.xray_max_retries,
                "retry_delay": self.xray_retry_delay,
                "settings": (
                    None if self.xray_settings is None else asdict(self.xray_settings)
                ),
                "screening": self.screening,
                "quality_policy": (
                    None
                    if self.quality_policy is None
                    else asdict(self.quality_policy)
                ),
                "objective_function": self.objective_function,
                "min_radius": self.min_radius,
                "max_radius": self.max_radius,
                "cnn_dataset_path": self.cnn_dataset_path,
                "cnn_weights_path": self.cnn_weights_path,
                "fraction_mode": self.fraction_mode,
                "phases": (
                    None
                    if self.phases is None
                    else [asdict(phase) for phase in self.phases]
                ),
            },
            "uvvis": {
                "peak_target": self.peak_target,
                "peak_tolerance": self.peak_tolerance,
                "fwhm_role": self.fwhm_role,
                "plqy_role": self.plqy_role,
                "peak_distance_role": self.peak_distance_role,
                "max_fwhm_nm": self.max_fwhm_nm,
                "min_plqy": self.min_plqy,
                "max_peak_distance_nm": self.max_peak_distance_nm,
                "max_retries": self.uvvis_max_retries,
                "retry_delay": self.uvvis_retry_delay,
                "fit_settings": (
                    None if self.fit_settings is None else asdict(self.fit_settings)
                ),
                "plqy": None if self.plqy is None else asdict(self.plqy),
            },
            "run": asdict(self.run_settings),
        }
        if filename is None:
            return _json

        else:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(_json, f, ensure_ascii=False, indent=4)

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], *, http_api_key: str | None = None
    ) -> "BuildAgent":
        """Rebuild a `BuildAgent` from a `to_config()`-shaped dict.

        Replays the same setter calls a notebook would make, reconstructing
        each group's real objects (`Pump`, `FlowSource`, `Phase`, ...) from
        its own dict -- a group is only replayed if the corresponding
        setter was actually called when the config was produced (detected
        by its always-set field being non-`None`), so an agent that never
        configured e.g. UV-Vis round-trips back to the same unconfigured
        state, not a spuriously-defaulted one.

        `http_api_key` is never read from `config` (see `to_config`) --
        pass it here directly (e.g. from an environment variable) if the
        rebuilt agent needs one.
        """
        connection = config["connection"]
        agent = cls(
            queue_server=connection["queue_server"],
            http_server_uri=connection["http_server_uri"],
            http_api_key=http_api_key,
            zmq_consumer_address=connection["zmq_consumer_address"],
            tiled_profile=connection["tiled_profile"],
            tiled_uri=connection["tiled_uri"],
            sandbox_uri=connection["sandbox_uri"],
            pdf_mode=config["pdf_mode"],
            evaluation_method=config["evaluation_method"],
            agent_data_path=config["agent_data_path"],
            checkpoint_path=config["checkpoint_path"],
        )

        if config["metadata"] is not None:
            agent.set_metadata(config["metadata"])

        if config["success_criteria"] is not None:
            agent.set_success_criteria(**config["success_criteria"])

        if config["pumps"] is not None:
            agent.set_dofs(
                [
                    Pump(
                        name=pump["name"],
                        id=pump["id"],
                        bounds=pump["bounds"],
                        parameter_type=pump["parameter_type"],
                    )
                    for pump in config["pumps"]
                ]
            )

        experiment = config["experiment"]
        agent.experiment(
            sources=(
                None
                if experiment["sources"] is None
                else [FlowSource(**source) for source in experiment["sources"]]
            ),
            dilutions=(
                None
                if experiment["dilutions"] is None
                else [DilutionStage(**stage) for stage in experiment["dilutions"]]
            ),
            wash_cycles=(
                None
                if experiment["wash_cycles"] is None
                else [WashCycle(**cycle) for cycle in experiment["wash_cycles"]]
            ),
        )

        xray = config["xray"]
        if xray["settings"] is not None:
            quality = xray["quality_policy"] or {}
            agent.set_xray_objectives(
                max_retries=xray["max_retries"],
                retry_delay=xray["retry_delay"],
                exposure=xray["settings"]["exposure"],
                frame_acq_time=xray["settings"]["frame_acq_time"],
                no_dark=xray["settings"]["no_dark"],
                stream_name=xray["settings"]["stream_name"],
                screening=xray["screening"],
                use_good_bad=quality.get("enabled", True),
                good_target=quality.get("good_batches", 2),
                max_bad=quality.get("max_bad_batches", 3),
                num_abs=quality.get("absorbance_shots", 16),
                num_flu=quality.get("fluorescence_shots", 16),
                objective_function=xray["objective_function"],
                min_radius=xray["min_radius"],
                max_radius=xray["max_radius"],
                # .get: configs saved before cnn support lack these keys.
                cnn_dataset_path=xray.get("cnn_dataset_path"),
                cnn_weights_path=xray.get("cnn_weights_path"),
                fraction_mode=xray.get("fraction_mode", False),
                phases=(
                    []
                    if xray["phases"] is None
                    # "simulated" was an unused per-phase flag; older configs have it.
                    # "role"/"bound"/"objective_threshold" are absent in older configs;
                    # Phase defaults (role="objective", bound=None) restore the old
                    # all-objectives behaviour.
                    else [
                        Phase(**{k: v for k, v in phase.items() if k != "simulated"})
                        for phase in xray["phases"]
                    ]
                ),
            )

        uvvis = config["uvvis"]
        if uvvis["fit_settings"] is not None or uvvis["plqy"] is not None:
            agent.set_uvvis_objectives(
                peak_target=uvvis["peak_target"],
                peak_tolerance=uvvis["peak_tolerance"],
                # Older configs predate roles and objective thresholds.
                fwhm_role=uvvis.get("fwhm_role", "objective"),
                plqy_role=uvvis.get("plqy_role", "objective"),
                peak_distance_role=uvvis.get("peak_distance_role", "objective"),
                max_fwhm_nm=uvvis.get("max_fwhm_nm"),
                min_plqy=uvvis.get("min_plqy"),
                max_peak_distance_nm=uvvis.get("max_peak_distance_nm"),
                max_retries=uvvis["max_retries"],
                retry_delay=uvvis["retry_delay"],
                fit_settings=(
                    SpectraFitSettings()
                    if uvvis["fit_settings"] is None
                    # JSON has no tuple type, so the range fields come back
                    # as lists -- SpectraFitSettings.__post_init__ restores
                    # them to tuples.
                    else SpectraFitSettings(**uvvis["fit_settings"])
                ),
                plqy=(
                    PlqyReference()
                    if uvvis["plqy"] is None
                    else PlqyReference(**uvvis["plqy"])
                ),
            )

        # Configs saved before "run" existed keep the defaults.
        if config.get("run") is not None:
            agent.set_run(**config["run"])

        return agent
