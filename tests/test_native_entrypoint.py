"""Regression guards for the native-canonical mncs-test entrypoint."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeEntrypointTests(unittest.TestCase):
    def test_status_declares_native_canonical(self) -> None:
        status = json.loads((ROOT / "native-userland-status.json").read_text())
        self.assertEqual(status["status"], "native_canonical")
        self.assertEqual(status["canonical_entrypoint"], "mncs test")
        self.assertFalse(status["host_fallback"]["allowed"])
        self.assertEqual(status["bootstrap_boundary"]["python_runner_calls"], 0)

    def test_canonical_launcher_contains_no_python_fallback(self) -> None:
        launcher = (ROOT / "bin/mncs-test").read_text()
        self.assertNotIn("python3", launcher)
        self.assertIn('"$MNCS_BIN" test', launcher)
        self.assertTrue((ROOT / "bin/mncs-test-compat").exists())

    def test_missing_bootstrap_fails_closed(self) -> None:
        environment = dict(os.environ)
        environment["MNCS"] = "/definitely/missing/mncs"
        completed = subprocess.run(
            [str(ROOT / "bin/mncs-test"), str(ROOT / "tests/self_suite.mncs")],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("trusted bootstrap executable", completed.stderr)
        self.assertNotIn("python", completed.stderr.lower())


if __name__ == "__main__":
    unittest.main()
