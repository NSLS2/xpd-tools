"""Smoke tests for the halide perovskite UV-Vis integration.

Three tiers:

1. Mock-logic tests (no IOCs required): run in CI.
   - Device factory returns all expected keys in mock mode.
   - ``register_acquisition_plans`` returns the right plan(s).
   - Full config round-trip → registration → plan callable is executable.

2. Device-connection smoke test (live IOCs required): hardware-gated with
   ``pytest.mark.hardware`` and skipped unless ``XPD_HARDWARE=1`` is set.
   - Instantiates real devices and validates EPICS connectivity.
   - Read-only probes only -- no pump motion, no triggers.

Run offline::

    pytest tests/optimization/test_halide_smoke.py -k "not hardware"

Run on beamline::

    XPD_HARDWARE=1 pytest tests/optimization/test_halide_smoke.py -v
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from xpd_tools.optimization.devices import (
    build_halide_devices,
    check_halide_connections,
    print_connection_report,
)


# ---------------------------------------------------------------------------
# Helpers to build a minimal but fully-wired UV-Vis BuildAgent
# ---------------------------------------------------------------------------

def _make_uvvis_agent():
    """Return a BuildAgent configured for a 4-DOF UV-Vis run (no xray)."""
    from xpd_tools.optimization.agent import BuildAgent
    from xpd_tools.optimization.helpers.dofs import Pump
    from xpd_tools.optimization.helpers.qepro import SpectraFitSettings
    from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle

    agent = BuildAgent(
        queue_server=True,
        http_server_uri="https://example.invalid",
        evaluation_method="uvvis",
    )
    agent.set_dofs([
        Pump(name="CsPb", id="dds2_p1", bounds=(10, 200)),
        Pump(name="Br",   id="dds2_p2", bounds=(10, 200)),
        Pump(name="Cl",   id="dds3_p1", bounds=(10, 200)),
        Pump(name="I",    id="dds3_p2", bounds=(10, 200)),
    ])
    agent.experiment(
        sources=[
            FlowSource(dof="infusion_rate_CsPb", pump="dds2_p1",
                       precursor="CsPbOA", sample_label="CsPb",
                       syringe_ml=20, material="steel"),
            FlowSource(dof="infusion_rate_Br",   pump="dds2_p2",
                       precursor="TOABr",  sample_label="Br",
                       syringe_ml=20, material="steel"),
            FlowSource(dof="infusion_rate_Cl",   pump="dds3_p1",
                       precursor="TOACl",  sample_label="Cl",
                       syringe_ml=20, material="steel"),
            FlowSource(dof="infusion_rate_I",    pump="dds3_p2",
                       precursor="TOAI",   sample_label="I",
                       syringe_ml=20, material="steel"),
        ],
        dilutions=[
            DilutionStage(pump="dds1_p1", syringe_ml=50, material="steel",
                          ratio=0.5, position="after_equilibrium", target_ml=40),
            DilutionStage(pump="ultra2",  syringe_ml=50, material="steel",
                          ratio=0.5, position="after_equilibrium", target_ml=40),
        ],
        wash_cycles=[
            WashCycle(pump="ultra1", syringe_ml=50, material="steel",
                      duration_sec=60, rate_ul_min=500, target_ml=40),
        ],
    )
    agent.set_uvvis_objectives(
        peak_target=660.0,
        peak_tolerance=5.0,
        fit_settings=SpectraFitSettings(),
    )
    return agent


# ---------------------------------------------------------------------------
# Tier 1: mock-logic tests (no IOCs)
# ---------------------------------------------------------------------------

class TestBuildHalideDevicesMock:
    """build_halide_devices(mock=True) returns the right shape."""

    @pytest.fixture
    def mock_devices(self) -> dict[str, Any]:
        return build_halide_devices(mock=True)

    def test_all_pump_keys_present(self, mock_devices):
        expected_pumps = {
            "dds1_p1", "dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2",
            "ultra1", "ultra2",
        }
        assert expected_pumps.issubset(mock_devices.keys())

    def test_optical_keys_present(self, mock_devices):
        assert "qepro" in mock_devices
        assert "led" in mock_devices
        assert "uv_shutter" in mock_devices
        assert "fast_shutter" in mock_devices

    def test_pumps_have_required_attributes(self, mock_devices):
        for key in ("dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2", "ultra1", "ultra2"):
            pump = mock_devices[key]
            assert hasattr(pump, "set_infuse2"), f"{key} missing set_infuse2"
            assert hasattr(pump, "infuse_pump2"), f"{key} missing infuse_pump2"
            assert hasattr(pump, "stop_pump2"), f"{key} missing stop_pump2"

    def test_qepro_has_required_attributes(self, mock_devices):
        qepro = mock_devices["qepro"]
        assert hasattr(qepro, "x_axis")
        assert hasattr(qepro, "output")
        assert hasattr(qepro, "spectrum_type")
        assert hasattr(qepro, "correction")


class TestRegisterAcquisitionPlans:
    """register_acquisition_plans returns the right plan for each evaluation_method."""

    @pytest.fixture
    def mock_devices(self) -> dict[str, Any]:
        return build_halide_devices(mock=True)

    def test_uvvis_registers_uvvis_acquire(self, mock_devices):
        agent = _make_uvvis_agent()
        plans = agent.register_acquisition_plans(mock_devices)
        assert "uvvis_acquire" in plans
        assert callable(plans["uvvis_acquire"])

    def test_uvvis_does_not_register_xray_plans(self, mock_devices):
        agent = _make_uvvis_agent()
        plans = agent.register_acquisition_plans(mock_devices)
        assert "xray_acquire" not in plans
        assert "xray_uvvis_acquire" not in plans

    def test_namespace_injection(self, mock_devices):
        agent = _make_uvvis_agent()
        ns: dict[str, Any] = {}
        agent.register_acquisition_plans(mock_devices, namespace=ns)
        assert "uvvis_acquire" in ns
        assert callable(ns["uvvis_acquire"])

    def test_xray_without_wrap_raises(self, mock_devices):
        """register_acquisition_plans raises ValueError when wrap_xray_run is
        required (xray evaluation) but not supplied."""
        from unittest.mock import patch

        from xpd_tools.optimization.agent import BuildAgent
        from xpd_tools.optimization.helpers.dofs import Pump
        from xpd_tools.optimization.plans import FlowSource

        agent = BuildAgent(
            queue_server=True,
            http_server_uri="https://example.invalid",
            evaluation_method="xray",
        )
        agent.set_dofs([Pump(name="CsPb", id="dds2_p1", bounds=(10, 200))])
        agent.experiment(sources=[
            FlowSource(dof="infusion_rate_CsPb", pump="dds2_p1",
                       precursor="CsPbOA", sample_label="CsPb",
                       syringe_ml=20, material="steel"),
        ])
        # Patch _validate_before_build so we reach the wrap_xray_run check
        with (
            patch.object(agent, "_validate_before_build", return_value=(True, False)),
            pytest.raises(ValueError, match="wrap_xray_run"),
        ):
            agent.register_acquisition_plans(mock_devices)


class TestConfigRoundTrip:
    """to_config() → from_config() → register_acquisition_plans() round-trip."""

    @pytest.fixture
    def mock_devices(self) -> dict[str, Any]:
        return build_halide_devices(mock=True)

    def test_roundtrip_produces_callable_plan(self, mock_devices, tmp_path):
        original = _make_uvvis_agent()
        config_path = tmp_path / "halide_test.json"
        original.to_config(str(config_path))

        import json
        from xpd_tools.optimization.agent import BuildAgent
        with open(config_path) as f:
            config = json.load(f)
        rebuilt = BuildAgent.from_config(config)

        plans = rebuilt.register_acquisition_plans(mock_devices)
        assert "uvvis_acquire" in plans
        assert callable(plans["uvvis_acquire"])

    def test_roundtrip_preserves_evaluation_method(self, tmp_path):
        original = _make_uvvis_agent()
        config = original.to_config()
        from xpd_tools.optimization.agent import BuildAgent
        rebuilt = BuildAgent.from_config(config)
        assert rebuilt.evaluation_method == "uvvis"

    def test_roundtrip_preserves_source_pump_ids(self, tmp_path):
        original = _make_uvvis_agent()
        config = original.to_config()
        from xpd_tools.optimization.agent import BuildAgent
        rebuilt = BuildAgent.from_config(config)
        source_pumps = {s.pump for s in rebuilt.sources}
        assert source_pumps == {"dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2"}


class TestCheckHalideConnectionsLiveness:
    """Offline tests for the three-tier liveness logic in check_halide_connections.

    Uses lightweight mock objects that mimic the signal-attribute shape of the
    real devices without touching EPICS.  Each test targets one specific tier
    or edge case of the liveness verdict.
    """

    # ── Mock builders ────────────────────────────────────────────────────────

    @staticmethod
    def _sig(value, *, alarm_severity: int = 0):
        """Return a mock that behaves like an EpicsSignalRO with .get()."""
        m = MagicMock()
        m.get.return_value = value
        sev = MagicMock()
        sev.get.return_value = alarm_severity
        m.alarm_severity = sev
        return m

    def _dds_axis(self, comms: str = "Enabled"):
        """Mock SyrngDDSAxis: has parent.communication, status, read_infuse_rate."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.status = self._sig("Infusing")
        device.read_infuse_rate = self._sig(25.0)
        device.read_infuse_rate_unit = self._sig("ul/min")
        device.parent.communication.get.return_value = comms
        return device

    def _ultra(self, comms: str = "Enabled"):
        """Mock SyrngUltra: has communication, status, read_infuse_rate."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.status = self._sig("Idle")
        device.read_infuse_rate = self._sig(10.0)
        device.read_infuse_rate_unit = self._sig("ul/min")
        device.communication.get.return_value = comms
        return device

    def _qepro(self, *, connected: int = 1):
        """Mock QEPro: has device_connected, x_axis, output, etc."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.x_axis = self._sig([400.0, 800.0])
        device.output = self._sig(0.5)
        device.spectrum_type = self._sig("Corrected Sample")
        device.correction = self._sig(1)
        device.device_connected.get.return_value = connected
        return device

    def _shutter(self, *, alarm_severity: int = 0):
        """Mock EpicsSignal shutter: bare .get() + alarm_severity."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.get.return_value = 0
        sev = MagicMock()
        sev.get.return_value = alarm_severity
        device.alarm_severity = sev
        # No 'status' attr so the generic branch uses the device's alarm_severity
        del device.status
        return device

    # ── Tests ────────────────────────────────────────────────────────────────

    def test_dds_enabled_is_alive(self):
        devices = {"dds2_p1": self._dds_axis(comms="Enabled")}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["dds2_p1"].alive is True
        assert "Enabled" in reports["dds2_p1"].liveness_detail

    def test_dds_disabled_not_alive(self):
        devices = {"dds2_p1": self._dds_axis(comms="Disabled")}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["dds2_p1"].alive is False
        assert "Disabled" in reports["dds2_p1"].liveness_detail

    def test_ultra_enabled_is_alive(self):
        devices = {"ultra1": self._ultra(comms="Enabled")}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["ultra1"].alive is True

    def test_ultra_disabled_not_alive(self):
        devices = {"ultra2": self._ultra(comms="Disabled")}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["ultra2"].alive is False

    def test_qepro_connected_is_alive(self):
        devices = {"qepro": self._qepro(connected=1)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["qepro"].alive is True
        assert "CONNECTED_RBV=1" in reports["qepro"].liveness_detail

    def test_qepro_disconnected_not_alive(self):
        devices = {"qepro": self._qepro(connected=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["qepro"].alive is False
        assert "CONNECTED_RBV=0" in reports["qepro"].liveness_detail

    def test_shutter_valid_alarm_is_alive(self):
        devices = {"led": self._shutter(alarm_severity=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["led"].alive is True

    def test_shutter_invalid_alarm_not_alive(self):
        devices = {"led": self._shutter(alarm_severity=3)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["led"].alive is False

    def test_print_report_three_state(self, capsys):
        """print_connection_report emits ✓ / ⚠ / ✗ correctly."""
        from xpd_tools.optimization.devices import ConnectionReport

        alive_r = ConnectionReport("good")
        alive_r.connected = True
        alive_r.alive = True
        alive_r.liveness_detail = "comms=Enabled"

        warn_r = ConnectionReport("ioc_only")
        warn_r.connected = True
        warn_r.alive = False
        warn_r.liveness_detail = "comms=Disabled"

        fail_r = ConnectionReport("dead")
        fail_r.connected = False
        fail_r.error = "timeout"

        reports = {"good": alive_r, "ioc_only": warn_r, "dead": fail_r,
                   "_require_alive": True}
        result = print_connection_report(reports)

        out = capsys.readouterr().out
        assert "✓" in out
        assert "⚠" in out
        assert "✗" in out
        assert result is False  # dead + not-alive with require_alive=True

    def test_require_alive_false_warns_not_fails(self, capsys):
        """require_alive=False downgrades ⚠ to a non-failing warning."""
        from xpd_tools.optimization.devices import ConnectionReport

        warn_r = ConnectionReport("ioc_only")
        warn_r.connected = True
        warn_r.alive = False
        warn_r.liveness_detail = "comms=Disabled"

        reports = {"ioc_only": warn_r, "_require_alive": False}
        result = print_connection_report(reports)

        out = capsys.readouterr().out
        assert "⚠" in out
        assert result is True  # not-alive is only a warning here

    def test_ca_fail_short_circuits_liveness(self):
        """A CA connect failure skips the liveness probe (alive stays None)."""
        device = MagicMock()
        device.wait_for_connection.side_effect = TimeoutError("no ioc")
        reports = check_halide_connections({"dds2_p1": device}, timeout=0.1)
        r = reports["dds2_p1"]
        assert r.connected is False
        assert r.alive is None


class TestRealDeviceClasses:
    """Instantiate real device classes in isolation (no EPICS, just construction)."""

    def test_syrng_dds_instantiates(self):
        from xpd_tools.optimization.devices.pumps import SyrngDDS
        d = SyrngDDS("TEST:", name="test_dds")
        assert d.name == "test_dds"

    def test_syrng_dds_ax_instantiates(self):
        from xpd_tools.optimization.devices.pumps import SyrngDDS, SyrngDDSAxis
        parent = SyrngDDS("TEST:", name="test_dds_parent")
        ax = SyrngDDSAxis("TEST:AX:", name="test_dds_ax", parent=parent)
        assert ax.name == "test_dds_ax"
        assert hasattr(ax, "set_infuse2")
        assert hasattr(ax, "infuse_pump2")
        assert hasattr(ax, "stop_pump2")

    def test_syrng_ultra_instantiates(self):
        from xpd_tools.optimization.devices.pumps import SyrngUltra
        d = SyrngUltra("TEST:", name="test_ultra")
        assert d.name == "test_ultra"
        assert hasattr(d, "set_infuse2")
        assert hasattr(d, "infuse_pump2")
        assert hasattr(d, "stop_pump2")

    def test_qepro_instantiates(self):
        from xpd_tools.optimization.devices.qepro import QEPro
        d = QEPro("TEST:", name="QEPro")
        assert d.name == "QEPro"
        assert hasattr(d, "x_axis")
        assert hasattr(d, "output")
        assert hasattr(d, "spectrum_type")
        assert hasattr(d, "correction")

    def test_lights_instantiate(self):
        from xpd_tools.optimization.devices.lights import FastShutter, LED, UVShutter
        led = LED("TEST:LED", name="led")
        shutter = UVShutter("TEST:SHT", name="uv_shutter")
        fast = FastShutter("TEST:MTR", name="fast_shutter")
        assert led.name == "led"
        assert shutter.name == "uv_shutter"
        assert fast.name == "fast_shutter"


# ---------------------------------------------------------------------------
# Tier 2: device-connection smoke test (live IOCs, no acquisition)
# ---------------------------------------------------------------------------

hardware = pytest.mark.skipif(
    os.environ.get("XPD_HARDWARE", "0").strip() not in ("1", "true", "yes"),
    reason="Set XPD_HARDWARE=1 to run live-device connection checks.",
)


@hardware
class TestHalideDeviceConnections:
    """Validate EPICS connectivity of every device in the halide rig.

    These tests perform read-only probes only -- no pump motion, no
    spectrometer triggers, no plan execution.  Safe to run at any time
    when IOCs are up.

    Run::

        XPD_HARDWARE=1 pytest tests/optimization/test_halide_smoke.py \
            -k hardware -v --timeout=30
    """

    @pytest.fixture(scope="class")
    def live_devices(self) -> dict[str, Any]:
        return build_halide_devices(mock=False)

    @pytest.fixture(scope="class")
    def connection_report(self, live_devices):
        return check_halide_connections(live_devices, timeout=10.0)

    def test_print_report(self, connection_report, capsys):
        """Always print the full report so CI logs capture the state."""
        all_ok = print_connection_report(connection_report)
        captured = capsys.readouterr()
        print(captured.out)
        # Don't fail here -- individual signal tests below are the assertions.

    def test_qepro_connected(self, connection_report):
        r = connection_report["qepro"]
        assert r.connected, f"QEPro not connected: {r.error}"
        assert not r.failed_signals, f"QEPro signal failures: {r.failed_signals}"
        assert r.alive is not False, (
            f"QEPro IOC up but device not alive: {r.liveness_detail}"
        )

    def test_led_connected(self, connection_report):
        r = connection_report["led"]
        assert r.connected, f"LED not connected: {r.error}"

    def test_uv_shutter_connected(self, connection_report):
        r = connection_report["uv_shutter"]
        assert r.connected, f"UV shutter not connected: {r.error}"

    def test_fast_shutter_connected(self, connection_report):
        r = connection_report["fast_shutter"]
        assert r.connected, f"Fast shutter not connected: {r.error}"

    @pytest.mark.parametrize("pump_key", [
        "dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2",
        "dds1_p1", "ultra1", "ultra2",
    ])
    def test_pump_connected(self, connection_report, pump_key):
        r = connection_report[pump_key]
        assert r.connected, f"{pump_key} not connected: {r.error}"
        assert not r.failed_signals, (
            f"{pump_key} signal failures: {r.failed_signals}"
        )
        assert r.alive is not False, (
            f"{pump_key} IOC up but device not alive: {r.liveness_detail}"
        )
