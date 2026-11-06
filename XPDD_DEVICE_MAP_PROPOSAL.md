# XPD-D Device Map Proposal — Answers to Q1–Q7

**For:** Mehmet Topsakal (XPD-D tomography MVP, sprint week ~2026-11-02)  
**Date:** 2026-11-05  
**Status:** Proposal for review and adjustment  

This document proposes concrete answers to questions Q1–Q7 from the `xpdd-tomo-hextools-migration` task. These are starting points for discussion on sprint week day 1; Mehmet should revise as needed based on current beamline configuration and user needs.

---

## Device Map

| hextools plan argument | HEX device | XPD-D device | Q# | Proposal | Notes |
|---|---|---|---|---|---|
| `detectors` | Kinetix / Phantom | `pilatus1` | — | Pilatus4 2M (XF:28ID2-ES{Pilatus4-Det:1}) | Phase 1 detector; IRIS & Xpress3 follow in later phases |
| `panda` | `panda` | `panda1` | — | XF:28ID2-ES{PANDA:1}: | Configured; encoder `calc.1.inpa = INENC1.VAL` ✓ |
| `rot_motor` | `RotationMotor` | `amPhi` or `twister` | **Q1** | **Phi2 spinner** (`XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr`) | Alternative: Twister if used; encoder zero position TBD during sprint |
| out-of-beam move | `sample_tower.x1` | `amBaseX` | **Q2** | **Move amBaseX +10mm** | Clears sample for dark/flat. Confirm travel and direction. |
| `fe_shutter` / `photon_shutter` | HEX shutters | — | **Q3** | **None (operator-managed)** | Plans call with `use_shutter=False`; hextools PR to make optional |
| stream name | `primary` | `tomo` | **Q4** | **Use 'tomo'** | Reconstruction pipeline expects this; confirm with data team |
| plans | — | — | **Q5** | **Full suite** | Fly, dark/flat, radiography, N-D loops; all needed for beamtime |
| — | — | — | **Q6** | **TBD Mehmet** | Beamline time: days 3–4 (no beam vs with beam) for sprint week |
| — | — | — | **Q7** | **Week 2026-11-02** | Confirm timing + Kyle's availability |

---

## Q1: Rotation Stage(s)

**Proposal:** Use **Phi2 (amPhi)** as the primary rotation motor.

- **PV:** `XF:28IDD-ES:2{Stg:Stack-Ax:Phi2}Mtr`
- **Type:** `RotationMotor` (ophyd-async, with encoder feedback)
- **Encoder zero position:** Measure during sprint week day 1 on beamline. Current placeholder: `encoder_pos_at_zero=0`
- **Alternative:** Twister (`XF:28IDD-ES:2{Twister}Mtr`) if experiments require dual rotation capability or Phi2 is unavailable.

**Action:** Measure and hardcode `encoder_pos_at_zero` once beamline confirms homing position.

---

## Q2: Out-of-Beam Motor for Flat Fields

**Proposal:** Use **amBaseX** (XF:28IDD-ES:2{Stg:Stack-Ax:Xbase}Mtr) to move the sample out of the beam.

- **Current position:** Sample in beam at X = 0
- **Flat field position:** Move +10 mm (or confirm distance needed based on beam footprint)
- **Function:** `tomo_dark_flat()` plan uses this motor to clear the sample before taking dark and flat images

**Action:** Confirm exact travel distance and direction during sprint week day 1.

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

## Q6: Beamline Time Availability

**Proposal:** TBD — Mehmet to specify.

- **Sprint week days 3–4:** Need beamline access for real scans (with beam preferred, but no-beam dry-run acceptable first)
- **Rehearsal period (11-09 → 11-20):** Additional availability for users to practice scans before freeze (11-20)

**Action:** Mehmet to provide beamline calendar; AJ coordinates with beamline scheduler.

---

## Q7: Sprint Week Timing and Team

**Proposal:** Week of **2026-11-02 → 2026-11-06** (Mon–Fri).

- **Attendees:** Mehmet (primary), AJ (hextools integration), Kyle (optional; QueueServer work elsewhere)
- **Format:** 1–2 hours/day in-person or video sync
- **Day 1 (11-02):** Walk through hextools plans, settle device map, confirm Q1–Q2 measurements
- **Day 2–5:** Profile instantiation, mock tests, dry-run on beamline, real scans, runbook

**Action:** Mehmet to confirm dates and availability.

---

## Next Steps

1. **Mehmet reviews and provides feedback** on this proposal (adjust any Q1–Q7 answers)
2. **AJ prepares hextools PR** (shutters optional, generic motors, mock tests)
3. **Sprint week day 1:** Hands-on measurement of encoder zero, motor travel, confirm device map
4. **Days 2–5:** Profile instantiation, CI testing, beamline validation
5. **11-20:** Code freeze; deploy locked environment

---

## References

- Task file: [`xpdd-tomo-hextools-migration.md`](https://github.com/NSLS2/hxm_program/blob/main/planning/tasks/xpd/xpdd-tomo-hextools-migration.md)
- hextools source: [`src/hextools/tomography/`](https://github.com/NSLS2/hextools/tree/main/src/hextools/tomography)
- xpd-tools source: [`src/xpd_tools/`](https://github.com/NSLS2/xpd-tools/tree/main/src/xpd_tools)
- Device map PVs: [`ioc_host_vars` / XPD-D](https://github.com/NSLS2/ioc_host_vars) (internal)

---

**Author:** AJ Sligar (DAD Duo)  
**Status:** Open for Mehmet's input and adjustment
