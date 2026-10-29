#!/usr/bin/env python3
"""Exercise a Compiler /4 application build through the canonical Test VM path."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mncs_test
from vm_transport import VmTestSession


REQUEST_SCHEMA = "mncs.compiler.application-build-request/1"
RESPONSE_SCHEMA = "mncs.compiler.application-build/4"
APP_MODULE = "tests.native_application_build_readiness"
APP_SOURCE_ID = "tests/native_application_build_readiness.mncs"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load selected provider module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _repository_root(path: Path, repository: str) -> Path | None:
    manifest_path = path / ".mncs/project.json"
    try:
        if path.is_symlink() or manifest_path.is_symlink():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("repository") == repository:
            return path.resolve(strict=True)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _selected_toolchain_root(
    environment_name: str,
    repository: str,
    required_files: tuple[str, ...],
) -> Path:
    environment_definition = ROOT / ".mncs/environment.json"
    try:
        definition = json.loads(environment_definition.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Test Environment definition is unavailable") from error
    workspace_scope = definition.get("workspace_scope", {})
    intent = definition.get("intent", {})
    selected_repositories = workspace_scope.get("repositories", [])
    protected_repositories = intent.get("protected_repositories", [])
    if (repository not in selected_repositories
            or repository not in protected_repositories):
        raise RuntimeError(
            f"{repository} is not declared as a protected toolchain for this Test session"
        )

    candidates: list[Path] = []
    configured = os.environ.get(environment_name)
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path("/workspace") / repository)
    language_checkout = os.environ.get("MNCS_LANGUAGE_CHECKOUT") or os.environ.get("MNCS_LANGUAGE_ROOT")
    if language_checkout:
        candidates.append(Path(language_checkout).resolve().parent / repository)
    candidates.extend(parent / repository for parent in (ROOT, *ROOT.parents))
    for candidate in candidates:
        selected = _repository_root(candidate, repository)
        if selected is not None and all((selected / relative).is_file()
                                        for relative in required_files):
            return selected
    raise RuntimeError(
        f"Environment selected no usable {repository} toolchain checkout"
    )


def _reset_output_namespace(output_root: Path) -> Path:
    """Give this verifier a fresh private directory on each invocation retry."""
    legacy_names = {
        "native-application-build.json",
        "native-application-build-vm-result.json",
    }
    for name in legacy_names:
        legacy = output_root / name
        if legacy.is_symlink():
            raise RuntimeError("prior Environment result artifact is a symlink")
        if legacy.is_file():
            legacy.unlink()
        elif legacy.exists():
            raise RuntimeError("prior Environment result artifact has an unexpected type")

    namespace = output_root / "native-application-build-vm"
    if namespace.is_symlink():
        raise RuntimeError("prior Environment result namespace is a symlink")
    if namespace.exists():
        if not namespace.is_dir():
            raise RuntimeError("prior Environment result namespace has an unexpected type")
        allowed = legacy_names
        for child in namespace.iterdir():
            if child.is_symlink() or not child.is_file() or child.name not in allowed:
                raise RuntimeError("prior Environment result namespace contains an unexpected entry")
            child.unlink()
        namespace.rmdir()
    namespace.mkdir(mode=0o700)
    return namespace


def run() -> dict[str, object]:
    compiler_root = _selected_toolchain_root(
        "MNCS_COMPILER_CHECKOUT",
        "mncs-compiler",
        ("tools/application_build_provider.py",
         ".bootstrap/target/release/mncs-compiler-stage0-probe"),
    )
    vm_root = _selected_toolchain_root(
        "MNCS_VM_CHECKOUT",
        "mncs-vm",
        ("python/mncs_vm_client/__init__.py", "target/debug/mncs-vm"),
    )
    vm_executable = Path(os.environ.get("MNCS_VM_BIN", vm_root / "target/debug/mncs-vm")).resolve()
    probe = (compiler_root / ".bootstrap/target/release/mncs-compiler-stage0-probe").resolve()
    for path in (compiler_root / "tools/application_build_provider.py", probe,
                 vm_root / "python/mncs_vm_client/__init__.py", vm_executable):
        if not path.is_file():
            raise RuntimeError(f"selected read-only toolchain input is unavailable: {path}")
    if not probe.is_relative_to(compiler_root):
        raise RuntimeError("selected Compiler probe is outside the declared Compiler checkout")
    os.environ["MNCS_COMPILER_PROBE"] = str(probe)

    compiler_provider = load_module(
        "mncs_test_selected_application_build_provider",
        compiler_root / "tools/application_build_provider.py",
    )
    source_path = ROOT / APP_SOURCE_ID
    source_text = source_path.read_text(encoding="utf-8")
    modules = {
        "mncs.test.assertions": (ROOT / "native/mncs/test/assertions.mncs").read_text(encoding="utf-8"),
        "mncs.test.suite": (ROOT / "native/mncs/test/suite.mncs").read_text(encoding="utf-8"),
    }
    request = {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": APP_SOURCE_ID,
        "root_source": source_text,
        "entry": {"module": APP_MODULE, "function": "application_entry"},
        "modules": modules,
        "generic_seeds": [{
            "module": "mncs.test.suite",
            "function": "summarize",
            "type_arguments": [{"kind": "nat", "value": 2}],
        }],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }

    configured_output = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
    output_context = None
    if configured_output:
        output_dir = Path(configured_output)
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise RuntimeError("Environment invocation artifact directory is unavailable")
        output_dir = output_dir.resolve(strict=True)
    else:
        output_context = tempfile.TemporaryDirectory(prefix="mncs-test-app-output-")
        output_dir = Path(output_context.name)
    provider_output_dir = _reset_output_namespace(output_dir)
    os.environ["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(provider_output_dir)

    try:
        with tempfile.TemporaryDirectory(prefix="mncs-test-app-cache-") as cache_name:
            cache_dir = Path(cache_name)
            response = compiler_provider.run_build(
                compiler_provider.json_bytes(request),
                cache=cache_dir,
                timeout=120,
            )
            if response.get("schema_version") != RESPONSE_SCHEMA:
                raise RuntimeError("Compiler returned an unsupported application-build response")
            if response.get("status") != "verified" or response.get("entry", {}).get("status") != "verified":
                raise RuntimeError("Compiler did not verify the Test application entry: " + json.dumps({
                    "status": response.get("status"),
                    "entry": response.get("entry"),
                    "unresolved_obligations": response.get("unresolved_obligations"),
                    "diagnostics": response.get("diagnostics"),
                }, sort_keys=True))
            artifact = response.get("artifact")
            artifact_value = artifact.get("value") if isinstance(artifact, dict) else None
            artifact_identity = artifact.get("identity") if isinstance(artifact, dict) else None
            interface = response.get("entry", {}).get("interface")
            if (not isinstance(artifact_value, dict) or not isinstance(artifact_identity, str)
                    or not isinstance(interface, dict) or interface.get("status") != "verified"):
                raise RuntimeError("Compiler /4 response omitted its verified artifact or callable interface")

            response_file_name = compiler_provider.publish_response(response)
            if response_file_name != "native-application-build.json":
                raise RuntimeError("Compiler provider did not publish its declared /4 response artifact")
            response_file = provider_output_dir / response_file_name
            response_bytes = response_file.read_bytes()
            if json.loads(response_bytes) != response:
                raise RuntimeError("published Compiler response does not match the returned /4 document")

            artifact_path = cache_dir / "native-application.vm.json"
            artifact_bytes = compiler_provider.json_bytes(artifact_value)
            artifact_path.write_bytes(artifact_bytes)
            product = {
                "cache": str(cache_dir),
                "artifact": {"address": artifact_path.name, "identity": artifact_identity},
                "build_receipt": None,
                "producer": {
                    "identity": response.get("producer_identity", "mncs-compiler:application-build/4"),
                    "response_schema": RESPONSE_SCHEMA,
                },
            }
            evidence = {"callable_bindings": []}
            session = VmTestSession(
                vm_checkout=vm_root,
                vm_executable=vm_executable,
                product=product,
                evidence=evidence,
                timeout=120,
                adapter_error=mncs_test.AdapterError,
            )
            try:
                if session.info()["artifact_identity"] != artifact_identity:
                    raise RuntimeError("canonical VM admitted an artifact with a different Compiler identity")
                tests = [
                    {"id": "application-build-suite-call-1", "entry": "application_entry",
                     "kind": "application_build_integration", "arguments": [], "step_budget": 200000},
                    {"id": "application-build-suite-call-2", "entry": "application_entry",
                     "kind": "application_build_integration", "arguments": [], "step_budget": 200000},
                ]
                artifacts = mncs_test.ArtifactStore(cache_dir / "runner-artifacts")
                transport = {
                    "mode": "canonical-vm-session",
                    "artifact_identity": artifact_identity,
                    "batch_size": len(tests),
                }
                workspace_root = next(
                    (
                        candidate
                        for candidate in (ROOT.parent, *ROOT.parents)
                        if (candidate / "mncs-stdlib" / "stdlib-manifest.json").is_file()
                    ),
                    ROOT.parent,
                )
                selected_environment = mncs_test.command_environment(
                    [workspace_root / "mncs-stdlib" / "library", ROOT / "native"],
                    dict(os.environ),
                )
                results = mncs_test.execute_batch(
                    tests=tests,
                    source_path=source_path,
                    source_text=source_text,
                    module=APP_MODULE,
                    session=session,
                    mncs=mncs_test.resolve_run_mncs(
                        os.environ.get("MNCS") or os.environ.get("MNCS_BINARY"),
                        "stage0-reference", ROOT,
                    ),
                    cwd=ROOT,
                    environment=selected_environment,
                    default_step_budget=200000,
                    artifacts=artifacts,
                    transport=transport,
                    suite=True,
                )
                summaries = [row.get("native_result") for row in results]
                if (len(results) != 2 or any(row.get("status") != "passed" for row in results)
                        or any(not isinstance(item, dict) or item.get("verdict") != "PASS"
                               or item.get("total") != 2 or item.get("passed") != 2
                               or item.get("failed") != 0 for item in summaries)
                        or session.runtime.count != 2 or len(session.timings) != 1):
                    raise RuntimeError("canonical Test retained-session batch did not return the expected results: " + json.dumps({
                        "tests": [{"id": row.get("id"), "status": row.get("status"),
                                  "verdict": row.get("verdict"), "failure": row.get("failure")}
                                 for row in results],
                        "native_summaries": summaries,
                        "vm_calls": session.runtime.count,
                    }, sort_keys=True))
                summary = summaries[0]
                runtime = session.info()["runtime"]
                runner_summary = {
                    "schema_version": 1,
                    "kind": "mncs-test.compiler-application-build-vm-integration",
                    "status": "pass",
                    "compiler_response_schema": response["schema_version"],
                    "compiler_response_sha256": sha256(response_bytes),
                    "compiler_artifact_identity": artifact_identity,
                    "vm_artifact_identity": session.info()["artifact_identity"],
                    "entry_interface_identity": interface.get("identity"),
                    "test_cases": [
                        {"id": row["id"], "verdict": row["verdict"], "status": row["status"]}
                        for row in results
                    ],
                    "suite": summary,
                    "retained_session": {
                        "runtime": {
                            "vm_contract": runtime.get("contract", {}).get("vm_contract"),
                            "build_origin_status": runtime.get("build_origin", {}).get("status"),
                            "build_receipt_identity": runtime.get("build_origin", {}).get("receipt_identity"),
                            "executable_sha256": runtime.get("executable_sha256"),
                        },
                        "calls": session.runtime.count,
                        "batch_calls": len(session.timings),
                    },
                }
            finally:
                session.close()

        result_path = provider_output_dir / "native-application-build-vm-result.json"
        result_bytes = json.dumps(
            runner_summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with result_path.open("xb") as stream:
            stream.write(result_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        runner_summary["result_artifact"] = {
            "name": f"{provider_output_dir.name}/{result_path.name}",
            "byte_count": len(result_bytes),
            "sha256": sha256(result_bytes),
        }
        return runner_summary
    finally:
        if output_context is not None:
            output_context.cleanup()


def main() -> int:
    try:
        result = run()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
