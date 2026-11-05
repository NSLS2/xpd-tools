"""Blocking boundary for the generic widgets changed in this follow-up."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest


def _imports(source: str, package: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = resolve_name("." * node.level + (node.module or ""), package)
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
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = node.args[0].value
                if name.startswith(".") and len(node.args) > 1:
                    package_arg = node.args[1]
                    if isinstance(package_arg, ast.Constant) and isinstance(
                        package_arg.value, str
                    ):
                        name = resolve_name(name, package_arg.value)
                names.add(name)
    return names


@pytest.mark.parametrize("module", ["plan_widget", "shutter_status", "device_sources"])
def test_generic_widgets_do_not_import_beamline_policy(module):
    path = Path(__file__).parents[2] / "src" / "xpd_tools" / "gui" / f"{module}.py"
    source = path.read_text()
    tree = ast.parse(source)
    names = _imports(source, "xpd_tools.gui")
    assert "xpd_tools.gui.xpdd_config" not in names
    assert not any(
        name == "xpd_tools" or name.startswith("xpd_tools.")
        for name in names
        if not name.startswith("xpd_tools.gui.")
    ), f"{module} imports beamline modules: {names}"
    assert not any(
        identity in node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        for identity in (
            "move_slits",
            "edxd_scan",
            "edxd_2theta_tilt",
            "Two theta",
            "sleep_time=0",
            "XF:27",
            "XF:28",
        )
    ), f"{module} embeds beamline-specific identity or policy"


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from xpd_tools.gui import xpdd_config", "xpd_tools.gui.plan_widget"),
        ("from . import xpdd_config", "xpd_tools.gui"),
        ("from .. import plans", "xpd_tools.gui"),
        ("importlib.import_module('xpd_tools.gui.xpdd_config')", "xpd_tools.gui"),
    ],
)
def test_boundary_resolves_absolute_relative_and_dynamic_imports(source, package):
    imports = _imports(source, package)
    assert (
        any(
            name == "xpd_tools" or name.startswith("xpd_tools.")
            for name in imports
            if not name.startswith("xpd_tools.gui.")
        )
        or "xpd_tools.gui.xpdd_config" in imports
    )
