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


def identity_bytes(value: str) -> list[int]:
    return list(value.encode("utf-8"))


def digest_bytes(value: str) -> list[int]:
    return list(bytes.fromhex(value.removeprefix("sha256:")))


def compiler_binding_projection(document: dict[str, object]) -> list[dict[str, object]]:
    """Encode compiler identity text at the no-text transport boundary."""
    artifact_identity = document.get("artifact_identity")
    bindings = document.get("callable_bindings")
    assert isinstance(artifact_identity, str) and artifact_identity
    assert isinstance(bindings, list)
    output = []
    for binding in bindings:
        if not isinstance(binding, dict) or not binding.get("test_case_identity"):
            continue
        output.append(
            {
                "artifact_identity": identity_bytes(artifact_identity),
                "test_case_identity": identity_bytes(str(binding["test_case_identity"])),
                "declaration_identity": identity_bytes(str(binding["declaration_identity"])),
                "callable_identity": identity_bytes(str(binding["callable_identity"])),
                "signature_identity": identity_bytes(str(binding["signature_identity"])),
            }
        )
    return output


def project_provider_request(value: dict[str, object]) -> dict[str, object]:
    """Materialize the request against its compiler-owned ProviderRequest contract."""
    import ctypes

    descriptor_path = ROOT / "native-applications/test-provider.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    source_path = (descriptor_path.parent / descriptor["source"]).resolve()
    libraries = [
        (descriptor_path.parent / item).resolve()
        for item in descriptor.get("libraries", [])
        if isinstance(item, str)
    ]
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(str(path) for path in libraries)
    with tempfile.TemporaryDirectory(prefix=".provider-projection-artifact-", dir=ROOT) as directory:
        output = Path(directory)
        completed = subprocess.run(
            [
                str(MNCS),
                "compile",
                str(source_path),
                "--emit",
                "backend",
                "--target",
                "research-bytecode",
                "--include-tests",
                "--output-dir",
                str(output),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        backend_path = output / "backend.json"
        assert completed.returncode == 0 and backend_path.is_file(), completed.stderr
        backend_bytes = backend_path.read_bytes()
        backend = json.loads(backend_bytes)
        library_path = Path(
            os.environ.get("MNCS_EMBED_LIBRARY", MNCS.parent / "libmncs_embed.so")
        )
        library = ctypes.CDLL(str(library_path))
        byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
        library.mncs_session_open.argtypes = [byte_pointer, ctypes.c_size_t]
        library.mncs_session_open.restype = ctypes.c_void_p
        library.mncs_session_composite_types.argtypes = [ctypes.c_void_p]
        library.mncs_session_composite_types.restype = ctypes.c_void_p
        library.mncs_session_project_value.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.mncs_session_project_value.restype = ctypes.c_void_p
        library.mncs_session_serialize_value.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.mncs_session_serialize_value.restype = ctypes.c_void_p
        library.mncs_session_close.argtypes = [ctypes.c_void_p]
        library.mncs_session_close.restype = None
        library.mncs_response_text.argtypes = [ctypes.c_void_p]
        library.mncs_response_text.restype = ctypes.c_char_p
        library.mncs_response_free.argtypes = [ctypes.c_void_p]
        library.mncs_response_free.restype = None
        library.mncs_last_error.argtypes = []
        library.mncs_last_error.restype = ctypes.c_char_p

        def response(pointer: int) -> object:
            if not pointer:
                message = library.mncs_last_error()
                raise AssertionError(
                    message.decode("utf-8", errors="replace") if message else "projection failed"
                )
            try:
                payload = library.mncs_response_text(pointer)
                assert payload
                return json.loads(payload.decode("utf-8"))
            finally:
                library.mncs_response_free(pointer)

        buffer = (ctypes.c_ubyte * len(backend_bytes)).from_buffer_copy(backend_bytes)
        session = library.mncs_session_open(buffer, len(backend_bytes))
        assert session, "mncs-embed rejected the provider artifact"
        try:
            composites = response(library.mncs_session_composite_types(session))
            matches = [
                item
                for item in composites
                if isinstance(item, dict)
                and item.get("name") == "ProviderRequest"
                and item.get("kind") == "record"
            ]
            assert len(matches) == 1, matches
            reference = matches[0]["reference"]
            projection_value = {
                key: item for key, item in value.items() if key != "schema_version"
            }
            request = json.dumps(
                {"reference": reference, "value": projection_value},
                separators=(",", ":"),
            ).encode()
            projected = response(library.mncs_session_project_value(session, request))
            assert projected.get("artifact_identity") == backend.get("identity")
            serialized = json.dumps(
                {"reference": reference, "value": projected["value"]},
                separators=(",", ":"),
            ).encode()
            canonical = response(library.mncs_session_serialize_value(session, serialized))
            assert isinstance(canonical, dict)
            return {"schema_version": value.get("schema_version"), **canonical}
        finally:
            library.mncs_session_close(session)


def identity_bound_test_executions() -> tuple[
    list[str], list[dict[str, object]], list[str], list[dict[str, object]]
]:
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

    def run_selected(
        manifest: str, identities: list[str]
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
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
            execution = result["execution"]
            metadata = execution.get("compiler_callable_bindings")
            assert isinstance(metadata, dict), execution
            assert metadata.get("artifact_identity") == execution.get("artifact_identity")
            return observations, metadata

    self_inventory = compiler_inventory("tests/self_suite.mncs")
    self_selected = [
        item for item in self_inventory["callables"]
        if item.get("callable_kind") == "test" and item.get("name") in {"arithmetic", "boolean"}
    ]
    assert {item["name"] for item in self_selected} == {"arithmetic", "boolean"}
    self_selected.sort(key=lambda item: item["test_case_identity"])
    self_ids = [item["test_case_identity"] for item in self_selected]
    observations, self_bindings = run_selected("mncs-test.toml", self_ids)
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
        other_observations, other_bindings = run_selected(
            str(other_manifest), [other["test_case_identity"]]
        )
    finally:
        other_manifest.unlink(missing_ok=True)
    observations.extend(other_observations)

    late_source = ROOT / "tests/provider_projection_late.mncs"
    late_manifest = ROOT / "tests/provider_projection_late.toml"
    late_source.write_text(
        "\n".join(
            [
                "mncs 0.18;",
                "module tests.provider_projection_late;",
                "use mncs.test.assertions;",
                "test added_after_provider_source() -> (result: TestResult) {",
                "    return from_assertion(equals_i64(9, 4 +% 5, 9101));",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    libraries = [LANGUAGE_ROOT / "library", ROOT / "native", ROOT]
    late_manifest.write_text(
        "\n".join(
            [
                'schema_version = "mncs.test-manifest/1"',
                'name = "mncs-test-provider-projection-late"',
                f"source = {json.dumps(str(late_source))}",
                'module = "tests.provider_projection_late"',
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
        late_inventory = compiler_inventory(str(late_source))
        late_test = next(
            item
            for item in late_inventory["callables"]
            if isinstance(item, dict)
            and item.get("callable_kind") == "test"
            and item.get("name") == "added_after_provider_source"
        )
        late_observations, late_bindings = run_selected(
            str(late_manifest), [str(late_test["test_case_identity"])]
        )
    finally:
        late_source.unlink(missing_ok=True)
        late_manifest.unlink(missing_ok=True)
    observations.extend(late_observations)
    observations.sort(key=lambda item: item["semantic"]["test_case_identity"])
    selected_ids = [item["semantic"]["test_case_identity"] for item in observations]
    modules = [item["semantic"]["module"] for item in observations]
    assert len(set(modules)) >= 2, modules

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
    projected_bindings = compiler_binding_projection(self_bindings)
    projected_bindings.extend(compiler_binding_projection(other_bindings))
    projected_bindings.extend(compiler_binding_projection(late_bindings))
    projected_bindings.sort(
        key=lambda row: (
            row["artifact_identity"],
            row["test_case_identity"],
            row["callable_identity"],
        )
    )
    return selected_ids, executions, modules, projected_bindings


def test_native_provider_projects_compiler_bindings_without_generated_inventory() -> None:
    selected_ids, selected_executions, modules, compiler_bindings = identity_bound_test_executions()
    assert len(set(modules)) >= 2
    assert "tests.provider_projection_late" in modules
    assert not (ROOT / "native/mncs/test/provider_inventory.mncs").exists()
    assert not (ROOT / "tools/generate_provider.py").exists()
    provider = json.loads(
        (ROOT / "native-applications/provider-admitted.json").read_text(encoding="utf-8")
    )
    projected_executions = [
        {
            **{
                key: identity_bytes(str(execution[key]))
                for key in (
                    "test_case_identity",
                    "declaration_identity",
                    "callable_identity",
                    "signature_identity",
                    "artifact_identity",
                )
            },
            "execution_status": execution["execution_status"],
            "native_result": execution["native_result"],
        }
        for execution in selected_executions
    ]
    request = {
        "schema_version": "mncs.test-provider-request/1",
        "compiler_callable_bindings": compiler_bindings,
        "selected_test_identities": [identity_bytes(identity) for identity in selected_ids],
        "selected_test_executions": projected_executions,
        "selection_count": len(selected_ids),
        "interface_identity": digest_bytes(provider["interface_identity"]),
        "provider_revision_identity": provider["revision_identity"],
    }
    request["provider_revision_identity"] = digest_bytes(provider["revision_identity"])
    def run_provider(value: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix=".provider-projection-", dir=ROOT) as directory:
            artifact_root = Path(directory)
            projected_request = project_provider_request(value)
            (artifact_root / "request.json").write_text(
                json.dumps(projected_request), encoding="utf-8"
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
            return (
                json.loads((artifact_root / "result.json").read_text(encoding="utf-8")),
                json.loads((artifact_root / "check.json").read_text(encoding="utf-8")),
            )

    result, check = run_provider(request)
    assert result["native_result"]["verdict"] == "PASS", json.dumps(
        result["native_result"], sort_keys=True
    )
    assert result["selected_test_identities"] == selected_ids
    assert result["compiler_callable_bindings_identity"]
    assert result["result_identity"] != check["result_identity"]
    assert check["test_result_identity"] == result["result_identity"]

    stale = json.loads(json.dumps(request))
    selected_artifact = stale["selected_test_executions"][0]["artifact_identity"]
    foreign_artifact = next(
        row["artifact_identity"]
        for row in compiler_bindings
        if row["artifact_identity"] != selected_artifact
    )
    stale["selected_test_executions"][0]["artifact_identity"] = foreign_artifact
    stale_result, _stale_check = run_provider(stale)
    assert stale_result["native_result"]["verdict"] == "UNSUPPORTED"


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
