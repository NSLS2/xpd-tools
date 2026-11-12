# Step 4: `scan_with_dark` in `main`

Move `scan_with_dark` and its `trigger_areaDet` into `main`'s `plans/`, built on steps 1 and 3. Keep its name and
argument names so that Queue Server callers do not change.

| PR | Change | Source | Needs | Shown working by |
|---|---|---|---|---|
| 4.1 | `scan_with_dark(dets, exposure, sample_info, md, stream_name, no_dark, …)` | qserver `94-CHL-plans.py:197` (`trigger_areaDet` at `:116`, `_pre_plan` at `:28`) | 1.1, 1.2, 3.1–3.5 | Mock RunEngine test: one dark run, then one light run whose start document carries `sc_dk_field_uid`, `calibration_md`, sample metadata and `analysis_stage`. Beamline: one run processed by pdfstream. |
| 4.2 | `ct_dark`, only if XPD still uses it | qserver `94-CHL-plans.py:11` | 4.1 | Mock test |

## Questions

- `frame_acq_time`, `jogging` and `user_config`: which of these arguments are still used?
- The profile's `qserver` branch is still being edited (`_pre_plan` changed on 2026-11-10). Who tells us when it
  stops changing, so that 4.1 copies the latest version?
