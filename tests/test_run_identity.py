from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

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


def _native_environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        (str(stdlib), str(ROOT / "native"))
    )
    return environment


def _run_inputs() -> dict[str, object]:
    return {
        "provenance": {
            "compiler": {
                "requested": "mncs --research-mode",
                "path": "/opt/MNCS Compiler/bin/mncs",
                "sha256": None,
                "version": "MNCS 0.18 — Lúna 東京",
            },
            "manifest": {
                "path": "/work/MNCS tests/測試.toml",
                "sha256": "a" * 64,
            },
            "source": {
                "path": "/work/MNCS tests/cases/東京.mncs",
                "sha256": "b" * 64,
            },
        },
        "profile": "0.18",
        "test_results": [
            {"id": 'line\n"quoted" and slash\\\\'},
            {"id": "Lúna 東京\tcase"},
        ],
        "test_inventory": {
            "subject_identity": "mncs:test:subject:case-1",
            "subject_fingerprint": None,
        },
        "verification_plan": None,
        "obligation_plan": {"obligation_plan_id": "mncs:test:obligation:plan-7"},
        "execution": {
            "mode": "native-batch",
            "artifact_identity": None,
            "artifact_sha256": "c" * 64,
            "batch_size": None,
        },
    }


def _reference_run_identity(inputs: dict[str, object]) -> str:
    provenance = inputs["provenance"]
    test_inventory = inputs["test_inventory"] or {}
    verification_plan = inputs["verification_plan"] or {}
    obligation_plan = inputs["obligation_plan"] or {}
    execution = inputs["execution"] or {}
    material = {
        "manifest": provenance["manifest"],
        "source": provenance["source"],
        "compiler": provenance["compiler"],
        "profile": inputs["profile"],
        "tests": [item["id"] for item in inputs["test_results"]],
        "subject_identity": test_inventory.get("subject_identity"),
        "subject_fingerprint": test_inventory.get("subject_fingerprint"),
        "verification_plan": verification_plan.get("plan_id"),
        "obligation_plan": obligation_plan.get("obligation_plan_id"),
        "execution": {
            key: execution.get(key)
            for key in ("mode", "artifact_identity", "artifact_sha256", "batch_size")
        },
    }
    payload = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _call_native_run_identity(
    inputs: dict[str, object], tmp_path: Path
) -> str:
    environment = _native_environment()
    environment["MNCS_NATIVE_APPLICATION_CACHE_DIR"] = str(tmp_path / "toolchain-cache")
    return mncs_test.build_run_identity_sha256(
        **inputs,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=environment,
    )


def test_native_run_identity_matches_independent_compact_json(tmp_path: Path) -> None:
    inputs = _run_inputs()
    actual = _call_native_run_identity(inputs, tmp_path)

    assert actual == _reference_run_identity(inputs)


def test_make_result_uses_native_run_identity(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mncs"
    manifest_path = tmp_path / "manifest.toml"
    source_path.write_text("mncs 0.18;\nmodule tests.identity_fixture;\n", encoding="utf-8")
    manifest_path.write_text("schema_version = 'fixture'\n", encoding="utf-8")
    environment = _native_environment()
    environment["MNCS_NATIVE_APPLICATION_CACHE_DIR"] = str(tmp_path / "toolchain-cache")
    suite_summary = {
        "verdict": "PASS",
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "unsupported": 0,
        "assertion_failures": 0,
        "setup_failures": 0,
        "compile_failures": 0,
        "runtime_failures": 0,
        "timeout_failures": 0,
        "infrastructure_failures": 0,
        "assertion_count": 0,
        "failure_count": 0,
    }
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        result = mncs_test.make_result(
            manifest={
                "name": "run-identity-fixture",
                "manifest_path": str(manifest_path),
                "source_path": str(source_path),
                "profile": "0.18",
                "module": "tests.identity_fixture",
                "tests": [],
                "step_budget": 1000,
                "timeout_seconds": 1,
            },
            source_text=source_path.read_text(encoding="utf-8"),
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=environment,
            library_paths=[],
            artifacts=mncs_test.ArtifactStore(tmp_path / "artifacts"),
            test_results=[],
            suite_result=None,
            suite_summary=suite_summary,
            test_inventory=None,
            execution=None,
            classification="success",
            failure_class="",
            message=None,
            failure_details=None,
            allow_unsupported=False,
        )

    assert result["run_id"] == _reference_run_identity(
        {
            "provenance": result["provenance"],
            "profile": "0.18",
            "test_results": [],
            "test_inventory": None,
            "verification_plan": None,
            "obligation_plan": None,
            "execution": None,
        }
    )
    assert "test-run-identity.json" in [
        call.args[0] for call in native_app.call_args_list
    ]


def test_over_bound_run_identity_uses_exact_compatibility_payload(
    tmp_path: Path,
) -> None:
    inputs = _run_inputs()
    inputs["profile"] = "x" * (mncs_test.TEST_RUN_IDENTITY_MAX_TEXT_BYTES + 1)
    expected = _reference_run_identity(inputs)
    with mock.patch.object(
        mncs_test, "result_identity_sha256", return_value=expected
    ) as fallback, mock.patch.object(mncs_test, "run_native_app") as native_app:
        actual = _call_native_run_identity(inputs, tmp_path)

    assert actual == expected
    native_app.assert_not_called()
    payload = fallback.call_args.args[0]
    expected_material = {
        "manifest": inputs["provenance"]["manifest"],
        "source": inputs["provenance"]["source"],
        "compiler": inputs["provenance"]["compiler"],
        "profile": inputs["profile"],
        "tests": [item["id"] for item in inputs["test_results"]],
        "subject_identity": inputs["test_inventory"]["subject_identity"],
        "subject_fingerprint": None,
        "verification_plan": None,
        "obligation_plan": inputs["obligation_plan"]["obligation_plan_id"],
        "execution": {
            key: inputs["execution"].get(key)
            for key in ("mode", "artifact_identity", "artifact_sha256", "batch_size")
        },
    }
    assert payload == json.dumps(
        expected_material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
