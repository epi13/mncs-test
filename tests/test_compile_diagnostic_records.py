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


def test_native_diagnostic_span_matches_independent_json_values() -> None:
    diagnostics = [
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "escaped \u03bb and 東京",
            "span": {"start": 1, "end": 9, "line": 2, "column": 4},
            "expected": [
                "type", "expression", "lambda", "τ", "東京", "identifier", "pattern", "module"
            ],
            "found": None,
        },
        {
            "code": "MNE042",
            "stage": "elaboration",
            "severity": "error",
            "message": "found string",
            "span": {"start": 9, "end": 12, "line": 3, "column": 1},
            "expected": ["identifier"],
            "found": "value",
            "related": [
                {
                    "code": "MNE043",
                    "stage": "elaboration",
                    "severity": "error",
                    "message": "import boundary",
                    "span": {"start": 23, "end": 25, "line": 6, "column": 2},
                    "expected": ["module"],
                    "found": "leaf",
                    "related": [],
                }
            ],
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
        {"valid": False, "diagnostics": [{"wrong": True}], "errors": diagnostics},
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
    assert native["diagnostics_json_present"] is True
    start = native["diagnostics_json_start"]
    length = native["diagnostics_json_length"]
    assert document[start:start + length].startswith(b"[")
    assert document[start:start + length].endswith(b"]")
    assert mncs_test.native_compile_diagnostic_records(native, document) == diagnostics


def test_native_projection_accepts_empty_main_related_array() -> None:
    diagnostic = {
        "code": "MNE041",
        "stage": "elaboration",
        "severity": "error",
        "message": "empty related list",
        "span": {"start": 1, "end": 2, "line": 1, "column": 1},
        "expected": [],
        "found": None,
        "related": [],
    }
    document = json.dumps([diagnostic], separators=(",", ":")).encode("utf-8")

    native = _document_result(document)

    assert native is not None
    assert native["projection_supported"] is True
    assert mncs_test.native_compile_diagnostic_records(native, document) == [diagnostic]


def test_native_projection_without_a_diagnostic_array_returns_empty_list() -> None:
    document = b'{"valid":true}'

    native = mncs_test.native_compile_diagnostic_document_result(
        document,
        [],
        compiler_exit_code=0,
        expects_failure=False,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )

    assert native is not None
    assert native["projection_supported"] is True
    assert native["diagnostics_json_present"] is False
    assert native["diagnostic_count"] == 0
    assert mncs_test.native_compile_diagnostic_records(native, document) == []


@pytest.mark.parametrize(
    "diagnostic",
    [
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "needs expected values",
            "span": {"start": 1, "end": 2, "line": 1, "column": 1},
            "expected": ["type", 17],
            "found": None,
        },
        {
            "code": "MNE041",
            "stage": "elaboration",
            "severity": "error",
            "message": "has too many related records",
            "span": {"start": 1, "end": 2, "line": 1, "column": 1},
            "expected": [],
            "found": None,
            "related": [
                {
                    "code": "MNE042",
                    "stage": "elaboration",
                    "severity": "error",
                    "message": "related",
                    "span": {"start": 1, "end": 2, "line": 1, "column": 1},
                    "expected": [],
                    "found": None,
                }
                for _ in range(5)
            ],
        },
    ],
)
def test_native_span_preserves_extended_json_diagnostic_values(
    diagnostic: dict,
) -> None:
    document = json.dumps([diagnostic], separators=(",", ":")).encode("utf-8")

    native = _document_result(document)

    assert native is not None
    assert native["projection_supported"] is True
    assert mncs_test.native_compile_diagnostic_records(native, document) == [diagnostic]


def test_native_span_filters_non_object_diagnostic_array_entries() -> None:
    diagnostic = {
        "code": "MNE041",
        "stage": "elaboration",
        "severity": "error",
        "message": "kept object",
        "span": {"start": 1, "end": 2, "line": 1, "column": 1},
        "expected": ["type"],
        "found": None,
    }
    document = json.dumps(
        [None, diagnostic, "ignored scalar", [diagnostic]], separators=(",", ":")
    ).encode("utf-8")

    native = _document_result(document)

    assert native is not None
    assert native["projection_supported"] is True
    assert native["diagnostic_count"] == 1
    assert mncs_test.native_compile_diagnostic_records(native, document) == [diagnostic]


def test_native_span_declines_selected_arrays_over_its_item_bound() -> None:
    diagnostic = {
        "code": "MNE041",
        "stage": "elaboration",
        "severity": "error",
        "message": "bounded item",
        "span": {"start": 1, "end": 2, "line": 1, "column": 1},
        "expected": [],
        "found": None,
    }
    document = json.dumps([diagnostic] * 65, separators=(",", ":")).encode("utf-8")

    native = _document_result(document)

    assert native is None or native["projection_supported"] is False


def test_compile_entry_decodes_only_native_diagnostic_span(
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
