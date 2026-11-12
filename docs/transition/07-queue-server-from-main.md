# Step 7: the Queue Server runs from `main`

| PR | Repo | Change | Needs | Shown working by |
|---|---|---|---|---|
| 7.1 | xpd-profile-collection `qserver` | Load the plans from xpd-tools `main` instead of `install(namespace)`, and delete `97-xray_uvvis.py`, `98-xpd-tools.py` and the moved parts of `94-CHL-plans.py` | 5, 6.8 | The Queue Server lists the plans, and the agent submits one |
| 7.2 | xpd-profile-collection `qserver` | Pin the qs1 environment to a release or commit of xpd-tools `main` instead of `autonomous` | 7.1 | qs1 starts its worker |
| 7.3 | — | One autonomous campaign at the beamline runs entirely from `main`; then retire `autonomous` | 7.2 | The campaign's runs in Tiled |

## Question

- Who decides when `autonomous` is retired: TJ, Tom, or the XPD team?
