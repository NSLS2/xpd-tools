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

from xpd_tools.detectors.emergent_vision import (
    EmergentVisionDetector,
    framerate_for_period,
)


@pytest.fixture
async def evt(tmp_path: Path) -> EmergentVisionDetector:
    path_provider = StaticPathProvider(UUIDFilenameProvider(), tmp_path)
    async with init_devices(mock=True):
        evt = EmergentVisionDetector(
            "EVT:", ADWriterFactory.hdf(path_provider), name="evt"
        )
    return evt


@pytest.mark.parametrize(
    ("trigger_info", "image_mode", "framerate"),
    [
        (TriggerInfo(number_of_events=3, livetime=0.5), ADImageMode.MULTIPLE, 2.0),
        (TriggerInfo(number_of_events=3, livetime=2.0), ADImageMode.MULTIPLE, 1.0),
        (
            TriggerInfo(number_of_events=3, livetime=0.02, deadtime=0.01),
            ADImageMode.MULTIPLE,
            33.0,
        ),
    ],
)
async def test_prepare_internal(
    evt: EmergentVisionDetector,
    trigger_info: TriggerInfo,
    image_mode: ADImageMode,
    framerate: float,
):
    await evt.prepare(trigger_info)
    driver = evt.driver
    assert await driver.image_mode.get_value() == image_mode
    assert await driver.num_images.get_value() == 3
    # The driver's AcquireTime is in milliseconds.
    assert await driver.acquire_time.get_value() == pytest.approx(
        trigger_info.livetime * 1000
    )
    assert await driver.framerate.get_value() == framerate


async def test_zero_deadtime_resets_framerate(evt: EmergentVisionDetector):
    await evt.prepare(TriggerInfo(number_of_events=3, livetime=0.02, deadtime=0.98))
    assert await evt.driver.framerate.get_value() == 1.0
    await evt.prepare(TriggerInfo(number_of_events=3, livetime=0.02, deadtime=0))
    assert await evt.driver.framerate.get_value() == 50.0


async def test_readout_time_sets_deadtime(evt: EmergentVisionDetector):
    await evt.driver.readout_time.set(0.005)
    _, deadtime = await evt.get_trigger_deadtime()
    assert deadtime == 0.005
    assert "evt-driver-readout_time" in await evt.read_configuration()
    await evt.prepare(TriggerInfo(number_of_events=3, livetime=0.02))
    assert await evt.driver.framerate.get_value() == 40.0


async def test_external_trigger_rejected(evt: EmergentVisionDetector):
    with pytest.raises(ValueError, match="(?i)trigger"):
        await evt.prepare(
            TriggerInfo(number_of_events=1, trigger=DetectorTrigger.EXTERNAL_EDGE)
        )


@pytest.mark.parametrize(("period", "expected"), [(0.03, 33), (0.5, 2), (1.0, 1)])
def test_framerate_for_period(period: float, expected: int):
    assert framerate_for_period(period) == expected


def test_framerate_for_period_too_long():
    with pytest.raises(ValueError, match="exceeds 1 s"):
        framerate_for_period(1.5)


def test_mock_internal_acquisition(RE: RunEngine, evt: EmergentVisionDetector):
    set_mock_value(evt.driver.acquire_time, 1.0)
    docs: dict[str, list] = {}
    RE.subscribe(lambda name, doc: docs.setdefault(name, []).append(doc))

    RE(bp.count([evt]))

    assert docs["stop"][0]["exit_status"] == "success"
    assert len(docs["stream_datum"]) == 1
