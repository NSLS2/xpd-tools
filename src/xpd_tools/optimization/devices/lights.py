"""Light sources and shutters for the 28-ID-2 halide perovskite rig.

Ported from xpd-profile-collection startup scripts:
    28-Lights_shutter.py  -- LED, UV_shutter
    10-motors.py          -- fast shutter (fs = shctl1, an EpicsMotor)

The devices here are **thin wrappers** so that the factory
``build_halide_devices`` can instantiate them with arbitrary PV
strings, keeping the PV addresses in one place (``devices/__init__.py``).
"""

from __future__ import annotations

from ophyd import EpicsMotor, EpicsSignal


class LED(EpicsSignal):
    """Fluorescence LED.

    PV value: 'High' = on, 'Low' = off.
    Constructed with ``string=True`` by default.
    """

    def __init__(self, *args, string: bool = True, kind: str = "hinted", **kwargs):
        super().__init__(*args, string=string, kind=kind, **kwargs)


class UVShutter(EpicsSignal):
    """UV/absorbance light shutter.

    PV value: 'High' = open, 'Low' = closed.
    Constructed with ``string=True`` by default.
    """

    def __init__(self, *args, string: bool = True, kind: str = "hinted", **kwargs):
        super().__init__(*args, string=string, kind=kind, **kwargs)


class FastShutter(EpicsMotor):
    """Beam fast-shutter motor (shctl1 on the beamline).

    The acquisition plans move this motor to ±20 to open/close the X-ray beam.
    Using ``EpicsMotor`` matches the original ``fs = shctl1`` assignment in
    ``10-motors.py``.
    """
