from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "mncs_test.py"
LANGUAGE = Path(os.environ.get("MNCS_LANGUAGE_REPO", REPO.parent / "mncs-language"))
MNCS = Path(os.environ.get("MNCS", LANGUAGE / "target" / "debug" / "mncs"))
EMBED_LIBRARY = Path(
    os.environ.get("MNCS_EMBED_LIBRARY", LANGUAGE / "target" / "debug" / "libmncs_embed.so")
)
LIVE = MNCS.is_file() and os.access(MNCS, os.X_OK)

sys.path.insert(0, str(REPO / "tools"))
import mncs_test  # noqa: E402
from family_contract import verification_plan_contract  # noqa: E402


class RunnerTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )

    def live_args(self) -> tuple[str, ...]:
        arguments = ("--mncs", str(MNCS), "--library", str(LANGUAGE / "library"))
        if EMBED_LIBRARY.is_file():
            arguments += ("--embed-library", str(EMBED_LIBRARY))
        return arguments

    def test_discovery_is_deterministic(self):
        first = self.invoke("discover")
        second = self.invoke("discover")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        document = json.loads(first.stdout)
        self.assertEqual(document["schema_version"], "mncs.test-discovery/1")
        self.assertEqual([item["name"] for item in document["manifests"]], ["mncs-test-self"])
        self.assertEqual(document["manifests"][0]["discovery"], "compiler-inventory")
        self.assertEqual(document["manifests"][0]["tests"], [])

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_compiler_inventory_discovery(self):
        completed = self.invoke(
            "discover",
            "--inventory",
            "--mncs",
            str(MNCS),
            "--library",
            str(LANGUAGE / "library"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        manifest = document["manifests"][0]
        self.assertEqual(manifest["inventory"]["schema_version"], "mncs.test-inventory/1")
        self.assertEqual(
            [item["entry"] for item in manifest["tests"]],
            ["arithmetic", "boolean", "property_replay", "skipped", "snapshot_witness", "task_lifecycle"],
        )
        self.assertTrue(all(item["source_span"]["start"] < item["source_span"]["end"] for item in manifest["tests"]))

    def test_validate_manifest(self):
        completed = self.invoke("validate-manifest", "--manifest", "mncs-test.toml")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertTrue(document["valid"])
        self.assertEqual(document["tests"], 0)

    def test_replay_is_non_executing(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-") as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "mncs.test-result/1",
                        "reproduction": {"command": "mncs-test run --manifest x", "run_id": "r"},
                    }
                ),
                encoding="utf-8",
            )
            completed = self.invoke("replay", "--result", str(result_path), "--format", "text")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("mncs-test run --manifest x", completed.stdout)

    def test_process_timeout_is_classified_at_adapter_boundary(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-timeout-") as directory:
            store = mncs_test.ArtifactStore(Path(directory) / "artifacts")
            process = mncs_test.run_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=REPO,
                environment=dict(os.environ),
                timeout_seconds=1,
                artifacts=store,
                artifact_key="timeout",
            )
            self.assertTrue(process["timed_out"])
            self.assertTrue(process["stderr_artifact"])

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_self_suite(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-self-") as directory:
            result = Path(directory) / "result.json"
            check = Path(directory) / "check.json"
            artifacts = Path(directory) / "artifacts"
            completed = self.invoke(
                "run",
                "--manifest",
                "mncs-test.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(check),
                "--artifacts",
                str(artifacts),
                "--format",
                "text",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], "mncs.test-result/1")
            self.assertEqual(document["verdict"], "PASS")
            self.assertEqual(document["summary"]["total"], 6)
            self.assertEqual(document["summary"]["passed"], 5)
            self.assertEqual(document["summary"]["skipped"], 1)
            self.assertEqual(
                document["summary"]["authority"],
                "native_suite",
            )
            self.assertEqual(document["execution"]["mode"], "retained-embed-batch")
            self.assertEqual(
                document["execution"]["native_aggregation"]["authority"],
                "mncs.test.suite.v1",
            )
            self.assertEqual(document["execution"]["native_aggregation"]["calls"], 7)
            self.assertEqual(document["native_suite_summary"]["total"], 6)
            self.assertEqual(document["test_inventory"]["test_count"], 6)
            self.assertEqual(len(document["experiment"]["definition"]["test_cases"]), 6)
            self.assertEqual(len(document["experiment"]["observations"]), 6)
            self.assertTrue(
                all("first-class" in item["tags"] for item in document["tests"])
            )
            self.assertTrue(all(item["location"]["authority"] == "mncs-compiler-test-inventory" for item in document["tests"]))
            self.assertTrue(
                all(
                    item["request"]["schema_version"] == "0.1"
                    and item["request"]["target"]["module"]
                    and item["request"]["target"]["function"]
                    and item["request_artifact_ref"]["sha256"]
                    for item in document["tests"]
                )
            )
            self.assertEqual(json.loads(check.read_text(encoding="utf-8"))["verdict"], "PASS")
            self.assertTrue(list((artifacts / "requests").glob("*.json")))
            self.assertTrue(list((artifacts / "stdout").glob("*.out")))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_filter_selects_inventory_without_mutating_authority(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-filter-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "mncs-test.toml",
                *self.live_args(),
                "--filter",
                "arithmetic",
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["test_inventory"]["test_count"], 6)
            self.assertEqual(document["execution"]["selected_test_count"], 1)
            self.assertEqual(document["execution"]["batch_size"], 1)
            self.assertEqual(document["summary"]["authority"], "native_suite")
            self.assertEqual([item["entry"] for item in document["tests"]], ["arithmetic"])
            self.assertEqual(len(document["experiment"]["observations"]), 1)
            item = document["tests"][0]
            self.assertEqual(item["request"]["target"]["function"], "arithmetic")
            self.assertEqual(item["transport_request"]["function"], "arithmetic")
            self.assertEqual(item["transport_request"]["args"], item["request"]["arguments"])
            self.assertEqual(
                item["execution_lineage"]["execution_identity"], item["execution_identity"]
            )
            self.assertEqual(
                item["execution_lineage"]["request"]["sha256"],
                item["request_artifact_ref"]["sha256"],
            )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_verification_plan_selects_exact_identity_and_keeps_check_compact(self):
        source = REPO / "tests" / "self_suite.mncs"
        environment = dict(os.environ)
        environment["MNCS_LIBRARY_PATH"] = str(REPO / "native") + os.pathsep + str(LANGUAGE / "library")
        inventory_process = subprocess.run(
            [str(MNCS), "test-inventory", str(source)],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(inventory_process.returncode, 0, inventory_process.stderr)
        inventory_document = json.loads(inventory_process.stdout)
        inventory = inventory_document["inventory"]
        selected = inventory["tests"][0]
        plan = {
            "schema_version": "mncs.verification-plan/1",
            "plan_id": "",
            "source": {
                "path": str(source),
                "sha256": mncs_test.sha256_file(source),
                "subject_identity": inventory["subject_identity"],
                "subject_fingerprint": inventory["subject_fingerprint"],
            },
            "impact": {
                "graph_identity": "a" * 64,
                "roots": [selected["function_identity"]],
                "affected_count": 1,
                "direct_dependents": [],
                "test_identities": [selected["test_case_identity"]],
                "risk_flags": [],
                "complete": True,
                "limitations": ["test fixture impact evidence"],
                "cross_repository": {
                    "graph_identity": "b" * 64,
                    "edges": [],
                    "selected_repositories": [],
                    "complete": True,
                    "limitations": ["test fixture does not exercise family topology"],
                },
            },
            "selection": {
                "level": "changed_item",
                "selected_test_identities": [selected["test_case_identity"]],
                "available_test_count": len(inventory["tests"]),
                "escalation_reasons": [],
                "selected_repositories": [],
                "available_repository_count": 0,
            },
            "proof": {
                "sufficient_to_stop": True,
                "required_evidence": ["selected_test_cases_pass"],
                "boundary": {
                    "claimed_scope": "changed_item",
                    "established": True,
                    "executor": "mncs-test",
                    "stop_condition": "selected_test_cases_pass",
                },
            },
            "provenance": {"provider": "ravel", "policy": "test-fixture", "impact_schema": "mncs.semantic-impact/1"},
        }
        plan["plan_id"] = verification_plan_contract().plan_identity(plan)
        with tempfile.TemporaryDirectory(prefix="mncs-test-plan-") as directory:
            plan_path = Path(directory) / "plan.json"
            result_path = Path(directory) / "result.json"
            check_path = Path(directory) / "check.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            completed = self.invoke(
                "run",
                "--manifest",
                "mncs-test.toml",
                *self.live_args(),
                "--verification-plan",
                str(plan_path),
                "--result",
                str(result_path),
                "--check-result",
                str(check_path),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            check = json.loads(check_path.read_text(encoding="utf-8"))
            self.assertEqual(result["selection"]["level"], "changed_item")
            self.assertEqual(result["selection"]["selected_count"], 1)
            self.assertEqual(result["summary"]["total"], 1)
            self.assertNotIn("test_result", check)
            self.assertEqual(check["selection"]["plan_id"], plan["plan_id"])

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_failing_assertion_is_not_hidden(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-fail-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/failing.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 1, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "FAIL")
            self.assertEqual(document["classification"], "test_failure")
            self.assertEqual(document["failure_class"], "assertion")
            self.assertEqual(document["tests"][0]["native_result"]["expected"], 7)
            self.assertEqual(document["tests"][0]["native_result"]["actual"], 6)

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_first_class_failing_assertion_uses_native_suite_fold(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-first-class-fail-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/first-class-failing.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 1, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "FAIL")
            self.assertEqual(document["summary"]["authority"], "native_suite")
            self.assertEqual(document["native_suite_summary"]["failed"], 1)
            self.assertEqual(document["native_suite_summary"]["assertion_failures"], 1)
            self.assertEqual(
                document["execution"]["native_aggregation"]["status"],
                "returned",
            )
            item = document["tests"][0]
            self.assertEqual(item["request"]["target"]["function"], "failing_assertion")
            self.assertEqual(item["request"]["target"]["module"], "tests.first_class_failing")
            self.assertEqual(item["execution_lineage"]["test_case_identity"], item["semantic"]["test_case_identity"])

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_expected_compile_failure(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-compile-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/compile-fail.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "PASS")
            self.assertEqual(document["tests"][0]["status"], "expected_failure")
            self.assertTrue(document["tests"][0]["diagnostics"])

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_compile_pass(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-compile-pass-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/compile-pass.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "PASS")
            self.assertEqual(document["tests"][0]["status"], "passed")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_expected_runtime_failure(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-runtime-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/runtime-failure.toml",
                *self.live_args(),
                "--result",
                str(result),
                "--check-result",
                str(Path(directory) / "check.json"),
                "--artifacts",
                str(Path(directory) / "artifacts"),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "PASS")
            self.assertEqual(document["tests"][0]["status"], "expected_failure")
            self.assertEqual(document["tests"][0]["execution_status"], "runtime_failure")

    def test_unsupported_is_explicit_unknown(self):
        completed = self.invoke(
            "run",
            "--manifest",
            "tests/fixtures/unsupported.toml",
            "--result",
            "/tmp/mncs-test-unsupported-unittest-result.json",
            "--check-result",
            "/tmp/mncs-test-unsupported-unittest-check.json",
            "--artifacts",
            "/tmp/mncs-test-unsupported-unittest-artifacts",
            "--library",
            str(LANGUAGE / "library"),
        )
        self.assertEqual(completed.returncode, 6, completed.stdout)
        document = json.loads(Path("/tmp/mncs-test-unsupported-unittest-result.json").read_text(encoding="utf-8"))
        self.assertEqual(document["verdict"], "UNKNOWN")
        self.assertEqual(document["classification"], "unsupported")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_modules_validate(self):
        environment = dict(os.environ)
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(REPO / "native"), str(LANGUAGE / "library")))
        for source in sorted((REPO / "native" / "mncs" / "test").rglob("*.mncs")):
            completed = subprocess.run(
                [str(MNCS), "validate", str(source)],
                cwd=REPO,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, f"{source}: {completed.stdout}\n{completed.stderr}")


if __name__ == "__main__":
    unittest.main()
