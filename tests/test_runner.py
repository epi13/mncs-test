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
                "compiler_inventory_native_observation_projection",
            )
            self.assertEqual(document["execution"]["mode"], "retained-embed-batch")
            self.assertEqual(document["test_inventory"]["test_count"], 6)
            self.assertEqual(len(document["experiment"]["definition"]["test_cases"]), 6)
            self.assertEqual(len(document["experiment"]["observations"]), 6)
            self.assertTrue(
                all("first-class" in item["tags"] for item in document["tests"])
            )
            self.assertTrue(all(item["location"]["authority"] == "mncs-compiler-test-inventory" for item in document["tests"]))
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
            self.assertEqual([item["entry"] for item in document["tests"]], ["arithmetic"])
            self.assertEqual(len(document["experiment"]["observations"]), 1)

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
