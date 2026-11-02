"""Real regular-ophyd device classes for the 28-ID-2 C-hutch halide perovskite rig.

Device subpackage layout:
  pumps.py   -- SyrngDDS / SyrngDDSAxis (Harvard DDS) and SyrngUltra (Harvard Ultra)
  qepro.py   -- QEPro spectrometer
  lights.py  -- LED, UV_shutter, fast shutter (EpicsMotor)

The factory `build_halide_devices` returns a dict keyed by the same pump-id
strings used in the experiment config (`FlowSource.pump`, etc.) so it can be
passed directly to `BuildAgent.build_local` or `BuildAgent.register_acquisition_plans`.
"""

from __future__ import annotations

from typing import Any

from xpd_tools.optimization.devices.lights import LED, FastShutter, UVShutter
from xpd_tools.optimization.devices.pumps import SyrngDDS, SyrngDDSAxis, SyrngUltra
from xpd_tools.optimization.devices.qepro import QEPro

__all__ = [
    "ConnectionReport",
    "FastShutter",
    "LED",
    "QEPro",
    "SyrngDDS",
    "SyrngDDSAxis",
    "SyrngUltra",
    "UVShutter",
    "build_halide_devices",
    "check_halide_connections",
    "diagnose_connections",
    "print_connection_report",
]

# PV prefixes for the C-hutch flow rig (XF:28IDC-ES:1)
_DDS1_PREFIX = "XF:28IDC-ES:1{Pump:Syrng-DDS:1"
_DDS2_PREFIX = "XF:28IDC-ES:1{Pump:Syrng-DDS:2"
_DDS3_PREFIX = "XF:28IDC-ES:1{Pump:Syrng-DDS:3"
_ULTRA1_PREFIX = "XF:28IDC-ES:1{Pump:Syrng-Ultra:1}:"
_ULTRA2_PREFIX = "XF:28IDC-ES:1{Pump:Syrng-Ultra:2}:"
_QEPRO_PREFIX = "XF:28ID2-ES{QEPro:Spec-1}:"
_LED_PV = "XF:28IDC-ES:1{Light:Flu-LED:1}Cmd"
_UV_SHUTTER_PV = "XF:28IDC-ES:1{Light:Abs-Sht:1}Cmd"
_FAST_SHUTTER_PV = "XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr"


def build_halide_devices(*, mock: bool = False) -> dict[str, Any]:
    """Instantiate every device needed for a halide UV-Vis (or xray-uvvis) campaign.

    Returns a dict keyed by the pump-id strings used in experiment config
    (`FlowSource.pump`, `DilutionStage.pump`, `WashCycle.pump`) plus the
    named optical/detector devices:

        dds1_p1, dds2_p1, dds2_p2, dds3_p1, dds3_p2  -- DDS pump axes
        ultra1, ultra2                                  -- Ultra pump axes
        qepro                                           -- QEPro spectrometer
        led                                             -- fluorescence LED
        uv_shutter                                      -- absorbance shutter
        fast_shutter                                    -- beam fast-shutter motor

    Parameters
    ----------
    mock:
        When True return simulated devices (`legacy.devices.build_xpd_objects()`)
        instead of real EPICS-connected ones.  Useful for offline testing.
    """
    if mock:
        from xpd_tools.optimization.legacy.devices import build_xpd_objects
        return build_xpd_objects()

    dds1 = SyrngDDS(_DDS1_PREFIX, name="DDS1")
    dds2 = SyrngDDS(_DDS2_PREFIX, name="DDS2")
    dds3 = SyrngDDS(_DDS3_PREFIX, name="DDS3")

    return {
        # DDS pump axes  (note: PV strings contain literal braces from IOC naming)
        "dds1_p1": SyrngDDSAxis(
            _DDS1_PREFIX + "-Ax:A}",
            name="DDS1_p1",
            parent=dds1,
        ),
        "dds1_p2": SyrngDDSAxis(
            _DDS1_PREFIX + "-Ax:B}",
            name="DDS1_p2",
            parent=dds1,
        ),
        "dds2_p1": SyrngDDSAxis(
            _DDS2_PREFIX + "-Ax:A}",
            name="DDS2_p1",
            parent=dds2,
        ),
        "dds2_p2": SyrngDDSAxis(
            _DDS2_PREFIX + "-Ax:B}",
            name="DDS2_p2",
            parent=dds2,
        ),
        "dds3_p1": SyrngDDSAxis(
            _DDS3_PREFIX + "-Ax:A}",
            name="DDS3_p1",
            parent=dds3,
        ),
        "dds3_p2": SyrngDDSAxis(
            _DDS3_PREFIX + "-Ax:B}",
            name="DDS3_p2",
            parent=dds3,
        ),
        # Ultra pump axes
        "ultra1": SyrngUltra(_ULTRA1_PREFIX, name="Pump_Ultra1"),
        "ultra2": SyrngUltra(_ULTRA2_PREFIX, name="Pump_Ultra2"),
        # Spectrometer
        "qepro": QEPro(_QEPRO_PREFIX, name="QEPro"),
        # Lights and shutters
        "led": LED(_LED_PV, name="LED_M365LP1"),
        "uv_shutter": UVShutter(_UV_SHUTTER_PV, name="UV_shutter"),
        "fast_shutter": FastShutter(_FAST_SHUTTER_PV, name="shctl1"),
    }


