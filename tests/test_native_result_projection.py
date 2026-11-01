from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = next(
    (path for path in (ROOT.parent, *ROOT.parents) if (path / "mncs-language").is_dir()),
    ROOT.parent,
)
LANGUAGE_ROOT = WORKSPACE_ROOT / "mncs-language"
MNCS = Path(os.environ.get(
    "MNCS_BINARY",
    os.environ.get("MNCS", str(LANGUAGE_ROOT / "target/release/mncs")),
))
sys.path.insert(0, str(ROOT / "tools"))
import mncs_test  # noqa: E402
import mncs_test_native  # noqa: E402


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        (str(stdlib), str(ROOT / "native"))
    )
    return environment


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


def _test_result(
    *, verdict: str, failure_kind: str, discriminant: int,
    expected: int, actual: int, failures: int,
) -> dict[str, object]:
    fields = [
        ["verdict", _finite("Verdict", verdict, 0 if verdict == "PASS" else 1)],
        ["verdict_code", _integer(0 if verdict == "PASS" else 1)],
        ["failure_kind", _finite("FailureKind", failure_kind, discriminant)],
        ["failure_code", _integer(9000 + failures)],
        ["assertions", _integer(4 + failures)],
        ["failures", _integer(failures)],
        ["expected", _integer(expected)],
        ["actual", _integer(actual)],
        ["assertion_code", _integer(1001 + failures)],
    ]
    return {"record": {"name": "TestResult", "type_identity": "test-result", "fields": fields}}


def _suite_summary() -> dict[str, object]:
    fields = [
        ["verdict", _finite("Verdict", "FAIL", 1)],
        ["verdict_code", _integer(1)],
    ]
    fields.extend([
        [name, _integer(value)]
        for name, value in zip(
            (
                "total", "passed", "failed", "skipped", "unsupported",
                "assertion_failures", "setup_failures", "compile_failures",
                "runtime_failures", "timeout_failures", "infrastructure_failures",
                "assertion_count", "failure_count",
            ),
            range(10, 23),
            strict=True,
        )
    ])
    return {"record": {"name": "SuiteSummary", "type_identity": "suite-summary", "fields": fields}}


def test_native_projection_replaces_python_record_interpretation() -> None:
    test_values = [
        _test_result(
            verdict="PASS", failure_kind="NoFailure", discriminant=0,
            expected=-7, actual=9, failures=0,
        ),
        _test_result(
            verdict="FAIL", failure_kind="AssertionFailure", discriminant=2,
            expected=41, actual=42, failures=1,
        ),
    ]
    summary_value = _suite_summary()
    executions = [{"returned": [value]} for value in test_values]
    observed: list[tuple[str, dict[str, object]]] = []
    original = mncs_test.run_native_app

    def observe(name: str, request: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed.append((name, request))
        return original(name, request, **kwargs)

    with mock.patch.object(mncs_test, "run_native_app", side_effect=observe):
        native_tests, test_stats = mncs_test.native_result_projection_batch(
            executions,
            kind=1,
            mncs=str(MNCS),
            cwd=ROOT,
            environment=_environment(),
        )
        native_summaries, summary_stats = mncs_test.native_result_projection_batch(
            [{"returned": [summary_value]}],
            kind=2,
            mncs=str(MNCS),
            cwd=ROOT,
            environment=_environment(),
        )

    assert native_tests == [
        mncs_test._compatibility_native_test_result(executions[0]),
        mncs_test._compatibility_native_test_result(executions[1]),
    ]
    assert native_summaries == [
        mncs_test._compatibility_native_suite_summary({"returned": [summary_value]})
    ]
    assert test_stats == {"calls": 1, "candidates": 2, "projected": 2, "unsupported": 0}
    assert summary_stats == {"calls": 1, "candidates": 1, "projected": 1, "unsupported": 0}
    assert len(observed) == 2
    assert all(name == "test-native-result-projection.json" for name, _ in observed)
    for _name, request in observed:
        assert request["schema_version"] == "mncs.test.native-result-projection-request/1"
        assert all(
            isinstance(chunk["bytes"], str)
            for item in request["items"]
            for chunk in item["document"]["chunks"]
        )


def test_native_projection_declines_invalid_finite_identity_for_compatibility() -> None:
    value = _test_result(
        verdict="PASS", failure_kind="NoFailure", discriminant=0,
        expected=0, actual=0, failures=0,
    )
    fields = value["record"]["fields"]
    fields[0][1]["finite"]["type_identity"] = "foreign::Outcome"
    native, stats = mncs_test.native_result_projection_batch(
        [{"returned": [value]}],
        kind=1,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )
    assert native == [None]
    assert stats == {"calls": 1, "candidates": 1, "projected": 0, "unsupported": 1}


def test_native_projection_builds_execution_cases_in_mncs(tmp_path: Path) -> None:
    values = [
        _test_result(
            verdict="PASS", failure_kind="NoFailure", discriminant=0,
            expected=-7, actual=9, failures=0,
        ),
        _test_result(
            verdict="FAIL", failure_kind="AssertionFailure", discriminant=2,
            expected=41, actual=42, failures=1,
        ),
    ]
    executions = [
        {"status": "returned", "returned": [value]}
        for value in values
    ]
    tests = [
        {"id": "pass-case", "kind": "unit"},
        {"id": "assertion-case", "kind": "unit"},
    ]
    observed: list[str] = []
    original = mncs_test_native.run_native_app

    def observe(name: str, request: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed.append(name)
        return original(name, request, **kwargs)

    with mock.patch.object(mncs_test_native, "run_native_app", side_effect=observe):
        rows, stats = mncs_test.native_execution_result_adjudication_batch(
            executions,
            tests,
            suite=False,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_environment(),
        )

    assert observed == ["test-execution-result-adjudication.json"]
    assert [row["native_projection"] for row in rows if row is not None] == [
        mncs_test._compatibility_native_test_result(executions[0]),
        mncs_test._compatibility_native_test_result(executions[1]),
    ]
    assert [row["process"]["execution"]["decision"] for row in rows if row is not None] == [
        "success",
        "assertion_failure",
    ]
    assert stats == {
        "calls": 1,
        "case_count": 2,
        "projection_calls": 1,
        "projection_candidates": 2,
        "projection_native": 2,
        "projection_unsupported": 0,
    }

