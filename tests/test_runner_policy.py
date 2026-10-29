from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LANGUAGE = Path(os.environ.get("MNCS_LANGUAGE_REPO", REPO.parent / "mncs-language"))
STDLIB = Path(os.environ.get("MNCS_STDLIB_ROOT", REPO.parent / "mncs-stdlib"))
COMMONS = Path(os.environ.get("MNCS_COMMONS_REPO", REPO.parent / "MNCS-Commons"))
MNCS = os.environ.get("MNCS", str(LANGUAGE / "target" / "release" / "mncs"))

sys.path.insert(0, str(REPO / "tools"))
from mncs_test import minimal_result  # noqa: E402
from mncs_test_native import run_runner_policy  # noqa: E402


def runner_environment() -> dict[str, str]:
    environment = dict(os.environ)
    roots = [
        REPO / "native",
        STDLIB / "library",
        COMMONS / "src" / "mncs_commons" / "mesh" / "mncs",
    ]
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        str(path.resolve()) for path in roots if path.is_dir()
    )
    return environment


class NativeRunnerPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Path(MNCS).is_file():
            raise unittest.SkipTest(f"selected MNCS toolchain is unavailable: {MNCS}")
        cls.environment = runner_environment()

    def policy(
        self,
        *,
        failure_class: str | None = None,
        classification: str | None = None,
        allow_unsupported: bool = False,
    ) -> tuple[str, int]:
        return run_runner_policy(
            mncs=MNCS,
            cwd=REPO,
            environment=self.environment,
            failure_class=failure_class,
            classification=classification,
            allow_unsupported=allow_unsupported,
        )

    def test_failure_classes_match_the_independent_behavior_table(self) -> None:
        expected = [
            ("assertion", "test_failure", 1),
            ("expectation_mismatch", "test_failure", 1),
            ("diagnostic_mismatch", "test_failure", 1),
            ("compile_expectation_mismatch", "test_failure", 1),
            ("runtime_failure", "runtime_failure", 1),
            ("setup", "setup_failure", 1),
            ("compile_failure", "compile_failure", 4),
            ("timeout", "timeout", 5),
            ("unsupported", "unsupported", 6),
            ("malformed_test_declaration", "infrastructure_failure", 3),
            ("malformed_test_result", "infrastructure_failure", 3),
            ("infrastructure_failure", "infrastructure_failure", 3),
            ("unlisted_failure_class", "test_failure", 1),
        ]
        for failure_class, classification, exit_code in expected:
            with self.subTest(failure_class=failure_class):
                self.assertEqual(
                    self.policy(failure_class=failure_class),
                    (classification, exit_code),
                )

    def test_classifications_match_the_independent_process_exit_table(self) -> None:
        expected = [
            ("success", 0),
            ("test_failure", 1),
            ("runtime_failure", 1),
            ("setup_failure", 1),
            ("invalid_invocation", 2),
            ("infrastructure_failure", 3),
            ("compile_failure", 4),
            ("timeout", 5),
            ("unsupported", 6),
            ("unlisted_classification", 3),
        ]
        for classification, exit_code in expected:
            with self.subTest(classification=classification):
                self.assertEqual(
                    self.policy(classification=classification),
                    (classification, exit_code),
                )

    def test_allow_unsupported_changes_only_the_exit_code(self) -> None:
        self.assertEqual(
            self.policy(classification="unsupported", allow_unsupported=True),
            ("unsupported", 0),
        )

    def test_early_cli_failures_keep_the_bootstrap_exit_boundary(self) -> None:
        invalid = minimal_result(
            classification="invalid_invocation",
            message="invalid manifest",
            cwd=REPO,
        )
        infrastructure = minimal_result(
            classification="infrastructure_failure",
            message="native policy could not start",
            cwd=REPO,
        )
        self.assertEqual(invalid["exit_code"], 2)
        self.assertEqual(infrastructure["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
