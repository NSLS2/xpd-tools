"""Tests for the Queue Server worker plans that take the config with each step."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest

from xpd_tools.optimization.agent import BuildAgent
from xpd_tools.optimization.helpers.dofs import Pump
from xpd_tools.optimization.helpers.qepro import PlqyReference
from xpd_tools.optimization.plans import FlowSource
from xpd_tools.optimization.worker import WORKER_PLAN_NAMES, register_worker_plans

_DEVICES = {
    name: object()
    for name in ("dds-CsPb", "dds-Br", "qepro", "led", "uv_shutter", "fast_shutter")
}


def _config(evaluation_method: str = "uvvis", pump: str = "dds-CsPb") -> dict[str, Any]:
    agent = BuildAgent(
        evaluation_method=evaluation_method, http_server_uri="https://example.invalid"
    )
    agent.set_dofs([Pump(name="CsPb", id=pump, bounds=(10, 200))])
    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump=pump,
                precursor="CsPbOA",
                sample_label="CsPb",
            ),
        ]
    )
    agent.set_uvvis_objectives(plqy=PlqyReference())
    return agent.to_config()


def _fake_bound_plan(calls: list[tuple]):
    def plan(suggestions, actuators, sensors=None, md=None):
        calls.append((suggestions, actuators, sensors, md))
        yield "msg"
        return "uid"

    return plan


def _run(gen) -> tuple[list, Any]:
    messages = []
    try:
        while True:
            messages.append(next(gen))
    except StopIteration as stop:
        return messages, stop.value


def test_registers_all_plans_into_namespace() -> None:
    namespace: dict[str, Any] = {}
    plans = register_worker_plans(namespace, devices=_DEVICES)
    assert set(plans) == set(WORKER_PLAN_NAMES)
    for name in WORKER_PLAN_NAMES:
        assert namespace[name] is plans[name]
        assert namespace[name].__name__ == name
        assert list(inspect.signature(plans[name]).parameters) == [
            "suggestions",
            "actuators",
            "sensors",
            "md",
            "config",
        ]


def test_missing_config_names_the_fix() -> None:
    plan = register_worker_plans(devices=_DEVICES)["uvvis_acquire"]
    with pytest.raises(ValueError, match=r"build\(send_config=True\)"):
        next(plan([{}], []))


def test_config_selecting_another_plan_is_refused() -> None:
    plan = register_worker_plans(devices=_DEVICES)["xray_acquire"]
    with pytest.raises(ValueError, match="selects uvvis_acquire"):
        next(plan([{}], [], config=_config()))


def test_xray_plan_without_detector_is_refused() -> None:
    plan = register_worker_plans(devices=_DEVICES)["xray_uvvis_acquire"]
    config = _config()
    with (
        patch.object(
            BuildAgent, "_acquisition_plan_name", return_value="xray_uvvis_acquire"
        ),
        pytest.raises(ValueError, match="xray_detector"),
    ):
        next(plan([{}], [], config=config))


def test_binding_is_cached_per_config() -> None:
    calls: list[tuple] = []
    with patch.object(
        BuildAgent,
        "register_acquisition_plans",
        autospec=True,
        side_effect=lambda self, devices, **kw: {
            "uvvis_acquire": _fake_bound_plan(calls)
        },
    ) as register:
        plan = register_worker_plans(devices=_DEVICES)["uvvis_acquire"]
        first = _config()
        messages, uid = _run(plan([{"a": 1}], ["act"], None, {"k": 1}, config=first))
        _run(plan([{"a": 2}], ["act"], config=dict(first)))
        assert register.call_count == 1
        _run(plan([{"a": 3}], ["act"], config=_config(pump="dds-Br")))
        assert register.call_count == 2

    assert messages == ["msg"]
    assert uid == "uid"
    assert calls[0] == ([{"a": 1}], ["act"], None, {"k": 1})


def test_xray_detector_and_wrapper_reach_the_binding() -> None:
    detector = object()

    def wrap(plan, no_dark):
        return plan

    with (
        patch.object(BuildAgent, "_acquisition_plan_name", return_value="xray_acquire"),
        patch.object(
            BuildAgent,
            "register_acquisition_plans",
            autospec=True,
            return_value={"xray_acquire": _fake_bound_plan([])},
        ) as register,
    ):
        plan = register_worker_plans(
            devices=_DEVICES, xray_detector=detector, wrap_xray_run=wrap
        )["xray_acquire"]
        _run(plan([{}], [], config=_config()))

    _, devices = register.call_args.args
    assert devices["xray_detector"] is detector
    assert register.call_args.kwargs["wrap_xray_run"] is wrap


@pytest.mark.parametrize(
    ("send_config", "expected_keys"),
    [(True, {"config"}), (False, {"flow_config", "wash_config", "quality_config"})],
)
def test_build_sends_config_only_when_asked(
    send_config: bool, expected_keys: set[str]
) -> None:
    agent = BuildAgent(
        evaluation_method="uvvis", http_server_uri="https://example.invalid"
    )
    agent.set_dofs([Pump(name="CsPb", id="dds-CsPb", bounds=(10, 200))])
    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds-CsPb",
                precursor="CsPbOA",
                sample_label="CsPb",
            ),
        ]
    )
    agent.set_uvvis_objectives(plqy=PlqyReference())
    targets = (
        "xpd_tools.optimization.agent.from_uri",
        "xpd_tools.optimization.agent.from_profile",
        "xpd_tools.optimization.agent.REManagerAPI",
        "xpd_tools.optimization.agent.RemoteDispatcher",
    )
    patchers = [patch(target) for target in targets]
    for patcher in patchers:
        patcher.start()
    try:
        with patch("xpd_tools.optimization.agent.QueueserverAgent") as qs_agent:
            agent.build(send_config=send_config)
    finally:
        for patcher in patchers:
            patcher.stop()
    kwargs = qs_agent.call_args.kwargs["acquisition_plan_kwargs"]
    assert set(kwargs) == expected_keys
    if send_config:
        assert kwargs["config"] == agent.to_config()
