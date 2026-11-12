# Step 6: the `optimization` package in `main`

The order below follows the imports inside `optimization/` on `autonomous` `24adb3c`: each PR needs only the PRs
before it. Line counts are for that commit.

| PR | Modules | Lines | Needs |
|---|---|---|---|
| 6.1 | `helpers/common.py`, `utils/` | ~300 | — |
| 6.2 | `scoring/` (`_shared` and six scorers; can be split per scorer) | ~540 | 6.1 |
| 6.3 | `analysis.py` (`main` has an older copy) | 488 | 6.1, 6.2 |
| 6.4 | `helpers/qepro.py`, `sources.py`, `beamline.py`, `dofs.py`, `phases.py`, `run.py` | ~870 | 6.3 |
| 6.5 | `pdffit/`, `helpers/pdf.py` | ~640 | 6.3 |
| 6.6 | `plugins/` | ~420 | 6.4, 6.5 |
| 6.7 | `simulation/`, `legacy/tiled.py` | ~540 | 6.4, 6.5 |
| 6.8 | `agent.py`, `cli.py`, `stopping.py` | ~1,970 | 6.6, step 5 |
| 6.9 | `campaign.py`, `profiles/halide.py`, `visualization.py` | ~550 | 6.8, 2.4 |
| 6.10 | `autonomous-gui/` | — | 6.9 |

Each PR moves its tests from `tests/optimization` with it.

## Questions

- **6.8:** `agent.py` alone is 1,741 lines. Should it be split before it moves, so it fits the "small PR" rule?
- **6.10:** should the web GUI (React and a server) live in xpd-tools, or in a repo of its own?
- Which optional dependencies (for example the CNN scorer's) belong in `main`'s `pyproject.toml`, and as extras?
