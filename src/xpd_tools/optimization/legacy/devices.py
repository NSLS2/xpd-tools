"""Simulated ophyd devices matching the shape `plans/runtime.py` needs."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import numpy as np
from bluesky import plan_stubs as bps
from ophyd import Component as Cpt
from ophyd import Device, Signal
from ophyd.status import DeviceStatus


class FakeQEPro(Device):
    """Simulated QEPro spectrometer: triggers pop a pre-loaded spectrum."""

    x_axis = Cpt(Signal, value=np.linspace(200.0, 950.0, 751))
    output = Cpt(Signal, value=np.zeros(751))
    spectrum_type = Cpt(Signal, value="Corrected Sample")
    correction = Cpt(Signal, value="Dark")

    def __init__(
        self,
        *args: Any,
        spectra: Sequence[np.ndarray] | None = None,
        fail_on_trigger: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.spectra = list(spectra or [])
        self.fail_on_trigger = fail_on_trigger
        self.trigger_count = 0

    def trigger(self) -> DeviceStatus:
        self.trigger_count += 1
        status = DeviceStatus(self)
        if self.fail_on_trigger == self.trigger_count:
            status.set_exception(RuntimeError("QEPro trigger failed"))
            return status
        if self.spectra:
            spectrum = self.spectra.pop(0)
            self.output.put(np.asarray(spectrum, dtype=float))
        status.set_finished()
        return status


class FakePump(Device):
    """Simulated New Era syringe pump: set_infuse2/infuse_pump2/stop_pump2."""

    read_infuse_rate = Cpt(Signal, value=0.0)
    read_infuse_rate_unit = Cpt(Signal, value="ul/min")
    status = Cpt(Signal, value="Stopped")
    # Infused-volume counter like the real pumps' IVOLUME:RBV: rate x running time,
    # added when the pump stops; putting 1 to clear_infused resets it.
    read_infused = Cpt(Signal, value=0.0)
    read_infused_unit = Cpt(Signal, value="ml")
    clear_infused = Cpt(Signal, value=0)

    def __init__(
        self,
        *args: Any,
        fail_start: bool = False,
        fail_stop_call: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fail_start = fail_start
        self.fail_stop_call = fail_stop_call
        self.configurations: list[dict[str, Any]] = []
        self.start_count = 0
        self.stop_count = 0
        self._infusing_since: float | None = None
        self.clear_infused.subscribe(self._on_clear, run=False)

    def _on_clear(self, value: Any, **kwargs: Any) -> None:
        if value:
            self.read_infused.put(0.0)

    def set_infuse2(
        self,
        input_size: float,
        *,
        syringe_material: str,
        set_target: bool,
        target_vol: float,
        target_unit: str,
        infuse_rate: float,
        infuse_unit: str,
    ):
        self.configurations.append(
            {
                "input_size": input_size,
                "syringe_material": syringe_material,
                "set_target": set_target,
                "target_vol": target_vol,
                "target_unit": target_unit,
                "infuse_rate": infuse_rate,
                "infuse_unit": infuse_unit,
            }
        )
        yield from bps.mv(
            self.read_infuse_rate,
            infuse_rate,
            self.read_infuse_rate_unit,
            infuse_unit,
        )

    def infuse_pump2(self):
        self.start_count += 1
        if self.fail_start:
            raise RuntimeError(f"failed to start {self.name}")
        yield from bps.mv(self.status, "Infusing")
        self._infusing_since = time.monotonic()

    def stop_pump2(self):
        self.stop_count += 1
        if self.fail_stop_call == self.stop_count:
            raise RuntimeError(f"failed to stop {self.name}")
        if self._infusing_since is not None:
            minutes = (time.monotonic() - self._infusing_since) / 60
            infused = (
                self.read_infused.get() + self.read_infuse_rate.get() / 1000 * minutes
            )
            self._infusing_since = None
            yield from bps.mv(self.read_infused, infused)
        yield from bps.mv(self.status, "Stopped")


class FakeAreaDetector(Device):
    """Simulated X-ray area detector: just enough for trigger_and_read."""

    class Cam(Device):
        """Simulated cam plugin exposing acquire_time."""

        acquire_time = Cpt(Signal, value=0.1)

    cam = Cpt(Cam, "")
    images_per_set = Cpt(Signal, value=1)
    image = Cpt(Signal, value=1.0)

    def __init__(self, *args: Any, fail_trigger: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_trigger = fail_trigger

    def trigger(self) -> DeviceStatus:
        status = DeviceStatus(self)
        if self.fail_trigger:
            status.set_exception(RuntimeError("X-ray trigger failed"))
        else:
            status.set_finished()
        return status


# Pump ids matching this package's own examples (examples/halide_example_class.ipynb,
# tests/optimization/conftest.py's fake_pumps) -- not any particular real beamline's
# device names, since xpd-profile-collection (which owned those) is being deprecated.
_PUMP_IDS = ("dds1_p1", "dds2_p1", "dds2_p2", "dds3_p1", "dds3_p2", "ultra1", "ultra2")


def build_xpd_objects() -> dict[str, Any]:
    """Build a full set of simulated devices for `BuildAgent.build_local()`."""
    devices: dict[str, Any] = {
        "led": Signal(name="led", value="Low"),
        "uv_shutter": Signal(name="uv_shutter", value="Low"),
        "fast_shutter": Signal(name="fast_shutter", value=20),
        "qepro": FakeQEPro(name="QEPro"),
        "xray_detector": FakeAreaDetector(name="xray_detector"),
    }
    devices.update({pump_id: FakePump(name=pump_id) for pump_id in _PUMP_IDS})
    return devices


def skip_waits(RE: Any) -> None:  # noqa: N803
    """
    Make `RE` treat every `sleep` message as instant, for fast simulated runs.

    The plans' waits (dilution wait_sec, wash duration_sec, optical settling,
    mixer equilibrium) are real hardware timing; simulated devices don't need them.
    Fake pumps then count almost no volume, so the refill check barely triggers.

    Args:
        - RE: A RunEngine driving simulated devices only.
    """

    async def _no_sleep(msg: Any) -> None:
        return None

    RE.register_command("sleep", _no_sleep)


def identity_wrap_xray_run(plan: Any, no_dark: bool) -> Any:
    """Passthrough `wrap_xray_run`."""
    del no_dark
    return plan
