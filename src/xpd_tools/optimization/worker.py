"""Queue Server worker plans that take the campaign config with each step.

A profile collection registers these from one short startup file::

    from xpd_tools.optimization.worker import register_worker_plans

    register_worker_plans(
        globals(),
        xray_detector=pe1c,
        wrap_xray_run=lambda plan, no_dark: plan if no_dark else periodic_dark_02(plan),
    )

The host side then builds its agent with ``build_agent.build(send_config=True)``,
so every queued step carries ``config=build_agent.to_config()``.  Each worker
plan reads the plan settings out of that config, binds them to the worker's
devices and runs the matching xpd-tools plan.

The worker runs plans only: the optimizer (blop/bluesky-adaptive, Ax, torch)
stays with the agent on the workstation, so nothing here imports
`xpd_tools.optimization.agent`.  The bound plan is cached per
``(plan name, config)``, so cross-step state (infused volumes, quality signals)
carries across the steps of one campaign and starts fresh when the config
changes (e.g. a syringe swap).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import replace
from typing import Any

from xpd_tools.optimization.plans import (
    DilutionStage,
    FlowSource,
    QualityPolicy,
    SpectraFitSettings,
    UvvisPlanContext,
    WashCycle,
    XrayPlanContext,
    XraySettings,
    XrayUvvisPlanContext,
    create_uvvis_plan,
    create_xray_plan,
    create_xray_screened_plan,
    create_xray_uvvis_plan,
)

logger = logging.getLogger(__name__)

WORKER_PLAN_NAMES = (
    "uvvis_acquire",
    "xray_acquire",
    "xray_screened_acquire",
    "xray_uvvis_acquire",
)
_XRAY_PLAN_NAMES = frozenset(WORKER_PLAN_NAMES) - {"uvvis_acquire"}
_PLAN_FACTORIES: dict[str, Callable[[Any], Callable[..., Any]]] = {
    "uvvis_acquire": create_uvvis_plan,
    "xray_acquire": create_xray_plan,
    "xray_screened_acquire": create_xray_screened_plan,
    "xray_uvvis_acquire": create_xray_uvvis_plan,
}

# BuildAgent's defaults for configs that predate these keys.
_DEFAULT_MIXER_LENGTHS_CM = (30.0,)
_DEFAULT_RESIDENCE_TIME_RATIO = 1.0
_DEFAULT_TUBING_ID_MM = 1.016


def config_fingerprint(config: Mapping[str, Any]) -> str:
    """Stable hash of a `BuildAgent.to_config()` dict."""
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode()
    ).hexdigest()


def plan_name_for_config(config: Mapping[str, Any]) -> str:
    """Return the worker plan a `BuildAgent.to_config()` dict selects."""
    method = config["evaluation_method"]
    if method == "uvvis":
        return "uvvis_acquire"
    if method == "xray-uvvis":
        return "xray_uvvis_acquire"
    screening = config["xray"]["screening"]
    if screening == "unscreened":
        return "xray_acquire"
    if screening == "screen_only":
        return "xray_screened_acquire"
    return "xray_uvvis_acquire"


def plan_context_from_config(
    plan_name: str,
    config: Mapping[str, Any],
    devices: Mapping[str, Any],
    wrap_xray_run: Callable[[Any, bool], Any] | None,
) -> UvvisPlanContext | XrayPlanContext | XrayUvvisPlanContext:
    """Build the context `BuildAgent._build_plan_context` would, from its config."""
    experiment = config["experiment"]
    xray = config["xray"]
    uvvis = config["uvvis"]

    def bind(item: Any) -> Any:
        return replace(item, pump=devices[item.pump])

    quality = uvvis.get("quality_policy") or xray.get("quality_policy")
    mixer = experiment.get("mixer_lengths_cm")
    residence = experiment.get("residence_time_ratio")
    tubing = experiment.get("tubing_id_mm")
    common = {
        "sources": tuple(bind(FlowSource(**s)) for s in experiment["sources"] or ()),
        "dilutions": tuple(
            bind(DilutionStage(**d)) for d in experiment["dilutions"] or ()
        ),
        "wash_cycles": tuple(
            bind(WashCycle(**w)) for w in experiment["wash_cycles"] or ()
        ),
        "quality": QualityPolicy(**quality)
        if quality
        else QualityPolicy(enabled=False),
        "mixer_lengths_cm": (
            tuple(mixer) if mixer is not None else _DEFAULT_MIXER_LENGTHS_CM
        ),
        "residence_time_ratio": (
            residence if residence is not None else _DEFAULT_RESIDENCE_TIME_RATIO
        ),
        "tubing_id_mm": tubing if tubing is not None else _DEFAULT_TUBING_ID_MM,
    }
    fit_settings = (
        SpectraFitSettings(**uvvis["fit_settings"])
        if uvvis.get("fit_settings")
        else SpectraFitSettings()
    )
    if plan_name == "uvvis_acquire":
        return UvvisPlanContext(
            qepro=devices["qepro"],
            led=devices["led"],
            uv_shutter=devices["uv_shutter"],
            fast_shutter=devices["fast_shutter"],
            fit_settings=fit_settings,
            **common,
        )
    if not xray.get("settings"):
        raise ValueError(f"{plan_name} needs a config with X-ray settings.")
    if wrap_xray_run is None:
        raise ValueError(f"{plan_name} needs wrap_xray_run (dark frames).")
    settings = XraySettings(**xray["settings"])
    if plan_name == "xray_acquire":
        return XrayPlanContext(
            led=devices["led"],
            fast_shutter=devices["fast_shutter"],
            xray_detector=devices["xray_detector"],
            wrap_xray_run=wrap_xray_run,
            xray=settings,
            **common,
        )
    return XrayUvvisPlanContext(
        qepro=devices["qepro"],
        led=devices["led"],
        uv_shutter=devices["uv_shutter"],
        fast_shutter=devices["fast_shutter"],
        xray_detector=devices["xray_detector"],
        wrap_xray_run=wrap_xray_run,
        xray=settings,
        fit_settings=fit_settings,
        **common,
    )


def register_worker_plans(
    namespace: MutableMapping[str, Any] | None = None,
    *,
    devices: Mapping[str, Any] | None = None,
    xray_detector: Any = None,
    wrap_xray_run: Callable[[Any, bool], Any] | None = None,
    mock: bool = False,
) -> dict[str, Callable[..., Any]]:
    """Build the four worker plans and optionally inject them into a namespace.

    Parameters
    ----------
    namespace:
        Pass ``globals()`` from a startup file so the Queue Server lists the plans.
    devices:
        Device mapping as returned by `build_halide_devices`; built here when omitted.
    xray_detector:
        Area detector for the X-ray plans (``pe1c`` in the XPD profile).
    wrap_xray_run:
        ``(plan, no_dark) -> plan`` wrapper adding dark frames to X-ray runs.
    mock:
        Build simulated devices when ``devices`` is omitted.

    Returns
    -------
    dict[str, Callable]
        ``{plan_name: worker_plan}`` for every name in `WORKER_PLAN_NAMES`.
    """
    if devices is None:
        from xpd_tools.optimization.devices import build_halide_devices

        devices = build_halide_devices(mock=mock)
    devices = dict(devices)
    if xray_detector is not None:
        devices["xray_detector"] = xray_detector

    cache: dict[tuple[str, str], Callable[..., Any]] = {}

    def bound_plan(plan_name: str, config: Mapping[str, Any]) -> Callable[..., Any]:
        key = (plan_name, config_fingerprint(config))
        if key not in cache:
            expected = plan_name_for_config(config)
            if expected != plan_name:
                raise ValueError(
                    f"{plan_name} was queued, but the config it carries selects "
                    f"{expected} (evaluation_method={config['evaluation_method']!r})."
                )
            if plan_name in _XRAY_PLAN_NAMES:
                if "xray_detector" not in devices:
                    raise ValueError(
                        f"{plan_name} needs an X-ray detector: pass xray_detector= "
                        "to register_worker_plans()."
                    )
                if wrap_xray_run is None:
                    raise ValueError(
                        f"{plan_name} needs wrap_xray_run= (dark frames) "
                        "passed to register_worker_plans()."
                    )
            context = plan_context_from_config(
                plan_name, config, devices, wrap_xray_run
            )
            logger.info(
                "Binding %s for a new campaign config (sources=%s)",
                plan_name,
                [source["pump"] for source in config["experiment"]["sources"] or ()],
            )
            cache[key] = _PLAN_FACTORIES[plan_name](context)
        return cache[key]

    def make_plan(plan_name: str) -> Callable[..., Any]:
        def worker_plan(suggestions, actuators, sensors=None, md=None, config=None):
            if config is None:
                raise ValueError(
                    f"{plan_name} needs config=build_agent.to_config(); "
                    "build the agent with build_agent.build(send_config=True)."
                )
            return (
                yield from bound_plan(plan_name, config)(
                    suggestions, actuators, sensors, md
                )
            )

        worker_plan.__name__ = worker_plan.__qualname__ = plan_name
        worker_plan.__doc__ = (
            f"xpd-tools {plan_name}; the campaign settings arrive as ``config``."
        )
        return worker_plan

    plans = {name: make_plan(name) for name in WORKER_PLAN_NAMES}
    if namespace is not None:
        namespace.update(plans)
    logger.info("Registered xpd-tools worker plans: %s", ", ".join(plans))
    return plans
