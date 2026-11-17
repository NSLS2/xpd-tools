# 28-ID Shutter System Architecture

**Purpose:** Canonical reference for the beam shutter topology, PV addresses, and control logic across PDF (28-ID-1) and XPD (28-ID-2) beamlines. This document guides fast-shutter PR development, operation manuals, and CS-Studio screen corrections.

**Status:** PVs confirmed by Kyle Brogan (2026-11-17). Diagram scope: topology + control PV addresses + feedback readback patterns.

---

## Beam Path Topology

```
NSLS-II ring 
    ↓ (all beams to 28-ID)
    
FE Shutter (Front End, Hutch B)
    ↓ (gates all downstream 28-ID)
    
Monochromator (LAU optic splits PDF vs XPD energy)
    ├─→ PDF path → Fast Shutter (Hutch A/B) → PDF Hutch (28-ID-1)
    │
    └─→ XPD path → Fast Shutter (C-hutch) → XPD C-hutch (28-ID-2)
                   [NO shutter in D-hutch] ← KNOWN GAP
```

---

## Shutter Devices & PV Addresses

### 1. FE Shutter (Front End, Hutch B)

**Function:** Gates beam to both PDF and XPD. Used in both beamlines' tomo and XRD plans.

| Property | Value |
|----------|-------|
| **Physical location** | Hutch B (upstream of monochromator) |
| **Device class** | `PPSShutter` (Primary Photon Shutter, two-command style) |
| **PV prefix** | PV unknown — **ask Kyle** |
| **Control commands** | `{Cmd:Opn-Cmd}` (open), `{Cmd:Cls-Cmd}` (close) |
| **Feedback readback** | `{Pos-Sts}` (position status: "Open" / "Not Open") |
| **Settle time** | TBD after PV confirmed |
| **Used in** | xpdtools tomo plans (fe_shutter device) |
| **Status enum** | `ShutterStatus` = {OPEN: "Open", CLOSED: "Not Open"} |

**Device instantiation** (xpdtools profile):
```python
from xpd_tools.shutter import PPSShutter
fe_shutter = PPSShutter("PV_unknown", name="fe_shutter")  # production PV TBD
```

---

### 2. PDF Fast Shutter (Hutch A/B, before PDF Hutch)

**Function:** Protects PDF detectors and optics from beam. Used in PDF XRD plans.

| Property | Value |
|----------|-------|
| **Physical location** | Hutch A/B (between monochromator and PDF experimental hutch) |
| **Device class** | `FastShutter` (ophyd_async, inverted logic) |
| **PV prefix** | `XF:28ID1B-OP{PSh:1-Det:2}` ✓ |
| **Control command** | `{Cmd}` — OFF opens, ON closes (INVERTED) |
| **Feedback readback** | `{Sts}` — OFF = Open, ON = Close |
| **Settle time** | 0.1 s (soft timeout after move) |
| **Used in** | pdftools profiles, PDF XRD plans |
| **Status enum** | `OnOff` = {OFF, ON} |
| **Source** | pdftools/profiles/collection.py |

**Device instantiation** (pdftools profile):
```python
from pdftools.shutter import FastShutter
fs = FastShutter("XF:28ID1B-OP{PSh:1-Det:2}", name="fs")
```

**Note:** PDF fast shutter has **inverted control logic**: OFF opens the shutter, ON closes it. This is a hardware difference from XPD shutters.

---

### 3. XPD C-Hutch Fast Shutter

**Function:** Protects XPD C-hutch detectors and optics from beam. Used in XPD tomo and XRD plans.

| Property | Value |
|----------|-------|
| **Physical location** | C-hutch (between monochromator and XPD C-hutch experimental station) |
| **Device class** | `Shutter` (ophyd_async, two-command style) |
| **PV prefix** | PV unknown — **ask Kyle** |
| **Control commands** | `{Cmd:Opn-Cmd}` (open), `{Cmd:Cls-Cmd}` (close) |
| **Feedback readback** | `{Pos-Sts}` (position status: "Open" / "Not Open") |
| **Settle time** | TBD after PV confirmed |
| **Used in** | xpdtools tomo plans (photon_shutter device) |
| **Status enum** | `ShutterStatus` = {OPEN: "Open", CLOSED: "Not Open"} |

**Device instantiation** (xpdtools profile):
```python
from xpd_tools.shutter import Shutter
photon_shutter = Shutter("PV_unknown", name="photon_shutter")  # production PV TBD
```

---

### 4. XPD D-Hutch

**Status:** ⚠️ **NO FAST SHUTTER INSTALLED** (known hardware gap)

| Property | Value |
|----------|-------|
| **Physical location** | D-hutch (beam line from monochromator) |
| **Fast shutter** | None (not installed) |
| **Plans** | Tomo plans can run with FE shutter gate only; beam is not gated per-hutch |
| **Future work** | Add fast shutter and update CS-Studio screens (xf28id/28id2-phoebus) |

---

## Device Classes & Control Patterns

### PPSShutter (Front-End style)
- Two separate triggerable commands: open and close
- Status readback: "Open" or "Not Open"
- Example: FE shutter, potentially XPD fast shutter
- Source: xpdtools/shutter.py

