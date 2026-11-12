# Transition plan: autonomous + profile Queue Server code into `main`

**Status:** draft plan for comment by Tom, TJ and the XPD team. No code changes in this PR, and nothing here is
decided until reviewers agree. As comments come in, we change the plan in this PR.

## Goal

Have one branch, `main`. Everything TJ's autonomous campaign needs (devices, plans and the `optimization` package)
lives in xpd-tools `main` and is tested by CI. The XPD Queue Server (`xf28id2-xpd-qs1`) loads it from there. After
that we retire the `autonomous` branch and the copies on xpd-profile-collection's `qserver` branch
(`startup/94-CHL-plans.py`, `97-xray_uvvis.py`, `98-xpd-tools.py`).

## Rules

These come from Tom and TJ on Slack:

- **One PR changes one device or one plan.** Each PR should be small enough to review in one sitting and should
  stay working once it is merged.
- **Every PR targets `main` first.** `autonomous` gets the change by merging `main` into it, and builds on what is
  there.
- **Shared code goes outside `optimization/`.** That covers devices, `scan_with_dark` and the pieces it needs.
- **Every PR:**
  - uses ophyd-async devices;
  - has mock tests that run in CI;
  - says how it was tested (mock, simulated device or beamline);
  - deletes the copy it replaces, or names the PR that will.

## Where things stand (read 2026-11-12)

| Branch | Tip | Compared with |
|---|---|---|
| xpd-tools `main` | `6f8491d` | — |
| xpd-tools `autonomous` | `24adb3c` | 138 commits ahead of `main` and 64 behind; 155 files, +23,948 / −196 |
| xpd-profile-collection `qserver` | `141f81f` | 145 commits ahead of its `main` and 3 behind |

## Steps

Start with **1 → 3 → 4**: that chain brings `scan_with_dark` into `main`. Step 2 changes only `autonomous`, so it
can run alongside. Steps 5, 6 and 7 come after.

| Step | File | What | PRs | Needs |
|---|---|---|---|---|
| 0 | [00-inventory.md](00-inventory.md) | This PR: rules, plus an inventory of every object to move | 1 | — |
| 1 | [01-devices.md](01-devices.md) | Fast shutter and PerkinElmer into `main` | 2 (+1) | — |
| 2 | [02-one-device-stack.md](02-one-device-stack.md) | `autonomous` uses `main`'s devices; its ophyd v1 copies are deleted | 6 | 2.5 needs 1.1 |
| 3 | [03-replace-xpdacq.md](03-replace-xpdacq.md) | Dark frames, calibration, sample and analysis-stage metadata, and exposure setup, without xpdAcq | 5 | 1 |
| 4 | [04-scan-with-dark.md](04-scan-with-dark.md) | `scan_with_dark` | 1 (+1) | 1, 3 |
| 5 | [05-acquisition-plans.md](05-acquisition-plans.md) | The four Queue Server acquisition plans | 5 | 4 |
| 6 | [06-optimization.md](06-optimization.md) | The `optimization` package, in dependency order | 10 | 5 (in part) |
| 7 | [07-queue-server-from-main.md](07-queue-server-from-main.md) | qs1 loads from `main`; copies and `autonomous` retired | 3 | 5, 6 |

PR numbers are labelled `<step>.<n>` (for example 1.1). As each PR opens, we add its GitHub number to the step's
table.
