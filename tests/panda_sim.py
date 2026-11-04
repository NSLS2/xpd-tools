"""A small simulation of a PandA encoder input and a PCOMP block.

The encoder counter counts continuously (no reset on Z) from an arbitrary
offset, and wraps around when it overflows its 32 bit range.
"""

from dataclasses import dataclass, field

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
