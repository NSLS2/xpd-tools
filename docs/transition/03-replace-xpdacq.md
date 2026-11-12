# Step 3: replace what `scan_with_dark` takes from xpdAcq

`scan_with_dark` relies on xpdAcq's globals (`glbl`, `bt`, `xpd_configuration`) and on its message mutators. xpd-tools
does not depend on xpdAcq. Each PR here adds one small bluesky-native replacement and keeps the start-document keys
that pdfstream reads.

| PR | Change | Replaces | Shown working by |
|---|---|---|---|
| 3.1 | Dark frames: a `take_dark` plan (fast shutter closed, its own run) and a preprocessor that takes a new dark when the last one is older than a set window or the exposure changed. Each light run's start document gets `sc_dk_field_uid` | `periodic_dark`, `take_dark`, `_inject_qualified_dark_frame_uid`, `glbl['dk_window']` | Mock RunEngine test: the first run gets a dark, a second run inside the window reuses it, and a change of exposure takes a new one |
| 3.2 | Calibration metadata: a preprocessor that writes `calibration_md` (and `detector_calibration_client_uid`) from a calibration file the profile names | `_inject_calibration_md`, `glbl` calibration file | Mock test: the start document carries the calibration |
| 3.3 | Sample metadata from a plan argument (`sample_info`) instead of `bt.samples` | `bt.samples.sel(sample_ID)` | Mock test |
| 3.4 | `analysis_stage: "raw"` in the start document | `_inject_analysis_stage` | Mock test; pdfstream's servers still process the run |
| 3.5 | Exposure and frame-time setup through ophyd-async `prepare` (`TriggerInfo`), for `pe1c` and Pilatus | `_pre_plan`, `configure_area_det`, `xpd_configuration['area_det']` | Mock test: the detector is prepared with the requested exposure |

3.1 needs 1.1 (shutter) and 1.2 (detector); 3.5 needs 1.2. 3.2 to 3.4 need nothing.

## Questions

- **3.1:** should a dark be a separate run linked by uid (as xpdAcq and hextools do), or a `dark` stream inside the
  same run? A separate run keeps pdfstream working unchanged.
- **3.1:** what window should we use? xpdAcq's default `dk_window` is a value in `glbl`; XPD should say which value
  they use.
- **3.2:** where does the calibration come from without xpdAcq: the latest calibration run in Tiled, or a file?
