# Step 0: inventory

What the autonomous campaign uses today, where each piece is defined, and which step moves it.

- **qserver:** xpd-profile-collection `qserver` `141f81f`, `startup/` folder.
- **main:** xpd-tools `main` `6f8491d`.
- **autonomous:** xpd-tools `autonomous` `24adb3c`, under `src/xpd_tools/optimization/`.

## Devices

| Object | Defined in qserver | In `main`? | Copy on `autonomous` | Step |
|---|---|---|---|---|
| Fast shutter `fs` (= `shctl1`) | `10-motors.py:53`, `EpicsMotor('XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr')`; −20 open, 20 closed | no | `devices/lights.py` `FastShutter(EpicsMotor)` | 1.1, 2.5 |
| PerkinElmer `pe1c` | `80-areadetector.py:303`, `PerkinElmerContinuous`, prefix `XF:28IDC-ES:1{Det:PE1}` | no (hextools has an ophyd-async `PerkinElmerDetector`) | `legacy/devices.py` `FakeAreaDetector` (simulation only) | 1.2 |
| `sample_y` (default jog motor) | `10-motors.py:20`, `XF:28IDC-ES:1{SampArray-Ax:Y}Mtr` | no | — | 1.3 |
| QEPro `qepro` | `25-QEPro.py:555` | yes, `detectors/qepro.py` | `devices/qepro.py` (ophyd v1) | 2.2 |
| DDS and Ultra syringe pumps | `26-pump_ultra.py`, `27-pump_dds.py` | yes, `pumps.py` | `devices/pumps.py` (ophyd v1) | 2.1 |
| `LED`, `UV_shutter` | `28-Lights_shutter.py` | yes, `light_path.py` `UVVisLightPath` | `devices/lights.py` (ophyd v1) | 2.3 |

## What xpdAcq provides today

| Piece | Where | What it does | Step |
|---|---|---|---|
| `periodic_dark`, `take_dark` | xpdAcq `xpdacq.py:62`; `80-areadetector.py:114` | Takes a dark run before a run when the last dark is older than `glbl['dk_window']` | 3.1 |
| `_inject_qualified_dark_frame_uid` | xpdAcq `xpdacq.py:623` | Writes `sc_dk_field_uid` in the start document | 3.1 |
| `_inject_calibration_md` | xpdAcq `xpdacq.py:631` | Writes `calibration_md` and `detector_calibration_client_uid` | 3.2 |
| `bt.samples` | xpdAcq beamtime (`970-load.py`) | Supplies sample metadata from the spreadsheet | 3.3 |
| `_inject_analysis_stage` | xpdAcq `xpdacq.py:657` | Writes `analysis_stage: "raw"`; pdfstream's servers read it | 3.4 |
| `_pre_plan`, `configure_area_det`, `xpd_configuration['area_det']` | `94-CHL-plans.py:28`, xpdAcq, `30-bundle-plan.py:70` | Set exposure, frame time and the default detector | 3.5 |

## Plans

| Plan | Defined in qserver | Copies on `autonomous` | Step |
|---|---|---|---|
| `scan_with_dark` (`trigger_areaDet`) | `94-CHL-plans.py:197` (`:116`) | — | 4.1 |
| `ct_dark` | `94-CHL-plans.py:11` | — | 4.2 (if still used) |
| Flow helpers: `start_group_infuse`, `stop_group`, `set_group_infuse2`, `wait_equilibrium2`, `sleep_sec_q` | `29-flow_plan.py` | `plans/runtime.py`; `main` has `plans/pump_plans.py` | 5.1 |
| `uvvis_acquire` | `98-xpd-tools.py` | `qserver/acquire_variants.py` (verbatim), `plans/uvvis_only.py` (TJ's) | 5.2 |
| `xray_acquire` | `98-xpd-tools.py` | `qserver/acquire_variants.py` (verbatim), `plans/xray_only.py` (TJ's) | 5.3 |
| `xray_uvvis_acquire` | `97-xray_uvvis.py` | `qserver/xray_uvvis.py` (verbatim), `plans/xray_uvvis.py` (TJ's) | 5.4 |
| `xray_screened_acquire` | — | `plans/xray_screened.py` (TJ's) | 5.5 |
