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
import math
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
from typing import Any, Callable, Iterable

import tomllib
from family_contract import (
    validate_inventory,
    validate_obligation_inventory,
    validate_obligation_plan,
    validate_plan,
)
from mncs_test_native import (
    EXECUTION_ADJUDICATION_MAX_CASES,
    NativeBudgetExhaustedError,
    NativeInputBoundError,
    NativeTransportError,
    run_exact_test_identity_selection,
    run_execution_adjudication,
    run_native_app,
    run_process_transport_adjudication,
    run_runner_policy,
    run_test_inventory_filter,
)

RESULT_SCHEMA = "mncs.test-result/1"
CHECK_SCHEMA = "mncs.check-result/1"
CHECK_RESULT_PROJECTION_REQUEST_SCHEMA = "mncs.test-check-result-projection-request/1"
CHECK_RESULT_PROJECTION_RESULT_SCHEMA = "mncs.test-check-result-projection/1"
PUBLIC_RESULT_JSON_REQUEST_SCHEMA = "mncs.test.public-json-request/1"
PUBLIC_RESULT_JSON_RESULT_SCHEMA = "mncs.test.public-json/1"
PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA = "mncs.test.public-json-window-request/2"
PUBLIC_RESULT_JSON_WINDOW_RESULT_SCHEMA = "mncs.test.public-json-window/2"
PUBLIC_RESULT_JSON_MAX_NODES = 512
PUBLIC_RESULT_JSON_MAX_DEPTH = 63
PUBLIC_RESULT_JSON_WINDOW_BYTES = 8192
PUBLIC_RESULT_JSON_V1_MAX_OUTPUT_BYTES = 8192
PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES = 32684
MANIFEST_SCHEMA = "mncs.test-manifest/1"
DISCOVERY_SCHEMA = "mncs.test-discovery/1"
INVENTORY_SCHEMA = "mncs.test-inventory/1"
EXPERIMENT_PROJECTION_SCHEMA = "mncs.test-experiment/1"
VERIFICATION_PLAN_SCHEMA = "mncs.verification-plan/1"
OBLIGATION_PLAN_SCHEMA = "mncs.verification-obligation-plan/1"
RUNNER_VERSION = "0.2.11"
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
FAMILY_CHECK_MAX_SELECTOR_LENGTH = 4096
FAMILY_CHECK_MAX_TEST_IDENTITY_LENGTH = 512
NATIVE_OBLIGATION_SELECTION_MAX_ITEMS = 16
NATIVE_OBLIGATION_SELECTION_MAX_IDENTITY_LENGTH = 1024
OBLIGATION_SELECTION_REQUEST_SCHEMA = "mncs.test-obligation-selection-request/1"
OBLIGATION_SELECTION_RESULT_SCHEMA = "mncs.test-obligation-selection/1"
VERIFICATION_COHERENCE_REQUEST_SCHEMA = "mncs.test-verification-coherence-request/1"
VERIFICATION_COHERENCE_RESULT_SCHEMA = "mncs.test-verification-coherence/1"
VERIFICATION_COHERENCE_MAX_OBLIGATIONS = 32
VERIFICATION_COHERENCE_MAX_IDENTITIES = 64
VERIFICATION_COHERENCE_MAX_IDENTITY_LENGTH = 1024
COMPILE_VERDICT_REQUEST_SCHEMA = "mncs.test-compile-verdict-request/1"
COMPILE_VERDICT_RESULT_SCHEMA = "mncs.test-compile-verdict/1"
TEST_RESULT_SUMMARY_REQUEST_SCHEMA = "mncs.test-result-summary-request/1"
TEST_RESULT_SUMMARY_RESULT_SCHEMA = "mncs.test-result-summary/1"
TEST_RESULT_IDENTITY_REQUEST_SCHEMA = "mncs.test-result-identity-request/1"
TEST_RESULT_IDENTITY_RESULT_SCHEMA = "mncs.test-result-identity/1"
TEST_RESULT_IDENTITY_STREAM_REQUEST_SCHEMA = "mncs.test-result-identity-stream-request/1"
TEST_RESULT_IDENTITY_STREAM_RESULT_SCHEMA = "mncs.test-result-identity-stream/1"
LIBRARY_TREE_IDENTITY_REQUEST_SCHEMA = "mncs.test-library-tree-identity-request/1"
LIBRARY_TREE_IDENTITY_RESULT_SCHEMA = "mncs.test-library-tree-identity/1"
LIBRARY_TREE_IDENTITY_MAX_ENTRIES = 128
LIBRARY_TREE_IDENTITY_MAX_PATH_BYTES = 1024
LIBRARY_TREE_IDENTITY_MAX_REQUEST_BYTES = 65_536
LIBRARY_TREE_IDENTITY_MAX_NATIVE_LEAF_TOTAL_BYTES = 1024 * 1024
TEST_RESULT_IDENTITY_MAX_BYTES = 65536
TEST_RESULT_IDENTITY_MAX_PAYLOADS = 16
TEST_RESULT_IDENTITY_MAX_CHUNKS = 64
TEST_RESULT_IDENTITY_MAX_STEP_BUDGET = 8_000_000
TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES = 14 * 1024
TEST_RESULT_IDENTITY_STREAM_CHUNKS_PER_REQUEST = 14
TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES = 65_536
TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES = 512 * 1024
TEST_PROVENANCE_FILE_ADAPTER_MAX_BYTES = TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES
# The first few artifact hashes measure the native-app compatibility path
# without paying the one-time backend compile and retained-session open cost.
TEST_RESULT_IDENTITY_EMBED_SESSION_AFTER_ARTIFACTS = 3
TEST_LINEAGE_IDENTITY_REQUEST_SCHEMA = "mncs.test.lineage-identity-request/1"
TEST_LINEAGE_IDENTITY_RESULT_SCHEMA = "mncs.test.lineage-identity/1"
TEST_LINEAGE_IDENTITY_MAX_ROWS = 16
TEST_LINEAGE_IDENTITY_MAX_BYTES = 16384
TEST_LINEAGE_IDENTITY_MAX_STEP_BUDGET = 8_000_000
TEST_RUN_IDENTITY_REQUEST_SCHEMA = "mncs.test.run-identity-request/1"
TEST_RUN_IDENTITY_RESULT_SCHEMA = "mncs.test.run-identity/1"
TEST_RUN_IDENTITY_MAX_TESTS = 64
TEST_RUN_IDENTITY_MAX_TEXT_BYTES = 1024
TEST_RUN_IDENTITY_MAX_BYTES = 8192
TEST_RUN_IDENTITY_MAX_STEP_BUDGET = 8_000_000
COMPILE_VERDICT_EXPECTED_BATCH = 16
COMPILE_VERDICT_ACTUAL_BATCH = 32
MAX_DIAGNOSTIC_CODE_BYTES = 512
MAX_COMPILE_VERDICT_PAIRS = 65_536
MAX_COMPILE_VERDICT_CALLS = 64
MAX_DIAGNOSTIC_EXPECTATIONS = COMPILE_VERDICT_EXPECTED_BATCH * MAX_COMPILE_VERDICT_CALLS

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


def _native_public_result_request(value: Any) -> tuple[dict[str, Any], int] | None:
    """Project one JSON-compatible value into the bounded MNCS serializer tree."""
    nodes: list[dict[str, Any]] = []
    edges: list[int] = []
    active: set[int] = set()
    escaped_text_bytes = 0
    depth_sum = 0

    def encode_text(text: str, maximum: int) -> tuple[list[int], int]:
        nonlocal escaped_text_bytes
        encoded = text.encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("native public JSON text bound exceeded")
        escaped_text_bytes += sum(
            6 if byte < 0x20 else 2 if byte in (0x22, 0x5C) else 1
            for byte in encoded
        )
        return list(encoded), len(encoded)

    def append_node(item: Any, parent: int, key: str, depth: int) -> int:
        nonlocal depth_sum, escaped_text_bytes
        if len(nodes) >= PUBLIC_RESULT_JSON_MAX_NODES or depth > PUBLIC_RESULT_JSON_MAX_DEPTH:
            raise ValueError("native public JSON structure bound exceeded")
        key_bytes, _ = encode_text(key, 256)
        depth_sum += depth
        index = len(nodes)
        kind = 0
        text_bytes: list[int] = []
        integer_value = 0
        children: list[tuple[str, Any]] | None = None
        container_identity: int | None = None

        if type(item) is dict:
            if len(item) > 64:
                raise ValueError("native public JSON object width exceeded")
            children = []
            for child_key, child_value in item.items():
                if type(child_key) is not str:
                    raise ValueError("native public JSON requires string object keys")
                children.append((child_key, child_value))
            kind = 1
            container_identity = id(item)
        elif type(item) in (list, tuple):
            if len(item) + len(nodes) > PUBLIC_RESULT_JSON_MAX_NODES:
                raise ValueError("native public JSON array width exceeded")
            children = [("", child) for child in item]
            kind = 2
            container_identity = id(item)
        elif item is None:
            kind = 8
        elif type(item) is bool:
            kind = 6 if item else 7
            escaped_text_bytes += 4 if item else 5
        elif type(item) is int:
            if not -(1 << 63) <= item < (1 << 63):
                raise ValueError("native public JSON integer is outside signed 64-bit range")
            kind = 4
            escaped_text_bytes += len(str(item))
            integer_value = item
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("native public JSON does not accept non-finite numbers")
            kind = 5
            text_bytes, _ = encode_text(repr(item), 1024)
        elif type(item) is str:
            kind = 3
            text_bytes, _ = encode_text(item, 1024)
        else:
            raise ValueError("native public JSON value is outside the JSON data model")

        if children is not None:
            if container_identity in active:
                raise ValueError("native public JSON input contains a cycle")
            if len(edges) + len(children) > PUBLIC_RESULT_JSON_MAX_NODES - 1:
                raise ValueError("native public JSON edge bound exceeded")

        first_edge = len(edges)
        node = {
            "kind": kind,
            "parent": parent,
            "key": key_bytes,
            "text": text_bytes,
            "first_edge": first_edge,
            "child_count": len(children) if children is not None else 0,
            "integer_value": integer_value,
        }
        nodes.append(node)
        if children is not None:
            if container_identity is not None:
                active.add(container_identity)
            try:
                edges.extend([0] * len(children))
                for offset, (child_key, child_value) in enumerate(children):
                    child_index = append_node(child_value, index, child_key, depth + 1)
                    edges[first_edge + offset] = child_index
            finally:
                if container_identity is not None:
                    active.remove(container_identity)
        return index

    try:
        append_node(value, 0, "", 0)
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError, RuntimeError):
        return None

    output_upper_bound = escaped_text_bytes + 24 * len(nodes) + 4 * depth_sum + 32
    if output_upper_bound > PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES:
        return None
    return (
        {
            "schema_version": PUBLIC_RESULT_JSON_REQUEST_SCHEMA,
            "nodes": nodes,
            "edge_count": len(edges),
            "edges": edges + [0] * (PUBLIC_RESULT_JSON_MAX_NODES - 1 - len(edges)),
        },
        output_upper_bound,
    )


def _decode_public_result_chunks(
    result: dict[str, Any], *, byte_length: int, maximum_chunks: int
) -> bytes:
    raw_chunks = result.get("chunks")
    chunk_count = result.get("chunk_count")
    if (
        not isinstance(raw_chunks, list)
        or not isinstance(chunk_count, int)
        or isinstance(chunk_count, bool)
        or chunk_count != len(raw_chunks)
        or not 0 <= chunk_count <= maximum_chunks
    ):
        raise AdapterError("MNCS public-result JSON app returned malformed chunk metadata")

    document = bytearray()
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            raise AdapterError("MNCS public-result JSON app returned a malformed chunk")
        raw_bytes = chunk.get("bytes")
        length = chunk.get("length")
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or not 0 <= length <= 1024
        ):
            raise AdapterError("MNCS public-result JSON app returned an invalid chunk length")
        if isinstance(raw_bytes, str):
            try:
                encoded = raw_bytes.encode("utf-8")
            except UnicodeEncodeError as error:
                raise AdapterError("MNCS public-result JSON app returned invalid UTF-8") from error
        elif isinstance(raw_bytes, list) and all(
            isinstance(byte, int) and not isinstance(byte, bool) and 0 <= byte <= 255
            for byte in raw_bytes
        ):
            encoded = bytes(raw_bytes)
        else:
            raise AdapterError("MNCS public-result JSON app returned a malformed byte chunk")
        if length > len(encoded):
            raise AdapterError("MNCS public-result JSON app returned a short byte chunk")
        document.extend(encoded[:length])

    if len(document) != byte_length:
        raise AdapterError("MNCS public-result JSON app returned a short document window")
    return bytes(document)


