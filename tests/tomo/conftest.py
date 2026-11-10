"""Fixtures for the tomography plan tests."""

from pathlib import Path

import pytest
from tomo_rig import TomoRig, make_tomo_rig


@pytest.fixture
async def tomo_rig(tmp_path: Path) -> TomoRig:
    return await make_tomo_rig(tmp_path)


@pytest.fixture
def docs(RE) -> dict[str, list[dict]]:
    """Every document the RunEngine emits, by name."""
    collected: dict[str, list[dict]] = {}
    RE.subscribe(lambda name, doc: collected.setdefault(name, []).append(doc))
    return collected
