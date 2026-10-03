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
    finish_execution,
    run_stamp,
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
