import numpy as np
import pytest
from ophyd_async.core import init_devices

from xpd_tools.detectors import QEPro


@pytest.fixture
def wavelength() -> np.ndarray:
    return np.arange(200.0, 951.0)


@pytest.fixture
async def qepro() -> QEPro:
    async with init_devices(mock=True):
        qepro = QEPro("SIM:QEPRO:", name="QEPro")
    return qepro
