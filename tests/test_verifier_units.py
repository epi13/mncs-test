"""Fast unit tests for verifier soundness helpers (no toolchain needed)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test_native import run_native_app, toolchain_cache_dir  # noqa: E402
from mncs_test_verify import (  # noqa: E402
    VERIFY_VERSION,
    build_coherence_row,
    evidence_envelope,
    evidence_id_for,
    find_recall_candidate,
    finish_execution,
    measure_closure,
    obligation_scope,
    parse_evidence_envelope,
    receipt_core,
    run_stamp,
    semantic_match,
    stdlib_bundle_digest,
    toolchain_identity,
)


def test_cache_scope_is_stable_per_toolchain_and_distinct_across(tmp_path: Path) -> None:
    debug = tmp_path / "debug" / "mncs"
    release = tmp_path / "release" / "mncs"
    debug.parent.mkdir(parents=True)
    release.parent.mkdir(parents=True)
    debug.write_bytes(b"d")
    release.write_bytes(b"r")
    first = toolchain_cache_dir(str(debug))
    assert toolchain_cache_dir(str(debug)) == first
    assert toolchain_cache_dir(str(release)) != first
    assert first.parent.parent.name == ".mncs"


def test_bash_and_python_scopes_agree(tmp_path: Path) -> None:
    binary = tmp_path / "mncs"
    binary.write_bytes(b"x")
    # These three lines mirror the bin/mncs-test-* adapters exactly.
    completed = subprocess.run(
        [
            "bash",
            "-c",
            '_R="$(command -v "$1" 2>/dev/null || printf \'%s\' "$1")";'
            ' _C="$(realpath -m "$_R" 2>/dev/null || printf \'%s\' "$_R")";'
            ' printf \'%s\' "$_C" | sha256sum | cut -c1-16',
            "scope",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    assert toolchain_cache_dir(str(binary)).name == completed.stdout.strip()


def test_transport_scopes_cache_unless_overridden(tmp_path: Path) -> None:
    descriptor_dir = ROOT / "native-applications"
    assert (descriptor_dir / "test-digest.json").is_file()
    request = {"schema_version": "mncs.test-digest-request/1"}
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["argv"] = command
        completed = mock.Mock()
        completed.returncode = 2
        completed.stderr = "boom"
        completed.stdout = ""
        return completed

    environment = dict(os.environ)
    environment.pop("MNCS_NATIVE_APPLICATION_CACHE_DIR", None)
    with mock.patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(Exception):
            run_native_app(
                "test-digest.json", request,
                request_schema="mncs.test-digest-request/1",
                result_schema="mncs.test-digest/1",
                mncs=str(tmp_path / "mncs"), cwd=tmp_path, environment=environment,
            )
    argv = seen["argv"]
    assert "--cache-dir" in argv
    scope = argv[argv.index("--cache-dir") + 1]
    assert scope == str(toolchain_cache_dir(str(tmp_path / "mncs")))

    environment["MNCS_NATIVE_APPLICATION_CACHE_DIR"] = "/tmp/custom-cache"
    with mock.patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(Exception):
            run_native_app(
                "test-digest.json", request,
                request_schema="mncs.test-digest-request/1",
                result_schema="mncs.test-digest/1",
                mncs=str(tmp_path / "mncs"), cwd=tmp_path, environment=environment,
            )
    assert "--cache-dir" not in seen["argv"]


def test_run_stamps_are_unique_and_carry_pid() -> None:
    stamps = {run_stamp() for _ in range(1000)}
    assert len(stamps) == 1000
    assert all(f"{os.getpid():x}" in stamp for stamp in stamps)


def test_toolchain_identity_hashes_content_every_time(tmp_path: Path) -> None:
    binary = tmp_path / "mncs"
    binary.write_text("#!/bin/sh\necho mncs 0.1.0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    identity, version = toolchain_identity(str(binary))
    assert version == "mncs 0.1.0"
    assert len(identity) == 64
    again, _ = toolchain_identity(str(binary))
    assert again == identity
    binary.write_bytes(b"#!/bin/sh\necho mncs 0.1.0\n# changed\n")
    changed, _ = toolchain_identity(str(binary))
    assert changed != identity


def test_stdlib_bundle_digest_tracks_set_unset_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MNCS_STDLIB_BUNDLE", raising=False)
    assert stdlib_bundle_digest() == "stdlib-bundle:unset"
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MNCS_STDLIB_BUNDLE", str(bundle))
    first = stdlib_bundle_digest()
    assert first.startswith("stdlib-bundle:sha256:")
    bundle.write_text('{"a":1}', encoding="utf-8")
    assert stdlib_bundle_digest() != first
    monkeypatch.setenv("MNCS_STDLIB_BUNDLE", str(tmp_path / "absent.json"))
    with pytest.raises(Exception):
        stdlib_bundle_digest()


def _world(*, truncated: bool, receipt=None) -> dict:
    return {
        "obligation": {"identity": "ob", "executor": {}},
        "bound": {"definition_identity": "d"},
        "libraries": [],
        "inventory_identities": ["t1", "t2"],
        "inventory_truncated": truncated,
        "test_count": 70 if truncated else 2,
        "receipt": receipt,
    }


def _execution() -> dict:
    return {
        "outcome": "pass",
        "detail": None,
        "exit_code": 0,
        "result_path": "result.json",
        "result": {
            "schema_version": "mncs.test-result/1",
            "run_id": "ab" * 32,
            "scope": {"module": "m", "source": "s.mncs"},
            "execution": {"backend": "b", "artifact_identity": "a"},
            "tests": [
                {
                    "source": "s.mncs",
                    "source_span": {"line": 1, "column": 1},
                    "semantic": {
                        "qualified_name": "m::t",
                        "semantic_fingerprint": "cd" * 32,
                    },
                    "native_result": {
                        "verdict": "PASS",
                        "failure_kind": "nofailure",
                        "assertions": 1,
                        "failures": 0,
                        "expected": 1,
                        "actual": 1,
                        "assertion_code": 7,
                    },
                }
            ],
        },
    }


def test_truncated_inventory_reports_partial_without_receipt(tmp_path: Path) -> None:
    import mncs_test_verify as verify_module

    digest = {
        "schema_version": "mncs.test-digest/1",
        "summary": {"verdict": "PASS"},
        "failures": [],
        "failures_omitted": 0,
        "run": {},
    }
    world = _world(truncated=True)
    entry: dict = {"identity": "ob", "notes": []}
    stats = {"subprocesses": 0, "digest_runs": 0}
    with mock.patch.object(verify_module, "evaluate_digest", return_value=digest):
        result = finish_execution(
            world, entry, _execution(), tmp_path / "artifacts",
            tmp_path / "receipts", tmp_path / "store", "mncs",
            tmp_path, 8, False, stats, ["t1", "t2"],
        )
    assert result["action"] == "executed"
    assert result["verdict"] == "UNKNOWN"
    assert result["verdict_source"] == "executed-partial"
    assert result["coverage"] == {"considered": 70, "selected": 2, "complete": False}
    assert any("truncated" in note for note in result["notes"])
    assert list((tmp_path / "receipts").glob("*.json")) == [] if (
        tmp_path / "receipts"
    ).exists() else True


def test_executed_entry_carries_previous_evidence_link(tmp_path: Path) -> None:
    import mncs_test_verify as verify_module

    digest = {
        "schema_version": "mncs.test-digest/1",
        "summary": {"verdict": "PASS"},
        "failures": [],
        "failures_omitted": 0,
        "run": {"artifact_identity": ""},
    }
    receipt = {"verdict": "FAIL", "evidence_id": "previous-evidence"}
    world = _world(truncated=False, receipt=receipt)
    entry: dict = {"identity": "ob", "notes": []}
    stats = {"subprocesses": 0, "digest_runs": 0}
    with mock.patch.object(verify_module, "evaluate_digest", return_value=digest):
        result = finish_execution(
            world, entry, _execution(), tmp_path / "artifacts",
            tmp_path / "receipts", tmp_path / "store", "mncs",
            tmp_path, 8, False, stats, ["t1", "t2"],
        )
    assert result["verdict"] == "PASS"
    assert result["transition"] == "fixed"
    assert result["previous_evidence_id"] == "previous-evidence"
    stored = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert stored["previous_evidence_id"] == "previous-evidence"


def _closure_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "suite.mncs").write_text("test one {}\n")
    (repo / "src" / "other.mncs").write_text("module other {}\n")
    (repo / "README.md").write_text("docs\n")
    (repo / ".mncs").mkdir()
    (repo / ".mncs" / "project.json").write_text("{}\n")
    return repo


def _closure(repo: Path, *roots: str, stats=None, mncs: str = "mncs-absent"):
    return measure_closure(
        [str(repo), *roots],
        {} if stats is None else stats,
        mncs=mncs,
        repo=repo,
        inventory_relpath=".mncs/project.json",
    )


def test_closure_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    repo = _closure_repo(tmp_path)
    first, trusted = _closure(repo)
    assert trusted and first
    again, trusted_again = _closure(repo)
    assert trusted_again and again == first
    (repo / "src" / "other.mncs").write_text("module other { changed }\n")
    changed, trusted_changed = _closure(repo)
    assert trusted_changed and changed != first


def test_closure_ignores_non_source_but_tracks_manifests(tmp_path: Path) -> None:
    repo = _closure_repo(tmp_path)
    before, _ = _closure(repo)
    (repo / "README.md").write_text("docs with more words\n")
    (repo / "notes.txt").write_text("scratch\n")
    after, trusted = _closure(repo)
    assert trusted and after == before
    # The repository manifests are host inputs: manifest edits matter.
    (repo / ".mncs" / "project.json").write_text('{"v": 2}\n')
    manifest_changed, _ = _closure(repo)
    assert manifest_changed != before


def test_closure_prunes_volatile_vcs_and_derived(tmp_path: Path) -> None:
    repo = _closure_repo(tmp_path)
    before, _ = _closure(repo)
    (repo / ".git" / "objects").mkdir(parents=True)
    (repo / ".git" / "objects" / "pack").write_text("git-bytes\n")
    (repo / ".mncs" / "test-receipts").mkdir(parents=True)
    (repo / ".mncs" / "test-receipts" / "r.json").write_text("{}\n")
    (repo / ".mncs" / "test-artifacts" / "run1").mkdir(parents=True)
    (repo / ".mncs" / "test-artifacts" / "run1" / "result.json").write_text("{}\n")
    (repo / "native-applications" / ".mncs" / "toolchains").mkdir(parents=True)
    (repo / "native-applications" / ".mncs" / "toolchains" / "a.json").write_text("{}\n")
    (repo / "tools" / "__pycache__").mkdir(parents=True)
    (repo / "tools" / "__pycache__" / "m.pyc").write_bytes(b"bytecode")
    after, trusted = _closure(repo)
    assert trusted and after == before


def test_closure_follows_symlinked_roots_and_fails_closed(tmp_path: Path) -> None:
    repo = _closure_repo(tmp_path)
    sibling = tmp_path / "sibling-lib"
    (sibling / "lib").mkdir(parents=True)
    (sibling / "lib" / "dep.mncs").write_text("module dep {}\n")
    (repo / "sibling-lib").symlink_to(sibling, target_is_directory=True)
    direct, _ = _closure(repo, str(sibling))
    assert direct
    (sibling / "lib" / "dep.mncs").write_text("module dep { changed }\n")
    changed, trusted_changed = _closure(repo, str(sibling))
    assert trusted_changed and changed != direct
    # Entries use logical paths: repointing the link at an identical
    # target preserves the closure.
    via_link, _ = measure_closure(
        [str(repo), str(repo / "sibling-lib")],
        {},
        mncs="mncs-absent",
        repo=repo,
        inventory_relpath=".mncs/project.json",
    )
    moved = tmp_path / "moved-lib"
    sibling.rename(moved)
    (repo / "sibling-lib").unlink()
    (repo / "sibling-lib").symlink_to(moved, target_is_directory=True)
    restated, trusted_restated = measure_closure(
        [str(repo), str(repo / "sibling-lib")],
        {},
        mncs="mncs-absent",
        repo=repo,
        inventory_relpath=".mncs/project.json",
    )
    assert trusted_restated and restated == via_link
    (tmp_path / "cycle").symlink_to(tmp_path / "cycle", target_is_directory=True)
    cyclic, trusted_cyclic = _closure(repo, str(tmp_path / "cycle"))
    assert (cyclic, trusted_cyclic) == ("", False)
    missing, trusted_missing = _closure(repo, str(tmp_path / "absent"))
    assert (missing, trusted_missing) == ("", False)
    dangling = repo / "dangling.mncs"
    dangling.symlink_to(repo / "nowhere.mncs")
    try:
        dangled, trusted_dangled = _closure(repo)
        assert (dangled, trusted_dangled) == ("", False)
    finally:
        dangling.unlink()


def test_closure_bounds_fail_closed_without_exceptions(tmp_path: Path) -> None:
    import mncs_test_verify as verify_module

    repo = _closure_repo(tmp_path)
    with mock.patch.object(verify_module, "MAX_CLOSURE_FILES", 1):
        identity, trusted = _closure(repo)
    assert (identity, trusted) == ("", False)
    with mock.patch.object(verify_module, "MAX_CLOSURE_BYTES", 1):
        identity, trusted = _closure(repo)
    assert (identity, trusted) == ("", False)


def test_stdlib_root_authority_tristate(tmp_path: Path, monkeypatch) -> None:
    import mncs_test_verify as verify_module

    monkeypatch.delenv("MNCS_STDLIB_ROOT", raising=False)
    marker, extra = verify_module.stdlib_root_authority("mncs-absent")
    assert (marker, extra) == ("stdlib-root:absent", None)
    monkeypatch.setenv("MNCS_STDLIB_ROOT", "")
    marker, extra = verify_module.stdlib_root_authority("mncs-absent")
    assert (marker, extra) == ("stdlib-root:disabled", None)
    monkeypatch.setenv("MNCS_STDLIB_ROOT", str(tmp_path))
    (tmp_path / "library").mkdir()
    marker, extra = verify_module.stdlib_root_authority("mncs-absent")
    assert marker == f"stdlib-root:pinned:{tmp_path}"
    assert extra == tmp_path / "library"
    repo = _closure_repo(tmp_path)
    pinned, trusted = _closure(repo)
    assert trusted and pinned
    monkeypatch.delenv("MNCS_STDLIB_ROOT")
    unpinned, _ = _closure(repo)
    assert unpinned != pinned


def _obligation() -> dict:
    return {
        "identity": "ob-closure",
        "lifecycle": "permanent",
        "executor": {
            "kind": "native_first_class_test",
            "source_paths": ["src/suite.mncs"],
            "declaration_identities": ["*"],
        },
    }


def _bound(**overrides: object) -> dict:
    bound = {
        "definition_identity": "d",
        "subject_identity": "s",
        "subject_fingerprint": "f",
        "executor_identity": "e",
        "invalidation_identity": "i",
        "toolchain_identity": "t",
        "inventory_identity": "v",
        "repository_revision": "r",
        "repository_fingerprint": "p",
        "closure_identity": "c" * 64,
        "closure_trusted": True,
        "closure_fileset": "mncs+manifests/1",
    }
    bound.update(overrides)
    return bound


def test_coherence_row_v2_carries_closure_both_sides() -> None:
    receipt = {
        "bound": _bound(closure_identity="old"),
        "verifier_identity": "ver",
        "verdict": "PASS",
        "evidence_id": "ev",
    }
    row = build_coherence_row(
        _obligation(), _bound(), ["t1"], False, receipt, True,
    )
    assert row["current"]["closure_identity"] == "c" * 64
    assert row["current"]["closure_trusted"] is True
    assert row["current"]["closure_fileset"] == "mncs+manifests/1"
    assert row["evidence"]["closure_identity"] == "old"
    assert row["evidence"]["closure_trusted"] is True
    assert row["evidence"]["closure_fileset"] == "mncs+manifests/1"
    assert row["evidence"]["verdict"] == "PASS"


def test_coherence_row_v2_defaults_legacy_receipt_untrusted() -> None:
    stored = _bound()
    del stored["closure_identity"]
    del stored["closure_trusted"]
    del stored["closure_fileset"]
    receipt = {
        "bound": stored,
        "verifier_identity": "ver",
        "verdict": "PASS",
        "evidence_id": "ev",
    }
    row = build_coherence_row(
        _obligation(), _bound(), ["t1"], False, receipt, True,
    )
    assert row["evidence"]["closure_identity"] == ""
    assert row["evidence"]["closure_trusted"] is False
    assert row["evidence"]["closure_fileset"] == ""


def _receipt() -> dict:
    return {
        "schema_version": "mncs.test-receipt/1",
        "obligation_identity": "ob-ev",
        "verdict": "PASS",
        "bound": _bound(),
        "verifier_identity": "ver",
        "selected_test_identities": ["t1"],
        "digest": {"summary": {"verdict": "PASS"}},
        "result_sha256": "r" * 64,
        "producer": "mncs-test-verify/0.3.0",
        "evidence_id": "",
        "previous_evidence_id": "prev",
    }


def test_evidence_envelope_round_trip() -> None:
    receipt = _receipt()
    receipt["evidence_id"] = evidence_id_for(receipt_core(receipt))
    payload = evidence_envelope(receipt, "prev")
    parsed = parse_evidence_envelope(payload, "ob-ev", receipt["evidence_id"])
    assert parsed is not None
    assert parsed["verdict"] == "PASS"
    assert parsed["bound"] == _bound()
    assert parsed["evidence_id"] == receipt["evidence_id"]
    assert parsed["previous_evidence_id"] == "prev"
    assert parsed["producer"] == "mncs-test-verify/0.3.0"


def test_evidence_envelope_rejects_misbinding() -> None:
    receipt = _receipt()
    receipt["evidence_id"] = evidence_id_for(receipt_core(receipt))
    payload = evidence_envelope(receipt, None)
    assert parse_evidence_envelope(payload, "other-obligation", receipt["evidence_id"]) is None
    assert parse_evidence_envelope(payload, "ob-ev", "0" * 64) is None
    assert parse_evidence_envelope(b"not json", "ob-ev", receipt["evidence_id"]) is None
    tampered = json.loads(payload.decode())
    tampered["core"]["verdict"] = "FAIL"
    assert (
        parse_evidence_envelope(
            json.dumps(tampered).encode(), "ob-ev", receipt["evidence_id"]
        )
        is None
    )


def test_obligation_scope_is_stable_and_prefixed() -> None:
    first = obligation_scope("ob-ev")
    assert first == obligation_scope("ob-ev")
    assert first != obligation_scope("ob-other")
    assert first.endswith(b":")


def test_semantic_match_ignores_revision_only() -> None:
    current = _bound(repository_revision="r2", repository_fingerprint="p2")
    candidate = _bound(repository_revision="r1", repository_fingerprint="p1")
    assert semantic_match(candidate, current) is True
    assert semantic_match(_bound(closure_identity="other"), current) is False
    assert semantic_match(_bound(closure_trusted=False), current) is False
    assert semantic_match(_bound(closure_fileset="other/1"), current) is False
    assert semantic_match(_bound(toolchain_identity="other"), current) is False
    assert semantic_match(_bound(definition_identity="other"), current) is False


def _history_evidence(evidence_id: str, bound: dict, previous: str | None) -> dict:
    return {
        "evidence_id": evidence_id,
        "verdict": "PASS",
        "bound": bound,
        "producer": VERIFY_VERSION,
        "selected_test_identities": ["t1"],
        "previous_evidence_id": previous,
    }


def _recall_world(head: dict, history: list[dict], bound: dict) -> dict:
    return {
        "obligation": _obligation(),
        "bound": bound,
        "inventory_identities": ["t1"],
        "receipt": head,
        "history": history,
    }


def test_find_recall_candidate_walks_chain_newest_first() -> None:
    world_bound = _bound()
    old = _history_evidence("old", _bound(), None)
    mid = _history_evidence("mid", _bound(closure_identity="other"), "old")
    head = _history_evidence("head", _bound(closure_identity="another"), "mid")
    world = _recall_world(head, [head, mid, old], world_bound)
    assert find_recall_candidate(world)["evidence_id"] == "old"


def test_find_recall_candidate_rejects_mismatch_and_gaps() -> None:
    world_bound = _bound()
    head = _history_evidence("head", _bound(closure_identity="another"), "mid")
    # No match anywhere on the chain.
    world = _recall_world(head, [head], world_bound)
    assert find_recall_candidate(world) is None
    # Chain leaves known history.
    orphan = _history_evidence("orphan", _bound(), "missing-parent")
    world = _recall_world(head, [head, orphan], world_bound)
    assert find_recall_candidate(world) is None
    # Producer drift disqualifies even an exact semantic match.
    stale = _history_evidence("stale", _bound(), None)
    stale["producer"] = "mncs-test-verify/0.0.0"
    head2 = _history_evidence("head2", _bound(closure_identity="x"), "stale")
    world = _recall_world(head2, [head2, stale], world_bound)
    assert find_recall_candidate(world) is None
