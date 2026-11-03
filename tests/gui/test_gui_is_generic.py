"""Blocking checks for the generic GUI's import and identity boundary.

The identity list is a focused regression check, not an exhaustive semantic
analysis of every possible beamline-specific behavior.
"""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import xpd_tools.gui
from xpd_tools.gui.spec import GuiSpec, load_spec

GUI_DIR = Path(xpd_tools.gui.__file__).parent
LAUNCHER = GUI_DIR / "__main__.py"
FORBIDDEN_IDENTITIES = (
    "HEX",
    "PDF",
    "XPD",
    "XF:27",
    "XF:28",
    "27-ID",
    "28-ID",
    "GeRM",
    "SampleSmall",
    "HEXTOOLS_RUNNING_IN_CI",
    "HEXTOOLS_PLAN_HISTORY_FILE",
    "XPD_TOOLS_RUNNING_IN_CI",
    "Pilatus4",
    "single_axis_flyscan",
    "BEAMLINE_ACRONYM",
    "edxd_scan",
    "move_slits",
)


def _package_for(path: Path) -> str:
    parts = path.relative_to(GUI_DIR).with_suffix("").parts
    return ".".join(("xpd_tools", "gui", *parts[:-1]))


def _import_names(source: str, package: str) -> set[str]:
    """Resolve absolute, relative, and literal dynamic imports in source."""
    names: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
                if node.module is None:
                    names.update(f"{base}.{alias.name}" for alias in node.names)
                else:
                    names.add(base)
            elif node.module:
                names.add(node.module)
                names.update(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                if node.args and isinstance(node.args[0], ast.Constant):
                    if isinstance(node.args[0].value, str):
                        names.add(node.args[0].value)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                value = node.args[0].value
                if value.startswith(".") and len(node.args) > 1:
                    package_arg = node.args[1]
                    if isinstance(package_arg, ast.Constant) and isinstance(
                        package_arg.value, str
                    ):
                        value = importlib.util.resolve_name(value, package_arg.value)
                names.add(value)
    return names


def _outside_gui_imports(source: str, package: str) -> set[str]:
    return {
        name
        for name in _import_names(source, package)
        if (name == "xpd_tools" or name.startswith("xpd_tools."))
        and name != "xpd_tools.gui"
        and not name.startswith("xpd_tools.gui.")
    }


@pytest.mark.parametrize(
    "path",
    [path for path in sorted(GUI_DIR.rglob("*.py")) if path != LAUNCHER],
    ids=lambda path: str(path.relative_to(GUI_DIR)),
)
def test_generic_modules_import_no_beamline_modules(path: Path):
    source = path.read_text()
    assert not _outside_gui_imports(source, _package_for(path)), path


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from xpd_tools import plans", "xpd_tools.gui"),
        ("from ... import plans", "xpd_tools.gui.widgets"),
        ("from ...detectors import Pilatus4Detector", "xpd_tools.gui.widgets"),
        ("importlib.import_module('xpd_tools.detectors.pilatus4')", "xpd_tools.gui"),
        ("__import__('xpd_tools.detectors.pilatus4')", "xpd_tools.gui"),
    ],
)
def test_boundary_detector_rejects_absolute_and_relative_imports(source, package):
    assert _outside_gui_imports(source, package)


def test_generic_modules_do_not_embed_known_beamline_identities():
    for path in sorted(GUI_DIR.rglob("*.py")):
        if path == LAUNCHER:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not any(
            identity in value for identity in FORBIDDEN_IDENTITIES for value in strings
        ), path


def test_minimal_synthetic_spec_runs_generic_entrypoint_without_beamline_imports():
    pytest.importorskip("qtpy")
    pytest.importorskip("bluesky_widgets")
    code = """
import os
import sys
from types import ModuleType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bluesky import RunEngine
from qtpy.QtWidgets import QApplication
from xpd_tools.gui import GuiSpec, Technique, run

module = ModuleType("_synthetic_gui_config")
module.SPEC = GuiSpec(name="Test Beamline", techniques=(Technique("Acquisition"),))
sys.modules[module.__name__] = module
try:
    run("_synthetic_gui_config:SPEC", ["--help"])
except SystemExit as error:
    assert error.code == 0
else:
    raise AssertionError("generic --help did not exit")

from xpd_tools.gui.window import QtDataAcquisitionWindow

app = QApplication.instance() or QApplication([])
window = QtDataAcquisitionWindow(
    RunEngine(), module.SPEC, show=False, end_ipython_on_close=False
)
assert "Acquisition" in window.qt_widget.technique_views
window.close()
app.processEvents()

for prefix in (
    "xpd_tools.detectors",
    "xpd_tools.flyers",
    "xpd_tools.motors",
    "xpd_tools.plans",
    "xpd_tools.profiles",
    "xpd_tools.pump_plans",
    "xpd_tools.pumps",
    "xpd_tools.qepro_plans",
    "xpd_tools.xpdd_config",
):
    assert not any(
        name == prefix or name.startswith(prefix + ".") for name in sys.modules
    ), prefix
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_xpdd_spec_loads_and_names_callable_plans():
    spec = load_spec("xpd_tools.xpdd_config:XPDD_GUI")
    assert isinstance(spec, GuiSpec)
    assert spec.title == "XPD-D Data Acquisition"
    assert [technique.title for technique in spec.techniques] == ["Flyscan"]
    assert [label for label, _ in spec.techniques[0].plans] == ["Single Axis Flyscan"]
    for technique in spec.techniques:
        assert technique.widget_factory is None or callable(technique.widget_factory)
        assert all(callable(plan) for _, plan in technique.plans)


def test_load_spec_refuses_something_that_is_not_a_spec():
    with pytest.raises(TypeError, match="not a GuiSpec"):
        load_spec("xpd_tools.gui.spec:Site")
