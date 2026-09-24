from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test import (
    ArtifactStore,
    _apply_host_grants,
    _cargo_test_target_declarations,
    apply_obligation_plan,
    canonical_execution_request,
    embed_execution_request,
    execute_batch,
    execute_repository_host_obligations,
    execute_repository_native_obligations,
    validate_manifest,
)

LANGUAGE_ROOT = ROOT.parent / "mncs-language"
COMMONS_ROOT = ROOT.parent / "MNCS-Commons"
MNCS = Path(os.environ.get("MNCS_BINARY", LANGUAGE_ROOT / "target/debug/mncs"))


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


def test_reused_obligation_is_not_executed_and_new_obligation_is_exact() -> None:
    tests = [
        {"id": "case-a", "entry": "a"},
        {"id": "case-b", "entry": "b"},
    ]
    plan = {
        "obligation_plan_id": "plan-1",
        "_selected_obligation_identities": ["obligation-current", "obligation-new"],
        "_reused_obligation_identities": ["obligation-current"],
        "_new_execution_obligation_identities": ["obligation-new"],
        "obligations": [
            {"identity": "obligation-current", "status": "current", "test_case_identities": ["case-a"]},
            {"identity": "obligation-new", "status": "new_execution_required", "test_case_identities": ["case-b"]},
        ],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }

    selected, summary = apply_obligation_plan(tests, plan, native=False)

    assert [item["id"] for item in selected] == ["case-b"]
    assert summary["reused_obligation_identities"] == ["obligation-current"]
    assert summary["new_execution_obligation_identities"] == ["obligation-new"]


def test_unresolved_non_test_obligation_is_reported_without_host_selection() -> None:
    selected, summary = apply_obligation_plan(
        [{"id": "case-a"}],
        {
            "obligation_plan_id": "plan-2",
            "_selected_obligation_identities": ["compile-obligation"],
            "_reused_obligation_identities": [],
            "_new_execution_obligation_identities": ["compile-obligation"],
            "obligations": [
                {"identity": "compile-obligation", "status": "new_execution_required", "test_case_identities": []}
            ],
            "evidence": [],
            "stop": {"sufficient_to_stop": False},
        },
        native=False,
    )

    assert selected == []
    assert summary["selection_unresolved"] == ["compile-obligation"]


def test_native_selection_kernel_matches_the_oracle_projection() -> None:
    tests = [
        {"id": "case-a", "entry": "a"},
        {"id": "case-b", "entry": "b"},
    ]
    plan = {
        "obligation_plan_id": "plan-native",
        "_selected_obligation_identities": ["obligation-current", "obligation-new"],
        "_reused_obligation_identities": ["obligation-current"],
        "_new_execution_obligation_identities": ["obligation-new"],
        "obligations": [
            {"identity": "obligation-current", "status": "current", "test_case_identities": ["case-a"]},
            {"identity": "obligation-new", "status": "new_execution_required", "test_case_identities": ["case-b"]},
        ],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }
    native_selected, native_summary = apply_obligation_plan(
        tests,
        plan,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=compiler_environment(),
    )
    oracle_selected, oracle_summary = apply_obligation_plan(tests, plan, native=False)
    assert [item["id"] for item in native_selected] == [item["id"] for item in oracle_selected]
    assert native_summary["selected_obligation_identities"] == ["obligation-current", "obligation-new"]
    assert native_summary["reused_obligation_identities"] == oracle_summary["reused_obligation_identities"]
    assert native_summary["new_execution_obligation_identities"] == oracle_summary["new_execution_obligation_identities"]


def test_native_selection_keeps_external_repository_obligations_out_of_its_small_bound() -> None:
    external = [
        {
            "identity": f"repo.cargo-target-{index}",
            "status": "new_execution_required",
            "test_case_identities": [],
            "executor": {"kind": "external_integration", "entrypoint": f"cargo-target-{index}"},
        }
        for index in range(24)
    ]
    native = {
        "identity": "repo.native-process-test",
        "status": "new_execution_required",
        "test_case_identities": ["case-native"],
        "executor": {"kind": "native_first_class_test"},
    }
    plan = {
        "obligation_plan_id": "mixed-repository-plan",
        "repository": {"selected_obligation_identities": [item["identity"] for item in [*external, native]]},
        "obligations": [*external, native],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }
    selected, summary = apply_obligation_plan(
        [{"id": "case-native", "entry": "native_test"}],
        plan,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=compiler_environment(),
    )
    assert [test["id"] for test in selected] == ["case-native"]
    assert summary["selected_obligation_identities"] == [item["identity"] for item in [*external, native]]
    assert len(summary["new_execution_obligation_identities"]) == 25


