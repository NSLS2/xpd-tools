# XPD-D tomography: questions for Kyle

**For:** Kyle Brogan · **From:** AJ Sligar (DAD Duo) · **Date:** 2026-11-06
**Goal:** xpd-tools ready for the XPD-D tomography experiments starting **2026-11-28**

All of this work goes into **xpd-tools**. hextools' tomography plans (`src/hextools/tomography/`) are the
template for how to write and test them; nothing changes in hextools.

Each answer below sets the direction of one or more xpd-tools PRs. The proposed answers are starting points
read from `src/xpd_tools/profiles/xpdd.py`; please correct them.

## What xpd-tools has

**On `main` when these questions were asked (2026-11-06):** `single_axis_flyscan` (stream `tomo`), the profile's
`rot_motor` for the Twister, `pilatus1` and `panda1`, and the mock fly-scan tests. `amPhi` and `iris1` were proposals;
both were in the `xpdd-tomo` profile by `1175d91` (`amPhi` from #29).

**On `xpdd-tomo` at `0889d26` (2026-11-22):**

- Plans: `tomo_flyscan`, `tomo_dark_flat`, `tomo_flyscan_loop`, `tomo_1d/2d/nd_step_scan` (#53, from hextools'
  template), each detector at its own rate; `single_axis_flyscan` as before.
- `profiles/xpdd.py`: `amPhi` (`RotationMotor`, Phi2, PandA encoder 1), stage motors including `amBaseX` and `amTopX`,
  `pilatus1`, `iris1` (`KinetixDetector`), `xspress3-1`, `panda1`, and the real shutters (#63). `profiles/devd.py`
  also has `dexela1` and `_fly` twins of each imaging detector.
- Mock tests for all of the above in `tests/tomo/`, `tests/test_shutter.py` and `tests/detectors/`.

Where each question stands now is under it, as **Status 2026-11-22**.

## Q1: Rotation stage

Which stage do the Nov–Dec experiments rotate on: `amPhi` (Phi2), the air bearing, the Twister, or more than
one? For each: which PandA encoder input, and the encoder resolution.

- **Proposed:** `amPhi` only, PandA encoder 1, as in the profile today.
- **Leads to:** a profile PR adding or fixing each `RotationMotor`, with a mock fly-scan test per stage, and a
  dry-run fly scan on that stage at the beamline (ascending and descending).

- **Status 2026-11-22:** answered: `amPhi` (Phi2, PandA encoder 1), in the `xpdd-tomo` profile since #29 and
  confirmed by Mehmet's device list (2026-11-09). A dry run ascending and descending at the beamline is not recorded.

## Q2: Detectors

Which detectors take the tomography images, and is each triggered by the PandA: Pilatus4, IRIS,
Xspress3?

- **Already known:** Jakub added the Xspress3 ophyd object and flew it with and without the Pilatus4
  (2026-11-06). The Xspress3 Mini's trigger input is **TTL1**, not TTL0.
- **Proposed:** Pilatus4, IRIS and Xspress3, all PandA-triggered.
- **Leads to:** Jakub's Xspress3 device in the XPD-D profile (his PR), and mock tests of a fly scan with each
  detector.

- **Status 2026-11-22:** Pilatus4 (TTL1), Xspress3 (TTL2), IRIS (TTL3) and, since #63, Dexela (TTL4), all
  PandA-triggered, each at its own rate (#53). Dexela is only in `devd.py`; the Prosilica is not usable in the tomography
  plans yet.

## Q3: Dark and flat fields

1. Which motor moves the sample out of the beam for flats, which way, and how far?
2. How is a dark taken at XPD-D: does Bluesky close a shutter (which PV), or does the operator?

- **Proposed:** `amBaseX`, +10 mm; no shutter in Bluesky (the profile has none today).
- **Leads to:** a `tomo_dark_flat` plan in xpd-tools, modelled on hextools' `tomo_dark_flat`, taking the
  out-of-beam motor and distance as arguments; a shutter device in the profile only if Bluesky should run it.

- **Status 2026-11-22:** `tomo_dark_flat` exists. It moves the sample out of the beam for the **flats** (default
  `amTopX`, +2.0; G6 in the review asks Mehmet which motor and how far). Bluesky closes the C-hutch motorized fast shutter
  for darks and opens it for flats (#63), after checking the front-end and photon shutters are open, which it never
  moves. `use_shutter=False` skips all shutter handling, so darks then see the beam.

## Q4: Who reads the data

What reads the scans afterwards (reconstruction, exporter), and what does it expect: stream names (`tomo`
today), and darks and flats in the same run or separate runs?

- **Proposed:** keep `tomo` for projections; darks and flats as their own run, in streams `dark` and `flat`, as
  hextools does.
- **Leads to:** the stream and run layout the new plans use, so reconstruction reads them without changes.

- **Status 2026-11-22:** darks and flats are their own run (`dark`, `flat`), as proposed. Projections changed with
  per-detector rates: one stream per detector plus a `rotation` stream, not `tomo` (#53). Who reads it is still
  Mehmet's call; see `XPD_DATA_PROCESSING.md`.

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

- **Kyle (2026-11-06):** basics first. Mehmet needs helper functions that do the basic things, like a count
  with keyword arguments for exposure time.
- **Order:** basic helpers (count with `exposure_time`, setting exposure) → dark/flat → `tomo_flyscan`
  (#45, already open) → radiograph → loops and step scans → alignment. Templates for the helpers: hextools'
  `edxd_count(count_time, num)` and `take_radiograph(exposure_time, num_images, …)`.
- **Leads to:** one xpd-tools PR per plan, in that order, each with mock tests as in `hextools/tests/tomography/`.
- **Status 2026-11-22:** dark/flat, `tomo_flyscan`, the loop and the step scans are in (#53). The basic helpers
  (a count with `exposure_time`), the radiograph and the alignment scan are not.
