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
from typing import Any
from unittest.mock import MagicMock

import pytest

from xpd_tools.optimization.devices import (
    ConnectionReport,
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
    agent.set_dofs(
        [
            Pump(name="CsPb", id="dds2_p1", bounds=(10, 200)),
            Pump(name="Br", id="dds2_p2", bounds=(10, 200)),
            Pump(name="Cl", id="dds3_p1", bounds=(10, 200)),
            Pump(name="I", id="dds3_p2", bounds=(10, 200)),
        ]
    )
    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor="CsPbOA",
                sample_label="CsPb",
                syringe_ml=20,
                material="steel",
            ),
            FlowSource(
                dof="infusion_rate_Br",
                pump="dds2_p2",
                precursor="TOABr",
                sample_label="Br",
                syringe_ml=20,
                material="steel",
            ),
            FlowSource(
                dof="infusion_rate_Cl",
                pump="dds3_p1",
                precursor="TOACl",
                sample_label="Cl",
                syringe_ml=20,
                material="steel",
            ),
            FlowSource(
                dof="infusion_rate_I",
                pump="dds3_p2",
                precursor="TOAI",
                sample_label="I",
                syringe_ml=20,
                material="steel",
            ),
        ],
        dilutions=[
            DilutionStage(
                pump="dds1_p1",
                syringe_ml=50,
                material="steel",
                ratio=0.5,
                position="after_equilibrium",
                target_ml=40,
            ),
            DilutionStage(
                pump="ultra2",
                syringe_ml=50,
                material="steel",
                ratio=0.5,
                position="after_equilibrium",
                target_ml=40,
            ),
        ],
        wash_cycles=[
            WashCycle(
                pump="ultra1",
                syringe_ml=50,
                material="steel",
                duration_sec=60,
                rate_ul_min=500,
                target_ml=40,
            ),
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
            "dds1_p1",
            "dds2_p1",
            "dds2_p2",
            "dds3_p1",
            "dds3_p2",
            "ultra1",
            "ultra2",
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
        agent.experiment(
            sources=[
                FlowSource(
                    dof="infusion_rate_CsPb",
                    pump="dds2_p1",
                    precursor="CsPbOA",
                    sample_label="CsPb",
                    syringe_ml=20,
                    material="steel",
                ),
            ]
        )
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
        assert rebuilt.sources is not None
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

    def _pump(self, *, status_sevr: int = 0, status_stat: int = 0) -> MagicMock:
        """Mock pump device (DDS axis or Ultra).

        ``status_sevr`` drives the liveness verdict:
          * 0 (NO_ALARM) → alive=True
          * 3 (INVALID)  → alive=False
        The four ``read_*`` readbacks are included with INVALID/COMM to confirm
        they are no longer consulted by the liveness probe.
        """
        device = MagicMock()
        device.wait_for_connection.return_value = None
        # Liveness signal (tier 3) — what the probe actually reads
        device.status = self._alarm_sig("Idle", sevr=status_sevr, stat=status_stat)
        # Plan-critical signals (tier 2)
        device.read_infuse_rate = self._alarm_sig(25.0)
        device.read_infuse_rate_unit = self._alarm_sig("ul/min")
        # Deliberately INVALID — confirm these are ignored by the new probe
        device.read_infused = self._alarm_sig(0.0, sevr=3, stat=9)
        device.read_withdrawn = self._alarm_sig(0.0, sevr=3, stat=9)
        device.read_withdraw_rate = self._alarm_sig(0.0, sevr=3, stat=12)
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

    def test_pump_status_no_alarm_is_alive(self):
        """status NO_ALARM → alive=True even though read_* are INVALID."""
        devices = {"dds2_p1": self._pump(status_sevr=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["dds2_p1"]
        assert r.alive is True
        assert "NO_ALARM" in r.liveness_detail

    def test_pump_status_invalid_comm_not_alive(self):
        """status INVALID/COMM (controller serial dropped) → alive=False."""
        devices = {"dds2_p1": self._pump(status_sevr=3, status_stat=9)}
        reports = check_halide_connections(devices, timeout=1.0)
        r = reports["dds2_p1"]
        assert r.alive is False
        assert "INVALID" in r.liveness_detail
        assert "COMM" in r.liveness_detail

    def test_pump_read_pvs_ignored_when_status_ok(self):
        """read_infused/read_infuse_rate INVALID doesn't trip liveness if status is OK.

        This is the core regression guard: the four read_* records sit permanently
        in INVALID/COMM or INVALID/CALC on this IOC even on healthy pumps.  The
        mock wires them to INVALID to confirm they are not consulted.
        """
        # _pump() always sets read_infused/read_withdrawn/read_withdraw_rate to INVALID
        devices = {"ultra1": self._pump(status_sevr=0)}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["ultra1"].alive is True, (
            "read_* INVALID should be ignored; only status SEVR matters"
        )

    def test_pump_status_missing_gives_none(self):
        """If the status attribute is absent, alive=None (can't probe)."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        device.read_infuse_rate = self._alarm_sig(25.0)
        device.read_infuse_rate_unit = self._alarm_sig("ul/min")
        del device.status  # simulate missing attribute
        devices = {"dds2_p1": device}
        reports = check_halide_connections(devices, timeout=1.0)
        assert reports["dds2_p1"].alive is None

    def test_all_pump_keys_alive_when_status_ok(self):
        """Every pump key reports alive=True when status is NO_ALARM."""
        pump_keys = [
            "dds1_p1",
            "dds1_p2",
            "dds2_p1",
            "dds2_p2",
            "dds3_p1",
            "dds3_p2",
            "ultra1",
            "ultra2",
        ]
        devices = {k: self._pump(status_sevr=0) for k in pump_keys}
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

        assert "✓" in out  # pump_ok and led (alive=None → ✓)
        assert "⚠" in out  # pump_dead
        assert "✗" in out  # ioc_down
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


class TestDiagnoseConnections:
    """Offline tests for diagnose_connections."""

    @staticmethod
    def _alive_report(name: str) -> ConnectionReport:
        from xpd_tools.optimization.devices import ConnectionReport

        r = ConnectionReport(name)
        r.connected = True
        r.alive = True
        r.liveness_detail = "all readbacks NO_ALARM"
        return r

    @staticmethod
    def _dead_report(name: str) -> ConnectionReport:
        from xpd_tools.optimization.devices import ConnectionReport

        r = ConnectionReport(name)
        r.connected = True
        r.alive = False
        r.liveness_detail = "INVALID: read_infused(COMM)"
        return r

    @staticmethod
    def _disconnected_report(name: str) -> ConnectionReport:
        from xpd_tools.optimization.devices import ConnectionReport

        r = ConnectionReport(name)
        r.connected = False
        r.error = "timeout"
        return r

    @staticmethod
    def _mock_pump() -> MagicMock:
        """Pump device mock with all diagnostic signal attributes."""
        device = MagicMock()
        device.wait_for_connection.return_value = None
        for attr in (
            "status",
            "read_infuse_rate",
            "read_infused",
            "read_withdrawn",
            "read_withdraw_rate",
            "read_target_vol",
        ):
            sig = MagicMock()
            sig.get.return_value = 0.0
            sev = MagicMock()
            sev.get.return_value = 0
            sig.alarm_severity = sev
            sta = MagicMock()
            sta.get.return_value = 0
            sig.alarm_status = sta
            sig.timestamp = None
            setattr(device, attr, sig)
        return device

    def test_only_failures_skips_healthy_devices(self, capsys):
        """only_failures=True: healthy devices produce no output."""
        from xpd_tools.optimization.devices import diagnose_connections

        devices = {"dds2_p1": self._mock_pump()}
        reports = {"dds2_p1": self._alive_report("dds2_p1")}

        diagnose_connections(devices, reports, only_failures=True)
        out = capsys.readouterr().out
        assert "All devices healthy" in out
        assert "dds2_p1" not in out.split("All")[1]  # not in the table body

    def test_only_failures_prints_dead_device(self, capsys):
        """only_failures=True: ⚠ device gets a PV table."""
        from xpd_tools.optimization.devices import diagnose_connections

        devices = {"dds2_p1": self._mock_pump()}
        reports = {"dds2_p1": self._dead_report("dds2_p1")}

        diagnose_connections(devices, reports, only_failures=True)
        out = capsys.readouterr().out
        assert "dds2_p1" in out
        assert "read_infused" in out  # diagnostic signal present
        assert "SEVR" in out

    def test_only_failures_prints_disconnected_device(self, capsys):
        """only_failures=True: ✗ (not connected) device gets a PV table."""
        from xpd_tools.optimization.devices import diagnose_connections

        # Device that fails wait_for_connection
        dead_dev = MagicMock()
        dead_dev.wait_for_connection.side_effect = TimeoutError("no ioc")
        for attr in (
            "status",
            "read_infuse_rate",
            "read_infused",
            "read_withdrawn",
            "read_withdraw_rate",
            "read_target_vol",
        ):
            sig = MagicMock()
            sig.get.return_value = 0.0
            sig.alarm_severity = MagicMock()
            sig.alarm_severity.get.return_value = 0
            sig.alarm_status = MagicMock()
            sig.alarm_status.get.return_value = 0
            sig.timestamp = None
            setattr(dead_dev, attr, sig)

        devices = {"dds2_p1": dead_dev}
        reports = {"dds2_p1": self._disconnected_report("dds2_p1")}

        diagnose_connections(devices, reports, only_failures=True)
        out = capsys.readouterr().out
        assert "dds2_p1" in out

    def test_only_failures_false_dumps_all(self, capsys):
        """only_failures=False: healthy devices are also printed."""
        from xpd_tools.optimization.devices import diagnose_connections

        devices = {
            "dds2_p1": self._mock_pump(),
            "ultra1": self._mock_pump(),
        }
        reports = {
            "dds2_p1": self._alive_report("dds2_p1"),
            "ultra1": self._dead_report("ultra1"),
        }

        diagnose_connections(devices, reports, only_failures=False)
        out = capsys.readouterr().out
        assert "dds2_p1" in out  # healthy — still printed
        assert "ultra1" in out  # failed — printed

    def test_reports_none_runs_fresh_check(self, capsys):
        """reports=None triggers an internal check_halide_connections call."""
        from xpd_tools.optimization.devices import diagnose_connections

        # Pump with INVALID status → will show ⚠ under the new status-based probe
        pump = MagicMock()
        pump.wait_for_connection.return_value = None

        # status: INVALID/COMM → liveness fails → device appears in diagnose output
        status_sig = MagicMock()
        status_sig.get.return_value = "?"
        sev = MagicMock()
        sev.get.return_value = 3  # INVALID
        sta = MagicMock()
        sta.get.return_value = 9  # COMM
        status_sig.alarm_severity = sev
        status_sig.alarm_status = sta
        status_sig.timestamp = None
        pump.status = status_sig

        for attr in ("read_infuse_rate", "read_infuse_rate_unit"):
            sig = MagicMock()
            sig.get.return_value = 0.0
            sig.alarm_severity = MagicMock()
            sig.alarm_severity.get.return_value = 0
            sig.alarm_status = MagicMock()
            sig.alarm_status.get.return_value = 0
            sig.timestamp = None
            setattr(pump, attr, sig)

        diagnose_connections({"dds2_p1": pump}, reports=None, timeout=1.0)
        out = capsys.readouterr().out
        assert "dds2_p1" in out
        assert "status" in out


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
        from xpd_tools.optimization.devices.lights import LED, FastShutter, UVShutter

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
        print_connection_report(connection_report)
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

    @pytest.mark.parametrize(
        "pump_key",
        [
            "dds2_p1",
            "dds2_p2",
            "dds3_p1",
            "dds3_p2",
            "dds1_p1",
            "ultra1",
            "ultra2",
        ],
    )
    def test_pump_connected(self, connection_report, pump_key):
        r = connection_report[pump_key]
        assert r.connected, f"{pump_key} not connected: {r.error}"
        assert not r.failed_signals, f"{pump_key} signal failures: {r.failed_signals}"
        assert r.alive is not False, (
            f"{pump_key} IOC up but device not alive: {r.liveness_detail}"
        )


# ---------------------------------------------------------------------------
# Script-equivalence smoke tests
# Verify the configuration that halide_beamline.py and single_trial.py
# exercise produces correct _acquisition_plan_kwargs() output and passes
# the round-trip. This covers the same logic the scripts exercise without
# requiring a live queueserver.
# ---------------------------------------------------------------------------


def _make_beamline_agent():
    """Construct the BuildAgent exactly as halide_beamline.py does."""
    from xpd_tools.optimization.agent import BuildAgent
    from xpd_tools.optimization.helpers.dofs import Pump
    from xpd_tools.optimization.helpers.qepro import (
        PlqyReference,
        QualityPolicy,
        SpectraFitSettings,
    )
    from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle

    agent = BuildAgent(
        queue_server=True,
        http_server_uri="https://example.invalid",
        evaluation_method="uvvis",
    )

    agent.set_uvvis_objectives(
        peak_target=520.0,
        peak_tolerance=5.0,
        fwhm_role="objective",
        plqy_role="objective",
        peak_distance_role="objective",
        max_peak_distance_nm=50.0,
        min_plqy=1e-3,
        max_fwhm_nm=60,
        max_retries=10,
        retry_delay=2.0,
        fit_settings=SpectraFitSettings(
            pl_screen_key_height=200,
            pl_screen_peak_height=50,
            pl_screen_peak_distance=100,
            pl_percent_range=(40, 100),
            pl_wavelength_range=(400, 800),
            pl_fit_maxfev=100000,
            pl_fit_r2_window_sigma=3,
            absorbance_percent_range=(10, 70),
            absorbance_wavelength_range=(210, 700),
        ),
        plqy=PlqyReference(
            excitation_wavelength_nm=365,
            absorbance=0.361,
            pl_integral=952628,
            refractive_index=1.337,
            plqy=0.546,
            solvent_refractive_index=1.506,
        ),
    )

    agent.set_dofs(
        [
            Pump(name="CsPb", id="dds2_p1", bounds=(0, 100)),
            Pump(name="TOABr", id="dds2_p2", bounds=(0, 100)),
            Pump(name="Cl", id="dds3_p1", bounds=(0, 100)),
            Pump(name="I", id="dds3_p2", bounds=(0, 100)),
        ]
    )

    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor="CsPb",
                sample_label="Cs-rich 33mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_TOABr",
                pump="dds2_p2",
                precursor="TOABr",
                sample_label="TOABr",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_Cl",
                pump="dds3_p1",
                precursor="Cl",
                sample_label="Pb-rich 33mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_I",
                pump="dds3_p2",
                precursor="I",
                sample_label="I",
                loaded_ml=30.0,
            ),
        ],
        dilutions=[
            DilutionStage(
                pump="dds1_p1",
                ratio=1.0,
                position="before_equilibrium",
                syringe_ml=20,
                material="plastic_BD",
                target_ml=20,
                loaded_ml=20.0,
            ),
            DilutionStage(
                pump="ultra2",
                ratio=1.0,
                position="after_equilibrium",
                syringe_ml=100,
                material="steel",
                target_ml=100,
                loaded_ml=100.0,
                wait_sec=30,
            ),
        ],
        wash_cycles=[
            WashCycle(pump="ultra1", loaded_ml=30.0),
        ],
        rate_unit="ul/min",
        mixer_lengths_cm=(30.0,),
        residence_time_ratio=1.0,
    )

    agent.quality_policy = QualityPolicy(
        enabled=True,
        good_batches=3,
        max_bad_batches=3,
        absorbance_shots=10,
        fluorescence_shots=10,
    )

    agent.set_run(iterations=50, extra_initialization_trials=5)

    return agent


class TestScriptEquivalence:
    """Smoke tests mirroring what halide_beamline.py and single_trial.py exercise.

    These run offline (no queueserver, no IOCs) and confirm:
    - _acquisition_plan_kwargs() produces all required config dicts.
    - flow_config contains the correct pump mappings and geometry.
    - wash_config and quality_config are populated (not empty/None).
    - The config round-trips cleanly through to_config()/from_config().
    - The rebuilt agent produces identical plan kwargs (the single_trial
      reload path).
    """

    def test_flow_config_has_all_four_sources(self) -> None:
        agent = _make_beamline_agent()
        fc = agent._acquisition_plan_kwargs()["flow_config"]
        assert len(fc["syringe_list"]) == 4
        assert len(fc["precursor_prefix_list"]) == 4
        assert len(fc["dof_to_pump"]) == 4

    def test_flow_config_dof_to_pump_mapping(self) -> None:
        agent = _make_beamline_agent()
        fc = agent._acquisition_plan_kwargs()["flow_config"]
        assert fc["dof_to_pump"] == {
            "infusion_rate_CsPb": "dds2_p1",
            "infusion_rate_TOABr": "dds2_p2",
            "infusion_rate_Cl": "dds3_p1",
            "infusion_rate_I": "dds3_p2",
        }

    def test_flow_config_geometry_fields(self) -> None:
        agent = _make_beamline_agent()
        fc = agent._acquisition_plan_kwargs()["flow_config"]
        assert fc["rate_unit"] == "ul/min"
        assert fc["mixer_lengths_cm"] == [30.0]
        assert fc["resident_t_ratio"] == 1.0

    def test_post_dilute_uses_both_dilution_stages(self) -> None:
        agent = _make_beamline_agent()
        fc = agent._acquisition_plan_kwargs()["flow_config"]
        assert fc["post_dilute"] is True
        assert fc["dilute_pump_name"] == ["dds1_p1", "ultra2"]
        assert fc["post_dilute_ratio"] == [1.0, 1.0]
        assert fc["post_dilute_wait_sec"] == 30.0

    def test_wash_config_ultra1(self) -> None:
        agent = _make_beamline_agent()
        wc = agent._acquisition_plan_kwargs()["wash_config"]
        assert wc["do_wash"] is True
        assert wc["pump_names"] == ["ultra1"]

    def test_quality_config_enabled(self) -> None:
        agent = _make_beamline_agent()
        qc = agent._acquisition_plan_kwargs()["quality_config"]
        assert qc["use_good_bad"] is True
        assert qc["good_target"] == 3
        assert qc["max_bad"] == 3
        assert qc["num_abs"] == 10
        assert qc["num_flu"] == 10

    def test_no_xray_config_for_uvvis(self) -> None:
        agent = _make_beamline_agent()
        assert "xray_config" not in agent._acquisition_plan_kwargs()

    def test_plan_kwargs_survive_config_round_trip(self) -> None:
        """single_trial.py reloads the config; flow and wash kwargs must be identical.

        Note: quality_policy.enabled is serialised inside the xray config block and
        is therefore only restored by from_config() when an xray objective is also
        configured.  For a uvvis-only agent the field defaults back to False after a
        round-trip, which is a known limitation of the current serialisation.  The
        test verifies the fields that *do* survive: flow_config and wash_config.
        """
        import json

        original = _make_beamline_agent()
        config = json.loads(json.dumps(original.to_config()))

        from xpd_tools.optimization.agent import BuildAgent

        rebuilt = BuildAgent.from_config(config)

        assert rebuilt.rate_unit == original.rate_unit
        assert rebuilt.mixer_lengths_cm == original.mixer_lengths_cm
        assert rebuilt.residence_time_ratio == original.residence_time_ratio

        orig_kwargs = original._acquisition_plan_kwargs()
        rebuilt_kwargs = rebuilt._acquisition_plan_kwargs()
        assert rebuilt_kwargs["flow_config"] == orig_kwargs["flow_config"]
        assert rebuilt_kwargs["wash_config"] == orig_kwargs["wash_config"]

    def test_build_produces_uvvis_acquire_plan(self) -> None:
        from unittest.mock import patch

        targets = [
            "xpd_tools.optimization.agent.from_uri",
            "xpd_tools.optimization.agent.from_profile",
            "xpd_tools.optimization.agent.REManagerAPI",
            "xpd_tools.optimization.agent.RemoteDispatcher",
            "bluesky_adaptive.ax.queueserver_agent.QueueserverClient",
            "bluesky_adaptive.ax.queueserver_agent.QueueserverOptimizationRunner",
        ]
        patchers = [patch(target) for target in targets]
        for patcher in patchers:
            patcher.start()
        try:
            agent = _make_beamline_agent()
            run_agent = agent.build()
            assert run_agent.acquisition_plan == "uvvis_acquire"
        finally:
            for patcher in patchers:
                patcher.stop()
