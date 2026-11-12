# Step 1: devices into `main`

Bring in the C-hutch devices that `scan_with_dark` needs and that `main` does not have yet. Each PR adds:

- one ophyd-async device;
- a mock and tests;
- the device's line in the C-hutch profile (`profiles/ldrd.py` today).

Nothing else changes.

| PR | Change | Source | Needs | Shown working by |
|---|---|---|---|---|
| 1.1 | Fast shutter: an ophyd-async motor with `open()` and `close()` plans (−20 is open, 20 is closed) | qserver `10-motors.py:53`; `autonomous` `devices/lights.py` `FastShutter` | — | Mock test: open and close move the motor to −20 and 20. Beamline: `RE(open)` and `RE(close)` with the beam off. |
| 1.2 | PerkinElmer `pe1c` as an ophyd-async area detector, prefix `XF:28IDC-ES:1{Det:PE1}` | qserver `80-areadetector.py:170-303`; hextools `detectors/perkin_elmer.py` | — | Mock test: prepare, trigger and read. Beamline: one `count` written to Tiled. |
| 1.3 | `sample_y`, only if a plan in steps 4–5 keeps the jogging option | qserver `10-motors.py:20` | — | Mock test |

## Questions

- **1.2:** the profile drives `pe1c` with `ContinuousAcquisitionTrigger`: the detector runs continuously, and a
  trigger collects the next frames. Do we need the same behaviour in ophyd-async, or is a normal triggered
  acquisition enough?
- **1.2:** should we copy hextools' `PerkinElmerDetector`, or move it into a shared package that both use?
- **1.3:** is the jogging in `trigger_areaDet` still used?
