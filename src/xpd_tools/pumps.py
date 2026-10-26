"""EPICS syringe-pump interfaces and syringe/rate policy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated as A

from ophyd_async.core import SignalR, SignalRW, StandardReadable, TriggerableCommand
from ophyd_async.core import StandardReadableFormat as Format
from ophyd_async.epics.core import EpicsDevice, PvSuffix


class DDSSyringeAxis(StandardReadable, EpicsDevice):
    """Represent one DDS pump axis with axis-local commands and readbacks.

    Parameters
    ----------
    prefix : str, optional
        Axis PV prefix, ending in ``-Ax:A}`` or ``-Ax:B}``.
    with_pvi : bool, optional
        Use PVI discovery instead of static suffixes; default is False.
    name : str, optional
        Device name propagated to its signals; default is empty.
    connector : DeviceConnector, optional
        Prebuilt connector, mutually exclusive with a nonempty prefix.

    Attributes
    ----------
    status : SignalR[str]
        Live axis state, such as ``Idle``, ``Infusing`` or ``Withdrawing``.
    pump_infuse, pump_withdraw, pump_stop : TriggerableCommand
        Start infusion, start withdrawal, or stop this axis.
    clear_infused, clear_withdrawn : TriggerableCommand
        Reset only this axis's cumulative volume counters.
    target_vol, infuse_rate, withdraw_rate : SignalRW[float]
        Target volume and directional rate setpoints.
    target_vol_unit, infuse_rate_unit, withdraw_rate_unit : SignalRW[str]
        Units of the corresponding setpoints.
    read_target_vol, read_infused, read_withdrawn : SignalR[float]
        Live target volume and cumulative directional volumes.
    read_target_vol_unit, read_infused_unit, read_withdrawn_unit : SignalR[str]
        Units of the corresponding volume readbacks.
    read_infuse_rate, read_withdraw_rate : SignalR[float]
        Live directional rates; infusion rate is the hinted reading.
    read_infuse_rate_unit, read_withdraw_rate_unit : SignalR[str]
        Units of the corresponding rate readbacks.
    max_infuse, max_withdraw, min_infuse, min_withdraw : SignalRW[float]
        Directional rate-limit setpoints.
    set_infuse_range, set_withdraw_range : TriggerableCommand
        Refresh limits for the syringe selected on the controller panel.
    read_max_infuse, read_max_withdraw : SignalR[float]
        Live maximum directional rates.
    read_max_infuse_unit, read_max_withdraw_unit : SignalR[str]
        Units of the maximum rate readbacks.
    read_min_infuse, read_min_withdraw : SignalR[float]
        Live minimum directional rates.
    read_min_infuse_unit, read_min_withdraw_unit : SignalR[str]
        Units of the minimum rate readbacks.
    LIM : SignalRW[str]
        IOC infusion-rate limit enable setting.
    target_time, force : SignalRW[float]
        Target time and force setpoints.
    read_target_time, read_force : SignalR[float]
        Live target time and force readbacks.
    read_force_unit : SignalR[str]
        Units of the force readback.

    Notes
    -----
    Normally created as ``DDSSyringeController.p1`` or ``p2``; communication
    settings belong to that parent, not to either axis.
    """

    status: A[SignalR[str], PvSuffix("STATUS"), Format.UNCACHED_SIGNAL]
    pump_infuse: A[TriggerableCommand, PvSuffix("IRUN")]
    pump_withdraw: A[TriggerableCommand, PvSuffix("WRUN")]
    pump_stop: A[TriggerableCommand, PvSuffix("STOP")]
    target_vol: A[SignalRW[float], PvSuffix("TVOLUME"), Format.CONFIG_SIGNAL]
    target_vol_unit: A[SignalRW[str], PvSuffix("TVOLUME:EGU"), Format.CONFIG_SIGNAL]
    read_target_vol: A[SignalR[str], PvSuffix("TVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_target_vol_unit: A[
        SignalR[str], PvSuffix("TVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    clear_infused: A[TriggerableCommand, PvSuffix("CIVOLUME")]
    clear_withdrawn: A[TriggerableCommand, PvSuffix("CWVOLUME")]
    read_infused: A[SignalR[float], PvSuffix("IVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_withdrawn: A[SignalR[float], PvSuffix("WVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_infused_unit: A[
        SignalR[str], PvSuffix("IVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_withdrawn_unit: A[
        SignalR[str], PvSuffix("WVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    infuse_rate: A[SignalRW[float], PvSuffix("IRATE"), Format.CONFIG_SIGNAL]
    infuse_rate_unit: A[SignalRW[str], PvSuffix("IRATE:EGU"), Format.CONFIG_SIGNAL]
    withdraw_rate: A[SignalRW[float], PvSuffix("WRATE"), Format.CONFIG_SIGNAL]
    withdraw_rate_unit: A[SignalRW[str], PvSuffix("WRATE:EGU"), Format.CONFIG_SIGNAL]
    read_infuse_rate: A[
        SignalR[float], PvSuffix("IRATE:RBV"), Format.HINTED_UNCACHED_SIGNAL
    ]
    read_infuse_rate_unit: A[
        SignalR[str], PvSuffix("IRATE:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_withdraw_rate: A[SignalR[float], PvSuffix("WRATE:RBV"), Format.UNCACHED_SIGNAL]
    read_withdraw_rate_unit: A[
        SignalR[str], PvSuffix("WRATE:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    max_infuse: A[SignalRW[float], PvSuffix("IRATE:MAX"), Format.CONFIG_SIGNAL]
    max_withdraw: A[SignalRW[float], PvSuffix("WRATE:MAX"), Format.CONFIG_SIGNAL]
    min_infuse: A[SignalRW[float], PvSuffix("IRATE:MIN"), Format.CONFIG_SIGNAL]
    min_withdraw: A[SignalRW[float], PvSuffix("WRATE:MIN"), Format.CONFIG_SIGNAL]
    set_infuse_range: A[TriggerableCommand, PvSuffix("IRATE:LIM.PROC")]
    set_withdraw_range: A[TriggerableCommand, PvSuffix("WRATE:LIM.PROC")]
    read_max_infuse: A[
        SignalR[float], PvSuffix("IRATE:MAX:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_max_infuse_unit: A[
        SignalR[str], PvSuffix("IRATE:MAX:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_max_withdraw: A[
        SignalR[float], PvSuffix("WRATE:MAX:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_max_withdraw_unit: A[
        SignalR[str], PvSuffix("WRATE:MAX:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_min_infuse: A[
        SignalR[float], PvSuffix("IRATE:MIN:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_min_infuse_unit: A[
        SignalR[str], PvSuffix("IRATE:MIN:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_min_withdraw: A[
        SignalR[float], PvSuffix("WRATE:MIN:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_min_withdraw_unit: A[
        SignalR[str], PvSuffix("WRATE:MIN:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    # TODO: make lowercase
    LIM: A[SignalRW[str], PvSuffix("IRATE-LIM:ENABLE"), Format.CONFIG_SIGNAL]
    target_time: A[SignalRW[float], PvSuffix("TTIME"), Format.CONFIG_SIGNAL]
    read_target_time: A[SignalR[str], PvSuffix("TTIME:RBV"), Format.UNCACHED_SIGNAL]
    force: A[SignalRW[float], PvSuffix("FORCE"), Format.CONFIG_SIGNAL]
    read_force: A[SignalR[float], PvSuffix("FORCE:RBV"), Format.UNCACHED_SIGNAL]
    read_force_unit: A[SignalR[str], PvSuffix("FORCE:RBV:EGU"), Format.UNCACHED_SIGNAL]


class DDSSyringeController(EpicsDevice):
    """Represent a DDS controller owning two independently usable pump axes.

    Parameters
    ----------
    prefix : str, optional
        Controller PV prefix before ``}`` or either ``-Ax:`` suffix.
    with_pvi : bool, optional
        Use PVI discovery instead of static suffixes; default is False.
    name : str, optional
        Device name propagated to both axes; default is empty.
    connector : DeviceConnector, optional
        Prebuilt connector, mutually exclusive with a nonempty prefix.

    Attributes
    ----------
    p1, p2 : DDSSyringeAxis
        Owned IOC axes A and B, respectively.
    communication : SignalRW[str]
        Shared communication enable/disable setting.
    update_pump : SignalRW[str]
        Shared IOC update-scan setting.
    set_condition : SignalRW[str]
        Shared condition setpoint.
    show_condition : SignalR[str]
        Shared condition readback.

    Notes
    -----
    Connecting the controller connects both axes. Shared signals affect both
    pumps; flow and counter-clear commands remain axis-local.
    """

    p1: A[DDSSyringeAxis, PvSuffix("-Ax:A}")]
    p2: A[DDSSyringeAxis, PvSuffix("-Ax:B}")]
    communication: A[SignalRW[str], PvSuffix("}DISABLE")]
    update_pump: A[SignalRW[str], PvSuffix("}UPDATE.SCAN")]
    set_condition: A[SignalRW[str], PvSuffix("}CONDITION")]
    show_condition: A[SignalR[str], PvSuffix("}CONDITION:RBV")]


class UltraSyringePump(StandardReadable, EpicsDevice):
    """Represent an Ultra pump with per-pump communication and flow control.

    Parameters
    ----------
    prefix : str, optional
        Pump PV prefix, including the trailing ``}:`` separator.
    with_pvi : bool, optional
        Use PVI discovery instead of static suffixes; default is False.
    name : str, optional
        Device name propagated to its signals; default is empty.
    connector : DeviceConnector, optional
        Prebuilt connector, mutually exclusive with a nonempty prefix.

    Attributes
    ----------
    status : SignalR[str]
        Live pump state, such as ``Idle``, ``Infusing`` or ``Withdrawing``.
    communication, update_pump : SignalRW[str]
        Per-pump communication and IOC update-scan settings.
    pump_infuse, pump_withdraw, pump_stop : TriggerableCommand
        Start infusion, start withdrawal, or stop this pump.
    clear_infused, clear_withdrawn : TriggerableCommand
        Reset this pump's cumulative volume counters.
    target_vol, infuse_rate, withdraw_rate : SignalRW[float]
        Target volume and directional rate setpoints.
    target_vol_unit, infuse_rate_unit, withdraw_rate_unit : SignalRW[str]
        Units of the corresponding setpoints.
    read_target_vol, read_infused, read_withdrawn : SignalR[float]
        Live target volume and cumulative directional volumes.
    read_target_vol_unit, read_infused_unit, read_withdrawn_unit : SignalR[str]
        Units of the corresponding volume readbacks.
    read_infuse_rate, read_withdraw_rate : SignalR[float]
        Live directional rates; infusion rate is the hinted reading.
    read_infuse_rate_unit, read_withdraw_rate_unit : SignalR[str]
        Units of the corresponding rate readbacks.
    target_time, force : SignalRW[float]
        Target time and force setpoints.
    read_target_time, read_force : SignalR[float]
        Live target time and force readbacks.
    diameter : SignalR[float]
        Syringe diameter reported by the IOC.

    Notes
    -----
    Syringe size and material must be confirmed on the panel; the IOC provides
    no syringe-size feedback for configuration validation.
    """

    status: A[SignalR[str], PvSuffix("STATUS"), Format.UNCACHED_SIGNAL]
    communication: A[SignalRW[str], PvSuffix("DISABLE"), Format.CONFIG_SIGNAL]
    update_pump: A[SignalRW[str], PvSuffix("UPDATE.SCAN"), Format.CONFIG_SIGNAL]
    pump_infuse: A[TriggerableCommand, PvSuffix("IRUN")]
    pump_withdraw: A[TriggerableCommand, PvSuffix("WRUN")]
    pump_stop: A[TriggerableCommand, PvSuffix("STOP")]
    target_vol: A[SignalRW[float], PvSuffix("TVOLUME"), Format.CONFIG_SIGNAL]
    target_vol_unit: A[SignalRW[str], PvSuffix("TVOLUMEUNITS"), Format.CONFIG_SIGNAL]
    read_target_vol: A[SignalR[str], PvSuffix("TVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_target_vol_unit: A[
        SignalR[str], PvSuffix("TVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    clear_infused: A[TriggerableCommand, PvSuffix("CLEARINFUSED")]
    clear_withdrawn: A[TriggerableCommand, PvSuffix("CLEARWITHDRAWN")]
    read_infused: A[SignalR[float], PvSuffix("IVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_withdrawn: A[SignalR[float], PvSuffix("WVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_infused_unit: A[
        SignalR[str], PvSuffix("IVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_withdrawn_unit: A[
        SignalR[str], PvSuffix("WVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    infuse_rate: A[SignalRW[float], PvSuffix("IRATE"), Format.CONFIG_SIGNAL]
    infuse_rate_unit: A[SignalRW[str], PvSuffix("IRATEUNITS"), Format.CONFIG_SIGNAL]
    withdraw_rate: A[SignalRW[float], PvSuffix("WRATE"), Format.CONFIG_SIGNAL]
    withdraw_rate_unit: A[SignalRW[str], PvSuffix("WRATEUNITS"), Format.CONFIG_SIGNAL]
    read_infuse_rate: A[
        SignalR[float], PvSuffix("IRATE:RBV"), Format.HINTED_UNCACHED_SIGNAL
    ]
    read_infuse_rate_unit: A[
        SignalR[str], PvSuffix("IRATEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_withdraw_rate: A[SignalR[float], PvSuffix("WRATE:RBV"), Format.UNCACHED_SIGNAL]
    read_withdraw_rate_unit: A[
        SignalR[str], PvSuffix("WRATEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    target_time: A[SignalRW[float], PvSuffix("TTIME"), Format.CONFIG_SIGNAL]
    read_target_time: A[SignalR[str], PvSuffix("TTIME:RBV"), Format.UNCACHED_SIGNAL]
    force: A[SignalRW[float], PvSuffix("FORCE"), Format.CONFIG_SIGNAL]
    read_force: A[SignalR[float], PvSuffix("FORCE:RBV"), Format.UNCACHED_SIGNAL]
    diameter: A[SignalR[float], PvSuffix("DIAMETER"), Format.CONFIG_SIGNAL]


_DDS_RANGES: Mapping[str, Mapping[float, tuple[float, float]]] = MappingProxyType(
    {
        "steel": MappingProxyType(
            {
                2.5: (2.264, 2.351),
                8: (8.728, 9.064),
                20: (35.21, 36.56),
                50: (78.69, 81.72),
            }
        ),
        "glass_H1000": MappingProxyType(
            {
                1: (2.043, 2.121),
                1.25: (2.553, 2.651),
                2.5: (5.106, 5.302),
                5: (10.21, 10.6),
                10: (20.41, 21.2),
                25: (51.04, 53),
                50: (102.1, 106),
            }
        ),
        "plastic_BD": MappingProxyType(
            {
                1: (2.124, 2.206),
                3: (7.09, 7.363),
                5: (13.83, 14.36),
                10: (20.02, 20.79),
                20: (34.91, 36.26),
                30: (44.84, 46.57),
                50: (68.04, 70.66),
            }
        ),
    }
)
_ULTRA_RANGES: Mapping[str, Mapping[float, tuple[float, float]]] = MappingProxyType(
    {
        "steel": MappingProxyType(
            {
                2.5: (3.39588, 3.5265),
                8: (13.0924, 13.596),
                20: (52.8105, 54.8417),
                50: (118.038, 122.578),
                100: (175.769, 182.529),
            }
        ),
        "glass_H1000": MappingProxyType(
            {
                1: (3.06414, 3.18204),
                1.25: (3.82884, 3.97616),
                2.5: (7.65858, 7.95317),
                5: (15.3096, 15.8985),
                10: (30.6218, 31.7996),
                25: (76.5582, 79.5028),
                50: (153.111, 159.0),
                100: (153.111, 159.0),
            }
        ),
        "plastic_BD": MappingProxyType(
            {
                1: (3.18636, 3.30896),
                3: (10.6358, 11.0449),
                5: (20.7422, 21.5401),
                10: (30.036, 31.1913),
                20: (52.3697, 54.384),
                30: (67.2661, 69.8532),
                50: (102.061, 105.986),
                60: (102.061, 105.986),
            }
        ),
    }
)

_VOLUME_ML = MappingProxyType({"pl": 1e-9, "nl": 1e-6, "ul": 1e-3, "ml": 1.0})
_TIME_MIN = MappingProxyType({"sec": 1 / 60, "min": 1.0, "hr": 60.0})


def vol_unit_converter(v0: str, v1: str) -> float:
    """Return a multiplicative volume-unit conversion factor.

    Parameters
    ----------
    v0 : {'pl', 'nl', 'ul', 'ml'}
        Source volume unit.
    v1 : {'pl', 'nl', 'ul', 'ml'}
        Destination volume unit.

    Returns
    -------
    float
        Factor multiplying a value in ``v0`` to obtain a value in ``v1``.

    Raises
    ------
    KeyError
        If either unit is unsupported.
    """
    return _VOLUME_ML[v0] / _VOLUME_ML[v1]


def rate_unit_converter(r0: str, r1: str) -> float:
    """Return a multiplicative volume/time-unit conversion factor.

    Parameters
    ----------
    r0 : str
        Source rate unit: ``pl``, ``nl``, ``ul`` or ``ml`` per ``sec``, ``min``
        or ``hr``, separated by ``/``.
    r1 : str
        Destination rate unit in the same form.

    Returns
    -------
    float
        Factor multiplying a value in ``r0`` to obtain a value in ``r1``.

    Raises
    ------
    KeyError
        If a volume or time unit is unsupported.
    ValueError
        If a rate unit does not contain exactly one ``/`` separator.
    """
    v0, t0 = r0.split("/")
    v1, t1 = r1.split("/")
    return vol_unit_converter(v0, v1) * _TIME_MIN[t1] / _TIME_MIN[t0]


def rate_limits(
    pump: DDSSyringeAxis | UltraSyringePump,
    syringe_material: str,
    syringe_size_ml: float,
) -> tuple[float, float]:
    """Return theoretical rate limits for a pump and syringe.

    Parameters
    ----------
    pump : DDSSyringeAxis or UltraSyringePump
        Pump selecting the DDS or Ultra lookup table.
    syringe_material : {'steel', 'glass_H1000', 'plastic_BD'}
        Syringe material/model identifier.
    syringe_size_ml : float
        Nominal syringe capacity in mL.

    Returns
    -------
    minimum : float
        Minimum rate in nL/min.
    maximum : float
        Maximum rate in mL/min.

    Raises
    ------
    ValueError
        If the material/size combination is not tabulated.
    """
    table = _DDS_RANGES if isinstance(pump, DDSSyringeAxis) else _ULTRA_RANGES
    try:
        return table[syringe_material][syringe_size_ml]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported {syringe_material} syringe size {syringe_size_ml} mL"
        ) from exc


def validate_pump_settings(
    pump: DDSSyringeAxis | UltraSyringePump,
    syringe_material: str,
    syringe_size_ml: float,
    target_ml: float | None,
    rate_ul_min: float,
) -> None:
    """Reject settings outside the syringe's theoretical capacity and rate limits.

    Parameters
    ----------
    pump : DDSSyringeAxis or UltraSyringePump
        Pump selecting the DDS or Ultra lookup table.
    syringe_material : {'steel', 'glass_H1000', 'plastic_BD'}
        Syringe material/model identifier.
    syringe_size_ml : float
        Nominal syringe capacity in mL.
    target_ml : float or None
        Target volume in mL; None skips target-volume validation.
    rate_ul_min : float
        Nonnegative rate in uL/min; zero bypasses the rate limits.

    Raises
    ------
    ValueError
        If the syringe is unsupported or a setting is nonfinite or out of range.
    """
    minimum, maximum = rate_limits(pump, syringe_material, syringe_size_ml)
    if target_ml is not None and (
        not math.isfinite(target_ml) or target_ml < 0 or target_ml > syringe_size_ml
    ):
        raise ValueError(
            f"Target {target_ml} mL exceeds syringe capacity {syringe_size_ml} mL"
        )
    if not math.isfinite(rate_ul_min) or rate_ul_min < 0:
        raise ValueError(f"Invalid pump rate {rate_ul_min} ul/min")
    if rate_ul_min and not (
        minimum * rate_unit_converter("nl/min", "ul/min")
        <= rate_ul_min
        <= maximum * rate_unit_converter("ml/min", "ul/min")
    ):
        raise ValueError(f"Rate {rate_ul_min} ul/min outside syringe range")
