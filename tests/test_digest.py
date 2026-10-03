from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test_digest import (
    DigestError,
    evaluate_digest,
    project_digest_request,
    render_digest_text,
)

LANGUAGE_ROOT = ROOT.parent / "mncs-language"
COMMONS_ROOT = ROOT.parent / "MNCS-Commons"
MNCS = Path(os.environ.get("MNCS_BINARY", LANGUAGE_ROOT / "target/debug/mncs"))


def _stdlib_library() -> Path:
    """Standard-library tree: explicit root, else family sibling, else legacy."""
    explicit = os.environ.get("MNCS_STDLIB_ROOT")
    if explicit:
        return Path(explicit) / "library"
    sibling = ROOT.parent / "mncs-stdlib"
    if (sibling / "stdlib-manifest.json").is_file():
        return sibling / "library"
    return LANGUAGE_ROOT / "library"


STDLIB_LIBRARY = _stdlib_library()


def compiler_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = ":".join(
        [
            str(STDLIB_LIBRARY),
            str(COMMONS_ROOT / "src/mncs_commons/mesh"),
            str(ROOT / "native"),
            str(ROOT),
        ]
    )
    return environment


def make_entry(
    name: str,
    verdict: str,
    kind: str,
    expected: int,
    actual: int,
    code: int,
    line: int,
) -> dict:
    return {
        "entry": name.rsplit("::", 1)[-1],
        "source": "sparktest/fail_suite.mncs",
        "source_span": {"line": line, "column": 1, "start": 0, "end": 1},
        "semantic": {
            "qualified_name": name,
            "semantic_fingerprint": "ab" * 32,
        },
        "native_result": {
            "verdict": verdict,
            "failure_kind": kind,
            "assertions": 1,
            "failures": 1 if verdict == "FAIL" else 0,
            "expected": expected,
            "actual": actual,
            "assertion_code": code,
        },
    }


def make_result(entries: list[dict]) -> dict:
    return {
        "schema_version": "mncs.test-result/1",
        "run_id": "cd" * 32,
        "scope": {"module": "sparktest.fail_suite", "source": "sparktest/fail_suite.mncs"},
        "execution": {
            "backend": "mncs-research-bytecode",
            "artifact_identity": "mncs:compiler:backend-artifact:probe",
        },
        "tests": entries,
    }


def test_digest_counts_fold_through_the_native_suite(tmp_path: Path) -> None:
    request = project_digest_request(
        make_result(
            [
                make_entry("s::wrong", "FAIL", "assertion", 43, 42, 2001, 5),
                make_entry("s::right", "PASS", "nofailure", 42, 42, 2002, 8),
                make_entry("s::later", "SKIP", "nofailure", 0, 0, 0, 11),
            ]
        ),
        max_failures=8,
    )
    digest = evaluate_digest(
        request, mncs=str(MNCS), cwd=tmp_path, environment=compiler_environment()
    )
    assert digest["schema_version"] == "mncs.test-digest/1"
    summary = digest["summary"]
    assert (summary["total"], summary["passed"], summary["failed"], summary["skipped"]) == (3, 1, 1, 1)
    assert summary["verdict"] == "FAIL"
    assert summary["assertion_failures"] == 1
    assert digest["failure_rows"] == 1
    assert digest["failures_omitted"] == 0
    (row,) = digest["failures"]
    assert row["qualified_name"] == "s::wrong"
    assert (row["expected"], row["actual"], row["assertion_code"]) == (43, 42, 2001)
    assert (row["source"], row["line"]) == ("sparktest/fail_suite.mncs", 5)


def test_digest_truncation_counts_omissions_without_changing_the_verdict(tmp_path: Path) -> None:
    request = project_digest_request(
        make_result(
            [
                make_entry("s::wrong", "FAIL", "assertion", 43, 42, 2001, 5),
                make_entry("s::alsowrong", "FAIL", "assertion", 1, 2, 2002, 9),
            ]
        ),
        max_failures=1,
    )
    digest = evaluate_digest(
        request, mncs=str(MNCS), cwd=tmp_path, environment=compiler_environment()
    )
    assert digest["summary"]["verdict"] == "FAIL"
    assert digest["failure_rows"] == 2
    assert digest["failures_omitted"] == 1
    (row,) = digest["failures"]
    assert row["qualified_name"] == "s::wrong"


def test_digest_rejects_unknown_result_vocabulary() -> None:
    with pytest.raises(DigestError):
        project_digest_request(
            make_result([make_entry("s::weird", "MAYBE", "nofailure", 0, 0, 0, 1)])
        )
    with pytest.raises(DigestError):
        project_digest_request(
            make_result([make_entry("s::weird", "FAIL", "cosmic-ray", 0, 0, 0, 1)])
        )
    with pytest.raises(DigestError):
        project_digest_request({"schema_version": "mncs.test-result/0"})


def test_rendered_digest_names_every_failure_compactly() -> None:
    digest = {
        "summary": {
            "verdict": "FAIL",
            "total": 2,
            "passed": 1,
            "failed": 1,
            "skipped": 0,
            "unsupported": 0,
        },
        "failures": [
            {
                "qualified_name": "s::wrong",
                "expected": 43,
                "actual": 42,
                "assertion_code": 2001,
                "source": "sparktest/fail_suite.mncs",
                "line": 5,
                "semantic_fingerprint": "ab" * 32,
            }
        ],
        "failures_omitted": 0,
        "run": {"run_id": "cd" * 32, "module_name": "s", "backend": "b"},
    }
    text = render_digest_text(digest)
    assert text.startswith("mncs-test digest: FAIL s (2 tests, 1 passed, 1 failed")
    assert "s::wrong expected=43 actual=42 code=2001 sparktest/fail_suite.mncs:5" in text
    assert len(text) < 600
