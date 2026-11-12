"""Tests for install(), which loads the ported Queue Server plans into a profile."""

from unittest.mock import MagicMock

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
import pytest

from xpd_tools.optimization import qserver


def _profile(**extra):
    ns = {"__name__": "profile", "bps": bps, "bpp": bpp}
    ns.update({name: MagicMock(name=name) for name in qserver.PROFILE_NAMES})
    ns.update(extra)
    return ns


def test_install_exports_the_plans():
    ns = _profile(dds2_p1=MagicMock(name="dds2_p1"))
    qserver.install(ns)
    for name in qserver.XRAY_UVVIS_NAMES + qserver.ACQUIRE_VARIANTS_NAMES:
        assert name in ns
    assert ns["_resolve_pumps"](["dds2_p1"]) == [ns["dds2_p1"]]


def test_install_again_rebinds_every_profile_name():
    """A second install must not leave plans on the first install's hardware."""
    old_pump, new_pump = MagicMock(name="old"), MagicMock(name="new")
    qserver.install(_profile(dds2_p1=old_pump))
    ns = _profile(dds2_p1=new_pump)
    qserver.install(ns)
    assert ns["_resolve_pumps"](["dds2_p1"]) == [new_pump]
    assert vars(qserver.acquire_variants)["dds2_p1"] is new_pump
    assert vars(qserver.xray_uvvis)["LED"] is ns["LED"]


def test_install_again_forgets_names_the_profile_no_longer_has():
    qserver.install(_profile(dds3_p1=MagicMock(name="dds3_p1")))
    ns = _profile()
    qserver.install(ns)
    with pytest.raises(ValueError, match="dds3_p1"):
        ns["_resolve_pumps"](["dds3_p1"])


def test_install_keeps_the_modules_own_definitions():
    ns = _profile()
    qserver.install(ns)
    qserver.install(ns)  # the namespace now also holds the exported plans
    assert vars(qserver.xray_uvvis)["steady_state_flow"] is ns["steady_state_flow"]
    assert ns["xray_uvvis_acquire"].__module__ == qserver.xray_uvvis.__name__


def test_install_names_what_the_profile_is_missing():
    ns = _profile()
    del ns["pe1c"]
    with pytest.raises(NameError, match="pe1c"):
        qserver.install(ns)
