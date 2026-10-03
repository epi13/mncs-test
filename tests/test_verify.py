from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test_verify import (  # noqa: E402
    read_receipt,
    receipt_path,
    verify_repository,
)

LANGUAGE_ROOT = ROOT.parent / "mncs-language"
MNCS = Path(os.environ.get("MNCS_BINARY", LANGUAGE_ROOT / "target/debug/mncs"))


def _stdlib_library() -> Path:
    explicit = os.environ.get("MNCS_STDLIB_ROOT")
    if explicit:
        return Path(explicit) / "library"
    sibling = ROOT.parent / "mncs-stdlib"
    if (sibling / "stdlib-manifest.json").is_file():
        return sibling / "library"
    return LANGUAGE_ROOT / "library"


STDLIB_LIBRARY = _stdlib_library()

TINY_SUITE = """mncs 0.18;
module fv.tiny;
use mncs.test.assertions;
use mncs.test.suite;
test one() -> (result: TestResult) {
    return from_assertion(equals_i64(1, 1, 9001));
}
test two() -> (result: TestResult) {
    return from_assertion(equals_bool(true, true, 9002));
}
"""


def _run_git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def make_fixture_repo(path: Path) -> Path:
    (path / ".mncs").mkdir(parents=True, exist_ok=True)
    (path / "fv").mkdir(parents=True, exist_ok=True)
    (path / ".mncs" / "project.json").write_text(
        json.dumps(
            {
                "schema_version": "mncs-family.repository-manifest/v0alpha1",
                "repository": "fixture-verify",
                "verification": {
                    "obligation_inventory": ".mncs/verification-obligations.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (path / ".mncs" / "verification-obligations.json").write_text(
        json.dumps(
            {
                "schema_version": "mncs-family.verification-obligation-inventory/v1",
                "repository": "fixture-verify",
                "revision": 1,
                "obligations": [
                    {
                        "identity": "fixture.suite",
                        "lifecycle": "permanent",
                        "invalidation_dependencies": ["fv/tiny.mncs"],
                        "executor": {
                            "provider": "mncs-test",
                            "kind": "native_first_class_test",
                            "entrypoint": "mncs-test run",
                            "source_paths": ["fv/tiny.mncs"],
                            "library_paths": [str(STDLIB_LIBRARY)],
                            "declaration_identities": ["*"],
                        },
                    },
                    {
                        "identity": "fixture.unbound",
                        "lifecycle": "permanent",
                        "invalidation_dependencies": [],
                        "executor": {
                            "provider": "mncs-test",
                            "kind": "native_first_class_test",
                            "entrypoint": "mncs-test run",
                            "declaration_identities": ["*"],
                        },
                    },
                    {
                        "identity": "fixture.hosted",
                        "lifecycle": "permanent",
                        "invalidation_dependencies": [],
                        "executor": {
                            "provider": "mncs-test",
                            "kind": "compile_experiment",
                            "entrypoint": "tests/test_hosted.py",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (path / "fv" / "tiny.mncs").write_text(TINY_SUITE, encoding="utf-8")
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    (path / ".gitignore").write_text(".mncs/test-receipts/\n.mncs/test-artifacts/\n.mncs/test-store/\n")
    _run_git(path, "init", "-q")
    _run_git(path, "config", "user.email", "fixture@mncs.local")
    _run_git(path, "config", "user.name", "fixture")
    _run_git(path, "config", "commit.gpgsign", "false")
    _run_git(path, "add", "-A")
    _run_git(path, "commit", "-qm", "init")
    return path


def entry_by_id(report: dict, identity: str) -> dict:
    for item in report["obligations"]:
        if item["identity"] == identity:
            return item
    raise AssertionError(f"missing obligation entry: {identity}")


def test_cold_executes_warm_reuses_irrelevant_preserves(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    cold = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    assert cold["summary"]["tests_considered"] == 2
    suite = entry_by_id(cold, "fixture.suite")
    assert suite["action"] == "executed"
    assert suite["verdict"] == "PASS"
    assert suite["transition"] == "new"
    assert suite["store"]["status"] == "admitted"
    assert cold["summary"]["executed"] == 1
    assert receipt_path(repo / ".mncs" / "test-receipts", "fixture.suite").is_file()
    assert entry_by_id(cold, "fixture.unbound")["action"] == "unresolved"
    assert entry_by_id(cold, "fixture.hosted")["action"] == "unresolved"

    warm = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(warm, "fixture.suite")
    assert suite["action"] == "reused"
    assert suite["verdict"] == "PASS"
    assert warm["summary"]["executed"] == 0
    assert warm["stats"]["suite_runs"] == 0
    assert any("store object matches" in note for note in suite["notes"])

    with (repo / "README.md").open("a", encoding="utf-8") as stream:
        stream.write("\nAn unrelated documentation change.\n")
    irrelevant = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(irrelevant, "fixture.suite")
    assert suite["action"] == "reused"
    assert irrelevant["stats"]["suite_runs"] == 0


def test_committed_irrelevant_change_reuses_via_closure(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    produced = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()
    with (repo / "README.md").open("a", encoding="utf-8") as stream:
        stream.write("\nA committed documentation change.\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-qm", "docs: irrelevant change")
    reused = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(reused, "fixture.suite")
    assert suite["action"] == "reused"
    assert suite["verdict"] == "PASS"
    assert suite["verdict_source"] == "reused-closure"
    assert reused["summary"]["reused_closure"] == 1
    assert reused["stats"]["suite_runs"] == 0
    assert any(
        produced[:12] in note and "semantic closure identical" in note
        for note in suite["notes"]
    )


def test_committed_relevant_change_reruns(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite_path = repo / "fv" / "tiny.mncs"
    suite_path.write_text(
        TINY_SUITE.replace("equals_i64(1, 1, 9001)", "equals_i64(2, 1, 9001)"),
        encoding="utf-8",
    )
    _run_git(repo, "add", "fv/tiny.mncs")
    _run_git(repo, "commit", "-qm", "suite: break one assertion")
    regressed = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(regressed, "fixture.suite")
    assert suite["action"] == "executed"
    assert suite["verdict"] == "FAIL"
    assert suite["transition"] == "regression"


def test_changed_reports_affected_without_executing(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    dry_cold = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0, dry_run=True)
    suite = entry_by_id(dry_cold, "fixture.suite")
    assert suite["action"] == "would_execute"
    assert dry_cold["summary"]["would_execute"] == 1
    assert dry_cold["stats"]["suite_runs"] == 0
    assert list((repo / ".mncs" / "test-receipts").glob("*.json")) == [] if (
        repo / ".mncs" / "test-receipts"
    ).exists() else True

    verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    dry_warm = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0, dry_run=True)
    suite = entry_by_id(dry_warm, "fixture.suite")
    assert suite["action"] == "would_reuse"
    assert dry_warm["summary"]["would_reuse"] == 1
    assert dry_warm["stats"]["suite_runs"] == 0

    (repo / "fv" / "tiny.mncs").write_text(
        TINY_SUITE.replace("equals_i64(1, 1, 9001)", "equals_i64(2, 1, 9001)"),
        encoding="utf-8",
    )
    dry_affected = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0, dry_run=True)
    suite = entry_by_id(dry_affected, "fixture.suite")
    assert suite["action"] == "would_execute"
    assert any(
        "affected:" in note and "closure_identity" in note for note in suite["notes"]
    )
    assert dry_affected["stats"]["suite_runs"] == 0


def test_relevant_change_regresses_then_fixes(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite_path = repo / "fv" / "tiny.mncs"
    suite_path.write_text(
        TINY_SUITE.replace("equals_i64(1, 1, 9001)", "equals_i64(2, 1, 9001)"),
        encoding="utf-8",
    )
    regressed = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(regressed, "fixture.suite")
    assert suite["action"] == "executed"
    assert suite["verdict"] == "FAIL"
    assert suite["transition"] == "regression"
    assert regressed["overall"] == "FAIL"
    (row,) = suite["digest"]["failures"]
    assert row["qualified_name"] == "fv.tiny::one"
    assert (row["expected"], row["actual"]) == (2, 1)

    suite_path.write_text(TINY_SUITE, encoding="utf-8")
    fixed = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(fixed, "fixture.suite")
    assert suite["action"] == "executed"
    assert suite["verdict"] == "PASS"
    assert suite["transition"] == "fixed"


def test_corrupt_receipt_heals_by_reexecution(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    path = receipt_path(repo / ".mncs" / "test-receipts", "fixture.suite")
    assert read_receipt(repo / ".mncs" / "test-receipts", "fixture.suite") is not None
    path.write_text("{corrupt", encoding="utf-8")
    healed = verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)
    suite = entry_by_id(healed, "fixture.suite")
    assert suite["action"] == "executed"
    assert suite["verdict"] == "PASS"
    assert read_receipt(repo / ".mncs" / "test-receipts", "fixture.suite") is not None


def test_concurrent_cold_runs_converge_without_corruption(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")

    def run() -> dict:
        return verify_repository(repo, mncs=str(MNCS), suite_timeout=300.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: run(), range(2)))
    for report in (first, second):
        suite = entry_by_id(report, "fixture.suite")
        assert suite["verdict"] == "PASS"
        assert suite["action"] in ("executed", "reused")
    assert read_receipt(repo / ".mncs" / "test-receipts", "fixture.suite") is not None
    sys.path.insert(0, str(ROOT.parent / "mncs-store" / "python"))
    from mncs_store.embedded import EmbeddedStore

    store = EmbeddedStore(repo / ".mncs" / "test-store", read_only=True)
    try:
        assert store.current_generation >= 1
    finally:
        store.close()


def test_cli_adapter_reports_compactly(tmp_path: Path) -> None:
    repo = make_fixture_repo(tmp_path / "repo")
    environment = dict(os.environ)
    environment["MNCS"] = str(MNCS)
    cold = subprocess.run(
        [str(ROOT / "bin" / "mncs-test-verify"), "--repo", str(repo), "--format", "text"],
        capture_output=True, text=True, check=False, timeout=400, env=environment,
    )
    assert cold.returncode == 3  # INCOMPLETE: the unbound/hosted obligations
    assert "3 obligations, 2 tests considered" in cold.stdout
    assert "[executed] fixture.suite: PASS (executed)" in cold.stdout
    assert "cost:" in cold.stdout and "subprocesses" in cold.stdout

    warm = subprocess.run(
        [str(ROOT / "bin" / "mncs-test-verify"), "--repo", str(repo), "--format", "text"],
        capture_output=True, text=True, check=False, timeout=400, env=environment,
    )
    assert warm.returncode == 3
    assert "[reused] fixture.suite: PASS (reused)" in warm.stdout


def test_required_selection_proves_only_exact_matches(tmp_path: Path) -> None:
    from mncs_test_verify import required_selection

    def world(patterns, inventory):
        return {
            "obligation": {"executor": {"declaration_identities": patterns}},
            "inventory_identities": inventory,
        }

    assert required_selection(world(["*"], ["a", "b"])) == ["a", "b"]
    assert required_selection(world(["a"], ["a", "b"])) == ["a"]
    assert required_selection(world(["b", "a"], ["a", "b"])) == ["b", "a"]
    # Unprovable patterns refuse: substrings, globs, and unknown identities.
    assert required_selection(world(["a*"], ["a", "b"])) is None
    assert required_selection(world([""], ["a", "b"])) is None
    assert required_selection(world(["c"], ["a", "b"])) is None
    assert required_selection(world(["*", "c"], ["a", "b"])) == ["a", "b"]


def test_evidence_identity_is_stable_over_run_anchored_fields(tmp_path: Path) -> None:
    from mncs_test_verify import evidence_id_for, receipt_core, write_receipt, read_receipt

    receipts = tmp_path / "receipts"
    base = {
        "schema_version": "mncs.test-receipt/1",
        "obligation_identity": "ob",
        "verdict": "PASS",
        "bound": {"definition_identity": "d"},
        "verifier_identity": "v",
        "selected_test_identities": ["t"],
        "digest": {"summary": {"verdict": "PASS"}},
        "result_sha256": "r",
    }
    first = dict(base, recorded_at="t1", artifact_dir="a1", producer="p")
    first["evidence_id"] = evidence_id_for(receipt_core(first))
    write_receipt(receipts, first)
    assert read_receipt(receipts, "ob") is not None
    # Same core, different run anchoring: identical evidence, still valid.
    second = dict(base, recorded_at="t2", artifact_dir="a2", producer="p")
    second["evidence_id"] = evidence_id_for(receipt_core(second))
    assert second["evidence_id"] == first["evidence_id"]
    write_receipt(receipts, second)
    assert read_receipt(receipts, "ob") is not None
    # A tampered verdict invalidates the receipt without touching the bound world.
    tampered = dict(second, verdict="FAIL")
    assert read_receipt(receipts, "ob") is not None  # file still holds `second`
    tampered["evidence_id"] = second["evidence_id"]
    write_receipt(receipts, tampered)
    assert read_receipt(receipts, "ob") is None
