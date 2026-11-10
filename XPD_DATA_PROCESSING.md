# XPD data processing reads its data from Tiled

Discussion draft (AJ, 2026-11-10). Comment on a line to discuss; answers move into the text.

## The goal

- **Acquisition** (xpd-tools, the DAD side) puts the right Bluesky documents into Tiled: the data and the metadata.
- **Processing** gets everything it needs from those documents in Tiled, through a Tiled client, and runs in a
  separate process from acquisition, preferably on a different machine. It reads nothing from the acquisition side
  directly: not the bsui session, not its Redis-backed `RE.md`, not the ZMQ stream, and no metadata or job files
  written next to the data for it.

How XPD-D data is processed is **Mehmet's call and not known yet**; this file does not propose a pipeline. It aims
to make sure that whatever pipeline is chosen finds what it needs in Tiled, without hard-to-maintain custom pieces
between the two sides.

The approach, not the tool, is what is proposed. One example is AJ's change to scriptrunner, the tool HEX uses
([fork branch `tiled-run-picker`](https://github.com/sligara7/scriptrunner/tree/tiled-run-picker)): a Tiled client
searches runs by date, plan and proposal, and the processing script gets the run's uid and reads the run itself. At
HEX this replaces an auto-exporter that wrote metadata files next to each run. Mehmet's notebook for scan 12462
already reads a run this way:

```python
from tiled.client import from_uri

c = from_uri("https://tiled.nsls2.bnl.gov")["xpdd"]["migration"]  # the catalog the XPD-D profile writes today
run = c[uid]
start = run.metadata["start"]              # proposal, sample, exposure times, ...
scan = run["primary"].read()               # detector frames and rotation_angle, per frame
darkflat = c[start["last_dark_flat_uid"]]  # the dark/flat run this scan was taken with
dark, flat = darkflat["dark"].read(), darkflat["flat"].read()
```

## Three checks for every run

What acquisition has to get right, checked against the `xpdd-tomo` branch at `1175d91` and scan 12462. **Mehmet:
please mark what processing needs and what is missing.**

### 1. Format

Every detector in the XPD-D profile writes **HDF5** through its ophyd-async writer, into the proposal's folder
(`NSLS2PathProvider`): Pilatus4 `pilatus1`, Kinetix `iris1`, Xspress3 `xspress3-1`, and the PandA `panda1`. Tiled
indexes those files and serves the arrays, so a reader through Tiled does not depend on the format. The format
matters when processing reads the files directly (large tomography stacks) or needs another format for a tool
(TIFF, netCDF as xpdDacq writes today). Is HDF5 enough?

### 2. Data products

| Product | In Tiled today | Open |
|---|---|---|
| Detector frames | `pilatus1`, `iris1`, `xspress3-1` (+ channel) in `primary` | One shared `time` axis; per-detector rates (Mehmet, 11-10) need a new layout |
| Rotation angle | `rotation_angle` in `primary`, one per frame, from the PandA | Per detector, once rates differ |
| Darks and flats | `dark` and `flat` streams of the `tomo_dark_flat` run, linked by `last_dark_flat_uid` | |
| Grid position of a step scan | `grid_reference_positions` in the start document | |
| Prosilica (visible camera) | **Not in the xpd-tools XPD-D profile** (xpdDacq has one) | Needed? For what? |
| Other motor positions (sample stack, slits) | **No baseline stream** in the profile | Which ones? |

### 3. Metadata

Scan 12462's start document has `data_session` and `proposal` (PI, title), `sample_name` when given,
`detectors`, `exposure_time` per detector, `acquire_period`, `num_points`, `start_position` / `stop_position`,
`rotation_motor`, `time_based`, `images_to_average`, `filter_type`, `last_dark_flat_uid` / `_scan_id`, the
`tiled_specs`, and versions. Not there today:

- **Energy or wavelength**, and **calibration** (detector distance, centre, tilt: a poni file or a run that holds it).
- **Sample** beyond its name (composition, environment).
- `start_datetime` is wrong: scan 12462 (2026-11-10) reads `2026-07-17T15:04:59`.

What else does processing need?

## Proposed requirements

Proposed by AJ (2026-11-10), not agreed yet.

- **R1. Processing reads runs from Tiled.** Every processing step (script, service or notebook) finds its input run
  in Tiled and reads its metadata and data there, by uid or by a Tiled search. Darks, flats and calibration runs are
  found the same way, through uids the input run carries. Anything that needs data sooner than Tiled can give it
  (feedback to a running plan, or to the scientist in the hutch) is an exception, decided and written down.
- **R2. No side files for processing.** Acquisition writes no file whose purpose is to tell processing about a run:
  no per-run metadata document and no job file for a watcher. The detector's own data files are not side files.
- **R3. Each run carries what its processing needs**, in its own documents (the three checks above), with a Tiled
  spec naming its layout (`TomoDarkFlat`, `TomoFlyscan`). What processing needs is agreed with whoever processes the
  data **before the plans are final**; a spec's layout changes only with a new spec version.
- **R4. Processing does not run on the acquisition machines.** Today the pdfstream servers run on the acquisition
  workstations `xf28id2-ws1..3`.

## Questions for Mehmet

1. What processing do you plan for XPD-D (tomography reconstruction, XRD/PDF integration, XRF fitting, ...), with
   which tools, and where do you run it today?
2. The three checks: what is missing for your processing?
3. xpdDacq writes one netCDF per scan. Should a Tiled-based reader give the same structure, so your notebooks keep
   working?
4. Where should results go: back into Tiled, the proposal folder, or both?

## Things to check about reading from Tiled

- **Latency:** fine after a scan; live feedback during a scan may need an exception (R1).
- **Large frames:** reading through Tiled's API versus the HDF5 files Tiled points to.
- **Access:** the account that reads needs access to the proposal. HEX's auto-exporter failed under the shared
  beamline account for this reason (2026-10-26 and 10-29).

## Background: what processes XPD data today

| Tool | What it does | Where it runs | Data source |
|---|---|---|---|
| pdfstream servers (`pdfstream_servers` Ansible role) | Live integration to 1D and PDF, save and plot servers, a ZMQ-to-Tiled inserter | `xf28id2-ws1`, `-ws2`, `-ws3` (acquisition workstations) | ZMQ document stream |
| [xpdAcq](https://github.com/xpdAcq/xpdAcq) (2016-2023) | Acquisition, darks, calibration (`pyFAI-calib2`); no reduction | The acquisition session | Detector images in the session |
| xpdDacq (Mehmet's D-hutch tools, not in git) | No RunEngine. Writes detector HDF5 plus a job `.yml`; a watcher calls `process_yml`, which packs darks, flats, counts and scans into netCDF; no integration or reconstruction | The watcher's machine | Files under `/nsls2/data/xpd-new/legacy/raw/xpdd/` |
