# Step 5: the Queue Server acquisition plans in `main`

The agent submits four plans by name: `uvvis_acquire`, `xray_acquire`, `xray_screened_acquire` and
`xray_uvvis_acquire`. `autonomous` has two versions of most of them:

- `optimization/qserver/`: verbatim copies of the profile's `97-xray_uvvis.py` and `98-xpd-tools.py` (from #44).
  They use the profile's devices and helpers through `install(namespace)`.
- `optimization/plans/`: TJ's own plans, built from validated device contexts (`create_xray_uvvis_plan`, …).

Only one version should move to `main`.

| PR | Change | Needs | Shown working by |
|---|---|---|---|
| 5.0 | Choose which version moves to `main` (decided in this PR's comments, no code) | — | — |
| 5.1 | Pump and flow steps the plans need that `main`'s `plans/pump_plans.py` does not have yet (for example waiting for equilibrium). With TJ's version, these are `plans/runtime.py`, `metadata.py`, `volumes.py`, `preflight.py` and `validation.py` (~800 lines; split if needed) | 5.0 | Mock test |
| 5.2 | `uvvis_acquire` | 5.0, 5.1 | Mock test; beamline UV-vis run |
| 5.3 | `xray_acquire` | 4.1, 5.1 | Mock test; beamline X-ray run |
| 5.4 | `xray_uvvis_acquire` | 4.1, 5.1 | Mock test; beamline run |
| 5.5 | `xray_screened_acquire` | 5.3 | Mock test |

If 5.0 chooses TJ's version, the shared pieces it imports (`helpers/beamline.py`, `helpers/qepro.py`,
`helpers/sources.py` and `analysis.py`) come first: they are 6.1, 6.3 and 6.4.
