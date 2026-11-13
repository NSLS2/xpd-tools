"""Interactive helpers for building the detector configs of a tomography flyscan.

:func:`build_detector_configs` asks which detectors to use, then each one's exposure
time and number of images, and returns a list of
:class:`~xpd_tools.tomo.utils.DetectorConfig` ready to pass to
:func:`~xpd_tools.tomo.flyscans.tomo_flyscan`::

    configs = build_detector_configs()
    RE(tomo_flyscan(configs, start=0, stop=180))

:func:`image_count_options` and :func:`suggest_base_images` help choose image counts
that work together, without any prompts. Only the standard library and packages the
project already uses are needed; colours and emoji are switched off when the
terminal can't show them.
"""

import math
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from functools import reduce
from typing import Any

from IPython.core.getipython import get_ipython

from xpd_tools.tomo.utils import (
    DetectorConfig,
    TomoDetector,
    default_ttl_out,
    validate_detector_configs,
)

__all__ = [
    "BuilderCancelledError",
    "ImageCountOption",
    "build_detector_configs",
    "discover_detectors",
    "divisors",
    "image_count_options",
    "print_image_count_options",
    "suggest_base_images",
]

#: Words that cancel the builder at any prompt.
_QUIT_WORDS = ("q", "quit", "exit")


class BuilderCancelledError(Exception):
    """Raised when the user cancels :func:`build_detector_configs`."""


# --------------------------------------------------------------------------- maths


