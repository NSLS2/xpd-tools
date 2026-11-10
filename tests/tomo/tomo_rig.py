"""Mock XPDD tomography devices for the tomo plan tests."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ophyd_async.core import (
    StaticPathProvider,
    UUIDFilenameProvider,
    callback_on_mock_execute,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)
from ophyd_async.epics.adcore import ADWriterFactory, NDFileHDF5IO
from ophyd_async.epics.motor import Motor
from ophyd_async.fastcs.panda import DatasetTable, PandaHdf5DatasetType
from panda_sim import HDFPandaWithCalc, add_pcomp_inp

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.motors import RotationMotor
from xpd_tools.shutter import Shutter, ShutterStatus
from xpd_tools.tomo.flyscans import ROTATION_DATASET_NAME


@dataclass
class TomoRig:
    """Mock XPDD tomography devices, wired together so a flyscan completes."""

    pilatus: Pilatus4Detector
    panda: HDFPandaWithCalc
    rot_motor: RotationMotor
    sample_out_motor: Motor
    fe_shutter: Shutter
    photon_shutter: Shutter
    #: Frames the simulated fly move produces; set it before a flyscan.
    num_frames: int = 11
    #: "open"/"close" for every shutter command, as "<shutter>:<cmd>".
    shutter_log: list[str] = field(default_factory=list)

    def device_kwargs(self) -> dict:
        """Supporting devices, as keyword arguments to the plans."""
        return {
            "panda": self.panda,
            "rot_motor": self.rot_motor,
            "fe_shutter": self.fe_shutter,
            "photon_shutter": self.photon_shutter,
        }


def _make_shutter_instant(shutter: Shutter, log: list[str]) -> None:
    def _cmd(name: str, status: ShutterStatus):
        def _execute() -> None:
            log.append(f"{shutter.name}:{name}")
            set_mock_value(shutter.status, status)

        return _execute

    callback_on_mock_execute(shutter.open_cmd, _cmd("open", ShutterStatus.OPEN))
    callback_on_mock_execute(shutter.close_cmd, _cmd("close", ShutterStatus.CLOSED))


def _simulate_fly_move(rig: TomoRig) -> None:
    """Produce captures when the rotation motor starts its fly move.

    The flyscan writes the rotation setpoint twice: the run-up move in prepare,
    then the fly move in kickoff.
    """
    panda = rig.panda
    hdf = rig.pilatus.get_plugin("hdf", NDFileHDF5IO)
    callback_on_mock_put(panda.pcap.arm, lambda v: set_mock_value(panda.pcap.active, v))
    set_mock_value(panda.data.directory_exists, True)
    set_mock_value(
        panda.data.datasets,
        DatasetTable(
            name=[ROTATION_DATASET_NAME], dtype=[PandaHdf5DatasetType.FLOAT_64]
        ),
    )

    from ophyd_async.core._mock_signal_utils import (  # noqa: PLC2701
        _get_mock_signal_backend,
    )

    backend = _get_mock_signal_backend(rig.rot_motor.user_setpoint)
    original_motor_cb = backend._mock_put_callback
    move_count = [0]

    def _on_setpoint(value):
        move_count[0] += 1
        if move_count[0] % 2 == 0:
            set_mock_value(panda.pcomp[1].active, True)
            start_panda = panda.data.num_captured.get_value()
            start_hdf = hdf.num_captured.get_value()

            async def _captures():
                panda_base, hdf_base = await asyncio.gather(start_panda, start_hdf)
                for i in range(rig.num_frames):
                    await asyncio.sleep(0.002)
                    set_mock_value(panda.data.num_captured, panda_base + i + 1)
                    set_mock_value(hdf.num_captured, hdf_base + i + 1)
                set_mock_value(panda.pcomp[1].active, False)

            asyncio.ensure_future(_captures())
        if original_motor_cb:
            return original_motor_cb(value)

    callback_on_mock_put(rig.rot_motor.user_setpoint, _on_setpoint)


async def make_tomo_rig(tmp_path: Path) -> TomoRig:
    """Connect mock tomography devices that complete flyscans and dark/flats."""
    path_provider = StaticPathProvider(
        UUIDFilenameProvider(), tmp_path, create_dir_depth=-2
    )
    async with init_devices(mock=True):
        panda = HDFPandaWithCalc("PANDA:", path_provider)
    await add_pcomp_inp(panda)
    async with init_devices(mock=True):
        rot_motor = RotationMotor("ROT:", name="amPhi")
        sample_out_motor = Motor("SMPL:X:", name="amTopX")
        pilatus = Pilatus4Detector(
            "DET1:", ADWriterFactory.hdf(path_provider), name="pilatus1"
        )
        fe_shutter = Shutter("FE:", name="fe_shutter")
        photon_shutter = Shutter("PH:", name="photon_shutter")

    set_mock_value(rot_motor.encoder_resolution, 0.1)
    set_mock_value(rot_motor.max_velocity, 1000.0)
    set_mock_value(rot_motor.acceleration_time, 0.0)
    set_mock_value(rot_motor.velocity, 5.0)
    set_mock_value(sample_out_motor.velocity, 1.0)
    set_mock_value(sample_out_motor.user_setpoint, 3.0)
    set_mock_value(sample_out_motor.user_readback, 3.0)
    set_mock_value(pilatus.driver.acquire_time, 0.5)
    set_mock_value(pilatus.driver.acquire_period, 0.6)

    rig = TomoRig(
        pilatus, panda, rot_motor, sample_out_motor, fe_shutter, photon_shutter
    )
    for shutter in (fe_shutter, photon_shutter):
        _make_shutter_instant(shutter, rig.shutter_log)
    set_mock_value(fe_shutter.status, ShutterStatus.OPEN)
    _simulate_fly_move(rig)
    return rig
