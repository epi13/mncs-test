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
import mncs_test_native  # noqa: E402
from mncs_test_native import (  # noqa: E402
    NativeTransportError,
    run_execution_batch_envelope_adjudication,
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


def _integer(value: int) -> dict[str, object]:
    return {"integer": {"value": value, "type": {"bits": 64, "signed": value < 0}}}


def _finite(type_name: str, variant: str, discriminant: int) -> dict[str, object]:
    return {
        "finite": {
            "type_identity": f"mncs:0.2:finite-type:mncs.test.assertions::{type_name}",
            "variant_identity": (
                f"mncs:0.2:finite-variant:mncs.test.assertions::{type_name}::{variant}"
            ),
            "discriminant": discriminant,
        }
    }


def _returned_pass_result() -> dict[str, object]:
    fields = [
        ["verdict", _finite("Verdict", "PASS", 0)],
        ["verdict_code", _integer(0)],
        ["failure_kind", _finite("FailureKind", "NoFailure", 0)],
        ["failure_code", _integer(0)],
        ["assertions", _integer(1)],
        ["failures", _integer(0)],
        ["expected", _integer(0)],
        ["actual", _integer(0)],
        ["assertion_code", _integer(0)],
    ]
    return {"record": {"name": "TestResult", "type_identity": "test-result", "fields": fields}}


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


def test_native_batch_envelope_projects_results_and_preserves_returned_json(
    tmp_path: Path,
) -> None:
    returned = _returned_pass_result()
    returned["record"]["fields"].append(["detail", {"text": "λ" * 5000}])
    call_output = {
        "schema_version": "mncs.execution/1",
        "target": "canonical-vm",
        "program_identity": "program:batch-case",
        "program_fingerprint": "fingerprint:batch-case",
        "function_identity": "function:exercise",
        "artifact_identity": "artifact:batch-case",
        "artifact_sha256": "a" * 64,
        "backend": {"identity": "backend:canonical-vm"},
        "failure": None,
        "failure_reason": "",
        "invoked_callable": {"callable_identity": "function:exercise"},
        "reused_session": True,
        "steps": 17,
        "effects": [],
        "vm_record": {"identity": "vm-record:batch-case"},
        "execution_provenance": {"identity": "provenance:batch-case"},
        "status": "returned",
        "returned": [returned],
    }
    document = json.dumps([call_output], ensure_ascii=False, separators=(",", ":")).encode()

    rows = run_execution_batch_envelope_adjudication(
        document,
        [{"expected_status": "", "suite": False, "kind": 1}],
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert rows is not None and len(rows) == 1
    row = rows[0]
    assert row["status"] == "returned"
    assert row["native_result"]["verdict"] == "PASS"
    assert row["projection_attempted"] is True
    assert row["process"]["decision"] == "continue"
    assert row["execution"] == {
        key: value for key, value in call_output.items()
        if key in {
            "schema_version", "target", "program_identity", "program_fingerprint",
            "function_identity", "artifact_identity", "artifact_sha256", "backend",
            "failure", "failure_reason", "invoked_callable", "reused_session", "steps",
            "effects", "vm_record", "execution_provenance",
        }
    }
    assert json.loads(row["returned_json"]) == [returned]
    assert len(row["returned_json"]) > 10_000


def test_raw_result_json_fragment_splice_preserves_nested_value() -> None:
    marker = mncs_test._native_json_fragment_marker(b"opaque", 0)
    fragment = b'{"record":{"fields":[["payload",[1,true,null]]],"name":"Nested"}}'
    payload = mncs_test._compact_json_with_raw_fragments(
        {"execution": {"returned": marker}, "status": "passed"},
        [(marker, fragment)],
    )

    assert b'"returned":' + fragment in payload
    assert json.loads(payload) == {
        "execution": {"returned": json.loads(fragment)},
        "status": "passed",
    }


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
    observed: list[str] = []
    original = mncs_test_native.run_native_app

    def observe(name: str, request: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed.append(name)
        return original(name, request, **kwargs)

    monkeypatch.setattr(mncs_test_native, "run_native_app", observe)
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
    assert observed == ["test-process-transport-adjudication.json"]


def test_execute_entry_projects_and_adjudicates_returned_result_in_mncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = {
        "timed_out": False,
        "returncode": 0,
        "stdout": json.dumps({
            "schema_version": "mncs.execution/1",
            "target": "canonical-vm",
            "program_identity": "program:returned-case",
            "program_fingerprint": "fingerprint:returned-case",
            "function_identity": "function:exercise",
            "steps": 17,
            "effects": [],
            "status": "returned",
            "returned": [_returned_pass_result()],
        }).encode(),
        "stderr": b"",
        "stdout_artifact": "stdout/case.out",
        "stderr_artifact": "stderr/case.err",
        "command_artifact": "commands/case.json",
    }
    monkeypatch.setattr(mncs_test, "run_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        mncs_test,
        "parse_json_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native stdout projection should bypass the host JSON tree")
        ),
    )
    observed: list[tuple[str, dict[str, object]]] = []
    original = mncs_test_native.run_native_app

    def observe(name: str, request: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed.append((name, request))
        return original(name, request, **kwargs)

    monkeypatch.setattr(mncs_test_native, "run_native_app", observe)

    result = mncs_test.execute_entry(
        test={"id": "returned-pass", "entry": "exercise", "kind": "unit"},
        source_path=tmp_path / "case.mncs",
        source_text="",
        module="mncs.test.transport_case",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        default_step_budget=4096,
        default_timeout=30,
        artifacts=mncs_test.ArtifactStore(tmp_path / "artifacts"),
        artifact_key="returned-pass",
    )

    assert result["verdict"] == "PASS"
    assert result["native_result"]["assertions"] == 1
    assert result["execution"] == {
        "schema_version": "mncs.execution/1",
        "target": "canonical-vm",
        "program_identity": "program:returned-case",
        "program_fingerprint": "fingerprint:returned-case",
        "function_identity": "function:exercise",
        "steps": 17,
        "effects": [],
    }
    assert [name for name, _request in observed] == [
        "test-execution-envelope-adjudication.json"
    ]
    native_document = observed[0][1]["items"][0]["document"]
    transported_stdout = "".join(chunk["bytes"] for chunk in native_document["chunks"])
    assert transported_stdout.encode("utf-8") == process["stdout"]


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