class ConnectionReport:
    """Result of a single-device connection check.

    Three tiers are recorded separately so callers can distinguish them:

    * ``connected`` — EPICS channel-access handshake succeeded (IOC is up).
    * ``failed_signals`` — plan-critical PVs that could not be read (IOC up,
      but record broken or missing).
    * ``alive`` / ``liveness_detail`` — device-level liveness verdict.
      ``None`` means the liveness probe was not run.  ``False`` means the
      IOC is serving the PV but the underlying hardware is not responding
      (comms disabled, spectrometer unplugged, alarm INVALID, etc.).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.connected: bool = False
        self.readable_signals: list[str] = []
        self.failed_signals: list[str] = []
        self.error: str | None = None
        self.alive: bool | None = None
        self.liveness_detail: str = ""

    def __repr__(self) -> str:
        if not self.connected:
            status = "FAIL"
        elif self.alive is False:
            status = "IOC-only"
        else:
            status = "OK"
        detail = ""
        if self.failed_signals:
            detail += f", failed PVs: {self.failed_signals}"
        if self.liveness_detail:
            detail += f", liveness: {self.liveness_detail}"
        if self.error:
            detail += f", error: {self.error}"
        return f"<ConnectionReport {self.name}: {status}{detail}>"


# Pump device keys — both DDS axes and Ultra pumps share the same readback
# attribute names (read_infused, read_withdrawn, read_infuse_rate, read_withdraw_rate).
_PUMP_KEYS = {
    "dds1_p1", "dds1_p2",
    "dds2_p1", "dds2_p2",
    "dds3_p1", "dds3_p2",
    "ultra1",  "ultra2",
}

# EPICS alarm status code → human-readable name (subset most relevant to device comms)
_EPICS_STAT: dict[int, str] = {
    0:  "NO_ALARM",
    1:  "READ",
    2:  "WRITE",
    3:  "HIHI",
    4:  "HIGH",
    5:  "LOLO",
    6:  "LOW",
    7:  "STATE",
    8:  "COS",
    9:  "COMM",
    10: "TIMEOUT",
    11: "HW_LIMIT",
    12: "CALC",
    13: "SCAN",
    14: "LINK",
    15: "SOFT",
    16: "BAD_SUB",
    17: "UDF",
    18: "DISABLE",
    19: "SIMM",
    20: "READ_ACCESS",
    21: "WRITE_ACCESS",
}

# The pump liveness signal is the ``status`` record (run-state string such as
# ``'Idle'`` / ``'Infusing'``).  The IOC refreshes this continuously while the
# controller is communicating; it goes INVALID/COMM when the serial link drops.
#
# The four ``read_*`` readbacks (read_infused, read_withdrawn, read_infuse_rate,
# read_withdraw_rate) were tried first but are permanently dirty on this IOC:
# they sit in INVALID/COMM, INVALID/CALC, or INVALID/UDF even on healthy,
# connected pumps (the record simply never processes for an idle direction).
# Keying liveness off those records produces spurious ⚠ on every connected pump.
_PUMP_LIVENESS_SIGNAL = "status"

# Devices where no device-backed readback exists that reflects hardware liveness.
# Command/output PVs stay NO_ALARM regardless of hardware state.
# alive is set to None → shown as ✓ if connected, never fails overall-OK.
_NO_LIVENESS_SIGNAL_KEYS = {"led", "uv_shutter", "fast_shutter"}


def _epics_sevr(signal: Any) -> int | None:
    """Return the EPICS alarm severity integer for *signal*, or None if unavailable."""
    sev_attr = getattr(signal, "alarm_severity", None)
    if sev_attr is None:
        return None
    # ophyd may expose alarm_severity as an EpicsSignalRO-like object or a plain int
    if hasattr(sev_attr, "get"):
        try:
            return int(sev_attr.get())
        except Exception:
            return None
    try:
        return int(sev_attr)
    except (TypeError, ValueError):
        return None


def _epics_stat(signal: Any) -> int | None:
    """Return the EPICS alarm status integer for *signal*, or None if unavailable."""
    stat_attr = getattr(signal, "alarm_status", None)
    if stat_attr is None:
        return None
    if hasattr(stat_attr, "get"):
        try:
            return int(stat_attr.get())
        except Exception:
            return None
    try:
        return int(stat_attr)
    except (TypeError, ValueError):
        return None


def _check_liveness(key: str, device: Any, report: ConnectionReport) -> None:
    """Populate report.alive and report.liveness_detail for *device*.

    Liveness semantics:

    * ``True``  — probe ran, hardware is responding.
    * ``False`` — probe ran, hardware is **not** responding (INVALID alarm or
      device_connected == 0).  Results in ``⚠`` in the report.
    * ``None``  — no device-backed liveness signal exists for this role
      (LEDs, shutters).  IOC reachability (tier 1) and PV readability (tier 2)
      are still verified; liveness is simply not provable.

    Probe strategy
    --------------
    Pumps (DDS + Ultra):
        Read ``alarm_severity`` on the ``status`` record (run-state string,
        e.g. ``'Idle'``).  The IOC refreshes this continuously while the
        controller is communicating; it goes ``INVALID/COMM`` when the serial
        link drops.  ``SEVR == INVALID (3)`` → not alive.

        The four ``read_*`` readbacks were tried first but are permanently in
        alarm on this IOC even when the controller is healthy (INVALID/CALC for
        idle directions, INVALID/UDF for never-processed records).  They cannot
        distinguish "connected" from "detached" and are not used for liveness.

    QEPro:
        Read ``device_connected`` (``CONNECTED_RBV``).  Falsy → not alive.
        Note: empirically ``CONNECTED_RBV`` stays 1 even when the spectrometer
        is physically unplugged on this beamline — passive liveness is not fully
        provable for the QEPro.  The ``✓`` means the IOC sees the areaDetector
        driver; a real acquisition is the only definitive test.

    LED / uv_shutter / fast_shutter:
        No liveness probe possible.  alive = None.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        if key in _PUMP_KEYS:
            sig = getattr(device, _PUMP_LIVENESS_SIGNAL, None)
            if sig is None:
                report.alive = None
                report.liveness_detail = f"{_PUMP_LIVENESS_SIGNAL!r} attribute not found"
            else:
                sevr = _epics_sevr(sig)
                if sevr is None:
                    report.alive = None
                    report.liveness_detail = f"{_PUMP_LIVENESS_SIGNAL}: alarm_severity unavailable"
                else:
                    stat = _epics_stat(sig)
                    stat_name = _EPICS_STAT.get(stat, str(stat)) if stat is not None else "?"
                    try:
                        val = sig.get()
                    except Exception:
                        val = "?"
                    if sevr == 3:  # INVALID
                        report.alive = False
                        report.liveness_detail = (
                            f"status INVALID/{stat_name} — controller not responding"
                        )
                    else:
                        report.alive = True
                        report.liveness_detail = f"status={val!r} (NO_ALARM)"

        elif key == "qepro":
            connected_val = device.device_connected.get()
            report.alive = bool(connected_val)
            report.liveness_detail = f"CONNECTED_RBV={connected_val}"

        elif key in _NO_LIVENESS_SIGNAL_KEYS:
            # Command/output PVs — no hardware readback; liveness not provable.
            report.alive = None
            report.liveness_detail = "no device-backed liveness signal"

        else:
            # Unknown device type — best-effort alarm check on the device itself.
            sevr = _epics_sevr(device)
            if sevr is not None:
                report.alive = sevr < 3
                report.liveness_detail = f"alarm_severity={sevr}"
            else:
                report.alive = None
                report.liveness_detail = "alarm_severity unavailable"

    except Exception as exc:
        report.alive = False
        report.liveness_detail = f"liveness probe error: {exc}"
        logger.warning("Device %s liveness probe failed: %s", key, exc)


