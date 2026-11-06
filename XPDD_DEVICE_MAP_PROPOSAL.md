# XPD-D tomography: questions for Kyle

**For:** Kyle Brogan · **From:** AJ Sligar (DAD Duo) · **Date:** 2026-11-06
**Goal:** xpd-tools ready for the XPD-D tomography experiments starting **2026-11-28**

All of this work goes into **xpd-tools**. hextools' tomography plans (`src/hextools/tomography/`) are the
template for how to write and test them; nothing changes in hextools.

Each answer below sets the direction of one or more xpd-tools PRs. The proposed answers are starting points
read from `src/xpd_tools/profiles/xpdd.py`; please correct them.

## What xpd-tools already has

- `single_axis_flyscan` (PandA-triggered fly scan, stream `tomo`)
- `RotationMotor` for `amPhi` (PandA encoder 1); stage motors `amBaseX/Y`, `amTopX/Z`
- Detectors: `pilatus1` (Pilatus4), `iris1` (Kinetix); `panda1`
- Mock tests for the fly-scan core (`tests/test_flyers.py`, `tests/test_plans.py`, `tests/test_motors.py`)
- Open fly-scan fixes: #17 (encoder anchored at zero), #18 (encoder counts per revolution)

## Q1: Rotation stage

Which stage do the Nov–Dec experiments rotate on: `amPhi` (Phi2), the air bearing, the Twister, or more than
one? For each: which PandA encoder input, and the encoder resolution.

- **Proposed:** `amPhi` only, PandA encoder 1, as in the profile today.
- **Leads to:** a profile PR adding or fixing each `RotationMotor`, with a mock fly-scan test per stage; merging
  #17 and #18 once they're checked on that stage.

## Q2: Detectors

Which detectors take the tomography images, and is each triggered by the PandA: Pilatus4, IRIS (Kinetix),
Xspress3?

- **Proposed:** Pilatus4 and IRIS, both PandA-triggered; Xspress3 from nslsii (NSLS2/nslsii#269) if it's needed
  in a tomography scan.
- **Leads to:** a profile PR adding the Xspress3 device (and any missing trigger setup), with mock tests of a fly
  scan using each detector.

## Q3: Dark and flat fields

1. Which motor moves the sample out of the beam for flats, which way, and how far?
2. How is a dark taken at XPD-D: does Bluesky close a shutter (which PV), or does the operator?

- **Proposed:** `amBaseX`, +10 mm; no shutter in Bluesky (the profile has none today).
- **Leads to:** a `tomo_dark_flat` plan in xpd-tools, modelled on hextools' `tomo_dark_flat`, taking the
  out-of-beam motor and distance as arguments; a shutter device in the profile only if Bluesky should run it.

## Q4: Who reads the data

What reads the scans afterwards (reconstruction, exporter), and what does it expect: stream names (`tomo`
today), and darks and flats in the same run or separate runs?

- **Proposed:** keep `tomo` for projections; darks and flats as their own run, in streams `dark` and `flat`, as
  hextools does.
- **Leads to:** the stream and run layout the new plans use, so reconstruction reads them without changes.

## Q5: Which plans, in what order

Which plans do users need on 2026-11-28, and which can wait? The hextools plans that could be ported:

| Plan (hextools template) | What it does |
|---|---|
| `tomo_flyscan` | fly scan with frame averaging and the fixes hextools added after xpd-tools split from it |
| `tomo_dark_flat` | darks and flats only (Q3) |
| `take_radiograph` | one image, sample in beam, no rotation |
| `tomo_flyscan_loop` | repeated fly scans with moves between them |
| `tomo_1d/2d/nd_step_scan` | a fly scan at each point of a 1-D, 2-D or N-D grid (e.g. with the Psylotech) |
| `tomo_alignment_scan` | rotation-axis alignment check |

- **Proposed order:** dark/flat → `tomo_flyscan` → radiograph → loops and step scans → alignment.
- **Leads to:** one xpd-tools PR per plan, in that order, each with mock tests as in `hextools/tests/tomography/`.
