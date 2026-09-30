import numpy as np
import pytest


@pytest.fixture
def wavelength() -> np.ndarray:
    return np.arange(200.0, 951.0)
