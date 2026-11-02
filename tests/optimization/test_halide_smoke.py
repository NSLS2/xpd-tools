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
    def _alarm_sig(value: object = 0, *, sevr: int = 0, stat: int = 0) -> MagicMock:
        """Return a mock EpicsSignalRO with .get(), .alarm_severity, .alarm_status."""
        sig = MagicMock()
        sig.get.return_value = value
        sev_mock = MagicMock()
        sev_mock.get.return_value = sevr
        sig.alarm_severity = sev_mock
        sta_mock = MagicMock()
        sta_mock.get.return_value = stat
        sig.alarm_status = sta_mock
        return sig

    def _pump(self, *, read_infused_sevr: int = 0, read_infuse_rate_sevr: int = 0) -> MagicMock:
        """Mock pump device (DDS axis or Ultra) with the four liveness readbacks."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        # Plan-critical signals (tier 2)
        device.status = self._alarm_sig("Idle")
        device.read_infuse_rate = self._alarm_sig(25.0, sevr=read_infuse_rate_sevr, stat=9 if read_infuse_rate_sevr == 3 else 0)
        device.read_infuse_rate_unit = self._alarm_sig("ul/min")
        # Liveness readbacks (tier 3)
        device.read_infused = self._alarm_sig(0.0, sevr=read_infused_sevr, stat=9 if read_infused_sevr == 3 else 0)
        device.read_withdrawn = self._alarm_sig(0.0)
        device.read_withdraw_rate = self._alarm_sig(0.0)
        return device

    def _qepro(self, *, connected: int = 1) -> MagicMock:
        """Mock QEPro with device_connected and plan-critical signals."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.x_axis = self._alarm_sig([400.0, 800.0])
        device.output = self._alarm_sig(0.5)
        device.spectrum_type = self._alarm_sig("Corrected Sample")
        device.correction = self._alarm_sig(1)
        device.device_connected.get.return_value = connected
        return device

    def _shutter(self) -> MagicMock:
        """Mock LED/shutter: bare .get(), no alarm fields (command PV)."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.get.return_value = 0
        # No alarm_severity on command PVs
        del device.alarm_severity
        return device

    # ── Pump liveness tests ──────────────────────────────────────────────────

    def test_dds_all_readbacks_ok_is_alive(self):
        """All readbacks NO_ALARM → alive."""
        devices = {"dds2_p1": self._pump(read_infused_sevr=0, read_infuse_rate_sevr=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["dds2_p1"]
        assert r.alive is True
        assert "NO_ALARM" in r.liveness_detail

    def test_dds_read_infused_invalid_not_alive(self):
        """DDS axis: read_infused INVALID/COMM → not alive (matches real beamline data)."""
        devices = {"dds2_p1": self._pump(read_infused_sevr=3)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["dds2_p1"]
        assert r.alive is False
        assert "read_infused" in r.liveness_detail
        assert "COMM" in r.liveness_detail

    def test_ultra_read_infuse_rate_invalid_not_alive(self):
        """Ultra: read_infuse_rate INVALID/COMM → not alive (matches real beamline data)."""
        devices = {"ultra1": self._pump(read_infuse_rate_sevr=3)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["ultra1"]
        assert r.alive is False
        assert "read_infuse_rate" in r.liveness_detail
        assert "COMM" in r.liveness_detail

    def test_any_invalid_readback_fails_liveness(self):
        """Any one INVALID readback is enough to mark not alive."""
        devices = {"dds3_p2": self._pump(read_infused_sevr=3, read_infuse_rate_sevr=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["dds3_p2"].alive is False

    def test_all_pump_keys_checked(self):
        """Every pump key goes through the liveness probe."""
        pump_keys = ["dds1_p1", "dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2", "ultra1", "ultra2"]
        devices = {k: self._pump() for k in pump_keys}
        reports = check_halide_connections(devices, timeout=1.0)
        for k in pump_keys:
            assert reports[k].alive is True, f"{k} should be alive"

    # ── QEPro liveness tests ─────────────────────────────────────────────────

    def test_qepro_connected_is_alive(self):
        devices = {"qepro": self._qepro(connected=1)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["qepro"]
        assert r.alive is True
        assert "CONNECTED_RBV=1" in r.liveness_detail

    def test_qepro_disconnected_not_alive(self):
        devices = {"qepro": self._qepro(connected=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["qepro"]
        assert r.alive is False
        assert "CONNECTED_RBV=0" in r.liveness_detail

    # ── LED / shutter — no liveness signal ──────────────────────────────────

    def test_led_alive_is_none(self):
        """LED is a command PV — no liveness probe, alive stays None."""
        devices = {"led": self._shutter()}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["led"].alive is None
        assert "no device-backed" in reports["led"].liveness_detail

    def test_uv_shutter_alive_is_none(self):
        devices = {"uv_shutter": self._shutter()}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["uv_shutter"].alive is None

    def test_fast_shutter_alive_is_none(self):
        devices = {"fast_shutter": self._shutter()}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["fast_shutter"].alive is None

    # ── print_connection_report three-state output ───────────────────────────

    def test_print_report_three_state(self, capsys):
        """print_connection_report emits ✓ / ⚠ / ✗ correctly."""
        from xpd_tools.optimization.devices import ConnectionReport

        alive_r = ConnectionReport("pump_ok")
        alive_r.connected = True
        alive_r.alive = True
        alive_r.liveness_detail = "all readbacks NO_ALARM"

        warn_r = ConnectionReport("pump_dead")
        warn_r.connected = True
        warn_r.alive = False
        warn_r.liveness_detail = "INVALID: read_infused(COMM)"

        no_probe_r = ConnectionReport("led")
        no_probe_r.connected = True
        no_probe_r.alive = None
        no_probe_r.liveness_detail = "no device-backed liveness signal"

        fail_r = ConnectionReport("ioc_down")
        fail_r.connected = False
        fail_r.error = "timeout"

        reports = {
            "pump_ok": alive_r,
            "pump_dead": warn_r,
            "led": no_probe_r,
            "ioc_down": fail_r,
            "_require_alive": True,
        }
        result = print_connection_report(reports)
        out = capsys.readouterr().out

        assert "✓" in out   # pump_ok and led (alive=None → ✓)
        assert "⚠" in out   # pump_dead
        assert "✗" in out   # ioc_down
        assert result is False  # pump_dead (require_alive) + ioc_down

    def test_require_alive_false_warns_not_fails(self, capsys):
        """require_alive=False downgrades ⚠ to a warning; overall-OK stays True."""
        from xpd_tools.optimization.devices import ConnectionReport

        warn_r = ConnectionReport("pump_dead")
        warn_r.connected = True
        warn_r.alive = False
        warn_r.liveness_detail = "INVALID: read_infused(COMM)"

        reports = {"pump_dead": warn_r, "_require_alive": False}
        result = print_connection_report(reports)
        out = capsys.readouterr().out

        assert "⚠" in out
        assert result is True  # liveness is only a warning

    def test_alive_none_does_not_fail(self, capsys):
        """alive=None (no liveness signal) never fails overall-OK."""
        from xpd_tools.optimization.devices import ConnectionReport

        r = ConnectionReport("led")
        r.connected = True
        r.alive = None
        r.liveness_detail = "no device-backed liveness signal"

        reports = {"led": r, "_require_alive": True}
        result = print_connection_report(reports)
        assert result is True

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
