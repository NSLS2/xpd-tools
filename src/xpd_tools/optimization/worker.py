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
plan rebuilds a `BuildAgent` from that config, binds it to the worker's devices
and runs the matching xpd-tools plan.  The bound plan is cached per
``(plan name, config)``, so cross-step state (infused volumes, quality signals)
carries across the steps of one campaign and starts fresh when the config
changes (e.g. a syringe swap).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

WORKER_PLAN_NAMES = (
    "uvvis_acquire",
    "xray_acquire",
    "xray_screened_acquire",
    "xray_uvvis_acquire",
)
_XRAY_PLAN_NAMES = frozenset(WORKER_PLAN_NAMES) - {"uvvis_acquire"}


def config_fingerprint(config: Mapping[str, Any]) -> str:
    """Stable hash of a `BuildAgent.to_config()` dict."""
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode()
    ).hexdigest()


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
            from xpd_tools.optimization.agent import BuildAgent

            agent = BuildAgent.from_config(config)
            expected = agent._acquisition_plan_name()  # noqa: SLF001
            if expected != plan_name:
                raise ValueError(
                    f"{plan_name} was queued, but the config it carries selects "
                    f"{expected} (evaluation_method={agent.evaluation_method!r})."
                )
            if plan_name in _XRAY_PLAN_NAMES and "xray_detector" not in devices:
                raise ValueError(
                    f"{plan_name} needs an X-ray detector: pass xray_detector= "
                    "to register_worker_plans()."
                )
            wrap = wrap_xray_run if plan_name in _XRAY_PLAN_NAMES else None
            logger.info(
                "Binding %s for a new campaign config (sources=%s)",
                plan_name,
                [source.pump for source in agent.sources or ()],
            )
            cache[key] = agent.register_acquisition_plans(devices, wrap_xray_run=wrap)[
                plan_name
            ]
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
