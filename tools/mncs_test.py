#!/usr/bin/env python3
"""The narrow transport adapter for the native MNCS test framework.

The semantic core lives in ``native/mncs/test`` and the declaration/inventory
authority lives in the MNCS compiler.  This module is deliberately limited to
TOML/file discovery, process supervision, bounded request/response transport,
retained embed-session transport, and preservation of raw artifacts.  It never
invents assertions, generates property values, or recomputes a native verdict.
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
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Iterable


RESULT_SCHEMA = "mncs.test-result/1"
CHECK_SCHEMA = "mncs.check-result/1"
MANIFEST_SCHEMA = "mncs.test-manifest/1"
DISCOVERY_SCHEMA = "mncs.test-discovery/1"
INVENTORY_SCHEMA = "mncs.test-inventory/1"
EXPERIMENT_PROJECTION_SCHEMA = "mncs.test-experiment/1"
VERIFICATION_PLAN_SCHEMA = "mncs.verification-plan/1"
RUNNER_VERSION = "0.2.0"

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

VERIFICATION_LEVELS = (
    "changed_item",
    "direct_dependents",
    "affected_subsystem",
    "repository_canonical",
    "family",
)
ESCALATION_REASONS = {
    "public_contract_changed",
    "shared_type_changed",
    "parser_semantics_changed",
    "serialization_format_changed",
    "effect_semantics_changed",
    "abi_boundary_changed",
    "canonical_fixture_changed",
    "high_connectivity_definition_changed",
    "dependent_targeted_test_failed",
    "insufficient_diagnostic_evidence",
    "migration_broad_semantic_surface",
    "language_profile_changed",
    "cross_repository_contract_changed",
    "impact_evidence_truncated",
    "unknown_changed_identity",
}


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


def compiler_location(test: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """Return source navigation supplied by the compiler inventory.

    Legacy manifest entries intentionally have only a symbol fallback.  The
    normal first-class path never derives a location by regex or by reparsing
    MNCS source in Python.
    """

    span = test.get("source_span")
    if isinstance(span, dict):
        location = {
            "file": source_path.as_posix(),
            "symbol": test.get("entry"),
            "authority": "mncs-compiler-test-inventory",
        }
        for field in ("start", "end", "line", "column"):
            if field in span:
                location[field] = span[field]
        return location
    return {
        "file": source_path.as_posix(),
        "symbol": test.get("entry"),
        "authority": "manifest-compatibility",
    }


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
        if not isinstance(errors, list):
            errors = value.get("diagnostics")
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
    profile = raw.get("profile", "0.17")
    if not isinstance(profile, str) or not profile:
        raise ManifestError("profile must be a non-empty string")
    for field in ("step_budget", "timeout_seconds"):
        if field in raw and (not isinstance(raw[field], int) or isinstance(raw[field], bool) or raw[field] <= 0):
            raise ManifestError(f"{field} must be a positive integer")
    libraries = raw.get("libraries", [])
    if not isinstance(libraries, list) or not all(isinstance(item, str) and item for item in libraries):
        raise ManifestError("libraries must be an array of non-empty strings")
    tests = raw.get("tests", [])
    if not isinstance(tests, list):
        raise ManifestError("tests must be an array when present")
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


def compiler_inventory(
    *,
    source_path: Path,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    artifacts: ArtifactStore,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask the authoritative compiler for the source test inventory."""

    process = run_process(
        [mncs, "test-inventory", str(source_path)],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        artifacts=artifacts,
        artifact_key="compiler-test-inventory",
    )
    decoded = parse_json_output(process["stdout"])
    if process.get("transport_error") or process["timed_out"] or process.get("returncode") != 0:
        return None, {"process": process, "document": decoded}
    if not isinstance(decoded, dict) or decoded.get("schema_version") != INVENTORY_SCHEMA:
        return None, {"process": process, "document": decoded}
    inventory = decoded.get("inventory")
    if decoded.get("valid") is not True or not isinstance(inventory, dict):
        return None, {"process": process, "document": decoded}
    return inventory, {"process": process, "document": decoded}


