# Retiring xpdAcq at XPD

This plan moves what XPD uses from xpdAcq into xpd-tools, one small PR at a time. When it is done,
xpd-profile-collection drops `xpdacq` from its environment.

## Rules

- **xpd-tools never imports `xpdacq` or `xpdconf`.** The behaviour is ported as plain bluesky plans,
  preprocessors and devices.
- **Settings in code and plans, state in Redis.** `glbl`'s settings become signals on an `AcquisitionSettings`
  device, changed with `bps.mv` or a preset plan, with named presets in code and no settings files. State that must
  outlive a session (the dark list, the current calibration) goes in xpd-tools' own DB on the endstation's Redis
  (see [Redis](#redis-the-xpd-tools-db)), not in `xpdUser/` files. Host settings (the pdfstream proxy addresses,
  Redis coordinates) stay in the files ansible deploys.
- **Port only what XPD uses today.** That means what xpd-tools `autonomous` and xpd-profile-collection `qserver`
  use. The `main` and `pixi_2026C2` profile branches use a subset of `qserver`. The rest of xpdAcq is not ported.
- **Small PRs**, each shown working with mock tests (ophyd-async `mock` devices and a `RunEngine`).
- **pdfstream keeps working unchanged.** The start-document keys it reads stay the same. Its deployed config
  (ansible `roles/pdfstream_servers`) reads `dark_frame`, `is_calibration`, `sc_dk_field_uid`, `calibration_md`,
  `sample_name`, `sample_composition`, `bkgd_sample_name`, `detector` and `bt_wavelength`, and writes its output
  under `{cycle}/{data_session}/{bt_piLast}_{bt_safN}_{bt_uid}/{sample_name}`. The Tiled-streaming pdfstream
  (NSLS-II-PDF/pdfstream#6) reads the same keys, so they also hold for the planned `pdfstream_ws` service.

The baseline is xpdAcq **1.1.4**, the version locked by xpd-profile-collection `qserver`. `main` pins 1.0.0.
Steps 3.1 to 3.5 are the same PRs as step 3 of the transition plan in NSLS2/xpd-tools#50
(`docs/transition/03-replace-xpdacq.md`). This plan adds what step 3 leaves out.

## What XPD uses, and what replaces it

Measured on 2026-11-18 against xpd-tools `autonomous` (beb88dd) and xpd-profile-collection `qserver` (141f81f).
13 of xpdAcq's 27 features are used there. TJ's beamline notes (comments on this PR, 2026-11-18) add two more:
`run_calibration` and the sample spreadsheet (`import_sample_info`).

| xpdAcq feature (what XPD calls) | Replacement in xpd-tools | PR |
|---|---|---|
| Settings in `glbl`: `frame_acq_time`, `dk_window`, `shutter_control`, `auto_load_calib` (set today by `set_glbl_qserver`) | An `AcquisitionSettings` device with presets in code (3.0a); its values kept in Redis (3.0b) | 3.0a, 3.0b |
| Darks: `periodic_dark`, `take_dark`, `_validate_dark`, `_inject_qualified_dark_frame_uid`, `glbl['_dark_dict_list']` | A `take_dark` plan and a dark preprocessor, with the dark list in Redis | 3.1 |
| Calibration: `run_calibration`, `_inject_calibration_md`, `_auto_load_calibration_file`, `load_calibration_md` | A calibration run, a `.poni` loader and a calibration preprocessor, with the current calibration in Redis | 3.2 |
| Sample metadata from `bt`, loaded from the SAF spreadsheet by `import_sample_info` | A spreadsheet loader and a plan argument | 3.3 |
| `_inject_analysis_stage`, `_inject_filter_positions`, `_inject_xpdacq_md_version` | Preprocessors writing `analysis_stage: "raw"`, the filter positions and an xpd-tools version key | 3.4 |
| Exposure: `_configure_area_det`, `configure_area_det`, `xpd_configuration['area_det']` | ophyd-async `prepare` with `TriggerInfo` for `pe1c` and the Pilatus, with the detector passed as a plan argument | 3.5 |
| Shutter: `open_shutter_stub`, `close_shutter_stub`, `shutter_step` | Plan stubs that open the fast shutter only while exposing | 3.6 |
| Plans: `ct`, `_nstep` | `ct` and a step plan built on 3.1 to 3.6 | 3.7 |
| Beamtime: `_start_beamtime`, `_end_beamtime`, `start_xpdacq`, the `bt` object | `start_beamtime` (sync-experiment, already in xpd-tools) plus the `bt_*` keys pdfstream reads | 3.8 |
| `xrun`: the preprocessors, sample lookup, its own subscriptions | `install(RE)` and a thin `xrun` callable (see [below](#how-xrun-is-replaced)) | 3.9 |

## How `xrun` is replaced

`xrun` is xpdAcq's RunEngine. Hui's scripts call `xrun(sample, plan)` 15 times (`92_Hui_userplans.py`,
`93_Hui_userscripts.py`), and TJ's workflow runs `xrun(0, 0)`. xpd-tools splits its work three ways:

- **On the RunEngine, for every run:** the metadata preprocessors (calibration 3.2, sample 3.3, analysis stage and
  the rest 3.4). `install(RE)` adds them once, the same way in bsui and in the Queue Server worker, so a plain
  `RE(plan)` carries them.
- **Inside the plans that drive a detector:** darks (3.1) and the shutter (3.6), through `ct` and the step plan
  (3.7). A plan without a detector takes no dark.
- **A thin `xrun(sample, plan, **md)`:** looks up the sample in the loaded spreadsheet (by index or name) and calls
  `RE`. It has no logic of its own. Existing scripts keep working, and it goes once they call `RE` directly.

`install(RE)` also attaches the subscriptions in one place for both engines: Tiled writing, and the zmq publisher
that feeds the pdfstream servers. Today bsui attaches the publisher to `xrun` only. It stays until a
Tiled-streaming pdfstream replaces the servers, since pdfstream#6 still takes start, descriptor and stop documents
over ZMQ.

Not carried over from `xrun`: `verify_write`, `ask_before_run`, the robot plans and commands, plan selection by
index, and xrun's own BestEffortCallback settings (see [Questions](#questions)).

## The PRs

Each PR adds one thing and its tests. Nothing in the profile changes until 3.10.

| PR | Change | Shown working by |
|---|---|---|
| 3.0a | `AcquisitionSettings`: an ophyd-async device whose soft signals `frame_acq_time` (s), `dark_window` (min), `shutter_control` and `auto_load_calibration` replace those `glbl` keys. Plans read them with `bps.rd`, users change them with `bps.mv`, and a run that reads the device records them. `AcquisitionConfiguration` names presets in code (`xpd`: `qserver`'s `set_glbl_qserver` defaults 0.5 s, 1000 min, true, true), applied by `use_acquisition_configuration(settings, "xpd")`. The plan takes the preset name as a string, because the Queue Server refuses a plan whose default is an Enum member. | Mock test: the device starts with the preset, `mv` changes a value, the plan restores the preset and refuses an unknown name, and a run records the values with units |
| 3.0b | `xpd_tools.state`: a Redis client for the xpd-tools DB, from the `[redis.<endstation>.xpdtools]` section of `/etc/bluesky/bluesky_redis_config.toml` (nslsii's `open_redis_client` takes a DB number but does not read that file). `AcquisitionSettings` keeps its values in that DB (through `soft_signal_rw`'s getter and setter), so bsui and the Queue Server share them and they survive a restart. | The client against `fakeredis`; two devices on one fake Redis see each other's changes |
| 3.1 | `take_dark(detector, shutter)`: closes the shutter and counts in its own run with `dark_frame: True`. A dark preprocessor takes a new dark before a light run when the cache has no dark with the same frame time and total exposure taken inside `dark_window`. Otherwise it reuses the cached one. Every light run gets `sc_dk_field_uid`. The dark list replaces `glbl['_dark_dict_list']`. It is kept in Redis, one key per dark (detector, frame time, exposure), each expiring after `dark_window`. So darks survive a restart, are shared by bsui and the Queue Server worker, and clear themselves. | Mock test with `fakeredis`: the first run takes a dark, a second run inside the window reuses it, and a new exposure or an expired key takes a new dark |
| 3.2 | `calibration_run(detector, calibrant, exposure)` takes the standard's image in a run marked `is_calibration: True`, as `run_calibration` does; fitting it in the pyFAI GUI stays a manual step. `load_calibration(path)` reads the resulting pyFAI `.poni` (TJ saves it as `xpdUser/config_base/xpdAcq_calib_info.poni`) and stores it as the detector's current calibration in Redis (when `auto_load_calibration` is on, the profile loads one at startup; Hui's scripts switch between `config_base/xrd.poni` and `config_base/pdf.poni`, so 3.2 names calibrations rather than keeping one file setting). A preprocessor writes the current one to `calibration_md`. A `calibration_md` the user passes is kept, not overwritten. That is the fix in xpdAcq `master` (PR #589), which 1.1.4 lacks. No calibration means no key, as in xpdAcq. | Mock test with `fakeredis`: a calibration run carries `is_calibration`; with a calibration, the start document carries it; a user-passed one wins; with no file, the key is absent |
| 3.3 | Sample metadata: a loader for the SAF sample spreadsheet (`<saf>_sample.xlsx`, replacing `import_sample_info`) that keeps the keys pdfstream reads (`sample_name`, `sample_composition`, `bkgd_sample_name`), and a plan argument (`sample_info`) | Mock test: a spreadsheet row lands in the start document with those keys |
| 3.4 | `analysis_stage: "raw"`, the filter positions and an xpd-tools version key in each start document | Mock test |
| 3.5 | Exposure setup through `prepare`, for `pe1c` and the Pilatus, as `qserver`'s `_pre_plan` does. PerkinElmer: frames = total exposure ÷ `frame_acq_time`, rounded up (1.1 s at 0.5 s per frame is 3 frames); an exposure shorter than one frame is refused, as xpdAcq does. Pilatus: one frame of the whole exposure (`_pre_plan` sets its `acquire_time` and `acquire_period` to the exposure). | Mock test: each detector is prepared with the requested exposure and frame count, including a PerkinElmer exposure that is not a whole number of frames |
| 3.6 | `open_shutter` / `close_shutter` stubs and a per-step stub that opens only around `trigger_and_read`. All are no-ops when `shutter_control` is false. They use the fast-shutter device from transition step 1.1. | Mock test: shutter states across a two-point scan |
| 3.7 | `ct(detector, exposure)` and a step plan. The gas plan's `_nstep` is not a plan: it turns a range into a point count and step size that are passed to `bp.scan`. 3.7 ports that helper and adds the step plan built on it. Both plans are wrapped with 3.1 and 3.6; the metadata comes from the RunEngine (3.9). | Mock test: one `ct` produces a dark run then a light run with every key above; the helper gives the same points as `_nstep` |
| 3.8 | `start_beamtime(proposal_id, saf, wavelength=None)` writes `RE.md["bt_wavelength"]` when given and the `bt_*` keys pdfstream's output folder needs (see [Beamtime](#beamtime-sync-experiment-instead-of-bt)); the rest comes from sync-experiment. No `end_beamtime`: switching to the next proposal ends the last one. | Mock test with a fake sync-experiment: every `bt_*` key changes with the proposal |
| 3.9 | `install(RE, settings)` adds the preprocessors of 3.2 to 3.4 to `RE.preprocessors` and attaches the subscriptions (Tiled, the zmq publisher). `xrun(sample, plan, **md)` is a thin callable over `RE`. | Mock test: `RE(ct(...))` and `xrun(0, ct(...))` give the same start documents, and a plain `RE(count(...))` carries the metadata |
| 3.10 | Profile switch, done in xpd-profile-collection: call `install(RE)`, use xpd-tools' `xrun` in place of xpdAcq's, replace the `glbl`/`bt` calls with the above, point `RE.md` at the endstation's Redis over TLS (`qserver` still names the old `info.xpd.nsls2.bnl.gov:6379`), then remove `xpdacq` from `pixi.toml`. One PR per endstation profile that uses it. | Beamline test at XPD before merge |

Dependencies: 3.0b needs 3.0a. 3.1 needs 3.0a, 3.0b, 3.5 and 3.6. 3.2 needs 3.0a and 3.0b. 3.5 needs 3.0a. 3.7 needs
3.1 to 3.6. 3.9 needs 3.2 to 3.4 and 3.8. 3.10 needs everything. Transition steps 1.1 (shutter) and 1.2 (detector)
come first. The tests use `fakeredis`, so 3.0a
to 3.9 can merge before the ansible change that adds the Redis DB. That change has to be in place before 3.10.

## Beamtime: sync-experiment instead of `bt`

`qserver`'s `pass_start_beamtime` already calls `sync_experiment(proposal, "XPD")`. It then builds an xpdAcq `bt`
on top and copies `cycle` and `data_session` into it. sync-experiment writes `data_session`, `username`,
`start_datetime`, `tiled_access_tags`, `cycle` and `proposal` (`proposal_id`, `title`, `type`, `pi_name`) to
`RE.md`. It keeps any other keys there.

pdfstream reads four of the `bt_*` keys xpdAcq adds to every run: `bt_wavelength` (`wavelength_key`), and
`bt_piLast`, `bt_safN` and `bt_uid`, which name its output folder (`directory_template`). So those four stay, and
3.8 sets them whenever the proposal changes. Stale values are the failure to design out: TJ reports that `bt`
settings left from an earlier proposal once sent pdfstream's output to the wrong folder on ws3.

| xpdAcq | After |
|---|---|
| `bt_piLast` | kept; set by `start_beamtime` from `proposal.pi_name` (sync-experiment) |
| `bt_safN` | kept; passed to `start_beamtime`. A proposal can have more than one approved SAF, so it is not derived |
| `bt_uid` | kept; a new value from each `start_beamtime` |
| `bt_wavelength` | kept, as `RE.md["bt_wavelength"]` (3.8) |
| `bt_experimenters` | dropped, since nothing reads it |
| `xpdUser/` workspace, archive at end of beamtime | dropped; data lives in the proposal directories |
| `bt.scanplans` (`ct_1`, `ct_60`) | `ct(detector, 1)`, `ct(detector, 60)` |

## Redis: the xpd-tools DB

Each XPD endstation already has a managed Redis, deployed by ansible: `xf28id2-xpd-redis1` (C-hutch) and
`xf28id2-xpdd-redis1` (D-hutch). It is TLS on port 6380 with 16 DBs. DB 0 holds `RE.md` (written by
sync-experiment) and DB 15 holds API keys. xpd-tools gets a DB of its own, so its keys never mix with `RE.md`:

- **Ansible:** one entry, `xpdtools: <N>`, in the `bluesky_redis_config` role's `bluesky_redis_config_dbs` for
  xf28id2. Workstations then get a `[redis.<endstation>.xpdtools]` section in
  `/etc/bluesky/bluesky_redis_config.toml`. `nslsii`'s `open_redis_client(redis_db=N)` already supports it.
- **What goes in:** the acquisition settings (3.0b), the dark list (3.1) and the current calibration per detector
  (3.2). Nothing else for now.
- **Limits to design for:**
  - dark keys get a TTL (`dark_window`), so they clear themselves. The current calibration gets none: at the 8 GB
    limit Redis evicts only keys that have a TTL, and an expired calibration would silently drop `calibration_md`
    from every later run;
  - the server saves snapshots only, so a crash can lose the last minutes, which is harmless here (a lost dark is
    retaken, and each run's start document already carries its calibration);
  - there is one password per host, so the DB is a namespace, not access control.

## Not ported

These are unused by `autonomous` and `qserver`, so they are not ported:

- the 1.1 API (`UserInterface`, `XrayBasicPlans`, `MultiDistPlans`, `TwoDetectors`, `BeamtimeHelper`, the
  calibration and mask preprocessors);
- the robot plans, `Tramp`, `Tlist` and `tseries`;
- the `xrun` features listed under [How `xrun` is replaced](#how-xrun-is-replaced);
- `Sample` and `ScanPlan` objects, user-file export, and the beam-dump suspender (beam suspenders are being added
  to xpd-tools separately, NSLS2/xpd-tools#57);
- the `glbl` setup keys (`base`, `home`, `blconfig_path`, `beamline_id`, `group`, `facility`). `mask_kwargs` is
  only printed.
- `xpdacq.simulation` devices, replaced by ophyd-async `mock` devices in the tests.

## Questions

- **Redis:** which DB number? DBs 1 to 14 should first be checked for existing keys (`INFO keyspace` on each XPD
  Redis). Does DSSI agree to the extra DB?
- **3.1:** `qserver`'s JSON dump/reload of `_dark_dict_list` and `pixi_2026C2`'s export helper are replaced by the
  Redis dark list. Does anything still need a dark list as a file?
- **xrun:** does anyone still use `verify_write`, `ask_before_run`, the robot, or plan selection by index?
- **3.9:** where does `install(RE)` get the pdfstream proxy address? Ansible's `xpdacq_config` sets it per host
  (`-qs` hosts publish to the `ipc-in-tcp-out` proxy; workstations to `ipc-in-ipc-out`), in a file that goes
  with xpdAcq.
- **3.3:** TJ notes that background metadata is missing. The spreadsheet's background column fills
  `bkgd_sample_name`, which pdfstream reads. Is that column left empty, or is other background metadata needed?
- **3.10:** which profile branch runs at each XPD workstation today? ws3 runs `pixi_2026C2`, which has 23 commits of
  operator work that are not on `qserver`.
