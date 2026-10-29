#!/usr/bin/env python3
"""Shared run-app transport for mncs-test native applications.

Every native-first mncs-test operation (digest, family verification, and
later selection/coherence callers) crosses the process boundary here: one
bounded request file in, one schema-validated result file out. Policy stays
in the native modules; this file owns only transport mechanics.

This module deliberately does not import ``mncs_test``: that file is the
frozen explicit-compatibility oracle, while this transport serves the
native-first path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class NativeTransportError(ValueError):
    """Raised for unusable native transports (fail closed)."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def toolchain_cache_dir(mncs: str) -> Path:
    """Scope the run-app artifact cache to the invoking toolchain binary.

    Debug and release builds of the same source report identical nominal
    identities but produce mutually unvalidating artifacts; sharing one
    descriptor-local cache turns routine mixed-toolchain use into a hard
    admission refusal (pressure MNCS-TEST-P-012). Each toolchain binary
    path gets its own cache namespace. An explicit
    MNCS_NATIVE_APPLICATION_CACHE_DIR still wins when set.
    """
    located = shutil.which(mncs) or mncs
    try:
        canonical = str(Path(located).resolve())
    except OSError:
        canonical = str(located)
    scope = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return repo_root() / "native-applications" / ".mncs" / "toolchains" / scope


def _ordered_path_suffix(path_parts: tuple[str, ...], suffix: tuple[str, ...]) -> bool:
    """Match a declared sibling-library path to a selected checkout root."""
    position = 0
    for part in path_parts:
        if position < len(suffix) and part == suffix[position]:
            position += 1
    return position == len(suffix)