def test_repository_manifest_grant_is_exactly_selected_and_transported(tmp_path: Path) -> None:
    identity = "mncs:0.2:test-case:process::cancel"
    other_identity = "mncs:0.2:test-case:process::observe"
    grant = {"capability": "process_capability", "locator": "/usr/bin/sleep", "bytes": []}
    source = tmp_path / "process.mncs"
    source.write_text("module fixture.process;\n", encoding="utf-8")
    manifest = validate_manifest(
        {
            "schema_version": "mncs.test-manifest/1",
            "name": "fixture-process",
            "source": str(source),
            "module": "fixture.process",
            "host_grant_sets": [{"test_case_identity": identity, "grants": [grant]}],
        },
        tmp_path / "mncs-test.toml",
    )
    selected = [
        {"id": identity, "entry": "cancel", "kind": "unit", "tags": []},
        {"id": other_identity, "entry": "observe", "kind": "unit", "tags": []},
    ]
    _apply_host_grants(selected, manifest, None)
    assert selected[0]["host_grants"] == [grant]
    assert "host_grants" not in selected[1]
    focused = [{"id": identity, "entry": "cancel", "kind": "unit", "tags": []}]
    _apply_host_grants(focused, manifest, {"scope": "changed_item"})
    assert focused[0]["host_grants"] == [grant]
    assert canonical_execution_request(
        module="fixture.process", function="cancel", arguments=[], step_budget=10,
        host_grants=selected[0]["host_grants"],
    )["host_grants"] == [grant]
    assert embed_execution_request(
        module="fixture.process", function="cancel", arguments=[], step_budget=10,
        host_grants=selected[0]["host_grants"],
    )["grants"] == [grant]

    class Session:
        def __init__(self):
            self.timings = []

        def call_batch(self, requests):
            self.requests = requests
            return [{"status": "unsupported"} for _ in requests]

    session = Session()
    execute_batch(
        tests=[selected[0]],
        source_path=source,
        source_text=source.read_text(encoding="utf-8"),
        module="fixture.process",
        session=session,
        default_step_budget=10,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        transport={},
    )
    assert session.requests[0]["grants"] == [grant]


def test_cargo_test_target_expansion_emits_stable_exact_commands() -> None:
    metadata = {
        "workspace_root": "/workspace",
        "workspace_members": ["path+file:///workspace#fixture@0.1.0"],
        "packages": [{
            "id": "path+file:///workspace#fixture@0.1.0",
            "name": "fixture",
            "targets": [
                {"name": "fixture", "kind": ["lib"], "src_path": "/workspace/src/lib.rs", "test": True, "doctest": True},
                {"name": "parser", "kind": ["test"], "src_path": "/workspace/tests/parser.rs", "test": True, "doctest": False},
                {"name": "fixture-tool", "kind": ["bin"], "src_path": "/workspace/src/bin/fixture-tool.rs", "test": True, "doctest": False},
            ],
        }],
    }
    targets = _cargo_test_target_declarations(
        metadata, "fixture", "fixture-tests", ["cargo", "test", "--package", "fixture"]
    )
    assert [target["target_identity"] for target in targets] == [
        "fixture:bin:fixture-tool",
        "fixture:doc:fixture",
        "fixture:lib:fixture",
        "fixture:test:parser",
    ]
    assert [target["argv"][4:] for target in targets] == [
        ["--bin", "fixture-tool"],
        ["--doc"],
        ["--lib"],
        ["--test", "parser"],
    ]


def test_external_obligation_is_not_reported_as_missing_native_test() -> None:
    plan = {
        "obligation_plan_id": "external-plan",
        "_selected_obligation_identities": ["repo.package-test"],
        "_reused_obligation_identities": [],
        "_new_execution_obligation_identities": ["repo.package-test"],
        "obligations": [
            {
                "identity": "repo.package-test",
                "status": "new_execution_required",
                "test_case_identities": [],
                "executor": {"kind": "external_integration", "entrypoint": "cargo test -p package"},
            }
        ],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }
    selected, summary = apply_obligation_plan(
        [{"id": "case-a"}],
        plan,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=compiler_environment(),
    )
    assert selected == []
    assert summary["selection_unresolved"] == []
    assert summary["new_execution_obligation_identities"] == ["repo.package-test"]


