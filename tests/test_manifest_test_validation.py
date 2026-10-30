from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = next(
    (candidate for candidate in (REPO.parent, *REPO.parents)
     if (candidate / "mncs-language").is_dir()),
    REPO.parent,
)
MNCS = Path(os.environ.get(
    "MNCS", WORKSPACE_ROOT / "mncs-language" / "target" / "release" / "mncs"
))
STDLIB_ROOT = Path(os.environ.get("MNCS_STDLIB_ROOT", WORKSPACE_ROOT / "mncs-stdlib"))
sys.path.insert(0, str(REPO / "tools"))
import mncs_test  # noqa: E402


LIVE = MNCS.is_file() and os.access(MNCS, os.X_OK)


def native_environment() -> dict[str, str]:
    environment = dict(os.environ)
    roots = [str(STDLIB_ROOT / "library"), str(REPO / "native")]
    inherited = environment.get("MNCS_LIBRARY_PATH", "").split(os.pathsep)
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        dict.fromkeys(path for path in [*roots, *inherited] if path)
    )
    return environment


def manifest_document(
    source: Path, tests: list[object], host_grant_sets: list[object] | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "mncs.test-manifest/1",
        "name": "native-manifest-validation",
        "source": str(source),
        "module": "fixture.native_manifest_validation",
        "tests": tests,
    }
    if host_grant_sets is not None:
        document["host_grant_sets"] = host_grant_sets
    return document


class NativeManifestTestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="mncs-manifest-native-")
        self.root = Path(self.directory.name)
        self.source = self.root / "source.mncs"
        self.source.write_text("module fixture.native_manifest_validation;\n", encoding="utf-8")
        self.path = self.root / "mncs-test.toml"
        self.environment = native_environment()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def native_validate(self, tests: list[object]) -> dict[str, object]:
        return mncs_test.validate_manifest(
            manifest_document(self.source, tests), self.path,
            mncs=str(MNCS), cwd=REPO, environment=self.environment,
        )

    def native_validate_document(self, document: dict[str, object]) -> dict[str, object]:
        return mncs_test.validate_manifest(
            document, self.path, mncs=str(MNCS), cwd=REPO,
            environment=self.environment,
        )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_policy_accepts_and_normalizes_supported_rows(self) -> None:
        declarations = [
            {
                "id": "case-a", "entry": "arithmetic", "kind": "unit",
                "tags": ["native", "fast"], "arguments": [],
                "step_budget": 0, "timeout_seconds": 15, "seed": 7,
                "case_index": 0, "expected_status": "returned",
                "diagnostic_codes": ["MNE", "A legacy suffix remains a prefix"],
            },
            {"id": "case-b", "entry": "compile_error", "kind": "compile-fail",
             "diagnostic_codes": ["MNP"]},
        ]
        host = mncs_test.validate_manifest(manifest_document(self.source, declarations), self.path)
        with mock.patch.object(mncs_test, "run_native_app", wraps=mncs_test.run_native_app) as native_app:
            native = self.native_validate(declarations)
        self.assertEqual(native["tests"], host["tests"])
        self.assertTrue(any(call.args[0] == "test-manifest-test-validation.json" for call in native_app.call_args_list))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_first_errors_match_independent_host_oracle(self) -> None:
        base = {"id": "case", "entry": "run", "kind": "unit"}
        cases = [
            ([base, dict(base)], "tests[1].id must be unique and non-empty"),
            ([{**base, "id": "   "}], "tests[0].id must be unique and non-empty"),
            ([{**base, "kind": "unknown"}], "tests[0].kind is unsupported: 'unknown'"),
            ([{**base, "tags": ["ok", 2]}], "tests[0].tags must be an array of strings"),
            ([{**base, "arguments": "not-an-array"}], "tests[0].arguments must be an array"),
            ([{**base, "step_budget": True}], "tests[0].step_budget must be a non-negative integer"),
            ([{**base, "case_index": -1}], "tests[0].case_index must be a non-negative integer"),
            ([{**base, "expected_status": "not-a-status"}], "tests[0].expected_status is invalid: 'not-a-status'"),
            ([{**base, "diagnostic_codes": ["lower"]}], "tests[0].diagnostic_codes must contain diagnostic code prefixes"),
        ]
        for declarations, message in cases:
            with self.subTest(message=message):
                document = manifest_document(self.source, declarations)
                with self.assertRaises(mncs_test.ManifestError) as host_error:
                    mncs_test.validate_manifest(document, self.path)
                with self.assertRaises(mncs_test.ManifestError) as native_error:
                    self.native_validate(declarations)
                self.assertEqual(str(host_error.exception), message)
                self.assertEqual(str(native_error.exception), str(host_error.exception))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_precedence_matches_host_when_multiple_fields_are_invalid(self) -> None:
        declaration = {
            "id": "case", "entry": "run", "kind": "unknown",
            "tags": [""], "arguments": "bad", "step_budget": -1,
        }
        document = manifest_document(self.source, [declaration])
        with self.assertRaises(mncs_test.ManifestError) as host_error:
            mncs_test.validate_manifest(document, self.path)
        with self.assertRaises(mncs_test.ManifestError) as native_error:
            self.native_validate([declaration])
        self.assertEqual(str(host_error.exception), "tests[0].kind is unsupported: 'unknown'")
        self.assertEqual(str(native_error.exception), str(host_error.exception))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_over_bound_manifest_uses_compatibility_validator(self) -> None:
        declarations = [
            {"id": f"case-{index}", "entry": "run"}
            for index in range(mncs_test.MANIFEST_TEST_VALIDATION_MAX_TESTS + 1)
        ]
        with mock.patch.object(mncs_test, "run_native_app") as native_app:
            native = self.native_validate(declarations)
        self.assertEqual(len(native["tests"]), len(declarations))
        native_app.assert_not_called()

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_run_command_uses_native_manifest_policy_before_execution(self) -> None:
        source = self.root / "source.mncs"
        source.write_text("module fixture.native_manifest_validation;\n", encoding="utf-8")
        manifest = self.root / "mncs-test.toml"
        manifest.write_text(
            "schema_version = 'mncs.test-manifest/1'\n"
            "name = 'invalid-native-declaration'\n"
            "source = 'source.mncs'\n"
            "module = 'fixture.native_manifest_validation'\n"
            "[[tests]]\n"
            "id = 'case'\n"
            "entry = 'run'\n"
            "kind = 'invalid-kind'\n",
            encoding="utf-8",
        )
        arguments = mncs_test.parser().parse_args([
            "run", "--manifest", str(manifest), "--mncs", str(MNCS),
            "--result", str(self.root / "result.json"),
            "--artifacts", str(self.root / "artifacts"), "--format", "json",
        ])
        output = StringIO()
        with (
            mock.patch.dict(os.environ, self.environment),
            mock.patch.object(mncs_test, "run_native_app", wraps=mncs_test.run_native_app) as native_app,
            redirect_stdout(output),
        ):
            exit_code = mncs_test.run_manifest(arguments)
        self.assertEqual(exit_code, mncs_test.EXIT_INVALID_INVOCATION)
        self.assertIn("tests[0].kind is unsupported: 'invalid-kind'", output.getvalue())
        self.assertTrue(any(call.args[0] == "test-manifest-test-validation.json" for call in native_app.call_args_list))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_host_grant_policy_accepts_and_preserves_valid_grants(self) -> None:
        grants = [{
            "test_case_identity": "mncs:0.2:test-case:process::cancel",
            "grants": [{
                "capability": "process_capability", "locator": "/usr/bin/sleep",
                "bytes": [0, 1, 127, 255] + [255] * 1020,
            }],
        }]
        document = manifest_document(self.source, [], grants)
        host = mncs_test.validate_manifest(document, self.path)
        with mock.patch.object(mncs_test, "run_native_app", wraps=mncs_test.run_native_app) as native_app:
            native = self.native_validate_document(document)
        self.assertEqual(native["host_grant_sets"], host["host_grant_sets"])
        self.assertTrue(any(call.args[0] == "test-manifest-host-grant-validation.json" for call in native_app.call_args_list))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_host_grant_first_errors_match_host_oracle(self) -> None:
        valid_set = {
            "test_case_identity": "case",
            "grants": [{"capability": "process_capability", "locator": "/usr/bin/sleep", "bytes": []}],
        }
        cases = [
            ([{"test_case_identity": "case", "grants": [], "extra": True}],
             "host_grant_sets[0] must contain only test_case_identity and grants"),
            ([valid_set, {**valid_set}],
             "host_grant_sets[1].test_case_identity must be unique and non-empty"),
            ([{"test_case_identity": "case", "grants": []}],
             "host_grant_sets[0].grants must be a non-empty array"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "extra": 1}]}],
             "host_grant_sets[0].grants[0] is malformed"),
            ([{"test_case_identity": "case", "grants": [{"locator": "x"}]}],
             "host_grant_sets[0].grants[0] needs capability and locator strings"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "bytes": [True]}]}],
             "host_grant_sets[0].grants[0].bytes must be a bounded byte array"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "bytes": [-1]}]}],
             "host_grant_sets[0].grants[0].bytes must be a bounded byte array"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "bytes": [256]}]}],
             "host_grant_sets[0].grants[0].bytes must be a bounded byte array"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "bytes": [1.5]}]}],
             "host_grant_sets[0].grants[0].bytes must be a bounded byte array"),
            ([{"test_case_identity": "case", "grants": [{"capability": "x", "bytes": [0] * 4097}]}],
             "host_grant_sets[0].grants[0].bytes must be a bounded byte array"),
            ([{"test_case_identity": "case", "grants": [
                {"capability": "x", "locator": "y"},
                {"capability": "x", "locator": "y"},
            ]}], "host_grant_sets[0] contains a duplicate capability and locator"),
        ]
        for grants, message in cases:
            with self.subTest(message=message):
                document = manifest_document(self.source, [], grants)
                with self.assertRaises(mncs_test.ManifestError) as host_error:
                    mncs_test.validate_manifest(document, self.path)
                with self.assertRaises(mncs_test.ManifestError) as native_error:
                    self.native_validate_document(document)
                self.assertEqual(str(host_error.exception), message)
                self.assertEqual(str(native_error.exception), str(host_error.exception))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_over_bound_host_grants_use_compatibility_validator(self) -> None:
        grants = [
            {"test_case_identity": f"case-{index}", "grants": [{"capability": "x"}]}
            for index in range(mncs_test.MANIFEST_HOST_GRANT_VALIDATION_MAX_SETS + 1)
        ]
        document = manifest_document(self.source, [], grants)
        with mock.patch.object(mncs_test, "run_native_app") as native_app:
            native = self.native_validate_document(document)
        self.assertEqual(len(native["host_grant_sets"]), len(grants))
        self.assertFalse(any(
            call.args[0] == "test-manifest-host-grant-validation.json"
            for call in native_app.call_args_list
        ))

        byte_values = [{
            "test_case_identity": "case",
            "grants": [
                {"capability": "x", "bytes": [0] * 1024},
                {"capability": "y", "bytes": [1]},
            ],
        }]
        document = manifest_document(self.source, [], byte_values)
        with mock.patch.object(mncs_test, "run_native_app") as native_app:
            native = self.native_validate_document(document)
        self.assertEqual(native["host_grant_sets"], mncs_test._validate_host_grant_sets_host(byte_values))
        self.assertFalse(any(
            call.args[0] == "test-manifest-host-grant-validation.json"
            for call in native_app.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
