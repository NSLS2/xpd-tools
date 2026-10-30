from importlib.metadata import version

import xpd_tools
from xpd_tools._version import __version__


def test_package_imports():
    assert xpd_tools.__name__ == "xpd_tools"


def test_version_matches_installed_metadata():
    assert __version__ == version("xpd-tools")
