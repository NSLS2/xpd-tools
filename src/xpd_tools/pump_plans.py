"""Bluesky plans for syringe-pump configuration, operation and safe cleanup."""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Generator, Sequence

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from ophyd_async.core import wait_for_value

from .pumps import (
    DDSSyringeAxis,
    DDSSyringeController,
    UltraSyringePump,
    rate_limits,
    rate_unit_converter,
    validate_pump_settings,
    vol_unit_converter,
)

Pump = DDSSyringeAxis | UltraSyringePump


def _rate(value: float | str, unit: str) -> float:
    if isinstance(value, str):
        number, unit = value.split()
        value = float(number)
    return float(value) * rate_unit_converter(unit, "ul/min")


def _target(value: float | str) -> float:
    if isinstance(value, str):
        number, unit = value.split()
        return float(number) * vol_unit_converter(unit, "ml")
    return float(value)


def _count(pumps: Sequence[Pump], **parameters: Sequence) -> None:
    for name, values in parameters.items():
        if len(values) != len(pumps):
            raise ValueError(f"{name} has {len(values)} entries for {len(pumps)} pumps")


def _configure(
    pumps: Sequence[Pump],
    syringe_sizes_ml: Sequence[float],
    target_volumes: Sequence[float | str],
    rates: Sequence[float | str],
    materials: Sequence[str],
    set_targets: Sequence[bool],
    direction: str,
    rate_unit: str,
) -> Generator:
    _count(
        pumps,
        syringe_sizes_ml=syringe_sizes_ml,
        target_volumes=target_volumes,
        rates=rates,
        materials=materials,
        set_targets=set_targets,
    )
    normalized = []
    for pump, size, target, rate, material, set_target in zip(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        strict=True,
    ):
        rate_ul_min = _rate(rate, rate_unit)
        target_ml = _target(target) if set_target else None
        validate_pump_settings(pump, material, size, target_ml, rate_ul_min)
        normalized.append((pump, size, target_ml, rate_ul_min, material))

    # Range refresh/read is performed for *every* requested DDS axis before any
    # target or rate write; a wrong syringe on one axis cannot partly configure another.
    for pump, size, _, rate, material in normalized:
        if not rate:
            continue
        if isinstance(pump, DDSSyringeAxis):
            yield from bps.trigger(pump.set_infuse_range, wait=True)
            yield from bps.trigger(pump.set_withdraw_range, wait=True)
            selected = yield from bps.rd(pump.read_min_infuse)
            selected_unit = yield from bps.rd(pump.read_min_infuse_unit)
            expected, _ = rate_limits(pump, material, size)
            if not math.isclose(
                selected * rate_unit_converter(selected_unit, "nl/min"),
                expected,
                rel_tol=1e-3,
                abs_tol=0,
            ):
                raise ValueError(
                    f"{pump.name}: selected DDS syringe does not match "
                    f"{size} mL {material}"
                )
            lower = yield from bps.rd(getattr(pump, f"read_min_{direction}"))
            lower_unit = yield from bps.rd(getattr(pump, f"read_min_{direction}_unit"))
            upper = yield from bps.rd(getattr(pump, f"read_max_{direction}"))
            upper_unit = yield from bps.rd(getattr(pump, f"read_max_{direction}_unit"))
            lower *= rate_unit_converter(lower_unit, "ul/min")
            upper *= rate_unit_converter(upper_unit, "ul/min")
            if not (
                math.isfinite(lower)
                and math.isfinite(upper)
                and 0 < lower <= rate <= upper
            ):
                raise ValueError(f"{pump.name}: {direction} rate outside IOC limits")
        else:
            warnings.warn(
                f"{pump.name}: confirm {size} mL {material} syringe on Ultra panel",
                UserWarning,
                stacklevel=2,
            )

    for pump, _, target, rate, _ in normalized:
        if not rate:
            continue
        if target is not None:
            yield from bps.mv(pump.target_vol_unit, "ml")
            yield from bps.mv(pump.target_vol, target)
        yield from bps.mv(getattr(pump, f"{direction}_rate_unit"), "ul/min")
        yield from bps.mv(getattr(pump, f"{direction}_rate"), rate)