def normalize_inventory_tests(
    inventory: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert compiler inventory entries to runner selection records.

    The conversion adds runner-local tags/kind only.  It does not create a
    second declaration registry or infer names from source text.
    """

    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise ManifestError(f"compiler emitted an unsupported inventory schema: {inventory.get('schema_version')!r}")
    if inventory.get("module") != manifest["module"]:
        raise ManifestError(
            "manifest module does not match compiler inventory: "
            f"{manifest['module']!r} != {inventory.get('module')!r}"
        )
    if inventory.get("source_profile") != manifest["profile"]:
        raise ManifestError(
            "manifest profile does not match compiler inventory: "
            f"{manifest['profile']!r} != {inventory.get('source_profile')!r}"
        )
    entries = inventory.get("tests")
    if not isinstance(entries, list):
        raise ManifestError("compiler inventory tests must be an array")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"compiler inventory tests[{index}] must be an object")
        required = ("test_case_identity", "declaration_identity", "function_identity", "name")
        if not all(isinstance(entry.get(field), str) and entry[field] for field in required):
            raise ManifestError(f"compiler inventory tests[{index}] is missing a canonical identity")
        test_id = entry["test_case_identity"]
        name = entry["name"]
        if test_id in seen_ids or name in seen_names:
            raise ManifestError(f"compiler inventory contains a duplicate test identity/name: {name!r}")
        seen_ids.add(test_id)
        seen_names.add(name)
        tags = ["first-class", "mncs"]
        normalized.append(
            {
                "id": test_id,
                "entry": name,
                "kind": "unit",
                "tags": tags,
                "arguments": [],
                "declaration_identity": entry["declaration_identity"],
                "test_case_identity": test_id,
                "function_identity": entry["function_identity"],
                "module": entry.get("module", manifest["module"]),
                "qualified_name": entry.get("qualified_name", name),
                "profile": entry.get("profile", inventory.get("source_profile", manifest["profile"])),
                "source_span": entry.get("source_span"),
                "generic_params": entry.get("generic_params", []),
                "semantic_fingerprint": entry.get("semantic_fingerprint"),
                "subject_identity": entry.get("subject_identity", inventory.get("subject_identity")),
                "subject_fingerprint": entry.get("subject_fingerprint", inventory.get("subject_fingerprint")),
                "inputs": entry.get("inputs", []),
                "outputs": entry.get("outputs", []),
                "effects": entry.get("effects", []),
                "capabilities": entry.get("capabilities", []),
                "first_class": True,
            }
        )
    return normalized


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

    def record_existing(self, path: Path, kind: str) -> str:
        """Record a compiler-created file without changing its bytes."""

        try:
            relative = path.resolve().relative_to(self.root).as_posix()
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            raise AdapterError(f"unable to record compiler artifact {path}: {error}") from error
        item = {"path": relative, "kind": kind, "sha256": sha256_bytes(payload)}
        self.items.append(item)
        return relative


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


def safe_artifact_key(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


class EmbedSession:
    """Small ctypes adapter for the stable mncs-embed C ABI."""

    def __init__(self, library_path: Path, artifact_bytes: bytes):
        import ctypes

        self._ctypes = ctypes
        self.library_path = library_path
        self.library = ctypes.CDLL(str(library_path))
        uchar_p = ctypes.POINTER(ctypes.c_ubyte)
        self.library.mncs_session_open.argtypes = [uchar_p, ctypes.c_size_t]
        self.library.mncs_session_open.restype = ctypes.c_void_p
        self.library.mncs_session_close.argtypes = [ctypes.c_void_p]
        self.library.mncs_session_close.restype = None
        self.library.mncs_session_info.argtypes = [ctypes.c_void_p]
        self.library.mncs_session_info.restype = ctypes.c_void_p
        self.library.mncs_session_call_batch.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.library.mncs_session_call_batch.restype = ctypes.c_void_p
        self.library.mncs_response_text.argtypes = [ctypes.c_void_p]
        self.library.mncs_response_text.restype = ctypes.c_char_p
        self.library.mncs_response_free.argtypes = [ctypes.c_void_p]
        self.library.mncs_response_free.restype = None
        self.library.mncs_last_error.argtypes = []
        self.library.mncs_last_error.restype = ctypes.c_char_p
        self.artifact_bytes = artifact_bytes
        buffer = (ctypes.c_ubyte * len(artifact_bytes)).from_buffer_copy(artifact_bytes)
        self._artifact_buffer = buffer
        self.handle = self.library.mncs_session_open(buffer, len(artifact_bytes))
        if not self.handle:
            raise AdapterError(self.last_error())

    def last_error(self) -> str:
        value = self.library.mncs_last_error()
        return value.decode("utf-8", errors="replace") if value else "unknown embed error"

    def _response(self, handle: Any) -> Any:
        if not handle:
            raise AdapterError(self.last_error())
        try:
            payload = self.library.mncs_response_text(handle)
            if not payload:
                raise AdapterError("mncs-embed returned an empty response")
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterError(f"mncs-embed returned invalid JSON: {error}") from error
        finally:
            self.library.mncs_response_free(handle)

    def info(self) -> dict[str, Any]:
        return self._response(self.library.mncs_session_info(self.handle))

    def call_batch(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded = compact_json(requests).encode("utf-8")
        response = self.library.mncs_session_call_batch(self.handle, encoded)
        value = self._response(response)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise AdapterError("mncs-embed batch response is not an array of call outputs")
        return value

    def close(self) -> None:
        if self.handle:
            self.library.mncs_session_close(self.handle)
            self.handle = None

    def __enter__(self) -> "EmbedSession":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def embed_library_candidates(mncs: str, requested: str | None) -> list[Path]:
    candidates: list[Path] = []
    if requested:
        candidates.append(resolve_path(requested, Path.cwd()))
    environment = os.environ.get("MNCS_EMBED_LIBRARY")
    if environment:
        candidates.append(Path(environment).resolve())
    binary = Path(mncs)
    if binary.is_file():
        for name in ("libmncs_embed.so", "libmncs_embed.dylib", "mncs_embed.dll"):
            candidates.append(binary.resolve().parent / name)
    unique: list[Path] = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate not in unique:
            unique.append(candidate)
    return unique


def compile_embed_artifact(
    *,
    source_path: Path,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    artifacts: ArtifactStore,
    embed_library: str | None,
) -> tuple[EmbedSession | None, dict[str, Any]]:
    """Compile once and open a retained session when the embed library exists."""

    output_dir = artifacts.root / "compiler"
    output_dir.mkdir(parents=True, exist_ok=True)
    process = run_process(
        [
            mncs,
            "compile",
            str(source_path),
            "--emit",
            "backend",
            "--target",
            "research-bytecode",
            "--include-tests",
            "--output-dir",
            str(output_dir),
        ],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        artifacts=artifacts,
        artifact_key="compiler-first-class-artifact",
    )
    detail: dict[str, Any] = {
        "mode": "embed-unavailable",
        "compile": {
            "returncode": process.get("returncode"),
            "timed_out": process.get("timed_out", False),
            "stdout_artifact": process.get("stdout_artifact"),
            "stderr_artifact": process.get("stderr_artifact"),
            "command_artifact": process.get("command_artifact"),
        },
    }
    if process.get("transport_error") or process.get("timed_out") or process.get("returncode") != 0:
        detail["reason"] = "compiler could not produce a reusable backend artifact"
        return None, detail
    backend_path = output_dir / "backend.json"
    if not backend_path.is_file():
        detail["reason"] = "compiler reported success without backend.json"
        return None, detail
    try:
        backend_bytes = backend_path.read_bytes()
        backend = json.loads(backend_bytes)
    except (OSError, json.JSONDecodeError) as error:
        detail["reason"] = f"backend artifact is not readable JSON: {error}"
        return None, detail
    detail["backend_artifact"] = artifacts.record_existing(backend_path, "backend-artifact")
    if isinstance(backend, dict):
        detail["artifact_identity"] = backend.get("identity")
        detail["artifact_kind"] = backend.get("artifact_kind")
    candidates = embed_library_candidates(mncs, embed_library)
    detail["library_candidates"] = [str(candidate) for candidate in candidates]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            session = EmbedSession(candidate, backend_bytes)
        except (AdapterError, OSError) as error:
            detail["reason"] = f"embed session refused backend artifact: {error}"
            continue
        detail["mode"] = "retained-embed-batch"
        detail["library"] = str(candidate)
        detail["session"] = session.info()
        return session, detail
    detail.setdefault("reason", "mncs-embed library was not found")
    return None, detail


def expected_status_for(test: dict[str, Any]) -> str | None:
    if test.get("expected_status") is not None:
        return str(test["expected_status"])
    if test.get("kind") == "runtime-failure":
        return "runtime_failure"
    return None


def canonical_execution_request(
    *,
    module: str,
    function: str,
    arguments: Any,
    step_budget: int,
    type_arguments: Any = None,
    include_type_arguments: bool = False,
) -> dict[str, Any]:
    """Build the stable request document shared with debugger consumers.

    The retained embed ABI intentionally has a smaller transport request. It
    must never replace the canonical request in a TestResult because the
    request document is part of the test-to-debug lineage.
    """

    request: dict[str, Any] = {
        "schema_version": "0.1",
        "target": {"module": module, "function": function},
        "arguments": arguments,
        "step_budget": step_budget,
    }
    if include_type_arguments:
        request["type_arguments"] = type_arguments
    return request


def embed_execution_request(
    *,
    module: str,
    function: str,
    arguments: Any,
    step_budget: int,
    type_arguments: Any = None,
    include_type_arguments: bool = False,
) -> dict[str, Any]:
    """Build only the request shape accepted by the retained embed ABI."""

    request = {
        "module": module,
        "function": function,
        "args": arguments,
        "step_budget": step_budget,
    }
    if include_type_arguments:
        request["type_arguments"] = type_arguments
    return request


def base_test_result(test: dict[str, Any], source_path: Path, source_text: str = "") -> dict[str, Any]:
    del source_text  # source locations come from the compiler inventory.
    result = {
        "id": test["id"],
        "entry": test["entry"],
        "kind": test["kind"],
        "tags": list(test.get("tags", [])),
        "source": source_path.as_posix(),
        "location": compiler_location(test, source_path),
        "verdict": "PASS",
        "status": "pending",
    }
    if isinstance(test.get("source_span"), dict):
        result["source_span"] = dict(test["source_span"])
    semantic = {
        key: test[key]
        for key in (
            "declaration_identity",
            "test_case_identity",
            "function_identity",
            "module",
            "qualified_name",
            "profile",
            "generic_params",
            "semantic_fingerprint",
            "subject_identity",
            "subject_fingerprint",
        )
        if key in test
    }
    if semantic:
        result["semantic"] = semantic
    return result


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


def execution_result_from_decoded(
    *,
    test: dict[str, Any],
    source_path: Path,
    source_text: str,
    decoded: Any,
    step_budget: int,
    suite: bool = False,
    transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one structured MNCS observation into the runner envelope."""

    test_result = base_test_result(test, source_path, source_text)
    if transport:
        test_result["transport"] = transport
    if not isinstance(decoded, dict):
        diagnostics = diagnostic_list(decoded)
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
            "artifact_identity",
            "artifact_sha256",
            "backend",
            "reused_session",
            "steps",
            "failure",
            "failure_reason",
            "effects",
            "returned",
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
            execution_failure=decoded.get("failure") or decoded.get("failure_reason"),
        )
    return result_failure(
        test_result,
        failure_class="malformed_test_declaration",
        message="MNCS rejected the execution request",
        execution_failure=decoded.get("failure") or decoded.get("failure_reason"),
    )


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
    request = canonical_execution_request(
        module=module,
        function=entry,
        arguments=test.get("arguments", []),
        step_budget=step_budget,
        type_arguments=test.get("type_arguments"),
        include_type_arguments="type_arguments" in test,
    )
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
    test_result["request_artifact_ref"] = dict(request_artifact)
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


