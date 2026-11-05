from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from bluesky.run_engine import RunEngine

from xpd_tools.optimization.agent import BuildAgent
from xpd_tools.optimization.helpers.dofs import Pump
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.legacy import (
    build_fake_tiled_clients,
    build_xpd_objects,
    identity_wrap_xray_run,
)
from xpd_tools.optimization.plans import FlowSource


@pytest.fixture
def phase_factory(tmp_path: Path) -> Callable[..., Phase]:
    radial = np.linspace(1.0, 25.0, 200)

    def factory(name: str = "A") -> Phase:
        gr_path = tmp_path / f"{name}.gr"
        np.savetxt(gr_path, np.column_stack((radial, np.sin(radial))))
        cif_path = tmp_path / f"{name}.cif"
        cif_path.write_text(
            "data_test\n_symmetry_space_group_name_H-M 'P 1'\n"
            "_cell_length_a 6\n_cell_length_b 6\n_cell_length_c 6\n"
            "_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n"
        )
        return Phase(name=name, gr=str(gr_path), cif=str(cif_path), minimize=False)

    return factory


def test_build_xpd_objects_covers_every_device_build_local_can_need() -> None:
    devices = build_xpd_objects()
    for key in (
        "led",
        "uv_shutter",
        "fast_shutter",
        "qepro",
        "xray_detector",
        "dds1_p1",
        "dds2_p1",
        "dds2_p2",
        "dds3_p1",
        "ultra1",
        "ultra2",
    ):
        assert key in devices


@patch("xpd_tools.optimization.agent.from_profile")
@patch("xpd_tools.optimization.agent.from_uri")
def test_build_xpd_objects_drives_build_local_end_to_end(
    _from_uri: Any,
    _from_profile: Any,
    phase_factory: Callable[..., Phase],
    RE: RunEngine,
    documents: list[tuple[str, dict[str, Any]]],
) -> None:
    """The actual point of build_xpd_objects(): verifying a BuildAgent
    config builds and its acquisition plan runs locally, using nothing but
    simulated devices -- no queue server, no real hardware."""
    agent = BuildAgent(evaluation_method="xray", queue_server=False)
    agent.set_dofs([Pump(name="CsPb", id="dds2_p1", bounds=(10, 200))])
    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor="CsPbOA",
                sample_label="CsPb",
            )
        ]
    )
    agent.set_xray_objectives(
        screening="unscreened",
        exposure=0.1,
        frame_acq_time=0.1,
        phases=[phase_factory()],
    )

    built = agent.build_local(
        devices=build_xpd_objects(),
        wrap_xray_run=identity_wrap_xray_run,
        mixer_lengths_cm=(0.0,),
        residence_time_ratio=0.0,
    )

    assert built.acquisition_plan is not None
    RE(built.acquisition_plan([{"_id": 1, "infusion_rate_CsPb": 25}], []))

    assert any(name == "start" for name, _ in documents)
    assert any(name == "stop" for name, _ in documents)


def test_build_fake_tiled_clients_drives_a_real_optimize_iteration(
    phase_factory: Callable[..., Phase],
    RE: RunEngine,
) -> None:
    """The actual point of build_fake_tiled_clients(): a full suggest ->
    acquire -> evaluate -> ingest iteration through build_local(), with no
    real Queue Server, Tiled, or sandbox connection anywhere -- not even
    mocked, genuinely absent.
    """
    agent = BuildAgent(evaluation_method="xray", queue_server=False)
    agent.set_dofs([Pump(name="CsPb", id="dds2_p1", bounds=(10, 200))])
    agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor="CsPbOA",
                sample_label="CsPb",
            )
        ]
    )
    phase = phase_factory("Wanted")
    agent.set_xray_objectives(
        screening="unscreened",
        exposure=0.1,
        frame_acq_time=0.1,
        phases=[phase],
    )

    tiled_client, sandbox_client = build_fake_tiled_clients(RE, phases=[phase])

    run_agent = agent.build_local(
        devices=build_xpd_objects(),
        wrap_xray_run=identity_wrap_xray_run,
        mixer_lengths_cm=(0.0,),
        residence_time_ratio=0.0,
        tiled_client=tiled_client,
        sandbox_client=sandbox_client,
    )

    RE(run_agent.optimize(iterations=1))

    summary = run_agent.ax_client.summarize()
    assert len(summary) == 1
    # Echoing the phase's own reference G(r) back as the "measurement"
    # should score as an almost-perfect correlation against itself.
    assert summary["corr_Wanted"].iloc[0] > 0.99
