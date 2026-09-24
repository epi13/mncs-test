from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_ROOT = Path(os.environ.get("MNCS_LANGUAGE_REPO", ROOT.parent / "mncs-language"))
COMMONS_ROOT = Path(os.environ.get("MNCS_COMMONS_REPO", ROOT.parent / "MNCS-Commons"))
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


def test_inventory_generator_check_is_authoritative() -> None:
    completed = subprocess.run(
        [
            "python3",
            str(ROOT / "tools/check_generated_provider_inventory.py"),
            "--mncs",
            str(MNCS),
        ],
        cwd=ROOT,
        env=compiler_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["valid"] is True
    assert report["test_count"] == 8
    assert report["provider_revision_identity"]
    generated = (ROOT / "native/mncs/test/provider_inventory.mncs").read_text(encoding="utf-8")
    assert "record TestCallableEntry" in generated
    assert "fn callable_inventory()" in generated
    assert "fn run_one(" not in generated
    assert "use tests.self_suite" not in generated


def identity_bound_test_executions() -> tuple[list[str], list[dict[str, object]], list[str]]:
    def compiler_inventory(source: str) -> dict[str, object]:
        completed = subprocess.run(
            [str(MNCS), "declaration-inventory", source],
            cwd=ROOT,
            env=compiler_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        document = json.loads(completed.stdout)
        assert document["valid"] is True, document.get("diagnostics")
        return document["inventory"]

    def run_selected(manifest: str, identities: list[str]) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix=".phase6-test-callables-", dir=ROOT) as directory:
            root = Path(directory)
            command = [
                sys.executable,
                str(ROOT / "tools/mncs_test.py"),
                "run",
                "--manifest",
                manifest,
                "--mncs",
                str(MNCS),
                "--result",
                str(root / "result.json"),
                "--check-result",
                str(root / "check.json"),
                "--artifacts",
                str(root / "artifacts"),
                "--format",
                "json",
            ]
            for identity in identities:
                command.extend(("--test-identity", identity))
            command.extend(("--library", str(LANGUAGE_ROOT / "library")))
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=compiler_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
            result = json.loads((root / "result.json").read_text(encoding="utf-8"))
            observations = result["tests"]
            assert all(item["verdict"] == "PASS" for item in observations)
            assert all(item.get("callable_invocation") for item in observations)
            return observations

    self_inventory = compiler_inventory("tests/self_suite.mncs")
    self_selected = [
        item for item in self_inventory["callables"]
        if item.get("callable_kind") == "test" and item.get("name") in {"arithmetic", "boolean"}
    ]
    assert {item["name"] for item in self_selected} == {"arithmetic", "boolean"}
    self_selected.sort(key=lambda item: item["test_case_identity"])
    self_ids = [item["test_case_identity"] for item in self_selected]
    observations = run_selected("mncs-test.toml", self_ids)
    assert [item["semantic"]["test_case_identity"] for item in observations] == self_ids

    other_inventory = compiler_inventory("tests/provider_cross_module.mncs")
    other_selected = [
        item for item in other_inventory["callables"]
        if item.get("callable_kind") == "test"
        and item.get("name") == "separate_module_identity"
    ]
    assert len(other_selected) == 1
    other = other_selected[0]
    assert other["module"] == "tests.provider_cross_module"
    other_manifest = ROOT / "tests/provider_cross_module.toml"
    libraries = [LANGUAGE_ROOT / "library", ROOT / "native", ROOT]
    other_manifest.write_text(
        "\n".join(
            [
                'schema_version = "mncs.test-manifest/1"',
                'name = "mncs-test-cross-module-identity"',
                f"source = {json.dumps(str(ROOT / 'tests/provider_cross_module.mncs'))}",
                'module = "tests.provider_cross_module"',
                'profile = "0.18"',
                "step_budget = 200000",
                "timeout_seconds = 60",
                "libraries = [" + ", ".join(json.dumps(str(path)) for path in libraries) + "]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        other_observations = run_selected(
            str(other_manifest), [other["test_case_identity"]]
        )
    finally:
        other_manifest.unlink(missing_ok=True)
    observations.extend(other_observations)
    observations.sort(key=lambda item: item["semantic"]["test_case_identity"])
    selected_ids = [item["semantic"]["test_case_identity"] for item in observations]
    modules = [item["semantic"]["module"] for item in observations]
    assert len(set(modules)) == 2, modules

    executions = []
    for item in observations:
        semantic = item["semantic"]
        invocation = item["callable_invocation"]
        native = item["native_result"]
        assert invocation["callable_identity"] == semantic["function_identity"]
        assert invocation["declaration_identity"] == semantic["declaration_identity"]
        assert invocation["signature_identity"] == semantic["signature_identity"]
        assert invocation["test_case_identity"] == semantic["test_case_identity"]
        executions.append(
            {
                "test_case_identity": semantic["test_case_identity"],
                "declaration_identity": semantic["declaration_identity"],
                "callable_identity": semantic["function_identity"],
                "signature_identity": semantic["signature_identity"],
                "artifact_identity": invocation["artifact_identity"],
                "execution_status": "RETURNED",
                "native_result": {
                    "verdict": native["verdict"],
                    "verdict_code": native["verdict_code"],
                    "failure_kind": native["failure_kind_name"],
                    "failure_code": native["failure_code"],
                    "assertions": native["assertions"],
                    "failures": native["failures"],
                    "expected": native["expected"],
                    "actual": native["actual"],
                    "assertion_code": native["assertion_code"],
                },
            }
        )
    return selected_ids, executions, modules


def test_native_provider_dispatches_inventory_test_and_separates_check_identity() -> None:
    selected_ids, selected_executions, modules = identity_bound_test_executions()
    assert len(set(modules)) == 2
    provider = json.loads(
        (ROOT / "native-applications/provider-admitted.json").read_text(encoding="utf-8")
    )
    request = {
        "schema_version": "mncs.test-provider-request/1",
        "inventory_identity": provider["inventory_identity"],
        "selected_test_identities": selected_ids,
        "selected_test_executions": selected_executions,
        "selection_count": len(selected_ids),
        "interface_identity": provider["interface_identity"],
        "provider_revision_identity": provider["revision_identity"],
    }
    with tempfile.TemporaryDirectory(prefix=".phase6-provider-", dir=ROOT) as directory:
        artifact_root = Path(directory)
        (artifact_root / "request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        relative = artifact_root.relative_to(ROOT)
        completed = subprocess.run(
            [
                str(MNCS),
                "run-app",
                str(ROOT / "native-applications/test-provider.json"),
                "--grant-structured",
                "provider_artifact",
                "--grant-structured",
                "provider_digest",
                "--",
                str(relative / "request.json"),
                str(relative / "result.json"),
                str(relative / "check.json"),
            ],
            cwd=ROOT,
            env=compiler_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads((artifact_root / "result.json").read_text(encoding="utf-8"))
        check = json.loads((artifact_root / "check.json").read_text(encoding="utf-8"))
    assert result["native_result"]["verdict"] == "PASS", json.dumps(
        result["native_result"], sort_keys=True
    )
    assert result["selected_test_identities"] == request["selected_test_identities"]
    assert result["result_identity"] != check["result_identity"]
    assert check["test_result_identity"] == result["result_identity"]


def test_native_application_refuses_mismatched_interface_descriptor() -> None:
    descriptor = json.loads(
        (ROOT / "native-applications/test-provider.json").read_text(
            encoding="utf-8"
        )
    )
    descriptor["interface_identity"] = "00" * 32
    temporary = tempfile.NamedTemporaryFile(
        prefix=".phase6-provider-descriptor-",
        suffix=".json",
        dir=ROOT / "native-applications",
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    path = Path(temporary.name)
    try:
        temporary.write(json.dumps(descriptor))
        temporary.close()
        completed = subprocess.run(
            [
                str(MNCS),
                "run-app",
                str(path),
                "--grant-structured",
                "provider_artifact",
                "--grant-structured",
                "provider_digest",
                "--",
            ],
            cwd=ROOT,
            env=compiler_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    finally:
        path.unlink()
    assert completed.returncode == 3
    assert "interface" in completed.stderr.lower()
