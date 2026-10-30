from __future__ import annotations

import json
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
from mncs_test_native import (  # noqa: E402
    NativeTransportError,
    run_process_transport_adjudication,
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
    *,
    process_start_failed: bool = False,
    timed_out: bool = False,
    structured_result_present: bool = False,
    nonzero_exit: bool = False,
    diagnostics_present: bool = False,
) -> dict[str, bool]:
    return {
        "process_start_failed": process_start_failed,
        "timed_out": timed_out,
        "structured_result_present": structured_result_present,
        "nonzero_exit": nonzero_exit,
        "diagnostics_present": diagnostics_present,
    }


def _execution_case(status: str = "runtime_failure") -> dict[str, object]:
    return {
        "execution_status": status,
        "expected_status": "",
        "suite": False,
        "native_result_present": False,
        "native_verdict": "",
        "native_failure_class": "",
    }


def _previous_host_contract(case: dict[str, bool]) -> dict[str, object]:
    if case["process_start_failed"]:
        return {
            "decision": "start_failure",
            "failure_class": "infrastructure_failure",
            "terminal": True,
        }
    if case["timed_out"]:
        return {"decision": "timeout", "failure_class": "timeout", "terminal": True}
    if case["structured_result_present"]:
        return {"decision": "continue", "failure_class": "", "terminal": False}
    if case["nonzero_exit"] and case["diagnostics_present"]:
        return {
            "decision": "compile_failure",
            "failure_class": "compile_failure",
            "terminal": True,
        }
    return {
        "decision": "malformed_result",
        "failure_class": "infrastructure_failure",
        "terminal": True,
    }


def test_native_transport_policy_matches_prior_execution_contract(tmp_path: Path) -> None:
    cases = [
        _case(process_start_failed=True),
        _case(process_start_failed=True, timed_out=True),
        _case(timed_out=True, structured_result_present=True),
        _case(nonzero_exit=True, diagnostics_present=True),
        _case(nonzero_exit=True),
        _case(diagnostics_present=True),
        _case(structured_result_present=True),
    ]

    for case in cases:
        result = run_process_transport_adjudication(
            case,
            _execution_case(),
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )
        assert {
            "decision": result["decision"],
            "failure_class": result["failure_class"],
            "terminal": result["terminal"],
        } == _previous_host_contract(case)
        if result["decision"] == "continue":
            assert result["execution"] == {
                "decision": "runtime_failure",
                "status": "failed",
                "verdict": "FAIL",
                "failure_class": "runtime_failure",
                "expected_failure": False,
            }
        else:
            assert result["execution"] is None


def test_native_transport_policy_rejects_non_boolean_observations(tmp_path: Path) -> None:
    with pytest.raises(NativeTransportError, match="must be booleans"):
        run_process_transport_adjudication(
            _case() | {"timed_out": 1},
            _execution_case(),
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )


def test_native_transport_adapter_rejects_invalid_execution_case(tmp_path: Path) -> None:
    with pytest.raises(NativeTransportError, match="invalid field set"):
        run_process_transport_adjudication(
            _case(),
            {},
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )


@pytest.mark.parametrize(
    ("process", "expected_class", "expected_message"),
    [
        (
            {
                "transport_error": "missing compiler",
                "timed_out": False,
                "returncode": None,
                "stdout": b"",
                "stderr": b"missing compiler",
                "stdout_artifact": "stdout/case.out",
                "stderr_artifact": "stderr/case.err",
                "command_artifact": "commands/case.json",
            },
            "infrastructure_failure",
            "MNCS process could not be started",
        ),
        (
            {
                "timed_out": False,
                "returncode": 2,
                "stdout": json.dumps([{"code": "MNP061"}]).encode(),
                "stderr": b"",
                "stdout_artifact": "stdout/case.out",
                "stderr_artifact": "stderr/case.err",
                "command_artifact": "commands/case.json",
            },
            "compile_failure",
            "MNCS rejected the source before execution",
        ),
        (
            {
                "timed_out": False,
                "returncode": 2,
                "stdout": json.dumps({"diagnostics": [{"code": "MNP061"}]}).encode(),
                "stderr": b"",
                "stdout_artifact": "stdout/case.out",
                "stderr_artifact": "stderr/case.err",
                "command_artifact": "commands/case.json",
            },
            "infrastructure_failure",
            "MNCS emitted an unknown execution status",
        ),
    ],
)
def test_execute_entry_uses_native_transport_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: dict[str, object],
    expected_class: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(mncs_test, "run_process", lambda *args, **kwargs: process)
    result = mncs_test.execute_entry(
        test={"id": "transport-case", "entry": "exercise", "kind": "unit"},
        source_path=tmp_path / "case.mncs",
        source_text="",
        module="mncs.test.transport_case",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        default_step_budget=4096,
        default_timeout=30,
        artifacts=mncs_test.ArtifactStore(tmp_path / "artifacts"),
        artifact_key="transport-case",
    )

    assert result["verdict"] == "FAIL"
    assert result["status"] == "failed"
    assert result["failure"]["class"] == expected_class
    assert result["failure"]["message"] == expected_message


def test_execute_entry_adjudicates_transport_and_execution_in_one_native_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = {
        "timed_out": False,
        "returncode": 0,
        "stdout": json.dumps({"status": "runtime_failure"}).encode(),
        "stderr": b"",
        "stdout_artifact": "stdout/case.out",
        "stderr_artifact": "stderr/case.err",
        "command_artifact": "commands/case.json",
    }
    monkeypatch.setattr(mncs_test, "run_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        mncs_test,
        "run_execution_adjudication",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("execute_entry should use its combined native adjudicator")
        ),
    )

    result = mncs_test.execute_entry(
        test={"id": "transport-runtime", "entry": "exercise", "kind": "unit"},
        source_path=tmp_path / "case.mncs",
        source_text="",
        module="mncs.test.transport_case",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        default_step_budget=4096,
        default_timeout=30,
        artifacts=mncs_test.ArtifactStore(tmp_path / "artifacts"),
        artifact_key="transport-runtime",
    )

    assert result["failure"]["class"] == "runtime_failure"
    assert result["execution_status"] == "runtime_failure"


@pytest.mark.parametrize(
    ("process", "expected_class", "expected_message"),
    [
        (
            {
                "transport_error": "missing MNCS executable",
                "timed_out": False,
                "returncode": None,
                "stdout": b"",
                "stderr": b"missing MNCS executable",
                "stdout_artifact": "stdout/case.out",
                "stderr_artifact": "stderr/case.err",
                "command_artifact": "commands/case.json",
            },
            "infrastructure_failure",
            "MNCS process could not be started",
        ),
        (
            {
                "timed_out": True,
                "returncode": -15,
                "stdout": b"",
                "stderr": b"",
                "stdout_artifact": "stdout/case.out",
                "stderr_artifact": "stderr/case.err",
                "command_artifact": "commands/case.json",
            },
            "timeout",
            "MNCS execution exceeded 30s",
        ),
    ],
)
def test_execute_entry_preserves_platform_failure_if_native_classifier_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: dict[str, object],
    expected_class: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(mncs_test, "run_process", lambda *args, **kwargs: process)
    missing_mncs = tmp_path / "missing-mncs"
    result = mncs_test.execute_entry(
        test={"id": "unavailable-classifier", "entry": "exercise", "kind": "unit"},
        source_path=tmp_path / "case.mncs",
        source_text="",
        module="mncs.test.transport_case",
        mncs=str(missing_mncs),
        cwd=tmp_path,
        environment=_environment(),
        default_step_budget=4096,
        default_timeout=30,
        artifacts=mncs_test.ArtifactStore(tmp_path / "artifacts"),
        artifact_key="unavailable-classifier",
    )

    assert result["verdict"] == "FAIL"
    assert result["status"] == "failed"
    assert result["failure"]["class"] == expected_class
    assert result["failure"]["message"] == expected_message
