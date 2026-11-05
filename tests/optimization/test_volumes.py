from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from bluesky.run_engine import RunEngine

from xpd_tools.optimization.plans import (
    FlowSource,
    RefillRequired,
    WashCycle,
    clear_infused_volumes,
    create_xray_uvvis_plan,
    refill_pumps,
)
from xpd_tools.optimization.plans.volumes import FIRST_MEASURE_SEC


def _source(pump: Any, **volumes: Any) -> FlowSource:
    return FlowSource(
        dof="infusion_rate_CsPb",
        pump=pump,
        precursor="CsPbOA",
        sample_label="CsPb",
        **volumes,
    )


# 600 µL/min for the first trial's assumed measurement time (no mixer wait): mL needed.
RATE = 600
NEED_ML = RATE / 1000 * FIRST_MEASURE_SEC / 60


def test_short_syringe_stops_before_any_pump_moves(
    RE: RunEngine, fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    pump = fake_pumps["dds2_p1"]
    # 3 mL loaded - 1 mL reserve - used = just under what the trial needs
    pump.read_infused.put(2.0 - NEED_ML + 0.01)
    plan = create_xray_uvvis_plan(
        plan_context_factory(sources=(_source(pump, loaded_ml=3.0),))
    )

    with pytest.raises(RefillRequired) as raised:
        RE(plan([{"infusion_rate_CsPb": RATE}], []))

    assert pump.configurations == [] and pump.start_count == 0
    assert raised.value.pumps == ["dds2_p1"]
    assert "dds2_p1 (CsPb)" in str(raised.value)
    assert refill_pumps(str(raised.value)) == ["dds2_p1"]  # as the GUI reads it

    # Refilled: clearing the counter lets the same plan run the trial.
    RE(clear_infused_volumes([pump]))
    assert pump.read_infused.get() == 0.0
    RE(plan([{"infusion_rate_CsPb": RATE}], []))
    assert pump.start_count == 1


def test_enough_volume_runs_and_the_counter_grows(
    RE: RunEngine, fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    pump = fake_pumps["dds2_p1"]
    pump.read_infused.put(2.0 - NEED_ML - 0.01)
    plan = create_xray_uvvis_plan(
        plan_context_factory(sources=(_source(pump, loaded_ml=3.0),))
    )
    RE(plan([{"infusion_rate_CsPb": RATE}], []))
    assert pump.read_infused.get() > 2.0 - NEED_ML - 0.01


def test_target_volume_and_wash_count(
    RE: RunEngine, fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    source, wash = fake_pumps["dds2_p1"], fake_pumps["ultra1"]
    context = plan_context_factory(
        # loaded 50 mL but the pump stops itself at its 30 mL target
        sources=(_source(source, loaded_ml=50.0, target_ml=30.0),),
        # 500 µL/min for 60 s = 0.5 mL per trial
        wash_cycles=(WashCycle(pump=wash, loaded_ml=2.0, reserve_ml=1.0),),
    )
    source.read_infused.put(28.9)  # 30 - 1 reserve - 28.9 = 0.1 < 0.2 mL needed
    wash.read_infused.put(0.6)

    with pytest.raises(RefillRequired) as raised:
        RE(create_xray_uvvis_plan(context)([{"infusion_rate_CsPb": 100}], []))
    assert raised.value.pumps == ["dds2_p1", "ultra1"]
    assert "ultra1 (wash)" in str(raised.value)


def test_counter_units_are_converted(
    RE: RunEngine, fake_pumps: Mapping[str, Any], plan_context_factory: Any
) -> None:
    pump = fake_pumps["dds2_p1"]
    pump.read_infused_unit.put("uL")
    pump.read_infused.put(1500.0)  # 1.5 mL of the 2 usable: not enough for NEED_ML
    plan = create_xray_uvvis_plan(
        plan_context_factory(sources=(_source(pump, loaded_ml=3.0),))
    )
    with pytest.raises(RefillRequired):
        RE(plan([{"infusion_rate_CsPb": RATE}], []))


def test_loaded_volume_must_fit_the_syringe(fake_pumps: Mapping[str, Any]) -> None:
    # Checked when the entry is created, so a config is refused at Load/Apply.
    with pytest.raises(
        ValueError,
        match=r"dds2_p1: loaded_ml must be > 0 and at most syringe_ml \(50.0\)",
    ):
        _source(fake_pumps["dds2_p1"], loaded_ml=60.0)
    with pytest.raises(ValueError, match="reserve_ml can't be negative"):
        _source(fake_pumps["dds2_p1"], reserve_ml=-1.0)


def test_refill_pumps_ignores_other_errors() -> None:
    assert refill_pumps("ValueError('xray_uvvis_acquire requires ...')") is None


def test_skip_waits_makes_plan_sleeps_instant() -> None:
    import time

    from bluesky import plan_stubs as bps

    from xpd_tools.optimization.legacy import skip_waits

    RE = RunEngine(context_managers=[])  # noqa: N806
    skip_waits(RE)
    start = time.monotonic()
    RE(bps.sleep(60))
    assert time.monotonic() - start < 5


def test_clear_plan_passes_queue_server_validation() -> None:
    """The worker's queue server must accept it and turn pump names into devices."""
    po = pytest.importorskip("bluesky_queueserver.manager.profile_ops")
    from xpd_tools.optimization.legacy.devices import FakePump

    pump = FakePump(name="dds2_p1")
    nspace = {"clear_infused_volumes": clear_infused_volumes, "dds2_p1": pump}
    plans, devices, *_ = po.existing_plans_and_devices_from_nspace(nspace=nspace)
    prepared = po.prepare_plan(
        {
            "name": "clear_infused_volumes",
            "args": [["dds2_p1"]],
            "kwargs": {},
            "user_group": "g",
        },
        plans_in_nspace={"clear_infused_volumes": clear_infused_volumes},
        devices_in_nspace={"dds2_p1": pump},
        allowed_plans={"g": plans},
        allowed_devices={"g": devices},
    )
    assert prepared["args"][0][0] is pump