def configure_infusion(
    pumps,
    syringe_sizes_ml,
    target_volumes,
    rates,
    materials,
    set_targets,
    *,
    rate_unit="ul/min",
):
    """Validate syringes and IOC limits before writing infusion settings.

    Parameters
    ----------
    pumps : sequence of DDSSyringeAxis or UltraSyringePump
        Connected pumps; zero-rate entries are not written.
    syringe_sizes_ml : sequence of float
        Syringe capacities in mL, one per pump.
    target_volumes : sequence of float or str
        Targets in mL or strings such as ``'30 ml'``, one per pump.
    rates : sequence of float or str
        Rates in ``rate_unit`` or strings such as ``'100 ul/min'``.
    materials : sequence of str
        Syringe identifiers: ``steel``, ``glass_H1000`` or ``plastic_BD``.
    set_targets : sequence of bool
        Whether to validate and write each target; False preserves it.
    rate_unit : str, optional
        Units of numeric rates; default is ``'ul/min'``.

    Yields
    ------
    Msg
        RunEngine messages for range checks and setpoint writes.

    Raises
    ------
    ValueError
        If input lengths differ or syringe, target, or rate checks fail.

    Warns
    -----
    UserWarning
        For nonzero Ultra rates: confirm the syringe on the pump panel.
    """
    yield from _configure(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        "infuse",
        rate_unit,
    )


def configure_withdrawal(
    pumps,
    syringe_sizes_ml,
    target_volumes,
    rates,
    materials,
    set_targets,
    *,
    rate_unit="ul/min",
):
    """Validate syringes and IOC limits before writing withdrawal settings.

    Parameters
    ----------
    pumps : sequence of DDSSyringeAxis or UltraSyringePump
        Connected pumps; zero-rate entries are not written.
    syringe_sizes_ml : sequence of float
        Syringe capacities in mL, one per pump.
    target_volumes : sequence of float or str
        Targets in mL or strings such as ``'30 ml'``, one per pump.
    rates : sequence of float or str
        Rates in ``rate_unit`` or strings such as ``'100 ul/min'``.
    materials : sequence of str
        Syringe identifiers: ``steel``, ``glass_H1000`` or ``plastic_BD``.
    set_targets : sequence of bool
        Whether to validate and write each target; False preserves it.
    rate_unit : str, optional
        Units of numeric rates; default is ``'ul/min'``.

    Yields
    ------
    Msg
        RunEngine messages for range checks and setpoint writes.

    Raises
    ------
    ValueError
        If input lengths differ or syringe, target, or rate checks fail.

    Warns
    -----
    UserWarning
        For nonzero Ultra rates: confirm the syringe on the pump panel.
    """
    yield from _configure(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        "withdraw",
        rate_unit,
    )


def _start(pumps, rates, direction, rate_unit):
    _count(pumps, rates=rates)
    normalized = [_rate(rate, rate_unit) for rate in rates]
    if any(not math.isfinite(rate) or rate < 0 for rate in normalized):
        raise ValueError("Start rates must be finite and nonnegative")
    for pump, rate in zip(pumps, normalized, strict=True):
        if rate:
            yield from bps.trigger(getattr(pump, f"pump_{direction}"), wait=True)
            expected = "Infusing" if direction == "infuse" else "Withdrawing"
            tasks = yield from bps.wait_for(
                [
                    lambda pump=pump, expected=expected: wait_for_value(
                        pump.status,
                        expected,
                        timeout=10,
                    )
                ]
            )
            for task in tasks:
                task.result()


def start_infusion(pumps, rates, *, rate_unit="ul/min"):
    """Start selected pumps with nonzero rates and await infusion status.

    Parameters
    ----------
    pumps : sequence of DDSSyringeAxis or UltraSyringePump
        Connected pumps, already configured for infusion.
    rates : sequence of float or str
        Nonnegative rates, one per pump; zero skips starting without stopping.
        Values only select pumps to start; they do not set rates.
    rate_unit : str, optional
        Units of numeric rates; strings include units. Default is ``'ul/min'``.

    Yields
    ------
    Msg
        Start commands and waits for ``Infusing`` status, up to 10 seconds.

    Raises
    ------
    ValueError
        If lengths differ or rates are negative or nonfinite.
    """
    yield from _start(pumps, rates, "infuse", rate_unit)


