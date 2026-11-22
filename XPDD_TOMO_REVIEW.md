# XPD-D tomography: review of the `xpdd-tomo` branch

Review of [`xpdd-tomo`](https://github.com/NSLS2/xpd-tools/tree/xpdd-tomo) at
[`1175d91`](https://github.com/NSLS2/xpd-tools/commit/1175d91ea1c8170fb2a5c864aec5be43a73e22a8) (Kyle's port of hextools'
tomography plans, 2026-11-09/10), for the XPD-D experiments starting **2026-11-28** (code freeze **2026-11-20**).
XPD-D work continues on `xpdd-tomo` and the LDRD/autonomous work on `autonomous`; both merge into `main` later.

Comment on a line of this file to discuss a finding; tick it in the table when it is fixed.

**Re-checked 2026-11-22 against [`0889d26`](https://github.com/NSLS2/xpd-tools/commit/0889d26fd55c616ab8af55faf54dfd91c1bb25ab)** (#63 merged 2026-11-20), after #53, #55, #57, #58, #60, #62 and #63 merged
into `xpdd-tomo`. The findings below are as found at `1175d91`; the table and the **Update 2026-11-22** notes give their
state now. Read from the code at `0889d26`; nothing was rerun on hardware.

## What was checked

- Run locally with the branch's locked pixi `dev` environment: unit tests **206 passed**, pyright **0 errors**,
  pre-commit clean.
- Read against hextools' `tomography/` (the template). The port keeps hextools' fixes: rotation speed restored after a
  fly scan, a few missed frames tolerated, the Proc filter type set, data in `primary` with the PandA position in
  `rotation_angle`, darks and flats in `dark` / `flat`. It adds per-detector exposure times (Pilatus4, IRIS
  `iris1`, Xspress3), a configurable sample-out motor for flats, and relative step axes.
- Everything below was **read from the code**, not run on hardware.

## Summary

| # | Finding | Kind | Status at `0889d26` (2026-11-22) |
|---|---|---|---|
| B1 | Shutters are mock devices in the profiles | bug | **fixed in `xpdd.py`** (#63: real PVs, mock only in CI); `devd.py` still mocks them on purpose until a hardware test |
| B2 | Dark/flat link survives a proposal switch | bug | open |
| B3 | `profiles/devd.py` writes to Tiled unbuffered, with no backup | bug | open (`devd.py` only) |
| B4 | `single_axis_flyscan` has no safe exit and other data names | bug | open |
| G1 | No CI on `xpdd-tomo` | gap | **fixed** for PRs into `xpdd-tomo` (#60); pushes to it and `autonomous` are not checked |
| G2 | nslsii pinned to a fork branch | gap | fork pin gone (#63: `NSLS2/nslsii@main`); still a moving git ref, not a release |
| G3 | Tiled spec names shared with hextools, different schema | gap | open, and wider: `TomoFlyscan` v1 now names a third layout (#53) |
| G4 | Two profiles: which one runs at XPD-D? | gap | open; `devd.py` has diverged further (Dexela, `_fly` twins, mock shutters) |
| G5 | `xpdd-tomo` also changes the LDRD profile and pins | gap | **closed**: `ldrd.py` is the same as `main`, and the fork pin is gone |
| G6 | Sample-out motor default `amTopX` vs `amBaseX` | gap | open; default still `amTopX` |
| G7 | No real shutter device | gap | **done** (#63: `FastShutter`) |
| G8 | Multi-exposure not yet run on hardware or from the locked env | gap | per-detector rates **implemented** (#53); not yet known to have run on hardware |

Suggested order now: **B2**, a `tomo_dark_flat` with `use_shutter=True` at XPD-D, **G6**, then **G4/B3, G3, B4, G2**
and the follow-ons below before merging to `main`.

### Follow-ons found after `1175d91`

From the reviews of #58 and #63, checked again at `0889d26`:

- **Prosilica cannot be used in tomography plans.** #63 added the Dexela (`TomoDetector`, PandA TTL 4), but only
  `devd.py` creates `dexela1`; the Prosilica is in neither `TomoDetector` nor `DEFAULT_TTL_OUT`.
- **`build_detector_config_noninteractive` fails in `devd.py`.** `discover_detectors` returns every detector variable,
  both each step-scan wrapper and its `_fly` twin (seven in `devd.py`), and a fly scan takes at most three
  ([`builder.py:329`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/tomo/builder.py#L329)). Prefer the `_fly` twin, or pass `detectors=`.
- **`ttl_out=0` hides the real error.** The same function still passes `default_ttl_out(det) or 0`
  ([`builder.py:623`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/tomo/builder.py#L623)), so an unwired detector fails with "not one of the wired
  outputs" instead of `DetectorConfig`'s clearer "has no default TTL output".
- **Shutters:** which photon shutter gates XPD (`{PSh}` or `:2{PSh:2}`), the fast shutter's open/closed angles
  (−20/+20 in #63, 60/0 on the screen), and `fe_shutter` built as `Shutter` rather than `PPSShutter` (which adds the
  interlock acknowledge). See [`SHUTTER_SYSTEM_ARCHITECTURE.md`](SHUTTER_SYSTEM_ARCHITECTURE.md).

## Bugs

### B1. Shutters are mock devices in the profiles

[`profiles/xpdd.py:237-240`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/xpdd.py#L237-L240) (same in
[`devd.py:236-239`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/devd.py#L236-L239)) creates `fe_shutter` and `photon_shutter` inside
`init_devices(mock=True)` on `TEST` PVs ("TODO(XPDD): Get shutter PVs for the tomo plans"). A mock shutter starts
"Not Open" ([`shutter.py:41`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/shutter.py#L41)), and `ensure_shutter_open` without actuation raises
([`shutter.py:238`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/shutter.py#L238)).

- With the default `use_shutter=True`, `tomo_flyscan` and `tomo_dark_flat` stop at the front-end shutter check.
- With `use_shutter=False`, `tomo_dark_flat` takes its `dark` stream with the beam on.

**Proposed fix:** give the profile the real shutter (G7), or, until then, make the plans refuse to take darks without
one rather than record light frames as darks.

**Update 2026-11-22: fixed in `xpdd.py`.** #63 builds `fe_shutter` (`XF:28ID-PPS{Sh:FE}`), `photon_shutter`
(`XF:28IDA-PPS{PSh}`) and `fast_shutter` (`FastShutter`) with `init_devices(mock=XPDTOOLS_RUNNING_IN_CI)`
([`xpdd.py:246`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/profiles/xpdd.py#L246)). `devd.py` still builds them in `init_devices(mock=True)`
([`devd.py:294`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/profiles/devd.py#L294)), on purpose until a real-hardware test (Kyle). In `devd.py` a
dark is therefore still not protected by a real shutter. With `use_shutter=False` the plans now say so
(`SHUTTER_BYPASS_HINT`: "darks are only as dark as the beam is").

### B2. Dark/flat link survives a proposal switch

[`LastDarkFlatUIDTracker`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/utils.py#L119) writes `last_dark_flat_uid` into `RE.md`, which is
the shared Redis store ([`xpdd.py:146`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/xpdd.py#L146)).
[`clear_last_dark_flat`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/utils.py#L155) exists, but
[`start_beamtime`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/utils.py#L74) does not call it. The first fly scans of a new proposal therefore
link to the previous proposal's darks and flats. HEX hit the same bug on 2026-10-21; it was fixed in
[hextools#105](https://github.com/NSLS2/hextools/pull/105) (merged 2026-10-23, `ae14996`). That fix is on the
reading side: hextools' tomography exporter uses a dark/flat run only if its `data_session` and `cycle` match the
scan's, and `reset_scan_id` clears the link. hextools' `switch_proposal` still does not clear it (dropped in review).

**Proposed fix:** clear the link in `start_beamtime`, with a test; and have whatever reads the link check it belongs to
the same proposal and cycle, as hextools' exporter does since #105. At XPD-D that reader is Mehmet's processing.

**Update 2026-11-22: still open.** The beamline prompt calls the profiles' own `start_beamtime`
([`xpdd.py:54`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/profiles/xpdd.py#L54), [`devd.py:57`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/profiles/devd.py#L57)), not the one in
`xpd_tools/utils.py`, and neither calls `clear_last_dark_flat`. The fix and its test must cover those entry points, or
the profiles must use one shared helper.

### B3. `profiles/devd.py` writes to Tiled unbuffered, with no backup

[`devd.py:119`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/devd.py#L119) is the only real difference from `xpdd.py`: its production
`TiledWriter` has no `BufferingWrapper` and no backup directory
([`xpdd.py:119-120`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/xpdd.py#L119-L120) has both). A Tiled outage during a scan loses
documents. It looks like a workaround for the backup directory under `TILED_BACKUP_ROOT`, which the `xf28idd` account
cannot write (checked on ws8 2026-11-09).

**Proposed fix:** add an ACL entry for `xf28idd@bnl.gov` on that directory (as HEX has for `xf27id1`), then drop
`devd.py` (G4).

**Update 2026-11-22: still open** in `devd.py` ([line 123](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/profiles/devd.py#L123)); `xpdd.py` buffers
and backs up.

### B4. `single_axis_flyscan` has no safe exit and other data names

[`plans/plans.py:171`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L171) has no `finalize_wrapper`, leaves the stage at its fly
speed, and writes stream `tomo` ([line 178](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L178)) with position `Angle`
([line 58](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L58)). `tomo_flyscan` uses `primary` and `rotation_angle`, as HEX does.

**Proposed fix:** route it through `tomo_flyscan`, or give it the same cleanup and names.

**Update 2026-11-22: still open.** #53 changed only how it routes triggers (each detector from its default TTL
output); it still writes stream `tomo` with `Angle` ([`plans.py:173`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/plans/plans.py#L173)).

## Gaps

### G1. No CI on `xpdd-tomo`

[`ci.yml`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/.github/workflows/ci.yml#L8-L12) runs only on pushes and PRs to `main`, so neither `xpdd-tomo` nor
`autonomous` is checked. **Proposed:** add both branches to the triggers, or open a draft PR from each into `main`.

**Update 2026-11-22: fixed for PRs into `xpdd-tomo`** (#60 added it to the `pull_request` branches). Pushes straight
to `xpdd-tomo`, and anything on `autonomous`, are still not checked.

### G2. nslsii pinned to a fork branch

[`pixi.toml:21`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/pixi.toml#L21) and [`pyproject.toml:33`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/pyproject.toml#L33) take nslsii from
`jwlodek/nslsii@xspress3-fix-for-ophyd-async-main` (Xspress3; NSLS2/nslsii#317 still open). The lock pins the commit,
so the environment is reproducible, but the branch cannot merge to `main` on a fork pin. **Proposed:** move to an nslsii
release once #317 is in.

**Update 2026-11-22.** #63 moved the pin to `NSLS2/nslsii@main` ([`pixi.toml:22`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/pixi.toml#L22)): no fork, but
still a moving git ref. The lock pins the commit; merging to `main` still wants a released nslsii.

### G3. Tiled spec names shared with hextools, different schema

[`specs.py:139`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/specs.py#L139) and [`:207`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/specs.py#L207) define
`TomoDarkFlat` v1 and `TomoFlyscan` v1, hextools' names and versions, but here `exposure_time` is a per-detector
dict and flats use `sample_out_motor` / `sample_out_offset` instead of `x_offset`. A run labelled `TomoFlyscan` v1
would mean two layouts depending on the beamline. **Proposed:** agree one layout with hextools (a v2), or use XPD-D's
own spec names.

**Update 2026-11-22: open, and wider.** Since #53 a fly scan writes one stream per detector and a `rotation` stream
(`rotation_angle` on every PandA base pulse), with each detector's settings in `detector_configs`
([`specs.py:221`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/specs.py#L221)). It is still labelled `TomoFlyscan` v1, the name hextools uses for
`primary` plus `rotation_angle`, and the name `1175d91` used for its own layout.

### G4. Two profiles: which one runs at XPD-D?

`profiles/xpdd.py` and `profiles/devd.py` are the same apart from B3. **Question for Kyle:** which one does the
beamline start? Keep one.

**Update 2026-11-22: open.** The two have diverged: `devd.py` adds the Dexela, `_fly` twins of each imaging detector
and mock shutters, and keeps the unbuffered writer (B3).

### G5. `xpdd-tomo` also changes the LDRD profile and pins

[`profiles/ldrd.py:88`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/ldrd.py#L88) changes the data path, and the nslsii fork pin applies
to the LDRD environment too. `autonomous` will meet both when the branches merge. **Proposed:** keep LDRD changes on
`autonomous`, or tell its owners.

**Update 2026-11-22: closed.** `ldrd.py` on `xpdd-tomo` is the same as on `main`, and the nslsii fork pin is gone (G2).

### G6. Sample-out motor default `amTopX` vs `amBaseX`

[`tomo/flyscans.py:65`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/flyscans.py#L65) defaults to `amTopX` (`Stack-Ax:Xfine`, "Top X"),
while Q3 in [`XPDD_DEVICE_MAP_PROPOSAL.md`](XPDD_DEVICE_MAP_PROPOSAL.md) proposed `amBaseX`. **Question for
Mehmet:** which motor takes the sample out of the beam, and does its travel cover the offset (default 2.0)?

**Update 2026-11-22: open.** The default is still `amTopX` ([`flyscans.py:69`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/tomo/flyscans.py#L69));
the profile defines both `amBaseX` and `amTopX`.

### G7. No real shutter device

Mehmet's device list has the fast shutter as a motor, `XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr`. **Question for Kyle and
Mehmet:** is that the shutter the plans should close for darks, and how do open and closed map to its positions?
Needed for B1.

**Update 2026-11-22: done.** #63 added `FastShutter` ([`shutter.py:237`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/shutter.py#L237)): it moves
the motor `XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr` (open −20, closed +20 by default) and reads the switches
`XF:28IDC-ES:1{Sh:Exp}Sw:Opn1-Sts` / `Sw:Cls1-Sts`; its status is unknown unless exactly one switch is made. The plans
open and close only this shutter; the front-end and photon shutters are checked open, never moved. Open: the angles
(the screen says 60/0) and which photon shutter gates XPD.

### G8. Multi-exposure not yet run on hardware or from the locked env

The multi-exposure commits ([`a0215f8`](https://github.com/NSLS2/xpd-tools/commit/a0215f8),
[`1175d91`](https://github.com/NSLS2/xpd-tools/commit/1175d91)) came after the 2026-11-09 beamline run, and that run
used a personal `uv` environment, not the branch's locked pixi environment. **Proposed:** rerun the 11-09 scan (and one
with per-detector exposures) from the locked environment before 11-20.

**Update 2026-11-10.** Mehmet ran it on hardware: scan 12462 (`b7ce33ac`), `pilatus1` 1.0 s and `xspress3-1` 0.5 s,
`acquire_period` 1.002 s, 101 projections over 0 to 180 degrees, exit `success`. `primary` holds 101 frames of each
detector and 101 `rotation_angle` values on one shared `time` axis. Still open: whether it ran from the locked
environment. It also shows what the code does by design: one trigger train at the longest exposure, one frame per
detector per trigger, so the Xspress3 waits 0.5 s on every trigger. Mehmet wants each detector to run at its own rate
(e.g. Xspress3 0.1 s, Pilatus 0.01 s: about 1,000 and 10,000 frames over a 100 s rotation), with frames lined up
across detectors and with the angle. That is new scope rather than a bug here. Kyle is trying independent PandA
triggering and has asked Jakub to check the approach. Frames at two rates cannot share one `time` axis, so the run
layout, and with it the `TomoFlyscan` spec ([G3](#g3-tiled-spec-names-shared-with-hextools-different-schema)),
would change; see R3 in [`XPD_DATA_PROCESSING.md`](XPD_DATA_PROCESSING.md#proposed-requirements).

**Update 2026-11-22: implemented in #53** (Kyle, `497af97`). Each detector runs at its own rate: the PandA makes one
base pulse per image of the detector with the most projections, and feeds each other detector every D-th pulse through
a DIV block (`trigger_divisor`), with `images_to_average` per image ([`flyscans.py:307`](https://github.com/NSLS2/xpd-tools/blob/0889d26fd55c616ab8af55faf54dfd91c1bb25ab/src/xpd_tools/tomo/flyscans.py#L307)).
Each detector writes its own stream and the PandA writes `rotation_angle` to `rotation` on every base pulse, so a
detector's angles are `rotation_angle[::D]`. This is the layout change foreseen above (G3, R3). Not yet known: a run
of this layout at the beamline, and whether processing has agreed to it.
