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
            request_path = directory_path / "request.json"
            result_path = directory_path / result_filename
            request_path.write_text(json.dumps(request), encoding="utf-8")
            relative_request = request_path.relative_to(cwd).as_posix()
            relative_result = result_path.relative_to(cwd).as_posix()
            command = [mncs, "run-app", str(descriptor)]
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
