"""A small simulation of a PandA encoder input and a PCOMP block.

The encoder counter counts continuously (no reset on Z) from an arbitrary
offset, and wraps around when it overflows its 32 bit range.
"""

from dataclasses import dataclass, field

from ophyd_async.core import (
    Device,
    DeviceVector,
    StaticPathProvider,
    soft_signal_r_and_setter,
    soft_signal_rw,
)
from ophyd_async.fastcs.panda import HDFPanda

INT32_MIN = -(2**31)
INT32_SPAN = 2**32


def _wrap(value: int) -> int:
    return (value - INT32_MIN) % INT32_SPAN + INT32_MIN


@dataclass
class ContinuousCounter:
    """32 bit PandA encoder counter following a motor, seen through CALC = -INENC.

    ``value`` is the position compare input, which increases with the motor.
    """

    resolution: float
    position: float
    value: int

    def set_counter(self, value: int) -> None:
        """Write INENC.SETP, which sets the counter (and so CALC = -value)."""
        self.value = _wrap(-value)

    def move_to(self, position: float) -> list[tuple[int, int]]:
        """Move monotonically to ``position``.

        Returns
        -------
        list[tuple[int, int]]
            The continuous segments of counter values swept, as (from, to). A new
            segment starts if the counter overflows.
        """
        delta = round(position / self.resolution) - round(
            self.position / self.resolution
        )
        unwrapped = self.value + delta
        segments = [(self.value, unwrapped)]
        if _wrap(unwrapped) != unwrapped:
            limit = 2**31 if delta > 0 else INT32_MIN - 1
            segments = [(self.value, limit), (_wrap(limit), _wrap(unwrapped))]
        self.position = position
        self.value = _wrap(unwrapped)
        return segments


@dataclass
class PcompSim:
    """Position compare block fed by a :class:`ContinuousCounter`."""

    start: int = 0
    step: int = 0
    width: int = 0
    pulses: int = 0
    direction: int = 1
    enabled: bool = False
    fired: int = 0
    errors: list[str] = field(default_factory=list)

    def enable(self, value: int) -> None:
        """Enable with the input currently at ``value``."""
        self.enabled = True
        self.fired = 0
        if self.direction * (value - self.start) >= 0:
            self.errors.append(
                f"Enabled at {value}, already at or past start {self.start}"
            )

    def disable(self) -> None:
        self.enabled = False

    @property
    def done(self) -> bool:
        return self.fired >= self.pulses

    def observe(self, segments: list[tuple[int, int]]) -> int:
        """Feed the swept counter segments. Returns the number of new pulses."""
        if not self.enabled or self.errors or self.done:
            return 0
        before = self.fired
        for i, (begin, end) in enumerate(segments):
            if i > 0 and not self.done:
                self.errors.append(f"Position jumped after {self.fired} pulses")
                break
            if self.direction * (end - begin) <= 0:
                continue
            while not self.done:
                target = self.start + self.direction * self.fired * self.step
                if self.direction * (target - begin) > 0 and (
                    self.direction * (end - target) >= 0
                ):
                    self.fired += 1
                else:
                    break
        return self.fired - before


class CalcBlock(Device):
    def __init__(self):
        self.out, _ = soft_signal_r_and_setter(int)
        self.out_dataset = soft_signal_rw(str)
        self.out_units = soft_signal_rw(str)
        self.out_scale = soft_signal_rw(float)
        self.out_offset = soft_signal_rw(float)
        super().__init__(name="calc1")


class InencBlock(Device):
    def __init__(self):
        self.setp = soft_signal_rw(int)
        self.rst_on_z = soft_signal_rw(str, initial_value="0")
        super().__init__(name="inenc1")


class DivBlock(Device):
    """A PandA pulse divider, as saved in ``MultiTrigMultiDetV1.yaml``."""

    def __init__(self):
        self.divisor = soft_signal_rw(float, initial_value=1.0)
        self.first_pulse = soft_signal_rw(str, initial_value="OutN")
        self.inp = soft_signal_rw(str, initial_value="PULSE1.OUT")
        self.enable = soft_signal_rw(str, initial_value="PCAP.ACTIVE")
        super().__init__(name="div")


class TtlOutBlock(Device):
    """A PandA TTL output."""

    def __init__(self, val: str = "ZERO"):
        self.val = soft_signal_rw(str, initial_value=val)
        super().__init__(name="ttlout")


async def add_pcomp_inp(panda: HDFPanda) -> None:
    """Add the PCOMP1 input signal, which the flyer logic
    selects but PcompBlock lacks."""
    panda.pcomp[1].inp = soft_signal_rw(str)
    await panda.pcomp[1].inp.connect(mock=True)  # type: ignore


class HDFPandaWithCalc(HDFPanda):
    def __init__(self, prefix: str, path_provider: StaticPathProvider):
        super().__init__(prefix, path_provider)
        self.calc = DeviceVector({1: CalcBlock(), 2: CalcBlock()})
        self.inenc = DeviceVector({1: InencBlock(), 2: InencBlock()})
        self.div = DeviceVector({1: DivBlock(), 2: DivBlock()})
        # Wired as saved in MultiTrigMultiDetV1.yaml.
        self.ttlout = DeviceVector(
            {
                1: TtlOutBlock("DIV1.OUTD"),
                2: TtlOutBlock("DIV2.OUTD"),
                3: TtlOutBlock("PULSE1.OUT"),
                4: TtlOutBlock(),
            }
        )