def check_halide_connections(
    devices: dict[str, Any],
    *,
    timeout: float = 10.0,
    require_alive: bool = True,
) -> dict[str, ConnectionReport]:
    """Validate EPICS connectivity for every device in *devices*.

    Performs **read-only** probes only — no set, no trigger, no plan execution.
    Three tiers are checked in order:

    1. **CA connect** — ``wait_for_connection``.  Confirms the IOC is up and
       serving the PV.  A failure here means the IOC itself is unreachable.

    2. **Signal readability** — ``.get()`` on the PVs the acquisition plans
       depend on.  Catches broken or missing records.

    3. **Device liveness** — device-specific probe that checks whether the
       physical hardware behind the IOC is actually responding:

       * Pumps (DDS + Ultra) → ``alarm_severity`` on the ``status`` record.
         The IOC refreshes ``status`` continuously while the controller
         communicates; ``INVALID/COMM`` when the serial link drops → not alive.
       * QEPro → ``device_connected`` (``CONNECTED_RBV``); falsy → not alive.
       * LED / shutters → ``alive = None``; command/output PVs have no
         device-backed readback so liveness cannot be probed.

    Parameters
    ----------
    devices:
        Dict as returned by ``build_halide_devices()``.
    timeout:
        Per-device connection timeout in seconds.
    require_alive:
        When ``True`` (default) the overall-OK verdict from
        ``print_connection_report`` is ``False`` if any device is not alive
        (IOC up but hardware detached).  Set ``False`` to downgrade liveness
        to a warning (⚠) that doesn't affect overall-OK — useful when testing
        against IOCs with no hardware attached.

    Returns
    -------
    dict mapping device key → ``ConnectionReport``.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Plan-critical signals per device role
    _required_signals: dict[str, list[str]] = {
        "dds1_p1": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "dds1_p2": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "dds2_p1": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "dds2_p2": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "dds3_p1": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "dds3_p2": ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "ultra1":  ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "ultra2":  ["status", "read_infuse_rate", "read_infuse_rate_unit"],
        "qepro":   ["x_axis", "output", "spectrum_type", "correction"],
        "led":     [],
        "uv_shutter":   [],
        "fast_shutter": [],
    }

    reports: dict[str, ConnectionReport] = {}

    for key, device in devices.items():
        report = ConnectionReport(key)
        reports[key] = report

        # ── Tier 1: CA connect ───────────────────────────────────────────────
        try:
            device.wait_for_connection(timeout=timeout)
            report.connected = True
        except Exception as exc:
            report.connected = False
            report.error = str(exc)
            logger.warning("Device %s failed to connect: %s", key, exc)
            continue

        # ── Tier 2: plan-critical signal readability ─────────────────────────
        required = _required_signals.get(key, [])
        for sig_name in required:
            try:
                getattr(device, sig_name).get()
                report.readable_signals.append(sig_name)
            except Exception as exc:
                report.failed_signals.append(sig_name)
                logger.warning("Device %s signal %s unreadable: %s", key, sig_name, exc)

        # Bare .get() for plain EpicsSignal devices (led, shutters)
        if not required:
            try:
                device.get()
                report.readable_signals.append("(value)")
            except Exception as exc:
                report.failed_signals.append("(value)")
                logger.warning("Device %s unreadable: %s", key, exc)

        # ── Tier 3: device liveness ──────────────────────────────────────────
        _check_liveness(key, device, report)

    # Attach the policy so print_connection_report can apply it
    reports["_require_alive"] = require_alive  # type: ignore[assignment]
    return reports


def print_connection_report(
    reports: dict[str, ConnectionReport],
) -> bool:
    """Print a three-state connection report and return True if all OK.

    Glyphs:

    * ``✓`` — connected, all signals readable, device alive.
    * ``⚠`` — IOC reachable and PVs served, but device is **not alive**
      (hardware detached or comms disabled).  Fails overall-OK when
      ``require_alive=True`` was passed to ``check_halide_connections``.
    * ``✗`` — CA connect failed or a plan-critical signal is unreadable.
    """
    require_alive: bool = reports.pop("_require_alive", True)  # type: ignore[arg-type]

    all_ok = True
    for key, r in reports.items():
        if not r.connected or r.failed_signals:
            all_ok = False
            status = "not connected" if not r.connected else "signal failure"
            print(f"  ✗  {key:<15}  [{status}]")
            if r.error:
                print(f"       error          : {r.error}")
            for sig in r.failed_signals:
                print(f"       failed signal  : {sig}")

        elif r.alive is False:
            if require_alive:
                all_ok = False
            print(f"  ⚠  {key:<15}  [IOC up — device not alive]  {r.liveness_detail}")

        else:
            live_note = f"  ({r.liveness_detail})" if r.liveness_detail else ""
            print(f"  ✓  {key:<15}{live_note}")

    return all_ok


# EPICS severity integer → label
_EPICS_SEVR: dict[int, str] = {
    0: "NO_ALARM",
    1: "MINOR",
    2: "MAJOR",
    3: "INVALID",
}

# Diagnostic PV attributes per device role — broader than the liveness probe.
# These are read for the per-PV triage table only, never written.
_DIAG_SIGNALS: dict[str, list[str]] = {
    "dds1_p1":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "dds1_p2":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "dds2_p1":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "dds2_p2":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "dds3_p1":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "dds3_p2":    ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "ultra1":     ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "ultra2":     ["status", "read_infuse_rate", "read_infused", "read_withdrawn", "read_withdraw_rate", "read_target_vol"],
    "qepro":      ["device_connected", "status", "status_msg", "x_axis", "output"],
    "led":        [],   # bare EpicsSignal — handled separately
    "uv_shutter": [],
    "fast_shutter": [],
}


def _diag_one_signal(name: str, sig: Any) -> None:
    """Print one row of the per-PV diagnostic table."""
    import time as _time

    try:
        val = sig.get()
    except Exception as exc:
        print(f"      {name:<28}  ERROR: {exc}")
        return

    # Severity
    sevr_int = _epics_sevr(sig)
    sevr_str = _EPICS_SEVR.get(sevr_int, str(sevr_int)) if sevr_int is not None else "n/a"

    # Status
    stat_int = _epics_stat(sig)
    stat_str = _EPICS_STAT.get(stat_int, str(stat_int)) if stat_int is not None else "n/a"

    # Timestamp age
    ts = getattr(sig, "timestamp", None)
    if ts and hasattr(ts, "__float__"):
        try:
            age = f"{_time.time() - float(ts):.1f}s ago"
        except Exception:
            age = "n/a"
    else:
        age = "n/a"

    # Truncate long values (arrays etc.)
    val_repr = repr(val)
    if len(val_repr) > 40:
        val_repr = val_repr[:37] + "..."

    flag = "  ← INVALID" if sevr_int == 3 else ""
    print(f"      {name:<28}  val={val_repr:<42}  SEVR={sevr_str:<8}  STAT={stat_str:<10}  {age}{flag}")


def diagnose_connections(
    devices: dict[str, Any],
    reports: dict[str, ConnectionReport] | None = None,
    *,
    only_failures: bool = True,
    timeout: float = 5.0,
) -> None:
    """Print a detailed per-PV diagnostic table for failed or all devices.

    Intended as a follow-up to ``print_connection_report`` when something shows
    ``⚠`` or ``✗``.  For each selected device it prints every diagnostic PV
    with its current value, decoded alarm severity (``NO_ALARM`` / ``MINOR`` /
    ``MAJOR`` / ``INVALID``), decoded alarm status (``COMM``, ``TIMEOUT``, …),
    and timestamp age.

    Parameters
    ----------
    devices:
        Dict as returned by ``build_halide_devices()``.
    reports:
        Existing ``ConnectionReport`` dict from a prior
        ``check_halide_connections`` call.  If ``None``, a fresh check is run
        automatically (using *timeout*).
    only_failures:
        When ``True`` (default) only print devices that are ``✗`` (not connected
        or failed signals) or ``⚠`` (``alive is False``).  Pass ``False`` to
        dump every device.
    timeout:
        Timeout used when *reports* is ``None`` and a fresh check is needed.

    Example
    -------
    ::

        from xpd_tools.optimization.devices import (
            build_halide_devices, check_halide_connections,
            print_connection_report, diagnose_connections,
        )

        devices = build_halide_devices()
        reports = check_halide_connections(devices, timeout=10.0)
        print_connection_report(reports)

        # Deep-dive on anything that failed:
        diagnose_connections(devices, reports)
    """
    # If reports were already consumed by print_connection_report (which pops
    # _require_alive) we need a fresh check.  Run one if reports is None or empty.
    if reports is None:
        reports = check_halide_connections(devices, timeout=timeout)
        # pop the sentinel so iteration below is clean
        reports.pop("_require_alive", None)  # type: ignore[arg-type]
    else:
        # Defensive: pop in case caller hasn't called print_connection_report yet
        reports.pop("_require_alive", None)  # type: ignore[arg-type]

    # Select which device keys to diagnose
    if only_failures:
        targets = {
            key for key, r in reports.items()
            if isinstance(r, ConnectionReport) and (
                not r.connected
                or bool(r.failed_signals)
                or r.alive is False
            )
        }
    else:
        targets = {key for key in reports if isinstance(reports[key], ConnectionReport)}

    if not targets:
        print("  ✓  All devices healthy — nothing to diagnose.")
        return

    print(f"  Diagnosing {len(targets)} device(s): {sorted(targets)}\n")

    for key in sorted(targets):
        r = reports[key]
        device = devices.get(key)
        if device is None:
            print(f"  [{key}]  device not found in devices dict")
            continue

        glyph = "✗" if (not r.connected or r.failed_signals) else "⚠"
        print(f"  {glyph}  {key}  —  {r.liveness_detail or r.error or ''}")
        print(f"      {'signal':<28}  {'value':<44}  {'SEVR':<12}  {'STAT':<12}  age")
        print(f"      {'-'*28}  {'-'*44}  {'-'*12}  {'-'*12}  {'-'*12}")

        sig_names = _DIAG_SIGNALS.get(key, [])

        if sig_names:
            for name in sig_names:
                sig = getattr(device, name, None)
                if sig is None:
                    print(f"      {name:<28}  (attribute not found)")
                    continue
                _diag_one_signal(name, sig)
        else:
            # Bare EpicsSignal (led, shutters) or unknown — probe device directly
            _diag_one_signal(f"({key})", device)

        print()
