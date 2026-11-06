"""Tests for the tomo_count helper."""

import asyncio
from pathlib import Path

import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    StaticPathProvider,
    UUIDFilenameProvider,
    init_devices,
    set_mock_value,
    soft_signal_rw,
)
from ophyd_async.epics.adcore import ADWriterFactory

from xpd_tools.detectors.pilatus4 import Pilatus4Detector, Pilatus4TriggerMode
from xpd_tools.plans import tomo_count


@pytest.fixture
async def pilatus1(tmp_path: Path) -> Pilatus4Detector:
    path_provider = StaticPathProvider(
        UUIDFilenameProvider(), tmp_path, create_dir_depth=-2
    )
    async with init_devices(mock=True):
        pilatus1 = Pilatus4Detector(
            "DET1:", ADWriterFactory.hdf(path_provider), name="pilatus1"
        )
    return pilatus1


def _run(RE: RunEngine, plan) -> dict[str, list]:
    docs: dict[str, list] = {}
    RE(plan, lambda name, doc: docs.setdefault(name, []).append(doc))
    return docs


def test_tomo_count_sets_exposure_time(RE: RunEngine, pilatus1: Pilatus4Detector):
    docs = _run(RE, tomo_count([pilatus1], num=3, exposure_time=0.01))

    assert docs["stop"][0]["exit_status"] == "success"
    assert len(docs["event"]) == 3
    assert len(docs["stream_datum"]) == 3
    driver = pilatus1.driver
    assert asyncio.run(driver.acquire_time.get_value()) == pytest.approx(0.01)
    # AcquirePeriod follows the exposure, plus the detector's own deadtime
    assert asyncio.run(driver.acquire_period.get_value()) == pytest.approx(0.011)
    assert (
        asyncio.run(driver.trigger_mode.get_value())
        == Pilatus4TriggerMode.INTERNAL_SERIES
    )


def test_tomo_count_keeps_current_exposure_when_not_given(
    RE: RunEngine, pilatus1: Pilatus4Detector
):
    set_mock_value(pilatus1.driver.acquire_time, 0.02)
    set_mock_value(pilatus1.driver.acquire_period, 0.03)

    docs = _run(RE, tomo_count([pilatus1]))

    assert docs["stop"][0]["exit_status"] == "success"
    assert asyncio.run(pilatus1.driver.acquire_time.get_value()) == pytest.approx(0.02)


def test_tomo_count_records_metadata(RE: RunEngine, pilatus1: Pilatus4Detector):
    docs = _run(
        RE,
        tomo_count(
            [pilatus1],
            num=2,
            exposure_time=0.01,
            sample_name="sample A",
            md={"operator": "mt"},
        ),
    )

    start = docs["start"][0]
    assert start["plan_name"] == "tomo_count"
    assert start["detectors"] == ["pilatus1"]
    assert start["num_points"] == 2
    assert start["exposure_time"] == 0.01
    assert start["sample_name"] == "sample A"
    assert start["operator"] == "mt"


def test_tomo_count_reads_other_readables_alongside(
    RE: RunEngine, pilatus1: Pilatus4Detector
):
    temperature = soft_signal_rw(float, initial_value=25.0, name="temperature")

    docs = _run(RE, tomo_count([pilatus1, temperature], num=2, exposure_time=0.01))

    assert docs["stop"][0]["exit_status"] == "success"
    assert [e["data"]["temperature"] for e in docs["event"]] == [25.0, 25.0]
