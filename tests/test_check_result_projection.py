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


def test_native_check_result_projection_matches_family_contract(tmp_path: Path) -> None:
    long_identity = "mncs:test-case:" + ("long-identity-" * 12)
    result_digest = "d" * 64
    source_digest = "a" * 64
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": {"manifest": "mncs-test.toml"},
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": "the native test application is unavailable"},
        "summary": {"passed": 12, "failed": 2, "skipped": 3},
        "tests": [
            {"id": long_identity, "verdict": "FAIL"},
            {"id": "mncs:test-case:later", "verdict": "FAIL"},
        ],
        "provenance": {"runner": {"source_sha256": source_digest}},
        "run_id": "run-native-17",
        "selection": {
            "selected_test_identities": ["mncs:test-case:execution"]
        },
    }

    projected = mncs_test.check_result(
        result,
        result_digest,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        check_id="family:exact:check",
        claim="The exact repository-owned behavioral family check was executed by mncs-test",
    )

    digest = f"sha256:{result_digest}"
    uri = "urn:mncs-test:run-native-17"
    expected_summary = (
        f"unsupported: 12 passed, 2 failed, 3 skipped; failing_identity={long_identity}"
    )
    assert projected == {
        "schema_version": "mncs.check-result/1",
        "id": "family:exact:check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": "mncs-test.toml",
        "claim": "The exact repository-owned behavioral family check was executed by mncs-test",
        "summary": expected_summary,
        "contract_revision": "mncs.test-result/1",
        "producer_revision": f"sha256:{source_digest}",
        "digest": digest,
        "unresolved": ["the native test application is unavailable"],
        "references": [
            {"kind": "mncs-test-result", "uri": uri, "digest": digest}
        ],
        "result_ref": {
            "kind": "mncs-test-result",
            "uri": uri,
            "digest": digest,
            "detail": "retrieve the retained result artifact when per-test detail is required",
        },
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "selection": result["selection"],
    }


def test_native_check_result_projection_preserves_defaults_and_verdict_policy(
    tmp_path: Path,
) -> None:
    result_digest = "b" * 64
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "summary": {},
        "tests": [{"id": "not-a-failure", "verdict": "PASS"}],
        "run_id": "",
    }

    projected = mncs_test.check_result(
        result,
        result_digest,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    digest = f"sha256:{result_digest}"
    uri = "urn:mncs-test:"
    assert projected == {
        "schema_version": "mncs.check-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": "mncs-test",
        "claim": "MNCS-native tests were executed under the compiler-owned inventory or declared compatibility fixture",
        "summary": "unsupported: 0 passed, 0 failed, 0 skipped",
        "contract_revision": "mncs.test-result/1",
        "producer_revision": "sha256:",
        "digest": digest,
        "unresolved": ["unsupported capability"],
        "references": [
            {"kind": "mncs-test-result", "uri": uri, "digest": digest}
        ],
        "result_ref": {
            "kind": "mncs-test-result",
            "uri": uri,
            "digest": digest,
            "detail": "retrieve the retained result artifact when per-test detail is required",
        },
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "selection": None,
    }


def test_native_check_result_projection_reassembles_long_utf8_failure_message(
    tmp_path: Path,
) -> None:
    message = ("failure-🦆é" * 220) + "end"
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-utf8",
    }

    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert projected["unresolved"] == [message]
    assert projected["result_ref"]["uri"] == "urn:mncs-test:run-utf8"


def test_native_check_result_projection_uses_string_scope_for_minimal_result(
    tmp_path: Path,
) -> None:
    result = mncs_test.minimal_result(
        classification="unsupported",
        message="native capability is unavailable",
        cwd=tmp_path,
    )

    projected = mncs_test.check_result(
        result,
        "e" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert projected["scope"] == "mncs-test"
