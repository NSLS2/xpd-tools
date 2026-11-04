"""Regular-ophyd pump device classes for the 28-ID-2 halide rig.

Ported from xpd-profile-collection startup scripts 26-pump_ultra.py and
27-pump_dds.py, retaining only the device definition and the plan-level
methods (``set_infuse2``, ``infuse_pump2``, ``stop_pump2``) that
``plans/runtime.py`` calls.  Helper functions (vol_unit_converter, etc.)
that are only used internally are kept private.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from bluesky import plan_stubs as bps
from ophyd import Component as Cpt
from ophyd import Device, EpicsSignal, EpicsSignalRO

# ---------------------------------------------------------------------------
# Unit helpers (private)
# ---------------------------------------------------------------------------


def _vol_unit_converter(v0: str = "ul", v1: str = "ml") -> float:
    """Return the scalar that converts a volume in units *v0* to *v1*."""
    vol_unit = ["pl", "nl", "ul", "ml"]
    vol_frame = pd.DataFrame(
        data={
            "pl": np.geomspace(1, 1e9, num=4),
            "nl": np.geomspace(1e-3, 1e6, num=4),
            "ul": np.geomspace(1e-6, 1e3, num=4),
            "ml": np.geomspace(1e-9, 1, num=4),
        },
        index=vol_unit,
    )
    return float(vol_frame.loc[v0, v1])


def _rate_unit_converter(r0: str = "ul/min", r1: str = "ul/min") -> float:
    """Return the scalar that converts a flow-rate in units *r0* to *r1*."""
    vol_part = _vol_unit_converter(v0=r0.split("/")[0], v1=r1.split("/")[0])
    t_unit = ["sec", "min", "hr"]
    t_frame = pd.DataFrame(
        data={
            "sec": np.geomspace(1, 3600, num=3),
            "min": np.geomspace(1 / 60, 60, num=3),
            "hr": np.geomspace(1 / 3600, 1, num=3),
        },
        index=t_unit,
    )
    t_part = float(t_frame.loc[r0.split("/")[1], r1.split("/")[1]])
    return vol_part / t_part


# ---------------------------------------------------------------------------
# Harvard DDS pump
# ---------------------------------------------------------------------------

class SyrngDDS(Device):
    """Top-level DDS controller (communication enable/scan rate).

    Instantiate one per physical DDS unit; pass as *parent* to each
    ``SyrngDDSAxis`` axis that belongs to it.
    """

    communication = Cpt(EpicsSignal, "}DISABLE", string=True, kind="config")
    update_pump = Cpt(EpicsSignal, "}UPDATE.SCAN", string=True, kind="config")
    set_condition = Cpt(EpicsSignal, "}CONDITION", string=True, kind="config")
    show_condition = Cpt(EpicsSignalRO, "}CONDITION:RBV", string=True, kind="config")


class SyrngDDSAxis(Device):
    """One axis (syringe channel) of a Harvard DDS pump.

    Exposes the plan API that ``plans/runtime.py`` calls:
        - ``set_infuse2``  -- configure syringe size, target volume and rate
        - ``infuse_pump2`` -- start infusion (plan stub)
        - ``stop_pump2``   -- stop pump (plan stub)

    The *parent* keyword should be the ``SyrngDDS`` controller device so
    that ``communication`` is accessible via ``self.parent.communication``.
    """

    status = Cpt(EpicsSignalRO, "STATUS", string=True)

    pump_infuse = Cpt(EpicsSignal, "IRUN")
    pump_withdraw = Cpt(EpicsSignal, "WRUN")
    pump_stop = Cpt(EpicsSignal, "STOP")

    target_vol = Cpt(EpicsSignal, "TVOLUME", kind="config")
    target_vol_unit = Cpt(
        EpicsSignal,
        "TVOLUME:EGU",
        string=True,
        kind="config"
    )
    read_target_vol = Cpt(EpicsSignalRO, "TVOLUME:RBV", kind="config")
    read_target_vol_unit = Cpt(
        EpicsSignalRO,
        "TVOLUME:RBV:EGU",
        string=True,
        kind="config"
    )

    clear_infused = Cpt(EpicsSignal, "CIVOLUME")
    clear_withdrawn = Cpt(EpicsSignal, "CWVOLUME")

    read_infused = Cpt(EpicsSignalRO, "IVOLUME:RBV")
    read_withdrawn = Cpt(EpicsSignalRO, "WVOLUME:RBV")
    read_infused_unit = Cpt(EpicsSignalRO, "IVOLUME:RBV:EGU", string=True)
    read_withdrawn_unit = Cpt(EpicsSignalRO, "WVOLUME:RBV:EGU", string=True)

    infuse_rate = Cpt(EpicsSignal, "IRATE", kind="hinted")
    infuse_rate_unit = Cpt(
        EpicsSignal,
        "IRATE:EGU",
        string=True,
        kind="hinted"
    )
    withdraw_rate = Cpt(EpicsSignal, "WRATE", kind="hinted")
    withdraw_rate_unit = Cpt(
        EpicsSignal,
        "WRATE:EGU",
        string=True,
        kind="hinted"
    )

    read_infuse_rate = Cpt(EpicsSignalRO, "IRATE:RBV", kind="hinted")
    read_infuse_rate_unit = Cpt(
        EpicsSignalRO,
        "IRATE:RBV:EGU",
        string=True,
        kind="hinted"
    )
    read_withdraw_rate = Cpt(EpicsSignalRO, "WRATE:RBV", kind="hinted")
    read_withdraw_rate_unit = Cpt(
        EpicsSignalRO,
        "WRATE:RBV:EGU",
        string=True,
        kind="hinted"
    )

    max_infuse = Cpt(EpicsSignal, "IRATE:MAX", kind="hinted")
    max_withdraw = Cpt(EpicsSignal, "WRATE:MAX", kind="hinted")
    min_infuse = Cpt(EpicsSignal, "IRATE:MIN", kind="hinted")
    min_withdraw = Cpt(EpicsSignal, "WRATE:MIN", kind="hinted")

    set_infuse_range = Cpt(EpicsSignal, "IRATE:LIM.PROC", kind="hinted")
    set_withdraw_range = Cpt(EpicsSignal, "WRATE:LIM.PROC", kind="hinted")

    read_max_infuse = Cpt(EpicsSignalRO, "IRATE:MAX:RBV", kind="hinted")
    read_max_infuse_unit = Cpt(
        EpicsSignalRO,
        "IRATE:MAX:RBV:EGU",
        string=True,
        kind="hinted"
    )
    read_min_infuse = Cpt(EpicsSignalRO, "IRATE:MIN:RBV", kind="hinted")
    read_min_infuse_unit = Cpt(
        EpicsSignalRO,
        "IRATE:MIN:RBV:EGU",
        string=True,
        kind="hinted"
    )

    target_time = Cpt(EpicsSignal, "TTIME", kind="hinted")
    read_target_time = Cpt(EpicsSignalRO, "TTIME:RBV", kind="hinted")

    force = Cpt(EpicsSignal, "FORCE", kind="hinted")
    read_force = Cpt(EpicsSignalRO, "FORCE:RBV", kind="hinted")

    # ------------------------------------------------------------------
    # Per-syringe min/max rate tables (copied verbatim from startup script)
    # ------------------------------------------------------------------

    _RATE_TABLES: dict[str, dict] = {
        "steel": {
            "min_vol": {
                "2.264nl/min": 2.5,
                "8.728nl/min": 8,
                "35.21nl/min": 20,
                "78.69nl/min": 50,
            },
            "range": {
                2.5: [2.264, 2.351],
                8: [8.728, 9.064],
                20: [35.21, 36.56],
                50: [78.69, 81.72],
            },
        },
        "glass_H1000": {
            "min_vol": {
                "2.043nl/min": 1,
                "2.553nl/min": 1.25,
                "5.106nl/min": 2.5,
                "10.21nl/min": 5,
                "20.41nl/min": 10,
                "51.04nl/min": 25,
                "102.1nl/min": 50,
            },
            "range": {
                1: [2.043, 2.121],
                1.25: [2.553, 2.651],
                2.5: [5.106, 5.302],
                5: [10.21, 10.6],
                10: [20.41, 21.2],
                25: [51.04, 53.0],
                50: [102.1, 106.0],
            },
        },
        "plastic_BD": {
            "min_vol": {
                "2.124nl/min": 1,
                "7.09nl/min": 3,
                "13.83nl/min": 5,
                "20.02nl/min": 10,
                "34.91nl/min": 20,
                "44.84nl/min": 30,
                "68.04nl/min": 50,
            },
            "range": {
                1: [2.124, 2.206],
                3: [7.09, 7.363],
                5: [13.83, 14.36],
                10: [20.02, 20.79],
                20: [34.91, 36.26],
                30: [44.84, 46.57],
                50: [68.04, 70.66],
            },
        },
    }

    def _vol_rate_table(self, syringe_material: str = "steel"):
        table = self._RATE_TABLES.get(syringe_material)
        if table is None:
            raise ValueError(
                f"Unknown syringe material {syringe_material!r}. "
                f"Known: {list(self._RATE_TABLES)}"
            )
        return table["min_vol"], table["range"]

    # ------------------------------------------------------------------
    # Plan API called by plans/runtime.py
    # ------------------------------------------------------------------

    def set_infuse2(
        self,
        input_size: float,
        syringe_material: str = "steel",
        clear: bool = False,
        set_target: bool = True,
        target_vol: float = 20,
        target_unit: str = "ml",
        infuse_rate: float = 100,
        infuse_unit: str = "ul/min",
    ):
        """Configure syringe size, target volume, and infusion rate.

        This is a Bluesky plan: iterate it with ``yield from``.
        """
        if clear:
            yield from bps.mv(self.clear_infused, 1, self.clear_withdrawn, 1)

        yield from bps.mv(self.set_infuse_range, 1, self.set_withdraw_range, 1)

        _, rate_range = self._vol_rate_table(syringe_material)
        if input_size not in rate_range:
            raise ValueError(
                f"Syringe size {input_size} mL not in \
                    rate table for {syringe_material!r}. "
                f"Valid sizes: {list(rate_range)}"
            )
        size = input_size

        if set_target:
            c = _vol_unit_converter(v0=target_unit, v1="ml")
            if target_vol * c > size:
                raise ValueError(
                    f"Target volume {target_vol} {target_unit} \
                        ({target_vol * c:.3f} mL) "
                    f"exceeds syringe size {size} mL."
                )
            yield from bps.abs_set(self.target_vol_unit, target_unit, wait=True)
            yield from bps.abs_set(self.target_vol, target_vol, wait=True)

        min_rate, max_rate = rate_range[size]
        const_max = _rate_unit_converter(r0=infuse_unit, r1="ml/min")
        const_min = _rate_unit_converter(r0=infuse_unit, r1="nl/min")

        if infuse_rate * const_max > max_rate:
            raise ValueError(
                f"Infuse rate {infuse_rate} {infuse_unit} \
                    ({infuse_rate * const_max:.3f} ml/min) "
                f"exceeds maximum {max_rate} ml/min for {size} mL \
                    {syringe_material} syringe."
            )
        if infuse_rate * const_min < min_rate:
            raise ValueError(
                f"Infuse rate {infuse_rate} {infuse_unit}\
                    ({infuse_rate * const_min:.3f} nl/min) "
                f"below minimum {min_rate} nl/min for\
                    {size} mL {syringe_material} syringe."
            )

        yield from bps.abs_set(self.infuse_rate_unit, infuse_unit, wait=True)
        yield from bps.abs_set(self.infuse_rate, infuse_rate, wait=True)
        yield from bps.sleep(1)

    def infuse_pump2(self, clear: bool = False):
        """Start infusion. Bluesky plan."""
        if clear:
            yield from bps.mv(self.clear_infused, 1, self.clear_withdrawn, 1)
        yield from bps.abs_set(self.pump_infuse, 1, wait=True)
        yield from bps.sleep(1)

    def stop_pump2(self, clear: bool = False):
        """Stop pump. Bluesky plan."""
        yield from bps.abs_set(self.pump_stop, 1, wait=True)
        if clear:
            yield from bps.mv(self.clear_infused, 1, self.clear_withdrawn, 1)
        yield from bps.sleep(1)


# ---------------------------------------------------------------------------
# Harvard Ultra pump
# ---------------------------------------------------------------------------

class SyrngUltra(Device):
    """Harvard Ultra syringe pump.

    Exposes the same plan API as ``SyrngDDSAxis``:
        - ``set_infuse2``
        - ``infuse_pump2``
        - ``stop_pump2``
    """

    status = Cpt(EpicsSignalRO, "STATUS", string=True)
    communication = Cpt(
        EpicsSignal,
        "DISABLE",
        string=True,
        kind="config"
    )
    update_pump = Cpt(
        EpicsSignal,
        "UPDATE.SCAN",
        string=True,
        kind="config")

    pump_infuse = Cpt(EpicsSignal, "IRUN")
    pump_withdraw = Cpt(EpicsSignal, "WRUN")
    pump_stop = Cpt(EpicsSignal, "STOP")

    target_vol = Cpt(EpicsSignal, "TVOLUME", kind="config")
    target_vol_unit = Cpt(
        EpicsSignal,
        "TVOLUMEUNITS",
        string=True,
        kind="config"
    )
    read_target_vol = Cpt(EpicsSignalRO, "TVOLUME:RBV", kind="config")
    read_target_vol_unit = Cpt(
        EpicsSignalRO,
        "TVOLUMEUNITS:RBV",
        string=True,
        kind="config"
    )

    clear_infused = Cpt(EpicsSignal, "CLEARINFUSED")
    clear_withdrawn = Cpt(EpicsSignal, "CLEARWITHDRAWN")

    read_infused = Cpt(EpicsSignalRO, "IVOLUME:RBV")
    read_withdrawn = Cpt(EpicsSignalRO, "WVOLUME:RBV")
    read_infused_unit = Cpt(EpicsSignalRO, "IVOLUMEUNITS:RBV", string=True)
    read_withdrawn_unit = Cpt(EpicsSignalRO, "WVOLUMEUNITS:RBV", string=True)

    infuse_rate = Cpt(EpicsSignal, "IRATE", kind="hinted")
    infuse_rate_unit = Cpt(
        EpicsSignal,
        "IRATEUNITS",
        string=True,
        kind="hinted"
    )
    withdraw_rate = Cpt(EpicsSignal, "WRATE", kind="hinted")
    withdraw_rate_unit = Cpt(
        EpicsSignal,
        "WRATEUNITS",
        string=True,
        kind="hinted"
    )

    read_infuse_rate = Cpt(EpicsSignalRO, "IRATE:RBV", kind="hinted")
    read_infuse_rate_unit = Cpt(
        EpicsSignalRO,
        "IRATEUNITS:RBV",
        string=True,
        kind="hinted"
    )
    read_withdraw_rate = Cpt(EpicsSignalRO, "WRATE:RBV", kind="hinted")
    read_withdraw_rate_unit = Cpt(
        EpicsSignalRO,
        "WRATEUNITS:RBV",
        string=True,
        kind="hinted"
    )

    target_time = Cpt(EpicsSignal, "TTIME", kind="hinted")
    read_target_time = Cpt(EpicsSignalRO, "TTIME:RBV", kind="hinted")

    force = Cpt(EpicsSignal, "FORCE", kind="hinted")
    read_force = Cpt(EpicsSignalRO, "FORCE:RBV", kind="hinted")

    diameter = Cpt(EpicsSignalRO, "DIAMETER", kind="config")

    # ------------------------------------------------------------------
    # Per-syringe min/max rate tables
    # ------------------------------------------------------------------

    _RATE_TABLES: dict[str, dict] = {
        "steel": {
            "range": {
                2.5: [3.39588, 3.5265],
                8: [13.0924, 13.596],
                20: [52.8105, 54.8417],
                50: [118.038, 122.578],
                100: [175.769, 182.529],
            },
        },
        "glass_H1000": {
            "range": {
                1: [3.06414, 3.18204],
                1.25: [3.82884, 3.97616],
                2.5: [7.65858, 7.95317],
                5: [15.3096, 15.8985],
                10: [30.6218, 31.7996],
                25: [76.5582, 79.5028],
                50: [153.111, 159.0],
                100: [153.111, 159.0],
            },
        },
        "plastic_BD": {
            "range": {
                1: [3.18636, 3.30896],
                3: [10.6358, 11.0449],
                5: [20.7422, 21.5401],
                10: [30.036, 31.1913],
                20: [52.3697, 54.384],
                30: [67.2661, 69.8532],
                50: [102.061, 105.986],
                60: [102.061, 105.986],
            },
        },
    }

    def _vol_rate_table(self, syringe_material: str = "steel"):
        table = self._RATE_TABLES.get(syringe_material)
        if table is None:
            raise ValueError(
                f"Unknown syringe material {syringe_material!r}. "
                f"Known: {list(self._RATE_TABLES)}"
            )
        return table["range"]

    # ------------------------------------------------------------------
    # Plan API called by plans/runtime.py
    # ------------------------------------------------------------------

    def set_infuse2(
        self,
        input_size: float,
        syringe_material: str = "steel",
        clear: bool = False,
        set_target: bool = True,
        target_vol: float = 20,
        target_unit: str = "ml",
        infuse_rate: float = 100,
        infuse_unit: str = "ul/min",
    ):
        """Configure syringe size, target volume, and infusion rate.

        This is a Bluesky plan: iterate it with ``yield from``.
        """
        if clear:
            yield from bps.mv(self.clear_infused, 1, self.clear_withdrawn, 1)

        if self.communication.get() == "Disabled":
            yield from bps.mv(self.communication, 0, self.update_pump, 6)

        size = input_size
        rate_range = self._vol_rate_table(syringe_material)

        if set_target:
            c = _vol_unit_converter(v0=target_unit, v1="ml")
            if target_vol * c > size:
                raise ValueError(
                    f"Target volume {target_vol} {target_unit} \
                        ({target_vol * c:.3f} mL) "
                    f"exceeds syringe size {size} mL."
                )
            yield from bps.abs_set(self.target_vol_unit, target_unit, wait=True)
            yield from bps.abs_set(self.target_vol, target_vol, wait=True)

        if size not in rate_range:
            raise ValueError(
                f"Syringe size {size} mL not in rate table for {syringe_material!r}. "
                f"Valid sizes: {list(rate_range)}"
            )
        min_rate, max_rate = rate_range[size]
        const_max = _rate_unit_converter(r0=infuse_unit, r1="ml/min")
        const_min = _rate_unit_converter(r0=infuse_unit, r1="nl/min")

        if infuse_rate * const_max > max_rate:
            raise ValueError(
                f"Infuse rate {infuse_rate} {infuse_unit}\
                    ({infuse_rate * const_max:.3f} ml/min) "
                f"exceeds maximum {max_rate} ml/min for {size} mL\
                    {syringe_material} syringe."
            )
        if infuse_rate * const_min < min_rate:
            raise ValueError(
                f"Infuse rate {infuse_rate} {infuse_unit}\
                    ({infuse_rate * const_min:.3f} nl/min) "
                f"below minimum {min_rate} nl/min for {size}\
                    mL {syringe_material} syringe."
            )

        yield from bps.abs_set(self.infuse_rate_unit, infuse_unit, wait=True)
        yield from bps.abs_set(self.infuse_rate, infuse_rate, wait=True)
        yield from bps.sleep(1)

    def infuse_pump2(self, clear: bool = False):
        """Start infusion. Bluesky plan."""
        if clear:
            yield from bps.abs_set(self.clear_infused, 1, wait=True)
            yield from bps.abs_set(self.clear_withdrawn, 1, wait=True)
        yield from bps.abs_set(self.pump_infuse, 1, wait=True)
        yield from bps.sleep(1)

    def stop_pump2(self, clear: bool = False):
        """Stop pump. Bluesky plan."""
        yield from bps.abs_set(self.pump_stop, 1, wait=True)
        if clear:
            yield from bps.abs_set(self.clear_infused, 1, wait=True)
            yield from bps.abs_set(self.clear_withdrawn, 1, wait=True)
        yield from bps.sleep(1)