def start_withdrawal(pumps, rates, *, rate_unit="ul/min"):
    """Start selected pumps with nonzero rates and await withdrawal status.

    Parameters
    ----------
    pumps : sequence of DDSSyringeAxis or UltraSyringePump
        Connected pumps, already configured for withdrawal.
    rates : sequence of float or str
        Nonnegative rates, one per pump; zero skips starting without stopping.
        Values only select pumps to start; they do not set rates.
    rate_unit : str, optional
        Units of numeric rates; strings include units. Default is ``'ul/min'``.

    Yields
    ------
    Msg
        Start commands and waits for ``Withdrawing`` status, up to 10 seconds.

    Raises
    ------
    ValueError
        If lengths differ or rates are negative or nonfinite.
    """
    yield from _start(pumps, rates, "withdraw", rate_unit)


def stop_pumps(pumps):
    """Stop every selected pump and confirm flow has stopped, even on failure.

    Parameters
    ----------
    pumps : iterable of DDSSyringeAxis or UltraSyringePump
        Connected pumps to stop; unselected siblings are untouched.

    Yields
    ------
    Msg
        STOP commands and status-confirmation waits, up to 10 seconds per pump.

    Raises
    ------
    ExceptionGroup
        Collected command or status failures, after attempting every pump.
    """
    failures = []
    pending = []
    for index, pump in enumerate(pumps):
        group = f"stop_pumps_{index}"
        try:
            yield from bps.trigger(pump.pump_stop, group=group)
            pending.append((pump, group))
        except Exception as exc:
            failures.append(exc)
    for pump, group in pending:
        try:
            yield from bps.wait(group=group)
            tasks = yield from bps.wait_for(
                [
                    lambda pump=pump: wait_for_value(
                        pump.status,
                        lambda status: status not in ("Infusing", "Withdrawing"),
                        timeout=10,
                    )
                ]
            )
            for task in tasks:
                task.result()
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("Pump stop failures", failures)


def enable_pumps(
    devices: Sequence[DDSSyringeController | UltraSyringePump],
    *,
    update: str = ".5 second",
) -> Generator:
    """Enable DDS controllers or Ultra pumps without starting flow or clearing.

    Parameters
    ----------
    devices : sequence of DDSSyringeController or UltraSyringePump
        Connected communication targets, enabled once each. DDS axes are not
        targets: enabling their controller affects both pumps.
    update : str, optional
        Ultra IOC scan setting; default is ``'.5 second'``. DDS scan settings
        are preserved.

    Yields
    ------
    Msg
        Communication-enable and Ultra update-scan writes.
    """
    selected = dict.fromkeys(devices)
    for device in selected:
        yield from bps.mv(device.communication, "Enabled")
        if isinstance(device, UltraSyringePump):
            yield from bps.mv(device.update_pump, update)


def clear_pumps(pumps: Sequence[Pump]) -> Generator:
    """Clear infused and withdrawn counters on explicitly selected pumps.

    Parameters
    ----------
    pumps : sequence of DDSSyringeAxis or UltraSyringePump
        Connected axes or Ultra pumps to clear, not DDS controllers.

    Yields
    ------
    Msg
        Both counter-clear commands for each selected pump.

    Notes
    -----
    Communication settings, flow, and unselected sibling counters are unchanged.
    """
    for pump in pumps:
        yield from bps.trigger(pump.clear_infused, wait=True)
        yield from bps.trigger(pump.clear_withdrawn, wait=True)


