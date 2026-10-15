"""Tool modules share code only through public names and the support modules (PR 25)."""

import ast
from pathlib import Path

import pytest

import garmin_coach_mcp

PACKAGE = Path(garmin_coach_mcp.__file__).parent
SUPPORT = {"__init__", "__main__", "access", "auth", "config", "formatting", "garmin_client", "login", "sections", "server"}
TOOL_MODULES = {"activities", "body", "exercises", "health", "performance", "workouts"}


def private_uses(module: str, source: str) -> list[str]:
    """The private (`_`) names that the source of tool module `module` takes from another tool module."""
    tree = ast.parse(source)
    found: list[str] = []
    aliases: set[str] = set()  # names bound to another tool module (`from . import exercises`)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            origin = (node.module or "").removeprefix("garmin_coach_mcp").removeprefix(".")
            if origin in TOOL_MODULES - {module}:
                found += [f"{origin}.{a.name}" for a in node.names if a.name.startswith("_")]
            if origin == "":
                aliases |= {a.asname or a.name for a in node.names if a.name in TOOL_MODULES - {module}}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
                and node.attr.startswith("_")):
            found.append(f"{node.value.id}.{node.attr}")
    return found


def test_every_module_is_classified():
    assert {p.stem for p in PACKAGE.glob("*.py")} == SUPPORT | TOOL_MODULES


def test_no_tool_module_imports_a_private_name_of_another():
    uses = {m: private_uses(m, (PACKAGE / f"{m}.py").read_text(encoding="utf-8")) for m in sorted(TOOL_MODULES)}
    assert {m: found for m, found in uses.items() if found} == {}


@pytest.mark.parametrize(
    ("module", "source", "expected"),
    [
        ("body", "from .health import _section", ["health._section"]),
        ("body", "from garmin_coach_mcp.health import _section", ["health._section"]),
        ("body", "from garmin_coach_mcp import health\nx = health._section", ["health._section"]),
        ("body", "from . import health as h\nx = h._section", ["h._section"]),
        ("workouts", "from . import exercises\nx = exercises._tokens(1)", ["exercises._tokens"]),
        ("body", "from .health import UNAVAILABLE, shape_sleep", []),
        ("body", "from . import health\nx = health.shape_sleep", []),
        ("body", "from .sections import _none_if_missing", []),  # a support module is not a tool module
        ("body", "from .body import _own", []),  # a module's own names are its business
    ],
)
def test_the_scan_sees_each_form_of_a_private_import(module, source, expected):
    assert private_uses(module, source) == expected
