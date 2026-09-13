#!/usr/bin/env python3
"""The narrow transport adapter for the native MNCS test framework.

The semantic core lives in ``native/mncs/test``.  This module is deliberately
limited to the boundaries MNCS does not currently expose to an external
consumer: TOML/file discovery, process supervision, bounded request/response
transport, and preservation of raw artifacts.  It never invents assertions,
generates property values, or recomputes a native suite verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable


RESULT_SCHEMA = "mncs.test-result/1"
CHECK_SCHEMA = "mncs.check-result/1"
MANIFEST_SCHEMA = "mncs.test-manifest/1"
DISCOVERY_SCHEMA = "mncs.test-discovery/1"
RUNNER_VERSION = "0.1.0"

EXIT_SUCCESS = 0
EXIT_TEST_FAILURE = 1
EXIT_INVALID_INVOCATION = 2
EXIT_INFRASTRUCTURE_FAILURE = 3
EXIT_COMPILE_FAILURE = 4
EXIT_TIMEOUT = 5
EXIT_UNSUPPORTED = 6

VERDICTS = ("PASS", "FAIL", "UNKNOWN")
EXECUTION_STATUSES = {
    "returned",
    "runtime_failure",
    "unsupported",
    "budget_exhausted",
    "invalid_request",
}
TEST_KINDS = {
    "unit",
    "specification",
    "integration",
    "property",
    "snapshot",
    "regression",
    "compile-pass",
    "compile-fail",
    "diagnostic",
    "runtime-failure",
    "skip",
    "unsupported",
}
DIAGNOSTIC_CODE = re.compile(r"^[A-Z][A-Z0-9_-]*")
FUNCTION_PATTERN = re.compile(r"\bfn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")


class ManifestError(ValueError):
    """A manifest is malformed or refers to an unsafe/missing input."""


class AdapterError(RuntimeError):
    """A transport boundary failed before a test could produce a result."""


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(path: Path) -> str:
    """Hash a directory by sorted relative names and file bytes.

    This is provenance only.  It is intentionally kept in the adapter until
    MNCS has a portable directory enumeration primitive; the limitation is
    recorded as MNCS-TEST-P-002 / Commons tooling pressure.
    """

    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        return sha256_bytes(f"missing:{path}".encode())
    entries: list[tuple[str, str]] = []
    for root, directories, files in os.walk(path, followlinks=False):
        directories.sort()
        files.sort()
        root_path = Path(root)
        for name in files:
            item = root_path / name
            relative = item.relative_to(path).as_posix()
            try:
                item_hash = sha256_file(item)
            except OSError:
                item_hash = sha256_bytes(f"unreadable:{relative}".encode())
            entries.append((relative, item_hash))
    return sha256_bytes("".join(f"{name}\0{digest}\n" for name, digest in entries).encode())


def resolve_path(value: str | Path, base: Path, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (base / path).resolve()
    if must_exist and not resolved.exists():
        raise ManifestError(f"path does not exist: {value}")
    return resolved


def relative_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def function_location(source: str, entry: str, source_path: Path) -> dict[str, Any]:
    for match in FUNCTION_PATTERN.finditer(source):
        if match.group("name") == entry:
            line = source.count("\n", 0, match.start()) + 1
            previous = source.rfind("\n", 0, match.start())
            column = match.start() - previous
            return {
                "file": source_path.as_posix(),
                "line": line,
                "column": column,
                "symbol": entry,
            }
    return {"file": source_path.as_posix(), "symbol": entry}


def record_fields(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    record = value.get("record")
    if not isinstance(record, dict):
        return None
    fields = record.get("fields")
    if not isinstance(fields, list):
        return None
    output: dict[str, Any] = {}
    for pair in fields:
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
            output[pair[0]] = pair[1]
    return output


def integer_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        integer = value.get("integer")
        if isinstance(integer, dict) and isinstance(integer.get("value"), int):
            return integer["value"]
    return None


def boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        boolean = value.get("boolean")
        if isinstance(boolean, dict) and isinstance(boolean.get("value"), bool):
            return boolean["value"]
    return None


def finite_discriminant(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    finite = value.get("finite")
    if isinstance(finite, dict) and isinstance(finite.get("discriminant"), int):
        return finite["discriminant"]
    return None


def diagnostic_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        errors = value.get("errors")
        if isinstance(errors, list):
            return [item for item in errors if isinstance(item, dict)]
    return []


def diagnostic_matches(diagnostics: Iterable[dict[str, Any]], expected: list[str]) -> bool:
    if not expected:
        return bool(list(diagnostics))
    codes = [str(item.get("code", "")) for item in diagnostics]
    return all(any(code == wanted or code.startswith(wanted) for code in codes) for wanted in expected)


def parse_json_output(stdout: bytes) -> Any:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def native_test_result(execution: dict[str, Any]) -> dict[str, Any] | None:
    returned = execution.get("returned")
    if not isinstance(returned, list) or len(returned) != 1:
        return None
    fields = record_fields(returned[0])
    if fields is None:
        return None
    verdict_code = integer_value(fields.get("verdict_code"))
    failure_kind = finite_discriminant(fields.get("failure_kind"))
    if verdict_code not in (0, 1, 2, 3) or failure_kind is None:
        return None
    verdicts = {0: "PASS", 1: "FAIL", 2: "SKIP", 3: "UNSUPPORTED"}
    failure_kinds = {
        0: "none",
        1: "assertion",
        2: "setup",
        3: "compile",
        4: "runtime",
        5: "timeout",
        6: "unsupported",
        7: "infrastructure",
    }
    return {
        "verdict": verdicts[verdict_code],
        "verdict_code": verdict_code,
        "failure_kind": failure_kinds.get(failure_kind, "unknown"),
        "failure_kind_code": failure_kind,
        "failure_code": integer_value(fields.get("failure_code")),
        "assertions": integer_value(fields.get("assertions")),
        "failures": integer_value(fields.get("failures")),
        "expected": integer_value(fields.get("expected")),
        "actual": integer_value(fields.get("actual")),
        "assertion_code": integer_value(fields.get("assertion_code")),
    }


def native_suite_summary(execution: dict[str, Any]) -> dict[str, Any] | None:
    returned = execution.get("returned")
    if not isinstance(returned, list) or len(returned) != 1:
        return None
    fields = record_fields(returned[0])
    if fields is None:
        return None
    verdict_code = integer_value(fields.get("verdict_code"))
    if verdict_code not in (0, 1, 3):
        return None
    values: dict[str, Any] = {}
    for name in (
        "total",
        "passed",
        "failed",
        "skipped",
        "unsupported",
        "assertion_failures",
        "setup_failures",
        "compile_failures",
        "runtime_failures",
        "timeout_failures",
        "infrastructure_failures",
        "assertion_count",
        "failure_count",
    ):
        values[name] = integer_value(fields.get(name))
    if any(value is None for value in values.values()):
        return None
    return {
        "verdict": {0: "PASS", 1: "FAIL", 3: "UNKNOWN"}[verdict_code],
        "verdict_code": verdict_code,
        **values,
    }


def resolve_mncs(binary: str, cwd: Path) -> str:
    candidate = Path(binary)
    if candidate.is_absolute() or "/" in binary:
        return str((cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    return shutil.which(binary) or binary


def validate_manifest(raw: Any, manifest_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a TOML table")
    if raw.get("schema_version") != MANIFEST_SCHEMA:
        raise ManifestError(f"schema_version must be {MANIFEST_SCHEMA}")
    for field in ("name", "source", "module"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ManifestError(f"manifest field {field!r} must be a non-empty string")
    if "suite" in raw and raw["suite"] is not None and not isinstance(raw["suite"], str):
        raise ManifestError("suite must be a string when present")
    profile = raw.get("profile", "0.16")
    if not isinstance(profile, str) or not profile:
        raise ManifestError("profile must be a non-empty string")
    for field in ("step_budget", "timeout_seconds"):
        if field in raw and (not isinstance(raw[field], int) or isinstance(raw[field], bool) or raw[field] <= 0):
            raise ManifestError(f"{field} must be a positive integer")
    libraries = raw.get("libraries", [])
    if not isinstance(libraries, list) or not all(isinstance(item, str) and item for item in libraries):
        raise ManifestError("libraries must be an array of non-empty strings")
    tests = raw.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ManifestError("tests must be a non-empty array")
    seen: set[str] = set()
    normalized_tests: list[dict[str, Any]] = []
    for index, item in enumerate(tests):
        if not isinstance(item, dict):
            raise ManifestError(f"tests[{index}] must be a table")
        test_id = item.get("id")
        entry = item.get("entry")
        kind = item.get("kind", "unit")
        if not isinstance(test_id, str) or not test_id.strip() or test_id in seen:
            raise ManifestError(f"tests[{index}].id must be unique and non-empty")
        if not isinstance(entry, str) or not entry.strip():
            raise ManifestError(f"tests[{index}].entry must be a non-empty string")
        if kind not in TEST_KINDS:
            raise ManifestError(f"tests[{index}].kind is unsupported: {kind!r}")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ManifestError(f"tests[{index}].tags must be an array of strings")
        arguments = item.get("arguments", [])
        if not isinstance(arguments, list):
            raise ManifestError(f"tests[{index}].arguments must be an array")
        for numeric in ("step_budget", "timeout_seconds", "seed", "case_index"):
            if numeric in item and (not isinstance(item[numeric], int) or isinstance(item[numeric], bool) or item[numeric] < 0):
                raise ManifestError(f"tests[{index}].{numeric} must be a non-negative integer")
        expected_status = item.get("expected_status")
        if expected_status is not None and expected_status not in EXECUTION_STATUSES:
            raise ManifestError(f"tests[{index}].expected_status is invalid: {expected_status!r}")
        diagnostic_codes = item.get("diagnostic_codes", [])
        if not isinstance(diagnostic_codes, list) or not all(
            isinstance(code, str) and DIAGNOSTIC_CODE.match(code) for code in diagnostic_codes
        ):
            raise ManifestError(f"tests[{index}].diagnostic_codes must contain diagnostic code prefixes")
        seen.add(test_id)
        normalized = dict(item)
        normalized.setdefault("kind", "unit")
        normalized.setdefault("tags", [])
        normalized.setdefault("arguments", [])
        normalized.setdefault("diagnostic_codes", [])
        normalized_tests.append(normalized)
    source_path = resolve_path(raw["source"], manifest_path.parent, must_exist=True)
    if not source_path.is_file():
        raise ManifestError(f"source is not a file: {raw['source']}")
    library_paths = [resolve_path(item, manifest_path.parent, must_exist=True) for item in libraries]
    if any(not path.is_dir() for path in library_paths):
        raise ManifestError("every library path must be a directory")
    result = dict(raw)
    result["manifest_path"] = manifest_path.resolve()
    result["source_path"] = source_path
    result["library_paths"] = library_paths
    result["tests"] = normalized_tests
    result.setdefault("profile", profile)
    result.setdefault("step_budget", 100000)
    result.setdefault("timeout_seconds", 60)
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"invalid TOML in {path}: {error}") from error
    return validate_manifest(raw, path.resolve())


def read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"cannot read source {path}: {error}") from error


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: list[dict[str, str]] = []

    def write(self, relative: str, payload: bytes, kind: str) -> str:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise AdapterError(f"unsafe artifact path: {relative}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = sha256_bytes(payload)
        item = {"path": path.relative_to(self.root).as_posix(), "kind": kind, "sha256": digest}
        self.items.append(item)
        return item["path"]


def command_environment(library_paths: list[Path], inherited: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if inherited is None else inherited)
    if library_paths:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(str(path) for path in library_paths)
    return environment


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    artifacts: ArtifactStore,
    artifact_key: str,
) -> dict[str, Any]:
    command_record = {"argv": argv, "cwd": str(cwd), "timeout_seconds": timeout_seconds}
    command_path = artifacts.write(
        f"commands/{artifact_key}.json", json_bytes(command_record), "command"
    )
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=environment,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, bytes) else (error.stdout or "").encode()
        stderr = error.stderr if isinstance(error.stderr, bytes) else (error.stderr or "").encode()
        stdout_path = artifacts.write(f"stdout/{artifact_key}.out", stdout, "stdout")
        stderr_path = artifacts.write(f"stderr/{artifact_key}.err", stderr, "stderr")
        return {
            "timed_out": True,
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_artifact": stdout_path,
            "stderr_artifact": stderr_path,
            "command_artifact": command_path,
        }
    except (OSError, ValueError) as error:
        return {
            "transport_error": str(error),
            "timed_out": False,
            "returncode": None,
            "stdout": b"",
            "stderr": str(error).encode("utf-8", errors="replace"),
            "stdout_artifact": artifacts.write(f"stdout/{artifact_key}.out", b"", "stdout"),
            "stderr_artifact": artifacts.write(
                f"stderr/{artifact_key}.err", str(error).encode("utf-8"), "stderr"
            ),
            "command_artifact": command_path,
        }
    stdout_path = artifacts.write(f"stdout/{artifact_key}.out", completed.stdout, "stdout")
    stderr_path = artifacts.write(f"stderr/{artifact_key}.err", completed.stderr, "stderr")
    return {
        "timed_out": False,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "stdout_artifact": stdout_path,
        "stderr_artifact": stderr_path,
        "command_artifact": command_path,
    }


def expected_status_for(test: dict[str, Any]) -> str | None:
    if test.get("expected_status") is not None:
        return str(test["expected_status"])
    if test.get("kind") == "runtime-failure":
        return "runtime_failure"
    return None


def base_test_result(test: dict[str, Any], source_path: Path, source_text: str) -> dict[str, Any]:
    return {
        "id": test["id"],
        "entry": test["entry"],
        "kind": test["kind"],
        "tags": list(test.get("tags", [])),
        "source": source_path.as_posix(),
        "location": function_location(source_text, test["entry"], source_path),
        "verdict": "PASS",
        "status": "pending",
    }


def result_failure(
    test_result: dict[str, Any],
    *,
    failure_class: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    test_result["verdict"] = "FAIL"
    test_result["status"] = "failed"
    test_result["failure"] = {"class": failure_class, "message": message, **details}
    return test_result


def execute_entry(
    *,
    test: dict[str, Any],
    source_path: Path,
    source_text: str,
    module: str,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    default_step_budget: int,
    default_timeout: int,
    artifacts: ArtifactStore,
    artifact_key: str,
    suite: bool = False,
) -> dict[str, Any]:
    entry = test["entry"]
    step_budget = int(test.get("step_budget", default_step_budget))
    timeout_seconds = int(test.get("timeout_seconds", default_timeout))
    request: dict[str, Any] = {
        "schema_version": "0.1",
        "target": {"module": module, "function": entry},
        "arguments": test.get("arguments", []),
        "step_budget": step_budget,
    }
    if "type_arguments" in test:
        request["type_arguments"] = test["type_arguments"]
    request_path = artifacts.root / "requests" / f"{artifact_key}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_bytes(json_bytes(request))
    request_artifact = {
        "path": request_path.relative_to(artifacts.root).as_posix(),
        "kind": "execution-request",
        "sha256": sha256_file(request_path),
    }
    artifacts.items.append(request_artifact)
    process = run_process(
        [mncs, "execute", str(source_path), str(request_path)],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        artifacts=artifacts,
        artifact_key=artifact_key,
    )
    test_result = base_test_result(test, source_path, source_text)
    test_result["request"] = request
    test_result["request_artifact"] = request_artifact["path"]
    test_result["stdout_artifact"] = process["stdout_artifact"]
    test_result["stderr_artifact"] = process["stderr_artifact"]
    test_result["command_artifact"] = process["command_artifact"]
    if process.get("transport_error"):
        return result_failure(
            test_result,
            failure_class="infrastructure_failure",
            message="MNCS process could not be started",
            error=process["transport_error"],
        )
    if process["timed_out"]:
        return result_failure(
            test_result,
            failure_class="timeout",
            message=f"MNCS execution exceeded {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
        )
    decoded = parse_json_output(process["stdout"])
    test_result["process_exit_code"] = process["returncode"]
    if not isinstance(decoded, dict):
        diagnostics = diagnostic_list(decoded)
        if process["returncode"] not in (0, None) and diagnostics:
            return result_failure(
                test_result,
                failure_class="compile_failure",
                message="MNCS rejected the source before execution",
                diagnostics=diagnostics,
            )
        return result_failure(
            test_result,
            failure_class="infrastructure_failure",
            message="MNCS did not emit a structured execution result",
            diagnostics=diagnostics,
        )
    status = decoded.get("status")
    test_result["execution_status"] = status
    test_result["execution"] = {
        key: decoded.get(key)
        for key in (
            "schema_version",
            "target",
            "program_identity",
            "program_fingerprint",
            "function_identity",
            "steps",
            "failure",
            "effects",
        )
        if key in decoded
    }
    if status not in EXECUTION_STATUSES:
        return result_failure(
            test_result,
            failure_class="infrastructure_failure",
            message="MNCS emitted an unknown execution status",
            observed_status=status,
        )
    expected_status = expected_status_for(test)
    if status == "returned":
        native = native_test_result(decoded)
        if suite:
            native = native_suite_summary(decoded)
        if native is None:
            return result_failure(
                test_result,
                failure_class="malformed_test_result",
                message="returned value is not the declared native test result shape",
            )
        test_result["native_result"] = native
        if suite:
            test_result["status"] = "passed" if native["verdict"] == "PASS" else "returned"
            test_result["verdict"] = {
                "PASS": "PASS",
                "FAIL": "FAIL",
                "UNKNOWN": "UNKNOWN",
            }[native["verdict"]]
            return test_result
        verdict = native["verdict"]
        if verdict == "PASS":
            test_result["status"] = "passed"
            test_result["verdict"] = "PASS"
        elif verdict == "SKIP":
            test_result["status"] = "skipped"
            test_result["verdict"] = "PASS"
        elif verdict == "UNSUPPORTED":
            test_result["status"] = "unsupported"
            test_result["verdict"] = "UNKNOWN"
            test_result["failure"] = {
                "class": "unsupported",
                "message": "native test reported an unsupported capability",
                "code": native.get("failure_code"),
            }
        else:
            return result_failure(
                test_result,
                failure_class=native.get("failure_kind", "assertion"),
                message="native assertion failed",
                expected=native.get("expected"),
                actual=native.get("actual"),
                assertion_code=native.get("assertion_code"),
                native_result=native,
            )
        if expected_status and expected_status != status:
            return result_failure(
                test_result,
                failure_class="expectation_mismatch",
                message=f"expected execution status {expected_status}, got {status}",
                expected_status=expected_status,
                observed_status=status,
            )
        return test_result
    if expected_status == status:
        test_result["status"] = "expected_failure"
        test_result["verdict"] = "PASS"
        test_result["expected_failure"] = True
        test_result["failure"] = {
            "class": status,
            "message": f"expected MNCS execution status {status}",
            "expected": True,
        }
        return test_result
    if status == "unsupported":
        test_result["verdict"] = "UNKNOWN"
        test_result["status"] = "unsupported"
        test_result["failure"] = {
            "class": "unsupported",
            "message": "MNCS reported an unsupported capability",
        }
        return test_result
    if status == "budget_exhausted":
        return result_failure(
            test_result,
            failure_class="timeout",
            message="MNCS exhausted the declared step budget",
            step_budget=step_budget,
        )
    if status == "runtime_failure":
        return result_failure(
            test_result,
            failure_class="runtime_failure",
            message="MNCS test entrypoint terminated with a runtime failure",
            execution_failure=decoded.get("failure"),
        )
    return result_failure(
        test_result,
        failure_class="malformed_test_declaration",
        message="MNCS rejected the execution request",
        execution_failure=decoded.get("failure"),
    )


def compile_entry(
    *,
    test: dict[str, Any],
    source_path: Path,
    source_text: str,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    default_timeout: int,
    artifacts: ArtifactStore,
    artifact_key: str,
) -> dict[str, Any]:
    timeout_seconds = int(test.get("timeout_seconds", default_timeout))
    process = run_process(
        [mncs, "validate", str(source_path)],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        artifacts=artifacts,
        artifact_key=artifact_key,
    )
    test_result = base_test_result(test, source_path, source_text)
    test_result["stdout_artifact"] = process["stdout_artifact"]
    test_result["stderr_artifact"] = process["stderr_artifact"]
    test_result["command_artifact"] = process["command_artifact"]
    if process.get("transport_error"):
        return result_failure(
            test_result,
            failure_class="infrastructure_failure",
            message="MNCS compiler could not be started",
            error=process["transport_error"],
        )
    if process["timed_out"]:
        return result_failure(
            test_result,
            failure_class="timeout",
            message=f"MNCS validation exceeded {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
        )
    decoded = parse_json_output(process["stdout"])
    diagnostics = diagnostic_list(decoded)
    test_result["process_exit_code"] = process["returncode"]
    test_result["diagnostics"] = diagnostics
    passed_compile = process["returncode"] == 0 and (
        isinstance(decoded, dict) and decoded.get("valid") is True
        or isinstance(decoded, dict) and "valid" not in decoded
    )
    kind = test["kind"]
    if kind in {"compile-fail", "diagnostic"}:
        if passed_compile:
            return result_failure(
                test_result,
                failure_class="compile_expectation_mismatch",
                message="source compiled but the manifest expected a diagnostic",
            )
        expected_codes = list(test.get("diagnostic_codes", []))
        if not diagnostic_matches(diagnostics, expected_codes):
            return result_failure(
                test_result,
                failure_class="diagnostic_mismatch",
                message="compiler diagnostics did not match the manifest",
                expected_codes=expected_codes,
                observed_codes=[str(item.get("code", "")) for item in diagnostics],
            )
        test_result["status"] = "expected_failure"
        test_result["verdict"] = "PASS"
        test_result["expected_failure"] = True
        return test_result
    if passed_compile:
        test_result["status"] = "passed"
        test_result["verdict"] = "PASS"
        return test_result
    return result_failure(
        test_result,
        failure_class="compile_failure",
        message="source did not compile",
        diagnostics=diagnostics,
    )


def classification_for_failure(failure_class: str) -> str:
    if failure_class in {"assertion", "expectation_mismatch", "diagnostic_mismatch", "compile_expectation_mismatch"}:
        return "test_failure"
    if failure_class in {"runtime_failure"}:
        return "runtime_failure"
    if failure_class in {"setup"}:
        return "setup_failure"
    if failure_class in {"compile_failure", "diagnostic_mismatch"}:
        return "compile_failure"
    if failure_class == "timeout":
        return "timeout"
    if failure_class == "unsupported":
        return "unsupported"
    if failure_class in {"malformed_test_declaration", "malformed_test_result", "infrastructure_failure"}:
        return "infrastructure_failure"
    return "test_failure"


def exit_code_for(classification: str, *, allow_unsupported: bool = False) -> int:
    if classification == "success":
        return EXIT_SUCCESS
    if classification == "test_failure" or classification in {"runtime_failure", "setup_failure"}:
        return EXIT_TEST_FAILURE
    if classification == "invalid_invocation":
        return EXIT_INVALID_INVOCATION
    if classification == "infrastructure_failure":
        return EXIT_INFRASTRUCTURE_FAILURE
    if classification == "compile_failure":
        return EXIT_COMPILE_FAILURE
    if classification == "timeout":
        return EXIT_TIMEOUT
    if classification == "unsupported":
        return EXIT_SUCCESS if allow_unsupported else EXIT_UNSUPPORTED
    return EXIT_INFRASTRUCTURE_FAILURE


def summarize_tests(test_results: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(test_results),
        "passed": sum(item.get("verdict") == "PASS" and item.get("status") not in {"skipped"} for item in test_results),
        "failed": sum(item.get("verdict") == "FAIL" for item in test_results),
        "skipped": sum(item.get("status") == "skipped" for item in test_results),
        "unsupported": sum(item.get("verdict") == "UNKNOWN" for item in test_results),
        "assertion_failures": 0,
        "setup_failures": 0,
        "compile_failures": 0,
        "runtime_failures": 0,
        "timeout_failures": 0,
        "infrastructure_failures": 0,
        "assertion_count": 0,
        "failure_count": 0,
    }
    for item in test_results:
        native = item.get("native_result")
        if isinstance(native, dict):
            summary["assertion_count"] += int(native.get("assertions") or 0)
            summary["failure_count"] += int(native.get("failures") or 0)
        failure = item.get("failure")
        if isinstance(failure, dict):
            failure_class = str(failure.get("class", ""))
            if failure_class == "assertion":
                summary["assertion_failures"] += 1
            elif failure_class == "setup":
                summary["setup_failures"] += 1
            elif failure_class in {"compile_failure", "compile_expectation_mismatch", "diagnostic_mismatch"}:
                summary["compile_failures"] += 1
            elif failure_class == "runtime_failure":
                summary["runtime_failures"] += 1
            elif failure_class == "timeout":
                summary["timeout_failures"] += 1
            elif failure_class in {"infrastructure_failure", "malformed_test_declaration", "malformed_test_result"}:
                summary["infrastructure_failures"] += 1
    return {key: int(value) for key, value in summary.items()}


def manifest_provenance(manifest: dict[str, Any], source_text: str, mncs: str, library_paths: list[Path]) -> dict[str, Any]:
    manifest_path = Path(manifest["manifest_path"])
    source_path = Path(manifest["source_path"])
    compiler_path = Path(mncs)
    compiler: dict[str, Any] = {"requested": mncs}
    if compiler_path.is_file():
        compiler["path"] = str(compiler_path.resolve())
        compiler["sha256"] = sha256_file(compiler_path)
        try:
            version = subprocess.run(
                [str(compiler_path), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            compiler["version"] = version.stdout.strip() or version.stderr.strip()
        except (OSError, subprocess.SubprocessError):
            compiler["version"] = "unavailable"
    else:
        compiler["path"] = mncs
        compiler["sha256"] = None
        compiler["version"] = "unavailable"
    return {
        "runner": {
            "name": "mncs-test",
            "version": RUNNER_VERSION,
            "adapter": "python-stdlib-process-transport",
            "source_sha256": sha256_file(Path(__file__)),
        },
        "language_profile": manifest["profile"],
        "compiler": compiler,
        "source": {"path": str(source_path), "sha256": sha256_file(source_path)},
        "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "libraries": [{"path": str(path), "sha256": hash_tree(path)} for path in library_paths],
        "host": {"os": platform.system(), "release": platform.release(), "arch": platform.machine()},
        "source_bytes": len(source_text.encode("utf-8")),
    }


def make_result(
    *,
    manifest: dict[str, Any],
    source_text: str,
    mncs: str,
    cwd: Path,
    library_paths: list[Path],
    artifacts: ArtifactStore,
    test_results: list[dict[str, Any]],
    suite_result: dict[str, Any] | None,
    suite_summary: dict[str, Any] | None,
    classification: str,
    failure_class: str,
    message: str | None,
    allow_unsupported: bool,
) -> dict[str, Any]:
    source_path = Path(manifest["source_path"])
    if suite_summary is not None:
        summary = {
            key: int(suite_summary[key])
            for key in (
                "total",
                "passed",
                "failed",
                "skipped",
                "unsupported",
                "assertion_failures",
                "setup_failures",
                "compile_failures",
                "runtime_failures",
                "timeout_failures",
                "infrastructure_failures",
                "assertion_count",
                "failure_count",
            )
        }
        summary["authority"] = "native_suite"
    else:
        summary = summarize_tests(test_results)
        summary["authority"] = "manifest_compile_or_adapter_projection"
    if classification == "success":
        verdict = "PASS"
    elif classification == "unsupported":
        verdict = "UNKNOWN"
    else:
        verdict = "FAIL"
    provenance = manifest_provenance(manifest, source_text, mncs, library_paths)
    identity_material = {
        "manifest": provenance["manifest"],
        "source": provenance["source"],
        "compiler": provenance["compiler"],
        "profile": manifest["profile"],
        "tests": [item["id"] for item in test_results],
    }
    run_id = sha256_bytes(compact_json(identity_material).encode())
    native_suite_payload = None
    if suite_summary is not None:
        native_suite_payload = {**suite_summary, "authority": "native_suite"}
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "protocol_version": 1,
        "id": "mncs-test",
        "provider": "mncs-test",
        "verdict": verdict,
        "classification": classification,
        "failure_class": failure_class,
        "exit_code": exit_code_for(classification, allow_unsupported=allow_unsupported),
        "run_id": run_id,
        "scope": {
            "manifest": str(manifest["name"]),
            "manifest_path": relative_path(Path(manifest["manifest_path"]), cwd),
            "source": relative_path(source_path, cwd),
            "module": manifest["module"],
            "suite": manifest.get("suite"),
        },
        "summary": summary,
        "tests": test_results,
        "suite": suite_result,
        "native_suite_summary": native_suite_payload,
        "provenance": provenance,
        "artifacts": artifacts.items,
        "reproduction": {
            "command": shlex.join(["mncs-test", "run", "--manifest", str(manifest["manifest_path"])]),
            "replay_command": shlex.join(["mncs-test", "replay", "--result", "<result-file>"]),
            "cwd": str(cwd),
            "run_id": run_id,
            "step_budget": manifest.get("step_budget"),
            "timeout_seconds": manifest.get("timeout_seconds"),
            "seed": next((item.get("seed") for item in manifest["tests"] if "seed" in item), None),
        },
    }
    if message:
        result["failure"] = {"class": failure_class, "message": message}
    return result


def minimal_result(*, classification: str, message: str, cwd: Path) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "protocol_version": 1,
        "id": "mncs-test",
        "provider": "mncs-test",
        "verdict": "UNKNOWN" if classification in {"invalid_invocation", "unsupported", "infrastructure_failure"} else "FAIL",
        "classification": classification,
        "failure_class": classification,
        "exit_code": exit_code_for(classification),
        "run_id": sha256_bytes(message.encode()),
        "scope": {"manifest": None, "cwd": str(cwd)},
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "unsupported": 0, "authority": "adapter"},
        "tests": [],
        "suite": None,
        "native_suite_summary": None,
        "provenance": {"runner": {"name": "mncs-test", "version": RUNNER_VERSION}},
        "artifacts": [],
        "reproduction": {"command": "mncs-test validate-manifest --manifest <manifest>"},
        "failure": {"class": classification, "message": message},
    }


def check_result(result: dict[str, Any], result_digest: str) -> dict[str, Any]:
    unresolved = []
    if result["verdict"] == "UNKNOWN":
        unresolved.append(result.get("failure", {}).get("message", "unsupported capability"))
    return {
        "schema_version": CHECK_SCHEMA,
        "id": result["id"],
        "provider": result["provider"],
        "verdict": result["verdict"],
        "scope": result.get("scope", {}).get("manifest", "mncs-test"),
        "claim": "MNCS-native tests were executed under the declared manifest and profile",
        "summary": f"{result['classification']}: {result.get('summary', {}).get('passed', 0)} passed, {result.get('summary', {}).get('failed', 0)} failed, {result.get('summary', {}).get('skipped', 0)} skipped",
        "contract_revision": RESULT_SCHEMA,
        "producer_revision": f"sha256:{result.get('provenance', {}).get('runner', {}).get('source_sha256', '')}",
        "digest": f"sha256:{result_digest}",
        "unresolved": unresolved,
        "references": [{"kind": "mncs-test-result", "uri": f"urn:mncs-test:{result.get('run_id', '')}", "digest": f"sha256:{result_digest}"}],
        "test_result": result,
        "classification": result["classification"],
        "failure_class": result["failure_class"],
    }


def write_output(path: Path | None, value: Any) -> str | None:
    if path is None:
        return None
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_bytes(value)
    path.write_bytes(payload)
    return sha256_bytes(payload)


def result_paths(args: argparse.Namespace, cwd: Path) -> tuple[Path | None, Path | None, Path]:
    result_path = resolve_path(args.result, cwd) if args.result else None
    check_path = resolve_path(args.check_result, cwd) if args.check_result else None
    artifact_root = resolve_path(args.artifacts, cwd)
    return result_path, check_path, artifact_root


def print_result(result: dict[str, Any], output_format: str) -> None:
    if output_format == "text":
        summary = result.get("summary", {})
        print(
            f"{result.get('id', 'mncs-test')}: {result.get('verdict')} "
            f"({result.get('classification')}) — "
            f"{summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed, "
            f"{summary.get('skipped', 0)} skipped, {summary.get('unsupported', 0)} unsupported"
        )
        for item in result.get("tests", []):
            suffix = ""
            if isinstance(item.get("failure"), dict):
                suffix = f": {item['failure'].get('message', '')}"
            print(f"  {item.get('id')}: {item.get('status')} [{item.get('verdict')}]" + suffix)
        if result.get("failure"):
            print(f"failure: {result['failure'].get('message', '')}")
        return
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


def run_manifest(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    result_path, check_path, artifact_root = result_paths(args, cwd)
    manifest_path = resolve_path(args.manifest, cwd)
    try:
        manifest = load_manifest(manifest_path)
        source_text = read_source(Path(manifest["source_path"]))
        mncs = resolve_mncs(args.mncs, cwd)
        library_paths = list(manifest["library_paths"])
        for raw_path in args.library or []:
            for component in raw_path.split(os.pathsep):
                if component:
                    library_paths.append(resolve_path(component, cwd, must_exist=True))
        deduplicated_libraries: list[Path] = []
        seen_libraries: set[Path] = set()
        for library_path in library_paths:
            resolved_library = library_path.resolve()
            if resolved_library not in seen_libraries:
                seen_libraries.add(resolved_library)
                deduplicated_libraries.append(resolved_library)
        library_paths = deduplicated_libraries
        environment = command_environment(library_paths)
        artifacts = ArtifactStore(artifact_root)
        suite_result: dict[str, Any] | None = None
        suite_summary: dict[str, Any] | None = None
        test_results: list[dict[str, Any]] = []
        if manifest.get("suite"):
            suite_test = {
                "id": "__suite__",
                "entry": manifest["suite"],
                "kind": "suite",
                "arguments": [],
                "step_budget": args.step_budget or manifest["step_budget"],
                "timeout_seconds": args.timeout_seconds or manifest["timeout_seconds"],
                "tags": ["suite"],
            }
            suite_result = execute_entry(
                test=suite_test,
                source_path=Path(manifest["source_path"]),
                source_text=source_text,
                module=manifest["module"],
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                default_step_budget=args.step_budget or manifest["step_budget"],
                default_timeout=args.timeout_seconds or manifest["timeout_seconds"],
                artifacts=artifacts,
                artifact_key="suite",
                suite=True,
            )
            suite_summary = suite_result.get("native_result") if suite_result else None
            if suite_result.get("verdict") == "FAIL" and suite_result.get("failure"):
                # The suite's compilation/transport failure is authoritative;
                # individual entries cannot be meaningfully interpreted.
                test_results = [
                    result_failure(
                        base_test_result(test, Path(manifest["source_path"]), source_text),
                        failure_class="infrastructure_failure",
                        message="suite entry did not produce a native summary",
                    )
                    for test in manifest["tests"]
                ]
        for index, test in enumerate(manifest["tests"]):
            if suite_result and suite_result.get("failure") and suite_result.get("status") == "failed":
                break
            if test["kind"] == "skip":
                test_result = base_test_result(test, Path(manifest["source_path"]), source_text)
                test_result["status"] = "skipped"
                test_result["verdict"] = "PASS"
            elif test["kind"] == "unsupported":
                test_result = base_test_result(test, Path(manifest["source_path"]), source_text)
                test_result["status"] = "unsupported"
                test_result["verdict"] = "UNKNOWN"
                test_result["failure"] = {
                    "class": "unsupported",
                    "message": "manifest marks this capability as unsupported",
                }
            elif test["kind"] in {"compile-pass", "compile-fail", "diagnostic"}:
                test_result = compile_entry(
                    test=test,
                    source_path=Path(manifest["source_path"]),
                    source_text=source_text,
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    default_timeout=args.timeout_seconds or manifest["timeout_seconds"],
                    artifacts=artifacts,
                    artifact_key=f"test-{index:03d}-{test['id']}",
                )
            else:
                test_result = execute_entry(
                    test=test,
                    source_path=Path(manifest["source_path"]),
                    source_text=source_text,
                    module=manifest["module"],
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    default_step_budget=args.step_budget or manifest["step_budget"],
                    default_timeout=args.timeout_seconds or manifest["timeout_seconds"],
                    artifacts=artifacts,
                    artifact_key=f"test-{index:03d}-{test['id']}",
                )
            test_results.append(test_result)
        classification = "success"
        failure_class = "none"
        message: str | None = None
        if suite_result and suite_result.get("failure"):
            failure = suite_result["failure"]
            failure_class = str(failure.get("class", "infrastructure_failure"))
            classification = classification_for_failure(failure_class)
            message = str(failure.get("message", "native suite failed"))
        elif suite_summary is not None:
            if suite_summary["verdict"] == "FAIL":
                classification = "test_failure"
                failure_class = next(
                    (
                        str(item.get("failure", {}).get("class"))
                        for item in test_results
                        if isinstance(item.get("failure"), dict)
                    ),
                    "assertion",
                )
                message = "native suite returned FAIL"
            elif suite_summary["verdict"] == "UNKNOWN":
                classification = "unsupported"
                failure_class = "unsupported"
                message = "native suite returned UNSUPPORTED"
            elif suite_summary["total"] != len(test_results):
                classification = "infrastructure_failure"
                failure_class = "native_summary_mismatch"
                message = "native suite total does not match manifest entries"
            elif any(item.get("verdict") != "PASS" for item in test_results):
                classification = "infrastructure_failure"
                failure_class = "native_summary_mismatch"
                message = "native suite PASS disagrees with an individual entry result"
        if classification == "success":
            for item in test_results:
                if item.get("verdict") == "FAIL":
                    failure = item.get("failure", {})
                    failure_class = str(failure.get("class", "test_failure"))
                    classification = classification_for_failure(failure_class)
                    message = str(failure.get("message", "test failed"))
                    break
                if item.get("verdict") == "UNKNOWN":
                    classification = "unsupported"
                    failure_class = "unsupported"
                    message = str(item.get("failure", {}).get("message", "unsupported capability"))
                    break
        result = make_result(
            manifest=manifest,
            source_text=source_text,
            mncs=mncs,
            cwd=cwd,
            library_paths=library_paths,
            artifacts=artifacts,
            test_results=test_results,
            suite_result=suite_result,
            suite_summary=suite_summary,
            classification=classification,
            failure_class=failure_class,
            message=message,
            allow_unsupported=args.allow_unsupported,
        )
    except ManifestError as error:
        result = minimal_result(classification="invalid_invocation", message=str(error), cwd=cwd)
    except AdapterError as error:
        result = minimal_result(classification="infrastructure_failure", message=str(error), cwd=cwd)
    if result_path:
        result_digest = write_output(result_path, result)
    else:
        result_digest = sha256_bytes(json_bytes(result))
    if check_path:
        write_output(check_path, check_result(result, result_digest or sha256_bytes(json_bytes(result))))
    print_result(result, args.format)
    return int(result["exit_code"])


def discover_manifests(root: Path, recursive: bool) -> list[Path]:
    candidates: list[Path] = []
    direct = root / "mncs-test.toml"
    if direct.is_file():
        candidates.append(direct)
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        candidates.extend(sorted(path for path in tests_dir.glob("*.toml") if path.is_file()))
    if recursive:
        candidates.extend(sorted(path for path in root.rglob("mncs-test.toml") if path.is_file() and path != direct))
    return sorted(set(path.resolve() for path in candidates))


def discover_command(args: argparse.Namespace) -> int:
    root = resolve_path(args.root, Path.cwd())
    manifests = []
    errors = []
    for path in discover_manifests(root, args.recursive):
        try:
            manifest = load_manifest(path)
            manifests.append(
                {
                    "path": relative_path(path, Path.cwd()),
                    "name": manifest["name"],
                    "source": relative_path(Path(manifest["source_path"]), Path.cwd()),
                    "module": manifest["module"],
                    "suite": manifest.get("suite"),
                    "tests": [
                        {"id": item["id"], "entry": item["entry"], "kind": item["kind"], "tags": item.get("tags", [])}
                        for item in manifest["tests"]
                    ],
                }
            )
        except ManifestError as error:
            errors.append({"path": relative_path(path, Path.cwd()), "error": str(error)})
    output = {"schema_version": DISCOVERY_SCHEMA, "root": str(root), "manifests": manifests, "errors": errors}
    if args.format == "text":
        for manifest in manifests:
            print(f"{manifest['path']}: {manifest['name']} ({len(manifest['tests'])} tests)")
        for error in errors:
            print(f"{error['path']}: INVALID — {error['error']}")
    else:
        print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
    return EXIT_INVALID_INVOCATION if errors else EXIT_SUCCESS


def validate_manifest_command(args: argparse.Namespace) -> int:
    try:
        manifest_path = resolve_path(args.manifest, Path.cwd())
        manifest = load_manifest(manifest_path)
        output = {
            "schema_version": MANIFEST_SCHEMA,
            "valid": True,
            "name": manifest["name"],
            "source": str(manifest["source_path"]),
            "module": manifest["module"],
            "tests": len(manifest["tests"]),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return EXIT_SUCCESS
    except ManifestError as error:
        print(json.dumps({"schema_version": MANIFEST_SCHEMA, "valid": False, "error": str(error)}, indent=2, sort_keys=True))
        return EXIT_INVALID_INVOCATION


def replay_command(args: argparse.Namespace) -> int:
    path = resolve_path(args.result, Path.cwd())
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"mncs-test replay: cannot read structured result: {error}", file=sys.stderr)
        return EXIT_INVALID_INVOCATION
    if not isinstance(result, dict) or result.get("schema_version") != RESULT_SCHEMA:
        print("mncs-test replay: result is not mncs.test-result/1", file=sys.stderr)
        return EXIT_INVALID_INVOCATION
    if args.format == "text":
        print(result.get("reproduction", {}).get("command", ""))
        print(result.get("reproduction", {}).get("replay_command", ""))
    else:
        print(json.dumps({"schema_version": "mncs.test-replay/1", "result": result, "execute": False}, indent=2, sort_keys=True, ensure_ascii=False))
    return EXIT_SUCCESS


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mncs-test", description="Native MNCS testing, verification, and conformance")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one explicit MNCS test manifest")
    run.add_argument("--manifest", default="mncs-test.toml")
    run.add_argument("--mncs", default="mncs")
    run.add_argument("--library", action="append", default=[], help="additional MNCS library root; may be repeated")
    run.add_argument("--result", default=".mncs/mncs-test-result.json")
    run.add_argument("--check-result", default=".mncs/mncs-test-check.json")
    run.add_argument("--artifacts", default=".mncs/mncs-test-artifacts")
    run.add_argument("--format", choices=("json", "text"), default="json")
    run.add_argument("--step-budget", type=int)
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--allow-unsupported", action="store_true")
    run.set_defaults(handler=run_manifest)

    discover = commands.add_parser("discover", help="inspect explicit manifests without executing them")
    discover.add_argument("--root", default=".")
    discover.add_argument("--recursive", action="store_true", help="include nested mncs-test.toml files")
    discover.add_argument("--format", choices=("json", "text"), default="json")
    discover.set_defaults(handler=discover_command)

    validate = commands.add_parser("validate-manifest", help="validate one manifest")
    validate.add_argument("--manifest", default="mncs-test.toml")
    validate.set_defaults(handler=validate_manifest_command)

    replay = commands.add_parser("replay", help="show a safe reproduction record; never executes it")
    replay.add_argument("--result", required=True)
    replay.add_argument("--format", choices=("json", "text"), default="json")
    replay.set_defaults(handler=replay_command)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
