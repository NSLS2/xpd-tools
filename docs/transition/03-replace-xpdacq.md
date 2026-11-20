# Step 3: replace what `scan_with_dark` takes from xpdAcq

`scan_with_dark` relies on xpdAcq's globals (`glbl`, `bt`, `xpd_configuration`) and on its message mutators. xpd-tools
does not depend on xpdAcq.

This step is the xpdAcq retirement plan, `docs/xpdacq-retirement.md` (#59). That plan says what each PR does, how it is
tested and what is still open, so comments on step 3 go there. This file only lists the PRs and which of them step 4
needs.

| PR | Change | Step 4 needs it |
|---|---|---|
| 3.0 | Settings YAML and an xpd-tools Redis client | yes |
| 3.1 | Darks: `take_dark` and a dark preprocessor | yes |
| 3.2 | Calibration run, `.poni` loader and `calibration_md` | yes |
| 3.3 | Sample spreadsheet and `sample_info` | yes |
| 3.4 | `analysis_stage`, filter positions and a version key | yes |
| 3.5 | Exposure through ophyd-async `prepare` | yes |
| 3.6 | Shutter stubs | yes, through 3.1 |
| 3.7 | `ct` and a step plan | no |
| 3.8 | `start_beamtime` and the `bt_*` keys pdfstream reads | yes, through 3.9 |
| 3.9 | `install(RE)`: the metadata preprocessors on the RunEngine, and a thin `xrun` | yes |
| 3.10 | Profile switch in xpd-profile-collection | no |

3.1, 3.5 and 3.6 need step 1 (shutter and detector). The order of the others is in #59.
