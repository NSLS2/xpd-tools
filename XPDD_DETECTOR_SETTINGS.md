# Choosing detectors and their settings for a scan (bsui, GUI and Queue Server)

Discussion draft (AJ, with ideas from Kyle, 2026-11-10). Comment on a line to discuss.

## The problem

An XPD-D scan uses 1 to 3 detectors (Pilatus4, IRIS, Xspress3), and each can need its own settings: at least
an exposure time, and, for the per-detector rates Mehmet asked for, how many frames it takes per projection. The
same scan must be easy to start from:

- **bsui / IPython**, without a long command;
- **the hextools GUI**, where a user picks detectors from dropdowns and fills in their settings;
- **the Queue Server**, which only passes device and plan names and plain data.

Detectors also have to agree with each other, e.g. one detector's frame count is a whole multiple of another's.

Today `tomo_flyscan` takes `detectors: list[...]` and `exposure_time: float | Mapping[detector | str, float]`
([`utils.py`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/utils.py#L20), [`flyscans.py`](https://github.com/NSLS2/xpd-tools/blob/1175d91ea1c8170fb2a5c864aec5be43a73e22a8/src/xpd_tools/tomo/flyscans.py#L235)); string keys are a detector's ophyd
`.name` (`xspress3-1`), not its session name (`xs3`).

## Recommendation

**One list, where each entry carries its detector as a value, plus one check function.**

```python
# bsui: objects, or a bare detector for the defaults
RE(tomo_flyscan([DetConfig(detector=pilatus1, exposure=0.01, collections_per_event=10),
                 DetConfig(detector=xs3, exposure=0.1)], num_projections=1000))

# Queue Server and GUI: the same entries as plain data
detectors = [{"detector": "pilatus1", "exposure": 0.01, "collections_per_event": 10},
             {"detector": "xs3", "exposure": 0.1}]
```

1. **`DetConfig`** (name to be agreed): a small pydantic model in xpd-tools with only what a user chooses:
   `detector`, `exposure` (s), `collections_per_event` (integer, default 1: frames per frame of the slowest detector,
   ophyd-async's `TriggerInfo` name, since a fly scan has no points). The plan builds each detector's full
   ophyd-async `TriggerInfo` from it (trigger type, number of events, deadtime), so a user cannot enter values that
   contradict the scan.
2. **The plan accepts any mix** of bare detectors, `DetConfig` objects and the equivalent dicts, and turns each into a
   `DetConfig` first.
3. **One check function**, pure and tested, called by the plan before anything moves, and by the GUI for live
   feedback: at most 3 detectors; no detector twice; each `collections_per_event` divides the larger ones, so frame
   counts are whole multiples of each other; each detector's frames (exposure plus deadtime) fit the period.
4. **The run records it**: each detector's settings go into the start document, so processing finds them in Tiled
   (R3 in [`XPD_DATA_PROCESSING.md`](XPD_DATA_PROCESSING.md#proposed-requirements)).
5. **The GUI form (hextools)** learns one generic thing: a list whose entries are a pydantic model gets one row per
   entry, a detector dropdown plus the model's fields, and it sends the dicts. No XPD-specific widget.

Why this shape:

- **Queue Server** ([bluesky-queueserver `8691232`](https://github.com/bluesky/bluesky-queueserver/commit/869123246aaf9c50172fecaeb5e8d3b2d41f60ac), v0.0.26):
  - it converts device names to devices in list items and **dict values, but not dict keys**
    ([`profile_ops.py#L922`](https://github.com/bluesky/bluesky-queueserver/blob/869123246aaf9c50172fecaeb5e8d3b2d41f60ac/src/bluesky_queueserver/manager/profile_ops.py#L922)), so the detector must be a value;
  - it **drops parameter annotations it cannot rebuild** from `typing` and `bluesky` ([`#L2907`](https://github.com/bluesky/bluesky-queueserver/blob/869123246aaf9c50172fecaeb5e8d3b2d41f60ac/src/bluesky_queueserver/manager/profile_ops.py#L2907)), such as a
    custom class; such a parameter gets all device names converted and no type check ([`#L963`](https://github.com/bluesky/bluesky-queueserver/blob/869123246aaf9c50172fecaeb5e8d3b2d41f60ac/src/bluesky_queueserver/manager/profile_ops.py#L963)), so the
    plan has to validate, which item 3 does;
  - it cannot pass a Python object built in a bsui session; entries as dicts travel fine.
- **bsui** stays short: a bare detector keeps today's behaviour; `DetConfig` lines can be built once and reused.
- **GUI**: in Queue Server mode the GUI imports the plan from its module, so its form still sees the real types.
- **No hidden state**: every scan call shows what it uses, which matters for a queue that can be edited or reordered.

### Order of work

Implementation: [#51](https://github.com/NSLS2/xpd-tools/pull/51) (`DetConfig` and the check, into `xpdd-tomo`).

1. Now: `DetConfig`, the check function, and `tomo_flyscan` accepting the list (with `collections_per_event` held at 1
   until the PandA can trigger detectors at different rates; Kyle is working on that).
2. hextools plan form: show "unsupported type" instead of a device dropdown for any type it does not know (today an
   unrecognised required parameter falls through to a device dropdown, [`_build_fields`](https://github.com/NSLS2/hextools/blob/f1aa3bc6ef452607162b381e40c9a445263eec3c/src/hextools/gui/plan_widget.py#L338)); then the
   list-of-entries editor.
3. `collections_per_event > 1` once per-detector triggering is in, with the run layout agreed with Mehmet (R3).

## Alternatives considered

| | Idea | Why not (or not yet) |
|---|---|---|
| A | A mapping keyed by detector (today's `exposure_time`), one GUI row per picked detector | The Queue Server leaves dict keys as names, which do not match `.name` (`xs3` vs `xspress3-1`, `pilatus1_fly` vs `pilatus1`); one value per detector only |
| B | A different settings class per detector type (Pilatus: exposure, threshold; IRIS: readout mode) | More machinery than needed now; can grow out of `DetConfig` later (subclasses), with the same list shape |
| C | A Detectors tab that sets standing detector settings, and scans only take the list | Settings live outside the scan call; per-scan values (exposure) still belong to the plan |
| D | An XPD-D-only tomography tab with 3 detector slots | A device-specific widget, which the generic GUI is meant to avoid |
| E | The full ophyd-async `TriggerInfo` per detector (Kyle) | Users could set what the plan must own (trigger type, event count); xpd-tools follows ophyd-async `main`, where this API moves (a related helper removed in #1329 broke the Pilatus4 on 11-03). `DetConfig` keeps the idea with only the user's fields |
| G | A setup plan, `configure_detectors([...])`, that later scans use (Kyle) | Hidden state: each scan depends on what ran before; the same class of problem as the dark/flat link surviving a proposal switch (review B2). Works in the Queue Server, but reordering the queue changes what a scan does |
| G' | Build a validated detector set once, `dets = detector_set(...)`, and pass it | Fine in bsui, but the Queue Server cannot pass a Python object; the list of dicts covers the same need |

## Questions

1. Kyle: is `collections_per_event` the right knob for per-detector rates, or would you rather give each detector its own
   period?
2. Mehmet: which settings besides exposure do you change per scan (Pilatus threshold, IRIS readout mode, ...)?
3. Name and home: `DetConfig` in xpd-tools, or shared with hextools from the start?
