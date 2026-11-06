"""Behavioral tests for the photoluminescence quality gate."""

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
import numpy as np
import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import callback_on_mock_put, set_mock_value

from xpd_tools.detectors import QEPro
from xpd_tools.light_path import UVVisLightPath
from xpd_tools.optimization.pl_quality import (
    NO_PEAK_WAVELENGTH,
    PLQualityMonitor,
    fluorescence_quality_gate,
)

# Each gate switches optics once and sleeps; record the sleeps instead of waiting.
pytestmark = pytest.mark.usefixtures("sleeps")


def _gaussian(x: np.ndarray, center: float, sigma: float, height: float) -> np.ndarray:
    return height * np.exp(-0.5 * ((x - center) / sigma) ** 2)


@pytest.fixture
def spectra(RE: RunEngine, qepro: QEPro) -> dict[str, np.ndarray]:
    x = cast(Any, RE(bps.rd(qepro.x_axis))).plan_result
    led = _gaussian(x, 365.0, 5.0, 3000.0)
    # "good" sits between the default key height (200) and classify_pl's own
    # default (2000), so it is good only with the evaluation's screening thresholds.
    return {
        "good": led + _gaussian(x, 520.0, 15.0, 1000.0),
        "weak": led + _gaussian(x, 520.0, 15.0, 100.0),
        "led_only": led,
    }


def _script_spectra(qepro: QEPro, spectra: Sequence[np.ndarray]) -> None:
    """Make each QEPro trigger publish the next spectrum."""
    remaining = iter(spectra)

    def complete_collection(collect: bool) -> None:
        if collect:
            set_mock_value(qepro.output, next(remaining))
            asyncio.get_running_loop().call_soon(set_mock_value, qepro.acquire, False)

    callback_on_mock_put(qepro.acquire, complete_collection)


def _run_gate(
    RE: RunEngine,
    qepro: QEPro,
    light_path: UVVisLightPath,
    monitor: PLQualityMonitor | None,
    num_spectra: int,
    *,
    good_target: int = 2,
    max_bad: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Run the gate in one run and return each stream's event data in order."""
    streams: dict[str, str] = {}
    data: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)

    def collect(name: str, doc: dict[str, Any]) -> None:
        if name == "descriptor":
            streams[doc["uid"]] = doc["name"]
        elif name == "event":
            data[streams[doc["descriptor"]]].append(doc["data"])

    subs = {"descriptor": [monitor], "event": [monitor]} if monitor is not None else {}
    RE.subscribe(collect)
    gate = fluorescence_quality_gate(
        qepro,
        light_path,
        monitor,
        num_spectra,
        good_target=good_target,
        max_bad=max_bad,
    )
    RE(bpp.subs_wrapper(bpp.run_wrapper(gate), subs))
    return dict(data)


def test_gate_classifies_last_spectrum_until_good_target(
    RE: RunEngine,
    qepro: QEPro,
    light_path: UVVisLightPath,
    spectra: dict[str, np.ndarray],
) -> None:
    good, weak = spectra["good"], spectra["weak"]
    _script_spectra(qepro, [good, weak, weak, good, weak, good])

    data = _run_gate(
        RE, qepro, light_path, PLQualityMonitor(qepro), 2, good_target=2, max_bad=3
    )

    quality = data["fluorescence_quality"]
    assert len(data["fluorescence"]) == 6
    assert [q["verdict"] for q in quality] == ["bad", "good", "good"]
    assert [q["batch_index"] for q in quality] == [0, 1, 2]
    assert [q["n_good_total"] for q in quality] == [0, 1, 2]
    assert [q["n_bad_total"] for q in quality] == [1, 1, 1]
    assert [q["n_events_in_batch"] for q in quality] == [2, 2, 2]
    assert [q["peak_wavelength_nm"] for q in quality] == [520.0] * 3


def test_gate_stops_at_max_bad_and_records_missing_peak(
    RE: RunEngine,
    qepro: QEPro,
    light_path: UVVisLightPath,
    spectra: dict[str, np.ndarray],
) -> None:
    _script_spectra(qepro, [spectra["led_only"]] * 2)
    monitor = PLQualityMonitor(qepro)

    data = _run_gate(RE, qepro, light_path, monitor, 1, good_target=2, max_bad=2)

    quality = data["fluorescence_quality"]
    assert [q["verdict"] for q in quality] == ["bad", "bad"]
    assert [q["n_bad_total"] for q in quality] == [1, 2]
    assert [q["peak_wavelength_nm"] for q in quality] == [NO_PEAK_WAVELENGTH] * 2
    assert monitor.batch_results == quality


def test_monitor_classifies_with_configured_thresholds(
    RE: RunEngine,
    qepro: QEPro,
    light_path: UVVisLightPath,
    spectra: dict[str, np.ndarray],
) -> None:
    _script_spectra(qepro, [spectra["good"]])
    monitor = PLQualityMonitor(qepro, key_height=2000.0)

    data = _run_gate(RE, qepro, light_path, monitor, 1, good_target=1, max_bad=1)

    assert [q["verdict"] for q in data["fluorescence_quality"]] == ["bad"]


def test_gate_without_monitor_runs_one_ungated_batch(
    RE: RunEngine, qepro: QEPro, light_path: UVVisLightPath
) -> None:
    data = _run_gate(RE, qepro, light_path, None, 3)

    assert len(data["fluorescence"]) == 3
    assert "fluorescence_quality" not in data


def test_gate_rejects_batch_without_spectrum(
    RE: RunEngine, qepro: QEPro, light_path: UVVisLightPath
) -> None:
    with pytest.raises(RuntimeError, match="recorded no spectrum"):
        _run_gate(RE, qepro, light_path, PLQualityMonitor(qepro), 0)