def execute_batch(
    *,
    tests: list[dict[str, Any]],
    source_path: Path,
    source_text: str,
    module: str,
    session: EmbedSession,
    default_step_budget: int,
    artifacts: ArtifactStore,
    transport: dict[str, Any],
) -> list[dict[str, Any]]:
    request_documents: list[dict[str, Any]] = []
    transport_requests: list[dict[str, Any]] = []
    request_artifacts: list[dict[str, Any]] = []
    for index, test in enumerate(tests):
        step_budget = int(test.get("step_budget", default_step_budget))
        request = canonical_execution_request(
            module=module,
            function=test["entry"],
            arguments=test.get("arguments", []),
            step_budget=step_budget,
            type_arguments=test.get("type_arguments"),
            include_type_arguments="type_arguments" in test,
        )
        request_documents.append(request)
        transport_requests.append(
            embed_execution_request(
                module=module,
                function=test["entry"],
                arguments=test.get("arguments", []),
                step_budget=step_budget,
                type_arguments=test.get("type_arguments"),
                include_type_arguments="type_arguments" in test,
            )
        )
        request_path = artifacts.root / "requests" / f"batch-{index:03d}-{safe_artifact_key(test['id'])}.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(json_bytes(request))
        request_artifact = {
            "path": request_path.relative_to(artifacts.root).as_posix(),
            "kind": "execution-request",
            "sha256": sha256_file(request_path),
        }
        artifacts.items.append(request_artifact)
        request_artifacts.append(request_artifact)
    try:
        decoded = session.call_batch(transport_requests)
    except AdapterError as error:
        results = []
        for index, test in enumerate(tests):
            result = result_failure(
                base_test_result(test, source_path, source_text),
                failure_class="infrastructure_failure",
                message="retained MNCS session could not execute the test batch",
                error=str(error),
                transport=transport,
            )
            result["request"] = request_documents[index]
            result["transport_request"] = transport_requests[index]
            result["request_artifact"] = request_artifacts[index]["path"]
            result["request_artifact_ref"] = dict(request_artifacts[index])
            results.append(result)
        return results
    results: list[dict[str, Any]] = []
    for index, test in enumerate(tests):
        if index >= len(decoded):
            result = result_failure(
                base_test_result(test, source_path, source_text),
                failure_class="infrastructure_failure",
                message="retained MNCS session returned fewer observations than requested",
                requested=len(tests),
                observed=len(decoded),
            )
        else:
            result = execution_result_from_decoded(
                test=test,
                source_path=source_path,
                source_text=source_text,
                decoded=decoded[index],
                step_budget=int(test.get("step_budget", default_step_budget)),
                transport=transport,
            )
        result["request"] = request_documents[index]
        result["transport_request"] = transport_requests[index]
        result["request_artifact"] = request_artifacts[index]["path"]
        result["request_artifact_ref"] = dict(request_artifacts[index])
        results.append(result)
    return results


