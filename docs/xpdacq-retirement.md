# Retiring xpdAcq at XPD

This plan moves what XPD uses from xpdAcq into xpd-tools, one small PR at a time. When it is done,
xpd-profile-collection drops `xpdacq` from its environment.

## Rules

- **xpd-tools never imports `xpdacq` or `xpdconf`.** The behaviour is ported as plain bluesky plans and
  preprocessors, plus YAML settings files.
- **Port only what XPD uses today.** That means what xpd-tools `autonomous` and xpd-profile-collection `qserver`
  use. The `main` and `pixi_2026C2` profile branches use a subset of `qserver`. The rest of xpdAcq is not ported.
- **Small PRs**, each shown working with mock tests (ophyd-async `mock` devices and a `RunEngine`).
- **pdfstream keeps working unchanged.** The start-document keys it reads stay the same: `sc_dk_field_uid`,
  `dark_frame`, `calibration_md`, `analysis_stage` and `bt_wavelength`.

The baseline is xpdAcq **1.1.4**, the version locked by xpd-profile-collection `qserver`. `main` pins 1.0.0.
Steps 3.1 to 3.5 are the same PRs as step 3 of the transition plan in NSLS2/xpd-tools#50
(`docs/transition/03-replace-xpdacq.md`). This plan adds what step 3 leaves out.

## What XPD uses, and what replaces it

Measured on 2026-11-18 against xpd-tools `autonomous` (beb88dd) and xpd-profile-collection `qserver` (141f81f).
13 of xpdAcq's 27 features are used.

| xpdAcq feature (what XPD calls) | Replacement in xpd-tools | PR |
|---|---|---|
| Settings in `glbl`: `frame_acq_time`, `dk_window`, `shutter_control`, `auto_load_calib`, the pdfstream proxy addresses (set today by `set_glbl_qserver`) | One YAML file per endstation, read at startup | 3.0 |
| Darks: `periodic_dark`, `take_dark`, `_validate_dark`, `_inject_qualified_dark_frame_uid`, `glbl['_dark_dict_list']` | A `take_dark` plan and a dark preprocessor with an in-memory dark cache | 3.1 |
| Calibration: `_inject_calibration_md`, `_auto_load_calibration_file`, `load_calibration_md` | A calibration preprocessor and loader | 3.2 |
| Sample metadata from `bt` | A plan argument | 3.3 |
| `_inject_analysis_stage` | A preprocessor writing `analysis_stage: "raw"` | 3.4 |
| Exposure: `_configure_area_det`, `configure_area_det`, `xpd_configuration['area_det']` | ophyd-async `prepare` with `TriggerInfo`, with the detector passed as a plan argument | 3.5 |
| Shutter: `open_shutter_stub`, `close_shutter_stub`, `shutter_step` | Plan stubs that open the fast shutter only while exposing | 3.6 |
| Plans: `ct`, `_nstep` | `ct` and a step plan built on 3.1 to 3.6 | 3.7 |
| Beamtime: `_start_beamtime`, `_end_beamtime`, `start_xpdacq`, the `bt` object | `start_beamtime` (sync-experiment, already in xpd-tools) plus `RE.md["bt_wavelength"]` | 3.8 |
| `xrun` | The plain `RE`, since the preprocessors now live in the plans | 3.9 |

## The PRs

Each PR adds one thing and its tests. Nothing in the profile changes until 3.9.

