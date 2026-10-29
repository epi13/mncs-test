from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = next(
    (path for path in (ROOT.parent, *ROOT.parents) if (path / "mncs-language").is_dir()),
    ROOT.parent,
)
LANGUAGE_ROOT = WORKSPACE_ROOT / "mncs-language"
_default_mncs = LANGUAGE_ROOT / "target/release/mncs"
if not _default_mncs.is_file():
    _default_mncs = LANGUAGE_ROOT / "target/debug/mncs"
MNCS = Path(os.environ.get("MNCS_BINARY", os.environ.get("MNCS", str(_default_mncs))))

sys.path.insert(0, str(ROOT / "tools"))
import mncs_test  # noqa: E402


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib), str(ROOT / "native")))
    return environment


pytestmark = pytest.mark.skipif(
    not MNCS.is_file() or not os.access(MNCS, os.X_OK),
    reason="a built sibling MNCS executable is required",
)


def test_native_result_summary_matches_independent_result_contract(tmp_path: Path) -> None:
    rows = [
        {
            "verdict": "PASS",
            "status": "passed",
            "native_result": {"assertions": 2, "failures": 0},
        },
        {
            "verdict": "FAIL",
            "status": "failed",
            "failure": {"class": "assertion"},
            "native_result": {"assertions": 3, "failures": 1},
        },
        {
            "verdict": "PASS",
            "status": "skipped",
            "native_result": {"assertions": 0, "failures": 0},
        },
        {
            "verdict": "UNKNOWN",
            "status": "unsupported",
            "failure": {"class": "unsupported"},
        },
        {
            "verdict": "PASS",
            "status": "expected_failure",
            "failure": {"class": "runtime_failure"},
        },
        {
            "verdict": "FAIL",
            "status": "failed",
            "failure": {"class": "diagnostic_mismatch"},
        },
        {
            "verdict": "FAIL",
            "status": "failed",
            "failure": {"class": "malformed_test_declaration"},
        },
        {
            "verdict": "FAIL",
            "status": "failed",
            "failure": {"class": "unmapped_failure"},
        },
    ]

    summary = mncs_test.native_result_summary(
        rows,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert summary == {
        "total": 8,
        "passed": 2,
        "failed": 4,
        "skipped": 1,
        "unsupported": 1,
        "assertion_failures": 1,
        "setup_failures": 0,
        "compile_failures": 1,
        "runtime_failures": 1,
        "timeout_failures": 0,
        "infrastructure_failures": 1,
        "assertion_count": 5,
        "failure_count": 1,
    }
    assert mncs_test.native_result_summary(
        [],
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    ) == {field: 0 for field in summary}
