"""The static checks of the test stage (ruff and mypy, configured in pyproject.toml) reject what they are there
for. Each tool runs once on canned files in a temporary directory, with this project's configuration."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# One file per selected rule family: (file name, source, the code ruff must report for it). A source of None
# is a file that is not written: a path ruff cannot read (E902), as a misspelled path in a Dockerfile would be.
LINT_CASES = [
    ("unused_import.py", "import os\n", "F401"),
    ("undefined_name.py", "print(missing)\n", "F821"),
    ("two_imports.py", "import os, sys\n\nprint(os, sys)\n", "E401"),
    ("none_comparison.py", "def f(x: object) -> bool:\n    return x == None\n", "E711"),
    ("mutable_default.py", "def f(x: list[int] = []) -> list[int]:\n    return x\n", "B006"),
    ("unused_noqa.py", "import os  # noqa: F401\n\nprint(os)\n", "RUF100"),
    ("missing.py", None, "E902"),
]
# (file name, source, the mypy error code; the second one needs strict mode).
TYPE_CASES = [
    ("wrong_type.py", 'x: int = "a"\n', "assignment"),
    ("untyped_def.py", "def f(x):\n    return x\n", "no-untyped-def"),
]
CLEAN = ("clean.py", "def double(x: int) -> int:\n    return 2 * x\n")


def _run(tool_args: list[str], files: list[tuple[str, str | None]], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    for name, source in files:
        if source is not None:
            (tmp_path / name).write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", *tool_args, *(str(tmp_path / name) for name, _ in files)],
        capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.fixture(scope="module")
def ruff_findings(tmp_path_factory):
    files = [(name, source) for name, source, _ in LINT_CASES] + [CLEAN]
    args = ["ruff", "check", "--no-cache", "--config", str(PYPROJECT), "--output-format", "concise"]
    done = _run(args, files, tmp_path_factory.mktemp("lint"))
    assert done.returncode == 1, done.stdout + done.stderr  # 1 = findings; 2 would be a broken configuration
    return {(Path(m[1]).name, m[2]) for m in re.finditer(r"^(\S+?):\d+:\d+: (\S+) ", done.stdout, re.M)}


@pytest.mark.parametrize(("name", "code"), [(name, code) for name, _, code in LINT_CASES])
def test_ruff_reports_the_lint_error(ruff_findings, name, code):
    assert (name, code) in ruff_findings


def test_ruff_reports_only_the_intended_errors(ruff_findings):
    assert ruff_findings == {(name, code) for name, _, code in LINT_CASES}  # nothing for clean.py, no extra rule


@pytest.fixture(scope="module")
def mypy_findings(tmp_path_factory):
    files = [(name, source) for name, source, _ in TYPE_CASES] + [CLEAN]
    args = ["mypy", "--config-file", str(PYPROJECT), "--cache-dir", "/dev/null", "--no-error-summary"]
    done = _run(args, files, tmp_path_factory.mktemp("types"))
    assert done.returncode == 1, done.stdout + done.stderr  # 1 = type errors; 2 would be a broken configuration
    return {(Path(m[1]).name, m[2]) for m in re.finditer(r"^(\S+?):\d+: error: .*\[([a-z-]+)\]$", done.stdout, re.M)}


@pytest.mark.parametrize(("name", "code"), [(name, code) for name, _, code in TYPE_CASES])
def test_mypy_reports_the_type_error(mypy_findings, name, code):
    assert (name, code) in mypy_findings


def test_mypy_reports_only_the_intended_errors(mypy_findings):
    assert mypy_findings == {(name, code) for name, _, code in TYPE_CASES}  # nothing for clean.py
