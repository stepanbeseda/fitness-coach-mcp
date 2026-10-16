"""The shared value guards (PR 24): one finite-number rule for the whole package."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from coach_mcp import values
from coach_mcp.values import finite, finite_float, valid_id, whole

PACKAGE = Path(values.__file__).parent


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (-3, -3),
        (2.5, 2.5),
        (1e308, 1e308),
        (True, None),
        (False, None),
        (math.inf, None),
        (-math.inf, None),
        (math.nan, None),
        ("5", None),
        (None, None),
        ([1], None),
    ],
)
def test_finite(value, expected):
    assert finite(value) == expected
    if expected is not None:
        assert type(finite(value)) is type(value)  # an int stays an int


def test_finite_float_turns_an_int_into_a_float():
    assert finite_float(100) == 100.0 and isinstance(finite_float(100), float)
    assert finite_float(math.inf) is None and finite_float(True) is None


@pytest.mark.parametrize(("value", "expected"), [(3, 3), (3.0, 3), (3.5, None), (True, None), ("3", None), (None, None)])
def test_whole(value, expected):
    assert whole(value) == expected


def test_valid_id_bounds():
    assert valid_id("a") and valid_id("x" * 64)
    assert not valid_id("") and not valid_id("x" * 65) and not valid_id(5) and not valid_id(None)


CHECKS = {"isfinite", "isnan", "isinf"}


def _isfinite_calls() -> list[str]:
    """The modules that use a ``math`` finiteness check: ``math.isfinite``, a bare name, or ``from math import``."""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = {a.name for a in node.names} if isinstance(node, ast.ImportFrom) else set()
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            if names & CHECKS:
                found.append(path.name)
    return found


def test_values_is_the_only_module_that_checks_for_finite_numbers():
    assert _isfinite_calls() == ["values.py"]
