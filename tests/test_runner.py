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
LANGUAGE = REPO.parent / "mncs-language"
MNCS = Path(os.environ.get("MNCS", LANGUAGE / "target" / "debug" / "mncs"))
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

    def live_args(self) -> tuple[str, str]:
        return ("--mncs", str(MNCS), "--library", str(LANGUAGE / "library"))

    def test_discovery_is_deterministic(self):
        first = self.invoke("discover")
        second = self.invoke("discover")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        document = json.loads(first.stdout)
        self.assertEqual(document["schema_version"], "mncs.test-discovery/1")
        self.assertEqual([item["name"] for item in document["manifests"]], ["mncs-test-self"])

    def test_validate_manifest(self):
        completed = self.invoke("validate-manifest", "--manifest", "mncs-test.toml")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertTrue(document["valid"])
        self.assertEqual(document["tests"], 6)

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
            self.assertEqual(document["summary"]["authority"], "native_suite")
            self.assertEqual(json.loads(check.read_text(encoding="utf-8"))["verdict"], "PASS")
            self.assertTrue(list((artifacts / "requests").glob("*.json")))
            self.assertTrue(list((artifacts / "stdout").glob("*.out")))

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
