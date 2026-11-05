"""Syringe volume check before each trial, and the plan that resets pump counters."""

from __future__ import annotations

import re
from collections.abc import Sequence
from math import pi
from typing import Any

from bluesky import plan_stubs as bps

from xpd_tools.optimization.plans.metadata import _device_name

# Measurement time assumed until the first trial has been timed (fluorescence quality
# batches, absorbance, X-ray). After that the longest measured one is used.
FIRST_MEASURE_SEC = 120.0

# Pump volume readback units, in mL.
_ML_PER_UNIT = {"ml": 1.0, "ul": 1e-3, "µl": 1e-3, "μl": 1e-3, "nl": 1e-6}

# RefillRequired messages end with this, so the GUI can find the pumps in any mode
# (the queue server passes the error on as text).
_PUMPS_PREFIX = "pumps to refill: "


class RefillRequired(RuntimeError):  # noqa: N818 -- public name, matched in error text
    """
    A syringe can't supply the next trial; refill, clear its counter, and continue.

    Args:
        - shortfalls: (pump name, label, left_ml, need_ml) for each short pump.
    """

    def __init__(self, shortfalls: Sequence[tuple[str, str, float, float]]) -> None:
        self.pumps = [name for name, *_ in shortfalls]
        details = "; ".join(
            f"{name} ({label}) has {left:.2f} mL left, next trial needs ~{need:.2f} mL"
            for name, label, left, need in shortfalls
        )
        super().__init__(
            f"Refill needed: {details}. {_PUMPS_PREFIX}{', '.join(self.pumps)}"
        )


def refill_pumps(message: str) -> list[str] | None:
    """
    Return the pumps named in a RefillRequired message.

    Args:
        - message: An error's text, e.g. as reported by the queue server.

    Returns
    -------
        - list[str] | None - Pump names, or None if it isn't a refill error.
    """
    found = re.search(rf"{_PUMPS_PREFIX}([\w, -]+)", message)
    return None if found is None else [p.strip() for p in found.group(1).split(",")]


def clear_infused_volumes(pumps):  # noqa: ANN001 -- see below
    """
    Reset the pumps' infused-volume counters (after refilling them).

    `pumps` is deliberately unannotated: the queue server validates plan parameters
    from their annotations, can't resolve this module's (string) ones, and converts
    unannotated device names (e.g. "dds2_p1") to the devices.

    Args:
        - pumps: Pump devices with `clear_infused`.

    Returns
    -------
        - Plan messages.
    """
    for pump in pumps:
        yield from bps.mv(pump.clear_infused, 1)


def _mixer_seconds(context: Any, rates: Sequence[float]) -> float:
    """Return the equilibrium wait for `rates`.

    Computed as runtime._wait_for_equilibrium does from readbacks (same 1.016 mm
    tubing).
    """
    lengths = context.mixer_lengths_cm
    stages = (
        ((lengths[0], rates[:2]), (lengths[1], rates))
        if len(lengths) == 2
        else ((lengths[0], rates),)
    )
    seconds = 0.0
    for length_cm, stage_rates in stages:
        total = sum(stage_rates)
        if total > 0:
            seconds += 60 * pi * (1.016 / 2) ** 2 * length_cm * 10 / total
    return seconds * context.residence_time_ratio


class VolumeTracker:
    """
    Checks, before each trial, that every pump with `loaded_ml` can supply it.

    Used = the pump's own infused-volume readback (so washes and dilutions count).
    Needed = rate x how long that pump runs in the trial: equilibrium and dilution
    waits for these rates, plus the longest measurement so far (FIRST_MEASURE_SEC
    before the first). Pumps without `loaded_ml` are not checked.

    Args:
        - context: The plan context (sources, dilutions, wash_cycles, mixer settings).
    """

    def __init__(self, context: Any) -> None:
        # (loaded_ml/reserve_ml are checked when each source/dilution/wash is created.)
        self.context = context
        self.measure_sec: float | None = None

    def record_measurement(self, seconds: float) -> None:
        """Remember the longest measurement, for the next trial's estimate."""
        self.measure_sec = max(self.measure_sec or 0.0, seconds)

    def check(self, rates: Sequence[float]):
        """
        Raise RefillRequired if any checked pump can't supply a trial at `rates`.

        Args:
            - rates: The trial's source rates (µL/min), in context.sources order.

        Returns
        -------
            - Plan messages (reads each checked pump's infused volume).
        """
        context = self.context
        total = sum(rates)
        measure = FIRST_MEASURE_SEC if self.measure_sec is None else self.measure_sec
        dilution_rates = [total * stage.ratio for stage in context.dilutions]
        waits = {
            position: sum(
                stage.wait_sec
                for stage, rate in zip(context.dilutions, dilution_rates, strict=True)
                if stage.position == position and rate > 0
            )
            for position in ("before_equilibrium", "after_equilibrium")
        }
        after_sec = waits["after_equilibrium"] + measure
        whole_sec = (
            waits["before_equilibrium"] + _mixer_seconds(context, rates) + after_sec
        )

        # (item, µL/min, seconds it runs) for everything a trial pumps
        runs = [
            *((s, r, whole_sec) for s, r in zip(context.sources, rates, strict=True)),
            *(
                (
                    stage,
                    rate,
                    whole_sec if stage.position == "before_equilibrium" else after_sec,
                )
                for stage, rate in zip(context.dilutions, dilution_rates, strict=True)
            ),
            *(
                (cycle, cycle.rate_ul_min, cycle.duration_sec)
                for cycle in context.wash_cycles
            ),
        ]
        # Per pump (one pump may serve several roles): smallest usable volume and
        # total need.
        pumps: dict[int, list[Any]] = {}
        for item, rate, seconds in runs:
            if item.loaded_ml is None:
                continue
            usable = item.loaded_ml
            if item.set_target:  # the pump stops itself at its target volume
                usable = min(usable, item.target_ml)
            label = getattr(item, "sample_label", None) or (
                "wash" if item in context.wash_cycles else "dilution"
            )
            entry = pumps.setdefault(
                id(item.pump), [item.pump, label, usable, 0.0, item.reserve_ml]
            )
            entry[2] = min(entry[2], usable)
            entry[3] += rate / 1000 * seconds / 60
            entry[4] = max(entry[4], item.reserve_ml)

        shortfalls = []
        for pump, label, usable, need, reserve in pumps.values():
            used = yield from bps.rd(pump.read_infused)
            unit = str((yield from bps.rd(pump.read_infused_unit))).lower()
            if unit not in _ML_PER_UNIT:
                raise ValueError(f"{_device_name(pump)}: unknown volume unit {unit!r}")
            left = usable - reserve - float(used) * _ML_PER_UNIT[unit]
            if need > left:
                shortfalls.append((_device_name(pump), label, max(left, 0.0), need))
        if shortfalls:
            raise RefillRequired(shortfalls)
