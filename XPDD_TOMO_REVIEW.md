# XPD-D tomography: review of the `xpdd-tomo` branch

Review of [`xpdd-tomo`](https://github.com/NSLS2/xpd-tools/tree/xpdd-tomo) at
[`1175d91`](https://github.com/NSLS2/xpd-tools/commit/1175d91ea1c8170fb2a5c864aec5be43a73e22a8) (Kyle's port of hextools'
tomography plans, 2026-11-09/10), for the XPD-D experiments starting **2026-11-28** (code freeze **2026-11-20**).
XPD-D work continues on `xpdd-tomo` and the LDRD/autonomous work on `autonomous`; both merge into `main` later.

Comment on a line of this file to discuss a finding; tick it in the table when it is fixed.

## What was checked

- Run locally with the branch's locked pixi `dev` environment: unit tests **206 passed**, pyright **0 errors**,
  pre-commit clean.
- Read against hextools' `tomography/` (the template). The port keeps hextools' fixes: rotation speed restored after a
  fly scan, a few missed frames tolerated, the Proc filter type set, data in `primary` with the PandA position in
  `rotation_angle`, darks and flats in `dark` / `flat`. It adds per-detector exposure times (Pilatus4, Kinetix
  `iris1`, Xspress3), a configurable sample-out motor for flats, and relative step axes.
- Everything below was **read from the code**, not run on hardware.

## Summary

| # | Finding | Kind | Needed by | Status |
|---|---|---|---|---|
| B1 | Shutters are mock devices in the profiles | bug | 11-20 | open |
| B2 | Dark/flat link survives a proposal switch | bug | 11-20 | open |
| B3 | `profiles/devd.py` writes to Tiled unbuffered, with no backup | bug | merge | open |
| B4 | `single_axis_flyscan` has no safe exit and other data names | bug | merge | open |
| G1 | No CI on `xpdd-tomo` | gap | 11-20 | open |
| G2 | nslsii pinned to a fork branch | gap | merge | open |
| G3 | Tiled spec names shared with hextools, different schema | gap | merge | open |
| G4 | Two profiles: which one runs at XPD-D? | gap | merge | open |
| G5 | `xpdd-tomo` also changes the LDRD profile and pins | gap | merge | open |
| G6 | Sample-out motor default `amTopX` vs `amBaseX` | gap | 11-20 | open |
| G7 | No real shutter device | gap | 11-20 | open |
| G8 | Multi-exposure not yet run on hardware or from the locked env | gap | 11-20 | ran on hardware 11-10; env not known |

Suggested order: **B1 + G7, B2**, then **G1, G8, G6**, then **B3/G4, G3, B4, G2, G5** before merging to `main`.

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

### B2. Dark/flat link survives a proposal switch

[`LastDarkFlatUIDTracker`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/utils.py#L119) writes `last_dark_flat_uid` into `RE.md`, which is
the shared Redis store ([`xpdd.py:146`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/xpdd.py#L146)).
[`clear_last_dark_flat`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/utils.py#L155) exists, but
[`start_beamtime`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/utils.py#L74) does not call it. The first fly scans of a new proposal therefore
link to the previous proposal's darks and flats. HEX hit the same bug on 2026-10-21.

**Proposed fix:** clear the link in `start_beamtime`, with a test; and have whatever reads the link check it belongs to
the same proposal, as hextools' exporter does.

### B3. `profiles/devd.py` writes to Tiled unbuffered, with no backup

[`devd.py:119`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/devd.py#L119) is the only real difference from `xpdd.py`: its production
`TiledWriter` has no `BufferingWrapper` and no backup directory
([`xpdd.py:119-120`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/xpdd.py#L119-L120) has both). A Tiled outage during a scan loses
documents. It looks like a workaround for the backup directory under `TILED_BACKUP_ROOT`, which the `xf28idd` account
cannot write (checked on ws8 2026-11-09).

**Proposed fix:** add an ACL entry for `xf28idd@bnl.gov` on that directory (as HEX has for `xf27id1`), then drop
`devd.py` (G4).

### B4. `single_axis_flyscan` has no safe exit and other data names

[`plans/plans.py:171`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L171) has no `finalize_wrapper`, leaves the stage at its fly
speed, and writes stream `tomo` ([line 178](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L178)) with position `Angle`
([line 58](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/plans/plans.py#L58)). `tomo_flyscan` uses `primary` and `rotation_angle`, as HEX does.

**Proposed fix:** route it through `tomo_flyscan`, or give it the same cleanup and names.

## Gaps

### G1. No CI on `xpdd-tomo`

[`ci.yml`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/.github/workflows/ci.yml#L8-L12) runs only on pushes and PRs to `main`, so neither `xpdd-tomo` nor
`autonomous` is checked. **Proposed:** add both branches to the triggers, or open a draft PR from each into `main`.

### G2. nslsii pinned to a fork branch

[`pixi.toml:21`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/pixi.toml#L21) and [`pyproject.toml:33`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/pyproject.toml#L33) take nslsii from
`jwlodek/nslsii@xspress3-fix-for-ophyd-async-main` (Xspress3; NSLS2/nslsii#317 still open). The lock pins the commit,
so the environment is reproducible, but the branch cannot merge to `main` on a fork pin. **Proposed:** move to an nslsii
release once #317 is in.

### G3. Tiled spec names shared with hextools, different schema

[`specs.py:139`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/specs.py#L139) and [`:207`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/specs.py#L207) define
`TomoDarkFlat` v1 and `TomoFlyscan` v1, hextools' names and versions, but here `exposure_time` is a per-detector
dict and flats use `sample_out_motor` / `sample_out_offset` instead of `x_offset`. A run labelled `TomoFlyscan` v1
would mean two layouts depending on the beamline. **Proposed:** agree one layout with hextools (a v2), or use XPD-D's
own spec names.

### G4. Two profiles: which one runs at XPD-D?

`profiles/xpdd.py` and `profiles/devd.py` are the same apart from B3. **Question for Kyle:** which one does the
beamline start? Keep one.

### G5. `xpdd-tomo` also changes the LDRD profile and pins

[`profiles/ldrd.py:88`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/profiles/ldrd.py#L88) changes the data path, and the nslsii fork pin applies
to the LDRD environment too. `autonomous` will meet both when the branches merge. **Proposed:** keep LDRD changes on
`autonomous`, or tell its owners.

### G6. Sample-out motor default `amTopX` vs `amBaseX`

[`tomo/flyscans.py:65`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/flyscans.py#L65) defaults to `amTopX` (`Stack-Ax:Xfine`, "Top X"),
while Q3 in [`XPDD_DEVICE_MAP_PROPOSAL.md`](XPDD_DEVICE_MAP_PROPOSAL.md) proposed `amBaseX`. **Question for
Mehmet:** which motor takes the sample out of the beam, and does its travel cover the offset (default 2.0)?

### G7. No real shutter device

Mehmet's device list has the fast shutter as a motor, `XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr`. **Question for Kyle and
Mehmet:** is that the shutter the plans should close for darks, and how do open and closed map to its positions?
Needed for B1.

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
