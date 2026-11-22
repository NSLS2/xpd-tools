"""Tests for the acquisition settings, their Redis-backed store and their plans."""

from fnmatch import fnmatchcase
from typing import Any

import pytest
from bluesky.run_engine import RunEngine
from redis_json_dict.redis_json_dict import RedisJSONDict

from xpd_tools.acquisition_settings import (
    AcquisitionConfiguration,
    AcquisitionSettings,
    PersistentAcquisitionSettings,
)
from xpd_tools.plans import acquisition_settings_plans

XPD = AcquisitionSettings(
    frame_acq_time=0.5,
    dark_window=1000.0,
    shutter_control=True,
    auto_load_calibration=True,
)


class FakeRedis:
    """The part of a Redis client that `RedisJSONDict` uses, kept in memory."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: bytes) -> None:
        self.data[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)

    def scan_iter(self, match: str):
        return (key.encode() for key in list(self.data) if fnmatchcase(key, match))

    def pipeline(self) -> "FakeRedis":
        return self

    def execute(self) -> None:
        pass


def test_every_configuration_has_settings():
    for configuration in AcquisitionConfiguration:
        assert isinstance(configuration.settings, AcquisitionSettings)


def test_new_settings_start_with_the_preset():
    assert PersistentAcquisitionSettings({}).get() == XPD


def test_stored_settings_survive_a_restart():
    store: dict[str, Any] = {}
    PersistentAcquisitionSettings(store).update(
        frame_acq_time=0.1, shutter_control=False
    )
    restarted = PersistentAcquisitionSettings(store)
    assert restarted.get().frame_acq_time == 0.1
    assert restarted.get().shutter_control is False


def test_two_sessions_on_one_store_see_each_others_changes():
    store: dict[str, Any] = {}
    bsui = PersistentAcquisitionSettings(store)
    queue_server = PersistentAcquisitionSettings(store)
    bsui.update(dark_window=10)
    assert queue_server.get().dark_window == 10.0


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"frame_acq_time": 0}, ValueError),
        ({"frame_acq_time": -0.1}, ValueError),
        ({"dark_window": 0.0}, ValueError),
        ({"frame_acq_time": "0.5"}, TypeError),
        ({"dark_window": True}, TypeError),
        ({"shutter_control": "yes"}, TypeError),
        ({"dk_window": 10}, TypeError),
    ],
)
def test_a_bad_change_is_refused_and_changes_nothing(
    changes: dict[str, Any], error: type[Exception]
):
    settings = PersistentAcquisitionSettings({})
    with pytest.raises(error):
        settings.update(**changes)
    assert settings.get() == XPD


def test_values_round_trip_through_redis_json_dict():
    redis = FakeRedis()
    store = RedisJSONDict(redis, "acquisition_settings:")
    PersistentAcquisitionSettings(store).update(frame_acq_time=0.1)
    restarted = PersistentAcquisitionSettings(
        RedisJSONDict(redis, "acquisition_settings:")
    )
    assert restarted.get() == AcquisitionSettings(
        frame_acq_time=0.1,
        dark_window=1000.0,
        shutter_control=True,
        auto_load_calibration=True,
    )
    assert set(redis.data) == {
        "acquisition_settings:frame_acq_time",
        "acquisition_settings:dark_window",
        "acquisition_settings:shutter_control",
        "acquisition_settings:auto_load_calibration",
    }


def test_set_plan_changes_only_what_is_given(RE: RunEngine):
    settings = PersistentAcquisitionSettings({})
    _, set_acquisition_settings = acquisition_settings_plans(settings)
    RE(set_acquisition_settings(frame_acq_time=0.1, shutter_control=False))
    assert settings.get() == AcquisitionSettings(
        frame_acq_time=0.1,
        dark_window=1000.0,
        shutter_control=False,
        auto_load_calibration=True,
    )


def test_use_plan_restores_the_preset(RE: RunEngine):
    settings = PersistentAcquisitionSettings({})
    use_acquisition_configuration, _ = acquisition_settings_plans(settings)
    settings.update(frame_acq_time=0.1, dark_window=10)
    RE(use_acquisition_configuration("xpd"))
    assert settings.get() == XPD


def test_plans_refuse_bad_input_and_change_nothing(RE: RunEngine):
    settings = PersistentAcquisitionSettings({})
    use_acquisition_configuration, set_acquisition_settings = (
        acquisition_settings_plans(settings)
    )
    with pytest.raises(ValueError, match="bogus"):
        RE(use_acquisition_configuration("bogus"))
    with pytest.raises(ValueError, match="frame_acq_time"):
        RE(set_acquisition_settings(frame_acq_time=0.0))
    assert settings.get() == XPD
