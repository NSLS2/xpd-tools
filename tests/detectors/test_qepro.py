import asyncio
from typing import Any

import numpy as np
import pytest
from ophyd_async.core import callback_on_mock_put, init_devices, set_mock_value

from xpd_tools.detectors import QEPro

FEATURE_MASKS = [
    ("has_nlc_feature", 32),
    ("has_lightsource_feature", 16),
    ("has_edc_feature", 8),
    ("has_buffer_feature", 4),
    ("has_tec_feature", 2),
    ("has_irrad_feature", 1),
]

PV_SUFFIXES = {
    "serial": "SERIAL",
    "model": "MODEL",
    "status": "STATUS",
    "status_msg": "STATUS_MSG",
    "device_connected": "CONNECTED_RBV",
    "check_status": "CHECK_STATUS",
    "features": "FEATURES_RBV",
    "strobe": "STROBE_RBV",
    "electric_dark_correction": "EDC_RBV",
    "non_linearity_correction": "NLC_RBV",
    "shutter": "SHUTTER_RBV",
    "tec_device.tec": "TEC_RBV",
    "tec_device.tec_temp": "TEC_TEMP_RBV",
    "tec_device.curr_tec_temp": "CURR_TEC_TEMP_RBV",
    "light_source": "LIGHT_SOURCE_RBV",
    "light_source_intensity": "LIGHT_SOURCE_INTENSITY_RBV",
    "light_source_count": "LIGHT_SOURCE_COUNT_RBV",
    "num_spectra": "NUM_SPECTRA_RBV",
    "spectra_collected": "SPECTRA_COLLECTED_RBV",
    "int_min_time": "INT_MIN_TIME_RBV",
    "int_max_time": "INT_MAX_TIME_RBV",
    "integration_time": "INTEGRATION_TIME_RBV",
    "buff_min_capacity": "BUFF_MIN_CAPACITY_RBV",
    "buff_max_capacity": "BUFF_MAX_CAPACITY_RBV",
    "buff_capacity": "BUFF_CAPACITY_RBV",
    "buff_element_count": "BUFF_ELEMENT_COUNT_RBV",
    "output": "OUTPUT",
    "sample": "SAMPLE",
    "dark": "DARK",
    "reference": "REFERENCE",
    "formatted_spectrum_len": "FORMATTED_SPECTRUM_LEN_RBV",
    "x_axis": "X_AXIS",
    "x_axis_format": "X_AXIS_FORMAT_RBV",
    "dark_available": "DARK_AVAILABLE_RBV",
    "ref_available": "REF_AVAILABLE_RBV",
    "acquire": "COLLECT_RBV",
    "collect_mode": "COLLECT_MODE_RBV",
    "spectrum_type": "SPECTRUM_TYPE_RBV",
    "correction": "CORRECTION_RBV",
    "trigger_mode": "TRIGGER_MODE_RBV",
}


@pytest.fixture
async def qepro() -> QEPro:
    async with init_devices(mock=True):
        device = QEPro("SIM:", name="QEPro")
    return device


def _source(device: QEPro, path: str) -> str:
    child: Any = device
    for attr in path.split("."):
        child = getattr(child, attr)
    return child.source


async def _next_loop_turn() -> None:
    marker = asyncio.Event()
    asyncio.get_running_loop().call_soon(marker.set)
    await marker.wait()


def test_qepro_pv_contract() -> None:
    qepro = QEPro("SIM:", name="QEPro")

    assert {path: _source(qepro, path) for path in PV_SUFFIXES} == {
        path: f"ca://SIM:{suffix}" for path, suffix in PV_SUFFIXES.items()
    }


@pytest.mark.parametrize(("feature_name", "mask"), FEATURE_MASKS)
async def test_qepro_feature_masks(qepro: QEPro, feature_name: str, mask: int) -> None:
    set_mock_value(qepro.features, mask)

    assert await getattr(qepro, feature_name)() == mask
    for other_feature_name, _ in FEATURE_MASKS:
        if other_feature_name != feature_name:
            assert await getattr(qepro, other_feature_name)() == 0


async def test_qepro_tec_set_completes_only_within_tolerance(qepro: QEPro) -> None:
    tec_enabled = asyncio.Event()

    def hold_temperature(enabled: bool) -> None:
        if enabled:
            tec_enabled.set()

    callback_on_mock_put(qepro.tec_device.tec, hold_temperature)
    status = qepro.tec_device.set(20.0)

    await asyncio.wait_for(tec_enabled.wait(), timeout=1)
    await _next_loop_turn()
    assert await qepro.tec_device.tec_temp.get_value() == 20.0
    assert not status.done

    set_mock_value(qepro.tec_device.curr_tec_temp, 21.0)
    await _next_loop_turn()
    assert not status.done

    set_mock_value(qepro.tec_device.curr_tec_temp, 20.5)
    await status
    assert status.success


async def test_qepro_tec_mock_completes_temperature_change(qepro: QEPro) -> None:
    status = qepro.tec_device.set(25.0)

    await status

    assert await qepro.tec_device.curr_tec_temp.get_value() == 25.0


async def test_qepro_trigger_completes_on_collect_falling_edge(qepro: QEPro) -> None:
    collection_started = asyncio.Event()

    def hold_collection(collect: bool) -> None:
        if collect:
            collection_started.set()

    callback_on_mock_put(qepro.acquire, hold_collection)
    status = qepro.trigger()

    await asyncio.wait_for(collection_started.wait(), timeout=1)
    await _next_loop_turn()
    assert await qepro.acquire.get_value() is True
    assert not status.done

    set_mock_value(qepro.acquire, False)
    await status
    assert status.success


async def test_qepro_mock_completes_collection_cycle(qepro: QEPro) -> None:
    status = qepro.trigger()

    await status

    assert await qepro.acquire.get_value() is False


async def test_qepro_reads_fresh_spectrum_after_trigger(qepro: QEPro) -> None:
    expected_output = np.array([101.0, 202.0])

    def complete_collection(collect: bool) -> None:
        if collect:
            set_mock_value(qepro.output, expected_output)
            asyncio.get_running_loop().call_soon(set_mock_value, qepro.acquire, False)

    callback_on_mock_put(qepro.acquire, complete_collection)

    await qepro.trigger()

    reading = await qepro.read()
    np.testing.assert_array_equal(reading["QEPro_output"]["value"], expected_output)


async def test_qepro_read_fields_include_export_data(qepro: QEPro) -> None:
    reading = await qepro.read()

    expected_fields = {
        "QEPro_x_axis",
        "QEPro_output",
        "QEPro_sample",
        "QEPro_dark",
        "QEPro_reference",
        "QEPro_spectrum_type",
        "QEPro_integration_time",
        "QEPro_num_spectra",
        "QEPro_buff_capacity",
    }

    assert set(reading) == expected_fields
    assert reading["QEPro_spectrum_type"]["value"] == "Absorbtion"

    configuration = await qepro.read_configuration()
    assert set(configuration) == {"QEPro_tec_device_tec_temp"}