def _materialize_application_descriptor(
    descriptor: Path,
    output_directory: Path,
    library_roots: list[Path],
) -> Path:
    """Resolve repository-relative inputs against the selected worktree closure.

    Native application descriptors are checked into a repository and their
    sibling paths are authored from its root checkout. A linked worktree adds
    another path component, so external sibling roots must come from the
    selected Environment library closure instead of an assumed checkout depth.
    """
    try:
        application = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeTransportError(
            f"native application descriptor is unreadable: {descriptor}: {error}"
        ) from error
    if not isinstance(application, dict):
        raise NativeTransportError("native application descriptor must be an object")

    source_value = application.get("source")
    if not isinstance(source_value, str) or not source_value:
        raise NativeTransportError("native application descriptor has no source path")
    source_path = Path(source_value).expanduser()
    if not source_path.is_absolute():
        source_path = descriptor.parent / source_path
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as error:
        raise NativeTransportError(
            f"native application source is unavailable: {source_value}: {error}"
        ) from error
    if not source_path.is_file():
        raise NativeTransportError(f"native application source is not a file: {source_path}")

    declared_libraries = application.get("libraries", [])
    if not isinstance(declared_libraries, list) or not all(
        isinstance(item, str) and item for item in declared_libraries
    ):
        raise NativeTransportError("native application libraries must be a list of paths")

    repository = repo_root().resolve()
    resolved_libraries: list[Path] = []
    for raw_library in declared_libraries:
        library_path = Path(raw_library).expanduser()
        if library_path.is_absolute():
            candidates = [library_path]
        else:
            suffix = tuple(
                part for part in library_path.parts if part not in {".", "..", os.sep}
            )
            matches = [
                root for root in library_roots
                if suffix and _ordered_path_suffix(root.parts, suffix)
            ]
            checkout_candidate = (descriptor.parent / library_path).resolve()
            try:
                checkout_candidate.relative_to(repository)
                belongs_to_checkout = True
            except ValueError:
                belongs_to_checkout = False

            if belongs_to_checkout and checkout_candidate.is_dir():
                candidates = [checkout_candidate]
            elif not belongs_to_checkout and len(matches) == 1:
                candidates = matches
            elif len(matches) > 1:
                raise NativeTransportError(
                    f"selected Environment has ambiguous roots for descriptor library {raw_library!r}"
                )
            elif checkout_candidate.is_dir():
                candidates = [checkout_candidate]
            else:
                raise NativeTransportError(
                    f"descriptor library {raw_library!r} is absent from this checkout and the selected Environment closure"
                )

        try:
            resolved = candidates[0].resolve(strict=True)
        except OSError as error:
            raise NativeTransportError(
                f"native application library is unavailable: {candidates[0]}: {error}"
            ) from error
        if not resolved.is_dir():
            raise NativeTransportError(f"native application library is not a directory: {resolved}")
        if resolved not in resolved_libraries:
            resolved_libraries.append(resolved)

    application["source"] = str(source_path)
    application["libraries"] = [str(path) for path in resolved_libraries]
    materialized = output_directory / "native-application.json"
    materialized.write_text(
        json.dumps(application, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return materialized


def run_native_app(
    descriptor_name: str,
    request: dict,
    *,
    request_schema: str,
    result_schema: str,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    grant: str = "test_artifact",
    step_budget: int = 1048576,
    timeout_seconds: float = 120.0,
    result_filename: str = "result.json",
) -> dict:
    """Evaluate one native application and return its validated result."""
    if not isinstance(request, dict):
        raise NativeTransportError("native application request must be an object")
    if request.get("schema_version") != request_schema:
        raise NativeTransportError("native application request has an invalid schema version")
    descriptor = repo_root() / "native-applications" / descriptor_name
    if not descriptor.is_file():
        raise NativeTransportError(f"native application descriptor is unavailable: {descriptor}")
    cwd = cwd.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix=".mncs-test-app-", dir=cwd) as directory:
            directory_path = Path(directory)
            library_roots: list[Path] = []
            for raw_root in environment.get("MNCS_LIBRARY_PATH", "").split(os.pathsep):
                if not raw_root:
                    continue
                try:
                    root = Path(raw_root).expanduser().resolve(strict=True)
                except OSError as error:
                    raise NativeTransportError(
                        f"selected MNCS library root is unavailable: {raw_root}: {error}"
                    ) from error
                if not root.is_dir():
                    raise NativeTransportError(
                        f"selected MNCS library root is not a directory: {root}"
                    )
                if root not in library_roots:
                    library_roots.append(root)
            invocation_descriptor = _materialize_application_descriptor(
                descriptor, directory_path, library_roots,
            )
            request_path = directory_path / "request.json"
            result_path = directory_path / result_filename
            request_path.write_text(json.dumps(request), encoding="utf-8")
            relative_request = request_path.relative_to(cwd).as_posix()
            relative_result = result_path.relative_to(cwd).as_posix()
            command = [mncs, "run-app", str(invocation_descriptor)]
            if "MNCS_NATIVE_APPLICATION_CACHE_DIR" not in environment:
                command.extend(("--cache-dir", str(toolchain_cache_dir(mncs))))
            command.extend(
                [
                    "--grant-structured",
                    grant,
                    "--step-budget",
                    str(step_budget),
                    "--",
                    relative_request,
                    relative_result,
                ]
            )
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise NativeTransportError(
                    f"native application {descriptor_name} failed "
                    f"(exit {completed.returncode}): {detail}"
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise NativeTransportError(
                    f"native application {descriptor_name} returned no valid result: {error}"
                ) from error
    except subprocess.TimeoutExpired as error:
        raise NativeTransportError(
            f"native application {descriptor_name} exceeded {timeout_seconds}s"
        ) from error
    if not isinstance(result, dict) or result.get("schema_version") != result_schema:
        raise NativeTransportError(
            f"native application {descriptor_name} returned an invalid result schema"
        )
    return result


RUNNER_POLICY_REQUEST_SCHEMA = "mncs.test.runner-policy-request/1"
RUNNER_POLICY_RESULT_SCHEMA = "mncs.test.runner-policy/1"
RUNNER_POLICY_CLASSIFICATIONS = frozenset({
    "success", "test_failure", "runtime_failure", "setup_failure",
    "invalid_invocation", "infrastructure_failure", "compile_failure",
    "timeout", "unsupported",
})


def run_runner_policy(
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    failure_class: str | None = None,
    classification: str | None = None,
    allow_unsupported: bool = False,
    timeout_seconds: float = 120.0,
) -> tuple[str, int]:
    """Delegate Test result classification and process-exit policy to MNCS."""
    if (failure_class is None) == (classification is None):
        raise NativeTransportError(
            "runner policy requires exactly one of failure_class or classification"
        )
    if failure_class is not None and not isinstance(failure_class, str):
        raise NativeTransportError("runner failure_class must be text")
    if classification is not None and not isinstance(classification, str):
        raise NativeTransportError("runner classification must be text")
    if type(allow_unsupported) is not bool:
        raise NativeTransportError("allow_unsupported must be a boolean")
    request = {
        "schema_version": RUNNER_POLICY_REQUEST_SCHEMA,
        "failure_class": failure_class or "",
        "classification": classification or "",
        "classification_override": classification is not None,
        "allow_unsupported": allow_unsupported,
    }
    result = run_native_app(
        "test-runner-policy.json",
        request,
        request_schema=RUNNER_POLICY_REQUEST_SCHEMA,
        result_schema=RUNNER_POLICY_RESULT_SCHEMA,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    resolved = result.get("classification")
    exit_code = result.get("exit_code")
    if not isinstance(resolved, str):
        raise NativeTransportError("native runner policy returned no classification")
    if type(exit_code) is not int or not 0 <= exit_code <= 6:
        raise NativeTransportError("native runner policy returned an invalid exit code")
    if failure_class is not None and resolved not in RUNNER_POLICY_CLASSIFICATIONS:
        raise NativeTransportError("native runner policy returned an unknown classification")
    if classification is not None and resolved != classification:
        raise NativeTransportError("native runner policy changed an explicit classification")
    return resolved, exit_code
