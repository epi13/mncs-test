from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test import (
    ManifestError,
    evaluate_verification_coherence,
)

LANGUAGE_ROOT = ROOT.parent / "mncs-language"
COMMONS_ROOT = ROOT.parent / "MNCS-Commons"
MNCS = Path(os.environ.get("MNCS_BINARY", LANGUAGE_ROOT / "target/debug/mncs"))

CURRENT = {
    "definition_identity": "def-1",
    "subject_identity": "subj-1",
    "subject_fingerprint": "fp-1",
    "executor_identity": "exec-1",
    "invalidation_identity": "inv-1",
    "toolchain_identity": "tool-1",
    "inventory_identity": "tinv-1",
    "repository_revision": "rev-1",
    "repository_fingerprint": "rfp-1",
}

EVIDENCE_EMPTY = {
    "definition_identity": "",
    "subject_identity": "",
    "subject_fingerprint": "",
    "executor_identity": "",
    "verifier_identity": "",
    "invalidation_identity": "",
    "toolchain_identity": "",
    "inventory_identity": "",
    "repository_revision": "",
    "repository_fingerprint": "",
    "verdict": "UNKNOWN",
    "evidence_id": "",
}


def compiler_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = ":".join(
        [
            str(LANGUAGE_ROOT / "library"),
            str(COMMONS_ROOT / "src/mncs_commons/mesh"),
            str(ROOT / "native"),
            str(ROOT),
        ]
    )
    return environment


def evidence_for(current: dict[str, str], verdict: str = "PASS", evidence_id: str = "ev-1") -> dict[str, str]:
    evidence = dict(EVIDENCE_EMPTY)
    for key in (
        "definition_identity",
        "subject_identity",
        "subject_fingerprint",
        "executor_identity",
        "invalidation_identity",
        "toolchain_identity",
        "inventory_identity",
        "repository_revision",
        "repository_fingerprint",
    ):
        evidence[key] = current[key]
    evidence["verifier_identity"] = "ver-1"
    evidence["verdict"] = verdict
    evidence["evidence_id"] = evidence_id
    return evidence


def obligation(identity: str, current: dict[str, str], **overrides) -> dict:
    item: dict = {
        "identity": identity,
        "lifecycle": "permanent",
        "executor_kind": "native_first_class_test",
        "runnable_native": True,
        "current": dict(current),
        "declared_patterns": ["*"],
        "inventory_test_identities": ["t1"],
        "inventory_truncated": False,
        "evidence_present": False,
        "evidence": dict(EVIDENCE_EMPTY),
        "evidence_conflict": False,
        "provider_available": True,
    }
    item.update(overrides)
    return item


def evaluate(obligations: list[dict], max_executions: int = 8) -> dict:
    return evaluate_verification_coherence(
        {
            "schema_version": "mncs.test-verification-coherence-request/1",
            "max_executions": max_executions,
            "obligations": obligations,
        },
        mncs=str(MNCS),
        cwd=ROOT,
        environment=compiler_environment(),
    )


def test_matching_evidence_is_current_without_queue() -> None:
    result = evaluate(
        [obligation("ob-cur", CURRENT, evidence_present=True, evidence=evidence_for(CURRENT))]
    )
    verdict = result["verdicts"][0]
    assert verdict["status"] == "current"
    assert verdict["reason"] == "evidence_current"
    assert result["run_queue"] == []
    assert result["summary"]["current"] == 1


def test_changed_input_invalidates_evidence() -> None:
    changed = dict(CURRENT, subject_fingerprint="fp-2")
    result = evaluate(
        [obligation("ob-stale", changed, evidence_present=True, evidence=evidence_for(CURRENT))]
    )
    verdict = result["verdicts"][0]
    assert verdict["status"] == "stale"
    assert verdict["reason"] == "input_changed"
    assert result["run_queue"] == ["ob-stale"]


def test_recorded_fail_is_current_knowledge() -> None:
    result = evaluate(
        [
            obligation(
                "ob-fail",
                CURRENT,
                evidence_present=True,
                evidence=evidence_for(CURRENT, verdict="FAIL"),
            )
        ]
    )
    assert result["verdicts"][0]["status"] == "current"
    assert result["summary"]["failed"] == 1
    assert result["run_queue"] == []


def test_exclusion_matrix_never_queues() -> None:
    result = evaluate(
        [
            obligation(
                "ob-conflict",
                CURRENT,
                evidence_present=True,
                evidence_conflict=True,
                evidence=evidence_for(CURRENT),
            ),
            obligation("ob-retired", CURRENT, lifecycle="retired"),
            obligation("ob-external", CURRENT, executor_kind="external_integration"),
            obligation("ob-noprov", CURRENT, provider_available=False),
            obligation(
                "ob-unres",
                CURRENT,
                declared_patterns=["missing"],
                inventory_test_identities=["t1"],
            ),
            obligation("ob-trunc", CURRENT, inventory_truncated=True),
        ]
    )
    got = {item["identity"]: (item["status"], item["reason"]) for item in result["verdicts"]}
    assert got["ob-conflict"] == ("contradictory", "evidence_conflict")
    assert got["ob-retired"] == ("not_selected", "lifecycle_excluded")
    assert got["ob-external"] == ("not_selected", "executor_not_runnable")
    assert got["ob-noprov"] == ("escalation_required", "provider_unavailable")
    assert got["ob-unres"] == ("selection_unresolved", "selection_unresolved")
    assert got["ob-trunc"] == ("selection_unresolved", "selection_unresolved")
    assert result["run_queue"] == []


def test_execution_budget_defers_overflow() -> None:
    result = evaluate([obligation(f"ob-{index}", CURRENT) for index in range(3)], max_executions=2)
    assert result["run_queue"] == ["ob-0", "ob-1"]
    assert result["summary"]["queued"] == 2
    assert result["summary"]["deferred"] == 1
    assert [item["deferred"] for item in result["verdicts"]] == [False, False, True]


def test_exact_patterns_resolve_with_dedup() -> None:
    result = evaluate(
        [
            obligation(
                "ob-exact",
                CURRENT,
                declared_patterns=["t1", "t2", "t1"],
                inventory_test_identities=["t1", "t2", "t3"],
            )
        ]
    )
    verdict = result["verdicts"][0]
    assert verdict["resolved_test_identities"] == ["t1", "t2"]
    assert verdict["unresolved_count"] == 0
    assert verdict["status"] == "new_execution_required"


def test_adapter_rejects_bad_schema() -> None:
    with pytest.raises(ManifestError):
        evaluate_verification_coherence(
            {"schema_version": "wrong", "obligations": []},
            mncs=str(MNCS),
            cwd=ROOT,
            environment=compiler_environment(),
        )
