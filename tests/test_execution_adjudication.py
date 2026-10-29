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
from mncs_test_native import (  # noqa: E402
    EXECUTION_ADJUDICATION_REQUEST_SCHEMA,
    EXECUTION_ADJUDICATION_RESULT_SCHEMA,
    NativeTransportError,
    main as native_application_main,
    run_execution_adjudication,
    run_native_app,
)


pytestmark = pytest.mark.skipif(
    not MNCS.is_file() or not os.access(MNCS, os.X_OK),
    reason="a built sibling MNCS executable is required",
)


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib), str(ROOT / "native")))
    return environment


def _case(
    status: str,
    *,
    expected: str = "",
    suite: bool = False,
    verdict: str = "",
    failure: str = "",
) -> dict[str, object]:
    return {
        "execution_status": status,
        "expected_status": expected,
        "suite": suite,
        "native_result_present": bool(verdict),
        "native_verdict": verdict,
        "native_failure_class": failure,
    }


def test_native_adjudicator_matches_independent_execution_contract(tmp_path: Path) -> None:
    cases = [
        _case("returned", verdict="PASS", failure="none"),
        _case("returned", verdict="SKIP", failure="none"),
        _case("returned", verdict="UNSUPPORTED", failure="unsupported"),
        _case("returned", verdict="FAIL", failure="assertion"),
        _case("returned", expected="runtime_failure", verdict="PASS", failure="none"),
        _case("runtime_failure", expected="runtime_failure"),
        _case("runtime_failure"),
        _case("unsupported", expected="runtime_failure"),
        _case("budget_exhausted"),
        _case("invalid_request", expected="invalid_request"),
        _case("invalid_request"),
        _case("new-runtime-state"),
        _case("returned"),
        _case("returned", suite=True, verdict="PASS"),
        _case("returned", suite=True, verdict="FAIL"),
        _case("returned", suite=True, verdict="UNSUPPORTED"),
        _case("returned", expected="returned", verdict="PASS", failure="none"),
        _case("returned", expected="runtime_failure", verdict="FAIL", failure="assertion"),
        _case("returned", expected="runtime_failure", verdict="SKIP", failure="none"),
        _case("returned", expected="runtime_failure", verdict="UNSUPPORTED", failure="unsupported"),
    ]
    expected = [
        {"decision": "success", "status": "passed", "verdict": "PASS", "failure_class": "", "expected_failure": False},
        {"decision": "skip", "status": "skipped", "verdict": "PASS", "failure_class": "", "expected_failure": False},
        {"decision": "unsupported_returned", "status": "unsupported", "verdict": "UNKNOWN", "failure_class": "unsupported", "expected_failure": False},
        {"decision": "assertion_failure", "status": "failed", "verdict": "FAIL", "failure_class": "assertion", "expected_failure": False},
        {"decision": "expectation_mismatch", "status": "failed", "verdict": "FAIL", "failure_class": "expectation_mismatch", "expected_failure": False},
        {"decision": "expected_failure", "status": "expected_failure", "verdict": "PASS", "failure_class": "runtime_failure", "expected_failure": True},
        {"decision": "runtime_failure", "status": "failed", "verdict": "FAIL", "failure_class": "runtime_failure", "expected_failure": False},
        {"decision": "unsupported_execution", "status": "unsupported", "verdict": "UNKNOWN", "failure_class": "unsupported", "expected_failure": False},
        {"decision": "timeout", "status": "failed", "verdict": "FAIL", "failure_class": "timeout", "expected_failure": False},
        {"decision": "expected_failure", "status": "expected_failure", "verdict": "PASS", "failure_class": "invalid_request", "expected_failure": True},
        {"decision": "invalid_request", "status": "failed", "verdict": "FAIL", "failure_class": "malformed_test_declaration", "expected_failure": False},
        {"decision": "unknown_status", "status": "failed", "verdict": "FAIL", "failure_class": "infrastructure_failure", "expected_failure": False},
        {"decision": "malformed_result", "status": "failed", "verdict": "FAIL", "failure_class": "malformed_test_result", "expected_failure": False},
        {"decision": "suite_result", "status": "passed", "verdict": "PASS", "failure_class": "", "expected_failure": False},
        {"decision": "suite_result", "status": "returned", "verdict": "FAIL", "failure_class": "", "expected_failure": False},
        {"decision": "suite_result", "status": "returned", "verdict": "UNKNOWN", "failure_class": "", "expected_failure": False},
        {"decision": "success", "status": "passed", "verdict": "PASS", "failure_class": "", "expected_failure": False},
        {"decision": "assertion_failure", "status": "failed", "verdict": "FAIL", "failure_class": "assertion", "expected_failure": False},
        {"decision": "expectation_mismatch", "status": "failed", "verdict": "FAIL", "failure_class": "expectation_mismatch", "expected_failure": False},
        {"decision": "expectation_mismatch", "status": "failed", "verdict": "FAIL", "failure_class": "expectation_mismatch", "expected_failure": False},
    ]

    outcomes = run_execution_adjudication(
        cases,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert outcomes == expected


def test_empty_batch_is_a_real_native_result(tmp_path: Path) -> None:
    assert run_execution_adjudication(
        [],
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    ) == []


def test_adjudicator_rejects_inconsistent_case_count(tmp_path: Path) -> None:
    with pytest.raises(NativeTransportError, match="failed"):
        run_native_app(
            "test-execution-adjudication.json",
            {
                "schema_version": EXECUTION_ADJUDICATION_REQUEST_SCHEMA,
                "case_count": 1,
                "cases": [],
            },
            request_schema=EXECUTION_ADJUDICATION_REQUEST_SCHEMA,
            result_schema=EXECUTION_ADJUDICATION_RESULT_SCHEMA,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )


def test_adapter_bounds_batches_before_invoking_the_toolchain(tmp_path: Path) -> None:
    with pytest.raises(NativeTransportError, match="at most 256"):
        run_execution_adjudication(
            [{}] * 257,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )


@pytest.mark.parametrize("field", ["request", "result"])
def test_native_application_cli_rejects_symbolic_link_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    outside = tmp_path.parent / f"{field}-outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    link = tmp_path / f"{field}.json"
    link.symlink_to(outside)
    request = tmp_path / "safe-request.json"
    request.write_text("{}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = native_application_main([
        "--descriptor", "obligation-selection.json",
        "--request", link.name if field == "request" else request.name,
        "--result", link.name if field == "result" else "safe-result.json",
        "--request-schema", "mncs.test-obligation-selection-request/1",
        "--result-schema", "mncs.test-obligation-selection/1",
    ])

    assert result == 3
    assert "symbolic link" in capsys.readouterr().err