def native_suite_fold(
    *,
    test_results: list[dict[str, Any]],
    session: EmbedSession,
    step_budget: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Fold native TestResult values through the MNCS suite module.

    The host carries returned ABI values between retained-session calls but
    never recomputes the suite verdict or failure counters.  A missing native
    value is a transport/compatibility condition: callers may retain the
    legacy adapter projection for external fixtures, but a normal
    first-class declaration path should have a complete native fold.
    """

    if not test_results:
        return None, {
            "authority": "mncs.test.suite.v1",
            "status": "not_run",
            "reason": "no selected first-class test results",
        }
    native_values: list[Any] = []
    for index, item in enumerate(test_results):
        if item.get("execution_status") != "returned":
            return None, {
                "authority": "mncs.test.suite.v1",
                "status": "unavailable",
                "reason": f"test result {index} did not return a native TestResult value",
            }
        execution = item.get("execution")
        returned = execution.get("returned") if isinstance(execution, dict) else None
        if not isinstance(returned, list) or len(returned) != 1:
            return None, {
                "authority": "mncs.test.suite.v1",
                "status": "unavailable",
                "reason": f"test result {index} has no single native TestResult value",
            }
        native_values.append(returned[0])

    try:
        empty_output = session.call_batch(
            [
                {
                    "module": "mncs.test.suite.v1",
                    "function": "empty",
                    "args": [],
                    "step_budget": step_budget,
                }
            ]
        )[0]
        empty_returned = empty_output.get("returned") if isinstance(empty_output, dict) else None
        if empty_output.get("status") != "returned" or not isinstance(empty_returned, list) or len(empty_returned) != 1:
            return None, {
                "authority": "mncs.test.suite.v1",
                "status": "unavailable",
                "reason": "suite.empty did not return a native SuiteSummary value",
                "output": empty_output,
            }
        summary_value = empty_returned[0]
        summary_output = empty_output
        calls = 1
        for value in native_values:
            summary_output = session.call_batch(
                [
                    {
                        "module": "mncs.test.suite.v1",
                        "function": "observe",
                        "args": [summary_value, value],
                        "step_budget": step_budget,
                    }
                ]
            )[0]
            calls += 1
            returned = summary_output.get("returned") if isinstance(summary_output, dict) else None
            if summary_output.get("status") != "returned" or not isinstance(returned, list) or len(returned) != 1:
                return None, {
                    "authority": "mncs.test.suite.v1",
                    "status": "unavailable",
                    "reason": "suite.observe did not return a native SuiteSummary value",
                    "call_index": calls - 1,
                    "output": summary_output,
                }
            summary_value = returned[0]
        summary = native_suite_summary(summary_output)
        if summary is None:
            return None, {
                "authority": "mncs.test.suite.v1",
                "status": "unavailable",
                "reason": "suite.observe returned a malformed SuiteSummary value",
            }
        return summary, {
            "authority": "mncs.test.suite.v1",
            "module": "mncs.test.suite.v1",
            "initializer": "empty",
            "observer": "observe",
            "status": "returned",
            "calls": calls,
        }
    except (AdapterError, IndexError) as error:
        return None, {
            "authority": "mncs.test.suite.v1",
            "status": "unavailable",
            "reason": str(error),
        }


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
            "adapter": "python-stdlib-transport-plus-mncs-embed-boundary",
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


def load_verification_plan(
    path: Path,
    *,
    source_path: Path,
    source_text: str,
) -> dict[str, Any]:
    """Load and bind one Ravel-produced minimum-sufficient-proof plan.

    The runner validates the transport shape and exact source binding, then
    selects compiler-inventory identities. It does not reinterpret the
    compiler graph or invent a second impact algorithm.
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read verification plan {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != VERIFICATION_PLAN_SCHEMA:
        raise ManifestError(f"verification plan must be {VERIFICATION_PLAN_SCHEMA}")
    plan_id = value.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise ManifestError("verification plan has no stable plan_id")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ManifestError("verification plan has no source binding")
    source_sha256 = source.get("sha256")
    current_sha256 = sha256_file(source_path)
    if source_sha256 != current_sha256:
        raise ManifestError(
            "verification plan is stale: source sha256 does not match the current source"
        )
    if source.get("path") is not None and not isinstance(source.get("path"), str):
        raise ManifestError("verification plan source.path must be a string")
    impact = value.get("impact")
    if not isinstance(impact, dict):
        raise ManifestError("verification plan has no impact projection")
    for field in ("graph_identity", "complete"):
        if field not in impact:
            raise ManifestError(f"verification plan impact is missing {field}")
    if not isinstance(impact.get("graph_identity"), str) or not impact["graph_identity"]:
        raise ManifestError("verification plan impact.graph_identity must be non-empty")
    if not isinstance(impact.get("complete"), bool):
        raise ManifestError("verification plan impact.complete must be boolean")
    selection = value.get("selection")
    if not isinstance(selection, dict):
        raise ManifestError("verification plan has no selection")
    level = selection.get("level")
    if level not in VERIFICATION_LEVELS:
        raise ManifestError(f"verification plan has unsupported selection level: {level!r}")
    selected = selection.get("selected_test_identities")
    if not isinstance(selected, list) or not all(isinstance(item, str) and item for item in selected):
        raise ManifestError("verification plan selection.selected_test_identities must be string identities")
    reasons = selection.get("escalation_reasons", value.get("escalation_reasons", []))
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ManifestError("verification plan escalation reasons must be strings")
    unknown_reasons = sorted(set(reasons) - ESCALATION_REASONS)
    if unknown_reasons:
        raise ManifestError(
            "verification plan has unknown escalation reasons: " + ", ".join(unknown_reasons)
        )
    risks = impact.get("risk_flags", [])
    if not isinstance(risks, list) or not all(isinstance(item, str) and item for item in risks):
        raise ManifestError("verification plan impact.risk_flags must be strings")
    value["_source_path"] = str(source_path.resolve())
    value["_source_sha256"] = current_sha256
    value["_selected_test_identities"] = sorted(set(selected))
    value["_escalation_reasons"] = sorted(set(reasons))
    return value


def apply_verification_plan(
    tests: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Select exact compiler identities named by a validated plan."""

    requested = set(plan.get("_selected_test_identities", []))
    by_id = {str(test.get("id")): test for test in tests}
    missing = sorted(requested - set(by_id))
    if missing:
        raise ManifestError(
            "verification plan names tests absent from the current compiler inventory: "
            + ", ".join(missing)
        )
    selected = [test for test in tests if test.get("id") in requested]
    available = len(tests)
    if available and not selected:
        raise ManifestError("verification plan selected no current test identities")
    expected_count = plan.get("selection", {}).get("available_test_count")
    if isinstance(expected_count, int) and expected_count != available:
        raise ManifestError(
            "verification plan is stale: compiler inventory test count changed"
        )
    return selected


def selection_summary(
    *,
    tests: list[dict[str, Any]],
    inventory: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    plan_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    selection = plan.get("selection", {}) if isinstance(plan, dict) else {}
    impact = plan.get("impact", {}) if isinstance(plan, dict) else {}
    selected_ids = [str(test.get("id")) for test in tests if test.get("id")]
    available_count = len(inventory.get("tests", [])) if isinstance(inventory, dict) else len(tests)
    summary: dict[str, Any] = {
        "authority": "ravel-verification-plan" if plan is not None else "manifest-default",
        "level": selection.get("level", "repository_canonical") if plan is not None else "repository_canonical",
        "selected_test_identities": selected_ids,
        "selected_count": len(tests),
        "available_count": available_count,
        "affected_surface_count": impact.get("affected_count", len(impact.get("nodes", []))) if isinstance(impact, dict) else len(tests),
        "risk_flags": sorted(impact.get("risk_flags", [])) if isinstance(impact, dict) else [],
        "escalation_reasons": sorted(plan.get("_escalation_reasons", [])) if plan is not None else [],
        "stop_sufficient": bool(plan.get("proof", {}).get("sufficient_to_stop", False)) if plan is not None else False,
    }
    if plan is not None:
        summary["plan_id"] = plan.get("plan_id")
        summary["graph_identity"] = impact.get("graph_identity")
        if plan_ref is not None:
            summary["plan_ref"] = plan_ref
    return summary


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
    test_inventory: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    classification: str,
    failure_class: str,
    message: str | None,
    failure_details: dict[str, Any] | None,
    allow_unsupported: bool,
    verification_plan: dict[str, Any] | None = None,
    verification_plan_ref: dict[str, Any] | None = None,
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
        summary["authority"] = (
            "compiler_inventory_native_observation_projection"
            if test_inventory is not None
            else "manifest_compile_or_adapter_projection"
        )
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
        "subject_identity": (test_inventory or {}).get("subject_identity"),
        "subject_fingerprint": (test_inventory or {}).get("subject_fingerprint"),
        "verification_plan": (verification_plan or {}).get("plan_id"),
        "execution": {
            key: (execution or {}).get(key)
            for key in ("mode", "artifact_identity", "artifact_sha256", "batch_size")
        },
    }
    run_id = sha256_bytes(compact_json(identity_material).encode())
    experiment_identity = f"mncs:language:experiment:test:{run_id}"
    experiment_run_identity = f"mncs:language:experiment-run:{run_id}"
    for index, item in enumerate(test_results):
        semantic = item.get("semantic")
        if not isinstance(semantic, dict) or not semantic.get("test_case_identity"):
            continue
        execution_digest = sha256_bytes(
            compact_json(
                {
                    "run": experiment_run_identity,
                    "index": index,
                    "test_case": semantic["test_case_identity"],
                }
            ).encode()
        )
        observation_digest = sha256_bytes(
            compact_json(
                {
                    "execution": execution_digest,
                    "status": item.get("execution_status", item.get("status")),
                    "verdict": item.get("verdict"),
                }
            ).encode()
        )
        item["execution_identity"] = f"{experiment_run_identity}:execution:{execution_digest}"
        item["observation_identity"] = f"{experiment_run_identity}:observation:{observation_digest}"
        item["oracle_evaluation"] = {
            "identity": f"{experiment_run_identity}:oracle:{observation_digest}",
            "test_case_identity": semantic["test_case_identity"],
            "verdict": item.get("verdict"),
            "status": item.get("execution_status", item.get("status")),
            "interpretation": "bounded oracle evaluation; not a universal proof",
        }
        request = item.get("request")
        request_ref = item.get("request_artifact_ref")
        item["execution_lineage"] = {
            "test_case_identity": semantic.get("test_case_identity"),
            "declaration_identity": semantic.get("declaration_identity"),
            "function_identity": semantic.get("function_identity"),
            "module": semantic.get("module"),
            "subject_identity": semantic.get("subject_identity")
            or (test_inventory or {}).get("subject_identity"),
            "execution_identity": item["execution_identity"],
            "observation_identity": item["observation_identity"],
            "oracle_evaluation_identity": item["oracle_evaluation"]["identity"],
            "source": {
                "path": item.get("source"),
                "sha256": provenance["source"].get("sha256"),
                "span": item.get("source_span"),
            },
            "request": {
                "schema_version": request.get("schema_version") if isinstance(request, dict) else None,
                "target": request.get("target") if isinstance(request, dict) else None,
                "sha256": request_ref.get("sha256") if isinstance(request_ref, dict) else None,
                "artifact": request_ref,
            },
        }
    native_suite_payload = None
    if suite_summary is not None:
        native_suite_payload = {**suite_summary, "authority": "native_suite"}
    selected_summary = selection_summary(
        tests=test_results,
        inventory=test_inventory,
        plan=verification_plan,
        plan_ref=verification_plan_ref,
    )
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
            "discovery": "compiler-inventory" if test_inventory is not None else "manifest",
        },
        "summary": summary,
        "selection": selected_summary,
        "tests": test_results,
        "suite": suite_result,
        "native_suite_summary": native_suite_payload,
        "execution": execution,
        "test_inventory": (
            {
                "schema_version": test_inventory.get("schema_version"),
                "source_artifact_identity": test_inventory.get("source_artifact_identity"),
                "source_profile": test_inventory.get("source_profile"),
                "subject_identity": test_inventory.get("subject_identity"),
                "subject_fingerprint": test_inventory.get("subject_fingerprint"),
                "test_count": len(test_inventory.get("tests", [])),
            }
            if test_inventory is not None
            else None
        ),
        "experiment": {
            "schema_version": EXPERIMENT_PROJECTION_SCHEMA,
            "identity": experiment_identity,
            "run_identity": experiment_run_identity,
            "definition": {
                "kind": "test",
                "source_artifact_identity": (test_inventory or {}).get("source_artifact_identity"),
                "subject_identity": (test_inventory or {}).get("subject_identity"),
                "subject_fingerprint": (test_inventory or {}).get("subject_fingerprint"),
                "test_cases": [
                    {
                        key: item.get("semantic", {}).get(key, item.get(key))
                        for key in (
                            "declaration_identity",
                            "test_case_identity",
                            "function_identity",
                            "qualified_name",
                            "source_span",
                            "semantic_fingerprint",
                        )
                        if item.get("semantic", {}).get(key, item.get(key)) is not None
                    }
                    for item in test_results
                    if item.get("semantic", {}).get("test_case_identity")
                ],
                "interpretation": "bounded_language_observation_not_universal_equivalence_or_conformance",
            },
            "execution": execution,
            "observations": [
                {
                    "identity": item.get("observation_identity"),
                    "execution_identity": item.get("execution_identity"),
                    "test_case_identity": item.get("semantic", {}).get("test_case_identity"),
                    "status": item.get("execution_status", item.get("status")),
                    "verdict": item.get("verdict"),
                    "observation_kind": "test_execution",
                }
                for item in test_results
                if item.get("semantic", {}).get("test_case_identity")
            ],
            "oracle_evaluations": [
                item["oracle_evaluation"]
                for item in test_results
                if isinstance(item.get("oracle_evaluation"), dict)
            ],
            "inference": "finite passing observations are bounded evidence, not universal proof",
        },
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
        result["failure"] = {"class": failure_class, "message": message, **(failure_details or {})}
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
        "selection": {
            "authority": "adapter",
            "level": "unknown",
            "selected_test_identities": [],
            "selected_count": 0,
            "available_count": 0,
            "affected_surface_count": 0,
            "risk_flags": [],
            "escalation_reasons": [],
            "stop_sufficient": False,
        },
        "tests": [],
        "suite": None,
        "native_suite_summary": None,
        "test_inventory": None,
        "experiment": {
            "schema_version": EXPERIMENT_PROJECTION_SCHEMA,
            "identity": None,
            "definition": None,
            "execution": None,
            "observations": [],
            "inference": "no executable experiment was produced",
        },
        "provenance": {"runner": {"name": "mncs-test", "version": RUNNER_VERSION}},
        "artifacts": [],
        "reproduction": {"command": "mncs-test validate-manifest --manifest <manifest>"},
        "failure": {"class": classification, "message": message},
    }


def check_result(result: dict[str, Any], result_digest: str) -> dict[str, Any]:
    unresolved = []
    if result["verdict"] == "UNKNOWN":
        unresolved.append(result.get("failure", {}).get("message", "unsupported capability"))
    summary = result.get("summary", {})
    failure_identity = next(
        (
            item.get("id")
            for item in result.get("tests", [])
            if isinstance(item, dict) and item.get("verdict") == "FAIL"
        ),
        None,
    )
    compact_summary = (
        f"{result['classification']}: {summary.get('passed', 0)} passed, "
        f"{summary.get('failed', 0)} failed, {summary.get('skipped', 0)} skipped"
    )
    if failure_identity:
        compact_summary += f"; failing_identity={failure_identity}"
    return {
        "schema_version": CHECK_SCHEMA,
        "id": result["id"],
        "provider": result["provider"],
        "verdict": result["verdict"],
        "scope": result.get("scope", {}).get("manifest", "mncs-test"),
        "claim": "MNCS-native tests were executed under the compiler-owned inventory or declared compatibility fixture",
        "summary": compact_summary,
        "contract_revision": RESULT_SCHEMA,
        "producer_revision": f"sha256:{result.get('provenance', {}).get('runner', {}).get('source_sha256', '')}",
        "digest": f"sha256:{result_digest}",
        "unresolved": unresolved,
        "references": [{"kind": "mncs-test-result", "uri": f"urn:mncs-test:{result.get('run_id', '')}", "digest": f"sha256:{result_digest}"}],
        "selection": result.get("selection"),
        "result_ref": {
            "kind": "mncs-test-result",
            "uri": f"urn:mncs-test:{result.get('run_id', '')}",
            "digest": f"sha256:{result_digest}",
            "detail": "retrieve the retained result artifact when per-test detail is required",
        },
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


def select_tests(tests: list[dict[str, Any]], filters: list[str]) -> list[dict[str, Any]]:
    if not filters:
        return tests
    selected = []
    for test in tests:
        haystack = " ".join(
            str(test.get(field, ""))
            for field in ("id", "entry", "qualified_name", "declaration_identity", "test_case_identity")
        )
        if any(pattern in haystack for pattern in filters):
            selected.append(test)
    return selected


def run_manifest(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    result_path, check_path, artifact_root = result_paths(args, cwd)
    manifest_path = resolve_path(args.manifest, cwd)
    try:
        manifest = load_manifest(manifest_path)
        source_text = read_source(Path(manifest["source_path"]))
        mncs = resolve_mncs(args.mncs, cwd)
        verification_plan: dict[str, Any] | None = None
        verification_plan_ref: dict[str, Any] | None = None
        if args.verification_plan:
            verification_plan_path = resolve_path(args.verification_plan, cwd, must_exist=True)
            verification_plan = load_verification_plan(
                verification_plan_path,
                source_path=Path(manifest["source_path"]),
                source_text=source_text,
            )
            verification_plan_ref = {
                "kind": "mncs-verification-plan",
                "path": relative_path(verification_plan_path, cwd),
                "sha256": sha256_file(verification_plan_path),
                "plan_id": verification_plan["plan_id"],
                "schema_revision": VERIFICATION_PLAN_SCHEMA,
            }
            if args.filter:
                raise ManifestError("--filter cannot be combined with --verification-plan; the plan owns exact selection")
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
        test_inventory: dict[str, Any] | None = None
        execution: dict[str, Any] | None = None
        inventory_failure: dict[str, Any] | None = None
        configured_tests = list(manifest["tests"])

        # A normal runtime manifest names a source/module and policy only.
        # The compiler is the sole authority for first-class test declarations.
        if not configured_tests and not manifest.get("suite"):
            test_inventory, inventory_transport = compiler_inventory(
                source_path=Path(manifest["source_path"]),
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                artifacts=artifacts,
            )
            if test_inventory is not None:
                configured_tests = normalize_inventory_tests(test_inventory, manifest)
                if verification_plan is not None:
                    if verification_plan.get("source", {}).get("subject_identity") not in (None, test_inventory.get("subject_identity")):
                        raise ManifestError("verification plan subject identity does not match the current compiler inventory")
                    configured_tests = apply_verification_plan(configured_tests, verification_plan)
                else:
                    configured_tests = select_tests(configured_tests, args.filter or [])
                execution = {
                    "mode": "compiler-inventory-awaiting-execution",
                    "inventory_artifact": inventory_transport["process"]["stdout_artifact"],
                    "selected_test_count": len(configured_tests),
                    "total_test_count": len(test_inventory.get("tests", [])),
                }
            else:
                process = inventory_transport.get("process", {})
                document = inventory_transport.get("document")
                diagnostics = diagnostic_list(document)
                if process.get("timed_out"):
                    inventory_failure = {
                        "class": "timeout",
                        "message": "compiler test inventory exceeded the declared timeout",
                        "details": {"timeout_seconds": args.timeout_seconds or manifest["timeout_seconds"]},
                    }
                elif diagnostics:
                    inventory_failure = {
                        "class": "compile_failure",
                        "message": "compiler could not produce a valid first-class test inventory",
                        "details": {
                            "diagnostics": diagnostics,
                            "inventory_document": document,
                        },
                    }
                else:
                    inventory_failure = {
                        "class": "infrastructure_failure",
                        "message": "compiler did not emit a structured first-class test inventory",
                        "details": {"inventory_document": document},
                    }
        else:
            if verification_plan is not None:
                configured_tests = apply_verification_plan(configured_tests, verification_plan)
            else:
                configured_tests = select_tests(configured_tests, args.filter or [])

        manifest_for_run = dict(manifest)
        manifest_for_run["tests"] = configured_tests

        if test_inventory is not None and not inventory_failure:
            if not configured_tests:
                execution = {
                    **(execution or {}),
                    "mode": "compiler-inventory-empty-selection",
                    "batch_size": 0,
                }
            else:
                session, execution_detail = compile_embed_artifact(
                    source_path=Path(manifest["source_path"]),
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                    artifacts=artifacts,
                    embed_library=args.embed_library,
                )
                execution = {**(execution or {}), **execution_detail}
                execution["selected_test_count"] = len(configured_tests)
                execution["batch_size"] = len(configured_tests) if session else 0
                if session is not None:
                    try:
                        transport = {
                            "mode": "retained-embed-batch",
                            "library": execution.get("library"),
                            "artifact_identity": execution.get("artifact_identity"),
                            "artifact_sha256": (execution.get("session") or {}).get("artifact_sha256"),
                            "batch_size": len(configured_tests),
                        }
                        test_results = execute_batch(
                            tests=configured_tests,
                            source_path=Path(manifest["source_path"]),
                            source_text=source_text,
                            module=manifest["module"],
                            session=session,
                            default_step_budget=args.step_budget or manifest["step_budget"],
                            artifacts=artifacts,
                            transport=transport,
                        )
                        suite_summary, native_aggregation = native_suite_fold(
                            test_results=test_results,
                            session=session,
                            step_budget=args.step_budget or manifest["step_budget"],
                        )
                        execution["native_aggregation"] = native_aggregation
                    finally:
                        session.close()
                else:
                    execution["mode"] = "subprocess-per-test-fallback"
                    execution["batch_size"] = 0
                    execution["fallback_reason"] = execution.get("reason")

        if test_inventory is None and not inventory_failure and manifest.get("suite"):
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
                    for test in configured_tests
                ]
        use_individual_processes = (
            test_inventory is None
            or (execution or {}).get("mode") == "subprocess-per-test-fallback"
        )
        if use_individual_processes and not inventory_failure:
            for index, test in enumerate(configured_tests):
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
                        artifact_key=safe_artifact_key(f"test-{index:03d}-{test['id']}"),
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
                        artifact_key=safe_artifact_key(f"test-{index:03d}-{test['id']}"),
                    )
                test_results.append(test_result)
        classification = "success"
        failure_class = "none"
        message: str | None = None
        failure_details: dict[str, Any] | None = None
        if inventory_failure:
            failure_class = inventory_failure["class"]
            classification = classification_for_failure(failure_class)
            message = inventory_failure["message"]
            failure_details = inventory_failure.get("details")
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
            manifest=manifest_for_run,
            source_text=source_text,
            mncs=mncs,
            cwd=cwd,
            library_paths=library_paths,
            artifacts=artifacts,
            test_results=test_results,
            suite_result=suite_result,
            suite_summary=suite_summary,
            test_inventory=test_inventory,
            execution=execution,
            classification=classification,
            failure_class=failure_class,
            message=message,
            failure_details=failure_details,
            allow_unsupported=args.allow_unsupported,
            verification_plan=verification_plan,
            verification_plan_ref=verification_plan_ref,
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
    mncs = resolve_mncs(args.mncs, Path.cwd())
    manifests = []
    errors = []
    for path in discover_manifests(root, args.recursive):
        try:
            manifest = load_manifest(path)
            document = {
                "path": relative_path(path, Path.cwd()),
                "name": manifest["name"],
                "source": relative_path(Path(manifest["source_path"]), Path.cwd()),
                "module": manifest["module"],
                "suite": manifest.get("suite"),
                "discovery": "manifest-compatibility" if manifest["tests"] or manifest.get("suite") else "compiler-inventory",
                "tests": [
                    {"id": item["id"], "entry": item["entry"], "kind": item["kind"], "tags": item.get("tags", [])}
                    for item in manifest["tests"]
                ],
            }
            if args.inventory and not manifest["tests"] and not manifest.get("suite"):
                libraries = list(manifest["library_paths"])
                for raw_path in args.library:
                    for component in raw_path.split(os.pathsep):
                        if component:
                            libraries.append(resolve_path(component, Path.cwd(), must_exist=True))
                with tempfile.TemporaryDirectory(prefix="mncs-test-discover-") as directory:
                    inventory, transport = compiler_inventory(
                        source_path=Path(manifest["source_path"]),
                        mncs=mncs,
                        cwd=Path.cwd(),
                        environment=command_environment(libraries),
                        timeout_seconds=manifest["timeout_seconds"],
                        artifacts=ArtifactStore(Path(directory)),
                    )
                if inventory is None:
                    errors.append(
                        {
                            "path": relative_path(path, Path.cwd()),
                            "error": "compiler inventory unavailable",
                            "diagnostics": diagnostic_list(transport.get("document")),
                        }
                    )
                    continue
                document["inventory"] = {
                    "schema_version": inventory.get("schema_version"),
                    "subject_identity": inventory.get("subject_identity"),
                    "subject_fingerprint": inventory.get("subject_fingerprint"),
                }
                document["tests"] = [
                    {
                        "id": item.get("test_case_identity"),
                        "entry": item.get("name"),
                        "kind": "unit",
                        "tags": ["first-class", "mncs"],
                        "declaration_identity": item.get("declaration_identity"),
                        "source_span": item.get("source_span"),
                    }
                    for item in inventory.get("tests", [])
                ]
            manifests.append(document)
        except ManifestError as error:
            errors.append({"path": relative_path(path, Path.cwd()), "error": str(error)})
    output = {"schema_version": DISCOVERY_SCHEMA, "root": str(root), "authority": "mncs-compiler", "manifests": manifests, "errors": errors}
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
    run = commands.add_parser("run", help="run one MNCS test manifest and compiler-discovered tests")
    run.add_argument("--manifest", default="mncs-test.toml")
    run.add_argument("--mncs", default="mncs")
    run.add_argument("--library", action="append", default=[], help="additional MNCS library root; may be repeated")
    run.add_argument("--embed-library", help="explicit mncs-embed shared library; otherwise discover beside mncs")
    run.add_argument("--filter", action="append", default=[], help="select tests containing this identity/name fragment; may be repeated")
    run.add_argument(
        "--verification-plan",
        "--plan",
        help="digest-bound mncs.verification-plan/1 selecting exact compiler test identities",
    )
    run.add_argument("--result", default=".mncs/mncs-test-result.json")
    run.add_argument("--check-result", default=".mncs/mncs-test-check.json")
    run.add_argument("--artifacts", default=".mncs/mncs-test-artifacts")
    run.add_argument("--format", choices=("json", "text"), default="json")
    run.add_argument("--step-budget", type=int)
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--allow-unsupported", action="store_true")
    run.set_defaults(handler=run_manifest)

    discover = commands.add_parser("discover", help="inspect manifests and optionally compiler-owned test inventories")
    discover.add_argument("--root", default=".")
    discover.add_argument("--recursive", action="store_true", help="include nested mncs-test.toml files")
    discover.add_argument("--inventory", action="store_true", help="query the compiler for each runtime inventory")
    discover.add_argument("--mncs", default="mncs")
    discover.add_argument("--library", action="append", default=[])
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
