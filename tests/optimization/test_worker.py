"""Tests for the Queue Server worker plans that take the config with each step."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import numpy as np
import pytest

from xpd_tools.optimization import worker
from xpd_tools.optimization.agent import BuildAgent
from xpd_tools.optimization.helpers.dofs import Pump
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.helpers.qepro import PlqyReference
from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle
from xpd_tools.optimization.worker import (
    WORKER_PLAN_NAMES,
    plan_context_from_config,
    plan_name_for_config,
    register_worker_plans,
)

Screening = Literal["unscreened", "screen_only", "screen_and_record"]

_PUMPS = ("dds-CsPb", "dds-Br", "dds-dil", "dds-wash")
_DEVICES = {
    name: object()
    for name in (*_PUMPS, "qepro", "led", "uv_shutter", "fast_shutter", "xray_detector")
}


def _wrap(plan: Any, no_dark: bool) -> Any:  # noqa: ARG001
    return plan


def _phase(tmp_path: Path, name: str = "Wanted") -> Phase:
    radial = np.linspace(1.0, 25.0, 200)
    gr = tmp_path / f"{name}.gr"
    np.savetxt(gr, np.column_stack((radial, np.sin(radial))))
    cif = tmp_path / f"{name}.cif"
    cif.write_text(
        "data_test\n_symmetry_space_group_name_H-M 'P 1'\n"
        "_cell_length_a 6\n_cell_length_b 6\n_cell_length_c 6\n"
        "_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n"
    )
    return Phase(name=name, gr=str(gr), cif=str(cif))


def _agent(
    evaluation_method: str = "uvvis",
    *,
    pump: str = "dds-CsPb",
    screening: Screening | None = None,
    tmp_path: Path | None = None,
) -> BuildAgent:
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
                syringe_ml=20,
                material="steel",
            ),
        ],
        dilutions=[
            DilutionStage(
                pump="dds-dil",
                syringe_ml=50,
                material="steel",
                ratio=0.5,
                position="after_equilibrium",
                target_ml=40,
            ),
        ],
        wash_cycles=[
            WashCycle(
                pump="dds-wash",
                syringe_ml=50,
                material="steel",
                duration_sec=60,
                rate_ul_min=500,
                target_ml=40,
            ),
        ],
        mixer_lengths_cm=[25.0, 10.0],
        residence_time_ratio=0.8,
    )
    if screening is not None:
        assert tmp_path is not None
        agent.set_xray_objectives(
            screening=screening, phases=[_phase(tmp_path)], exposure=30.0, no_dark=True
        )
    if evaluation_method != "xray" or screening != "unscreened":
        agent.set_uvvis_objectives(plqy=PlqyReference(), use_good_bad=True, max_bad=2)
    return agent


def _config(agent: BuildAgent) -> dict[str, Any]:
    config = agent.to_config()
    assert config is not None
    return config


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


@pytest.mark.parametrize(
    ("evaluation_method", "screening", "plan_name"),
    [
        ("uvvis", None, "uvvis_acquire"),
        ("xray", "unscreened", "xray_acquire"),
        ("xray", "screen_only", "xray_screened_acquire"),
        ("xray", "screen_and_record", "xray_uvvis_acquire"),
        ("xray-uvvis", "screen_and_record", "xray_uvvis_acquire"),
    ],
)
def test_context_matches_build_agent(
    tmp_path: Path, evaluation_method: str, screening: Screening | None, plan_name: str
) -> None:
    """The worker builds the same plan context BuildAgent would, from its config."""
    agent = _agent(evaluation_method, screening=screening, tmp_path=tmp_path)
    config = _config(agent)
    assert plan_name_for_config(config) == plan_name == agent._acquisition_plan_name()
    rebuilt = BuildAgent.from_config(config)
    expected = rebuilt._build_plan_context(
        _DEVICES, _wrap, rebuilt.mixer_lengths_cm, rebuilt.residence_time_ratio
    )
    assert plan_context_from_config(plan_name, config, _DEVICES, _wrap) == expected


def test_worker_imports_without_the_optimizer_stack() -> None:
    """qs1 runs plans only; blop/bluesky-adaptive, Ax, torch stay on the workstation."""
    code = """
import importlib.abc, sys
BLOCK = {"blop", "bluesky_adaptive", "ax", "torch", "botorch", "gpytorch", "sklearn",
         "pymatgen", "diffpy", "autoencoder_pdf", "mp_api"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCK:
            raise ModuleNotFoundError(f"blocked {name}")
sys.meta_path.insert(0, Block())
import xpd_tools.optimization.worker
from xpd_tools.optimization.devices import build_halide_devices
build_halide_devices(mock=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


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
        next(plan([{}], [], config=_config(_agent())))


def test_xray_plan_without_detector_or_wrapper_is_refused(tmp_path: Path) -> None:
    config = _config(_agent("xray", screening="unscreened", tmp_path=tmp_path))
    devices = {k: v for k, v in _DEVICES.items() if k != "xray_detector"}
    with pytest.raises(ValueError, match="xray_detector"):
        next(
            register_worker_plans(devices=devices)["xray_acquire"](
                [{}], [], config=config
            )
        )
    with pytest.raises(ValueError, match="wrap_xray_run"):
        next(
            register_worker_plans(devices=_DEVICES)["xray_acquire"](
                [{}], [], config=config
            )
        )


def test_binding_is_cached_per_config() -> None:
    calls: list[tuple] = []
    contexts: list[Any] = []

    def factory(context):
        contexts.append(context)
        return _fake_bound_plan(calls)

    with patch.dict(worker._PLAN_FACTORIES, {"uvvis_acquire": factory}):
        plan = register_worker_plans(devices=_DEVICES)["uvvis_acquire"]
        first = _config(_agent())
        messages, uid = _run(plan([{"a": 1}], ["act"], None, {"k": 1}, config=first))
        _run(plan([{"a": 2}], ["act"], config=dict(first)))
        assert len(contexts) == 1
        _run(plan([{"a": 3}], ["act"], config=_config(_agent(pump="dds-Br"))))
        assert len(contexts) == 2

    assert messages == ["msg"]
    assert uid == "uid"
    assert calls[0] == ([{"a": 1}], ["act"], None, {"k": 1})
    assert contexts[1].sources[0].pump is _DEVICES["dds-Br"]


def test_xray_detector_and_wrapper_reach_the_context(tmp_path: Path) -> None:
    detector = object()
    contexts: list[Any] = []

    def factory(context):
        contexts.append(context)
        return _fake_bound_plan([])

    devices = {k: v for k, v in _DEVICES.items() if k != "xray_detector"}
    with patch.dict(worker._PLAN_FACTORIES, {"xray_acquire": factory}):
        plan = register_worker_plans(
            devices=devices, xray_detector=detector, wrap_xray_run=_wrap
        )["xray_acquire"]
        config = _config(_agent("xray", screening="unscreened", tmp_path=tmp_path))
        _run(plan([{}], [], config=config))

    assert contexts[0].xray_detector is detector
    assert contexts[0].wrap_xray_run is _wrap
    assert contexts[0].xray.exposure == 30.0


@pytest.mark.parametrize(
    ("send_config", "expected_keys"),
    [(True, {"config"}), (False, {"flow_config", "wash_config", "quality_config"})],
)
def test_build_sends_config_only_when_asked(
    send_config: bool, expected_keys: set[str]
) -> None:
    agent = _agent()
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
        assert kwargs["config"] == _config(agent)