def read_pump_metadata(pumps):
    """Read live pump metadata for a run-start document.

    Parameters
    ----------
    pumps : iterable of DDSSyringeAxis or UltraSyringePump
        Connected pumps in metadata order.

    Returns
    -------
    dict
        Ordered parallel lists keyed by ``pumps``, ``infuse_rate``,
        ``infuse_rate_unit`` and ``pump_status``.

    Yields
    ------
    Msg
        Live rate, rate-unit and status reads.
    """
    result = {"pumps": [], "infuse_rate": [], "infuse_rate_unit": [], "pump_status": []}
    for pump in pumps:
        result["pumps"].append(pump.name)
        result["infuse_rate"].append((yield from bps.rd(pump.read_infuse_rate)))
        result["infuse_rate_unit"].append(
            (yield from bps.rd(pump.read_infuse_rate_unit))
        )
        result["pump_status"].append((yield from bps.rd(pump.status)))
    return result


def wait_for_equilibrium(mixer_pump_list, *, ratio=1, tubing_ID_mm=1.016):  # noqa: N803
    """Wait for the scaled sum of tubing-segment residence times.

    Parameters
    ----------
    mixer_pump_list : sequence of sequences
        Segments shaped as ``['30 cm', pump1, pump2, ...]``. Length units are
        ``mm``, ``cm`` or ``m``; only infusing pumps contribute live flow.
    ratio : float, optional
        Nonnegative residence-time multiplier; default is 1.
    tubing_ID_mm : float, optional
        Positive tubing inner diameter in mm; default is 1.016.

    Returns
    -------
    float
        Total wait duration in seconds, including ``ratio``.

    Yields
    ------
    Msg
        Pump readbacks followed by the residence-time sleep.

    Raises
    ------
    ValueError
        If segments or geometry are invalid, or a segment has no positive flow.
    """
    if (
        not mixer_pump_list
        or not math.isfinite(ratio)
        or ratio < 0
        or not math.isfinite(tubing_ID_mm)
        or tubing_ID_mm <= 0
    ):
        raise ValueError("Nonempty mixer and nonnegative residence geometry required")
    duration = 0.0
    lengths = {"mm": 1, "cm": 10, "m": 1000}
    for length_spec, *pumps in mixer_pump_list:
        length, unit = length_spec.split()
        length_mm = float(length) * lengths[unit]
        if not pumps or not math.isfinite(length_mm) or length_mm < 0:
            raise ValueError("Each mixer segment requires pumps and valid length")
        flow = 0.0
        for pump in pumps:
            if (yield from bps.rd(pump.status)) == "Infusing":
                rate = yield from bps.rd(pump.read_infuse_rate)
                unit = yield from bps.rd(pump.read_infuse_rate_unit)
                flow += rate * rate_unit_converter(unit, "ul/min")
        if not math.isfinite(flow) or flow <= 0:
            raise ValueError("Mixer segment has no positive infusing flow")
        duration += 60 * math.pi * (tubing_ID_mm / 2) ** 2 * length_mm / flow
    yield from bps.sleep(duration * ratio)
    return duration * ratio


def pump_session(
    body_factory: Callable[[], Generator], *, setup: Generator, pumps: list[Pump]
):
    """Run setup and acquisition, then stop tracked pumps on every exit path.

    Parameters
    ----------
    body_factory : callable
        Zero-argument factory producing the acquisition plan after setup.
    setup : generator
        Pump setup plan to execute before acquisition.
    pumps : list of DDSSyringeAxis or UltraSyringePump
        Cleanup targets; setup may append pumps before attempting to start them.

    Returns
    -------
    object
        Acquisition plan's return value after successful cleanup.

    Yields
    ------
    Msg
        Setup, acquisition and unconditional pump-stop messages.

    Raises
    ------
    ExceptionGroup
        If cleanup fails; includes the setup/acquisition error when both fail.
    """
    failure = None

    def body():
        nonlocal failure
        try:
            yield from setup
            return (yield from body_factory())
        except Exception as exc:
            failure = exc
            raise

    def cleanup():
        try:
            yield from stop_pumps(pumps)
        except Exception as exc:
            if failure is not None:
                raise ExceptionGroup(
                    "Pump session and cleanup failures", [failure, exc]
                ) from None
            raise

    return (yield from bpp.finalize_wrapper(body(), cleanup()))