def _native_public_result_payload(
    value: Any,
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> tuple[bytes, str] | None:
    """Execute MNCS serialization for values inside its typed transport bounds."""
    prepared = _native_public_result_request(value)
    if prepared is None:
        return None
    request, output_upper_bound = prepared
    if output_upper_bound <= PUBLIC_RESULT_JSON_V1_MAX_OUTPUT_BYTES:
        try:
            result = run_native_app(
                "test-public-result-json.json",
                request,
                request_schema=PUBLIC_RESULT_JSON_REQUEST_SCHEMA,
                result_schema=PUBLIC_RESULT_JSON_RESULT_SCHEMA,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                step_budget=8_000_000,
            )
        except NativeBudgetExhaustedError:
            return None
        except NativeTransportError as error:
            raise AdapterError(f"native Test public result JSON failed: {error}") from error

        byte_length = result.get("byte_length")
        digest = result.get("sha256")
        if (
            not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or not 0 <= byte_length <= PUBLIC_RESULT_JSON_V1_MAX_OUTPUT_BYTES
            or not isinstance(digest, str)
            or not SHA256_HEX.fullmatch(digest)
        ):
            raise AdapterError("MNCS public-result JSON app returned malformed metadata")
        payload = _decode_public_result_chunks(result, byte_length=byte_length, maximum_chunks=16)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise AdapterError("MNCS public-result JSON app returned an inconsistent document digest")
        return payload, digest

    document_length: int | None = None
    document = bytearray()
    window_start = 0
    continuation: list[dict[str, int]] = []
    visited = 0
    hash_state: dict[str, Any] | None = None
    digest: str | None = None
    for _ in range(32):
        window_request = {
            "schema_version": PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA,
            "window_start": window_start,
            "visited": visited,
            "has_hash_state": hash_state is not None,
            "hash_state": hash_state or {
                "h": [0] * 8,
                "total": 0,
                "buf": [0] * 64,
                "buffered": 0,
            },
            "stack": continuation,
            "nodes": request["nodes"],
            "edge_count": request["edge_count"],
            "edges": request["edges"],
        }
        try:
            result = run_native_app(
                "test-public-result-json-window.json",
                window_request,
                request_schema=PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA,
                result_schema=PUBLIC_RESULT_JSON_WINDOW_RESULT_SCHEMA,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                step_budget=8_000_000,
            )
        except NativeBudgetExhaustedError:
            return None
        except NativeTransportError as error:
            raise AdapterError(f"native Test public result JSON window failed: {error}") from error

        total = result.get("document_length")
        returned_start = result.get("window_start")
        byte_length = result.get("byte_length")
        next_offset = result.get("next_offset")
        done = result.get("done")
        next_visited = result.get("visited")
        next_stack = result.get("stack")
        if (
            result.get("schema_version") != PUBLIC_RESULT_JSON_WINDOW_RESULT_SCHEMA
            or not isinstance(total, int)
            or isinstance(total, bool)
            or not 0 <= total <= PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES
            or not isinstance(returned_start, int)
            or isinstance(returned_start, bool)
            or returned_start != window_start
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or not 0 <= byte_length <= PUBLIC_RESULT_JSON_WINDOW_BYTES
            or not isinstance(next_offset, int)
            or isinstance(next_offset, bool)
            or next_offset != window_start + byte_length
            or next_offset > PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES
            or type(done) is not bool
            or not isinstance(next_visited, int)
            or isinstance(next_visited, bool)
            or not 0 <= next_visited <= PUBLIC_RESULT_JSON_MAX_NODES
            or not isinstance(next_stack, list)
            or len(next_stack) > 64
        ):
            raise AdapterError("MNCS public-result JSON window app returned malformed metadata")
        if (done and (total != next_offset or next_stack or next_visited != len(request["nodes"]))) or (
            not done and (total != 0 or byte_length == 0 or not next_stack)
        ):
            raise AdapterError("MNCS public-result JSON window returned inconsistent continuation state")
        window_payload = _decode_public_result_chunks(
            result, byte_length=byte_length, maximum_chunks=16
        )
        document.extend(window_payload)
        returned_state = result.get("hash_state")
        returned_h = returned_state.get("h") if isinstance(returned_state, dict) else None
        returned_buf = returned_state.get("buf") if isinstance(returned_state, dict) else None
        returned_total = returned_state.get("total") if isinstance(returned_state, dict) else None
        returned_buffered = returned_state.get("buffered") if isinstance(returned_state, dict) else None
        if isinstance(returned_buf, str):
            returned_buf = list(returned_buf.encode("utf-8"))
        if (
            not isinstance(returned_h, list)
            or len(returned_h) != 8
            or any(
                not isinstance(word, int)
                or isinstance(word, bool)
                or not 0 <= word <= 0xFFFFFFFF
                for word in returned_h
            )
            or not isinstance(returned_buf, list)
            or len(returned_buf) != 64
            or any(
                not isinstance(byte, int)
                or isinstance(byte, bool)
                or not 0 <= byte <= 255
                for byte in returned_buf
            )
            or not isinstance(returned_total, int)
            or isinstance(returned_total, bool)
            or returned_total != next_offset
            or not isinstance(returned_buffered, int)
            or isinstance(returned_buffered, bool)
            or not 0 <= returned_buffered < 64
            or returned_buffered != returned_total % 64
            or (done and (
                not isinstance(result.get("sha256"), str)
                or not SHA256_HEX.fullmatch(result["sha256"])
            ))
        ):
            raise AdapterError("MNCS public-result JSON window returned malformed hash state")
        if done:
            native_digest = result.get("sha256")
            if not isinstance(native_digest, str) or not SHA256_HEX.fullmatch(native_digest):
                raise AdapterError("MNCS public-result JSON window returned a malformed digest")
            digest = native_digest
        else:
            hash_state = {
                "h": returned_h,
                "total": returned_total,
                "buf": returned_buf,
                "buffered": returned_buffered,
            }
        if done:
            document_length = total
            break
        if any(
            not isinstance(frame, dict)
            or not isinstance(frame.get("node"), int)
            or isinstance(frame.get("node"), bool)
            or not 0 <= frame["node"] < PUBLIC_RESULT_JSON_MAX_NODES
            or not isinstance(frame.get("next_child"), int)
            or isinstance(frame.get("next_child"), bool)
            or not 0 <= frame["next_child"] < PUBLIC_RESULT_JSON_MAX_NODES
            or not isinstance(frame.get("depth"), int)
            or isinstance(frame.get("depth"), bool)
            or not 0 <= frame["depth"] < 64
            for frame in next_stack
        ):
            raise AdapterError("MNCS public-result JSON window returned malformed continuation frames")
        continuation = next_stack
        visited = next_visited
        window_start = next_offset
    else:
        raise AdapterError("MNCS public-result JSON exceeded its native continuation count")

    payload = bytes(document)
    if document_length is None or len(payload) != document_length or digest is None:
        raise AdapterError("MNCS public-result JSON windows did not cover the document")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise AdapterError("MNCS Test native digest disagrees with the independent SHA-256 check")
    return payload, digest


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


def library_tree_identity_sha256(
    entries: list[tuple[str, str]],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    """Hash bounded ordered path/leaf records through the Test MNCS app."""
    if len(entries) > LIBRARY_TREE_IDENTITY_MAX_ENTRIES:
        raise NativeInputBoundError(
            "library tree exceeds the 128-entry native identity bound",
            reason="library_tree_entry_count",
        )
    wire_entries: list[dict[str, list[int]]] = []
    for relative, leaf_digest in entries:
        path_bytes = relative.encode("utf-8")
        if not path_bytes or len(path_bytes) > LIBRARY_TREE_IDENTITY_MAX_PATH_BYTES:
            raise NativeInputBoundError(
                "library tree path exceeds the 1,024-byte native identity bound",
                reason="library_tree_path_length",
            )
        if not SHA256_HEX.fullmatch(leaf_digest):
            raise AdapterError("library tree leaf digest is not lowercase SHA-256 hex")
        wire_entries.append(
            {
                "path": list(path_bytes),
                "leaf_sha256": list(leaf_digest.encode("ascii")),
            }
        )

    request = {
        "schema_version": LIBRARY_TREE_IDENTITY_REQUEST_SCHEMA,
        "entry_count": len(wire_entries),
        "entries": wire_entries,
    }
    if len(json.dumps(request).encode("utf-8")) > LIBRARY_TREE_IDENTITY_MAX_REQUEST_BYTES:
        raise NativeInputBoundError(
            "library tree identity request exceeds its 65,536-byte native bound",
            reason="library_tree_request_bytes",
        )
    result = run_native_app(
        "test-library-tree-identity.json",
        request,
        request_schema=LIBRARY_TREE_IDENTITY_REQUEST_SCHEMA,
        result_schema=LIBRARY_TREE_IDENTITY_RESULT_SCHEMA,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
        step_budget=TEST_RESULT_IDENTITY_MAX_STEP_BUDGET,
    )
    digest = result.get("digest")
    if (
        result.get("entry_count") != len(entries)
        or not isinstance(digest, str)
        or not SHA256_HEX.fullmatch(digest)
    ):
        raise AdapterError("MNCS library tree identity returned an invalid digest result")
    return digest


def _library_tree_native_leaf_fallback_reason(
    entries: list[tuple[Path, str]],
    *,
    artifacts: ArtifactStore | None,
) -> str | None:
    """Return why bounded leaf hashing cannot use the existing MNCS adapter."""
    if len(entries) > LIBRARY_TREE_IDENTITY_MAX_ENTRIES:
        return "library_tree_entry_count"
    if artifacts is None or artifacts.mncs is None:
        return "native_context_unavailable"
    if artifacts.cwd is None or artifacts.environment is None or artifacts._closed:
        return "native_context_unavailable"

    encoded_paths: list[list[int]] = []
    total_bytes = 0
    for path, relative in entries:
        path_bytes = relative.encode("utf-8")
        if not path_bytes or len(path_bytes) > LIBRARY_TREE_IDENTITY_MAX_PATH_BYTES:
            return "library_tree_path_length"
        try:
            total_bytes += path.stat().st_size
        except OSError:
            return "library_leaf_size_unavailable"
        encoded_paths.append(list(path_bytes))
    if total_bytes > LIBRARY_TREE_IDENTITY_MAX_NATIVE_LEAF_TOTAL_BYTES:
        return "library_tree_native_leaf_byte_bound"

    # Each hexadecimal digest byte is at most 102, so this request is an
    # upper bound on the eventual JSON wire size before reading any leaf.
    request = {
        "schema_version": LIBRARY_TREE_IDENTITY_REQUEST_SCHEMA,
        "entry_count": len(entries),
        "entries": [
            {"path": path_bytes, "leaf_sha256": [102] * 64}
            for path_bytes in encoded_paths
        ],
    }
    if len(json.dumps(request).encode("utf-8")) > LIBRARY_TREE_IDENTITY_MAX_REQUEST_BYTES:
        return "library_tree_request_bytes"
    return None


def _hash_tree_with_method(
    path: Path,
    *,
    mncs: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    artifacts: ArtifactStore | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the tree digest and report whether MNCS or the host computed it.

    Filesystem enumeration and leaf reads stay in the adapter. For bounded
    directories, MNCS owns the ordered `path NUL leaf-digest LF` fold. The
    historical host hash remains an explicit fallback for unsupported bounds
    and unavailable native execution.
    """
    if path.is_file():
        return sha256_file(path), {
            "algorithm": "sha256-file/1",
            "implementation": "host",
        }
    if not path.is_dir():
        return sha256_bytes(f"missing:{path}".encode()), {
            "algorithm": "sha256-missing-path/1",
            "implementation": "host",
        }
    file_entries: list[tuple[Path, str]] = []
    for root, directories, names in os.walk(path, followlinks=False):
        directories.sort()
        names.sort()
        root_path = Path(root)
        for name in names:
            item = root_path / name
            relative = item.relative_to(path).as_posix()
            file_entries.append((item, relative))

    entries: list[tuple[str, str]] = []
    leaf_fallback = _library_tree_native_leaf_fallback_reason(
        file_entries,
        artifacts=artifacts,
    )
    leaf_hashing: dict[str, Any] = {
        "algorithm": "sha256-file/1",
        "native_implementation": "mncs.test.result_identity_v1",
        "native_entries": 0,
        "native_bytes": 0,
        "native_transport_counts": {},
        "host_entries": 0,
        "host_bytes": 0,
        "host_fallback_reasons": {},
    }
    for item, relative in file_entries:
        try:
            if leaf_fallback is None and artifacts is not None:
                item_hash, leaf_method = artifacts.digest_file_with_method(item)
            else:
                item_hash = sha256_file(item)
                leaf_method = {
                    "implementation": "host",
                    "fallback_reason": leaf_fallback or "native_context_unavailable",
                }
            item_size = item.stat().st_size
        except OSError:
            item_hash = sha256_bytes(f"unreadable:{relative}".encode())
            item_size = 0
            leaf_method = {
                "implementation": "host",
                "fallback_reason": "library_leaf_unreadable",
            }
        entries.append((relative, item_hash))
        if leaf_method.get("implementation") in {"mncs-application", "mncs-embed"}:
            leaf_hashing["native_entries"] += 1
            leaf_hashing["native_bytes"] += item_size
            transport = leaf_method["implementation"]
            transports = leaf_hashing["native_transport_counts"]
            transports[transport] = transports.get(transport, 0) + 1
        else:
            leaf_hashing["host_entries"] += 1
            leaf_hashing["host_bytes"] += item_size
            reason = leaf_method.get("fallback_reason", "host_compatibility")
            fallbacks = leaf_hashing["host_fallback_reasons"]
            fallbacks[reason] = fallbacks.get(reason, 0) + 1

    if mncs is not None and cwd is not None and environment is not None:
        try:
            digest = library_tree_identity_sha256(
                entries,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
            )
            return digest, {
                "algorithm": "sha256-library-tree-records/1",
                "implementation": "mncs.test.library_tree_identity_v1",
                "entries": len(entries),
                "leaf_hashing": leaf_hashing,
            }
        except NativeInputBoundError as error:
            fallback_reason = error.reason
            fallback_detail = str(error)
        except NativeBudgetExhaustedError as error:
            fallback_reason = "native_budget_exhausted"
            fallback_detail = str(error)
        except NativeTransportError as error:
            fallback_reason = "native_transport_failure"
            fallback_detail = str(error)
        except AdapterError as error:
            fallback_reason = "native_result_invalid"
            fallback_detail = str(error)
    else:
        fallback_reason = "native_context_unavailable"
        fallback_detail = None

    material = "".join(f"{name}\0{digest}\n" for name, digest in entries).encode()
    hashing: dict[str, Any] = {
        "algorithm": "sha256-library-tree-records/1",
        "implementation": "host",
        "entries": len(entries),
        "fallback_reason": fallback_reason,
        "leaf_hashing": leaf_hashing,
    }
    if fallback_detail:
        hashing["fallback_detail"] = fallback_detail[:512]
    return sha256_bytes(material), hashing


def hash_tree(path: Path) -> str:
    """Compatibility helper preserving the existing directory identity."""
    return _hash_tree_with_method(path)[0]


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


def native_compile_verdict(
    diagnostics: Iterable[dict[str, Any]],
    expected: list[str],
    *,
    compiler_exit_code: int,
    compiler_result_is_object: bool,
    valid_field_present: bool,
    valid_field_is_true: bool,
    expects_failure: bool,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    """Carry compiler fields to MNCS and return its compile-only verdict."""
    if type(compiler_exit_code) is not int:
        raise AdapterError("compiler exit status is not an integer")
    if any(type(value) is not bool for value in (
        compiler_result_is_object, valid_field_present, valid_field_is_true, expects_failure,
    )):
        raise AdapterError("compiler result metadata is malformed")
    if len(expected) > MAX_DIAGNOSTIC_EXPECTATIONS:
        raise AdapterError("compile verdict exceeds the bounded native invocation limit")

    expected_codes: list[str] = []
    for item in (expected if expects_failure else []):
        if not isinstance(item, str):
            raise ManifestError("diagnostic expectations must be text prefixes")
        try:
            if len(item.encode("utf-8")) > MAX_DIAGNOSTIC_CODE_BYTES:
                raise ManifestError(
                    "diagnostic expectation exceeds the native 512-byte prefix bound"
                )
        except UnicodeEncodeError as error:
            raise ManifestError("diagnostic expectations must be valid UTF-8") from error
        expected_codes.append(item)
    expected_batch_count = (
        len(expected_codes) + COMPILE_VERDICT_EXPECTED_BATCH - 1
    ) // COMPILE_VERDICT_EXPECTED_BATCH
    expected_batch_count = max(1, expected_batch_count)
    if expected_batch_count > MAX_COMPILE_VERDICT_CALLS:
        raise AdapterError("compile verdict exceeds the bounded native invocation limit")

    def bounded_code(item: dict[str, Any]) -> str:
        try:
            raw = str(item.get("code", "")).encode("utf-8")
        except UnicodeEncodeError as error:
            raise AdapterError("compiler diagnostic codes must be valid UTF-8") from error
        # Prefix matching only observes as many actual bytes as an expected
        # prefix can contain. Truncating preserves that decision and keeps each
        # native record bounded.
        return raw[:MAX_DIAGNOSTIC_CODE_BYTES].decode("utf-8", errors="ignore")

    if not expects_failure:
        actual_codes: list[str] = []
    elif not expected_codes:
        # The native policy only asks whether a diagnostic exists in this
        # case, so forwarding one real diagnostic keeps the request bounded.
        absent = object()
        first = next(iter(diagnostics), absent)
        actual_codes = [bounded_code(first)] if first is not absent else []
    else:
        actual_codes: list[str] = []
        maximum_actual_codes = (
            MAX_COMPILE_VERDICT_CALLS // expected_batch_count
        ) * COMPILE_VERDICT_ACTUAL_BATCH
        for item in diagnostics:
            if len(actual_codes) >= maximum_actual_codes:
                raise AdapterError(
                    "compile verdict exceeds the bounded native invocation limit"
                )
            actual_codes.append(bounded_code(item))

    if len(expected_codes) * len(actual_codes) > MAX_COMPILE_VERDICT_PAIRS:
        raise AdapterError("compile verdict exceeds the bounded native comparison limit")

    expected_batches = [
        expected_codes[index:index + COMPILE_VERDICT_EXPECTED_BATCH]
        for index in range(0, len(expected_codes), COMPILE_VERDICT_EXPECTED_BATCH)
    ] or [[]]
    actual_batches = [
        actual_codes[index:index + COMPILE_VERDICT_ACTUAL_BATCH]
        for index in range(0, len(actual_codes), COMPILE_VERDICT_ACTUAL_BATCH)
    ] or [[]]
    if len(expected_batches) * len(actual_batches) > MAX_COMPILE_VERDICT_CALLS:
        raise AdapterError("compile verdict exceeds the bounded native invocation limit")

    all_expected_before = True
    final_verdict: str | None = None
    for expected_batch in expected_batches:
        matched_before = [False] * COMPILE_VERDICT_EXPECTED_BATCH
        diagnostics_seen_before = False
        final_match: bool | None = None
        batch_verdict: str | None = None
        for actual_batch in actual_batches:
            request = {
                "schema_version": COMPILE_VERDICT_REQUEST_SCHEMA,
                "compiler_exit_code": compiler_exit_code,
                "compiler_result_is_object": compiler_result_is_object,
                "valid_field_present": valid_field_present,
                "valid_field_is_true": valid_field_is_true,
                "expects_failure": expects_failure,
                "expected_count": len(expected_batch),
                "expected": [{"value": item} for item in expected_batch],
                "actual_count": len(actual_batch),
                "actual": [{"value": item} for item in actual_batch],
                "matched_before": matched_before,
                "all_expected_before": all_expected_before,
                "diagnostics_seen_before": diagnostics_seen_before,
            }
            try:
                result = run_native_app(
                    "test-compile-verdict.json",
                    request,
                    request_schema=COMPILE_VERDICT_REQUEST_SCHEMA,
                    result_schema=COMPILE_VERDICT_RESULT_SCHEMA,
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    step_budget=1_048_576,
                )
            except NativeTransportError as error:
                raise AdapterError(f"native compile verdict failed: {error}") from error

            returned_count = result.get("expected_count")
            returned_matches = result.get("matched")
            returned_all = result.get("all_matched")
            returned_seen = result.get("diagnostics_seen")
            if (
                type(returned_count) is not int
                or returned_count != len(expected_batch)
                or not isinstance(returned_matches, list)
                or len(returned_matches) != COMPILE_VERDICT_EXPECTED_BATCH
                or any(type(value) is not bool for value in returned_matches)
                or type(returned_all) is not bool
                or type(returned_seen) is not bool
            ):
                raise AdapterError("MNCS compile-verdict app returned malformed state")
            verdict = result.get("verdict")
            if not isinstance(verdict, str) or verdict not in {
                "compile_pass", "expected_failure", "compile_expectation_mismatch",
                "diagnostic_mismatch", "compile_failure",
            }:
                raise AdapterError("MNCS compile-verdict app returned an unknown verdict")
            matched_before = returned_matches
            diagnostics_seen_before = returned_seen
            final_match = returned_all
            batch_verdict = verdict
        if final_match is None:
            raise AdapterError("MNCS compile-verdict app returned no batch decision")
        if batch_verdict is None:
            raise AdapterError("MNCS compile-verdict app returned no verdict")
        all_expected_before = final_match
        final_verdict = batch_verdict
    if final_verdict is None:
        raise AdapterError("MNCS compile-verdict app returned no verdict")
    return final_verdict


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
        "failure_kind_name": failure_kind,
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


def native_result_summary(
    test_results: list[dict[str, Any]],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, int]:
    """Transport result-row facts to MNCS and return its bounded summary."""
    if len(test_results) > 256:
        raise AdapterError("test result summary exceeds the 256-row native bound")

    def bounded_text(value: str, field: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise AdapterError(f"test result {field} must be valid UTF-8") from error
        if size > 64:
            raise AdapterError(f"test result {field} exceeds the 64-byte native bound")
        return value

    entries: list[dict[str, Any]] = []
    maximum_count = (1 << 64) - 1
    for item in test_results:
        if not isinstance(item, dict):
            raise AdapterError("test result summary rows must be objects")
        verdict = item.get("verdict")
        if not isinstance(verdict, str) or verdict not in {"PASS", "FAIL", "UNKNOWN"}:
            raise AdapterError("test result summary row has an unknown verdict")
        status = item.get("status", "")
        if not isinstance(status, str):
            raise AdapterError("test result summary row status must be text")
        failure = item.get("failure")
        failure_class = (
            str(failure.get("class", "")) if isinstance(failure, dict) else ""
        )
        native = item.get("native_result")
        counts: dict[str, int] = {}
        if isinstance(native, dict):
            for field in ("assertions", "failures"):
                try:
                    value = int(native.get(field) or 0)
                except (TypeError, ValueError, OverflowError) as error:
                    raise AdapterError(
                        f"native test result {field} count is not an integer"
                    ) from error
                if not 0 <= value <= maximum_count:
                    raise AdapterError(
                        f"native test result {field} count is outside the u64 range"
                    )
                counts[field] = value
        else:
            counts = {"assertions": 0, "failures": 0}
        entries.append(
            {
                "verdict": bounded_text(verdict, "verdict"),
                "status": bounded_text(status, "status"),
                "failure_class": bounded_text(failure_class, "failure class"),
                **counts,
            }
        )

    request = {
        "schema_version": TEST_RESULT_SUMMARY_REQUEST_SCHEMA,
        "test_count": len(entries),
        "tests": entries,
    }
    try:
        result = run_native_app(
            "test-result-summary.json",
            request,
            request_schema=TEST_RESULT_SUMMARY_REQUEST_SCHEMA,
            result_schema=TEST_RESULT_SUMMARY_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
    except NativeTransportError as error:
        raise AdapterError(f"native Test result summary failed: {error}") from error

    returned = result.get("summary")
    fields = (
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
    if not isinstance(returned, dict):
        raise AdapterError("MNCS result-summary app returned no summary record")
    summary = {field: returned.get(field) for field in fields}
    if any(
        type(value) is not int or not 0 <= value <= maximum_count
        for value in summary.values()
    ):
        raise AdapterError("MNCS result-summary app returned malformed counters")
    return {field: int(value) for field, value in summary.items()}


def result_identity_sha256(
    payload: bytes,
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    """Hash one identity payload through the bounded MNCS SHA-256 app."""
    return result_identity_sha256_many(
        [payload], mncs=mncs, cwd=cwd, environment=environment
    )[0]


def result_identity_sha256_batch(
    payloads: list[bytes],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> list[str]:
    """Hash a bounded set of payloads through the MNCS result-identity app."""
    if not isinstance(payloads, list) or len(payloads) > TEST_RESULT_IDENTITY_MAX_PAYLOADS:
        raise NativeInputBoundError(
            "result identity batch exceeds its 16-payload native bound",
            reason="result_identity_payload_count",
        )
    if any(not isinstance(payload, bytes) for payload in payloads):
        raise AdapterError("Test result identity payloads must be bytes")
    total_bytes = sum(len(payload) for payload in payloads)
    if total_bytes > TEST_RESULT_IDENTITY_MAX_BYTES:
        raise NativeInputBoundError(
            "result identity batch exceeds its 65,536-byte native bound",
            reason="result_identity_payload_bytes",
        )

    chunks: list[list[int]] = []
    slices: list[dict[str, int]] = []
    for payload in payloads:
        first_chunk = len(chunks)
        for offset in range(0, len(payload), 1024):
            chunks.append(list(payload[offset:offset + 1024]))
        slices.append({
            "first_chunk": first_chunk,
            "chunk_count": len(chunks) - first_chunk,
        })
    if len(chunks) > TEST_RESULT_IDENTITY_MAX_CHUNKS:
        raise NativeInputBoundError(
            "result identity batch exceeds its 64-chunk native bound",
            reason="result_identity_chunk_count",
        )

    request = {
        "schema_version": TEST_RESULT_IDENTITY_REQUEST_SCHEMA,
        "payload_count": len(payloads),
        "chunk_count": len(chunks),
        "payloads": slices,
        "chunks": chunks,
    }
    step_budget = result_identity_step_budget(payloads)
    if step_budget > TEST_RESULT_IDENTITY_MAX_STEP_BUDGET:
        raise NativeInputBoundError(
            "result identity batch exceeds the 8,000,000-step native bound",
            reason="result_identity_step_budget",
        )
    try:
        result = run_native_app(
            "test-result-identity.json",
            request,
            request_schema=TEST_RESULT_IDENTITY_REQUEST_SCHEMA,
            result_schema=TEST_RESULT_IDENTITY_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            step_budget=step_budget,
        )
    except NativeTransportError as error:
        raise AdapterError(f"native Test result identity failed: {error}") from error

    digests = result.get("digests")
    if (
        result.get("payload_count") != len(payloads)
        or not isinstance(digests, list)
        or len(digests) != len(payloads)
    ):
        raise AdapterError("MNCS result-identity app returned malformed digest records")
    output: list[str] = []
    for item in digests:
        if not isinstance(item, dict):
            raise AdapterError("MNCS result-identity app returned a malformed digest record")
        digest = item.get("digest")
        if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
            raise AdapterError("MNCS result-identity app returned a malformed digest")
        output.append(digest)
    return output


def _bounded_result_identity_stream_chunks(chunks: Iterable[bytes]) -> Iterable[bytes]:
    """Coalesce source reads into fixed, SHA-block-aligned MNCS windows."""
    pending = bytearray()
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise AdapterError("Test artifact hash stream must yield byte chunks")
        offset = 0
        while offset < len(chunk):
            take = min(
                TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES - len(pending),
                len(chunk) - offset,
            )
            pending.extend(chunk[offset : offset + take])
            offset += take
            if len(pending) == TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def _result_identity_stream_state(
    result: dict[str, Any], *, expected_total: int, finalized: bool
) -> tuple[dict[str, Any] | None, str | None]:
    if (
        result.get("schema_version") != TEST_RESULT_IDENTITY_STREAM_RESULT_SCHEMA
        or type(result.get("finalized")) is not bool
        or result["finalized"] is not finalized
    ):
        raise AdapterError("MNCS result-identity stream returned malformed metadata")

    raw_state = result.get("state")
    if not isinstance(raw_state, dict):
        raise AdapterError("MNCS result-identity stream returned a malformed SHA-256 state")
    h = raw_state.get("h")
    total = raw_state.get("total")
    buffered = raw_state.get("buffered")
    buf = raw_state.get("buf")
    if (
        not isinstance(h, list)
        or len(h) != 8
        or any(
            not isinstance(word, int)
            or isinstance(word, bool)
            or not 0 <= word <= 0xFFFFFFFF
            for word in h
        )
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total != expected_total
        or not isinstance(buffered, int)
        or isinstance(buffered, bool)
        or not 0 <= buffered < 64
        or buffered != expected_total % 64
    ):
        raise AdapterError("MNCS result-identity stream returned an inconsistent SHA-256 state")

    if isinstance(buf, str):
        valid_buffer_shape = len(buf) == 64
        empty_buffer = buf == "\x00" * 64
    elif isinstance(buf, list):
        valid_buffer_shape = len(buf) == 64 and all(
            isinstance(byte, int)
            and not isinstance(byte, bool)
            and 0 <= byte <= 255
            for byte in buf
        )
        empty_buffer = valid_buffer_shape and not any(buf)
    else:
        valid_buffer_shape = False
        empty_buffer = False
    if not valid_buffer_shape or (not finalized and (buffered != 0 or not empty_buffer)):
        raise AdapterError("MNCS result-identity stream returned malformed buffered bytes")

    digest = result.get("digest")
    if finalized:
        if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
            raise AdapterError("MNCS result-identity stream returned a malformed digest")
        return None, digest
    if not isinstance(digest, str) or digest != "0" * 64:
        raise AdapterError("MNCS result-identity stream returned a digest before finalization")
    return {
        "h": h,
        "total": total,
        "buf": [0] * 64,
        "buffered": buffered,
    }, None


def _native_result_identity_stream_digest(
    chunks: Iterable[bytes],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    """Hash a bounded artifact as MNCS-owned SHA-256 state windows."""
    source = iter(_bounded_result_identity_stream_chunks(chunks))
    try:
        current = next(source)
    except StopIteration:
        current = b""

    total = 0
    hash_state: dict[str, Any] | None = None
    while True:
        try:
            following = next(source)
            finalized = False
        except StopIteration:
            following = None
            finalized = True

        request = {
            "schema_version": TEST_RESULT_IDENTITY_STREAM_REQUEST_SCHEMA,
            "has_state": hash_state is not None,
            "state": hash_state or {
                "h": [0] * 8,
                "total": 0,
                "buf": [0] * 64,
                "buffered": 0,
            },
            "chunks": [
                list(current[offset : offset + 1024])
                for offset in range(0, len(current), 1024)
            ],
            "finalize": finalized,
        }
        if len(request["chunks"]) > TEST_RESULT_IDENTITY_STREAM_CHUNKS_PER_REQUEST:
            raise AdapterError("Test artifact hash stream exceeded its bounded MNCS window")
        step_budget = result_identity_step_budget([current])
        if step_budget > TEST_RESULT_IDENTITY_MAX_STEP_BUDGET:
            raise AdapterError("Test artifact hash stream exceeded the MNCS step budget")
        try:
            result = run_native_app(
                "test-result-identity-stream.json",
                request,
                request_schema=TEST_RESULT_IDENTITY_STREAM_REQUEST_SCHEMA,
                result_schema=TEST_RESULT_IDENTITY_STREAM_RESULT_SCHEMA,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                step_budget=step_budget,
            )
        except NativeTransportError as error:
            raise AdapterError(f"native Test artifact hash stream failed: {error}") from error

        total += len(current)
        hash_state, digest = _result_identity_stream_state(
            result, expected_total=total, finalized=finalized
        )
        if finalized:
            if digest is None:
                raise AdapterError("MNCS result-identity stream did not finalize its digest")
            return digest
        if hash_state is None or following is None:
            raise AdapterError("MNCS result-identity stream returned no continuation state")
        current = following


def result_identity_sha256_many(
    payloads: list[bytes],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> list[str]:
    """Hash an ordered payload list in bounded MNCS batches.

    The shared Test application accepts at most 16 payloads, 64 chunks, 65,536
    bytes, and 8,000,000 estimated steps per call. This adapter batches larger
    lists without changing their order. A single payload that exceeds those
    bounds retains the compatibility hash path.
    """
    if not isinstance(payloads, list):
        raise AdapterError("Test result identity payloads must be a list")
    if any(not isinstance(payload, bytes) for payload in payloads):
        raise AdapterError("Test result identity payloads must be bytes")

    digests: list[str] = []
    batch: list[bytes] = []
    for payload in payloads:
        payload_chunks = (len(payload) + 1023) // 1024
        if (
            len(payload) > TEST_RESULT_IDENTITY_MAX_BYTES
            or payload_chunks > TEST_RESULT_IDENTITY_MAX_CHUNKS
            or result_identity_step_budget([payload]) > TEST_RESULT_IDENTITY_MAX_STEP_BUDGET
        ):
            if batch:
                digests.extend(result_identity_sha256_batch(
                    batch, mncs=mncs, cwd=cwd, environment=environment
                ))
                batch = []
            digests.append(sha256_bytes(payload))
            continue

        candidate = [*batch, payload]
        if batch and (
            len(candidate) > TEST_RESULT_IDENTITY_MAX_PAYLOADS
            or sum(len(item) for item in candidate) > TEST_RESULT_IDENTITY_MAX_BYTES
            or sum((len(item) + 1023) // 1024 for item in candidate)
            > TEST_RESULT_IDENTITY_MAX_CHUNKS
            or result_identity_step_budget(candidate) > TEST_RESULT_IDENTITY_MAX_STEP_BUDGET
        ):
            digests.extend(result_identity_sha256_batch(
                batch, mncs=mncs, cwd=cwd, environment=environment
            ))
            candidate = [payload]
        batch = candidate

    if batch:
        digests.extend(result_identity_sha256_batch(
            batch, mncs=mncs, cwd=cwd, environment=environment
        ))
    return digests


def _native_run_text(value: Any, *, nullable: bool = False) -> dict[str, Any] | None:
    """Project one ordinary JSON string into the bounded MNCS text record."""
    if value is None and nullable:
        return {"value": [], "length": 0, "is_null": True}
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > TEST_RUN_IDENTITY_MAX_TEXT_BYTES:
        return None
    return {"value": list(encoded), "length": len(encoded), "is_null": False}


def _native_run_identity_request(
    *,
    provenance: dict[str, Any],
    profile: Any,
    test_results: list[dict[str, Any]],
    test_inventory: dict[str, Any] | None,
    verification_plan: dict[str, Any] | None,
    obligation_plan: dict[str, Any] | None,
    execution: dict[str, Any] | None,
) -> tuple[dict[str, Any], int] | None:
    """Build typed app inputs; return None when compatibility handling is needed."""
    if not isinstance(provenance, dict) or not isinstance(test_results, list):
        return None
    compiler = provenance.get("compiler")
    manifest = provenance.get("manifest")
    source = provenance.get("source")
    if (
        not isinstance(compiler, dict)
        or set(compiler) != {"requested", "path", "sha256", "version"}
        or not isinstance(manifest, dict)
        or set(manifest) != {"path", "sha256"}
        or not isinstance(source, dict)
        or set(source) != {"path", "sha256"}
    ):
        return None

    compiler_wire = {
        "path": _native_run_text(compiler["path"]),
        "requested": _native_run_text(compiler["requested"]),
        "sha256": _native_run_text(compiler["sha256"], nullable=True),
        "version": _native_run_text(compiler["version"]),
    }
    manifest_wire = {
        "path": _native_run_text(manifest["path"]),
        "sha256": _native_run_text(manifest["sha256"]),
    }
    source_wire = {
        "path": _native_run_text(source["path"]),
        "sha256": _native_run_text(source["sha256"]),
    }
    inventory = test_inventory or {}
    verification = verification_plan or {}
    obligations = obligation_plan or {}
    execution_values = {
        key: (execution or {}).get(key)
        for key in ("mode", "artifact_identity", "artifact_sha256", "batch_size")
    }
    execution_wire = {
        "artifact_identity": _native_run_text(
            execution_values["artifact_identity"], nullable=True
        ),
        "artifact_sha256": _native_run_text(
            execution_values["artifact_sha256"], nullable=True
        ),
        "batch_size": 0,
        "batch_size_is_null": execution_values["batch_size"] is None,
        "mode": _native_run_text(execution_values["mode"], nullable=True),
    }
    batch_size = execution_values["batch_size"]
    if batch_size is not None:
        if type(batch_size) is not int or not 0 <= batch_size <= (1 << 64) - 1:
            return None
        execution_wire["batch_size"] = batch_size

    if len(test_results) > TEST_RUN_IDENTITY_MAX_TESTS:
        return None
    test_ids: list[dict[str, Any]] = []
    for item in test_results:
        if not isinstance(item, dict) or "id" not in item:
            return None
        projected = _native_run_text(item["id"])
        if projected is None:
            return None
        test_ids.append(projected)

    profile_wire = _native_run_text(profile)
    obligation_wire = _native_run_text(
        obligations.get("obligation_plan_id"), nullable=True
    )
    subject_identity_wire = _native_run_text(
        inventory.get("subject_identity"), nullable=True
    )
    subject_fingerprint_wire = _native_run_text(
        inventory.get("subject_fingerprint"), nullable=True
    )
    verification_wire = _native_run_text(
        verification.get("plan_id"), nullable=True
    )
    all_text = [
        *compiler_wire.values(),
        *manifest_wire.values(),
        *source_wire.values(),
        *execution_wire.values(),
        profile_wire,
        obligation_wire,
        subject_identity_wire,
        subject_fingerprint_wire,
        verification_wire,
        *test_ids,
    ]
    if (
        profile_wire is None
        or any(value is None for value in all_text)
    ):
        return None
    text_records = [value for value in all_text if isinstance(value, dict)]
    total_bytes = sum(value["length"] for value in text_records)
    if total_bytes > TEST_RUN_IDENTITY_MAX_BYTES:
        return None

    escaped_bytes_bound = sum(
        value["length"] * 6 + 2 for value in text_records if not value["is_null"]
    ) + 512 + len(test_ids) * 16
    blocks = (escaped_bytes_bound + 63) // 64 + 64
    step_budget = max(2_097_152, 100_000 + blocks * 32_768)
    if step_budget > TEST_RUN_IDENTITY_MAX_STEP_BUDGET:
        return None

    return (
        {
            "schema_version": TEST_RUN_IDENTITY_REQUEST_SCHEMA,
            "compiler": compiler_wire,
            "execution": execution_wire,
            "manifest": manifest_wire,
            "obligation_plan": obligation_wire,
            "profile": profile_wire,
            "source": source_wire,
            "subject_fingerprint": subject_fingerprint_wire,
            "subject_identity": subject_identity_wire,
            "test_count": len(test_ids),
            "tests": test_ids,
            "verification_plan": verification_wire,
        },
        step_budget,
    )


def _compatibility_run_identity_material(
    *,
    provenance: dict[str, Any],
    profile: Any,
    test_results: list[dict[str, Any]],
    test_inventory: dict[str, Any] | None,
    verification_plan: dict[str, Any] | None,
    obligation_plan: dict[str, Any] | None,
    execution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retain the previous JSON projection for unrepresentable values only."""
    return {
        "manifest": provenance["manifest"],
        "source": provenance["source"],
        "compiler": provenance["compiler"],
        "profile": profile,
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


def build_run_identity_sha256(
    *,
    provenance: dict[str, Any],
    profile: Any,
    test_results: list[dict[str, Any]],
    test_inventory: dict[str, Any] | None,
    verification_plan: dict[str, Any] | None,
    obligation_plan: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    """Build ordinary run identities in MNCS, preserving bounded host fallback."""
    prepared = _native_run_identity_request(
        provenance=provenance,
        profile=profile,
        test_results=test_results,
        test_inventory=test_inventory,
        verification_plan=verification_plan,
        obligation_plan=obligation_plan,
        execution=execution,
    )
    if prepared is not None:
        request, step_budget = prepared
        try:
            result = run_native_app(
                "test-run-identity.json",
                request,
                request_schema=TEST_RUN_IDENTITY_REQUEST_SCHEMA,
                result_schema=TEST_RUN_IDENTITY_RESULT_SCHEMA,
                mncs=mncs,
                cwd=cwd,
                environment=environment,
                step_budget=step_budget,
            )
        except NativeTransportError as error:
            raise AdapterError(f"native Test run identity failed: {error}") from error
        digest = result.get("digest")
        if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
            raise AdapterError("MNCS run-identity app returned a malformed SHA-256 digest")
        return digest

    material = _compatibility_run_identity_material(
        provenance=provenance,
        profile=profile,
        test_results=test_results,
        test_inventory=test_inventory,
        verification_plan=verification_plan,
        obligation_plan=obligation_plan,
        execution=execution,
    )
    return result_identity_sha256(
        compact_json(material).encode("utf-8"),
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )


def _native_lineage_identity_row(
    index: int,
    run_identity: Any,
    test_case_identity: Any,
    status: Any,
    verdict: Any,
) -> dict[str, Any] | None:
    """Project ordinary lineage fields into the bounded MNCS request shape."""
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or not isinstance(run_identity, str)
        or not isinstance(test_case_identity, str)
        or (status is not None and not isinstance(status, str))
        or (verdict is not None and not isinstance(verdict, str))
    ):
        return None

    run_bytes = run_identity.encode("utf-8")
    case_bytes = test_case_identity.encode("utf-8")
    status_bytes = status.encode("utf-8") if status is not None else b""
    verdict_bytes = verdict.encode("utf-8") if verdict is not None else b""
    if (
        len(run_bytes) > 256
        or len(case_bytes) > 512
        or len(status_bytes) > 64
        or len(verdict_bytes) > 32
    ):
        return None

    return {
        "run_identity": list(run_bytes),
        "run_identity_length": len(run_bytes),
        "index": index,
        "test_case_identity": list(case_bytes),
        "test_case_identity_length": len(case_bytes),
        "status": list(status_bytes),
        "status_length": len(status_bytes),
        "status_is_null": status is None,
        "verdict": list(verdict_bytes),
        "verdict_length": len(verdict_bytes),
        "verdict_is_null": verdict is None,
    }


def lineage_identity_step_budget(rows: list[dict[str, Any]]) -> int:
    """Conservatively bound streamed JSON and SHA work for native rows."""
    escaped_bytes = sum(
        6 * (
            row["run_identity_length"]
            + row["test_case_identity_length"]
            + row["status_length"]
            + row["verdict_length"]
        )
        + 192
        for row in rows
    )
    blocks = (escaped_bytes + 63) // 64 + len(rows) * 4 + 2
    return max(2_097_152, 100_000 + blocks * 32_768)


def lineage_identity_sha256_batch(
    rows: list[dict[str, Any]],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> list[tuple[str, str]]:
    """Hash one bounded set of typed per-test lineage rows in MNCS."""
    if not isinstance(rows, list) or len(rows) > TEST_LINEAGE_IDENTITY_MAX_ROWS:
        raise NativeInputBoundError(
            "lineage identity batch exceeds its 16-row native bound",
            reason="lineage_identity_row_count",
        )
    if any(not isinstance(row, dict) for row in rows):
        raise AdapterError("Test lineage identity rows must be objects")
    total_bytes = sum(
        row.get("run_identity_length", 0)
        + row.get("test_case_identity_length", 0)
        + row.get("status_length", 0)
        + row.get("verdict_length", 0)
        for row in rows
    )
    if total_bytes > TEST_LINEAGE_IDENTITY_MAX_BYTES:
        raise NativeInputBoundError(
            "lineage identity batch exceeds its 16,384-byte native bound",
            reason="lineage_identity_bytes",
        )

    step_budget = lineage_identity_step_budget(rows)
    if step_budget > TEST_LINEAGE_IDENTITY_MAX_STEP_BUDGET:
        raise NativeInputBoundError(
            "lineage identity batch exceeds the 8,000,000-step native bound",
            reason="lineage_identity_step_budget",
        )
    request = {
        "schema_version": TEST_LINEAGE_IDENTITY_REQUEST_SCHEMA,
        "row_count": len(rows),
        "rows": rows,
    }
    try:
        result = run_native_app(
            "test-lineage-identity.json",
            request,
            request_schema=TEST_LINEAGE_IDENTITY_REQUEST_SCHEMA,
            result_schema=TEST_LINEAGE_IDENTITY_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            step_budget=step_budget,
        )
    except NativeTransportError as error:
        raise AdapterError(f"native Test lineage identity failed: {error}") from error

    digests = result.get("digests")
    if (
        result.get("row_count") != len(rows)
        or not isinstance(digests, list)
        or len(digests) != len(rows)
    ):
        raise AdapterError("MNCS lineage-identity app returned malformed digest rows")
    output: list[tuple[str, str]] = []
    for item in digests:
        if not isinstance(item, dict):
            raise AdapterError("MNCS lineage-identity app returned a malformed row")
        row_digests: list[str] = []
        for field in ("execution", "observation"):
            digest_record = item.get(field)
            digest = digest_record.get("digest") if isinstance(digest_record, dict) else None
            if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
                raise AdapterError("MNCS lineage-identity app returned a malformed digest")
            row_digests.append(digest)
        output.append((row_digests[0], row_digests[1]))
    return output


def lineage_identity_sha256_many(
    rows: list[tuple[int, str, str, Any, Any]],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> list[tuple[str, str]]:
    """Hash ordinary per-test identities in MNCS with bounded compatibility fallbacks.

    The application constructs and hashes the canonical execution and
    observation JSON for ordinary string/null fields. Values outside its
    fixed field bounds or vocabulary keep the previous compact-JSON path.
    """
    if not isinstance(rows, list):
        raise AdapterError("Test lineage identity inputs must be a list")
    results: list[tuple[str, str] | None] = [None] * len(rows)
    native_batches: list[list[tuple[int, dict[str, Any]]]] = []
    pending: list[tuple[int, dict[str, Any]]] = []
    fallback: list[tuple[int, int, str, str, Any, Any]] = []

    def batch_too_large(candidate: list[tuple[int, dict[str, Any]]]) -> bool:
        wire_rows = [wire for _, wire in candidate]
        return (
            len(wire_rows) > TEST_LINEAGE_IDENTITY_MAX_ROWS
            or sum(
                wire["run_identity_length"]
                + wire["test_case_identity_length"]
                + wire["status_length"]
                + wire["verdict_length"]
                for wire in wire_rows
            ) > TEST_LINEAGE_IDENTITY_MAX_BYTES
            or lineage_identity_step_budget(wire_rows)
            > TEST_LINEAGE_IDENTITY_MAX_STEP_BUDGET
        )

    for position, (index, run_identity, case_identity, status, verdict) in enumerate(rows):
        wire = _native_lineage_identity_row(
            index, run_identity, case_identity, status, verdict
        )
        if wire is None:
            fallback.append((position, index, run_identity, case_identity, status, verdict))
            continue
        candidate = [*pending, (position, wire)]
        if pending and batch_too_large(candidate):
            native_batches.append(pending)
            pending = []
            candidate = [(position, wire)]
        if batch_too_large(candidate):
            fallback.append((position, index, run_identity, case_identity, status, verdict))
        else:
            pending = candidate
    if pending:
        native_batches.append(pending)

    for batch in native_batches:
        digests = lineage_identity_sha256_batch(
            [wire for _, wire in batch],
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
        for (position, _), digest_pair in zip(batch, digests):
            results[position] = digest_pair

    fallback_payloads: list[bytes] = []
    for _, index, run_identity, case_identity, _, _ in fallback:
        execution = {
            "index": index,
            "run": run_identity,
            "test_case": case_identity,
        }
        fallback_payloads.append(compact_json(execution).encode("utf-8"))

    if fallback_payloads:
        # The observation payload depends on the execution digest. Hash the
        # execution rows first, then construct and hash the exact old JSON.
        execution_digests = result_identity_sha256_many(
            fallback_payloads, mncs=mncs, cwd=cwd, environment=environment
        )
        observation_payloads: list[bytes] = []
        for fallback_row, execution_digest in zip(fallback, execution_digests):
            _, _, _, _, status, verdict = fallback_row
            observation_payloads.append(compact_json({
                "execution": execution_digest,
                "status": status,
                "verdict": verdict,
            }).encode("utf-8"))
        observation_digests = result_identity_sha256_many(
            observation_payloads, mncs=mncs, cwd=cwd, environment=environment
        )
        for fallback_row, execution_digest, observation_digest in zip(
            fallback, execution_digests, observation_digests
        ):
            position = fallback_row[0]
            results[position] = (execution_digest, observation_digest)

    if any(result is None for result in results):
        raise AdapterError("MNCS lineage-identity batching left an unbound row")
    return [result for result in results if result is not None]


def result_identity_step_budget(payloads: list[bytes]) -> int:
    blocks = sum((len(payload) + 63) // 64 + 2 for payload in payloads)
    return max(2_097_152, 100_000 + blocks * 32_768)


def external_verdict(variant: str) -> str:
    """Adapt the native enum to the public TestResult verdict vocabulary."""

    return "UNKNOWN" if variant == "UNSUPPORTED" else variant


def resolve_mncs(binary: str, cwd: Path) -> str:
    candidate = Path(binary)
    if candidate.is_absolute() or "/" in binary:
        return str((cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    return shutil.which(binary) or binary


def resolve_run_mncs(binary: str | None, execution: str, cwd: Path) -> str:
    """Resolve the selected Stage-0 reference without PATH fallback in VM mode."""
    if not binary:
        if execution == "canonical-vm":
            raise ManifestError(
                "canonical-vm requires the selected Stage-0 reference executable "
                "through MNCS or --mncs"
            )
        binary = "mncs"
    return resolve_mncs(binary, cwd)


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
        if len(diagnostic_codes) > MAX_DIAGNOSTIC_EXPECTATIONS:
            raise ManifestError(
                f"tests[{index}].diagnostic_codes may contain at most "
                f"{MAX_DIAGNOSTIC_EXPECTATIONS} prefixes"
            )
        for code in diagnostic_codes:
            try:
                code_bytes = code.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ManifestError(
                    f"tests[{index}].diagnostic_codes must contain valid UTF-8 prefixes"
                ) from error
            if len(code_bytes) > MAX_DIAGNOSTIC_CODE_BYTES:
                raise ManifestError(
                    f"tests[{index}].diagnostic_codes prefixes must be at most "
                    f"{MAX_DIAGNOSTIC_CODE_BYTES} UTF-8 bytes"
                )
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
                "signature_identity": callable_.get("signature_identity"),
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
        required = (
            "test_case_identity",
            "declaration_identity",
            "function_identity",
            "signature_identity",
            "name",
        )
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
                "signature_identity": entry["signature_identity"],
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
    def __init__(
        self,
        root: Path,
        *,
        mncs: str | None = None,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ):
        native_context = (mncs is not None, cwd is not None, environment is not None)
        if any(native_context) and not all(native_context):
            raise AdapterError(
                "native artifact hashing requires MNCS, cwd, and environment together"
            )
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: list[dict[str, str]] = []
        self.mncs = mncs
        self.cwd = cwd.resolve() if cwd is not None else None
        self.environment = environment
        self._native_hash_operations = 0
        self._identity_hasher_attempted = False
        self._identity_hasher: ResultIdentityEmbedHasher | None = None
        self._identity_hasher_failure: str | None = None
        self._closed = False

    def close(self) -> None:
        """Release a retained MNCS hash session owned by this artifact store."""
        if self._identity_hasher is not None:
            self._identity_hasher.close()
        self._closed = True

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _start_identity_hasher(self) -> ResultIdentityEmbedHasher | None:
        if self._identity_hasher_attempted:
            return self._identity_hasher
        self._identity_hasher_attempted = True
        if self.mncs is None or self.cwd is None or self.environment is None:
            return None
        try:
            self._identity_hasher = ResultIdentityEmbedHasher(
                mncs=self.mncs,
                cwd=self.cwd,
                environment=self.environment,
            )
        except AdapterError as error:
            # The retained path is an optimization. Keep the established
            # native-app transport available if this toolchain cannot admit
            # the compiled backend artifact.
            self._identity_hasher_failure = str(error)
        return self._identity_hasher

    def _digest_bounded_payload(self, payload: bytes) -> str:
        if self._closed:
            raise AdapterError("artifact store is closed")
        if self._native_hash_operations >= TEST_RESULT_IDENTITY_EMBED_SESSION_AFTER_ARTIFACTS:
            hasher = self._start_identity_hasher()
            if hasher is not None:
                try:
                    return hasher.digest(payload)
                except AdapterError as error:
                    self._identity_hasher_failure = str(error)
                    hasher.close()
                    self._identity_hasher = None

        self._native_hash_operations += 1
        if self.mncs is None or self.cwd is None or self.environment is None:
            raise AdapterError("native artifact hashing context is incomplete")
        if (
            len(payload) <= TEST_RESULT_IDENTITY_MAX_BYTES
            and result_identity_step_budget([payload])
            <= TEST_RESULT_IDENTITY_MAX_STEP_BUDGET
        ):
            return result_identity_sha256(
                payload,
                mncs=self.mncs,
                cwd=self.cwd,
                environment=self.environment,
            )
        return _native_result_identity_stream_digest(
            [payload],
            mncs=self.mncs,
            cwd=self.cwd,
            environment=self.environment,
        )

    def _retained_identity_hasher(self) -> ResultIdentityEmbedHasher | None:
        if self._native_hash_operations < TEST_RESULT_IDENTITY_EMBED_SESSION_AFTER_ARTIFACTS:
            return None
        return self._start_identity_hasher()

    def _digest_retained_payload(
        self,
        payload: bytes,
        *,
        fallback: Callable[[], str],
    ) -> str:
        hasher = self._retained_identity_hasher()
        if hasher is None:
            return fallback()
        try:
            return hasher.digest(payload)
        except AdapterError as error:
            self._identity_hasher_failure = str(error)
            hasher.close()
            self._identity_hasher = None
            return fallback()

    def digest_payload(self, payload: bytes) -> str:
        """Use MNCS sessions for bounded artifacts after repeated app invocations."""
        if self._closed:
            raise AdapterError("artifact store is closed")
        if self.mncs is None:
            return sha256_bytes(payload)
        if self.cwd is None or self.environment is None:
            raise AdapterError("native artifact hashing context is incomplete")
        if len(payload) > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
            return sha256_bytes(payload)
        if len(payload) > TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES:
            return self._digest_retained_payload(
                payload,
                fallback=lambda: sha256_bytes(payload),
            )
        return self._digest_bounded_payload(payload)

    def digest_file(self, path: Path) -> str:
        """Use MNCS for bounded files and the host path for larger files."""
        if self._closed:
            raise AdapterError("artifact store is closed")
        if self.mncs is None:
            return sha256_file(path)
        if self.cwd is None or self.environment is None:
            raise AdapterError("native artifact hashing context is incomplete")
        file_size = path.stat().st_size
        if file_size > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
            return sha256_file(path)
        if file_size > TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES:
            with path.open("rb") as handle:
                payload = handle.read(TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES + 1)
            if len(payload) > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
                return sha256_file(path)
            return self._digest_retained_payload(
                payload,
                fallback=lambda: sha256_file(path),
            )
        with path.open("rb") as handle:
            payload = handle.read(TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES + 1)
            if len(payload) > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
                return sha256_file(path)
        if len(payload) > TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES:
            return self._digest_retained_payload(
                payload,
                fallback=lambda: sha256_file(path),
            )
        return self._digest_bounded_payload(payload)

    def digest_file_with_method(self, path: Path) -> tuple[str, dict[str, str]]:
        """Hash one file and report the native adapter or explicit host fallback."""
        if self._closed:
            raise AdapterError("artifact store is closed")
        if self.mncs is None:
            return sha256_file(path), {
                "implementation": "host",
                "fallback_reason": "native_context_unavailable",
            }
        if self.cwd is None or self.environment is None:
            raise AdapterError("native artifact hashing context is incomplete")
        file_size = path.stat().st_size
        if file_size > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
            return sha256_file(path), {
                "implementation": "host",
                "fallback_reason": "artifact_file_over_native_bound",
            }

        operations_before = self._native_hash_operations
        try:
            digest = self.digest_file(path)
        except (AdapterError, NativeTransportError, NativeBudgetExhaustedError) as error:
            return sha256_file(path), {
                "implementation": "host",
                "fallback_reason": "native_hash_failure",
                "fallback_detail": str(error)[:512],
            }

        if self._identity_hasher is not None:
            return digest, {"implementation": "mncs-embed"}
        if self._native_hash_operations > operations_before:
            return digest, {"implementation": "mncs-application"}

        if file_size > TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES:
            if operations_before < TEST_RESULT_IDENTITY_EMBED_SESSION_AFTER_ARTIFACTS:
                reason = "native_embed_session_not_warm"
            else:
                reason = "native_embed_session_unavailable"
            detail = self._identity_hasher_failure
        else:
            reason = "native_hash_not_used"
            detail = self._identity_hasher_failure
        result: dict[str, str] = {
            "implementation": "host",
            "fallback_reason": reason,
        }
        if detail:
            result["fallback_detail"] = detail[:512]
        return digest, result

    def write(self, relative: str, payload: bytes, kind: str) -> str:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise AdapterError(f"unsafe artifact path: {relative}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = self.digest_payload(payload)
        item = {"path": path.relative_to(self.root).as_posix(), "kind": kind, "sha256": digest}
        self.items.append(item)
        return item["path"]

    def record_existing(self, path: Path, kind: str) -> str:
        """Record a compiler-created file without changing its bytes."""

        try:
            relative = path.resolve().relative_to(self.root).as_posix()
            digest = self.digest_file(path)
        except (OSError, ValueError) as error:
            raise AdapterError(f"unable to record compiler artifact {path}: {error}") from error
        item = {"path": relative, "kind": kind, "sha256": digest}
        self.items.append(item)
        return relative


def command_environment(library_paths: list[Path], inherited: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if inherited is None else inherited)
    if library_paths:
        # The manifest owns private roots (such as mncs-test/native); the
        # selected Environment composition may append an exact standard
        # library root through the provider contract. Preserve that declared
        # closure after private roots and deduplicate without reordering.
        roots = [str(path) for path in library_paths]
        roots.extend(root for root in environment.get("MNCS_LIBRARY_PATH", "").split(os.pathsep)
                     if root)
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(roots))
    return environment


def native_application_environment(cwd: Path) -> dict[str, str]:
    """Resolve the sibling native libraries for direct application consumers."""
    environment = dict(os.environ)
    if environment.get("MNCS_LIBRARY_PATH"):
        return environment
    workspace_root = next(
        (path for path in (cwd, *cwd.parents) if (path / "mncs-language").is_dir()),
        cwd.parent,
    )
    explicit_stdlib = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit_stdlib) / "library" if explicit_stdlib else workspace_root / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = workspace_root / "mncs-language/library"
    roots = [path for path in (stdlib, cwd / "native") if path.is_dir()]
    if roots:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(str(path.resolve()) for path in roots)
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
        self.library.mncs_session_callable_bindings.argtypes = [ctypes.c_void_p]
        self.library.mncs_session_callable_bindings.restype = ctypes.c_void_p
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

    def callable_bindings(self) -> dict[str, Any]:
        """Return compiler-issued bindings from this exact admitted artifact."""
        started = time.perf_counter()
        value = self._response(self.library.mncs_session_callable_bindings(self.handle))
        self.timings.append({
            "phase": "compiler_callable_bindings",
            "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        if not isinstance(value, dict) or not isinstance(value.get("callable_bindings"), list):
            raise AdapterError("mncs-embed callable metadata is not a binding document")
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


def _host_integer(value: int) -> dict[str, Any]:
    return {"integer": {"value": int(value)}}


def _host_byte(value: int) -> dict[str, Any]:
    return {"byte": {"value": int(value)}}


def _host_boolean(value: bool) -> dict[str, Any]:
    return {"boolean": {"value": bool(value)}}


def _host_sequence(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sequence": {"values": values}}


def _host_record(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"fields": fields}
    if name:
        record["type"] = name
    return {"record": record}


def _host_value_from_execution(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) != 1:
        raise AdapterError("mncs-embed returned a malformed execution value")
    kind, body = next(iter(value.items()))
    if not isinstance(body, dict):
        raise AdapterError("mncs-embed returned a malformed tagged execution value")
    if kind == "record":
        raw_fields = body.get("fields")
        if not isinstance(raw_fields, list):
            raise AdapterError("mncs-embed returned a malformed record value")
        fields: dict[str, Any] = {}
        for pair in raw_fields:
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise AdapterError("mncs-embed returned a malformed record field")
            fields[pair[0]] = _host_value_from_execution(pair[1])
        name = body.get("name", "")
        if not isinstance(name, str):
            raise AdapterError("mncs-embed returned a malformed record name")
        return _host_record(name, fields)
    if kind == "sequence":
        values = body.get("values")
        if not isinstance(values, list):
            raise AdapterError("mncs-embed returned a malformed sequence value")
        return _host_sequence([_host_value_from_execution(item) for item in values])
    if kind in ("integer", "byte", "boolean", "float") and "value" in body:
        return {kind: {"value": body["value"]}}
    raise AdapterError(f"mncs-embed returned an unsupported execution value: {kind}")


class ResultIdentityEmbedHasher:
    """Reuse one admitted MNCS backend session for bounded ArtifactStore hashes."""

    def __init__(
        self,
        *,
        mncs: str,
        cwd: Path,
        environment: dict[str, str],
    ):
        self.mncs = mncs
        self.cwd = cwd
        self.environment = environment
        self.session: EmbedSession | None = None
        self._closed = False

        libraries = [
            candidate
            for candidate in embed_library_candidates(mncs, None)
            if candidate.is_file()
        ]
        if not libraries:
            raise AdapterError("mncs-embed library is unavailable for a retained hash session")

        source_path = Path(__file__).resolve().parents[1] / "native/mncs/test/result_identity_v1.mncs"
        if not source_path.is_file():
            raise AdapterError(f"MNCS result identity source is unavailable: {source_path}")
        compile_command = [
            mncs,
            "compile",
            str(source_path),
            "--emit",
            "backend",
            "--target",
            "research-bytecode",
            "--include-tests",
            "--output-dir",
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="mncs-test-identity-backend-") as directory:
                compile_result = subprocess.run(
                    [*compile_command, directory],
                    cwd=str(cwd),
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                if compile_result.returncode != 0:
                    detail = (compile_result.stderr or compile_result.stdout).strip()
                    raise AdapterError(
                        "MNCS result identity backend compilation failed: "
                        + (detail[-1200:] if detail else f"exit {compile_result.returncode}")
                    )
                artifact_path = Path(directory) / "backend.json"
                try:
                    artifact_bytes = artifact_path.read_bytes()
                except OSError as error:
                    raise AdapterError(
                        f"MNCS result identity backend artifact is unavailable: {error}"
                    ) from error
                last_error: str | None = None
                for library in libraries:
                    try:
                        self.session = EmbedSession(library, artifact_bytes)
                        info = self.session.info()
                        if not isinstance(info.get("artifact_identity"), str):
                            raise AdapterError("mncs-embed omitted the compiled artifact identity")
                        return
                    except AdapterError as error:
                        if self.session is not None:
                            self.session.close()
                            self.session = None
                        last_error = str(error)
                raise AdapterError(
                    "mncs-embed could not admit the result identity backend: "
                    + (last_error or "no compatible library candidate")
                )
        except subprocess.TimeoutExpired as error:
            raise AdapterError("MNCS result identity backend compilation timed out") from error
        except OSError as error:
            raise AdapterError(f"MNCS result identity backend compilation failed: {error}") from error

    def _returned(self, output: dict[str, Any], label: str) -> Any:
        if output.get("status") != "returned":
            detail = json.dumps(output, ensure_ascii=False, separators=(",", ":"))[:800]
            raise AdapterError(f"MNCS {label} call failed: {detail}")
        values = output.get("returned")
        if not isinstance(values, list) or len(values) != 1:
            raise AdapterError(f"MNCS {label} call returned a malformed value list")
        return values[0]

    def _require_valid(self, output: dict[str, Any], label: str) -> None:
        value = self._returned(output, label)
        if not isinstance(value, dict) or value.get("boolean", {}).get("value") is not True:
            raise AdapterError(f"MNCS {label} rejected the bounded hash request")

    def _digest_bytes(self, value: Any, label: str) -> str:
        if not isinstance(value, dict):
            raise AdapterError(f"MNCS {label} returned no digest record")
        record = value.get("record")
        raw_fields = record.get("fields") if isinstance(record, dict) else None
        if not isinstance(raw_fields, list):
            raise AdapterError(f"MNCS {label} returned a malformed digest record")
        fields = {
            pair[0]: pair[1]
            for pair in raw_fields
            if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)
        }
        return self._digest_value(fields.get("digest"), label)

    def _digest_value(self, digest_value: Any, label: str) -> str:
        sequence = digest_value.get("sequence") if isinstance(digest_value, dict) else None
        raw_bytes = sequence.get("values") if isinstance(sequence, dict) else None
        if not isinstance(raw_bytes, list) or len(raw_bytes) != 32:
            raise AdapterError(f"MNCS {label} returned a malformed SHA-256 digest")
        digest: list[int] = []
        for item in raw_bytes:
            byte_value = item.get("byte") if isinstance(item, dict) else None
            byte = byte_value.get("value") if isinstance(byte_value, dict) else None
            if type(byte) is not int or byte < 0 or byte > 255:
                raise AdapterError(f"MNCS {label} returned a malformed digest byte")
            digest.append(byte)
        return bytes(digest).hex()

    def _request_value(self, payload: bytes) -> dict[str, Any]:
        chunks = [
            _host_sequence([_host_byte(byte) for byte in payload[offset : offset + 1024]])
            for offset in range(0, len(payload), 1024)
        ]
        return _host_record(
            "ResultIdentityRequestV1",
            {
                "schema_version": _host_sequence(
                    [_host_byte(byte) for byte in TEST_RESULT_IDENTITY_REQUEST_SCHEMA.encode()]
                ),
                "payload_count": _host_integer(1),
                "chunk_count": _host_integer(len(chunks)),
                "payloads": _host_sequence(
                    [
                        _host_record(
                            "IdentityPayloadSliceV1",
                            {
                                "first_chunk": _host_integer(0),
                                "chunk_count": _host_integer(len(chunks)),
                            },
                        )
                    ]
                ),
                "chunks": _host_sequence(chunks),
            },
        )

    def _hash_batch(self, payload: bytes) -> str:
        if self.session is None:
            raise AdapterError("MNCS result identity session is closed")
        budget = result_identity_step_budget([payload])
        request = self._request_value(payload)
        outputs = self.session.call_batch(
            [
                {
                    "module": "mncs.test.result_identity_v1",
                    "function": "valid_request",
                    "typed_args": [request],
                    "step_budget": budget,
                },
                {
                    "module": "mncs.test.result_identity_v1",
                    "function": "hash_payloads",
                    "typed_args": [request],
                    "step_budget": budget,
                },
            ]
        )
        if len(outputs) != 2:
            raise AdapterError("MNCS result identity batch returned the wrong result count")
        valid, hashed = outputs
        self._require_valid(valid, "result identity validation")
        hashes = self._returned(hashed, "result identity")
        if not isinstance(hashes, dict):
            raise AdapterError("MNCS result identity returned no payload list")
        records = hashes.get("sequence", {}).get("values")
        if not isinstance(records, list) or len(records) != 1:
            raise AdapterError("MNCS result identity returned the wrong payload count")
        return self._digest_bytes(records[0], "result identity")

    def _empty_stream_state(self) -> dict[str, Any]:
        return _host_record(
            "ResultIdentityHashStateV1",
            {
                "h": _host_sequence([_host_integer(0) for _ in range(8)]),
                "total": _host_integer(0),
                "buf": _host_sequence([_host_byte(0) for _ in range(64)]),
                "buffered": _host_integer(0),
            },
        )

    def _state_fields(self, state: dict[str, Any]) -> dict[str, Any]:
        record = state.get("record")
        fields = record.get("fields") if isinstance(record, dict) else None
        if not isinstance(fields, dict):
            raise AdapterError("MNCS stream hash returned a malformed continuation state")
        return fields

    def _update_stream_window(
        self,
        state: dict[str, Any],
        window: bytes,
        *,
        total_before: int,
        finalize: bool = False,
    ) -> tuple[dict[str, Any], str | None]:
        if self.session is None:
            raise AdapterError("MNCS result identity session is closed")
        has_state = total_before > 0
        request_state = (
            _host_record("ResultIdentityHashStateV1", self._state_fields(state))
            if has_state
            else self._empty_stream_state()
        )
        chunks = [
            _host_sequence([_host_byte(byte) for byte in window[offset : offset + 1024]])
            for offset in range(0, len(window), 1024)
        ]
        request = _host_record(
            "ResultIdentityStreamRequestV1",
            {
                "schema_version": _host_sequence(
                    [
                        _host_byte(byte)
                        for byte in TEST_RESULT_IDENTITY_STREAM_REQUEST_SCHEMA.encode()
                    ]
                ),
                "has_state": _host_boolean(has_state),
                "state": request_state,
                "chunks": _host_sequence(chunks),
                "finalize": _host_boolean(finalize),
            },
        )
        budget = result_identity_step_budget([window])
        if budget > TEST_RESULT_IDENTITY_MAX_STEP_BUDGET:
            raise AdapterError("MNCS result identity stream window exceeds its step budget")
        outputs = self.session.call_batch(
            [
                {
                    "module": "mncs.test.result_identity_v1",
                    "function": "update_stream_state",
                    "typed_args": [request],
                    "step_budget": budget,
                },
            ]
        )
        if len(outputs) != 1:
            raise AdapterError("MNCS result identity stream returned the wrong result count")
        update = _host_value_from_execution(
            self._returned(outputs[0], "result identity stream")
        )
        if not isinstance(update, dict):
            raise AdapterError("MNCS result identity stream returned no update record")
        record = update.get("record")
        update_fields = record.get("fields") if isinstance(record, dict) else None
        accepted = update_fields.get("accepted") if isinstance(update_fields, dict) else None
        accepted_boolean = accepted.get("boolean") if isinstance(accepted, dict) else None
        if not isinstance(accepted_boolean, dict) or accepted_boolean.get("value") is not True:
            raise AdapterError("MNCS result identity stream rejected the bounded hash request")
        finalized = update_fields.get("finalized") if isinstance(update_fields, dict) else None
        finalized_boolean = finalized.get("boolean") if isinstance(finalized, dict) else None
        if (
            not isinstance(finalized_boolean, dict)
            or finalized_boolean.get("value") is not finalize
        ):
            raise AdapterError("MNCS result identity stream returned the wrong finalization status")
        state = update_fields.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("record"), dict):
            raise AdapterError("MNCS result identity stream returned a malformed state record")
        total = total_before + len(window)
        fields = self._state_fields(state)
        total_value = fields.get("total", {}).get("integer", {}).get("value")
        buffered_value = fields.get("buffered", {}).get("integer", {}).get("value")
        if total_value != total or buffered_value != total % 64:
            raise AdapterError("MNCS result identity stream returned inconsistent SHA-256 state")
        digest = (
            self._digest_value(update_fields.get("digest"), "result identity stream finalization")
            if finalize else None
        )
        return state, digest

    def digest_chunks(self, source_chunks: Iterable[bytes]) -> str:
        """Hash a byte stream through MNCS with bounded, reusable session calls."""
        if self.session is None or self._closed:
            raise AdapterError("MNCS result identity session is closed")
        state = self._empty_stream_state()
        total = 0
        pending = bytearray()
        for source_chunk in source_chunks:
            if not isinstance(source_chunk, bytes):
                raise AdapterError("MNCS result identity stream requires byte chunks")
            if total + len(pending) + len(source_chunk) > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
                raise AdapterError("MNCS retained hash session input exceeds its bounded byte limit")
            offset = 0
            while offset < len(source_chunk):
                if len(pending) == TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES:
                    state, _digest = self._update_stream_window(
                        state, bytes(pending), total_before=total
                    )
                    total += len(pending)
                    pending.clear()
                take = min(
                    TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES - len(pending),
                    len(source_chunk) - offset,
                )
                pending.extend(source_chunk[offset : offset + take])
                offset += take
        _state, digest = self._update_stream_window(
            state, bytes(pending), total_before=total, finalize=True
        )
        if digest is None:
            raise AdapterError("MNCS result identity stream omitted its finalized digest")
        return digest

    def _hash_stream(self, payload: bytes) -> str:
        return self.digest_chunks(
            payload[offset : offset + TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES]
            for offset in range(0, len(payload), TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES)
        )

    def digest(self, payload: bytes) -> str:
        if self._closed:
            raise AdapterError("MNCS result identity session is closed")
        if len(payload) > TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES:
            raise AdapterError("payload exceeds the retained MNCS hash-session bound")
        if (
            len(payload) <= TEST_RESULT_IDENTITY_MAX_BYTES
            and result_identity_step_budget([payload]) <= TEST_RESULT_IDENTITY_MAX_STEP_BUDGET
        ):
            return self._hash_batch(payload)
        return self._hash_stream(payload)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        self._closed = True


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
        detail["compiler_callable_bindings"] = session.callable_bindings()
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
    callable_reference: dict[str, Any] | None = None,
    type_arguments: Any = None,
    include_type_arguments: bool = False,
    host_grants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build only the request shape accepted by the retained embed ABI."""

    request = (
        {"callable_reference": callable_reference, "args": arguments}
        if callable_reference is not None
        else {"module": module, "function": function, "args": arguments}
    )
    request["step_budget"] = step_budget
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
            "signature_identity",
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


def execution_adjudication_case(
    *,
    test: dict[str, Any],
    decoded: dict[str, Any],
    suite: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Project one decoded output into the bounded native policy request."""
    status = decoded.get("status")
    if isinstance(status, str):
        try:
            status_size = len(status.encode("utf-8"))
        except UnicodeEncodeError:
            status_size = 65
    else:
        status_size = 65
    # Any non-text or oversize status is deliberately mapped to an unknown
    # native token. The original value remains in TestResult for diagnostics.
    native_status = (
        status if isinstance(status, str) and status_size <= 64
        else "<invalid-status>"
    )
    native: dict[str, Any] | None = None
    if status == "returned":
        native = native_suite_summary(decoded) if suite else native_test_result(decoded)
    expected_status = expected_status_for(test) or ""
    return {
        "execution_status": native_status,
        "expected_status": expected_status,
        "suite": suite,
        "native_result_present": native is not None,
        "native_verdict": native.get("verdict", "") if native is not None else "",
        "native_failure_class": (
            native.get("failure_kind", "") if native is not None and not suite else ""
        ),
    }, native


def apply_execution_adjudication(
    *,
    test_result: dict[str, Any],
    decoded: dict[str, Any],
    native: dict[str, Any] | None,
    decision: dict[str, Any],
    step_budget: int,
    expected_status: str | None,
    failure_reason_fallback: bool,
) -> dict[str, Any]:
    """Materialize the native policy decision in the public result envelope."""
    classification = decision["decision"]
    status = decoded.get("status")
    test_result["status"] = decision["status"]
    test_result["verdict"] = decision["verdict"]
    if native is not None:
        test_result["native_result"] = native

    if classification in {"suite_result", "success", "skip"}:
        return test_result
    if classification == "expected_failure":
        test_result["expected_failure"] = True
        test_result["failure"] = {
            "class": decision["failure_class"],
            "message": f"expected MNCS execution status {status}",
            "expected": True,
        }
        return test_result
    if classification == "unknown_status":
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="MNCS emitted an unknown execution status",
            observed_status=status,
        )
    if classification == "malformed_result":
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="returned value is not the declared native test result shape",
        )
    if classification == "unsupported_returned":
        test_result["failure"] = {
            "class": decision["failure_class"],
            "message": "native test reported an unsupported capability",
            "code": (native or {}).get("failure_code"),
        }
        return test_result
    if classification == "unsupported_execution":
        test_result["failure"] = {
            "class": decision["failure_class"],
            "message": "MNCS reported an unsupported capability",
        }
        return test_result
    if classification == "assertion_failure":
        observed = native or {}
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="native assertion failed",
            expected=observed.get("expected"),
            actual=observed.get("actual"),
            assertion_code=observed.get("assertion_code"),
            native_result=observed,
        )
    if classification == "expectation_mismatch":
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message=f"expected execution status {expected_status}, got {status}",
            expected_status=expected_status,
            observed_status=status,
        )
    if classification == "timeout":
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="MNCS exhausted the declared step budget",
            step_budget=step_budget,
        )
    if classification == "runtime_failure":
        failure = decoded.get("failure")
        if failure_reason_fallback and not failure:
            failure = decoded.get("failure_reason")
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="MNCS test entrypoint terminated with a runtime failure",
            execution_failure=failure,
        )
    if classification == "invalid_request":
        failure = decoded.get("failure")
        if failure_reason_fallback and not failure:
            failure = decoded.get("failure_reason")
        return result_failure(
            test_result,
            failure_class=decision["failure_class"],
            message="MNCS rejected the execution request",
            execution_failure=failure,
        )
    raise AdapterError("MNCS execution adjudication returned an unknown decision")


def execution_result_from_decoded(
    *,
    test: dict[str, Any],
    source_path: Path,
    source_text: str,
    decoded: Any,
    step_budget: int,
    adjudication: dict[str, Any] | None = None,
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
            "vm_record",
            "execution_provenance",
        )
        if key in decoded
    }
    if test.get("first_class"):
        invoked = decoded.get("invoked_callable")
        expected_reference = {
            "artifact_identity": decoded.get("artifact_identity"),
            "callable_identity": test.get("function_identity"),
            "declaration_identity": test.get("declaration_identity"),
            "test_case_identity": test.get("test_case_identity"),
            "signature_identity": test.get("signature_identity"),
        }
        if not isinstance(invoked, dict) or any(
            invoked.get(field) != expected
            for field, expected in expected_reference.items()
        ):
            return result_failure(
                test_result,
                failure_class="identity_dispatch_failure",
                message="first-class test result lacks a matching runtime callable reference receipt",
                expected_callable_reference=expected_reference,
                observed_callable_reference=invoked,
            )
        test_result["callable_invocation"] = {
            **expected_reference,
            "execution_status": decoded.get("status"),
        }
    _native_case, native = execution_adjudication_case(
        test=test,
        decoded=decoded,
        suite=suite,
    )
    if not isinstance(adjudication, dict):
        raise AdapterError("execution result is missing its native adjudication")
    return apply_execution_adjudication(
        test_result=test_result,
        decoded=decoded,
        native=native,
        decision=adjudication,
        step_budget=step_budget,
        expected_status=expected_status_for(test),
        failure_reason_fallback=True,
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
        "sha256": artifacts.digest_file(request_path),
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
    decoded = parse_json_output(process["stdout"])
    diagnostics = diagnostic_list(decoded)
    case, native = execution_adjudication_case(
        test=test,
        decoded=decoded if isinstance(decoded, dict) else {},
        suite=suite,
    )
    try:
        process_decision = run_process_transport_adjudication(
            {
                "process_start_failed": "transport_error" in process,
                "timed_out": process.get("timed_out") is True,
                "structured_result_present": isinstance(decoded, dict),
                "nonzero_exit": (
                    type(process.get("returncode")) is int
                    and process["returncode"] != 0
                ),
                "diagnostics_present": bool(diagnostics),
            },
            case,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
    except (NativeTransportError, OSError, subprocess.TimeoutExpired) as error:
        # Start failures and deadlines are already observed at the host
        # process boundary. Preserve those outcomes if the classifier cannot
        # run, for example because the MNCS executable itself is unavailable.
        if "transport_error" in process:
            return result_failure(
                test_result,
                failure_class="infrastructure_failure",
                message="MNCS process could not be started",
                error=process["transport_error"],
            )
        if process.get("timed_out") is True:
            return result_failure(
                test_result,
                failure_class="timeout",
                message=f"MNCS execution exceeded {timeout_seconds}s",
                timeout_seconds=timeout_seconds,
            )
        raise AdapterError(f"native process transport adjudication failed: {error}") from error

    decision = process_decision["decision"]
    if decision == "start_failure":
        return result_failure(
            test_result,
            failure_class=str(process_decision["failure_class"]),
            message="MNCS process could not be started",
            error=process["transport_error"],
        )
    if decision == "timeout":
        return result_failure(
            test_result,
            failure_class=str(process_decision["failure_class"]),
            message=f"MNCS execution exceeded {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
        )
    test_result["process_exit_code"] = process["returncode"]
    if decision == "compile_failure":
        return result_failure(
            test_result,
            failure_class=str(process_decision["failure_class"]),
            message="MNCS rejected the source before execution",
            diagnostics=diagnostics,
        )
    if decision == "malformed_result":
        return result_failure(
            test_result,
            failure_class=str(process_decision["failure_class"]),
            message="MNCS did not emit a structured execution result",
            diagnostics=diagnostics,
        )
    if decision != "continue" or not isinstance(decoded, dict):
        raise AdapterError("native process transport adjudication returned an inconsistent continuation")
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
    outcome = process_decision["execution"]
    if not isinstance(outcome, dict):
        raise AdapterError("native process transport adjudication omitted execution adjudication")
    return apply_execution_adjudication(
        test_result=test_result,
        decoded=decoded,
        native=native,
        decision=outcome,
        step_budget=step_budget,
        expected_status=expected_status_for(test),
        failure_reason_fallback=False,
    )


def execute_batch(
    *,
    tests: list[dict[str, Any]],
    source_path: Path,
    source_text: str,
    module: str,
    session: EmbedSession,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    default_step_budget: int,
    artifacts: ArtifactStore,
    transport: dict[str, Any],
    suite: bool = False,
    native_adjudication_summary: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    request_documents: list[dict[str, Any]] = []
    transport_requests: list[dict[str, Any]] = []
    request_artifacts: list[dict[str, Any]] = []
    needs_identity_reference = any(test.get("first_class") for test in tests)
    artifact_identity = session.info().get("artifact_identity") if needs_identity_reference else None
    if needs_identity_reference and (not isinstance(artifact_identity, str) or not artifact_identity):
        raise AdapterError("retained MNCS session omitted its artifact identity")
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
        callable_reference = None
        if test.get("first_class"):
            identity_fields = (
                "function_identity",
                "declaration_identity",
                "test_case_identity",
                "signature_identity",
            )
            if any(not isinstance(test.get(field), str) or not test[field] for field in identity_fields):
                raise ManifestError(
                    f"first-class test {test.get('id')} is missing compiler callable/signature metadata"
                )
            if not isinstance(artifact_identity, str) or not artifact_identity:
                raise AdapterError("retained MNCS session omitted its artifact identity")
            callable_reference = {
                "artifact_identity": artifact_identity,
                "callable_identity": test["function_identity"],
                "declaration_identity": test["declaration_identity"],
                "test_case_identity": test["test_case_identity"],
                "signature_identity": test["signature_identity"],
            }
        transport_requests.append(
            embed_execution_request(
                module=module,
                function=test["entry"],
                arguments=test.get("arguments", []),
                step_budget=step_budget,
                callable_reference=callable_reference,
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
    adjudication_cases: list[dict[str, Any]] = []
    adjudication_indices: list[int] = []
    for index, test in enumerate(tests):
        if index >= len(decoded) or not isinstance(decoded[index], dict):
            continue
        case, _native = execution_adjudication_case(
            test=test,
            decoded=decoded[index],
            suite=suite,
        )
        adjudication_cases.append(case)
        adjudication_indices.append(index)
    adjudication_by_index: dict[int, dict[str, Any]] = {}
    adjudication_calls = 0
    for offset in range(0, len(adjudication_cases), EXECUTION_ADJUDICATION_MAX_CASES):
        case_batch = adjudication_cases[offset:offset + EXECUTION_ADJUDICATION_MAX_CASES]
        index_batch = adjudication_indices[offset:offset + EXECUTION_ADJUDICATION_MAX_CASES]
        adjudication_results = run_execution_adjudication(
            case_batch,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
        adjudication_by_index.update(
            zip(index_batch, adjudication_results, strict=True)
        )
        adjudication_calls += 1
    if native_adjudication_summary is not None:
        native_adjudication_summary.update({
            "calls": adjudication_calls,
            "case_count": len(adjudication_cases),
        })
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
                adjudication=adjudication_by_index.get(index),
                suite=suite,
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
    kind = test["kind"]
    expects_failure = kind in {"compile-fail", "diagnostic"}
    expected_codes = list(test.get("diagnostic_codes", [])) if expects_failure else []
    if type(process["returncode"]) is not int:
        raise AdapterError("MNCS validation returned no process exit status")
    compiler_result_is_object = isinstance(decoded, dict)
    valid_field_present = compiler_result_is_object and "valid" in decoded
    valid_field_is_true = compiler_result_is_object and decoded.get("valid") is True
    native_verdict = native_compile_verdict(
        diagnostics,
        expected_codes,
        compiler_exit_code=process["returncode"],
        compiler_result_is_object=compiler_result_is_object,
        valid_field_present=valid_field_present,
        valid_field_is_true=valid_field_is_true,
        expects_failure=expects_failure,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )
    test_result["compile_verdict"] = native_verdict
    if native_verdict == "expected_failure":
        test_result["status"] = "expected_failure"
        test_result["verdict"] = "PASS"
        test_result["expected_failure"] = True
        return test_result
    if native_verdict == "compile_pass":
        test_result["status"] = "passed"
        test_result["verdict"] = "PASS"
        return test_result
    if native_verdict == "compile_expectation_mismatch":
        return result_failure(
            test_result,
            failure_class=native_verdict,
            message="source compiled but the manifest expected a diagnostic",
        )
    if native_verdict == "diagnostic_mismatch":
        return result_failure(
            test_result,
            failure_class=native_verdict,
            message="compiler diagnostics did not match the manifest",
            expected_codes=expected_codes,
            observed_codes=[str(item.get("code", "")) for item in diagnostics],
        )
    if native_verdict != "compile_failure":
        raise AdapterError("MNCS compile verdict is not recognized by the Test adapter")
    return result_failure(
        test_result,
        failure_class=native_verdict,
        message="source did not compile",
        diagnostics=diagnostics,
    )


def native_runner_policy(
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    failure_class: str | None = None,
    classification: str | None = None,
    allow_unsupported: bool = False,
) -> tuple[str, int]:
    """Run the Test-owned MNCS classification and exit policy."""
    try:
        return run_runner_policy(
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            failure_class=failure_class,
            classification=classification,
            allow_unsupported=allow_unsupported,
        )
    except NativeTransportError as error:
        raise AdapterError(f"native runner policy failed: {error}") from error


def classification_for_failure(
    failure_class: str,
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
) -> str:
    classification, _exit_code = native_runner_policy(
        failure_class=failure_class,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )
    return classification


def bootstrap_exit_code_for(classification: str, *, allow_unsupported: bool = False) -> int:
    """Map only early failures that occur before the native policy can run."""
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


def manifest_provenance(
    manifest: dict[str, Any],
    source_text: str,
    mncs: str,
    library_paths: list[Path],
    *,
    artifacts: ArtifactStore,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    manifest_path = Path(manifest["manifest_path"])
    source_path = Path(manifest["source_path"])
    compiler_path = Path(mncs)

    def digest_provenance_file(path: Path) -> str:
        if path.stat().st_size > TEST_PROVENANCE_FILE_ADAPTER_MAX_BYTES:
            return sha256_file(path)
        with path.open("rb") as handle:
            payload = handle.read(TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES + 1)
        if len(payload) > TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES:
            return artifacts.digest_file(path)
        return artifacts.digest_payload(payload)

    compiler: dict[str, Any] = {"requested": mncs}
    if compiler_path.is_file():
        compiler["path"] = str(compiler_path.resolve())
        compiler["sha256"] = digest_provenance_file(compiler_path)
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
    tree_mncs = artifacts.mncs or mncs
    tree_cwd = artifacts.cwd or cwd
    tree_environment = artifacts.environment or environment
    libraries: list[dict[str, Any]] = []
    for path in library_paths:
        digest, hashing = _hash_tree_with_method(
            path,
            mncs=tree_mncs,
            cwd=tree_cwd,
            environment=tree_environment,
            artifacts=artifacts,
        )
        libraries.append({"path": str(path), "sha256": digest, "hashing": hashing})
    return {
        "runner": {
            "name": "mncs-test",
            "version": RUNNER_VERSION,
            "adapter": "python-stdlib-transport-plus-mncs-embed-boundary",
            "source_sha256": digest_provenance_file(Path(__file__)),
        },
        "language_profile": manifest["profile"],
        "compiler": compiler,
        "source": {"path": str(source_path), "sha256": digest_provenance_file(source_path)},
        "manifest": {"path": str(manifest_path), "sha256": digest_provenance_file(manifest_path)},
        "libraries": libraries,
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
            "library_paths": [],
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
        verifier = executor.get("verifier_identity") or f"mncs-test-runner/{RUNNER_VERSION}"
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
        "schema_version": OBLIGATION_SELECTION_REQUEST_SCHEMA,
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
    cwd = cwd.resolve()
    try:
        result = run_native_app(
            "obligation-selection.json",
            request,
            request_schema=OBLIGATION_SELECTION_REQUEST_SCHEMA,
            result_schema=OBLIGATION_SELECTION_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
    except NativeTransportError as error:
        raise ManifestError(f"native obligation selection failed: {error}") from error

    if not isinstance(result, dict) or result.get("schema_version") != OBLIGATION_SELECTION_RESULT_SCHEMA:
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


def _coherence_identity_values(obligation: dict[str, Any]) -> list[str]:
    values = [obligation.get("identity", "")]
    values.extend(obligation.get("declared_patterns", []) or [])
    values.extend(obligation.get("inventory_test_identities", []) or [])
    for section in (obligation.get("current", {}) or {}, obligation.get("evidence", {}) or {},
                    obligation.get("external_evidence", {}) or {}):
        if isinstance(section, dict):
            values.extend(
                value for value in section.values() if isinstance(value, str)
            )
    return [value for value in values if isinstance(value, str)]


def evaluate_verification_coherence(
    request: dict[str, Any],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Transport a verification-coherence request to the native policy.

    This is a process/filesystem boundary shim only: it bound-checks the
    request, invokes ``mncs.test.verification_coherence`` through run-app,
    and returns the validated native result. All reuse/selection policy
    lives in the native module.
    """
    if not isinstance(request, dict):
        raise ManifestError("verification coherence request must be an object")
    if request.get("schema_version") != VERIFICATION_COHERENCE_REQUEST_SCHEMA:
        raise ManifestError("verification coherence request has an invalid schema version")
    obligations = request.get("obligations", [])
    if not isinstance(obligations, list):
        raise ManifestError("verification coherence request obligations must be a list")
    if len(obligations) > VERIFICATION_COHERENCE_MAX_OBLIGATIONS:
        raise ManifestError(
            "verification coherence bound exceeded: at most "
            f"{VERIFICATION_COHERENCE_MAX_OBLIGATIONS} obligations"
        )
    for obligation in obligations:
        if not isinstance(obligation, dict):
            raise ManifestError("verification coherence obligation must be an object")
        identities = _coherence_identity_values(obligation)
        if len(obligation.get("declared_patterns", []) or []) > 8:
            raise ManifestError("verification coherence bound exceeded: at most 8 declared patterns")
        if len(obligation.get("inventory_test_identities", []) or []) > VERIFICATION_COHERENCE_MAX_IDENTITIES:
            raise ManifestError(
                "verification coherence bound exceeded: at most "
                f"{VERIFICATION_COHERENCE_MAX_IDENTITIES} inventory test identities"
            )
        if any(
            len(identity.encode("utf-8")) > VERIFICATION_COHERENCE_MAX_IDENTITY_LENGTH
            for identity in identities
        ):
            raise ManifestError(
                "verification coherence bound exceeded: at most "
                f"{VERIFICATION_COHERENCE_MAX_IDENTITY_LENGTH} UTF-8 bytes per identity"
            )
    cwd = cwd.resolve()
    try:
        result = run_native_app(
            "test-verification-coherence.json",
            request,
            request_schema=VERIFICATION_COHERENCE_REQUEST_SCHEMA,
            result_schema=VERIFICATION_COHERENCE_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
    except NativeTransportError as error:
        raise ManifestError(f"native verification coherence failed: {error}") from error
    if not isinstance(result, dict) or result.get("schema_version") != VERIFICATION_COHERENCE_RESULT_SCHEMA:
        raise ManifestError("native verification coherence returned an invalid result schema")
    return result


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
    environment: dict[str, str],
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
    _resolved_classification, result_exit_code = native_runner_policy(
        classification=classification,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
        allow_unsupported=allow_unsupported,
    )
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
        summary = native_result_summary(
            test_results,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
        summary["authority"] = "mncs.test.result_summary"
    if classification == "success":
        verdict = "PASS"
    elif classification == "unsupported":
        verdict = "UNKNOWN"
    else:
        verdict = "FAIL"
    provenance = manifest_provenance(
        manifest,
        source_text,
        mncs,
        library_paths,
        artifacts=artifacts,
        cwd=cwd,
        environment=environment,
    )
    run_id = build_run_identity_sha256(
        provenance=provenance,
        profile=manifest["profile"],
        test_results=test_results,
        test_inventory=test_inventory,
        verification_plan=verification_plan,
        obligation_plan=obligation_plan,
        execution=execution,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )
    experiment_identity = f"mncs:language:experiment:test:{run_id}"
    experiment_run_identity = f"mncs:language:experiment-run:{run_id}"
    lineage_rows: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    lineage_identity_inputs: list[tuple[int, str, str, Any, Any]] = []
    for index, item in enumerate(test_results):
        semantic = item.get("semantic")
        if not isinstance(semantic, dict) or not semantic.get("test_case_identity"):
            continue
        lineage_identity_inputs.append((
            index,
            experiment_run_identity,
            semantic["test_case_identity"],
            item.get("execution_status", item.get("status")),
            item.get("verdict"),
        ))
        lineage_rows.append((index, item, semantic))

    lineage_digests = lineage_identity_sha256_many(
        lineage_identity_inputs,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )

    for (index, item, semantic), (execution_digest, observation_digest) in zip(
        lineage_rows, lineage_digests
    ):
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
        "exit_code": result_exit_code,
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
        "exit_code": bootstrap_exit_code_for(classification),
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
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    check_id: str | None = None,
    claim: str | None = None,
) -> dict[str, Any]:
    """Project a detailed TestResult through the native CheckResult policy."""
    if not isinstance(result, dict):
        raise AdapterError("Test result for CheckResult projection must be an object")

    def bounded_text(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise AdapterError(f"Test result {field} must be text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise AdapterError(f"Test result {field} must be valid UTF-8") from error
        if len(encoded) > maximum:
            raise AdapterError(f"Test result {field} exceeds the {maximum}-byte native bound")
        return value

    maximum_u64 = (1 << 64) - 1

    def summary_count(summary: dict[str, Any], field: str) -> int:
        value = summary.get(field, 0)
        if type(value) is not int or not 0 <= value <= maximum_u64:
            raise AdapterError(f"Test result summary {field} is outside the u64 range")
        return value

    def bounded_chunks(value: str, field: str) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_bytes = 0
        total_bytes = 0
        for character in value:
            character_bytes = len(character.encode("utf-8"))
            total_bytes += character_bytes
            if total_bytes > 256 * 1024:
                raise AdapterError(f"Test result {field} exceeds the 262144-byte native bound")
            if current and current_bytes + character_bytes > 1024:
                chunks.append("".join(current))
                current = []
                current_bytes = 0
            current.append(character)
            current_bytes += character_bytes
        if current:
            chunks.append("".join(current))
        return chunks

    verdict = bounded_text(result.get("verdict"), "verdict", 16)
    raw_scope = result.get("scope", {})
    if not isinstance(raw_scope, dict):
        raise AdapterError("Test result scope must be an object")
    raw_scope_manifest = raw_scope.get("manifest")
    scope_manifest_present = "manifest" in raw_scope and raw_scope_manifest is not None
    scope_manifest = (
        bounded_text(raw_scope_manifest, "scope manifest", 1024)
        if raw_scope_manifest is not None
        else "mncs-test"
    )
    raw_summary = result.get("summary", {})
    if not isinstance(raw_summary, dict):
        raise AdapterError("Test result summary must be an object")
    raw_tests = result.get("tests", [])
    if not isinstance(raw_tests, list):
        raise AdapterError("Test result tests must be an array")
    test_rows: list[dict[str, str]] = []
    for item in raw_tests:
        if not isinstance(item, dict):
            continue
        raw_identity = item.get("id")
        # The former f-string omitted falsy identities and stringified other
        # truthy values. Keep that projection at the adapter boundary.
        identity = str(raw_identity) if raw_identity else ""
        test_rows.append({
            "id": bounded_text(identity, "test identity", 512),
            "verdict": bounded_text(
                item.get("verdict") if isinstance(item.get("verdict"), str) else "",
                "test verdict",
                16,
            ),
        })
    if len(test_rows) > 256:
        raise AdapterError("Test result CheckResult projection exceeds the 256-row native bound")

    raw_failure = result.get("failure", {})
    failure_message_present = isinstance(raw_failure, dict) and "message" in raw_failure
    failure_message = raw_failure.get("message") if failure_message_present else ""
    if failure_message_present:
        failure_message = bounded_text(failure_message, "failure message", 256 * 1024)
        failure_message_chunks = bounded_chunks(failure_message, "failure message")
    else:
        failure_message = ""
        failure_message_chunks = []
    raw_provenance = result.get("provenance", {})
    if not isinstance(raw_provenance, dict):
        raw_provenance = {}
    raw_runner = raw_provenance.get("runner", {})
    if not isinstance(raw_runner, dict):
        raw_runner = {}

    request = {
        "schema_version": CHECK_RESULT_PROJECTION_REQUEST_SCHEMA,
        "id": bounded_text(check_id or result["id"], "id", 1024),
        "provider": bounded_text(result.get("provider"), "provider", 256),
        "verdict": verdict,
        "scope_manifest": scope_manifest,
        "scope_manifest_present": scope_manifest_present,
        "claim": bounded_text(
            claim or "", "claim", 1024
        ),
        "classification": bounded_text(result.get("classification"), "classification", 128),
        "failure_class": bounded_text(result.get("failure_class"), "failure class", 128),
        "result_digest": bounded_text(result_digest, "result digest", 64),
        "source_sha256": bounded_text(
            raw_runner.get("source_sha256", ""), "runner source digest", 64
        ),
        "summary": {
            "passed": summary_count(raw_summary, "passed"),
            "failed": summary_count(raw_summary, "failed"),
            "skipped": summary_count(raw_summary, "skipped"),
        },
        "failure_message_present": failure_message_present,
        "failure_message_count": len(failure_message_chunks),
        "failure_message_chunks": failure_message_chunks,
        "run_id": bounded_text(result.get("run_id", ""), "run id", 512),
        "test_count": len(test_rows),
        "tests": test_rows,
    }
    try:
        projected = run_native_app(
            "test-check-result-projection.json",
            request,
            request_schema=CHECK_RESULT_PROJECTION_REQUEST_SCHEMA,
            result_schema=CHECK_RESULT_PROJECTION_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
    except NativeTransportError as error:
        raise AdapterError(f"native Test CheckResult projection failed: {error}") from error

    # The application transports long unresolved text in UTF-8-safe bounded
    # chunks. Reassembly and the external CheckResult field names belong to
    # this adapter boundary; the native application chose the disposition.
    if (
        projected.get("result_schema_version") != CHECK_SCHEMA
        or type(projected.get("unresolved_present")) is not bool
        or not isinstance(projected.get("unresolved_chunks"), list)
        or any(not isinstance(chunk, str) for chunk in projected["unresolved_chunks"])
    ):
        raise AdapterError("MNCS CheckResult projection returned malformed transport fields")
    unresolved_present = projected.pop("unresolved_present")
    unresolved_chunks = projected.pop("unresolved_chunks")
    result_schema_version = projected.pop("result_schema_version")
    projected["schema_version"] = result_schema_version
    projected["unresolved"] = ["".join(unresolved_chunks)] if unresolved_present else []

    # Selection is an opaque, potentially forward-compatible result field.
    # The native policy does not interpret it; the adapter preserves it.
    projected["selection"] = result.get("selection")
    return projected


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
        check_result_environment = native_application_environment(cwd)
        check_result_document = check_result(
            result,
            result_digest,
            mncs=args.mncs,
            cwd=cwd,
            environment=check_result_environment,
            check_id=request["check_identity"],
            claim="The exact repository-owned behavioral family check was executed by mncs-test",
        )
        _, check_result_digest = _public_result_json_payload(
            check_result_document,
            mncs=args.mncs,
            cwd=cwd,
            environment=check_result_environment,
        )
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
        response_payload, _ = _public_result_json_payload(
            response,
            mncs=args.mncs,
            cwd=cwd,
            environment=check_result_environment,
        )
        sys.stdout.write(response_payload.decode("utf-8"))
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


def _public_result_json_payload(
    value: Any,
    *,
    mncs: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Prefer the native public-result serializer and preserve legacy fallback."""
    if mncs is not None and cwd is not None and environment is not None:
        native = _native_public_result_payload(
            value,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
        if native is not None:
            return native
    payload = json_bytes(value)
    return payload, sha256_bytes(payload)


def write_output(
    path: Path | None,
    value: Any,
    *,
    mncs: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str | None:
    """Write a result artifact after native-first, byte-compatible serialization."""
    if path is None:
        return None
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload, digest = _public_result_json_payload(
        value,
        mncs=mncs,
        cwd=cwd,
        environment=environment,
    )
    path.write_bytes(payload)
    return digest


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


def select_tests(
    tests: list[dict[str, Any]],
    filters: list[str],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    selection_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select inventory records through the MNCS substring-filter kernel."""
    if not filters:
        selection_summary.update({
            "authority": "manifest_default",
            "mode": "all",
            "status": "not_required",
            "calls": 0,
            "case_count": len(tests),
            "filter_count": 0,
            "selected_count": len(tests),
        })
        return tests
    search_fields = (
        "id", "entry", "qualified_name", "declaration_identity", "test_case_identity",
    )
    search_texts = [
        " ".join(str(test.get(field, "")) for field in search_fields)
        for test in tests
    ]
    try:
        selected = run_test_inventory_filter(
            search_texts,
            filters,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
    except NativeInputBoundError as error:
        # MNCS v2 bounds search text at 65536 bytes and filter terms at 1024.
        # Preserve the legacy behavior beyond either input bound and report
        # the host fallback instead of claiming native selection.
        selected_tests = []
        if not filters:
            selected_tests = tests
        else:
            for test, haystack in zip(tests, search_texts, strict=True):
                if any(pattern in haystack for pattern in filters):
                    selected_tests.append(test)
        selection_summary.update({
            "authority": "mncs-test.python_filter_fallback",
            "requested_native_authority": "mncs.test.inventory_filter_v2",
            "mode": "substring_any",
            "status": (
                "host_fallback_input_bound"
                if error.reason in {
                    "input_exceeds_native_1024_byte_bound",
                    "input_exceeds_native_65536_byte_bound",
                }
                else "host_fallback_call_limit"
                if error.reason == "native_batch_call_limit_exceeded"
                else "host_fallback_work_estimate"
            ),
            "fallback_reason": error.reason,
            "calls": 0,
            "case_count": len(tests),
            "filter_count": len(filters),
            "selected_count": len(selected_tests),
        })
        return selected_tests
    except NativeTransportError as error:
        raise ManifestError(f"native Test inventory filter failed: {error}") from error
    indices = selected["selected_indices"]
    if not isinstance(indices, list):
        raise ManifestError("native Test inventory filter returned malformed selection indices")
    selection_summary.update({
        "authority": "mncs.test.inventory_filter_v2",
        "mode": "substring_any",
        "status": "returned" if selected["calls"] else "not_run",
        "calls": selected["calls"],
        "case_count": selected["case_count"],
        "filter_count": selected["filter_count"],
        "selected_count": selected["selected_count"],
    })
    return [tests[index] for index in indices]


def select_exact_test_identities(
    tests: list[dict[str, Any]],
    requested: list[str],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    selection_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select exact compiler identities through the MNCS Test kernel."""

    expected = sorted(set(requested))
    if not expected or len(expected) != len(requested):
        raise ManifestError("family check test identities must be a non-empty unique list")

    candidate_pairs = [
        (index, str(test.get("id")))
        for index, test in enumerate(tests)
        if test.get("id")
    ]
    try:
        selection = run_exact_test_identity_selection(
            [identity for _, identity in candidate_pairs],
            expected,
            candidate_indices=[index for index, _ in candidate_pairs],
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
    except NativeInputBoundError as error:
        # Keep the previous behavior for inputs beyond the native record or
        # step budget, and report that the host owns this exceptional result.
        by_id = {str(test.get("id")): test for test in tests if test.get("id")}
        missing = sorted(set(expected) - set(by_id))
        if missing:
            raise ManifestError(
                "family check names tests absent from the current compiler inventory: "
                + ", ".join(missing)
            )
        selected = [by_id[test_id] for test_id in expected]
        selection_summary.update({
            "authority": "mncs-test.python_identity_fallback",
            "requested_native_authority": "mncs.test.exact_identity_selection_v2",
            "mode": "exact_test_identity",
            "status": (
                "host_fallback_input_bound"
                if error.reason == "input_exceeds_native_8192_byte_bound"
                else (
                    "host_fallback_call_limit"
                    if error.reason == "native_batch_call_limit_exceeded"
                    else "host_fallback_work_estimate"
                )
            ),
            "fallback_reason": error.reason,
            "calls": 0,
            "case_count": len(candidate_pairs),
            "identity_count": len(expected),
            "selected_count": len(selected),
        })
        return selected
    except NativeTransportError as error:
        raise ManifestError(f"native Test identity selection failed: {error}") from error

    indices = selection.get("selected_indices")
    if not isinstance(indices, list) or len(indices) != len(expected):
        raise ManifestError("native Test identity selection returned malformed indices")
    missing = [identity for identity, index in zip(expected, indices, strict=True) if index is None]
    if missing:
        raise ManifestError(
            "family check names tests absent from the current compiler inventory: "
            + ", ".join(missing)
        )
    selected = []
    for index in indices:
        if type(index) is not int or index < 0 or index >= len(tests):
            raise ManifestError("native Test identity selection returned an out-of-range test index")
        selected.append(tests[index])
    selection_summary.update({
        "authority": "mncs.test.exact_identity_selection_v2",
        "mode": "exact_test_identity",
        "status": "returned" if selection["calls"] else "not_run",
        "calls": selection["calls"],
        "case_count": selection["case_count"],
        "identity_count": selection["identity_count"],
        "selected_count": selection["selected_count"],
    })
    return selected


def run_manifest(args: argparse.Namespace) -> int:
    run_started = time.perf_counter()
    cwd = Path.cwd().resolve()
    result_path, check_path, artifact_root = result_paths(args, cwd)
    manifest_path = resolve_path(args.manifest, cwd)
    artifacts: ArtifactStore | None = None
    try:
        manifest = load_manifest(manifest_path)
        source_text = read_source(Path(manifest["source_path"]))
        mncs = resolve_run_mncs(
            args.mncs, getattr(args, "execution", "stage0-reference"), cwd)
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
        artifacts = ArtifactStore(
            artifact_root,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
        suite_result: dict[str, Any] | None = None
        suite_summary: dict[str, Any] | None = None
        test_results: list[dict[str, Any]] = []
        test_inventory: dict[str, Any] | None = None
        execution: dict[str, Any] | None = None
        native_selection_summary: dict[str, Any] | None = None
        inventory_failure: dict[str, Any] | None = None
        configured_tests = list(manifest["tests"])
        if getattr(args, "execution", "stage0-reference") == "canonical-vm" and (configured_tests or manifest.get("suite")):
            raise ManifestError("canonical-vm requires compiler-owned first-class test inventory; explicit legacy tests/suite require the stage0-reference lane")

        # A normal runtime manifest names a source/module and policy only.
        # The compiler is the sole authority for first-class test declarations.
        if not configured_tests and not manifest.get("suite"):
            vm_product = vm_evidence = None
            if getattr(args, "execution", "stage0-reference") == "canonical-vm":
                if not args.compiler_checkout or not args.vm_checkout or not args.vm_executable or not args.vm_cache:
                    raise ManifestError("canonical-vm requires exact compiler/VM checkout, VM executable and artifact cache selections")
                from vm_transport import prepare
                try:
                    vm_product, vm_evidence, test_inventory = prepare(
                        checkout=args.compiler_checkout, executable=args.compiler_executable,
                        source=manifest["source_path"], libraries=[p for p in environment.get("MNCS_LIBRARY_PATH", "").split(os.pathsep) if p],
                        cache=args.vm_cache, timeout=args.timeout_seconds or manifest["timeout_seconds"])
                except (OSError, ValueError, RuntimeError) as error:
                    raise AdapterError("canonical compiler preparation refused: " + str(error)) from error
                inventory_path = artifacts.write("compiler/direct-test-inventory.json", json_bytes(test_inventory), "compiler-test-inventory")
                inventory_transport = {"process": {"stdout_artifact": inventory_path}, "document": test_inventory}
            else:
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
                    native_selection_summary = {}
                    configured_tests = select_exact_test_identities(
                        configured_tests,
                        args.test_identity,
                        mncs=mncs,
                        cwd=cwd,
                        environment=environment,
                        timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                        selection_summary=native_selection_summary,
                    )
                else:
                    native_selection_summary = {}
                    configured_tests = select_tests(
                        configured_tests,
                        args.filter or [],
                        mncs=mncs,
                        cwd=cwd,
                        environment=environment,
                        timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                        selection_summary=native_selection_summary,
                    )
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
                native_selection_summary = {}
                configured_tests = select_exact_test_identities(
                    configured_tests,
                    args.test_identity,
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                    selection_summary=native_selection_summary,
                )
            else:
                native_selection_summary = {}
                configured_tests = select_tests(
                    configured_tests,
                    args.filter or [],
                    mncs=mncs,
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=args.timeout_seconds or manifest["timeout_seconds"],
                    selection_summary=native_selection_summary,
                )

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
                if getattr(args, "execution", "stage0-reference") == "canonical-vm":
                    from vm_transport import VmTestSession
                    session = VmTestSession(vm_checkout=args.vm_checkout, vm_executable=args.vm_executable,
                        product=vm_product, evidence=vm_evidence, timeout=args.timeout_seconds or manifest["timeout_seconds"], adapter_error=AdapterError)
                    execution_detail = {"mode":"canonical-vm-session", "artifact_identity":vm_product["artifact"]["identity"],
                        "artifact":vm_product["artifact"], "compiler_producer":vm_product["producer"],
                        "build_receipt":vm_product["build_receipt"], "runtime":session.runtime.runtime,
                        "cache_reused":vm_product["cache_reused"], "source_map_identity":(vm_evidence.get("source_map") or {}).get("identity")}
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
                            "mode": execution.get("mode", "retained-embed-batch"),
                            "library": execution.get("library"),
                            "artifact_identity": execution.get("artifact_identity"),
                            "artifact_sha256": (execution.get("session") or {}).get("artifact_sha256"),
                            "batch_size": len(configured_tests),
                        }
                        adjudication_summary: dict[str, int] = {}
                        test_results = execute_batch(
                            tests=configured_tests,
                            source_path=Path(manifest["source_path"]),
                            source_text=source_text,
                            module=manifest["module"],
                            session=session,
                            mncs=mncs,
                            cwd=cwd,
                            environment=environment,
                            default_step_budget=args.step_budget or manifest["step_budget"],
                            artifacts=artifacts,
                            transport=transport,
                            native_adjudication_summary=adjudication_summary,
                        )
                        execution["native_adjudication"] = {
                            "authority": "mncs.test.execution_adjudication_v1",
                            "status": "returned" if adjudication_summary.get("calls", 0) else "not_run",
                            **adjudication_summary,
                        }
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
            classification = classification_for_failure(
                failure_class, mncs=mncs, cwd=cwd, environment=environment,
            )
            message = inventory_failure["message"]
            failure_details = inventory_failure.get("details")
        if suite_result and suite_result.get("failure"):
            failure = suite_result["failure"]
            failure_class = str(failure.get("class", "infrastructure_failure"))
            classification = classification_for_failure(
                failure_class, mncs=mncs, cwd=cwd, environment=environment,
            )
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
                    classification = classification_for_failure(
                        failure_class, mncs=mncs, cwd=cwd, environment=environment,
                    )
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
        if native_selection_summary is not None:
            execution = {
                **(execution or {}),
                "native_selection": native_selection_summary,
            }
        result = make_result(
            manifest=manifest_for_run,
            source_text=source_text,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
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
    finally:
        if artifacts is not None:
            artifacts.close()
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
        result_digest = write_output(
            result_path,
            result,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
    else:
        _, result_digest = _public_result_json_payload(
            result,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
    if check_path:
        write_output(
            check_path,
            check_result(
                result,
                result_digest or sha256_bytes(json_bytes(result)),
                mncs=mncs,
                cwd=cwd,
                environment=environment,
            ),
            mncs=mncs,
            cwd=cwd,
            environment=environment,
        )
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
                    artifacts = ArtifactStore(
                        Path(directory),
                        mncs=mncs,
                        cwd=Path.cwd(),
                        environment=command_environment(libraries),
                    )
                    try:
                        inventory, transport = compiler_inventory(
                            source_path=Path(manifest["source_path"]),
                            mncs=mncs,
                            cwd=Path.cwd(),
                            environment=command_environment(libraries),
                            timeout_seconds=manifest["timeout_seconds"],
                            artifacts=artifacts,
                        )
                    finally:
                        artifacts.close()
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
    # Environment binds MNCS to the selected Stage-0 reference executable.
    # Canonical VM execution also records that reference identity, so it must
    # not silently discover a compiler from ambient PATH.
    run.add_argument("--mncs", default=os.environ.get("MNCS"))
    run.add_argument("--execution", choices=("stage0-reference", "canonical-vm"), default="stage0-reference")
    run.add_argument("--compiler-checkout", default=os.environ.get("MNCS_COMPILER_CHECKOUT"))
    run.add_argument("--compiler-executable", default=os.environ.get("MNCS_COMPILER_PROBE"))
    run.add_argument("--vm-checkout", default=os.environ.get("MNCS_VM_CHECKOUT"))
    run.add_argument("--vm-executable", default=os.environ.get("MNCS_VM_BIN"))
    run.add_argument("--vm-cache", default=os.environ.get("MNCS_VM_ARTIFACT_CACHE"))
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
