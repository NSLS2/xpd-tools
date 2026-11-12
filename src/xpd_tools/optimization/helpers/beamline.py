"""Common beamline configuration utilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from xpd_tools.optimization.helpers.qepro import QualityPolicy, SpectraFitSettings
from xpd_tools.optimization.helpers.sources import DilutionStage, FlowSource, WashCycle

_DEFAULT_TUBING_ID_MM = 1.016  # 1/25" ID PEEK tubing standard at the beamline


def _normalize_context_sequences(context: Any) -> None:
    # Normalize to tuples regardless of what sequence type the caller
    # passed in -- these fields are typed tuple[...] but nothing else
    # enforces it, and a frozen dataclass with a list field breaks hash().
    for field in ("sources", "dilutions", "wash_cycles", "mixer_lengths_cm"):
        object.__setattr__(context, field, tuple(getattr(context, field)))


@dataclass(frozen=True, kw_only=True)
class XraySettings:
    """X-ray detector acquisition settings."""

    exposure: float = 5.0
    frame_acq_time: float = 0.2
    no_dark: bool = False
    stream_name: str = "scattering"


@dataclass(frozen=True, kw_only=True)
class XrayUvvisPlanContext:
    """Static hardware and process configuration for one bound plan."""

    qepro: Any
    led: Any
    uv_shutter: Any
    fast_shutter: Any
    xray_detector: Any
    wrap_xray_run: Callable[[Any, bool], Any]
    sources: tuple[FlowSource, ...]
    dilutions: tuple[DilutionStage, ...]
    wash_cycles: tuple[WashCycle, ...]
    mixer_lengths_cm: tuple[float, ...] = (30.0,)
    residence_time_ratio: float = 1.0
    tubing_id_mm: float = _DEFAULT_TUBING_ID_MM
    quality: QualityPolicy = QualityPolicy()
    xray: XraySettings = XraySettings()
    fit_settings: SpectraFitSettings = SpectraFitSettings()

    def __post_init__(self) -> None:
        """Populate class attributes to tuples."""
        _normalize_context_sequences(self)


@dataclass(frozen=True, kw_only=True)
class XrayPlanContext:
    """Static hardware and process configuration for one bound plan."""

    led: Any
    fast_shutter: Any
    xray_detector: Any
    wrap_xray_run: Callable[[Any, bool], Any]
    sources: tuple[FlowSource, ...]
    dilutions: tuple[DilutionStage, ...]
    wash_cycles: tuple[WashCycle, ...]
    mixer_lengths_cm: tuple[float, ...] = (30.0,)
    residence_time_ratio: float = 1.0
    tubing_id_mm: float = _DEFAULT_TUBING_ID_MM
    quality: QualityPolicy = QualityPolicy()
    xray: XraySettings = XraySettings()

    def __post_init__(self) -> None:
        """Populate class attributes to tuples."""
        _normalize_context_sequences(self)


@dataclass(frozen=True, kw_only=True)
class UvvisPlanContext:
    """Static hardware and process configuration for one bound plan."""

    qepro: Any
    led: Any
    uv_shutter: Any
    fast_shutter: Any
    sources: tuple[FlowSource, ...]
    dilutions: tuple[DilutionStage, ...]
    wash_cycles: tuple[WashCycle, ...]
    mixer_lengths_cm: tuple[float, ...] = (30.0,)
    residence_time_ratio: float = 1.0
    tubing_id_mm: float = _DEFAULT_TUBING_ID_MM
    quality: QualityPolicy = QualityPolicy()
    fit_settings: SpectraFitSettings = SpectraFitSettings()

    def __post_init__(self) -> None:
        """Populate class attributes to tuples."""
        _normalize_context_sequences(self)
