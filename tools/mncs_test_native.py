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

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class NativeTransportError(ValueError):
    """Raised for unusable native transports (fail closed)."""


class NativeInputBoundError(NativeTransportError):
    """Raised when a valid producer value exceeds a declared native record bound."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


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
NATIVE_CLI_REQUEST_MAX_BYTES = 1_048_576
NATIVE_CLI_RESULT_MAX_BYTES = 1_048_576

EXECUTION_ADJUDICATION_REQUEST_SCHEMA = (
    "mncs.test-execution-adjudication-request/1"
)
EXECUTION_ADJUDICATION_RESULT_SCHEMA = "mncs.test-execution-adjudication/1"
EXECUTION_ADJUDICATION_MAX_CASES = 256
EXECUTION_ADJUDICATION_DECISIONS = frozenset({
    "unknown_status", "malformed_result", "suite_result", "success", "skip",
    "unsupported_returned", "assertion_failure", "expectation_mismatch",
    "expected_failure", "unsupported_execution", "timeout", "runtime_failure",
    "invalid_request",
})
EXECUTION_ADJUDICATION_STATUSES = frozenset({
    "passed", "returned", "skipped", "unsupported", "failed", "expected_failure",
})
EXECUTION_ADJUDICATION_VERDICTS = frozenset({"PASS", "FAIL", "UNKNOWN"})
EXECUTION_STATUS_VALUES = frozenset({
    "returned", "runtime_failure", "unsupported", "budget_exhausted", "invalid_request",
})

INVENTORY_FILTER_REQUEST_SCHEMA = "mncs.test-inventory-filter-request/1"
INVENTORY_FILTER_RESULT_SCHEMA = "mncs.test-inventory-filter/1"
INVENTORY_FILTER_BATCH_SIZE = 16
INVENTORY_FILTER_TEXT_MAX_BYTES = 1024
INVENTORY_FILTER_ESTIMATED_WORK_MAX = 65_536
INVENTORY_FILTER_MAX_NATIVE_CALLS = 64
EXACT_IDENTITY_SELECTION_REQUEST_SCHEMA = "mncs.test-exact-identity-selection-request/1"
EXACT_IDENTITY_SELECTION_RESULT_SCHEMA = "mncs.test-exact-identity-selection/1"
EXACT_IDENTITY_SELECTION_BATCH_SIZE = 16
EXACT_IDENTITY_SELECTION_ID_MAX_BYTES = 512
EXACT_IDENTITY_SELECTION_ESTIMATED_WORK_MAX = 65_536


def _bounded_adjudication_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise NativeTransportError(f"execution adjudication {field} must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise NativeTransportError(
            f"execution adjudication {field} must be valid UTF-8"
        ) from error
    if size > 64:
        raise NativeTransportError(
            f"execution adjudication {field} exceeds the 64-byte native bound"
        )
    return value


def run_test_inventory_filter(
    search_texts: list[str],
    filters: list[str],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> dict[str, object]:
    """Select Test inventory indices through the native substring kernel.

    Inputs are split into bounded native batches. The returned indices remain
    tied to the original candidate list; host code only projects them back to
    the associated Test records.
    """
    if not isinstance(search_texts, list) or not isinstance(filters, list):
        raise NativeTransportError("Test inventory filters and search text must be lists")

    def bounded_text(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise NativeTransportError(f"Test inventory {field} must be text")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise NativeTransportError(
                f"Test inventory {field} must be valid UTF-8"
            ) from error
        if size > INVENTORY_FILTER_TEXT_MAX_BYTES:
            raise NativeInputBoundError(
                f"Test inventory {field} exceeds the native 1024-byte bound",
                reason="input_exceeds_native_1024_byte_bound",
            )
        return value

    normalized_texts = [bounded_text(value, "search text") for value in search_texts]
    normalized_filters = [bounded_text(value, "filter") for value in filters]
    if not normalized_texts:
        return {
            "selected_indices": [],
            "calls": 0,
            "case_count": 0,
            "filter_count": len(normalized_filters),
            "selected_count": 0,
        }
    candidate_batch_count = (
        len(normalized_texts) + INVENTORY_FILTER_BATCH_SIZE - 1
    ) // INVENTORY_FILTER_BATCH_SIZE
    filter_batch_count = max(
        1,
        (len(normalized_filters) + INVENTORY_FILTER_BATCH_SIZE - 1)
        // INVENTORY_FILTER_BATCH_SIZE,
    )
    if candidate_batch_count * filter_batch_count > INVENTORY_FILTER_MAX_NATIVE_CALLS:
        raise NativeInputBoundError(
            "Test inventory selection would exceed its bounded native call count",
            reason="native_batch_call_limit_exceeded",
        )
    encoded_texts = [value.encode("utf-8") for value in normalized_texts]
    encoded_filters = [value.encode("utf-8") for value in normalized_filters]
    selected_indices: set[int] = set()
    candidate_entries = list(zip(
        range(len(normalized_texts)), normalized_texts, encoded_texts, strict=True,
    ))
    filter_entries = list(zip(normalized_filters, encoded_filters, strict=True))
    planned_calls: list[tuple[list[tuple[int, str, bytes]], list[tuple[str, bytes]]]] = []
    estimated_pair_work: dict[tuple[bytes, bytes], int] = {}

    def estimate_pair_work(haystack: bytes, needle: bytes) -> int:
        cache_key = (haystack, needle)
        cached = estimated_pair_work.get(cache_key)
        if cached is not None:
            return cached
        if not needle or len(needle) > len(haystack):
            work = 1
        else:
            starts = len(haystack) - len(needle) + 1
            endpoint_hits = sum(
                haystack[start] == needle[0]
                and haystack[start + len(needle) - 1] == needle[-1]
                for start in range(starts)
            )
            # Each valid start pays for the range iteration and endpoint checks.
            # Only endpoint matches enter the full needle comparison loop.
            work = 4 * starts + endpoint_hits * len(needle)
        estimated_pair_work[cache_key] = work
        return work

    def plan_batch(
        candidates: list[tuple[int, str, bytes]],
        patterns: list[tuple[str, bytes]],
    ) -> None:
        estimated_work = 0
        for _, _, haystack in candidates:
            for _, needle in patterns:
                estimated_work += estimate_pair_work(haystack, needle)
                if estimated_work > INVENTORY_FILTER_ESTIMATED_WORK_MAX:
                    break
            if estimated_work > INVENTORY_FILTER_ESTIMATED_WORK_MAX:
                break
        if estimated_work <= INVENTORY_FILTER_ESTIMATED_WORK_MAX:
            if len(planned_calls) >= INVENTORY_FILTER_MAX_NATIVE_CALLS:
                raise NativeInputBoundError(
                    "Test inventory selection would exceed its bounded native call count",
                    reason="native_batch_call_limit_exceeded",
                )
            planned_calls.append((candidates, patterns))
            return
        if len(candidates) == 1 and len(patterns) == 1:
            raise NativeInputBoundError(
                "Test inventory substring request exceeds the native step-budget work estimate",
                reason="estimated_work_exceeds_native_step_budget",
            )
        if len(candidates) > 1 and len(candidates) >= len(patterns):
            midpoint = len(candidates) // 2
            plan_batch(candidates[:midpoint], patterns)
            plan_batch(candidates[midpoint:], patterns)
            return
        midpoint = len(patterns) // 2
        plan_batch(candidates, patterns[:midpoint])
        plan_batch(candidates, patterns[midpoint:])

    candidate_batches = [
        candidate_entries[offset:offset + INVENTORY_FILTER_BATCH_SIZE]
        for offset in range(0, len(candidate_entries), INVENTORY_FILTER_BATCH_SIZE)
    ]
    filter_batches = [
        filter_entries[offset:offset + INVENTORY_FILTER_BATCH_SIZE]
        for offset in range(0, len(filter_entries), INVENTORY_FILTER_BATCH_SIZE)
    ] or [[]]
    for candidate_batch in candidate_batches:
        for filter_batch in filter_batches:
            plan_batch(candidate_batch, filter_batch)

    for candidate_batch, filter_batch in planned_calls:
        request = {
            "schema_version": INVENTORY_FILTER_REQUEST_SCHEMA,
            "candidate_count": len(candidate_batch),
            "candidates": [
                {"test_index": index, "search_text": value}
                for index, value, _ in candidate_batch
            ],
            "filter_count": len(filter_batch),
            "filters": [pattern for pattern, _ in filter_batch],
        }
        result = run_native_app(
            "test-inventory-filter.json",
            request,
            request_schema=INVENTORY_FILTER_REQUEST_SCHEMA,
            result_schema=INVENTORY_FILTER_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
        indices = result.get("selected_indices")
        count = result.get("selected_count")
        if (not isinstance(indices, list) or type(count) is not int
                or count != len(indices)):
            raise NativeTransportError(
                "native Test inventory filter returned a mismatched result count"
            )
        allowed_indices = {index for index, _, _ in candidate_batch}
        for index in indices:
            if type(index) is not int or index not in allowed_indices:
                raise NativeTransportError(
                    "native Test inventory filter returned an out-of-range index"
                )
            selected_indices.add(index)
    return {
        "selected_indices": sorted(selected_indices),
        "calls": len(planned_calls),
        "case_count": len(normalized_texts),
        "filter_count": len(normalized_filters),
        "selected_count": len(selected_indices),
    }


def run_exact_test_identity_selection(
    candidate_identities: list[str],
    requested_identities: list[str],
    *,
    candidate_indices: list[int] | None = None,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> dict[str, object]:
    """Resolve exact Test identities through the native selection kernel.

    The adapter batches inventory and request rows while keeping compiler
    inventory indices stable. Repeated candidate identities resolve to the
    last inventory row, matching the runner's former ID-to-record map.
    """
    if (not isinstance(candidate_identities, list)
            or not isinstance(requested_identities, list)):
        raise NativeTransportError("Test identities and requests must be lists")
    if candidate_indices is None:
        normalized_indices = list(range(len(candidate_identities)))
    elif (not isinstance(candidate_indices, list)
          or len(candidate_indices) != len(candidate_identities)
          or any(type(index) is not int or index < 0 for index in candidate_indices)
          or len(set(candidate_indices)) != len(candidate_indices)):
        raise NativeTransportError("Test identity candidate indices are malformed")
    else:
        normalized_indices = list(candidate_indices)

    def bounded_identity(value: object, field: str) -> tuple[str, bytes]:
        if not isinstance(value, str):
            raise NativeTransportError(f"Test identity {field} must be text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise NativeTransportError(f"Test identity {field} must be valid UTF-8") from error
        if len(encoded) > EXACT_IDENTITY_SELECTION_ID_MAX_BYTES:
            raise NativeInputBoundError(
                "Test identity exceeds the native 512-byte record bound",
                reason="input_exceeds_native_512_byte_bound",
            )
        return value, encoded

    normalized_candidates = [
        bounded_identity(value, "candidate") for value in candidate_identities
    ]
    normalized_requests = [
        bounded_identity(value, "request") for value in requested_identities
    ]
    selected_indices: list[int | None] = [None] * len(normalized_requests)
    calls = 0
    for candidate_offset in range(0, len(normalized_candidates), EXACT_IDENTITY_SELECTION_BATCH_SIZE):
        candidate_batch = normalized_candidates[
            candidate_offset:candidate_offset + EXACT_IDENTITY_SELECTION_BATCH_SIZE
        ]
        index_batch = normalized_indices[
            candidate_offset:candidate_offset + EXACT_IDENTITY_SELECTION_BATCH_SIZE
        ]
        valid_candidate_indices = set(index_batch)
        for request_offset in range(0, len(normalized_requests), EXACT_IDENTITY_SELECTION_BATCH_SIZE):
            request_batch = normalized_requests[
                request_offset:request_offset + EXACT_IDENTITY_SELECTION_BATCH_SIZE
            ]
            estimated_work = sum(
                len(candidate_bytes)
                for _, candidate_bytes in candidate_batch
                for _, requested_bytes in request_batch
                if len(candidate_bytes) == len(requested_bytes)
            )
            if estimated_work > EXACT_IDENTITY_SELECTION_ESTIMATED_WORK_MAX:
                raise NativeInputBoundError(
                    "Test identity selection exceeds the native step-budget work estimate",
                    reason="estimated_work_exceeds_native_step_budget",
                )
            request = {
                "schema_version": EXACT_IDENTITY_SELECTION_REQUEST_SCHEMA,
                "candidate_count": len(candidate_batch),
                "candidates": [
                    {"test_index": test_index, "identity": identity}
                    for test_index, (identity, _) in zip(
                        index_batch, candidate_batch, strict=True
                    )
                ],
                "requested_count": len(request_batch),
                "requested": [
                    {"request_index": request_offset + index, "identity": identity}
                    for index, (identity, _) in enumerate(request_batch)
                ],
            }
            result = run_native_app(
                "test-exact-identity-selection.json",
                request,
                request_schema=EXACT_IDENTITY_SELECTION_REQUEST_SCHEMA,
                result_schema=EXACT_IDENTITY_SELECTION_RESULT_SCHEMA,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                timeout_seconds=timeout_seconds,
            )
            matches = result.get("matches")
            count = result.get("match_count")
            if (not isinstance(matches, list) or type(count) is not int
                    or count != len(matches) or count != len(request_batch)):
                raise NativeTransportError(
                    "native Test identity selection returned a mismatched result count"
                )
            expected_request_indices = set(
                range(request_offset, request_offset + len(request_batch))
            )
            seen_request_indices: set[int] = set()
            for match in matches:
                if not isinstance(match, dict):
                    raise NativeTransportError(
                        "native Test identity selection returned a malformed match row"
                    )
                request_index = match.get("request_index")
                found = match.get("found")
                test_index = match.get("test_index")
                if (type(request_index) is not int
                        or request_index not in expected_request_indices
                        or request_index in seen_request_indices
                        or not isinstance(found, bool)):
                    raise NativeTransportError(
                        "native Test identity selection returned an invalid request match"
                    )
                seen_request_indices.add(request_index)
                if found:
                    if type(test_index) is not int or test_index not in valid_candidate_indices:
                        raise NativeTransportError(
                            "native Test identity selection returned an out-of-range test index"
                        )
                    # Candidate batches follow inventory order. Keeping the
                    # newest hit preserves the former dict's last-row policy.
                    selected_indices[request_index] = test_index
            if seen_request_indices != expected_request_indices:
                raise NativeTransportError(
                    "native Test identity selection omitted request match rows"
                )
            calls += 1
    return {
        "selected_indices": selected_indices,
        "calls": calls,
        "case_count": len(normalized_candidates),
        "identity_count": len(normalized_requests),
        "selected_count": sum(index is not None for index in selected_indices),
        "missing_count": sum(index is None for index in selected_indices),
    }


def run_execution_adjudication(
    cases: list[dict],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> list[dict]:
    """Ask the native Test application to interpret a bounded result batch."""
    if not isinstance(cases, list) or len(cases) > EXECUTION_ADJUDICATION_MAX_CASES:
        raise NativeTransportError(
            "execution adjudication accepts at most "
            f"{EXECUTION_ADJUDICATION_MAX_CASES} result cases"
        )
    normalized: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise NativeTransportError("execution adjudication cases must be objects")
        if set(case) != {
            "execution_status", "expected_status", "suite",
            "native_result_present", "native_verdict", "native_failure_class",
        }:
            raise NativeTransportError(
                f"execution adjudication case {index} has an invalid field set"
            )
        execution_status = _bounded_adjudication_text(
            case["execution_status"], "execution_status",
        )
        expected_status = _bounded_adjudication_text(
            case["expected_status"], "expected_status",
        )
        native_verdict = _bounded_adjudication_text(
            case["native_verdict"], "native_verdict",
        )
        native_failure_class = _bounded_adjudication_text(
            case["native_failure_class"], "native_failure_class",
        )
        suite = case["suite"]
        native_result_present = case["native_result_present"]
        if type(suite) is not bool or type(native_result_present) is not bool:
            raise NativeTransportError(
                f"execution adjudication case {index} has invalid boolean fields"
            )
        if expected_status and expected_status not in EXECUTION_STATUS_VALUES:
            raise NativeTransportError(
                f"execution adjudication case {index} has an unknown expected status"
            )
        if native_result_present:
            allowed_verdicts = {"PASS", "FAIL", "UNSUPPORTED"}
            if not suite:
                allowed_verdicts.add("SKIP")
            if (execution_status != "returned"
                    or native_verdict not in allowed_verdicts
                    or (suite and native_failure_class)
                    or (not suite and not native_failure_class)):
                raise NativeTransportError(
                    f"execution adjudication case {index} has an invalid native result"
                )
        elif native_verdict or native_failure_class:
            raise NativeTransportError(
                f"execution adjudication case {index} supplied fields without a native result"
            )
        normalized.append({
            "execution_status": execution_status,
            "expected_status": expected_status,
            "suite": suite,
            "native_result_present": native_result_present,
            "native_verdict": native_verdict,
            "native_failure_class": native_failure_class,
        })
    request = {
        "schema_version": EXECUTION_ADJUDICATION_REQUEST_SCHEMA,
        "case_count": len(normalized),
        "cases": normalized,
    }
    result = run_native_app(
        "test-execution-adjudication.json",
        request,
        request_schema=EXECUTION_ADJUDICATION_REQUEST_SCHEMA,
        result_schema=EXECUTION_ADJUDICATION_RESULT_SCHEMA,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    outcomes = result.get("outcomes")
    if (type(result.get("outcome_count")) is not int
            or result.get("outcome_count") != len(normalized)
            or not isinstance(outcomes, list)
            or len(outcomes) != len(normalized)):
        raise NativeTransportError(
            "native execution adjudication returned a mismatched result count"
        )
    for index, value in enumerate(outcomes):
        if not isinstance(value, dict) or set(value) != {
            "decision", "status", "verdict", "failure_class", "expected_failure",
        }:
            raise NativeTransportError(
                f"native execution adjudication outcome {index} is malformed"
            )
        decision = value["decision"]
        status = value["status"]
        verdict = value["verdict"]
        failure_class = value["failure_class"]
        expected_failure = value["expected_failure"]
        if (not isinstance(decision, str)
                or decision not in EXECUTION_ADJUDICATION_DECISIONS
                or not isinstance(status, str)
                or status not in EXECUTION_ADJUDICATION_STATUSES
                or not isinstance(verdict, str)
                or verdict not in EXECUTION_ADJUDICATION_VERDICTS
                or not isinstance(failure_class, str)
                or type(expected_failure) is not bool):
            raise NativeTransportError(
                f"native execution adjudication outcome {index} has invalid values"
            )
        try:
            failure_size = len(failure_class.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise NativeTransportError(
                f"native execution adjudication outcome {index} has invalid UTF-8 text"
            ) from error
        if failure_size > 64:
            raise NativeTransportError(
                f"native execution adjudication outcome {index} exceeds the failure-class bound"
            )
        if decision == "assertion_failure":
            valid_failure = bool(failure_class)
        elif decision == "suite_result":
            valid_failure = not failure_class and not expected_failure
        elif decision == "expected_failure":
            valid_failure = failure_class in EXECUTION_STATUS_VALUES and expected_failure
        else:
            expected_class = {
                "unknown_status": "infrastructure_failure",
                "malformed_result": "malformed_test_result",
                "success": "",
                "skip": "",
                "unsupported_returned": "unsupported",
                "expectation_mismatch": "expectation_mismatch",
                "unsupported_execution": "unsupported",
                "timeout": "timeout",
                "runtime_failure": "runtime_failure",
                "invalid_request": "malformed_test_declaration",
            }.get(decision)
            valid_failure = failure_class == expected_class and not expected_failure
        if not valid_failure:
            raise NativeTransportError(
                f"native execution adjudication outcome {index} violates its decision contract"
            )
    return outcomes


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


def _native_cli_path(value: str, field: str, cwd: Path) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in value or not path.name:
        raise NativeTransportError(
            f"native application {field} path must be relative and remain in the current directory"
        )
    candidate = cwd / path
    cursor = cwd
    for part in path.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise NativeTransportError(
                f"native application {field} path cannot traverse a symbolic link"
            )
    return candidate


def main(argv: list[str] | None = None) -> int:
    """Run one declared native application through the shared transport."""
    parser = argparse.ArgumentParser(description="Run one bounded MNCS native application")
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--request-schema", required=True)
    parser.add_argument("--result-schema", required=True)
    parser.add_argument("--step-budget", type=int, default=1048576)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    descriptor_name = Path(args.descriptor).name
    if descriptor_name != args.descriptor or not descriptor_name.endswith(".json"):
        print("error: descriptor must be a JSON filename", file=sys.stderr)
        return 2
    try:
        if args.step_budget <= 0 or args.timeout <= 0:
            raise NativeTransportError("step budget and timeout must be positive")
        cwd = Path.cwd().resolve()
        request_path = _native_cli_path(args.request, "request", cwd)
        result_path = _native_cli_path(args.result, "result", cwd)
        if request_path == result_path:
            raise NativeTransportError("request and result paths must be different")
        resolved_request = request_path.resolve(strict=True)
        try:
            resolved_request.relative_to(cwd)
        except ValueError as error:
            raise NativeTransportError("request path resolves outside the current directory") from error
        if not resolved_request.is_file():
            raise NativeTransportError("request path is not a regular file")
        if resolved_request.stat().st_size > NATIVE_CLI_REQUEST_MAX_BYTES:
            raise NativeTransportError(
                f"native application request exceeds {NATIVE_CLI_REQUEST_MAX_BYTES} bytes"
            )
        request = json.loads(resolved_request.read_text(encoding="utf-8"))
        result = run_native_app(
            descriptor_name,
            request,
            request_schema=args.request_schema,
            result_schema=args.result_schema,
            mncs=os.environ.get("MNCS", "mncs"),
            cwd=cwd,
            environment=dict(os.environ),
            step_budget=args.step_budget,
            timeout_seconds=args.timeout,
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = result_path.parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(cwd)
        except ValueError as error:
            raise NativeTransportError("result path resolves outside the current directory") from error
        if result_path.is_symlink():
            raise NativeTransportError("result path cannot replace a symbolic link")
        encoded_result = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded_result) > NATIVE_CLI_RESULT_MAX_BYTES:
            raise NativeTransportError(
                f"native application result exceeds {NATIVE_CLI_RESULT_MAX_BYTES} bytes"
            )
        descriptor_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{result_path.name}.",
            suffix=".tmp",
            dir=resolved_parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor_fd, "wb") as stream:
                stream.write(encoded_result)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(result_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return 0
    except (NativeTransportError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