def divisors(n: int) -> list[int]:
    """Return every positive divisor of ``n``, smallest first.

    Parameters
    ----------
    n : int
        A positive integer.

    Returns
    -------
    list[int]
        The divisors of ``n``.

    Raises
    ------
    ValueError
        If ``n`` is not positive.

    Examples
    --------
    >>> divisors(12)
    [1, 2, 3, 4, 6, 12]
    """
    if n < 1:
        raise ValueError(f"n must be positive, got {n}.")
    small = [d for d in range(1, math.isqrt(n) + 1) if n % d == 0]
    return sorted({*small, *(n // d for d in small)})


@dataclass(frozen=True)
class ImageCountOption:
    """One way a slower detector can share a flyscan with a faster one.

    Attributes
    ----------
    nproj : int
        The projections (angular intervals) the slower detector takes.
    divisor : int
        It is triggered every this many base pulses, starting with the first.
    step : float
        The rotation between its images, in degrees.
    first_angle : float
        The angle of its first image.
    last_angle : float
        The angle of its last image, always the end of the range.
    """

    nproj: int
    divisor: int
    step: float
    first_angle: float
    last_angle: float

    @property
    def num_images(self) -> int:
        """The images the flyscan writes: one more than the projections."""
        return self.nproj + 1


def image_count_options(
    nproj: int, start: float = 0.0, stop: float = 180.0
) -> list[ImageCountOption]:
    """List the projection counts other detectors can take next to ``nproj``.

    A flyscan covers ``start`` to ``stop`` in ``nproj`` equal intervals, with an image
    at each end (so ``nproj + 1`` images). Any other detector must take a number of
    projections that divides ``nproj`` exactly, and is fired every ``nproj // count``
    base pulses starting with the first, so its images are on its own equal grid
    and the last is at ``stop``.

    Parameters
    ----------
    nproj : int
        The most projections any detector takes.
    start : float, default 0.0
        The starting angle of the rotation, in degrees.
    stop : float, default 180.0
        The stopping angle of the rotation, in degrees.

    Returns
    -------
    list[ImageCountOption]
        One option per divisor of ``nproj``, the fewest projections first.

    Raises
    ------
    ValueError
        If ``nproj`` is not positive.

    Examples
    --------
    >>> [(o.nproj, o.divisor) for o in image_count_options(12)]
    [(1, 12), (2, 6), (3, 4), (4, 3), (6, 2), (12, 1)]
    >>> fastest = image_count_options(100, 0, 360)[-1]
    >>> fastest.step, fastest.last_angle, fastest.num_images
    (3.6, 360, 101)
    """
    return [
        ImageCountOption(
            nproj=count,
            divisor=nproj // count,
            step=(stop - start) / count,
            first_angle=start,
            last_angle=stop,
        )
        for count in divisors(nproj)
    ]


def suggest_base_images(
    target: int, counts: Sequence[int], how_many: int = 5
) -> list[int]:
    """Suggest the most projections to ask for, closest to ``target``.

    The most projections must be a multiple of every other detector's projections.

    Parameters
    ----------
    target : int
        The number of projections wanted.
    counts : Sequence[int]
        The projections the other detectors take.
    how_many : int, default 5
        The most suggestions to return.

    Returns
    -------
    list[int]
        Multiples of every count, the closest to ``target`` first, ties going to
        the smaller.

    Raises
    ------
    ValueError
        If ``target`` or any count is not positive.

    Examples
    --------
    >>> suggest_base_images(1000, [3, 8], how_many=3)
    [1008, 984, 1032]
    """
    if target < 1 or any(c < 1 for c in counts):
        raise ValueError("target and counts must be positive.")
    step = reduce(math.lcm, counts, 1)
    below = max(target // step, 1)
    candidates = {
        step * k for k in range(max(below - how_many, 1), below + how_many + 2)
    }
    return sorted(candidates, key=lambda n: (abs(n - target), n))[:how_many]


# ----------------------------------------------------------------------- terminal


class _Style:
    """ANSI colours and emoji, or plain text when the terminal can't show them."""

    def __init__(self, color: bool, emoji: bool):
        self.color = color
        self.emoji = emoji

    def paint(self, text: str, *codes: str) -> str:
        """Wrap ``text`` in ANSI ``codes`` if colour is on."""
        if not self.color:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def icon(self, symbol: str, fallback: str = "") -> str:
        """Return ``symbol`` if emoji are on, else ``fallback``."""
        return symbol if self.emoji else fallback

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def good(self, text: str) -> str:
        return self.paint(text, "32")

    def warn(self, text: str) -> str:
        return self.paint(text, "33")

    def bad(self, text: str) -> str:
        return self.paint(text, "31")

    def info(self, text: str) -> str:
        return self.paint(text, "36")

    @classmethod
    def detect(cls, color: bool | None, emoji: bool | None) -> "_Style":
        """Choose colour and emoji from the terminal, unless given."""
        tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        use_color = tty and "NO_COLOR" not in os.environ if color is None else color
        if emoji is None:
            encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
            use_emoji = use_color and "utf" in encoding
        else:
            use_emoji = emoji
        return cls(use_color, use_emoji)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Format ``rows`` as aligned plain-text lines, header first."""
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max(len(h), *(len(row[i]) for row in cells)) if cells else len(h)
        for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    out = [line, "  ".join("-" * w for w in widths)]
    out.extend(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in cells
    )
    return out


def print_image_count_options(
    nproj: int,
    start: float = 0.0,
    stop: float = 180.0,
    *,
    color: bool | None = None,
    emoji: bool | None = None,
    print_fn: Callable[..., None] = print,
) -> None:
    """Print the projection counts other detectors can take next to ``nproj``.

    See :func:`image_count_options`.

    Parameters
    ----------
    nproj : int
        The most projections any detector takes.
    start : float, default 0.0
        The starting angle of the rotation, in degrees.
    stop : float, default 180.0
        The stopping angle of the rotation, in degrees.
    color : bool | None, default None
        Whether to use colour; None uses it if the terminal is interactive.
    emoji : bool | None, default None
        Whether to use emoji; None uses them if colour is on and the terminal is UTF.
    print_fn : Callable[..., None], default print
        Where the text goes.
    """
    style = _Style.detect(color, emoji)
    options = image_count_options(nproj, start, stop)
    print_fn(
        style.bold(
            f"{style.icon('📐 ')}Projection counts that divide {nproj} "
            f"over {start:g}° to {stop:g}°"
        )
    )
    rows = [
        (o.nproj, o.num_images, f"every {o.divisor}", f"{o.step:.4g}°") for o in options
    ]
    for i, line in enumerate(_table(["nproj", "images", "fired", "step"], rows)):
        print_fn(style.info(line) if i == 0 else line)
    print_fn(
        style.dim(
            f"Each detector also images the end of the range ({stop:g}°), so it "
            "writes nproj + 1 images."
        )
    )


# ----------------------------------------------------------------------- prompts


def discover_detectors(
    namespace: Mapping[str, Any] | None = None,
) -> dict[str, TomoDetector]:
    """Find the tomography detectors in the IPython namespace.

    Parameters
    ----------
    namespace : Mapping[str, Any] | None, default None
        The names to search; None uses the IPython user namespace.

    Returns
    -------
    dict[str, TomoDetector]
        Each detector by the variable name it is bound to, one entry per detector.
    """
    if namespace is None:
        ipython = get_ipython()
        namespace = ipython.user_ns if ipython is not None else {}
    found: dict[str, TomoDetector] = {}
    seen: set[int] = set()
    for var_name, obj in namespace.items():
        if var_name.startswith("_") or default_ttl_out(obj) is None:
            continue
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        found[var_name] = obj
    return found


def parse_selection(text: str, names: Sequence[str]) -> list[int]:
    """Turn "1 3", "1,3", "1-3", "all" or variable names into indexes into ``names``."""
    text = text.strip().lower()
    if text in ("a", "all"):
        return list(range(len(names)))
    chosen: list[int] = []
    for token in text.replace(",", " ").split():
        if "-" in token and all(part.isdigit() for part in token.split("-", 1)):
            low, high = (int(part) for part in token.split("-", 1))
            indexes = range(low - 1, high)
        elif token.isdigit():
            indexes = [int(token) - 1]
        elif token in (n.lower() for n in names):
            indexes = [[n.lower() for n in names].index(token)]
        else:
            raise ValueError(f"{token!r} is not one of the detectors.")
        for index in indexes:
            if not 0 <= index < len(names):
                raise ValueError(f"There is no detector number {index + 1}.")
            if index not in chosen:
                chosen.append(index)
    if not chosen:
        raise ValueError("Pick at least one detector.")
    return chosen


class _Prompter:
    """Asks questions through ``input_fn`` and checks the answers."""

    def __init__(
        self,
        input_fn: Callable[[str], str],
        print_fn: Callable[..., None],
        style: _Style,
    ):
        self.input_fn = input_fn
        self.print = print_fn
        self.style = style

    def ask(self, prompt: str, default: str | None = None) -> str:
        """Return the stripped answer, or ``default`` for an empty one."""
        suffix = f" [{default}]" if default else ""
        try:
            answer = self.input_fn(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise BuilderCancelledError("Cancelled.") from None
        if answer.lower() in _QUIT_WORDS:
            raise BuilderCancelledError("Cancelled.")
        if not answer and default is not None:
            return default
        return answer

    def error(self, message: str) -> None:
        self.print(self.style.bad(f"{self.style.icon('✖ ', '! ')}{message}"))

    def ask_number(
        self,
        prompt: str,
        convert: Callable[[str], float],
        default: str | None = None,
        minimum: float = 0,
    ) -> float:
        """Ask until the answer converts to a number above ``minimum``."""
        while True:
            answer = self.ask(prompt, default)
            try:
                value = convert(answer)
            except ValueError:
                self.error(f"{answer!r} is not a number.")
                continue
            if not math.isfinite(value) or value <= minimum:
                self.error(f"Enter a number greater than {minimum:g}.")
                continue
            return value

    def ask_float(self, prompt: str, default: str | None = None) -> float:
        return self.ask_number(prompt, float, default)

    def ask_int(self, prompt: str, default: str | None = None) -> int:
        return int(self.ask_number(prompt, int, default))

    def ask_yes_no(self, prompt: str, default: bool) -> bool:
        """Ask a yes/no question; an empty answer is ``default``."""
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.ask(f"{prompt} ({hint})", "").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.error("Answer y or n.")


#: The IPython variable :func:`build_detector_configs` stores its configs in.
DETCONFIG_NAME = "DETCONFIG"


def build_detector_configs(
    start: float = 0.0,
    stop: float = 360,
    *,
    detectors: Mapping[str, TomoDetector] | None = None,
    namespace: MutableMapping[str, Any] | None = None,
    color: bool | None = None,
    emoji: bool | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> list[DetectorConfig]:
    """Interactively build the detector configs for :func:`tomo_flyscan`.

    Lists the detectors found in the IPython namespace, asks which to use, then each
    one's exposure time and number of projections (a flyscan writes one more image
    at the end of the range). Every detector's TTL output defaults to the one it is
    wired to. The answers are checked as they come, and the finished configs are
    validated as a whole before they are returned. Type ``q`` at any prompt to cancel.

    Once confirmed, the configs are also stored as ``DETCONFIG`` in the IPython
    namespace (replacing any earlier ``DETCONFIG``), and the command printed at the
    end, ``RE(tomo_flyscan(DETCONFIG, ...))``, can be pasted and run as it is.

    Parameters
    ----------
    start : float, default 0.0
        The starting angle of the rotation, in degrees. Only used to show each
        detector's angles; pass the same to the plan.
    stop : float, default 360.0
        The stopping angle of the rotation, in degrees. Only used to show each
        detector's angles; pass the same to the plan.
    detectors : Mapping[str, TomoDetector] | None, default None
        The detectors to offer, by name. None searches the IPython namespace.
    namespace : MutableMapping[str, Any] | None, default None
        Where ``DETCONFIG`` is stored. None uses the IPython namespace, and outside
        IPython stores nothing.
    color : bool | None, default None
        Whether to use colour; None uses it if the terminal is interactive.
    emoji : bool | None, default None
        Whether to use emoji; None uses them if colour is on and the terminal is UTF.
    input_fn : Callable[[str], str], default input
        How answers are read.
    print_fn : Callable[..., None], default print
        Where the text goes.

    Returns
    -------
    list[DetectorConfig]
        One config per chosen detector, to pass as ``detector_configs``.

    Raises
    ------
    BuilderCancelledError
        If the user cancels, or no detectors are found.

    Examples
    --------
    >>> build_detector_configs()  # doctest: +SKIP
    >>> RE(tomo_flyscan(DETCONFIG, start=0, stop=180))  # doctest: +SKIP
    """
    style = _Style.detect(color, emoji)
    prompter = _Prompter(input_fn, print_fn, style)
    available = dict(detectors) if detectors is not None else discover_detectors()
    if not available:
        raise BuilderCancelledError(
            "No detectors found in the IPython namespace. Pass detectors={name: det}."
        )

    print_fn(style.bold(f"\n{style.icon('🔬 ')}Tomography detector setup"))
    print_fn(style.dim("Type q at any prompt to cancel.\n"))
    names = list(available)
    for i, var_name in enumerate(names, start=1):
        det = available[var_name]
        label = var_name if det.name == var_name else f"{var_name} ({det.name})"
        print_fn(
            f"  {style.info(str(i))}. {style.bold(label)}  "
            f"{style.dim(f'{type(det).__name__}, TTL {default_ttl_out(det)}')}"
        )

    chosen = _choose_detectors(prompter, names, available)
    configs = _collect_configs(prompter, [available[names[i]] for i in chosen])
    _show_summary(configs, start, stop, style, print_fn)
    if not prompter.ask_yes_no("Use these?", True):
        raise BuilderCancelledError("Cancelled.")

    if namespace is None:
        ipython = get_ipython()
        namespace = ipython.user_ns if ipython is not None else None
    if namespace is not None:
        if DETCONFIG_NAME in namespace:
            print_fn(style.dim(f"(Replaced the previous {DETCONFIG_NAME}.)"))
        namespace[DETCONFIG_NAME] = configs
    name = DETCONFIG_NAME if namespace is not None else "configs"
    saved = f"Saved as {DETCONFIG_NAME}. " if namespace is not None else ""
    print_fn(
        style.good(f"\n{style.icon('✅ ', '')}Ready. {saved}Run the flyscan:")
        + f"\n    RE(tomo_flyscan({name}, start={start:g}, stop={stop:g}))\n"
    )
    return configs


def _choose_detectors(
    prompter: _Prompter, names: Sequence[str], available: Mapping[str, TomoDetector]
) -> list[int]:
    """Ask which detectors to use, until the answer is valid."""
    while True:
        answer = prompter.ask("\nWhich detectors? (numbers or names, e.g. 1 3, or all)")
        try:
            chosen = parse_selection(answer, names)
        except ValueError as err:
            prompter.error(str(err))
            continue
        ttls = [default_ttl_out(available[names[i]]) or 0 for i in chosen]
        clash = {t for t in ttls if ttls.count(t) > 1}
        if clash:
            prompter.error(
                f"Two of those detectors share TTL output {sorted(clash)}. "
                "Pick one of each kind."
            )
            continue
        return chosen


def _collect_configs(
    prompter: _Prompter, detectors: Sequence[TomoDetector]
) -> list[DetectorConfig]:
    """Ask each detector's settings, then fix them until they work together."""
    style = prompter.style
    settings: dict[int, dict[str, Any]] = {}
    for i, det in enumerate(detectors):
        prompter.print(style.bold(f"\n{style.icon('📷 ')}{det.name}"))
        entry: dict[str, Any] = {
            "detector": det,
            "ttl_out": default_ttl_out(det),
            "exposure_time": prompter.ask_float("  Exposure time (s)"),
            "nproj": prompter.ask_int("  Number of projections"),
        }
        if hasattr(det, "proc"):
            average = prompter.ask_int("  Frames to average per image", "1")
            entry["images_to_average"] = average
        settings[i] = entry

    while True:
        try:
            configs = [DetectorConfig(**settings[i]) for i in range(len(detectors))]
            validate_detector_configs(configs)
        except ValueError as err:
            prompter.error(str(err).splitlines()[0] if str(err) else repr(err))
            if not _repair(prompter, detectors, settings):
                raise BuilderCancelledError("Cancelled.") from err
            continue
        return configs


def _repair(
    prompter: _Prompter,
    detectors: Sequence[TomoDetector],
    settings: dict[int, dict[str, Any]],
) -> bool:
    """Offer working projection counts and re-ask; False to give up."""
    style = prompter.style
    # Gaps between each detector's triggers: (nproj + 1) * frames averaged - 1.
    triggers = {
        i: (s["nproj"] + 1) * s.get("images_to_average", 1) - 1
        for i, s in settings.items()
    }
    base = max(triggers.values())
    bad = [i for i, t in triggers.items() if base % t]
    if not bad:
        # Not an image-count problem, so ask again from scratch.
        return prompter.ask_yes_no("Re-enter the settings?", True) and _reask_all(
            prompter, detectors, settings
        )
    prompter.print(
        style.warn(
            f"\n{style.icon('💡 ')}The most projections, {base}, must be a multiple of "
            "each detector's. Options:"
        )
    )
    others = [triggers[i] for i in triggers if i not in bad]
    for i in bad:
        prompter.print(f"  {detectors[i].name} takes {triggers[i]}:")
        average = settings[i].get("images_to_average", 1)
        # A gap count g is (nproj + 1) * average - 1 triggers' worth of gaps.
        fits = [
            (option, (option.nproj + 1) // average - 1)
            for option in image_count_options(base)[::-1]
            if (option.nproj + 1) % average == 0 and option.nproj + 1 > average
        ]
        for option, nproj in fits[:6]:
            prompter.print(
                f"    {nproj} projections (every {option.divisor} base pulses)"
            )
    prompter.print(
        "  Or raise the most projections: "
        + ", ".join(
            str(n)
            for n in suggest_base_images(base, [triggers[i] for i in bad] + others)
        )
    )
    return prompter.ask_yes_no(
        "Re-enter the projection counts?", True
    ) and _reask_counts(prompter, detectors, settings)


def _reask_counts(
    prompter: _Prompter,
    detectors: Sequence[TomoDetector],
    settings: dict[int, dict[str, Any]],
) -> bool:
    """Ask every detector's number of projections again."""
    for i, det in enumerate(detectors):
        settings[i]["nproj"] = prompter.ask_int(
            f"  {det.name} number of projections", str(settings[i]["nproj"])
        )
    return True


def _reask_all(
    prompter: _Prompter,
    detectors: Sequence[TomoDetector],
    settings: dict[int, dict[str, Any]],
) -> bool:
    """Ask every detector's exposure time and number of projections again."""
    for i, det in enumerate(detectors):
        settings[i]["exposure_time"] = prompter.ask_float(
            f"  {det.name} exposure time (s)", str(settings[i]["exposure_time"])
        )
    return _reask_counts(prompter, detectors, settings)


def _show_summary(
    configs: Sequence[DetectorConfig],
    start: float,
    stop: float,
    style: _Style,
    print_fn: Callable[..., None],
) -> None:
    """Print what each detector will do over ``start`` to ``stop``."""
    base = max(c.num_intervals for c in configs)
    rows = []
    for config in configs:
        option = next(
            o
            for o in image_count_options(base, start, stop)
            if o.nproj == config.num_intervals
        )
        rows.append(
            (
                config.name,
                f"TTL{config.ttl_out}",
                f"{config.exposure_time:g} s",
                config.nproj,
                config.num_images,
                f"every {option.divisor}",
                f"{option.step * config.images_to_average:.4g}°",
            )
        )
    print_fn(
        style.bold(
            f"\n{style.icon('📋 ')}Summary: {base + 1} base pulses over {base} steps"
        )
    )
    headers = ["detector", "ttl", "exposure", "nproj", "images", "fired", "step"]
    for i, line in enumerate(_table(headers, rows)):
        print_fn(style.info(line) if i == 0 else line)
    print_fn(
        style.dim(
            f"Every detector's first image is at {start:g}° and last at {stop:g}°."
        )
    )
