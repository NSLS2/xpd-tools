# XPD-D Device Map Proposal — Questions Q1–Q5

**For:** Kyle Brogan (with Mehmet Topsakal; XPD-D tomography MVP)  
**Date:** 2026-11-05  
**Status:** Proposal for review and adjustment  

This document proposes answers to questions Q1–Q5 from the `xpdd-tomo-hextools-migration` task, to confirm with Kyle. They are starting points; adjust them to the current beamline configuration and user needs.

---

## Device Map

| hextools plan argument | HEX device | XPD-D device | Q# | Proposal | Notes |
|---|---|---|---|---|---|
| `detectors` | Kinetix / Phantom | `pilatus1` | — | Pilatus4 2M (XF:28ID2-ES{Pilatus4-Det:1}) | Phase 1 detector; IRIS & Xpress3 follow in later phases |
| `panda` | `panda` | `panda1` | — | XF:28ID2-ES{PANDA:1}: | Configured; encoder `calc.1.inpa = INENC1.VAL` ✓ |
| `rot_motor` | `RotationMotor` | `amPhi` or `twister` | **Q1** | **Phi2 spinner** (`XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr`) | Alternative: Twister if used; encoder zero position to be measured at the beamline |
| out-of-beam move | `sample_tower.x1` | `amBaseX` | **Q2** | **Move amBaseX +10mm** | Clears sample for dark/flat. Confirm travel and direction. |
| `fe_shutter` / `photon_shutter` | HEX shutters | — | **Q3** | **None (operator-managed)** | Plans call with `use_shutter=False`; hextools PR to make optional |
| stream name | `primary` | `tomo` | **Q4** | **Use 'tomo'** | Reconstruction pipeline expects this; confirm with data team |
| plans | — | — | **Q5** | **Full suite** | Fly, dark/flat, radiography, N-D loops; all needed for beamtime |

---

## Q1: Rotation Stage(s)

**Proposal:** Use **Phi2 (amPhi)** as the primary rotation motor.

- **PV:** `XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr`
- **Type:** `RotationMotor` (ophyd-async, with encoder feedback)
- **Encoder zero position:** Measure at the beamline. Current placeholder: `encoder_pos_at_zero=0`
- **Alternative:** Twister (`XF:28IDD-ES:2{Twister}Mtr`) if experiments require dual rotation capability or Phi2 is unavailable.

**Action:** Measure and hardcode `encoder_pos_at_zero` once beamline confirms homing position.

---

## Q2: Out-of-Beam Motor for Flat Fields

**Proposal:** Use **amBaseX** (XF:28IDD-ES:2{Stg:Stack-Ax:Xbase}Mtr) to move the sample out of the beam.

- **Current position:** Sample in beam at X = 0
- **Flat field position:** Move +10 mm (or confirm distance needed based on beam footprint)
- **Function:** `tomo_dark_flat()` plan uses this motor to clear the sample before taking dark and flat images

**Action:** Confirm exact travel distance and direction at the beamline.

---

## Q3: Shutters

**Proposal:** **No shutters configured** — managed by beamline operators.

- **Current state:** xpd-tools' XPD-D profile (`src/xpd_tools/profiles/xpdd.py`) defines no `fe_shutter` or `photon_shutter`
- **hextools integration:** Plans call with `use_shutter=False`
- **hextools PR required:** Make shutters optional (not mandatory) when unused ([see task file](https://github.com/NSLS2/hxm_program/blob/main/planning/tasks/xpd/xpdd-tomo-hextools-migration.md))

**Action:** If a shutter PV is available and needed, define it in the profile and update the proposal. Otherwise, AJ to submit PR making shutters optional in hextools.

---

## Q4: Stream Name for Tomography Data

**Proposal:** Use **`tomo`** as the stream name for tomography fly scans.

- **Current:** xpd-tools' `single_axis_flyscan` writes data to stream `tomo`
- **hextools default:** Writes to stream `primary`
- **Action:** hextools PR should accept a `stream_name` parameter (default `primary` for HEX, override to `tomo` for XPD-D)
- **Reason:** Reconstruction and export pipelines may expect `tomo`; confirm with data team

**Action:** Confirm with data/reconstruction team that `tomo` is the expected stream name.

---

## Q5: Which Plans Are Needed

**Proposal:** Full tomography suite required.

- ✅ `tomo_flyscan()` — standard ascending rotation scan
- ✅ `tomo_flyscan_loop()` — multi-pass with position adjustment between passes
- ✅ `tomo_dark_flat()` — dark and flat field acquisition
- ✅ `take_radiograph()` — single projection (sample in beam, no rotation)
- ✅ `tomo_1d_step_scan()`, `tomo_2d_step_scan()`, `tomo_nd_step_scan()` — multi-axis step scans
- ✅ N-D loops with Psylotech load frame (if needed; [see task file](https://github.com/NSLS2/hxm_program/blob/main/planning/tasks/xpd/psylotech-xts-vendor-software-det4.md))

**Rationale:** Users want full capability. If time is constrained, focus on fly, dark/flat, and radiography; N-D loops can follow.

**Action:** Confirm priority if time-boxed.

---

## Next Steps

1. **Kyle (with Mehmet) confirms or adjusts** the Q1–Q5 answers
2. **AJ prepares the hextools PR** (shutters optional, generic motors, mock tests)
3. **At the beamline:** measure the encoder zero and motor travel, confirm the device map

---

## References

- Task file: [`xpdd-tomo-hextools-migration.md`](https://github.com/NSLS2/hxm_program/blob/main/planning/tasks/xpd/xpdd-tomo-hextools-migration.md)
- hextools source: [`src/hextools/tomography/`](https://github.com/NSLS2/hextools/tree/main/src/hextools/tomography)
- xpd-tools source: [`src/xpd_tools/`](https://github.com/NSLS2/xpd-tools/tree/main/src/xpd_tools)
- Device map PVs: [`ioc_host_vars` / XPD-D](https://github.com/NSLS2/ioc_host_vars) (internal)

---

**Author:** AJ Sligar (DAD Duo)  
**Status:** Open for Kyle's and Mehmet's input