| PR | Change | Shown working by |
|---|---|---|
| 3.0 | `src/xpd_tools/acquisition_configurations/<endstation>.yaml`, following the `panda_configurations/` pattern, plus a loader returning a typed settings object. Keys and today's `qserver` values: `frame_acq_time: 0.5` (s), `dark_window: 1000` (min), `shutter_control: true`, `auto_load_calibration: true`, `calibration_file`, `inbound_proxy_address`, `outbound_proxy_address`. | Unit test that loads each packaged file |
| 3.1 | `take_dark(detector, shutter)`: closes the shutter and counts in its own run with `dark_frame: True`. A dark preprocessor takes a new dark before a light run when the cache has no dark with the same frame time and total exposure taken inside `dark_window`. Otherwise it reuses the cached one. Every light run gets `sc_dk_field_uid`. The cache replaces `glbl['_dark_dict_list']`. | Mock test: the first run takes a dark, a second run inside the window reuses it, and a new exposure takes a new dark |
| 3.2 | `load_calibration(path)` and a preprocessor that writes `calibration_md` from `calibration_file`. A `calibration_md` the user passes is kept, not overwritten. That is the fix in xpdAcq `master` (PR #589), which 1.1.4 lacks. No file means no key, as in xpdAcq. | Mock test: with a file, the start document carries it; a user-passed one wins; with no file, the key is absent |
| 3.3 | Sample metadata as a plan argument (`sample_info`) | Mock test |
| 3.4 | `analysis_stage: "raw"` in each start document | Mock test |
| 3.5 | Exposure setup through `prepare`: frames = total exposure ÷ `frame_acq_time`. An exposure shorter than one frame is refused, as xpdAcq does. | Mock test: the detector is prepared with the requested exposure and frame count |
| 3.6 | `open_shutter` / `close_shutter` stubs and a per-step stub that opens only around `trigger_and_read`. All are no-ops when `shutter_control` is false. They use the fast-shutter device from transition step 1.1. | Mock test: shutter states across a two-point scan |
| 3.7 | `ct(detector, exposure)` and a step plan (replacing `_nstep` in the gas plan). Both are wrapped with 3.1, 3.2, 3.4 and 3.6, so a user call needs nothing else. | Mock test: one `ct` produces a dark run then a light run with every key above |
| 3.8 | `start_beamtime(proposal_id, wavelength=None)` writes `RE.md["bt_wavelength"]` when given; the rest comes from sync-experiment. No `end_beamtime`: switching to the next proposal ends the last one. | Mock test with a fake sync-experiment |
| 3.9 | Profile switch, done in xpd-profile-collection: replace `xrun(...)` with `RE(...)` and the `glbl`/`bt` calls with the above, then remove `xpdacq` from `pixi.toml`. One PR per endstation profile that uses it. | Beamline test at XPD before merge |

Dependencies: 3.1 needs 3.0, 3.5 and 3.6. 3.2 and 3.5 need 3.0. 3.7 needs 3.1 to 3.6. 3.9 needs everything.
Transition steps 1.1 (shutter) and 1.2 (detector) come first.

## Beamtime: sync-experiment instead of `bt`

`qserver`'s `pass_start_beamtime` already calls `sync_experiment(proposal, "XPD")`. It then builds an xpdAcq `bt`
on top and copies `cycle` and `data_session` into it. sync-experiment writes `data_session`, `username`,
`start_datetime`, `tiled_access_tags`, `cycle` and `proposal` (`proposal_id`, `title`, `type`, `pi_name`) to
`RE.md`. It keeps any other keys there.

Of the `bt_*` keys xpdAcq adds to every run, only `bt_wavelength` is read downstream: pdfstream's server configs use
`wavelength_key = bt_wavelength`. So:

| xpdAcq | After |
|---|---|
| `bt_piLast` | `proposal.pi_name` (sync-experiment) |
| `bt_uid` | `data_session` (sync-experiment) |
| `bt_wavelength` | kept, as `RE.md["bt_wavelength"]` (3.8) |
| `bt_safN`, `bt_experimenters` | dropped, since nothing reads them |
| `xpdUser/` workspace, archive at end of beamtime | dropped; data lives in the proposal directories |
| `bt.scanplans` (`ct_1`, `ct_60`) | `ct(detector, 1)`, `ct(detector, 60)` |

## Not ported

These are unused by `autonomous` and `qserver`, so they are not ported:

- the 1.1 API (`UserInterface`, `XrayBasicPlans`, `MultiDistPlans`, `TwoDetectors`, `BeamtimeHelper`, the
  calibration and mask preprocessors);
- `run_calibration`, the robot plans, `Tramp`, `Tlist` and `tseries`;
- `Sample` and `ScanPlan` objects, user-file import and export, and the beam-dump suspender (beam suspenders are
  being added to xpd-tools separately, NSLS2/xpd-tools#57);
- the `glbl` setup keys (`base`, `home`, `blconfig_path`, `beamline_id`, `group`, `facility`). `mask_kwargs` is
  only printed.
- `xpdacq.simulation` devices, replaced by ophyd-async `mock` devices in the tests.

## Questions

- **3.1:** should the dark cache survive a restart? `qserver` can dump and reload `_dark_dict_list` as JSON, and
  `pixi_2026C2` adds an export helper. If XPD relies on that, 3.1 adds `save` and `load` for the cache.
- **3.2:** is the calibration file still the pyFAI-derived YAML xpdAcq writes, or should 3.2 read a `.poni`?
- **3.3 and 3.9:** is the SAF sample spreadsheet still used? `import_sample_info` is imported on `qserver`, but
  nothing calls it.
- **3.9:** which profile branch runs at each XPD workstation today? ws3 runs `pixi_2026C2`, which has 23 commits of
  operator work that are not on `qserver`.
