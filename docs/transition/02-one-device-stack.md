# Step 2: `autonomous` uses `main`'s devices

`autonomous` drives the QEPro, the syringe pumps, the LED and the UV shutter through its own ophyd v1 copies in
`optimization/devices/`. `main` has ophyd-async versions of all four, so the same hardware currently has two device
classes each. This step switches `autonomous` to `main`'s classes, one device per PR. These PRs target
`autonomous`, so they can run alongside steps 1 and 3.

| PR | Change | Needs | Shown working by |
|---|---|---|---|
| 2.0 | Merge `main` into `autonomous` (64 commits). This resolves the move of `plans.py` into `plans/`, and `autonomous`'s edits to `motors.py`, `flyers.py`, `detectors/panda.py` and `detectors/pilatus4.py` | — | `autonomous` tests pass |
| 2.1 | Pumps: `SyrngDDS`, `SyrngDDSAxis` and `SyrngUltra` replaced by `main`'s `DDSSyringeAxis`, `DDSSyringeController` and `UltraSyringePump` | 2.0 | Mock tests, plus a pump check at the beamline |
| 2.2 | QEPro: `devices/qepro.py` replaced by `main`'s `detectors/qepro.py` | 2.0 | Mock tests, plus a UV-vis check at the beamline |
| 2.3 | `LED` and `UVShutter` replaced by `main`'s `UVVisLightPath` | 2.0 | Mock tests |
| 2.4 | `build_halide_devices` builds `main`'s devices, and the fakes in `legacy/devices.py` are replaced by ophyd-async mocks | 2.1–2.3 | `optimization` tests pass |
| 2.5 | `FastShutter` replaced by 1.1 | 1.1, 2.0 | Mock tests |

## Question

- Is each switch safe to make between beamtimes? qs1 installs `autonomous`, so every PR here changes what runs at
  the beamline.
