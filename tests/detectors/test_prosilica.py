from pathlib import Path

import bluesky.plans as bp
import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    DetectorTrigger,
    StaticPathProvider,
    TriggerInfo,
    UUIDFilenameProvider,
    init_devices,
    set_mock_value,
)
from ophyd_async.epics.adcore import ADImageMode, ADWriterFactory

from xpd_tools.detectors.prosilica import (
    ProsilicaDetector,
    ProsilicaExposureMode,
    ProsilicaLegacyDetector,
    ProsilicaTriggerEvent,
    ProsilicaTriggerMode,
)


@pytest.fixture
async def prosilica(tmp_path: Path) -> ProsilicaDetector:
    path_provider = StaticPathProvider(UUIDFilenameProvider(), tmp_path)
    async with init_devices(mock=True):
        prosilica = ProsilicaDetector(
            "PROS:",
            ADWriterFactory.hdf(path_provider),
            trigger_input=ProsilicaTriggerMode.SYNC_IN_2,
            name="prosilica",
        )
    return prosilica


@pytest.mark.parametrize(
    ("trigger_info", "trigger_mode", "exposure_mode", "trigger_event", "period"),
    [
        (
            TriggerInfo(number_of_events=3, livetime=0.5),
            ProsilicaTriggerMode.FREE_RUN,
            ProsilicaExposureMode.MANUAL,
            None,
            None,
        ),
        (
            TriggerInfo(number_of_events=3, livetime=0.5, deadtime=0.1),
            ProsilicaTriggerMode.FIXED_RATE,
            ProsilicaExposureMode.MANUAL,
            None,
            0.6,
        ),
        (
            TriggerInfo(
                number_of_events=3,
                trigger=DetectorTrigger.EXTERNAL_EDGE,
                livetime=0.5,
                deadtime=0.1,
            ),
            ProsilicaTriggerMode.SYNC_IN_2,
            ProsilicaExposureMode.MANUAL,
            ProsilicaTriggerEvent.RISING_EDGE,
            None,
        ),
        (
            TriggerInfo(
                number_of_events=3,
                trigger=DetectorTrigger.EXTERNAL_LEVEL,
                deadtime=0.1,
            ),
            ProsilicaTriggerMode.SYNC_IN_2,
            ProsilicaExposureMode.EXTERNAL,
            ProsilicaTriggerEvent.HIGH_LEVEL,
            None,
        ),
    ],
)
async def test_prepare_sets_trigger_mode(
    prosilica: ProsilicaDetector,
    trigger_info: TriggerInfo,
    trigger_mode: ProsilicaTriggerMode,
    exposure_mode: ProsilicaExposureMode,
    trigger_event: ProsilicaTriggerEvent | None,
    period: float | None,
):
    driver = prosilica.driver
    initial_period = await driver.acquire_period.get_value()
    await prosilica.prepare(trigger_info)

    assert await driver.trigger_mode.get_value() == trigger_mode
    assert await driver.exposure_mode.get_value() == exposure_mode
    assert await driver.num_images.get_value() == 3
    assert await driver.image_mode.get_value() == ADImageMode.MULTIPLE
    if trigger_event is not None:
        assert await driver.trigger_event.get_value() == trigger_event
    if trigger_info.livetime:
        assert await driver.acquire_time.get_value() == trigger_info.livetime
    expected_period = initial_period if period is None else period
    assert await driver.acquire_period.get_value() == pytest.approx(expected_period)


def test_invalid_trigger_input_rejected():
    with pytest.raises(ValueError, match="trigger_input"):
        ProsilicaDetector("PROS:", trigger_input=ProsilicaTriggerMode.FREE_RUN)


def test_mock_internal_acquisition(RE: RunEngine, prosilica: ProsilicaDetector):
    set_mock_value(prosilica.driver.acquire_time, 0.001)
    docs: dict[str, list] = {}
    RE.subscribe(lambda name, doc: docs.setdefault(name, []).append(doc))

    RE(bp.count([prosilica]))

    assert docs["stop"][0]["exit_status"] == "success"
    assert len(docs["stream_datum"]) == 1


@pytest.fixture
async def prosilica_legacy(tmp_path: Path) -> ProsilicaLegacyDetector:
    path_provider = StaticPathProvider(UUIDFilenameProvider(), tmp_path)
    async with init_devices(mock=True):
        prosilica_legacy = ProsilicaLegacyDetector(
            "PROS:",
            ADWriterFactory.hdf(path_provider),
            trigger_input=ProsilicaTriggerMode.SYNC_IN_2,
            name="prosilica_legacy",
        )
    return prosilica_legacy


def test_legacy_driver_omits_missing_pvs(prosilica_legacy: ProsilicaLegacyDetector):
    for attr in ("gain_mode", "exposure_mode", "temperature_mainboard"):
        assert not hasattr(prosilica_legacy.driver, attr)


@pytest.mark.parametrize(
    ("trigger_info", "trigger_mode"),
    [
        (
            TriggerInfo(number_of_events=3, livetime=0.5, deadtime=0.1),
            ProsilicaTriggerMode.FIXED_RATE,
        ),
        (
            TriggerInfo(
                number_of_events=3,
                trigger=DetectorTrigger.EXTERNAL_EDGE,
                livetime=0.5,
                deadtime=0.1,
            ),
            ProsilicaTriggerMode.SYNC_IN_2,
        ),
    ],
)
async def test_legacy_prepare_sets_trigger_mode(
    prosilica_legacy: ProsilicaLegacyDetector,
    trigger_info: TriggerInfo,
    trigger_mode: ProsilicaTriggerMode,
):
    await prosilica_legacy.prepare(trigger_info)
    assert await prosilica_legacy.driver.trigger_mode.get_value() == trigger_mode
    assert await prosilica_legacy.driver.num_images.get_value() == 3


async def test_legacy_rejects_level_trigger(prosilica_legacy: ProsilicaLegacyDetector):
    with pytest.raises(ValueError, match="(?i)trigger"):
        await prosilica_legacy.prepare(
            TriggerInfo(
                number_of_events=1,
                trigger=DetectorTrigger.EXTERNAL_LEVEL,
                deadtime=0.1,
            )
        )