def test_external_failure_keeps_obligation_and_executor_identity(tmp_path: Path, monkeypatch) -> None:
    import mncs_test

    identity = "mncs-language.project-test.package"
    obligation = {
        "identity": identity,
        "scope": "repository_canonical",
        "status": "new_execution_required",
        "subject_identity": "repo:package",
        "subject_fingerprint": "1" * 64,
        "definition_identity": "2" * 64,
        "executor_identity": "3" * 64,
        "verifier_identity": "4" * 64,
        "invalidation_identity": "5" * 64,
        "test_case_identities": [],
        "executor": {
            "provider": "mncs-test",
            "kind": "external_integration",
            "entrypoint": "project-test:package",
            "argv": ["cargo", "test", "--package", "package"],
            "working_directory": ".",
            "timeout_seconds": 60,
        },
    }
    plan = {
        "repository": {"identity": "mncs-language", "revision": "rev", "fingerprint": "6" * 64},
        "obligations": [obligation],
        "evidence": [],
    }
    context = {
        "root": tmp_path,
        "tests_by_identity": {identity: {"test": "package"}},
    }
    monkeypatch.setattr(
        mncs_test,
        "run_process",
        lambda *args, **kwargs: {
            "timed_out": False,
            "returncode": 17,
            "stdout_artifact": "stdout/package.out",
            "stderr_artifact": "stderr/package.err",
            "command_artifact": "commands/package.json",
            "timing": {"wall_time_ms": 12.5},
        },
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    evidence, _, results = execute_repository_host_obligations(
        plan, context, environment={}, artifacts=artifacts
    )
    assert evidence[0]["status"] == "FAIL"
    assert results[0]["obligation_identity"] == identity
    assert results[0]["executor_identity"] == "3" * 64
    assert results[0]["status"] == "FAIL"


def test_external_timeout_is_unknown_and_keeps_obligation_executor_identity(
    tmp_path: Path, monkeypatch
) -> None:
    import mncs_test

    identity = "mncs-language.project-test.package"
    obligation = {
        "identity": identity,
        "scope": "repository_canonical",
        "status": "new_execution_required",
        "subject_identity": "repo:package",
        "subject_fingerprint": "1" * 64,
        "definition_identity": "2" * 64,
        "executor_identity": "3" * 64,
        "verifier_identity": "4" * 64,
        "invalidation_identity": "5" * 64,
        "test_case_identities": [],
        "executor": {
            "provider": "mncs-test",
            "kind": "external_integration",
            "entrypoint": "project-test:package",
            "argv": ["cargo", "test", "--package", "package"],
            "working_directory": ".",
            "timeout_seconds": 60,
        },
    }
    plan = {
        "repository": {"identity": "mncs-language", "revision": "rev", "fingerprint": "6" * 64},
        "obligations": [obligation],
        "evidence": [],
    }
    context = {
        "root": tmp_path,
        "tests_by_identity": {identity: {"test": "package"}},
    }
    monkeypatch.setattr(
        mncs_test,
        "run_process",
        lambda *args, **kwargs: {
            "timed_out": True,
            "returncode": None,
            "stdout_artifact": "stdout/package.out",
            "stderr_artifact": "stderr/package.err",
            "command_artifact": "commands/package.json",
            "timing": {"wall_time_ms": 60_000.0},
        },
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    evidence, _, results = execute_repository_host_obligations(
        plan, context, environment={}, artifacts=artifacts
    )
    assert evidence[0]["status"] == "UNKNOWN"
    assert "timed out after 60s" in evidence[0]["reason"]
    assert results[0]["obligation_identity"] == identity
    assert results[0]["executor_identity"] == "3" * 64
    assert results[0]["status"] == "UNKNOWN"


def test_native_failure_keeps_exact_compiler_test_identity(tmp_path: Path) -> None:
    identity = "mncs-language.process-lifecycle.effect-contract"
    test_identity = "mncs:0.2:test-case:process::cancel"
    plan = {
        "repository": {"identity": "mncs-language", "revision": "rev", "fingerprint": "6" * 64},
        "obligations": [
            {
                "identity": identity,
                "scope": "repository_canonical",
                "status": "new_execution_required",
                "subject_identity": "mncs:process",
                "subject_fingerprint": "1" * 64,
                "definition_identity": "2" * 64,
                "executor_identity": "3" * 64,
                "verifier_identity": "4" * 64,
                "invalidation_identity": "5" * 64,
                "test_case_identities": [test_identity],
                "executor": {"kind": "native_first_class_test"},
            }
        ],
        "evidence": [],
    }
    test_result = {"id": test_identity, "verdict": "FAIL", "failure": {"message": "assertion failed"}}
    evidence, results = execute_repository_native_obligations(
        plan, {"root": tmp_path}, {}, [test_result], []
    )
    assert evidence[0]["status"] == "FAIL"
    assert results[0]["obligation_identity"] == identity
    assert results[0]["executor_identity"] == "3" * 64
    assert results[0]["test_case_identities"] == [test_identity]
