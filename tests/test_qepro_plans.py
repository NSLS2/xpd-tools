"""Behavioral tests for reusable QEPro plan primitives."""

from collections.abc import Callable, Generator
from typing import Any, cast

import bluesky.plan_stubs as bps
import pytest
from bluesky import Msg
from bluesky.run_engine import RunEngine
from ophyd_async.core import callback_on_mock_put, init_devices

from xpd_tools.detectors import QEPro
from xpd_tools.qepro_plans import (
    configure_qepro,
    set_qepro_temperature,
    take_qepro_dark_frame,
    take_qepro_reference_frame,
)


@pytest.fixture
async def qepro() -> QEPro:
    async with init_devices(mock=True):
        device = QEPro("SIM:", name="QEPro")
    return device


def _read(signal) -> Generator[Msg, None, object]:
    return (yield from bps.rd(signal))


def _read_value(RE: RunEngine, signal) -> object:
    return cast(Any, RE(_read(signal))).plan_result


def test_configure_qepro_sets_collection_contract(RE: RunEngine, qepro: QEPro) -> None:
    RE(
        configure_qepro(
            qepro,
            integration_time=25.0,
            num_spectra_to_average=1,
            buffer=7,
            spectrum_type="Corrected Sample",
            correction_type="Dark",
            electric_dark_correction=True,
        )
    )

    assert _read_value(RE, qepro.integration_time) == 25.0
    assert _read_value(RE, qepro.num_spectra) == 1
    assert _read_value(RE, qepro.buff_capacity) == 7
    assert _read_value(RE, qepro.collect_mode) == "Single"
    assert _read_value(RE, qepro.electric_dark_correction) is True
    assert _read_value(RE, qepro.correction) == "Dark"
    assert _read_value(RE, qepro.spectrum_type) == "Corrected Sample"

    RE(configure_qepro(qepro, num_spectra_to_average=2, electric_dark_correction=False))

    assert _read_value(RE, qepro.collect_mode) == "Average"
    assert _read_value(RE, qepro.electric_dark_correction) is True


@pytest.mark.parametrize(
    ("frame_plan", "requested_spectrum_type"),
    [
        (take_qepro_dark_frame, "Dark"),
        (take_qepro_reference_frame, "Reference"),
    ],
)
def test_frame_plans_restore_prior_spectrum_type(
    RE: RunEngine,
    qepro: QEPro,
    frame_plan: Callable[..., Generator[Msg, None, None]],
    requested_spectrum_type: str,
) -> None:
    RE(bps.abs_set(qepro.spectrum_type, "Corrected Sample", wait=True))
    spectrum_type_writes: list[str] = []
    callback_on_mock_put(qepro.spectrum_type, spectrum_type_writes.append)

    RE(frame_plan(qepro, settle_time=0))

    assert spectrum_type_writes == [requested_spectrum_type, "Corrected Sample"]
    assert _read_value(RE, qepro.spectrum_type) == "Corrected Sample"
    assert _read_value(RE, qepro.acquire) is False


def test_set_qepro_temperature_waits_for_tec_convergence(
    RE: RunEngine, qepro: QEPro
) -> None:
    RE(set_qepro_temperature(qepro, 23.5))

    assert _read_value(RE, qepro.tec_device.tec_temp) == 23.5
    assert _read_value(RE, qepro.tec_device.curr_tec_temp) == 23.5
