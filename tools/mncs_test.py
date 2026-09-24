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
import time
from pathlib import Path
from typing import Any, Iterable

import tomllib
from family_contract import (
    validate_inventory,
    validate_obligation_inventory,
    validate_obligation_plan,
    validate_plan,
)

RESULT_SCHEMA = "mncs.test-result/1"
CHECK_SCHEMA = "mncs.check-result/1"
MANIFEST_SCHEMA = "mncs.test-manifest/1"
DISCOVERY_SCHEMA = "mncs.test-discovery/1"
INVENTORY_SCHEMA = "mncs.test-inventory/1"
EXPERIMENT_PROJECTION_SCHEMA = "mncs.test-experiment/1"
VERIFICATION_PLAN_SCHEMA = "mncs.verification-plan/1"
OBLIGATION_PLAN_SCHEMA = "mncs.verification-obligation-plan/1"
RUNNER_VERSION = "0.2.1"
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
FAMILY_CHECK_MAX_SELECTOR_LENGTH = 4096
FAMILY_CHECK_MAX_TEST_IDENTITY_LENGTH = 512
NATIVE_OBLIGATION_SELECTION_MAX_ITEMS = 16
NATIVE_OBLIGATION_SELECTION_MAX_IDENTITY_LENGTH = 1024

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


def compiler_inventory_identity(inventory: dict[str, Any]) -> str:
    """Bind the compiler inventory without transport/path metadata.

    The native ``mncs test`` entrypoint uses the same three-part semantic
    projection.  Keeping this compatibility/oracle calculation independent
    from the serialized source envelope makes family selectors reusable when
    a checkout moves between worktrees or CI directories.
    """

    material = [
        inventory.get("subject_identity"),
        inventory.get("subject_fingerprint"),
        [
            entry.get("test_case_identity")
            for entry in inventory.get("tests", [])
            if isinstance(entry, dict)
        ],
    ]
    return sha256_bytes(json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode())


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


def finite_variant(value: Any, *, type_name: str) -> str | None:
    """Read the nominal finite variant exposed by the typed value ABI.

    The discriminant remains in the compatibility projection, but it is not
    a semantic authority.  Variant identity is stable across enum evolution
    and fails closed when a different finite type is returned.
    """

    if not isinstance(value, dict):
        return None
    finite = value.get("finite")
    if not isinstance(finite, dict):
        return None
    type_identity = finite.get("type_identity")
    variant_identity = finite.get("variant_identity")
    if isinstance(type_identity, str) and isinstance(variant_identity, str):
        variant_parts = variant_identity.rsplit("::", 2)
        if (
            not type_identity.endswith(f"::{type_name}")
            or len(variant_parts) != 3
            or variant_parts[-2] != type_name
        ):
            return None
        return variant_parts[-1]
    # Accept the legacy typed-value spelling only when it still carries the
    # nominal type name; never fall back to the numeric discriminant.
    if finite.get("type") == type_name and isinstance(finite.get("variant"), str):
        return finite["variant"]
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
    verdict = finite_variant(fields.get("verdict"), type_name="Verdict")
    failure_kind = finite_variant(fields.get("failure_kind"), type_name="FailureKind")
    if verdict not in {"PASS", "FAIL", "SKIP", "UNSUPPORTED"} or failure_kind is None:
        return None
    return {
        "verdict": verdict,
        # Compatibility fields are retained for downstream evidence readers;
        # neither field participates in semantic control flow anymore.
        "verdict_code": integer_value(fields.get("verdict_code")),
        "failure_kind": "none" if failure_kind == "NoFailure" else failure_kind.lower(),
        "failure_kind_code": finite_discriminant(fields.get("failure_kind")),
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
    verdict = finite_variant(fields.get("verdict"), type_name="Verdict")
    if verdict not in {"PASS", "FAIL", "UNSUPPORTED"}:
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
        "verdict": verdict,
        "verdict_code": integer_value(fields.get("verdict_code")),
        **values,
    }


def external_verdict(variant: str) -> str:
    """Adapt the native enum to the public TestResult verdict vocabulary."""

    return "UNKNOWN" if variant == "UNSUPPORTED" else variant


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
    raw_grant_sets = raw.get("host_grant_sets", [])
    if not isinstance(raw_grant_sets, list):
        raise ManifestError("host_grant_sets must be an array when present")
    grant_set_ids: set[str] = set()
    host_grant_sets: list[dict[str, Any]] = []
    for index, item in enumerate(raw_grant_sets):
        if not isinstance(item, dict) or set(item) != {"test_case_identity", "grants"}:
            raise ManifestError(f"host_grant_sets[{index}] must contain only test_case_identity and grants")
        test_identity = item.get("test_case_identity")
        grants = item.get("grants")
        if not isinstance(test_identity, str) or not test_identity or test_identity in grant_set_ids:
            raise ManifestError(f"host_grant_sets[{index}].test_case_identity must be unique and non-empty")
        if not isinstance(grants, list) or not grants:
            raise ManifestError(f"host_grant_sets[{index}].grants must be a non-empty array")
        normalized_grants: list[dict[str, Any]] = []
        seen_grants: set[tuple[str, str]] = set()
        for grant_index, grant in enumerate(grants):
            if not isinstance(grant, dict) or set(grant) - {"capability", "locator", "bytes"}:
                raise ManifestError(f"host_grant_sets[{index}].grants[{grant_index}] is malformed")
            capability = grant.get("capability")
            locator = grant.get("locator", "")
            grant_bytes = grant.get("bytes", [])
            if not isinstance(capability, str) or not capability or not isinstance(locator, str):
                raise ManifestError(f"host_grant_sets[{index}].grants[{grant_index}] needs capability and locator strings")
            if (
                not isinstance(grant_bytes, list)
                or len(grant_bytes) > 4096
                or any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255 for value in grant_bytes)
            ):
                raise ManifestError(f"host_grant_sets[{index}].grants[{grant_index}].bytes must be a bounded byte array")
            identity = (capability, locator)
            if identity in seen_grants:
                raise ManifestError(f"host_grant_sets[{index}] contains a duplicate capability and locator")
            seen_grants.add(identity)
            normalized_grants.append({"capability": capability, "locator": locator, "bytes": grant_bytes})
        grant_set_ids.add(test_identity)
        host_grant_sets.append({"test_case_identity": test_identity, "grants": normalized_grants})
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
    result["host_grant_sets"] = host_grant_sets
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
    """Ask the authoritative compiler for the generic callable inventory.

    The returned compatibility envelope keeps the existing runner contract,
    but its facts are projected from compiler declarations rather than from a
    Test-specific compiler query.
    """

    process = run_process(
        [mncs, "declaration-inventory", str(source_path)],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        artifacts=artifacts,
        artifact_key="compiler-declaration-inventory",
    )
    decoded = parse_json_output(process["stdout"])
    if process.get("transport_error") or process["timed_out"] or process.get("returncode") != 0:
        return None, {"process": process, "document": decoded}
    if not isinstance(decoded, dict) or decoded.get("schema_version") != "mncs.declaration-inventory/1":
        return None, {"process": process, "document": decoded}
    declaration_inventory = decoded.get("inventory")
    if decoded.get("valid") is not True or not isinstance(declaration_inventory, dict):
        return None, {"process": process, "document": decoded}
    callables = declaration_inventory.get("callables", [])
    if not isinstance(callables, list):
        return None, {"process": process, "document": decoded}
    tests = []
    for callable_ in callables:
        if not isinstance(callable_, dict) or callable_.get("callable_kind") != "test":
            continue
        if not callable_.get("test_case_identity"):
            return None, {"process": process, "document": decoded}
        tests.append(
            {
                "declaration_identity": callable_.get("declaration_identity"),
                "test_case_identity": callable_.get("test_case_identity"),
                "function_identity": callable_.get("callable_identity"),
                "module": callable_.get("module"),
                "name": callable_.get("name"),
                "qualified_name": callable_.get("qualified_name"),
                "source_span": callable_.get("source_span"),
                "profile": callable_.get("profile"),
                "generic_params": callable_.get("generic_params", []),
                "inputs": callable_.get("inputs", []),
                "outputs": callable_.get("outputs", []),
                "effects": callable_.get("effects", []),
                "capabilities": callable_.get("capabilities", []),
                "semantic_fingerprint": callable_.get("test_case_identity"),
                "subject_identity": declaration_inventory.get("subject_identity"),
                "subject_fingerprint": declaration_inventory.get("subject_fingerprint"),
            }
        )
    tests.sort(key=lambda item: (item.get("declaration_identity", ""), item.get("name", "")))
    inventory = {
        "schema_version": INVENTORY_SCHEMA,
        "source_path": str(source_path.resolve()),
        "scope": declaration_inventory.get("scope"),
        "module": declaration_inventory.get("module"),
        "source_artifact_identity": declaration_inventory.get("source_artifact_identity"),
        "source_profile": declaration_inventory.get("source_profile"),
        "subject_identity": declaration_inventory.get("subject_identity"),
        "subject_fingerprint": declaration_inventory.get("subject_fingerprint"),
        "declaration_inventory_identity": declaration_inventory.get("inventory_identity"),
        "tests": tests,
    }
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
    started = time.perf_counter()
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
            "timing": {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3), "phase": "process"},
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
            "timing": {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3), "phase": "process"},
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
        "timing": {"wall_time_ms": round((time.perf_counter() - started) * 1000, 3), "phase": "process"},
    }


def safe_artifact_key(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


class EmbedSession:
    """Small ctypes adapter for the stable mncs-embed C ABI."""

    def __init__(self, library_path: Path, artifact_bytes: bytes):
        import ctypes

        self._ctypes = ctypes
        self.timings: list[dict[str, Any]] = []
        self.library_path = library_path
        library_started = time.perf_counter()
        self.library = ctypes.CDLL(str(library_path))
        self.timings.append({"phase": "artifact_load", "wall_time_ms": round((time.perf_counter() - library_started) * 1000, 3)})
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
        session_started = time.perf_counter()
        self.handle = self.library.mncs_session_open(buffer, len(artifact_bytes))
        self.timings.append({"phase": "session_open", "wall_time_ms": round((time.perf_counter() - session_started) * 1000, 3)})
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
        started = time.perf_counter()
        value = self._response(self.library.mncs_session_info(self.handle))
        self.timings.append({"phase": "session_info", "wall_time_ms": round((time.perf_counter() - started) * 1000, 3)})
        return value

    def call_batch(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        started = time.perf_counter()
        encoded = compact_json(requests).encode("utf-8")
        response = self.library.mncs_session_call_batch(self.handle, encoded)
        value = self._response(response)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise AdapterError("mncs-embed batch response is not an array of call outputs")
        self.timings.append({
            "phase": "native_call_batch",
            "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
            "request_count": len(requests),
            "request_bytes": len(encoded),
        })
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
            "timing": process.get("timing"),
            "stdout_artifact": process.get("stdout_artifact"),
            "stderr_artifact": process.get("stderr_artifact"),
            "command_artifact": process.get("command_artifact"),
        },
    }
    if process.get("transport_error") or process.get("timed_out") or process.get("returncode") != 0:
        detail["reason"] = "compiler could not produce a reusable backend artifact"
        return None, detail
    backend_path = output_dir / "backend.json"
    backend_read_started = time.perf_counter()
    if not backend_path.is_file():
        detail["reason"] = "compiler reported success without backend.json"
        return None, detail
    try:
        backend_bytes = backend_path.read_bytes()
        backend = json.loads(backend_bytes)
    except (OSError, json.JSONDecodeError) as error:
        detail["reason"] = f"backend artifact is not readable JSON: {error}"
        return None, detail
    detail["backend_load_wall_time_ms"] = round((time.perf_counter() - backend_read_started) * 1000, 3)
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
        detail["session_timings"] = list(session.timings)
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
    host_grants: list[dict[str, Any]] | None = None,
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
    if host_grants:
        request["host_grants"] = host_grants
    return request