```python
class PPSShutter(EpicsDevice, AsyncMovable[bool]):
    open_cmd: EpicsTriggerableCommand   # {Cmd:Opn-Cmd}
    close_cmd: EpicsTriggerableCommand  # {Cmd:Cls-Cmd}
    status: EpicsSignalRO[bool]          # {Pos-Sts}
```

### Shutter (Two-command style, alias for PPSShutter)
- Same as PPSShutter; two triggerable commands
- Status readback: "Open" or "Not Open"
- Source: xpdtools/shutter.py

```python
class Shutter(PPSShutter):
    # Identical to PPSShutter; alias for semantic clarity
```

### FastShutter (Cmd/Sts style, PDF-specific)
- Single read-write command register (inverted logic on PDF)
- Status readback with timeout and settle time
- **PDF inverted:** OFF opens, ON closes
- Example: PDF fast shutter only
- Source: pdftools/shutter.py

```python
class FastShutter(StandardReadable, Movable[str | int]):
    cmd: EpicsSignalRW[OnOff]      # {Cmd} — OFF open, ON close (inverted)
    status: EpicsSignalRO[OnOff]   # {Sts}
    state: derived_signal_r        # "Open" or "Close" (derived from status)
```

---

## Plan Usage

### Tomo Plans (xpdtools/tomo/flyscans.py)

Tomo dark/flat scans require both shutters:

```python
def tomo_dark_flat(
    num_frames: int,
    ...,
    fe_shutter: PPSShutter | None = None,  # Front-end shutter (gates all)
    photon_shutter: Shutter | None = None,  # C-hutch fast shutter
    ...
):
    """
    - Opens FE shutter and photon shutter before scanning
    - Closes both shutters after scan
    - Closes photon shutter between darks and flats (if out-of-beam motor moves)
    """
```

### XRD Plans (pdftools/xrd.py, xpdtools/xrd.py)

XRD scans conditionally use fast shutters:

```python
def xrd(
    ...,
    use_shutter: bool = True,
    shutter: FastShutter | None = None,  # PDF or XPD fast shutter
    ...
):
    """
    - Opens shutter before sample data collection
    - Closes shutter after collection
    - Skips if use_shutter=False
    """
```

---

## Known Issues & Future Work

### 1. Missing Real PV Addresses

| Shutter | PV | Status |
|---------|----|----|
| FE (Hutch B) | PV unknown | **Ask Kyle** |
| PDF fast (Hutch A/B) | `XF:28ID1B-OP{PSh:1-Det:2}` | ✓ Confirmed |
| XPD C-hutch fast | PV unknown | **Ask Kyle** |
| XPD D-hutch fast | N/A (not installed) | — |

### 2. CS-Studio Screen Errors

**Current screens:** `cs-studio-xf/28id` and `cs-studio-xf/28id2-phoebus`

| Issue | Current | Should Be | Priority |
|-------|---------|-----------|----------|
| Top fork label | "XPD-1" | "PDF" | High |
| Bottom fork (XPD) | Missing fast shutter symbol | Add fast shutter symbol | Medium |
| D-hutch | No shutter shown | Correct (no shutter installed) | Low |

**Proposed fixes:** File 3 GitHub issues in cs-studio-xf repos to update screens and reference this document.

### 3. Test PV Placeholders (xpdtools profiles)

| Profile | FE Shutter | Photon Shutter | Status |
|---------|-----------|----------------|--------|
| xpdd.py (tomo branch) | `TEST{Sh:FE}` | `TEST{Sh:sum}` | Mock mode only |
| devd.py (D-hutch) | `TEST{Sh:FE}` | `TEST{Sh:sum}` | Mock mode only |

**Next step:** Replace `TEST{Sh:*}` with real PVs once Kyle confirms addresses.

---

## References

- **pdftools/shutter.py** — FastShutter and FilterBank device classes
- **pdftools/profiles/collection.py** — PDF profile startup (fs device)
- **xpdtools/shutter.py** — PPSShutter and Shutter device classes
- **xpdtools/profiles/xpdd.py** — XPD-D tomo profile startup (fe_shutter, photon_shutter)
- **xpdtools/profiles/devd.py** — XPD-D standard profile startup
- **xpdtools/tomo/flyscans.py** — Tomo dark/flat plan (shutter logic)
- **hxm_program/DECISIONS.md** — Shutter system decisions and history
- **hxm_program/pages/pdf.html** — PDF beamline overview
- **hxm_program/pages/xpd.html** — XPD beamline overview

---

## Discussion & Feedback

**For Kyle (2026-11-17):**
- Confirm real PV addresses for FE shutter and XPD C-hutch fast shutter
- Confirm settle times and control patterns for each shutter
- Confirm whether PDF fast shutter inverted logic is intentional or a bug
- Review CS-Studio screen issues and proposed fixes

**For Ops/Beamline Staff:**
- Use PV addresses from Section 2 for Phoebus / CSS manual operations
- Refer to Device Classes section when troubleshooting shutter behavior
- File issues with beamline scientist if shutter does not respond as documented

---

**Last updated:** 2026-11-17 by copilot (from code inspection of pdftools, xpdtools, and Kyle's whiteboard session)
