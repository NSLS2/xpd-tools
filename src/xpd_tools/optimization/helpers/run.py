"""How a campaign is run: iterations, generation strategy, and local-run settings."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, kw_only=True)
class LocalRunSettings:
    """
    build_local() settings for a simulated run (the GUI builds the devices from these).

    Args:
        - mixer_lengths_cm: Mixer lengths; 0 skips the equilibrium wait.
        - residence_time_ratio: Residence-time ratio; 0 skips the wait.
        - simulated: build_simulated_tiled_clients kwargs (without mp_api_key, which
          comes from MP_API_KEY); None uses the fake Tiled clients.
        - skip_waits: Make the plans' waits (dilution, wash, settling) instant
          (legacy.skip_waits); False keeps real timing, e.g. to exercise the refill
          check.
    """

    mixer_lengths_cm: tuple[float, ...] = (0.0,)
    residence_time_ratio: float = 0.0
    simulated: dict[str, Any] | None = None
    skip_waits: bool = True

    def __post_init__(self) -> None:
        """Store mixer_lengths_cm as a tuple."""
        # JSON has no tuples: a list read back from a config becomes a tuple again.
        object.__setattr__(self, "mixer_lengths_cm", tuple(self.mixer_lengths_cm))


@dataclass(frozen=True, kw_only=True)
class RunSettings:
    """
    How the campaign runs, saved with the config so notebook and GUI match.

    Args:
        - iterations: Optimization iterations to run.
        - n_points: Points per iteration; the acquisition plans take exactly one.
        - extra_initialization_trials: If set, initialization_budget = historical
          trials ingested at build + this (Sobol trials after the history).
        - generation_strategy: kwargs for ax_client.configure_generation_strategy().
        - local: build_local() settings for simulated runs.
    """

    iterations: int = 10
    n_points: int = 1
    extra_initialization_trials: int | None = None
    generation_strategy: dict[str, Any] = field(default_factory=dict)
    local: LocalRunSettings = field(default_factory=LocalRunSettings)

    def __post_init__(self) -> None:
        """Check iterations is a whole number of at least 1."""
        # bool is an int subclass: reject True/False too.
        if type(self.iterations) is not int or self.iterations < 1:
            raise ValueError(
                "run.iterations must be a whole number of at least 1, "
                f"got {self.iterations!r}"
            )
        extra = self.extra_initialization_trials
        if extra is not None and (type(extra) is not int or extra < 0):
            raise ValueError(
                "run.extra_initialization_trials must be a whole number of at least 0 "
                f"(or null), got {extra!r}"
            )
        if self.n_points != 1:
            # plans/preflight.py: one suggestion per iteration, or the run fails.
            raise ValueError(
                "run.n_points must be 1: the acquisition plans run one point per "
                "iteration"
            )
        if (
            self.extra_initialization_trials is not None
            and "initialization_budget" in self.generation_strategy
        ):
            raise ValueError(
                "Set run.extra_initialization_trials or "
                "run.generation_strategy.initialization_budget, not both"
            )
        # A dict read back from a config becomes LocalRunSettings again.
        if isinstance(self.local, dict):
            object.__setattr__(self, "local", LocalRunSettings(**self.local))
