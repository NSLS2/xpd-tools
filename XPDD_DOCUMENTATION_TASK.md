# Task: XPD-D documentation — what it is and how to use it

**Placeholder** (AJ, 2026-11-10). Start once the XPD-D functionality is proven at the beamline (experiments from
2026-11-28); until then the plans, profile and data layout are still moving. Comment on a line to add to it.

## Goal

Documentation in xpd-tools that tells beamline staff and users what the XPD-D endstation is and how to run it, so
nobody needs the people who built it in the room. xpd-tools has no documentation today beyond a one-line README.

## Shape

Follow hextools, the reference tools package: a `docs/` folder built with Sphinx from Markdown pages
(`index`, `getting_started`, `profile`, `cli`, `gui`, `developer`), with screenshots made by a script so they can be
regenerated. Built in CI so it cannot silently break.

## Contents (outline)

1. **What XPD-D is.** The D-hutch endstation of XPD (28-ID-2): what it is for (tomography, and X-ray
   diffraction, PDF and fluorescence measured during it), and its hardware: rotation stage, sample stack, PandA,
   detectors (Pilatus4, IRIS, Xspress3), shutters.
2. **Getting started.** Which workstation, starting the session with the XPD-D profile, starting a beamtime for a
   proposal.
3. **Plans.** Darks and flats, fly scans (including different exposures per detector), step and grid scans: what
   each does, its arguments, and a worked example.
4. **Data.** Where each run goes in Tiled, what it holds (streams, specs, metadata), and how to read it back,
   following [`XPD_DATA_PROCESSING.md`](XPD_DATA_PROCESSING.md).
5. **Troubleshooting.** Known limits and what to do when a scan fails.
6. **Developer.** Running the tests, adding a device or a plan, how changes reach the beamline.

## Done when

- The pages build in CI.
- Someone new can take darks and flats, run a fly scan and read the result from Tiled using only the docs.
- Kyle, Mehmet and Sanjit have reviewed them.

## Waits on

What the docs would describe: the shutter (review B1, G7), which profile runs at XPD-D (G4), per-detector rates in
fly scans, and the data layout processing needs (R3 in `XPD_DATA_PROCESSING.md`).
