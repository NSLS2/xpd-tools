"""Mock XPDD tomography devices for the tomo plan tests."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from nslsii.ophyd_async.devices import Xspress3Detector, Xspress3HDFWriterFactory
from ophyd_async.core import (
    StaticPathProvider,
    UUIDFilenameProvider,
    callback_on_mock_execute,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)
from ophyd_async.epics.adcore import ADWriterFactory, NDFileHDF5IO
from ophyd_async.epics.adkinetix import KinetixDetector, KinetixTriggerMode
from ophyd_async.epics.motor import Motor
from ophyd_async.fastcs.panda import DatasetTable, PandaHdf5DatasetType
from panda_sim import HDFPandaWithCalc, add_pcomp_inp

from xpd_tools.detectors.pilatus4 import Pilatus4Detector
from xpd_tools.motors import RotationMotor
from xpd_tools.shutter import Shutter, ShutterStatus
from xpd_tools.tomo import DetectorConfig
from xpd_tools.tomo.flyscans import ROTATION_DATASET_NAME

#: The PandA TTL output each rig detector's trigger input is wired to.
TTL_WIRING = {"pilatus1": 1, "xs3": 2, "iris1": 3}


@dataclass
class TomoRig:
    """Mock XPDD tomography devices, wired together so a flyscan completes."""

    pilatus: Pilatus4Detector
    panda: HDFPandaWithCalc
    rot_motor: RotationMotor
    sample_out_motor: Motor
    fe_shutter: Shutter
    photon_shutter: Shutter
    #: A second imaging detector, for per-detector exposure tests.
    iris: KinetixDetector
    #: A fluorescence detector, which takes no darks/flats.
    xs3: Xspress3Detector
    #: Base pulses the simulated fly move produces; set it before a flyscan.
    num_frames: int = 11
    #: "open"/"close" for every shutter command, as "<shutter>:<cmd>".
    shutter_log: list[str] = field(default_factory=list)

    def config(self, det, nproj: int, exposure_time: float, **kwargs):
        """A detector config for ``det`` on the TTL output it is wired to."""
        return DetectorConfig(
            detector=det,
            nproj=nproj,
            exposure_time=exposure_time,
            ttl_out=TTL_WIRING[det.name],
            **kwargs,
        )

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


async def _fires_on(panda: HDFPandaWithCalc, ttl_out: int, pulse: int) -> bool:
    """Whether a TTL output fires on base pulse ``pulse``, as the PandA wires it."""
    source = await panda.ttlout[ttl_out].val.get_value()
    if source == "PULSE1.OUT":
        return True
    if source.startswith("DIV") and source.endswith(".OUTD"):
        div = panda.div[int(source[3])]
        assert await div.inp.get_value() == "PULSE1.OUT"
        divisor = round(await div.divisor.get_value())
        first = 0 if await div.first_pulse.get_value() == "OutD" else divisor - 1
        return pulse % divisor == first
    return False


def _simulate_fly_move(rig: TomoRig) -> None:
    """Produce captures when the rotation motor starts its fly move.

    The flyscan writes the rotation setpoint twice: the run-up move in prepare,
    then the fly move in kickoff. The PandA captures on every one of
    ``num_frames`` base pulses, and each detector on the pulses its TTL output
    passes on.
    """
    panda = rig.panda
    hdfs = {
        TTL_WIRING[det.name]: det.get_plugin("hdf", NDFileHDF5IO)
        for det in (rig.pilatus, rig.iris, rig.xs3)
    }
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

            async def _captures():
                for pulse in range(rig.num_frames):
                    await asyncio.sleep(0.002)
                    captured = await panda.data.num_captured.get_value()
                    set_mock_value(panda.data.num_captured, captured + 1)
                    for ttl_out, hdf in hdfs.items():
                        if await _fires_on(panda, ttl_out, pulse):
                            captured = await hdf.num_captured.get_value()
                            set_mock_value(hdf.num_captured, captured + 1)
                set_mock_value(panda.pcomp[1].active, False)

            asyncio.ensure_future(_captures())
        if original_motor_cb:
            return original_motor_cb(value)

    callback_on_mock_put(rig.rot_motor.user_setpoint, _on_setpoint)


def _simulate_kinetix_internal(det: KinetixDetector) -> None:
    """Capture ``num_images`` frames when the Kinetix acquires internally.

    Externally triggered frames come from :func:`_simulate_fly_move` instead.
    """
    hdf = det.get_plugin("hdf", NDFileHDF5IO)

    async def _capture() -> None:
        if await det.driver.trigger_mode.get_value() != KinetixTriggerMode.INTERNAL:
            return
        num_images, captured = await asyncio.gather(
            det.driver.num_images.get_value(), hdf.num_captured.get_value()
        )
        set_mock_value(hdf.num_captured, captured + num_images)

    def _on_acquire(value: bool) -> None:
        if value:
            asyncio.ensure_future(_capture())

    callback_on_mock_put(det.driver.acquire, _on_acquire)


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
        iris = KinetixDetector(
            "DET2:", ADWriterFactory.hdf(path_provider), name="iris1"
        )
        xs3 = Xspress3Detector(
            "XSP:",
            Xspress3HDFWriterFactory(path_provider),
            channel_numbers=(1,),
            mca_roi_numbers=(1,),
            name="xs3",
        )

    set_mock_value(rot_motor.encoder_resolution, 0.1)
    set_mock_value(rot_motor.max_velocity, 1000.0)
    set_mock_value(rot_motor.acceleration_time, 0.0)
    set_mock_value(rot_motor.velocity, 5.0)
    set_mock_value(sample_out_motor.velocity, 1.0)
    set_mock_value(sample_out_motor.user_setpoint, 3.0)
    set_mock_value(sample_out_motor.user_readback, 3.0)
    set_mock_value(pilatus.driver.acquire_time, 0.5)
    set_mock_value(pilatus.driver.acquire_period, 0.6)
    set_mock_value(iris.driver.acquire_time, 0.4)
    set_mock_value(iris.get_plugin("hdf", NDFileHDF5IO).file_path_exists, True)
    _simulate_kinetix_internal(iris)
    set_mock_value(xs3.get_plugin("hdf", NDFileHDF5IO).file_path_exists, True)
    # What warmup() leaves behind: one (channels, bins) frame.
    set_mock_value(xs3.driver.array_size_y, 1)
    set_mock_value(xs3.driver.array_size_x, 4096)

    rig = TomoRig(
        pilatus,
        panda,
        rot_motor,
        sample_out_motor,
        fe_shutter,
        photon_shutter,
        iris,
        xs3,
    )
    for shutter in (fe_shutter, photon_shutter):
        _make_shutter_instant(shutter, rig.shutter_log)
    set_mock_value(fe_shutter.status, ShutterStatus.OPEN)
    _simulate_fly_move(rig)
    return rig