def embed_execution_request(
    *,
    module: str,
    function: str,
    arguments: Any,
    step_budget: int,
    type_arguments: Any = None,
    include_type_arguments: bool = False,
    host_grants: list[dict[str, Any]] | None = None,
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
    if host_grants:
        request["grants"] = host_grants
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
            test_result["verdict"] = external_verdict(native["verdict"])
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
            test_result["verdict"] = external_verdict(native["verdict"])
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
        host_grants = test.get("host_grants", [])
        if not isinstance(host_grants, list):
            raise ManifestError(f"test {test.get('id')} has malformed host grants")
        request = canonical_execution_request(
            module=module,
            function=test["entry"],
            arguments=test.get("arguments", []),
            step_budget=step_budget,
            type_arguments=test.get("type_arguments"),
            include_type_arguments="type_arguments" in test,
            host_grants=host_grants,
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
                host_grants=host_grants,
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
        transport["timing"] = session.timings[-1] if session.timings else None
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
            "authority": "mncs.test.suite",
            "status": "not_run",
            "reason": "no selected first-class test results",
        }
    native_values: list[Any] = []
    for index, item in enumerate(test_results):
        if item.get("execution_status") != "returned":
            return None, {
                "authority": "mncs.test.suite",
                "status": "unavailable",
                "reason": f"test result {index} did not return a native TestResult value",
            }
        execution = item.get("execution")
        returned = execution.get("returned") if isinstance(execution, dict) else None
        if not isinstance(returned, list) or len(returned) != 1:
            return None, {
                "authority": "mncs.test.suite",
                "status": "unavailable",
                "reason": f"test result {index} has no single native TestResult value",
            }
        native_values.append(returned[0])

    try:
        empty_output = session.call_batch(
            [
                {
                    "module": "mncs.test.suite",
                    "function": "empty",
                    "args": [],
                    "step_budget": step_budget,
                }
            ]
        )[0]
        empty_returned = empty_output.get("returned") if isinstance(empty_output, dict) else None
        if empty_output.get("status") != "returned" or not isinstance(empty_returned, list) or len(empty_returned) != 1:
            return None, {
                "authority": "mncs.test.suite",
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
                        "module": "mncs.test.suite",
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
                    "authority": "mncs.test.suite",
                    "status": "unavailable",
                    "reason": "suite.observe did not return a native SuiteSummary value",
                    "call_index": calls - 1,
                    "output": summary_output,
                }
            summary_value = returned[0]
        summary = native_suite_summary(summary_output)
        if summary is None:
            return None, {
                "authority": "mncs.test.suite",
                "status": "unavailable",
                "reason": "suite.observe returned a malformed SuiteSummary value",
            }
        return summary, {
            "authority": "mncs.test.suite",
            "module": "mncs.test.suite",
            "initializer": "empty",
            "observer": "observe",
            "status": "returned",
            "calls": calls,
        }
    except (AdapterError, IndexError) as error:
        return None, {
            "authority": "mncs.test.suite",
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
    try:
        value = validate_plan(value, source_path=source_path, plan_path=path)
    except (RuntimeError, ValueError) as error:
        raise ManifestError(str(error)) from error
    current_sha256 = sha256_file(source_path)
    selected = value["selection"]["selected_test_identities"]
    reasons = value["selection"]["escalation_reasons"]
    value["_source_path"] = str(source_path.resolve())
    value["_source_sha256"] = current_sha256
    value["_selected_test_identities"] = sorted(set(selected))
    value["_escalation_reasons"] = sorted(set(reasons))
    return value


def _digest_identity(value: Any) -> str:
    return sha256_bytes(compact_json(value).encode("utf-8"))


def _repository_root_for_path(path: Path) -> Path | None:
    for parent in (path.resolve().parent, *path.resolve().parents):
        if (parent / ".mncs" / "project.json").is_file():
            return parent
    return None


def _repository_file_fingerprint(root: Path, paths: Iterable[str]) -> tuple[str, bool]:
    files: dict[str, str] = {}
    complete = True
    visited = 0
    excluded = {".git", "target", "node_modules", "__pycache__", ".pytest_cache"}
    for raw in sorted(set(paths)):
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            complete = False
            continue
        if not candidate.exists():
            complete = False
            continue
        candidates: list[Path]
        if candidate.is_file():
            candidates = [candidate]
        elif candidate.is_dir():
            candidates = []
            for directory, child_dirs, child_files in os.walk(candidate):
                child_dirs[:] = sorted(name for name in child_dirs if name not in excluded)
                for name in sorted(child_files):
                    candidates.append(Path(directory) / name)
                    visited += 1
                    if visited > 50000:
                        return _digest_identity(files), False
        else:
            complete = False
            continue
        for item in candidates:
            try:
                files[item.relative_to(root).as_posix()] = sha256_file(item)
            except (OSError, ValueError):
                complete = False
    return _digest_identity(files), complete


def _repository_tool_identity(argv: list[str], *, cwd: Path) -> str | None:
    if not argv:
        return None
    probes = [[argv[0], "--version"]]
    if argv[0] == "cargo":
        probes.append(["rustc", "--version"])
    values: list[str] = []
    for command in probes:
        try:
            process = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = (process.stdout or process.stderr).strip()
        if process.returncode != 0 or not output:
            return None
        values.append(output)
    return "; ".join(values)


def _cargo_package_from_argv(argv: list[str]) -> str | None:
    for index, value in enumerate(argv):
        if value in {"--package", "-p"} and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--package="):
            return value.partition("=")[2]
    return None


def _cargo_package_paths(metadata: dict[str, Any], package_name: str) -> list[str] | None:
    packages = metadata.get("packages")
    resolution = metadata.get("resolve")
    nodes = resolution.get("nodes") if isinstance(resolution, dict) else None
    if not isinstance(packages, list) or not isinstance(nodes, list):
        return None
    workspace_ids = set(metadata.get("workspace_members", []))
    named = [item for item in packages if isinstance(item, dict) and item.get("name") == package_name and item.get("id") in workspace_ids]
    if len(named) != 1:
        return None
    by_id = {item.get("id"): item for item in packages if isinstance(item, dict)}
    node_by_id = {item.get("id"): item for item in nodes if isinstance(item, dict)}
    closure: set[str] = set()
    pending = [str(named[0]["id"])]
    while pending:
        identity = pending.pop()
        if identity in closure:
            continue
        closure.add(identity)
        node = node_by_id.get(identity, {})
        dependencies = node.get("deps", [])
        if not isinstance(dependencies, list):
            return None
        for dependency in dependencies:
            package_id = dependency.get("pkg") if isinstance(dependency, dict) else None
            if package_id in workspace_ids and package_id not in closure:
                pending.append(str(package_id))
    workspace_root = Path(str(metadata.get("workspace_root", "")))
    paths = {"Cargo.toml", "Cargo.lock"}
    for identity in closure:
        package = by_id.get(identity, {})
        manifest_path = package.get("manifest_path")
        if not isinstance(manifest_path, str):
            return None
        try:
            paths.add(Path(manifest_path).parent.relative_to(workspace_root).as_posix())
        except ValueError:
            return None
    for optional in ("rust-toolchain", "rust-toolchain.toml", ".cargo"):
        if (workspace_root / optional).exists():
            paths.add(optional)
    return sorted(paths)


def _cargo_test_target_declarations(
    metadata: dict[str, Any], package_name: str, parent_name: str, argv: list[str]
) -> list[dict[str, str | list[str]]]:
    packages = metadata.get("packages")
    workspace_ids = set(metadata.get("workspace_members", []))
    named = [
        item for item in packages if isinstance(item, dict)
        and item.get("name") == package_name and item.get("id") in workspace_ids
    ] if isinstance(packages, list) else []
    if len(named) != 1:
        return []
    workspace_root = Path(str(metadata.get("workspace_root", ""))).resolve()
    output: list[dict[str, str | list[str]]] = []
    for target in named[0].get("targets", []):
        if not isinstance(target, dict) or (target.get("test") is not True and target.get("doctest") is not True):
            continue
        kinds = target.get("kind", [])
        name = target.get("name")
        source_path = target.get("src_path")
        if not isinstance(kinds, list) or not isinstance(name, str) or not isinstance(source_path, str):
            continue
        source = Path(source_path).resolve()
        try:
            relative_source = source.relative_to(workspace_root).as_posix()
        except ValueError:
            continue
        if target.get("test") is True:
            target_kind = next((kind for kind in ("lib", "bin", "test", "example") if kind in kinds), None)
            if target_kind is not None:
                selector = {
                    "lib": ["--lib"],
                    "bin": ["--bin", name],
                    "test": ["--test", name],
                    "example": ["--example", name],
                }[target_kind]
                output.append({
                    "name": f"{parent_name}.cargo.{target_kind}.{name}",
                    "target_identity": f"{package_name}:{target_kind}:{name}",
                    "source_path": relative_source,
                    "argv": [*argv, *selector],
                })
        if "lib" in kinds and target.get("doctest") is True:
            output.append({
                "name": f"{parent_name}.cargo.doc.{name}",
                "target_identity": f"{package_name}:doc:{name}",
                "source_path": relative_source,
                "argv": [*argv, "--doc"],
            })
    output.sort(key=lambda item: str(item["target_identity"]))
    return output


def _cargo_test_target_paths(
    metadata: dict[str, Any], package_name: str, target_source_path: str
) -> list[str] | None:
    package_paths = _cargo_package_paths(metadata, package_name)
    if package_paths is None:
        return None
    workspace_root = Path(str(metadata.get("workspace_root", ""))).resolve()
    paths: set[str] = set()
    for package_path in package_paths:
        candidate = workspace_root / package_path
        if package_path in {"Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml", ".cargo"}:
            paths.add(package_path)
            continue
        if (candidate / "Cargo.toml").is_file():
            paths.add((Path(package_path) / "Cargo.toml").as_posix())
        if (candidate / "src").is_dir():
            paths.add((Path(package_path) / "src").as_posix())
        if (candidate / "build.rs").is_file():
            paths.add((Path(package_path) / "build.rs").as_posix())
    paths.add(target_source_path)
    return sorted(paths)


def _prepare_repository_obligation_context(
    obligation_plan: dict[str, Any], manifest_path: Path
) -> dict[str, Any]:
    repository = obligation_plan.get("repository")
    if not isinstance(repository, dict) or repository.get("scope") != "repository_canonical":
        return {}
    source_path = Path(str(obligation_plan.get("source", {}).get("path", ""))).resolve()
    root = _repository_root_for_path(source_path)
    manifest_root = _repository_root_for_path(manifest_path)
    if root is None or manifest_root != root:
        raise ManifestError("repository-canonical plan and mncs-test manifest must belong to the same declared repository")
    test_manifest = load_manifest(manifest_path)
    try:
        test_manifest_relative = manifest_path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise ManifestError("repository-canonical test manifest is outside the declared repository") from error
    project_path = root / ".mncs" / "project.json"
    inventory_path = root / ".mncs" / "verification-obligations.json"
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
        raw_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"repository verification metadata is unavailable: {error}") from error
    if not isinstance(project, dict) or not isinstance(raw_inventory, dict):
        raise ManifestError("repository verification metadata must contain objects")
    verification = project.get("verification")
    if not isinstance(verification, dict):
        raise ManifestError("repository project metadata lacks a verification contract")
    if verification.get("obligation_inventory") != ".mncs/verification-obligations.json":
        raise ManifestError("repository verification inventory path does not match the repository-canonical runner contract")
    repository_identity = project.get("repository")
    if repository_identity != repository.get("identity"):
        raise ManifestError("repository-canonical plan repository identity is stale")
    if verification.get("test_runner_identity") != f"mncs-test/{RUNNER_VERSION}":
        raise ManifestError("repository project metadata names a different mncs-test executor version")
    try:
        inventory = validate_obligation_inventory(raw_inventory, repository=repository_identity)
    except (RuntimeError, ValueError) as error:
        raise ManifestError(f"repository obligation inventory is invalid: {error}") from error
    project_tests = project.get("contracts", {}).get("tests", [])
    providers = project.get("contracts", {}).get("provides", [])
    if not isinstance(project_tests, list) or not isinstance(providers, list):
        raise ManifestError("repository project test declarations are malformed")
    tests_by_identity: dict[str, dict[str, Any]] = {}
    declared_tests: list[dict[str, Any]] = []
    required_project_test_ids: list[str] = []
    cargo_metadata: dict[str, Any] | None = None
    for test in project_tests:
        if not isinstance(test, dict) or test.get("obligation") != "self":
            continue
        name = test.get("test")
        command = test.get("command")
        argv = command.get("argv") if isinstance(command, dict) else None
        if not isinstance(name, str) or not isinstance(argv, list) or not argv:
            raise ManifestError("repository self-test declarations need a name and argv")
        identity = f"{repository_identity}.project-test.{name}"
        declared_tests.append({"identity": identity, "name": name, "command": argv})
        timeout_seconds = command.get("timeout_seconds", test.get("timeout_seconds")) if isinstance(command, dict) else None
        target_mode = command.get("target_mode") if isinstance(command, dict) else None
        if target_mode is None:
            if identity in tests_by_identity:
                raise ManifestError(f"duplicate repository self-test identity: {identity}")
            tests_by_identity[identity] = {
                "test": name,
                "project_test": test,
                "command": command,
            }
            required_project_test_ids.append(identity)
            continue
        if target_mode != "cargo_test_targets" or argv[0] != "cargo" or not isinstance(timeout_seconds, int):
            raise ManifestError(f"repository self-test {identity} has an unsupported target_mode")
        if cargo_metadata is None:
            try:
                metadata_process = subprocess.run(
                    ["cargo", "metadata", "--format-version", "1"],
                    cwd=root, capture_output=True, text=True, check=False,
                    timeout=120, stdin=subprocess.DEVNULL,
                )
                cargo_metadata = json.loads(metadata_process.stdout) if metadata_process.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                cargo_metadata = None
        package = _cargo_package_from_argv(argv)
        targets = _cargo_test_target_declarations(cargo_metadata or {}, package or "", name, argv)
        if not targets:
            raise ManifestError(f"repository self-test {identity} has no Cargo test targets to expand")
        for target in targets:
            child_name = str(target["name"])
            child_identity = f"{repository_identity}.project-test.{child_name}"
            if child_identity in tests_by_identity:
                raise ManifestError(f"duplicate repository self-test identity: {child_identity}")
            tests_by_identity[child_identity] = {
                "test": child_name,
                "project_test": test,
                "command": {"argv": target["argv"], "timeout_seconds": timeout_seconds},
                "cargo_target_identity": target["target_identity"],
                "cargo_target_source_path": target["source_path"],
            }
            required_project_test_ids.append(child_identity)
    canonical_inventory_ids = [
        item["identity"] for item in inventory["obligations"] if item.get("scope") == "repository_canonical"
    ]
    expected_required = canonical_inventory_ids + required_project_test_ids
    if expected_required != repository.get("required_obligation_identities"):
        raise ManifestError("repository-canonical obligation set is stale or incomplete")
    source_inventories = repository.get("compiler_test_inventories", [])
    if not isinstance(source_inventories, list):
        raise ManifestError("repository compiler test inventory list is malformed")
    runner_identity = verification.get("test_runner_identity")
    inventory_identity = _digest_identity({
        "repository": repository_identity,
        "project_revision": project.get("revision"),
        "test_runner_identity": runner_identity,
        "inventory": inventory,
        "project_tests": declared_tests,
        "compiler_test_inventories": source_inventories,
    })
    compiler_inventory_identity = _digest_identity(source_inventories)
    fingerprint = _digest_identity({
        "repository": repository_identity,
        "project_revision": project.get("revision"),
        "test_runner_identity": runner_identity,
        "inventory_identity": inventory_identity,
        "compiler_test_inventory_identity": compiler_inventory_identity,
    })
    if inventory_identity != repository.get("inventory_identity") or fingerprint != repository.get("fingerprint"):
        raise ManifestError("repository-canonical definition fingerprint is stale")
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
            check=False, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ManifestError(f"repository revision is unavailable: {error}") from error
    if revision.returncode != 0 or revision.stdout.strip() != repository.get("revision"):
        raise ManifestError("repository-canonical plan targets a different repository revision")
    return {
        "root": root,
        "project": project,
        "inventory": inventory,
        "tests_by_identity": tests_by_identity,
        "declared_tests": declared_tests,
        "providers": providers,
        "runner_identity": runner_identity,
        "source_inventories": source_inventories,
        "cargo_metadata": cargo_metadata,
        "test_manifest": test_manifest,
        "test_manifest_relative": test_manifest_relative,
    }


def _verify_repository_obligation_identities(
    obligation_plan: dict[str, Any],
    context: dict[str, Any],
    compiler_inventory_document: dict[str, Any],
) -> None:
    if not context:
        return
    root = context["root"]
    repository = obligation_plan["repository"]
    selected_by_identity = {
        item.get("identity"): item
        for item in obligation_plan.get("obligations", [])
        if isinstance(item, dict)
    }
    current_tests = compiler_inventory_document.get("tests", [])
    current_test_ids = sorted(
        item.get("test_case_identity")
        for item in current_tests
        if isinstance(item, dict) and isinstance(item.get("test_case_identity"), str)
    )
    if len(current_test_ids) != len(current_tests):
        raise ManifestError("current compiler inventory has an unstable first-class test identity")
    if compiler_inventory_document.get("scope") != "source_module":
        raise ManifestError("repository test inventory must retain source_module scope")
    subject_identity = compiler_inventory_document.get("subject_identity")
    subject_fingerprint = compiler_inventory_document.get("subject_fingerprint")
    inventory_material = {
        "subject_identity": subject_identity,
        "subject_fingerprint": subject_fingerprint,
        "test_case_identities": current_test_ids,
    }
    source_path_value = Path(str(compiler_inventory_document.get("source_path", ""))).resolve()
    try:
        source_relative = source_path_value.relative_to(root).as_posix()
    except ValueError:
        source_relative = ""
    source_identity = _digest_identity(inventory_material)
    current_source_inventory = {
        "path": source_relative,
        "scope": "source_module",
        "identity": source_identity,
        "test_case_identities": current_test_ids,
    }
    if context["source_inventories"] != [current_source_inventory]:
        raise ManifestError("source_module test inventory changed after RAVEL planned repository closure")

    project = context["project"]
    contracts = project.get("contracts", {})
    providers = contracts.get("provides", []) if isinstance(contracts, dict) else []
    fingerprint_sources: dict[str, list[str]] = {}
    for provider in providers:
        if not isinstance(provider, dict) or not isinstance(provider.get("contract"), str):
            continue
        declared = provider.get("fingerprint_sources", [])
        if isinstance(declared, list):
            fingerprint_sources[provider["contract"]] = [item for item in declared if isinstance(item, str)]
    metadata = context.get("cargo_metadata")
    tool_versions: dict[str, str] = {}

    for identity, declaration in context["tests_by_identity"].items():
        selected = selected_by_identity.get(identity)
        if selected is None:
            raise ManifestError(f"repository-canonical plan omitted declared project test {identity}")
        test = declaration["project_test"]
        command = declaration.get("command")
        argv = command.get("argv") if isinstance(command, dict) else None
        timeout_seconds = command.get("timeout_seconds") if isinstance(command, dict) else None
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            raise ManifestError(f"project test {identity} has no explicit argv executor")
        tool_version = tool_versions.get(argv[0])
        if tool_version is None:
            tool_version = _repository_tool_identity(argv, cwd=root)
            if tool_version is not None:
                tool_versions[argv[0]] = tool_version
        if tool_version is None:
            raise ManifestError(f"project test {identity} executor version is unavailable")
        dependencies = sorted({
            path
            for contract in test.get("covers", []) if isinstance(contract, str)
            for path in fingerprint_sources.get(contract, [])
        })
        explicit_dependencies = test.get("invalidation_dependencies", [])
        if isinstance(explicit_dependencies, list):
            dependencies = sorted(set(dependencies + [item for item in explicit_dependencies if isinstance(item, str)]))
        for argument in argv[1:]:
            if not argument.startswith("-") and (root / argument).exists():
                dependencies.append(argument)
        dependencies = sorted(set(dependencies))
        if argv[0] == "cargo":
            package = _cargo_package_from_argv(argv)
            target_source = declaration.get("cargo_target_source_path")
            paths = (
                _cargo_test_target_paths(metadata, package, str(target_source))
                if metadata and package and isinstance(target_source, str)
                else _cargo_package_paths(metadata, package) if metadata and package else None
            )
            if paths is None:
                raise ManifestError(f"project test {identity} has no bounded Cargo package target")
            dependencies = sorted(set(dependencies + paths))
        invalidation, complete = _repository_file_fingerprint(root, dependencies)
        if not complete:
            raise ManifestError(f"project test {identity} dependency closure is stale or incomplete")
        executor = {
            "provider": "mncs-test",
            "kind": "external_integration",
            "entrypoint": f"project-test:{declaration['test']}",
            "argv": argv,
            "working_directory": ".",
            "timeout_seconds": timeout_seconds,
            "target_identity": declaration.get("cargo_target_identity") or _cargo_package_from_argv(argv) or declaration["test"],
            "verifier_identity": tool_version,
        }
        definition = {"project_test": test, "repository": repository["identity"], "runner_identity": context["runner_identity"]}
        if declaration.get("cargo_target_identity"):
            definition["cargo_target_identity"] = declaration["cargo_target_identity"]
        subject = f"mncs.repository-test:{repository['identity']}:{declaration['test']}"
        expected = {
            "definition_identity": _digest_identity(definition),
            "subject_identity": subject,
            "subject_fingerprint": _digest_identity({"subject": subject, "invalidation": invalidation}),
            "executor_identity": _digest_identity(executor),
            "verifier_identity": _digest_identity({"verifier": tool_version, "runner": context["runner_identity"]}),
            "invalidation_identity": invalidation,
        }
        for field, value in expected.items():
            if selected.get(field) != value:
                raise ManifestError(f"project test {identity} has stale {field}")
        selected_executor = selected.get("executor", {})
        if not isinstance(selected_executor, dict) or any(selected_executor.get(key) != value for key, value in executor.items()):
            raise ManifestError(f"project test {identity} executor does not match its project declaration")
        if selected_executor.get("argv") != argv or selected_executor.get("timeout_seconds") != timeout_seconds:
            raise ManifestError(f"project test {identity} command or timeout is stale")

    for obligation in context["inventory"]["obligations"]:
        if obligation.get("scope") != "repository_canonical":
            continue
        identity = obligation["identity"]
        selected = selected_by_identity.get(identity)
        if selected is None:
            raise ManifestError(f"repository-canonical plan omitted declared obligation {identity}")
        executor = obligation.get("executor", {})
        if not isinstance(executor, dict) or executor.get("kind") != "native_first_class_test":
            raise ManifestError(f"unsupported declared repository executor for {identity}")
        source_paths = executor.get("source_paths", [])
        library_paths = executor.get("library_paths", [])
        grant_sets = context["test_manifest"].get("host_grant_sets", [])
        grants_by_test = {
            item["test_case_identity"]: item["grants"]
            for item in grant_sets
            if isinstance(item, dict)
            and isinstance(item.get("test_case_identity"), str)
            and isinstance(item.get("grants"), list)
        }
        stale_grants = sorted(set(grants_by_test) - set(current_test_ids))
        if stale_grants:
            raise ManifestError(
                f"native host grant selectors are absent from the current compiler inventory: {', '.join(stale_grants)}"
            )
        host_grants = [
            {"test_case_identity": test_identity, "grants": grants_by_test[test_identity]}
            for test_identity in current_test_ids if test_identity in grants_by_test
        ]
        selected_executor = selected.get("executor", {})
        if (
            source_paths != [source_relative]
            or not isinstance(selected_executor, dict)
            or selected_executor.get("source_paths") != source_paths
            or selected_executor.get("library_paths") != library_paths
            or selected_executor.get("test_case_identities") != current_test_ids
            or selected_executor.get("host_grants", []) != host_grants
        ):
            raise ManifestError(f"native obligation {identity} is not bound to the current exact source test identities")
        local_invalidation, local_complete = _repository_file_fingerprint(
            root, [*obligation.get("invalidation_dependencies", []), context["test_manifest_relative"]]
        )
        external_names: list[str] = []
        for raw_library in library_paths:
            library = (root / raw_library).resolve()
            try:
                external_names.append(library.relative_to(root.parent.resolve()).as_posix())
            except ValueError as error:
                raise ManifestError(f"native obligation {identity} declares a library outside the workspace") from error
        external_invalidation, external_complete = _repository_file_fingerprint(root.parent, external_names)
        if not local_complete or not external_complete:
            raise ManifestError(f"native obligation {identity} dependency fingerprint is incomplete")
        invalidation = _digest_identity({
            "declared_dependencies": local_invalidation,
            "external_executor_libraries": external_invalidation,
        })
        verifier = executor.get("verifier_identity") or "mncs-test-runner/0.2.1"
        executor_identity = _digest_identity({
            "provider": executor.get("provider"),
            "kind": executor.get("kind"),
            "entrypoint": executor.get("entrypoint"),
            "source_paths": source_paths,
            "library_paths": library_paths,
            "test_case_identities": selected_executor.get("test_case_identities"),
            "host_grants": host_grants,
            "verifier_identity": verifier,
        })
        subject = obligation.get("subjects", [identity])[0]
        expected = {
            "definition_identity": _digest_identity(obligation),
            "subject_identity": subject,
            "subject_fingerprint": _digest_identity({"identity": identity, "invalidation": invalidation}),
            "executor_identity": executor_identity,
            "verifier_identity": _digest_identity({"verifier": verifier, "runner": context["runner_identity"]}),
            "invalidation_identity": invalidation,
        }
        for field, value in expected.items():
            if selected.get(field) != value:
                raise ManifestError(f"native obligation {identity} has stale {field}")


def _repository_evidence_matches(
    evidence: dict[str, Any], obligation: dict[str, Any], repository: dict[str, Any]
) -> bool:
    return (
        evidence.get("obligation_identity") == obligation.get("identity")
        and evidence.get("status") == "PASS"
        and evidence.get("repository_identity") == repository.get("identity")
        and evidence.get("repository_fingerprint") == repository.get("fingerprint")
        and evidence.get("subject_identity") == obligation.get("subject_identity")
        and evidence.get("subject_fingerprint") == obligation.get("subject_fingerprint")
        and evidence.get("definition_identity") == obligation.get("definition_identity")
        and evidence.get("executor_identity") == obligation.get("executor_identity")
        and evidence.get("verifier_identity") == obligation.get("verifier_identity")
        and evidence.get("invalidation_identity") == obligation.get("invalidation_identity")
    )


def _repository_evidence_record(
    obligation_plan: dict[str, Any],
    obligation: dict[str, Any],
    *,
    status: str,
    reason: str,
    observation: Any,
    repository_revision: str,
) -> dict[str, Any]:
    repository = obligation_plan["repository"]
    identity_material = {
        "obligation_identity": obligation.get("identity"),
        "repository_identity": repository.get("identity"),
        "repository_fingerprint": repository.get("fingerprint"),
        "subject_identity": obligation.get("subject_identity"),
        "subject_fingerprint": obligation.get("subject_fingerprint"),
        "definition_identity": obligation.get("definition_identity"),
        "executor_identity": obligation.get("executor_identity"),
        "verifier_identity": obligation.get("verifier_identity"),
        "invalidation_identity": obligation.get("invalidation_identity"),
        "status": status,
        "observation": observation,
    }
    return {
        "status": status,
        "evidence_identity": _digest_identity(identity_material),
        "obligation_identity": obligation.get("identity"),
        "repository_identity": repository.get("identity"),
        "repository_revision": repository_revision,
        "repository_fingerprint": repository.get("fingerprint"),
        "subject_identity": obligation.get("subject_identity"),
        "subject_fingerprint": obligation.get("subject_fingerprint"),
        "definition_identity": obligation.get("definition_identity"),
        "executor_identity": obligation.get("executor_identity"),
        "verifier_identity": obligation.get("verifier_identity"),
        "invalidation_identity": obligation.get("invalidation_identity"),
        "reason": reason,
    }


def execute_repository_host_obligations(
    obligation_plan: dict[str, Any],
    context: dict[str, Any],
    *,
    environment: dict[str, str],
    artifacts: ArtifactStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    repository = obligation_plan["repository"]
    old_evidence = [item for item in obligation_plan.get("evidence", []) if isinstance(item, dict)]
    active_evidence: list[dict[str, Any]] = []
    prior_evidence: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    revision_process = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=context["root"], capture_output=True,
        text=True, check=False, timeout=10, stdin=subprocess.DEVNULL,
    )
    revision = revision_process.stdout.strip() if revision_process.returncode == 0 else str(repository.get("revision", "unknown"))
    for obligation in obligation_plan.get("obligations", []):
        if not isinstance(obligation, dict) or obligation.get("scope") != "repository_canonical":
            continue
        executor = obligation.get("executor", {})
        if not isinstance(executor, dict) or executor.get("kind") == "native_first_class_test":
            continue
        related = [item for item in old_evidence if item.get("obligation_identity") == obligation.get("identity")]
        exact_pass = [item for item in related if _repository_evidence_matches(item, obligation, repository)]
        if obligation.get("status") == "current" and exact_pass:
            active_evidence.extend(exact_pass)
            prior_evidence.extend(related)
            results.append({
                "obligation_identity": obligation["identity"],
                "executor_identity": obligation.get("executor_identity"),
                "executor": executor,
                "status": "PASS",
                "reason": "current identity-bound PASS evidence reused",
                "reused": True,
                "duration_ms": 0.0,
                "evidence_identities": [item.get("evidence_identity") for item in exact_pass],
            })
            continue
        prior_evidence.extend(related)
        declaration = context["tests_by_identity"].get(str(obligation.get("identity")))
        argv = executor.get("argv")
        timeout_seconds = executor.get("timeout_seconds")
        if (
            declaration is None
            or not isinstance(argv, list)
            or not all(isinstance(arg, str) and arg for arg in argv)
            or not isinstance(timeout_seconds, int)
        ):
            reason = "executor identity is not backed by a current project self-test declaration"
            evidence = _repository_evidence_record(
                obligation_plan, obligation, status="UNKNOWN", reason=reason,
                observation={"executor": executor}, repository_revision=revision,
            )
            active_evidence.append(evidence)
            results.append({
                "obligation_identity": obligation["identity"],
                "executor_identity": obligation.get("executor_identity"),
                "executor": executor,
                "status": "UNKNOWN",
                "reason": reason,
                "reused": False,
                "duration_ms": 0.0,
                "evidence_identity": evidence["evidence_identity"],
            })
            continue
        working_directory = executor.get("working_directory", ".")
        command_cwd = resolve_path(str(working_directory), context["root"], must_exist=True)
        host_environment = repository_host_environment(
            environment,
            repository_root=context["root"],
            library_paths=executor.get("library_paths", []),
        )
        process = run_process(
            list(argv),
            cwd=command_cwd,
            environment=host_environment,
            timeout_seconds=timeout_seconds,
            artifacts=artifacts,
            artifact_key=safe_artifact_key(f"obligation-{obligation['identity']}"),
        )
        if process.get("timed_out"):
            status = "UNKNOWN"
            reason = f"executor timed out after {timeout_seconds}s"
        elif process.get("transport_error"):
            status = "UNKNOWN"
            reason = f"executor transport failed: {process['transport_error']}"
        elif process.get("returncode") == 0:
            status = "PASS"
            reason = "declared executor exited with status 0"
        else:
            status = "FAIL"
            reason = f"declared executor exited with status {process.get('returncode')}"
        outputs = {
            key: next((item.get("sha256") for item in artifacts.items if item.get("path") == process.get(f"{key}_artifact")), None)
            for key in ("stdout", "stderr")
        }
        observation = {
            "status": status,
            "returncode": process.get("returncode"),
            "timed_out": bool(process.get("timed_out")),
            "outputs": outputs,
        }
        evidence = _repository_evidence_record(
            obligation_plan, obligation, status=status, reason=reason,
            observation=observation, repository_revision=revision,
        )
        active_evidence.append(evidence)
        results.append({
            "obligation_identity": obligation["identity"],
            "executor_identity": obligation.get("executor_identity"),
            "executor": executor,
            "status": status,
            "reason": reason,
            "reused": False,
            "duration_ms": (process.get("timing") or {}).get("wall_time_ms", 0.0),
            "returncode": process.get("returncode"),
            "timed_out": bool(process.get("timed_out")),
            "artifacts": {
                key: process.get(f"{key}_artifact")
                for key in ("command", "stdout", "stderr")
            },
            "evidence_identity": evidence["evidence_identity"],
        })
    return active_evidence, prior_evidence, results


def repository_host_environment(
    inherited: dict[str, str],
    *,
    repository_root: Path,
    library_paths: Any,
) -> dict[str, str]:
    """Build an external executor environment from its declared libraries.

    ``--library`` configures native MNCS inventory/test execution. Host
    repository obligations get their own explicitly declared library paths so
    those runner inputs cannot alter the behavior of Rust/Python tests.
    """

    if not isinstance(library_paths, list) or not all(
        isinstance(path, str) and path for path in library_paths
    ):
        raise ManifestError("repository executor library_paths must be a string array")
    environment = dict(inherited)
    environment.pop("MNCS_LIBRARY_PATH", None)
    resolved = [
        resolve_path(path, repository_root, must_exist=True) for path in library_paths
    ]
    if resolved:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(str(path) for path in resolved)
    return environment


def execute_repository_native_obligations(
    obligation_plan: dict[str, Any],
    context: dict[str, Any],
    compiler_inventory_document: dict[str, Any],
    test_results: list[dict[str, Any]],
    prior_evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repository = obligation_plan["repository"]
    active_evidence: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    revision_process = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=context["root"], capture_output=True,
        text=True, check=False, timeout=10, stdin=subprocess.DEVNULL,
    )
    revision = revision_process.stdout.strip() if revision_process.returncode == 0 else str(repository.get("revision", "unknown"))
    for obligation in obligation_plan.get("obligations", []):
        if not isinstance(obligation, dict) or obligation.get("scope") != "repository_canonical":
            continue
        executor = obligation.get("executor", {})
        if not isinstance(executor, dict) or executor.get("kind") != "native_first_class_test":
            continue
        related = [item for item in prior_evidence if item.get("obligation_identity") == obligation.get("identity")]
        exact_pass = [item for item in related if _repository_evidence_matches(item, obligation, repository)]
        if obligation.get("status") == "current" and exact_pass:
            active_evidence.extend(exact_pass)
            results.append({
                "obligation_identity": obligation["identity"],
                "executor_identity": obligation.get("executor_identity"),
                "test_case_identities": obligation.get("test_case_identities", []),
                "status": "PASS",
                "reason": "current identity-bound PASS evidence reused",
                "reused": True,
                "duration_ms": 0.0,
                "evidence_identities": [item.get("evidence_identity") for item in exact_pass],
            })
            continue
        required_tests = obligation.get("test_case_identities", [])
        result_by_identity = {
            str(item.get("id")): item for item in test_results if isinstance(item, dict) and item.get("id")
        }
        selected_results = [result_by_identity.get(identity) for identity in required_tests]
        missing = [identity for identity, result in zip(required_tests, selected_results) if result is None]
        failures = [item for item in selected_results if isinstance(item, dict) and item.get("verdict") == "FAIL"]
        unknowns = [item for item in selected_results if isinstance(item, dict) and item.get("verdict") != "PASS"]
        if missing:
            status = "UNKNOWN"
            reason = "required first-class test identities were not executed: " + ", ".join(missing)
        elif failures:
            status = "FAIL"
            first = failures[0]
            detail = first.get("failure", {})
            reason = f"first-class test {first.get('id')} failed: {detail.get('message', first.get('verdict'))}"
        elif unknowns:
            status = "UNKNOWN"
            first = unknowns[0]
            detail = first.get("failure", {})
            reason = f"first-class test {first.get('id')} is UNKNOWN: {detail.get('message', first.get('verdict'))}"
        else:
            status = "PASS"
            reason = "every exact compiler-selected first-class test passed"
        observation = {
            "test_case_identities": list(required_tests),
            "test_results": [
                {
                    "identity": item.get("id"),
                    "verdict": item.get("verdict"),
                    "status": item.get("status"),
                    "failure": item.get("failure"),
                }
                for item in selected_results
                if isinstance(item, dict)
            ],
        }
        evidence = _repository_evidence_record(
            obligation_plan, obligation, status=status, reason=reason,
            observation=observation, repository_revision=revision,
        )
        active_evidence.append(evidence)
        results.append({
            "obligation_identity": obligation["identity"],
            "executor_identity": obligation.get("executor_identity"),
            "test_case_identities": list(required_tests),
            "status": status,
            "reason": reason,
            "reused": False,
            "duration_ms": sum(
                float((item.get("transport") or {}).get("timing", {}).get("wall_time_ms", 0.0))
                for item in selected_results if isinstance(item, dict)
            ),
            "missing_test_case_identities": missing,
            "evidence_identity": evidence["evidence_identity"],
        })
    return active_evidence, results


def load_obligation_plan(
    path: Path,
    *,
    source_path: Path,
    verification_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """Load the RAVEL obligation/evidence projection without executing it."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read obligation plan {path}: {error}") from error
    try:
        value = validate_obligation_plan(value)
    except (RuntimeError, ValueError) as error:
        raise ManifestError(str(error)) from error
    if verification_plan is not None and value.get("verification_plan_id") != verification_plan.get("plan_id"):
        raise ManifestError("obligation plan is bound to a different verification plan")
    repository = value.get("repository")
    repository_plan = isinstance(repository, dict) and repository.get("scope") == "repository_canonical"
    plan_source = Path(str(value.get("source", {}).get("path", ""))).resolve() if repository_plan else source_path.resolve()
    if repository_plan:
        root = _repository_root_for_path(plan_source)
        if root is None or plan_source == root or not plan_source.is_file():
            raise ManifestError("repository-canonical plan source path is not a current repository file")
    if value.get("source", {}).get("sha256") != sha256_file(plan_source):
        raise ManifestError("obligation plan source identity is stale")
    value["_source_path"] = str(plan_source)
    value["_selected_obligation_identities"] = sorted(
        item.get("identity")
        for item in value.get("obligations", [])
        if isinstance(item, dict) and isinstance(item.get("identity"), str)
    )
    value["_reused_obligation_identities"] = sorted(
        item.get("identity")
        for item in value.get("obligations", [])
        if isinstance(item, dict) and item.get("status") == "current"
    )
    value["_new_execution_obligation_identities"] = sorted(
        item.get("identity")
        for item in value.get("obligations", [])
        if isinstance(item, dict)
        and item.get("status") in {"new_execution_required", "stale", "selection_unresolved", "escalation_required", "contradictory"}
    )
    return value


def apply_verification_plan(
    tests: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Select exact compiler identities named by a validated plan."""

    try:
        validate_inventory(plan, [str(test.get("id")) for test in tests if test.get("id")])
    except (RuntimeError, ValueError) as error:
        raise ManifestError(str(error)) from error
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
    if not selected:
        raise ManifestError("verification plan selected no current test identities; no behavioral proof can be established")
    expected_count = plan.get("selection", {}).get("available_test_count")
    if isinstance(expected_count, int) and expected_count != available:
        raise ManifestError(
            "verification plan is stale: compiler inventory test count changed"
        )
    return selected


def _apply_obligation_plan_oracle(
    tests: list[dict[str, Any]], obligation_plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Historical Python projection retained for explicit differential tests."""

    by_id = {str(test.get("id")): test for test in tests if test.get("id")}
    required_test_ids: set[str] = set()
    unresolved: list[str] = []
    for item in obligation_plan.get("obligations", []):
        if not isinstance(item, dict) or item.get("status") == "current":
            continue
        identities = item.get("test_case_identities", [])
        executor = item.get("executor")
        executor_kind = executor.get("kind") if isinstance(executor, dict) else None
        requires_native_test = executor_kind == "native_first_class_test" or executor is None
        if requires_native_test and (not isinstance(identities, list) or not identities):
            unresolved.append(str(item.get("identity", "unknown")))
            continue
        if isinstance(identities, list):
            required_test_ids.update(str(identity) for identity in identities)
    missing = sorted(required_test_ids - set(by_id))
    if missing:
        raise ManifestError(
            "obligation plan names tests absent from the current compiler inventory: "
            + ", ".join(missing)
        )
    selected = [test for test in tests if str(test.get("id")) in required_test_ids]
    return selected, {
        "selected_obligation_identities": list(obligation_plan.get("_selected_obligation_identities", [])),
        "reused_obligation_identities": list(obligation_plan.get("_reused_obligation_identities", [])),
        "new_execution_obligation_identities": list(obligation_plan.get("_new_execution_obligation_identities", [])),
        "selection_unresolved": unresolved,
        "evidence": list(obligation_plan.get("evidence", [])),
        "sufficient_to_stop": bool(obligation_plan.get("stop", {}).get("sufficient_to_stop")),
        "obligation_plan_id": obligation_plan.get("obligation_plan_id"),
    }


def _native_selection_identities(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ManifestError(f"native obligation selection returned malformed {label}")
    return list(value)


def apply_obligation_plan(
    tests: list[dict[str, Any]],
    obligation_plan: dict[str, Any],
    *,
    mncs: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout_seconds: int = 180,
    native: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply an identity-bound plan through the native Test selection kernel.

    The Python path is available only with ``native=False`` for differential
    testing. Normal runner invocations provide the compiler binary and use the
    MNCS application; Python retains only JSON/process transport and the
    final inventory projection.
    """

    if not native:
        return _apply_obligation_plan_oracle(tests, obligation_plan)
    if not mncs or cwd is None or environment is None:
        raise ManifestError("native obligation selection requires mncs, cwd, and environment")

    all_obligations = [
        item for item in obligation_plan.get("obligations", []) if isinstance(item, dict)
    ]
    native_obligations = [
        item for item in all_obligations
        if item.get("executor", {}).get("kind") == "native_first_class_test"
        or not isinstance(item.get("executor"), dict)
    ]
    required_test_ids = sorted({
        identity
        for item in native_obligations
        for identity in item.get("test_case_identities", [])
        if isinstance(identity, str)
    })
    available_test_ids = {
        str(test["id"])
        for test in tests
        if isinstance(test, dict) and isinstance(test.get("id"), str) and test["id"]
    }
    request = {
        "schema_version": "mncs.test-obligation-selection-request/1",
        "plan_identity": str(obligation_plan.get("obligation_plan_id", "")),
        "obligations": [
            {
                "identity": str(item.get("identity", "")),
                "status": str(item.get("status", "")),
                "requires_native_test": (
                    item.get("executor", {}).get("kind") == "native_first_class_test"
                    if isinstance(item.get("executor"), dict)
                    else not bool(item.get("test_case_identities"))
                ),
                "test_case_identities": [
                    str(identity)
                    for identity in item.get("test_case_identities", [])
                    if isinstance(identity, str)
                ],
            }
            for item in native_obligations
        ],
        "available_test_identities": [
            identity for identity in required_test_ids if identity in available_test_ids
        ],
    }
    if len(request["obligations"]) > NATIVE_OBLIGATION_SELECTION_MAX_ITEMS:
        raise ManifestError(
            "native obligation selection bound exceeded: at most "
            f"{NATIVE_OBLIGATION_SELECTION_MAX_ITEMS} selected obligations"
        )
    if len(request["available_test_identities"]) > NATIVE_OBLIGATION_SELECTION_MAX_ITEMS:
        raise ManifestError(
            "native obligation selection bound exceeded: at most "
            f"{NATIVE_OBLIGATION_SELECTION_MAX_ITEMS} compiler test identities"
        )
    identity_values = [
        item["identity"]
        for item in request["obligations"]
    ] + request["available_test_identities"] + [
        identity
        for item in request["obligations"]
        for identity in item["test_case_identities"]
    ]
    if any(len(identity.encode("utf-8")) > NATIVE_OBLIGATION_SELECTION_MAX_IDENTITY_LENGTH for identity in identity_values):
        raise ManifestError(
            "native obligation selection identity bound exceeded: at most "
            f"{NATIVE_OBLIGATION_SELECTION_MAX_IDENTITY_LENGTH} UTF-8 bytes"
        )
    descriptor = Path(__file__).resolve().parents[1] / "native-applications" / "obligation-selection.json"
    if not descriptor.is_file():
        raise ManifestError(f"native obligation selection descriptor is unavailable: {descriptor}")
    cwd = cwd.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix=".mncs-test-obligation-selection-", dir=cwd) as directory:
            directory_path = Path(directory)
            request_path = directory_path / "request.json"
            result_path = directory_path / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            relative_request = request_path.relative_to(cwd).as_posix()
            relative_result = result_path.relative_to(cwd).as_posix()
            completed = subprocess.run(
                [
                    mncs,
                    "run-app",
                    str(descriptor),
                    "--grant-structured",
                    "test_artifact",
                    "--step-budget",
                    "1048576",
                    "--",
                    relative_request,
                    relative_result,
                ],
                cwd=str(cwd),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ManifestError(
                    f"native obligation selection failed (exit {completed.returncode}): {detail}"
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ManifestError(f"native obligation selection returned no valid result: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ManifestError(
            f"native obligation selection exceeded {timeout_seconds}s"
        ) from error

    if not isinstance(result, dict) or result.get("schema_version") != "mncs.test-obligation-selection/1":
        raise ManifestError("native obligation selection returned an invalid result schema")
    missing = _native_selection_identities(result.get("missing_test_identities"), "missing test identities")
    if missing:
        raise ManifestError(
            "obligation plan names tests absent from the current compiler inventory: "
            + ", ".join(sorted(missing))
        )
    by_id = {str(test.get("id")): test for test in tests if test.get("id")}
    selected_ids = _native_selection_identities(result.get("selected_test_identities"), "selected test identities")
    selected = [test for test in tests if str(test.get("id")) in set(selected_ids)]
    if any(identity not in by_id for identity in selected_ids):
        raise ManifestError("native obligation selection returned a test absent from the current inventory")
    repository = obligation_plan.get("repository", {})
    plan_selected = (
        repository.get("selected_obligation_identities", [])
        if isinstance(repository, dict)
        else obligation_plan.get("_selected_obligation_identities", [])
    )
    selected_obligations = [
        item.get("identity") for item in all_obligations if isinstance(item.get("identity"), str)
    ]
    selected_all = list(plan_selected) if isinstance(plan_selected, list) and plan_selected else selected_obligations
    reused_all = [item["identity"] for item in all_obligations if item.get("status") == "current"]
    needs_execution = {
        "new_execution_required", "stale", "selection_unresolved", "escalation_required", "contradictory"
    }
    new_all = [item["identity"] for item in all_obligations if item.get("status") in needs_execution]
    native_unresolved = _native_selection_identities(
        result.get("selection_unresolved"), "unresolved obligations"
    )
    selected_native_obligations = _native_selection_identities(
        result.get("selected_obligation_identities"), "selected obligation identities"
    )
    if set(selected_native_obligations) != {
        str(item.get("identity")) for item in native_obligations if isinstance(item.get("identity"), str)
    }:
        raise ManifestError("native test selector returned a different selected first-class obligation set")
    return selected, {
        "selected_obligation_identities": selected_all,
        "reused_obligation_identities": reused_all,
        "new_execution_obligation_identities": new_all,
        "selection_unresolved": native_unresolved,
        "evidence": list(obligation_plan.get("evidence", [])),
        "sufficient_to_stop": bool(obligation_plan.get("stop", {}).get("sufficient_to_stop")),
        "obligation_plan_id": obligation_plan.get("obligation_plan_id"),
    }


def _apply_host_grants(
    tests: list[dict[str, Any]],
    manifest: dict[str, Any],
    obligation_plan: dict[str, Any] | None,
) -> None:
    grants_by_test = {
        item["test_case_identity"]: item["grants"]
        for item in manifest.get("host_grant_sets", [])
        if isinstance(item, dict)
        and isinstance(item.get("test_case_identity"), str)
        and isinstance(item.get("grants"), list)
    }
    repository = obligation_plan.get("repository") if isinstance(obligation_plan, dict) else None
    repository_canonical = (
        isinstance(repository, dict) and repository.get("scope") == "repository_canonical"
    )
    if repository_canonical and obligation_plan is not None:
        planned: dict[str, list[dict[str, Any]]] = {}
        for obligation in obligation_plan.get("obligations", []):
            if not isinstance(obligation, dict) or obligation.get("status") == "current":
                continue
            executor = obligation.get("executor")
            if not isinstance(executor, dict) or executor.get("kind") != "native_first_class_test":
                continue
            for item in executor.get("host_grants", []):
                if not isinstance(item, dict) or not isinstance(item.get("test_case_identity"), str):
                    raise ManifestError("repository plan has malformed native host grant identities")
                identity = item["test_case_identity"]
                values = item.get("grants")
                if not isinstance(values, list):
                    raise ManifestError("repository plan has duplicate or malformed native host grants")
                if grants_by_test.get(identity) != values:
                    raise ManifestError(f"repository plan host grants are stale for first-class test {identity}")
                if identity in planned and planned[identity] != values:
                    raise ManifestError(f"repository plan has conflicting host grants for first-class test {identity}")
                planned[identity] = values
        grants_by_test = planned
    for test in tests:
        identity = test.get("id")
        if isinstance(identity, str) and identity in grants_by_test:
            test["host_grants"] = grants_by_test[identity]


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
    impact_nodes = impact.get("nodes", []) if isinstance(impact, dict) else []
    if not isinstance(impact_nodes, list):
        impact_nodes = []
    summary: dict[str, Any] = {
        "authority": "ravel-verification-plan" if plan is not None else "manifest-default",
        "level": selection.get("level", "repository_canonical") if plan is not None else "repository_canonical",
        "selected_test_identities": selected_ids,
        "selected_count": len(tests),
        "available_count": available_count,
        "affected_surface_count": impact.get("affected_count", len(impact_nodes)) if isinstance(impact, dict) else len(tests),
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
    obligation_plan: dict[str, Any] | None = None,
    obligation_plan_ref: dict[str, Any] | None = None,
    obligation_selection: dict[str, Any] | None = None,
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
        "obligation_plan": (obligation_plan or {}).get("obligation_plan_id"),
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
        native_suite_payload = {
            **suite_summary,
            "verdict": external_verdict(suite_summary["verdict"]),
            "authority": "native_suite",
        }
    selected_summary = selection_summary(
        tests=test_results,
        inventory=test_inventory,
        plan=verification_plan,
        plan_ref=verification_plan_ref,
    )
    if obligation_selection is None and obligation_plan is not None:
        obligation_selection = {
            "selected_obligation_identities": obligation_plan.get("_selected_obligation_identities", []),
            "reused_obligation_identities": obligation_plan.get("_reused_obligation_identities", []),
            "new_execution_obligation_identities": obligation_plan.get("_new_execution_obligation_identities", []),
            "selection_unresolved": [],
            "sufficient_to_stop": obligation_plan.get("stop", {}).get("sufficient_to_stop", False),
            "obligation_plan_id": obligation_plan.get("obligation_plan_id"),
        }
    reproduction_command = ["mncs-test", "run", "--manifest", str(manifest["manifest_path"])]
    if verification_plan_ref is not None and verification_plan_ref.get("path"):
        reproduction_command.extend(["--verification-plan", str(verification_plan_ref["path"])])
    if obligation_plan_ref is not None and obligation_plan_ref.get("path"):
        reproduction_command.extend(["--obligation-plan", str(obligation_plan_ref["path"])])
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
        "verification_obligations": {
            "schema_version": OBLIGATION_PLAN_SCHEMA,
            "plan_id": (obligation_plan or {}).get("obligation_plan_id"),
            "selected": (obligation_selection or {}).get("selected_obligation_identities", []),
            "executed": [
                item
                for item in (obligation_selection or {}).get("selected_obligation_identities", [])
                if item not in set((obligation_selection or {}).get("reused_obligation_identities", []))
                and item not in set((obligation_selection or {}).get("selection_unresolved", []))
            ],
            "reused": (obligation_selection or {}).get("reused_obligation_identities", []),
            "new_execution_required": (obligation_selection or {}).get("new_execution_obligation_identities", []),
            "selection_unresolved": (obligation_selection or {}).get("selection_unresolved", []),
            "evidence": (obligation_selection or {}).get("evidence", []),
            "evidence_history": (obligation_selection or {}).get("evidence_history", []),
            "execution_results": (obligation_selection or {}).get("execution_results", []),
            "execution_complete": bool((obligation_selection or {}).get("execution_complete", False)),
            "sufficient_to_stop": bool((obligation_selection or {}).get("sufficient_to_stop", False)),
        },
        "evidence": (obligation_selection or {}).get("evidence", []),
        "tests": test_results,
        "suite": suite_result,
        "native_suite_summary": native_suite_payload,
        "execution": execution,
        "test_inventory": (
            {
                "schema_version": test_inventory.get("schema_version"),
                "inventory_identity": compiler_inventory_identity(test_inventory),
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
            "command": shlex.join(reproduction_command),
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
    if obligation_plan_ref is not None:
        result["verification_obligations"]["plan_ref"] = obligation_plan_ref
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


def check_result(
    result: dict[str, Any],
    result_digest: str,
    *,
    check_id: str | None = None,
    claim: str | None = None,
) -> dict[str, Any]:
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
        "id": check_id or result["id"],
        "provider": result["provider"],
        "verdict": result["verdict"],
        "scope": result.get("scope", {}).get("manifest", "mncs-test"),
        "claim": claim
        or "MNCS-native tests were executed under the compiler-owned inventory or declared compatibility fixture",
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


FAMILY_CHECK_REQUEST_SCHEMA = "mncs.family-check-request/1"
FAMILY_CHECK_RESPONSE_SCHEMA = "mncs.family-check-response/1"


def load_family_check(
    path: Path,
    *,
    check_identity: str,
    contract_identity: str,
    repository_id: str,
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read family verification checks: {error}") from error
    if not isinstance(document, dict) or document.get("schema_version") != "commons.mncs.family-verification-checks/v1":
        raise ManifestError("family verification checks have an unsupported schema")
    if document.get("repository_id") != repository_id:
        raise ManifestError("family verification checks repository_id does not match the runner")
    checks = document.get("checks")
    if not isinstance(checks, list):
        raise ManifestError("family verification checks must be an array")
    matches = [item for item in checks if isinstance(item, dict) and item.get("identity") == check_identity]
    if len(matches) != 1:
        raise ManifestError(f"family check identity is not unique in the repository manifest: {check_identity}")
    check = dict(matches[0])
    if check.get("contract_identity") != contract_identity:
        raise ManifestError("family check contract identity does not match the request")
    if check.get("runner") != "mncs-test":
        raise ManifestError(f"family check {check_identity} is not owned by the mncs-test runner")
    for forbidden in ("command", "commands", "shell", "script", "argv", "executable"):
        if forbidden in check:
            raise ManifestError(f"family check cannot carry executable field {forbidden!r}")
    selector = check.get("selector")
    if not isinstance(selector, dict):
        raise ManifestError("mncs-test family checks require a selector")
    unknown_selector_fields = set(selector) - {"manifest", "inventory_identity", "test_identities"}
    if unknown_selector_fields:
        raise ManifestError(
            "mncs-test family check selector contains unsupported fields: "
            + ", ".join(sorted(unknown_selector_fields))
        )
    manifest = selector.get("manifest")
    inventory_identity = selector.get("inventory_identity")
    identities = selector.get("test_identities")
    if (
        not isinstance(manifest, str)
        or not manifest
        or len(manifest) > FAMILY_CHECK_MAX_SELECTOR_LENGTH
        or Path(manifest).is_absolute()
        or "\\" in manifest
        or ".." in Path(manifest).parts
        or not isinstance(inventory_identity, str)
        or not SHA256_HEX.fullmatch(inventory_identity)
        or not isinstance(identities, list)
        or not identities
        or len(identities) > 256
        or not all(
            isinstance(item, str)
            and bool(item)
            and len(item) <= FAMILY_CHECK_MAX_TEST_IDENTITY_LENGTH
            for item in identities
        )
        or len(set(identities)) != len(identities)
    ):
        raise ManifestError("mncs-test family check selector is invalid")
    check["selector"] = {
        "manifest": manifest,
        "inventory_identity": inventory_identity,
        "test_identities": sorted(identities),
    }
    return check


def load_family_check_request(path: Path) -> dict[str, Any]:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read family check request: {error}") from error
    if not isinstance(request, dict) or request.get("schema_version") != FAMILY_CHECK_REQUEST_SCHEMA:
        raise ManifestError(f"family check request must be {FAMILY_CHECK_REQUEST_SCHEMA}")
    for field in (
        "check_identity",
        "contract_identity",
        "contract_revision",
        "verification_plan_id",
        "family_graph_identity",
        "edge_fingerprint",
    ):
        value = request.get(field)
        if not isinstance(value, str) or not value:
            raise ManifestError(f"family check request {field} must be non-empty")
    for field in (
        "verification_plan_id",
        "family_graph_identity",
        "edge_fingerprint",
        "source_change_sha256",
    ):
        value = request[field]
        if not SHA256_HEX.fullmatch(value):
            raise ManifestError(f"family check request {field} must be a lowercase digest")
    return request


def run_family_check(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    request: dict[str, Any] = {}
    try:
        request = load_family_check_request(resolve_path(args.request, cwd, must_exist=True))
        checks_path = resolve_path(args.checks, cwd, must_exist=True)
        check = load_family_check(
            checks_path,
            check_identity=request["check_identity"],
            contract_identity=request["contract_identity"],
            repository_id=args.repository_id,
        )
        selector = check["selector"]
        manifest_path = resolve_path(selector["manifest"], checks_path.parent, must_exist=True)
        with tempfile.TemporaryDirectory(prefix="mncs-family-check-") as directory:
            root = Path(directory)
            result_path = root / "test-result.json"
            check_path = root / "check-result.json"
            artifacts = root / "artifacts"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--manifest",
                str(manifest_path),
                "--mncs",
                args.mncs,
                "--result",
                str(result_path),
                "--check-result",
                str(check_path),
                "--artifacts",
                str(artifacts),
                "--format",
                "json",
            ]
            for identity in selector["test_identities"]:
                command.extend(["--test-identity", identity])
            for library in args.library:
                command.extend(["--library", library])
            if args.embed_library:
                command.extend(["--embed-library", args.embed_library])
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                timeout=args.timeout_seconds,
            )
            if not result_path.is_file() or not check_path.is_file():
                raise AdapterError(
                    "mncs-test family check did not produce the required TestResult/CheckResult artifacts: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            produced_check = json.loads(check_path.read_text(encoding="utf-8"))
            result_digest = sha256_file(result_path)
        if not isinstance(result, dict) or result.get("schema_version") != RESULT_SCHEMA:
            raise AdapterError("family check produced an invalid mncs.test-result/1 document")
        if not isinstance(produced_check, dict) or produced_check.get("schema_version") != CHECK_SCHEMA:
            raise AdapterError("family check produced an invalid mncs.check-result/1 document")
        if produced_check.get("verdict") != result.get("verdict"):
            raise AdapterError("family check TestResult and CheckResult verdicts disagree")
        selected = result.get("selection", {}).get("selected_test_identities", [])
        if sorted(selected) != selector["test_identities"]:
            raise AdapterError("family check did not execute exactly the declared test identities")
        inventory = result.get("test_inventory")
        inventory_identity = inventory.get("inventory_identity") if isinstance(inventory, dict) else None
        expected_inventory = selector.get("inventory_identity")
        if expected_inventory is not None and inventory_identity != expected_inventory:
            raise AdapterError("family check compiler inventory identity is stale")
        check_result_document = check_result(
            result,
            result_digest,
            check_id=request["check_identity"],
            claim="The exact repository-owned behavioral family check was executed by mncs-test",
        )
        check_result_digest = sha256_bytes(json_bytes(check_result_document))
        check_definition_identity = sha256_bytes(compact_json(check).encode())
        response = {
            "schema_version": FAMILY_CHECK_RESPONSE_SCHEMA,
            "check_identity": request["check_identity"],
            "contract_identity": request["contract_identity"],
            "contract_revision": request["contract_revision"],
            "runner": "mncs-test",
            "verdict": result.get("verdict", "UNKNOWN"),
            "test_result": result,
            "check_result": check_result_document,
            "execution": {
                "run_identity": result.get("experiment", {}).get("run_identity"),
                "test_case_identities": selected,
                "inventory_identity": inventory_identity,
                "runner_version": RUNNER_VERSION,
                "result_sha256": result_digest,
                "check_result_sha256": check_result_digest,
                "check_definition_identity": check_definition_identity,
            },
            "family_binding": {
                key: request[key]
                for key in (
                    "verification_plan_id",
                    "family_graph_identity",
                    "edge_fingerprint",
                    "source_change_sha256",
                )
            },
        }
        print(json.dumps(response, indent=2, sort_keys=True, ensure_ascii=False))
        return int(result.get("exit_code", completed.returncode))
    except (ManifestError, AdapterError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "schema_version": FAMILY_CHECK_RESPONSE_SCHEMA,
                    **(
                        {
                            "check_identity": request["check_identity"],
                            "contract_identity": request["contract_identity"],
                            "contract_revision": request["contract_revision"],
                            "family_binding": {
                                key: request[key]
                                for key in (
                                    "verification_plan_id",
                                    "family_graph_identity",
                                    "edge_fingerprint",
                                    "source_change_sha256",
                                )
                                if key in request
                            },
                        }
                        if request
                        else {}
                    ),
                    "runner": "mncs-test",
                    "verdict": "UNKNOWN",
                    "failure": {"class": "infrastructure_failure", "message": str(error)},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_INFRASTRUCTURE_FAILURE


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


def select_exact_test_identities(
    tests: list[dict[str, Any]], requested: list[str]
) -> list[dict[str, Any]]:
    """Select only compiler identities named by a family check."""

    expected = sorted(set(requested))
    if not expected or len(expected) != len(requested):
        raise ManifestError("family check test identities must be a non-empty unique list")
    by_id = {str(test.get("id")): test for test in tests if test.get("id")}
    missing = sorted(set(expected) - set(by_id))
    if missing:
        raise ManifestError(
            "family check names tests absent from the current compiler inventory: "
            + ", ".join(missing)
        )
    return [by_id[test_id] for test_id in expected]


def run_manifest(args: argparse.Namespace) -> int:
    run_started = time.perf_counter()
    cwd = Path.cwd().resolve()
    result_path, check_path, artifact_root = result_paths(args, cwd)
    manifest_path = resolve_path(args.manifest, cwd)
    try:
        manifest = load_manifest(manifest_path)
        source_text = read_source(Path(manifest["source_path"]))
        mncs = resolve_mncs(args.mncs, cwd)
        verification_plan: dict[str, Any] | None = None
        verification_plan_ref: dict[str, Any] | None = None
        obligation_plan: dict[str, Any] | None = None
        obligation_plan_ref: dict[str, Any] | None = None
        obligation_selection: dict[str, Any] | None = None
        if args.verification_plan:
            verification_plan_path = resolve_path(args.verification_plan, cwd, must_exist=True)
            try:
                plan_source_document = json.loads(verification_plan_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ManifestError(f"cannot read verification plan source identity: {error}") from error
            plan_source_value = (
                plan_source_document.get("source", {}).get("path")
                if isinstance(plan_source_document, dict)
                else None
            )
            plan_source_path = (
                Path(plan_source_value).resolve()
                if isinstance(plan_source_value, str)
                else Path(manifest["source_path"])
            )
            verification_plan = load_verification_plan(
                verification_plan_path,
                source_path=plan_source_path,
                source_text=read_source(plan_source_path),
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
            if args.test_identity:
                raise ManifestError("--test-identity cannot be combined with --verification-plan; the plan owns exact selection")
        if args.obligation_plan:
            obligation_plan_path = resolve_path(args.obligation_plan, cwd, must_exist=True)
            obligation_plan = load_obligation_plan(
                obligation_plan_path,
                source_path=Path(manifest["source_path"]),
                verification_plan=verification_plan,
            )
            obligation_plan_ref = {
                "kind": "mncs-verification-obligation-plan",
                "path": relative_path(obligation_plan_path, cwd),
                "sha256": sha256_file(obligation_plan_path),
                "plan_id": obligation_plan["obligation_plan_id"],
                "schema_revision": OBLIGATION_PLAN_SCHEMA,
            }
            if args.filter:
                raise ManifestError("--filter cannot be combined with --obligation-plan; the obligation plan owns exact selection")
            if args.test_identity:
                raise ManifestError("--test-identity cannot be combined with --obligation-plan; the obligation plan owns exact selection")
        repository_plan = bool(
            obligation_plan
            and isinstance(obligation_plan.get("repository"), dict)
            and obligation_plan["repository"].get("scope") == "repository_canonical"
        )
        repository_context: dict[str, Any] = {}
        repository_native_sources: list[str] = []
        if repository_plan and obligation_plan is not None:
            repository_context = _prepare_repository_obligation_context(obligation_plan, manifest_path)
            for item in obligation_plan.get("obligations", []):
                executor = item.get("executor") if isinstance(item, dict) else None
                if (
                    isinstance(item, dict)
                    and item.get("scope") == "repository_canonical"
                    and isinstance(executor, dict)
                    and executor.get("kind") == "native_first_class_test"
                ):
                    paths = executor.get("source_paths", [])
                    if isinstance(paths, list):
                        repository_native_sources.extend(path for path in paths if isinstance(path, str))
                    declared_libraries = executor.get("library_paths", [])
                    if isinstance(declared_libraries, list):
                        for raw_library in declared_libraries:
                            if isinstance(raw_library, str):
                                manifest["library_paths"].append(resolve_path(raw_library, repository_context["root"], must_exist=True))
            repository_native_sources = sorted(set(repository_native_sources))
            if len(repository_native_sources) > 1:
                raise ManifestError("repository-canonical plan currently requires one bounded source_module inventory per mncs-test invocation")
            if repository_native_sources:
                native_source = resolve_path(repository_native_sources[0], repository_context["root"], must_exist=True)
                manifest["source_path"] = str(native_source)
                source_text = read_source(native_source)
            else:
                manifest["source_path"] = obligation_plan["source"]["path"]
                source_text = read_source(Path(manifest["source_path"]))
                manifest["tests"] = []
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
                if repository_plan:
                    manifest["module"] = test_inventory.get("module")
                    manifest["profile"] = test_inventory.get("source_profile")
                    if obligation_plan is None:
                        raise ManifestError("repository-canonical plan was not loaded")
                    _verify_repository_obligation_identities(
                        obligation_plan,
                        repository_context,
                        test_inventory,
                    )
                configured_tests = normalize_inventory_tests(test_inventory, manifest)
                if verification_plan is not None and not repository_plan:
                    if verification_plan.get("source", {}).get("subject_identity") not in (None, test_inventory.get("subject_identity")):
                        raise ManifestError("verification plan subject identity does not match the current compiler inventory")
                    configured_tests = apply_verification_plan(configured_tests, verification_plan)
                if obligation_plan is not None:
                    configured_tests, obligation_selection = apply_obligation_plan(
                        configured_tests,
                        obligation_plan,
                        mncs=mncs,
                        cwd=cwd,
                        environment=environment,
                        timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                    )
                elif args.test_identity:
                    configured_tests = select_exact_test_identities(configured_tests, args.test_identity)
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
            if obligation_plan is not None:
                configured_tests, obligation_selection = apply_obligation_plan(
                    configured_tests,
                    obligation_plan,
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                )
            elif args.test_identity:
                configured_tests = select_exact_test_identities(configured_tests, args.test_identity)
            else:
                configured_tests = select_tests(configured_tests, args.filter or [])

        _apply_host_grants(configured_tests, manifest, obligation_plan)

        manifest_for_run = dict(manifest)
        manifest_for_run["tests"] = configured_tests

        if test_inventory is not None and not inventory_failure:
            if not configured_tests:
                execution = {
                    **(execution or {}),
                    "mode": "obligation-evidence-reuse" if obligation_plan is not None else "compiler-inventory-empty-selection",
                    "batch_size": 0,
                }
                if obligation_selection is not None:
                    execution["obligation_selection"] = obligation_selection
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
                        execution["session_timings"] = list(session.timings)
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
        repository_execution_results: list[dict[str, Any]] = []
        repository_evidence_history: list[dict[str, Any]] = []
        repository_execution_complete = False
        if repository_plan and obligation_plan is not None:
            host_evidence, prior_host_evidence, host_results = execute_repository_host_obligations(
                obligation_plan,
                repository_context,
                environment=environment,
                artifacts=artifacts,
            )
            native_evidence, native_results = execute_repository_native_obligations(
                obligation_plan,
                repository_context,
                test_inventory or {},
                test_results,
                [item for item in obligation_plan.get("evidence", []) if isinstance(item, dict)],
            )
            repository_evidence_history = [
                item for item in obligation_plan.get("evidence", []) if isinstance(item, dict)
            ]
            repository_evidence = host_evidence + native_evidence
            repository_execution_results = host_results + native_results
            if obligation_selection is None:
                obligation_selection = {
                    "selected_obligation_identities": obligation_plan.get("_selected_obligation_identities", []),
                    "reused_obligation_identities": obligation_plan.get("_reused_obligation_identities", []),
                    "new_execution_obligation_identities": obligation_plan.get("_new_execution_obligation_identities", []),
                    "selection_unresolved": [],
                    "sufficient_to_stop": False,
                    "obligation_plan_id": obligation_plan.get("obligation_plan_id"),
                }
            obligation_selection["evidence"] = repository_evidence
            obligation_selection["evidence_history"] = repository_evidence_history
            obligation_selection["execution_results"] = repository_execution_results
            execution_statuses = {
                item.get("obligation_identity"): item.get("status")
                for item in repository_evidence
                if isinstance(item, dict)
            }
            required_identities = obligation_plan.get("repository", {}).get("required_obligation_identities", [])
            repository_execution_complete = (
                bool(obligation_plan.get("repository", {}).get("complete"))
                and all(execution_statuses.get(identity) == "PASS" for identity in required_identities)
                and len(execution_statuses) >= len(required_identities)
            )
            obligation_selection["execution_complete"] = repository_execution_complete
            obligation_selection["sufficient_to_stop"] = False
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
            suite_verdict = external_verdict(suite_summary["verdict"])
            if suite_verdict == "FAIL":
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
            elif suite_verdict == "UNKNOWN":
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
        if (
            classification == "success"
            and obligation_selection is not None
            and obligation_selection.get("selection_unresolved")
        ):
            unresolved = ", ".join(obligation_selection["selection_unresolved"])
            classification = "unsupported"
            failure_class = "selection_unresolved"
            message = f"selected verification obligations have no executable mncs-test identity: {unresolved}"
        if repository_plan:
            failed_obligations = [item for item in repository_execution_results if item.get("status") == "FAIL"]
            unknown_obligations = [item for item in repository_execution_results if item.get("status") == "UNKNOWN"]
            if failed_obligations:
                failed = failed_obligations[0]
                classification = "test_failure"
                failure_class = "repository_obligation_failed"
                message = str(failed.get("reason", "repository obligation failed"))
                failure_details = {
                    "obligation_identity": failed.get("obligation_identity"),
                    "executor_identity": failed.get("executor_identity"),
                    "executor": failed.get("executor"),
                    "evidence_identity": failed.get("evidence_identity"),
                }
            elif unknown_obligations and classification == "success":
                unknown = unknown_obligations[0]
                classification = "unsupported"
                failure_class = "repository_obligation_unknown"
                message = str(unknown.get("reason", "repository obligation is UNKNOWN"))
                failure_details = {
                    "obligation_identity": unknown.get("obligation_identity"),
                    "executor_identity": unknown.get("executor_identity"),
                    "executor": unknown.get("executor"),
                    "evidence_identity": unknown.get("evidence_identity"),
                }
            elif not repository_execution_complete and classification == "success":
                classification = "unsupported"
                failure_class = "repository_closure_incomplete"
                message = "repository-canonical obligation set is incomplete"
                failure_details = {
                    "missing_obligation_identities": obligation_plan.get("repository", {}).get("missing_obligation_identities", []),
                    "required_obligation_identities": obligation_plan.get("repository", {}).get("required_obligation_identities", []),
                }
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
            obligation_plan=obligation_plan,
            obligation_plan_ref=obligation_plan_ref,
            obligation_selection=obligation_selection,
        )
    except ManifestError as error:
        result = minimal_result(classification="invalid_invocation", message=str(error), cwd=cwd)
    except AdapterError as error:
        result = minimal_result(classification="infrastructure_failure", message=str(error), cwd=cwd)
    result.setdefault("timing", {})["total_wall_time_ms"] = round((time.perf_counter() - run_started) * 1000, 3)
    result["timing"]["attribution"] = {
        "process_wall_time": "execution.process.timing.wall_time_ms when a compiler/provider process ran",
        "native_call_wall_time": "execution.session_timings entries with phase native_call_batch",
        "artifact_load": "execution.session_timings entries with phase artifact_load",
        "session_open": "execution.session_timings entries with phase session_open",
        "transport": "transport.timing on each native execution result",
        "unattributed": "total minus recorded phase observations",
    }
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
    run.add_argument("--test-identity", action="append", default=[], help="select exact compiler test identity; may be repeated")
    run.add_argument(
        "--verification-plan",
        "--plan",
        help="digest-bound mncs.verification-plan/1 selecting exact compiler test identities",
    )
    run.add_argument(
        "--obligation-plan",
        help="identity-bound mncs.verification-obligation-plan/1 selecting obligations and reusable evidence",
    )
    run.add_argument("--result", default=".mncs/mncs-test-result.json")
    run.add_argument("--check-result", default=".mncs/mncs-test-check.json")
    run.add_argument("--artifacts", default=".mncs/mncs-test-artifacts")
    run.add_argument("--format", choices=("json", "text"), default="json")
    run.add_argument("--step-budget", type=int)
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--allow-unsupported", action="store_true")
    run.set_defaults(handler=run_manifest)

    family_check = commands.add_parser(
        "run-check",
        help="resolve and execute one repository-owned behavioral family check",
    )
    family_check.add_argument("--request", required=True, help="mncs.family-check-request/1 document")
    family_check.add_argument("--checks", default="family-verification-checks-v1.json")
    family_check.add_argument("--repository-id", required=True)
    family_check.add_argument("--mncs", default="mncs")
    family_check.add_argument("--library", action="append", default=[])
    family_check.add_argument("--embed-library")
    family_check.add_argument("--timeout-seconds", type=int, default=120)
    family_check.set_defaults(handler=run_family_check)

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
