from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "mncs_test.py"
WORKSPACE_ROOT = next(
    (
        candidate
        for candidate in (REPO.parent, *REPO.parents)
        if (candidate / "mncs-language").is_dir()
    ),
    REPO.parent,
)
LANGUAGE = Path(os.environ.get("MNCS_LANGUAGE_REPO", WORKSPACE_ROOT / "mncs-language"))
COMMONS = Path(os.environ.get("MNCS_COMMONS_REPO", WORKSPACE_ROOT / "MNCS-Commons"))
os.environ.setdefault("MNCS_COMMONS_ROOT", str(COMMONS))
_RELEASE_MNCS = LANGUAGE / "target" / "release" / "mncs"
_DEBUG_MNCS = LANGUAGE / "target" / "debug" / "mncs"
MNCS = Path(os.environ.get(
    "MNCS", _RELEASE_MNCS if _RELEASE_MNCS.is_file() else _DEBUG_MNCS
))
_DEFAULT_EMBED_LIBRARY = MNCS.resolve().parent / "libmncs_embed.so"
if not _DEFAULT_EMBED_LIBRARY.is_file():
    _DEFAULT_EMBED_LIBRARY = LANGUAGE / "target" / "debug" / "libmncs_embed.so"
EMBED_LIBRARY = Path(os.environ.get("MNCS_EMBED_LIBRARY", _DEFAULT_EMBED_LIBRARY))
LIVE = MNCS.is_file() and os.access(MNCS, os.X_OK)


def _stdlib_library() -> Path:
    """Standard-library tree: explicit root, else family sibling, else legacy."""
    explicit = os.environ.get("MNCS_STDLIB_ROOT")
    if explicit:
        return Path(explicit) / "library"
    sibling = WORKSPACE_ROOT / "mncs-stdlib"
    if (sibling / "stdlib-manifest.json").is_file():
        return sibling / "library"
    return LANGUAGE / "library"


STDLIB_LIBRARY = _stdlib_library()

sys.path.insert(0, str(REPO / "tools"))
import mncs_test  # noqa: E402
from family_contract import verification_plan_contract  # noqa: E402


class RunnerTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["MNCS"] = str(MNCS)
        selected_roots = [
            str(STDLIB_LIBRARY),
            str(REPO / "native"),
            str(COMMONS / "src" / "mncs_commons" / "mesh" / "mncs"),
        ]
        inherited_roots = environment.get("MNCS_LIBRARY_PATH", "").split(os.pathsep)
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            dict.fromkeys(root for root in [*selected_roots, *inherited_roots] if root)
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_command_env_preserves_selected_library_after_private_root(self):
        private = REPO / "native"
        selected = REPO.parent / "mncs-language" / "library"
        declared = {"MNCS_LIBRARY_PATH": os.pathsep.join((str(selected), str(private)))}
        environment = mncs_test.command_environment(
            [private], declared
        )
        self.assertEqual(
            environment["MNCS_LIBRARY_PATH"],
            os.pathsep.join((str(private), str(selected))),
        )

    def test_run_parser_uses_environment_selected_reference_executable(self):
        selected = str(LANGUAGE / "target" / "release" / "mncs")
        with mock.patch.dict(os.environ, {"MNCS": selected}):
            arguments = mncs_test.parser().parse_args(
                ["run", "--execution", "canonical-vm"])
        self.assertEqual(arguments.mncs, selected)
        self.assertEqual(
            mncs_test.resolve_run_mncs(
                arguments.mncs, arguments.execution, REPO),
            str(Path(selected).resolve()),
        )

    def test_canonical_vm_refuses_ambient_compiler_discovery(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            arguments = mncs_test.parser().parse_args(
                ["run", "--execution", "canonical-vm"])
        self.assertIsNone(arguments.mncs)
        with self.assertRaisesRegex(
                mncs_test.ManifestError,
                "requires the selected Stage-0 reference executable"):
            mncs_test.resolve_run_mncs(
                arguments.mncs, arguments.execution, REPO)

    def live_args(self) -> tuple[str, ...]:
        arguments = ("--mncs", str(MNCS), "--library", str(STDLIB_LIBRARY))
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
            str(STDLIB_LIBRARY),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        document = json.loads(completed.stdout)
        manifest = document["manifests"][0]
        self.assertEqual(manifest["inventory"]["schema_version"], "mncs.test-inventory/1")
        self.assertEqual(
            [item["entry"] for item in manifest["tests"]],
            [
                "arithmetic",
                "boolean",
                "native_runner_policy",
                "property_replay",
                "skipped",
                "snapshot_witness",
                "task_lifecycle",
            ],
        )
        self.assertTrue(all(item["source_span"]["start"] < item["source_span"]["end"] for item in manifest["tests"]))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_compiler_issued_test_identity_from_another_source_is_rejected(self):
        environment = os.environ.copy()
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            [str(STDLIB_LIBRARY), str(REPO / "native"), str(REPO)]
        )
        foreign = subprocess.run(
            [str(MNCS), "declaration-inventory", str(REPO / "tests/fixtures/first_class_failing.mncs")],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(foreign.returncode, 0, foreign.stderr)
        inventory = json.loads(foreign.stdout)
        foreign_tests = [
            item["test_case_identity"]
            for item in inventory["inventory"]["callables"]
            if item.get("callable_kind") == "test"
        ]
        self.assertEqual(len(foreign_tests), 1)
        selected = self.invoke(
            "run",
            "--manifest",
            "mncs-test.toml",
            *self.live_args(),
            "--test-identity",
            foreign_tests[0],
        )
        self.assertNotEqual(selected.returncode, 0, selected.stdout)
        failure = json.loads(selected.stdout)["failure"]
        self.assertIn("absent from the current compiler inventory", failure["message"])

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

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_text_projects_commands_natively_without_python_result_tree(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-text-native-") as directory:
            result_path = Path(directory) / "result.json"
            stored = {
                "schema_version": "mncs.test-result/1",
                "reproduction": {
                    "command": 'mncs-test run --manifest "two words" --label "Lúna 😀"',
                    "replay_command": "mncs-test replay --format text",
                },
                "details": {"unicode": "Lúna 東京"},
            }
            source_document = json.dumps(stored, ensure_ascii=True).encode("utf-8")
            result_path.write_bytes(source_document)
            source_text = result_path.read_text(encoding="utf-8")
            arguments = mncs_test.parser().parse_args(
                ["replay", "--result", str(result_path), "--format", "text", "--mncs", str(MNCS)]
            )
            output = io.StringIO()
            string_scalar_parses = []
            native_apps = []
            native_results = []
            real_json_loads = mncs_test.json.loads
            real_run_native_app = mncs_test.run_native_app

            def json_loads(value, *args, **kwargs):
                if value == source_text or (
                    isinstance(value, str) and value.lstrip().startswith('"')
                ):
                    string_scalar_parses.append(value)
                    raise AssertionError("native text replay must not decode JSON strings on the host")
                return real_json_loads(value, *args, **kwargs)

            def run_native_app(*args, **kwargs):
                native_apps.append(args[0])
                result = real_run_native_app(*args, **kwargs)
                native_results.append(result)
                return result

            with mock.patch.object(
                mncs_test,
                "run_native_app",
                side_effect=run_native_app,
            ), mock.patch.object(
                mncs_test.json,
                "loads",
                side_effect=json_loads,
            ), redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)

            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(
                output.getvalue(),
                'mncs-test run --manifest "two words" --label "Lúna 😀"\n'
                "mncs-test replay --format text\n",
            )
            self.assertEqual(string_scalar_parses, [])
            replay_schema = native_results[1]
            self.assertEqual(
                replay_schema["reproduction_command_text"],
                stored["reproduction"]["command"],
            )
            self.assertEqual(
                replay_schema["reproduction_replay_command_text"],
                stored["reproduction"]["replay_command"],
            )
            self.assertNotIn("reproduction_command_start", replay_schema)
            self.assertNotIn("reproduction_replay_command_start", replay_schema)
            self.assertEqual(
                native_apps,
                [
                    "test-public-result-json-key-order.json",
                    "test-replay-schema.json",
                ],
            )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_text_falls_back_for_native_string_decode_bound(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-text-long-") as directory:
            result_path = Path(directory) / "result.json"
            command = "x" * 300
            result_path.write_bytes(
                mncs_test.json_bytes(
                    {
                        "schema_version": "mncs.test-result/1",
                        "reproduction": {"command": command},
                    }
                )
            )
            arguments = mncs_test.parser().parse_args(
                ["replay", "--result", str(result_path), "--format", "text", "--mncs", str(MNCS)]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)
            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(output.getvalue(), f"{command}\n\n")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_text_falls_back_for_non_text_reproduction_values(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-text-fallback-") as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_bytes(
                mncs_test.json_bytes(
                    {
                        "schema_version": "mncs.test-result/1",
                        "reproduction": {"command": 7},
                    }
                )
            )
            arguments = mncs_test.parser().parse_args(
                ["replay", "--result", str(result_path), "--format", "text", "--mncs", str(MNCS)]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)
            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(output.getvalue(), "7\n\n")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_json_uses_native_formatter_without_executing_record(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-json-") as directory:
            root = Path(directory)
            marker = root / "reproduction-was-executed"
            result_path = root / "result.json"
            stored = {
                "schema_version": "mncs.test-result/1",
                "reproduction": {
                    "command": f"{sys.executable} -c 'from pathlib import Path; Path({str(marker)!r}).touch()'",
                    "run_id": "offline-replay",
                },
                "details": {
                    "κλειδί": "Lúna 東京",
                    "numbers": [None, True, False, -17, 3.14159, -0.0, 1e20],
                },
            }
            result_path.write_bytes(mncs_test.json_bytes(stored))
            source_text = result_path.read_text(encoding="utf-8")
            arguments = mncs_test.parser().parse_args(
                [
                    "replay",
                    "--result",
                    str(result_path),
                    "--mncs",
                    str(MNCS),
                ]
            )
            output = io.StringIO()
            native_results = []
            source_tree_parses = []
            real_run_native_app = mncs_test.run_native_app
            real_json_loads = mncs_test.json.loads

            def run_native_app(*args, **kwargs):
                result = real_run_native_app(*args, **kwargs)
                native_results.append((args[0], result))
                return result

            def json_loads(value, *args, **kwargs):
                if value == source_text:
                    source_tree_parses.append(value)
                    raise AssertionError("canonical replay must validate without Python json.loads")
                return real_json_loads(value, *args, **kwargs)

            with mock.patch.object(
                mncs_test,
                "run_native_app",
                side_effect=run_native_app,
            ) as native_app, mock.patch.object(
                mncs_test.json,
                "loads",
                side_effect=json_loads,
            ), redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)

            replay = {
                "schema_version": "mncs.test-replay/1",
                "result": stored,
                "execute": False,
            }
            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(output.getvalue().encode("utf-8"), mncs_test.json_bytes(replay))
            self.assertEqual(
                [call.args[0] for call in native_app.call_args_list],
                [
                    "test-public-result-json-key-order.json",
                    "test-replay-schema.json",
                    "test-public-result-json-token.json",
                    "test-public-result-json-token.json",
                ],
            )
            self.assertEqual(
                [result.get("supported") for _, result in native_results],
                [True, True, True, True],
            )
            self.assertTrue(native_results[1][1]["result_schema_valid"])
            self.assertEqual(source_tree_parses, [])
            self.assertFalse(marker.exists())

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_json_preserves_duplicate_key_and_float_compatibility(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-compatibility-") as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_bytes(
                b'{"number":1e+03,"schema_version":"invalid",'
                b'"schema_version":"mncs.test-result/1"}'
            )
            stored = json.loads(result_path.read_bytes())
            arguments = mncs_test.parser().parse_args(
                [
                    "replay",
                    "--result",
                    str(result_path),
                    "--mncs",
                    str(MNCS),
                ]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)

            replay = {
                "schema_version": "mncs.test-replay/1",
                "result": stored,
                "execute": False,
            }
            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(output.getvalue().encode("utf-8"), mncs_test.json_bytes(replay))

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_replay_json_rejects_native_schema_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-invalid-schema-") as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_bytes(b'{"schema_version":"not-a-test-result"}')
            arguments = mncs_test.parser().parse_args(
                ["replay", "--result", str(result_path), "--mncs", str(MNCS)]
            )
            output = io.StringIO()
            errors = io.StringIO()
            native_apps = []
            run_native_app = mncs_test.run_native_app

            def record_native_app(*args, **kwargs):
                native_apps.append(args[0])
                return run_native_app(*args, **kwargs)

            with mock.patch.object(
                mncs_test,
                "run_native_app",
                side_effect=record_native_app,
            ), redirect_stdout(output), redirect_stderr(errors):
                exit_code = mncs_test.replay_command(arguments)

            self.assertEqual(exit_code, mncs_test.EXIT_INVALID_INVOCATION)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("result is not mncs.test-result/1", errors.getvalue())
            self.assertEqual(
                native_apps,
                [
                    "test-public-result-json-key-order.json",
                    "test-replay-schema.json",
                ],
            )

    def test_replay_json_keeps_offline_formatter_when_compiler_is_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-replay-offline-") as directory:
            root = Path(directory)
            result_path = root / "result.json"
            stored = {
                "schema_version": "mncs.test-result/1",
                "reproduction": {"command": "mncs-test run --manifest x"},
                "details": {"κλειδί": "Lúna 東京"},
            }
            result_path.write_text(json.dumps(stored), encoding="utf-8")
            arguments = mncs_test.parser().parse_args(
                [
                    "replay",
                    "--result",
                    str(result_path),
                    "--mncs",
                    str(root / "missing-mncs"),
                ]
            )
            output = io.StringIO()
            with mock.patch.object(
                mncs_test,
                "run_native_app",
                side_effect=AssertionError("offline replay must not invoke the toolchain"),
            ), redirect_stdout(output):
                exit_code = mncs_test.replay_command(arguments)

            replay = {
                "schema_version": "mncs.test-replay/1",
                "result": stored,
                "execute": False,
            }
            self.assertEqual(exit_code, mncs_test.EXIT_SUCCESS)
            self.assertEqual(output.getvalue().encode("utf-8"), mncs_test.json_bytes(replay))

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
            identity_material = {
                "manifest": document["provenance"]["manifest"],
                "source": document["provenance"]["source"],
                "compiler": document["provenance"]["compiler"],
                "profile": document["provenance"]["language_profile"],
                "tests": [item["id"] for item in document["tests"]],
                "subject_identity": document["test_inventory"].get("subject_identity"),
                "subject_fingerprint": document["test_inventory"].get("subject_fingerprint"),
                "verification_plan": document["selection"].get("plan_id"),
                "obligation_plan": document["verification_obligations"].get("plan_id"),
                "execution": {
                    key: document["execution"].get(key)
                    for key in ("mode", "artifact_identity", "artifact_sha256", "batch_size")
                },
            }
            reference_run_id = hashlib.sha256(
                json.dumps(
                    identity_material,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(document["run_id"], reference_run_id)
            experiment_run_identity = f"mncs:language:experiment-run:{reference_run_id}"
            self.assertEqual(document["experiment"]["run_identity"], experiment_run_identity)
            observations_by_test = {
                item["test_case_identity"]: item
                for item in document["experiment"]["observations"]
            }
            oracle_identities = {
                item["identity"]
                for item in document["experiment"]["oracle_evaluations"]
            }

            def reference_digest(value):
                encoded = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                return hashlib.sha256(encoded).hexdigest()

            lineage_count = 0
            for index, item in enumerate(document["tests"]):
                semantic = item.get("semantic")
                if not isinstance(semantic, dict) or not semantic.get("test_case_identity"):
                    continue
                lineage_count += 1
                execution_digest = reference_digest({
                    "run": experiment_run_identity,
                    "index": index,
                    "test_case": semantic["test_case_identity"],
                })
                observation_digest = reference_digest({
                    "execution": execution_digest,
                    "status": item.get("execution_status", item.get("status")),
                    "verdict": item.get("verdict"),
                })
                execution_identity = f"{experiment_run_identity}:execution:{execution_digest}"
                observation_identity = f"{experiment_run_identity}:observation:{observation_digest}"
                oracle_identity = f"{experiment_run_identity}:oracle:{observation_digest}"
                self.assertEqual(item["execution_identity"], execution_identity)
                self.assertEqual(item["observation_identity"], observation_identity)
                self.assertEqual(item["oracle_evaluation"]["identity"], oracle_identity)
                self.assertEqual(item["execution_lineage"]["execution_identity"], execution_identity)
                self.assertEqual(item["execution_lineage"]["observation_identity"], observation_identity)
                self.assertIn(oracle_identity, oracle_identities)
                self.assertEqual(
                    observations_by_test[semantic["test_case_identity"]],
                    {
                        "identity": observation_identity,
                        "execution_identity": execution_identity,
                        "test_case_identity": semantic["test_case_identity"],
                        "status": item.get("execution_status", item.get("status")),
                        "verdict": item.get("verdict"),
                        "observation_kind": "test_execution",
                    },
                )
            self.assertEqual(lineage_count, 7)
            self.assertEqual(document["summary"]["total"], 7)
            self.assertEqual(document["summary"]["passed"], 6)
            self.assertEqual(document["summary"]["skipped"], 1)
            self.assertEqual(
                document["summary"]["authority"],
                "native_suite",
            )
            self.assertEqual(document["execution"]["mode"], "retained-embed-batch")
            self.assertEqual(
                document["execution"]["native_adjudication"],
                {
                    "authority": "mncs.test.execution_adjudication_v1",
                    "status": "returned",
                    "calls": 1,
                    "case_count": 7,
                },
            )
            self.assertEqual(
                document["execution"]["native_aggregation"]["authority"],
                "mncs.test.suite",
            )
            self.assertEqual(document["execution"]["native_aggregation"]["calls"], 8)
            self.assertEqual(document["native_suite_summary"]["total"], 7)
            self.assertEqual(document["test_inventory"]["test_count"], 7)
            self.assertEqual(len(document["experiment"]["definition"]["test_cases"]), 7)
            self.assertEqual(len(document["experiment"]["observations"]), 7)
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
    def test_family_check_runs_exact_declared_behavioral_tests(self):
        request = {
            "schema_version": "mncs.family-check-request/1",
            "check_identity": "mncs-test:verification-plan-contract",
            "contract_identity": "mncs.verification-plan/1",
            "contract_revision": "1",
            "verification_plan_id": "a" * 64,
            "family_graph_identity": "b" * 64,
            "edge_fingerprint": "c" * 64,
            "source_change_sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="mncs-test-family-check-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            completed = self.invoke(
                "run-check",
                "--request",
                str(request_path),
                "--checks",
                "family-verification-checks-v1.json",
                "--repository-id",
                "mncs-test",
                "--library",
                str(REPO / "native"),
                "--library",
                str(REPO),
                *self.live_args(),
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        response = json.loads(completed.stdout)
        self.assertEqual(
            completed.stdout.encode("utf-8"),
            mncs_test.json_bytes(response),
        )
        self.assertEqual(response["schema_version"], "mncs.family-check-response/1")
        self.assertEqual(response["verdict"], "PASS")
        self.assertEqual(response["runner"], "mncs-test")
        self.assertEqual(response["test_result"]["schema_version"], "mncs.test-result/1")
        self.assertEqual(
            response["test_result"]["execution"]["native_selection"],
            {
                "authority": "mncs.test.exact_identity_selection_v2",
                "mode": "exact_test_identity",
                "status": "returned",
                "calls": 1,
                "case_count": 7,
                "identity_count": 2,
                "selected_count": 2,
            },
        )
        self.assertEqual(response["check_result"]["schema_version"], "mncs.check-result/1")
        self.assertEqual(response["check_result"]["id"], request["check_identity"])

        serializer_applications: list[str] = []
        original_run_native_app = mncs_test.run_native_app

        def observe_public_json_app(name, request, **kwargs):
            result = original_run_native_app(name, request, **kwargs)
            serializer_applications.append(name)
            return result

        with mock.patch.object(mncs_test, "run_native_app", side_effect=observe_public_json_app):
            check_result_payload, check_result_digest = mncs_test._public_result_json_payload(
                response["check_result"],
                mncs=str(MNCS),
                cwd=REPO,
                environment=mncs_test.native_application_environment(REPO),
            )
        self.assertTrue(
            serializer_applications
            and set(serializer_applications)
            <= {
                "test-public-result-json-native-token-tape.json",
                "test-public-result-json-source-digest.json",
                "test-public-result-json-token-tape.json",
                "test-public-result-json-key-order.json",
                "test-public-result-json-token.json",
                "test-public-result-json-token-window.json",
            }
        )
        independent_check_result = mncs_test.json_bytes(response["check_result"])
        self.assertEqual(check_result_payload, independent_check_result)
        self.assertEqual(
            check_result_digest,
            hashlib.sha256(independent_check_result).hexdigest(),
        )
        self.assertEqual(
            response["execution"]["check_result_sha256"],
            check_result_digest,
        )
        self.assertEqual(
            response["family_binding"]["source_change_sha256"],
            request["source_change_sha256"],
        )
        self.assertEqual(response["execution"]["runner_version"], mncs_test.RUNNER_VERSION)
        self.assertEqual(
            response["execution"]["check_definition_identity"],
            mncs_test.sha256_bytes(
                mncs_test.compact_json(
                    mncs_test.load_family_check(
                        REPO / "family-verification-checks-v1.json",
                        check_identity=request["check_identity"],
                        contract_identity=request["contract_identity"],
                        repository_id="mncs-test",
                    )
                ).encode()
            ),
        )
        self.assertEqual(len(response["execution"]["test_case_identities"]), 2)
        self.assertEqual(
            response["execution"]["inventory_identity"],
            "368a2b43a34d82c85ad4a2a6f2ea102bbff386080e677bbc7e33c5fc32ca7cba",
        )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_family_check_digest_uses_native_public_result_serializer(self):
        request = {
            "schema_version": "mncs.family-check-request/1",
            "check_identity": "mncs-test:verification-plan-contract",
            "contract_identity": "mncs.verification-plan/1",
            "contract_revision": "1",
            "verification_plan_id": "a" * 64,
            "family_graph_identity": "b" * 64,
            "edge_fingerprint": "c" * 64,
            "source_change_sha256": "d" * 64,
        }
        checks_path = REPO / "family-verification-checks-v1.json"
        check = mncs_test.load_family_check(
            checks_path,
            check_identity=request["check_identity"],
            contract_identity=request["contract_identity"],
            repository_id="mncs-test",
        )
        selected_identities = check["selector"]["test_identities"]
        result = {
            "schema_version": mncs_test.RESULT_SCHEMA,
            "id": "family-check-fixture",
            "provider": "mncs-test",
            "verdict": "PASS",
            "classification": "supported",
            "failure_class": "none",
            "scope": {"manifest": "mncs-test.toml"},
            "summary": {"passed": len(selected_identities), "failed": 0, "skipped": 0},
            "tests": [{"id": identity, "verdict": "PASS"} for identity in selected_identities],
            "run_id": "run-family-check-fixture",
            "selection": {"selected_test_identities": selected_identities},
            "test_inventory": {
                "inventory_identity": check["selector"].get("inventory_identity"),
            },
            "experiment": {"run_identity": "run-family-check-fixture"},
        }

        with tempfile.TemporaryDirectory(prefix="mncs-test-family-digest-") as directory:
            root = Path(directory)
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")

            def fake_inner_run(command, *, cwd, capture_output, text, check, timeout):
                result_path = Path(command[command.index("--result") + 1])
                check_path = Path(command[command.index("--check-result") + 1])
                result_path.write_text(json.dumps(result), encoding="utf-8")
                check_path.write_text(
                    json.dumps({"schema_version": mncs_test.CHECK_SCHEMA, "verdict": "PASS"}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            arguments = SimpleNamespace(
                request=str(request_path),
                checks=str(checks_path),
                repository_id="mncs-test",
                mncs=str(MNCS),
                library=[],
                embed_library=None,
                timeout_seconds=120,
            )
            native_applications: list[str] = []
            native_json_requests: list[dict] = []
            original_run_native_app = mncs_test.run_native_app
            public_json_apps = {
                "test-public-result-json-native-token-tape.json",
                "test-public-result-json-source-digest.json",
                "test-public-result-json-token-tape.json",
                "test-public-result-json-key-order.json",
                "test-public-result-json-token.json",
                "test-public-result-json-token-window.json",
            }

            def observe_native_app(name, native_request, **kwargs):
                response = original_run_native_app(name, native_request, **kwargs)
                native_applications.append(name)
                if name == "test-check-result-json-native.json":
                    native_json_requests.append(native_request)
                return response

            public_payload_inputs = []
            original_public_payload = mncs_test._public_result_json_payload

            def observe_public_payload(value, **kwargs):
                public_payload_inputs.append(value)
                return original_public_payload(value, **kwargs)

            output = io.StringIO()
            with (
                mock.patch.object(
                    mncs_test,
                    "subprocess",
                    SimpleNamespace(run=fake_inner_run, SubprocessError=subprocess.SubprocessError),
                ),
                mock.patch.object(
                    mncs_test, "run_native_app", side_effect=observe_native_app
                ),
                mock.patch.object(
                    mncs_test,
                    "_public_result_json_payload",
                    side_effect=observe_public_payload,
                ),
                redirect_stdout(output),
            ):
                exit_code = mncs_test.run_family_check(arguments)

        self.assertEqual(exit_code, 0, output.getvalue())
        response = json.loads(output.getvalue())
        self.assertIn("test-check-result-json-native.json", native_applications)
        expected_test_result_document = json.dumps(result).encode("utf-8")
        expected_native_document, _ = mncs_test._native_public_result_document_request(
            expected_test_result_document
        )
        self.assertEqual(
            [request["document"] for request in native_json_requests],
            [expected_native_document["document"]],
            "family CheckResult projection should consume the retained TestResult document",
        )
        self.assertIn("test-result-identity.json", native_applications)
        self.assertGreaterEqual(
            sum(name in public_json_apps for name in native_applications),
            2,
            native_applications,
        )
        self.assertEqual(
            len(public_payload_inputs),
            1,
            "the response path should pass serialized result fragments to MNCS",
        )
        self.assertEqual(
            public_payload_inputs[0]["schema_version"],
            mncs_test.CHECK_SCHEMA,
        )
        self.assertEqual(output.getvalue().encode("utf-8"), mncs_test.json_bytes(response))
        public_json = mncs_test.json_bytes(response["check_result"])
        self.assertEqual(
            response["execution"]["check_result_sha256"],
            hashlib.sha256(public_json).hexdigest(),
        )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_result_identity_stream_reuses_native_update_values(self):
        payload = bytes(
            (index * 37 + 5) % 256
            for index in range(mncs_test.TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES + 1)
        )
        environment = mncs_test.native_application_environment(REPO)
        with tempfile.TemporaryDirectory(prefix="mncs-test-identity-stream-") as directory:
            hasher = mncs_test.ResultIdentityEmbedHasher(
                mncs=str(MNCS),
                cwd=Path(directory),
                environment=environment,
            )
            try:
                self.assertIsNotNone(hasher.session)
                with mock.patch.object(
                    hasher.session,
                    "call_batch",
                    wraps=hasher.session.call_batch,
                ) as native_calls:
                    digest = hasher.digest(payload)
            finally:
                hasher.close()

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        requests = [call.args[0][0] for call in native_calls.call_args_list]
        self.assertEqual(
            [request["function"] for request in requests],
            ["update_stream_state_from_empty"]
            + ["update_stream_state_continue"] * 4,
        )
        self.assertTrue(
            all("args" in request and "typed_args" not in request for request in requests)
        )

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_family_inventory_join_runs_its_exact_behavioral_test(self):
        request = {
            "schema_version": "mncs.family-check-request/1",
            "check_identity": "mncs-test:test-inventory-join",
            "contract_identity": "mncs.compiler.test-inventory/1",
            "contract_revision": "0.18",
            "verification_plan_id": "a" * 64,
            "family_graph_identity": "b" * 64,
            "edge_fingerprint": "c" * 64,
            "source_change_sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="mncs-test-family-inventory-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            completed = self.invoke(
                "run-check",
                "--request",
                str(request_path),
                "--checks",
                "family-verification-checks-v1.json",
                "--repository-id",
                "mncs-test",
                "--library",
                str(REPO / "native"),
                "--library",
                str(REPO),
                *self.live_args(),
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        response = json.loads(completed.stdout)
        self.assertEqual(response["verdict"], "PASS")
        self.assertEqual(
            response["execution"]["test_case_identities"],
            [
                "mncs:0.2:test-case:tests.self_suite::task_lifecycle::"
                "b1ac42033bff08ddd2bdcbf98e5ff268458922c58a1f69a036906afe56734f75"
            ],
        )
        self.assertEqual(
            response["test_result"]["execution"]["native_selection"]["authority"],
            "mncs.test.exact_identity_selection_v2",
        )

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
            self.assertEqual(document["test_inventory"]["test_count"], 7)
            self.assertEqual(document["execution"]["selected_test_count"], 1)
            self.assertEqual(document["execution"]["batch_size"], 1)
            self.assertEqual(
                document["execution"]["native_selection"],
                {
                    "authority": "mncs.test.inventory_filter_v2",
                    "mode": "substring_any",
                    "status": "returned",
                    "calls": 1,
                    "case_count": 7,
                    "filter_count": 1,
                    "selected_count": 1,
                },
            )
            self.assertEqual(document["summary"]["authority"], "native_suite")
            self.assertEqual([item["entry"] for item in document["tests"]], ["arithmetic"])
            self.assertEqual(len(document["experiment"]["observations"]), 1)
            item = document["tests"][0]
            self.assertEqual(item["request"]["target"]["function"], "arithmetic")
            self.assertNotIn("function", item["transport_request"])
            self.assertEqual(
                item["transport_request"]["callable_reference"]["callable_identity"],
                item["semantic"]["function_identity"],
            )
            self.assertEqual(
                item["transport_request"]["callable_reference"]["test_case_identity"],
                item["semantic"]["test_case_identity"],
            )
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
        environment["MNCS_LIBRARY_PATH"] = str(REPO / "native") + os.pathsep + str(STDLIB_LIBRARY)
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
                    "coverage": {
                        "registry_identity": "c" * 64,
                        "registered_family_project_count": 0,
                        "classified_project_count": 0,
                        "semantic_graph_participant_count": 0,
                        "explicit_nonparticipant_count": 0,
                        "unclassified_project_count": 0,
                        "unclassified_repositories": [],
                        "coverage_status": "not_requested",
                        "topology_status": "not_requested",
                    },
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
            self.assertEqual(document["tests"][0]["compile_verdict"], "expected_failure")
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
            self.assertEqual(document["tests"][0]["compile_verdict"], "compile_pass")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_compile_expectation_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-compile-mismatch-") as directory:
            result = Path(directory) / "result.json"
            completed = self.invoke(
                "run",
                "--manifest",
                "tests/fixtures/compile-unexpected-success.toml",
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
            item = document["tests"][0]
            self.assertEqual(item["compile_verdict"], "compile_expectation_mismatch")
            self.assertEqual(item["failure"]["class"], "compile_expectation_mismatch")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_compile_failure_is_reported_for_compile_pass_expectation(self):
        with tempfile.TemporaryDirectory(prefix="mncs-test-compile-failure-") as directory:
            result = Path(directory) / "result.json"
            with tempfile.TemporaryDirectory(
                prefix=".compile-unexpected-failure-",
                dir=REPO / "tests" / "fixtures",
            ) as manifest_dir:
                manifest = Path(manifest_dir) / "manifest.toml"
                manifest.write_text(
                    'schema_version = "mncs.test-manifest/1"\n'
                    'name = "compile-unexpected-failure-fixture"\n'
                    'source = "../compile_fail.mncs"\n'
                    'module = "tests.fixtures.compile_fail"\n'
                    'profile = "0.16"\n'
                    'timeout_seconds = 30\n'
                    'libraries = ["../../../native"]\n\n'
                    '[[tests]]\n'
                    'id = "unexpected-failure"\n'
                    'entry = "invalid_return"\n'
                    'kind = "compile-pass"\n'
                    'tags = ["positive", "compiler"]\n',
                    encoding="utf-8",
                )
                completed = self.invoke(
                    "run",
                    "--manifest",
                    str(manifest.relative_to(REPO)),
                    *self.live_args(),
                    "--result",
                    str(result),
                    "--check-result",
                    str(Path(directory) / "check.json"),
                    "--artifacts",
                    str(Path(directory) / "artifacts"),
                )
            self.assertEqual(completed.returncode, 4, completed.stdout)
            document = json.loads(result.read_text(encoding="utf-8"))
            item = document["tests"][0]
            self.assertEqual(item["compile_verdict"], "compile_failure")
            self.assertEqual(item["failure"]["class"], "compile_failure")
            self.assertEqual(document["summary"]["authority"], "mncs.test.result_summary")
            self.assertEqual(document["summary"]["total"], 1)
            self.assertEqual(document["summary"]["failed"], 1)
            self.assertEqual(document["summary"]["compile_failures"], 1)

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

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_unsupported_is_explicit_unknown(self):
        completed = self.invoke(
            "run",
            "--manifest",
            "tests/fixtures/unsupported.toml",
            *self.live_args(),
            "--result",
            "/tmp/mncs-test-unsupported-unittest-result.json",
            "--check-result",
            "/tmp/mncs-test-unsupported-unittest-check.json",
            "--artifacts",
            "/tmp/mncs-test-unsupported-unittest-artifacts",
        )
        self.assertEqual(completed.returncode, 6, completed.stdout)
        document = json.loads(Path("/tmp/mncs-test-unsupported-unittest-result.json").read_text(encoding="utf-8"))
        self.assertEqual(document["verdict"], "UNKNOWN")
        self.assertEqual(document["classification"], "unsupported")

    @unittest.skipUnless(LIVE, "a built sibling mncs compiler is required")
    def test_native_modules_validate(self):
        environment = dict(os.environ)
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            (
                str(REPO / "native"),
                str(REPO),
                str(STDLIB_LIBRARY),
                str(COMMONS / "src" / "mncs_commons" / "mesh"),
            )
        )
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
