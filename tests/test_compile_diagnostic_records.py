from __future__ import annotations

import json
import os
import subprocess
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

import sys

sys.path.insert(0, str(ROOT / "tools"))
import mncs_test  # noqa: E402


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


def _document_result(document: bytes) -> dict:
    return mncs_test.native_compile_diagnostic_document_result(
        document,
        ["MNE"],
        compiler_exit_code=2,
        expects_failure=True,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )


def test_native_typed_records_match_independent_json_values() -> None:
    diagnostics = [
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "escaped \u03bb and 東京",
            "span": {"start": 1, "end": 9, "line": 2, "column": 4},
            "expected": [],
            "found": None,
        },
        {
            "code": "MNE042",
            "stage": "elaboration",
            "severity": "error",
            "message": "found string",
            "span": {"start": 9, "end": 12, "line": 3, "column": 1},
            "expected": [],
            "found": "value",
        },
        {
            "code": "MNE043",
            "stage": "elaboration",
            "severity": "error",
            "message": "found boolean",
            "span": {"start": 12, "end": 17, "line": 4, "column": 2},
            "expected": [],
            "found": False,
        },
        {
            "code": "MNE044",
            "stage": "elaboration",
            "severity": "error",
            "message": "found integer",
            "span": {"start": 17, "end": 23, "line": 5, "column": 3},
            "expected": [],
            "found": -7,
        },
    ]
    document = json.dumps(
        {"valid": False, "errors": diagnostics, "diagnostics": [{"wrong": True}]},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    native = _document_result(document)

    assert native["supported"] is True
    assert native["projection_supported"] is True
    assert native["root_is_object"] is True
    assert native["valid_field_present"] is True
    assert native["valid_field_is_true"] is False
    assert native["verdict"] == "expected_failure"
    assert mncs_test.native_compile_diagnostic_records(native) == diagnostics


@pytest.mark.parametrize(
    "diagnostic",
    [
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "needs expected values",
            "span": {"start": 1, "end": 2, "line": 1, "column": 1},
            "expected": ["type", "expression"],
            "found": None,
        },
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "has an extension field",
            "span": {"start": 1, "end": 2, "line": 1, "column": 1},
            "expected": [],
            "found": None,
            "related": [],
        },
    ],
)
def test_native_projection_declines_shapes_that_need_compatibility_fallback(
    diagnostic: dict,
) -> None:
    document = json.dumps([diagnostic], separators=(",", ":")).encode("utf-8")

    native = _document_result(document)

    assert native["supported"] is True
    assert native["projection_supported"] is False
    assert native["verdict"] == "expected_failure"


def test_compile_entry_uses_native_records_without_parsing_raw_compiler_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ROOT / "tests/fixtures/compile_fail.mncs"
    environment = _environment()
    reference = subprocess.run(
        [str(MNCS), "validate", str(source)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    expected = json.loads(reference.stdout)
    assert isinstance(expected, list)

    def reject_host_tree(_stdout: bytes):
        raise AssertionError("raw compiler stdout reached the Python JSON tree parser")

    monkeypatch.setattr(mncs_test, "parse_json_output", reject_host_tree)
    test = {
        "id": "native-projection-compile-fail",
        "entry": "invalid_return",
        "kind": "compile-fail",
        "diagnostic_codes": ["MNE"],
        "timeout_seconds": 30,
    }
    with mncs_test.ArtifactStore(
        tmp_path / "artifacts",
        mncs=str(MNCS),
        cwd=ROOT,
        environment=environment,
    ) as artifacts:
        result = mncs_test.compile_entry(
            test=test,
            source_path=source,
            source_text=source.read_text(encoding="utf-8"),
            mncs=str(MNCS),
            cwd=ROOT,
            environment=environment,
            default_timeout=30,
            artifacts=artifacts,
            artifact_key="native-diagnostic-projection",
        )

    assert result["status"] == "expected_failure"
    assert result["compile_verdict"] == "expected_failure"
    assert result["diagnostics"] == expected


def test_compile_entry_fallback_preserves_unsupported_diagnostic_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = {
        "code": "MNE045",
        "stage": "elaboration",
        "severity": "error",
        "message": "extended diagnostic",
        "span": {"start": 1, "end": 4, "line": 2, "column": 1},
        "expected": ["integer", "float"],
        "found": {"kind": "record", "value": 3.5},
        "related": [{"message": "see declaration", "span": {"start": 8}}],
    }
    document = json.dumps([diagnostic], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    process = {
        "stdout": document,
        "stderr": b"",
        "returncode": 2,
        "timed_out": False,
        "transport_error": None,
        "stdout_artifact": {"name": "stdout"},
        "stderr_artifact": {"name": "stderr"},
        "command_artifact": {"name": "command"},
    }
    declined = {
        "supported": True,
        "projection_supported": False,
        "verdict": "expected_failure",
    }
    monkeypatch.setattr(mncs_test, "run_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        mncs_test,
        "native_compile_diagnostic_document_result",
        lambda *args, **kwargs: declined,
    )

    result = mncs_test.compile_entry(
        test={
            "id": "native-projection-compatibility",
            "entry": "invalid_return",
            "kind": "compile-fail",
            "diagnostic_codes": ["MNE"],
        },
        source_path=ROOT / "tests/fixtures/compile_fail.mncs",
        source_text="",
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
        default_timeout=30,
        artifacts=object(),
        artifact_key="native-diagnostic-compatibility",
    )

    assert result["status"] == "expected_failure"
    assert result["compile_verdict"] == "expected_failure"
    assert result["diagnostics"] == json.loads(document)
