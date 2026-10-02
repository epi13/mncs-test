#!/usr/bin/env python3
"""Transport for the native test digest (`mncs.test.digest`).

A full ``mncs.test-result/1`` record is provenance-grade evidence: typed
value dumps, artifact receipts, execution lineage. Agents usually need the
opposite: which tests ran, what failed, and where. This module projects one
test result into a ``mncs.test-digest-request/1`` record, evaluates the
native digest policy through run-app, and renders the returned digest as
compact text.

Relevance policy (counts, verdict precedence, failure-row selection,
truncation) lives in ``native/mncs/test/digest.mncs``. This file only
re-keys documented result fields, moves JSON across the process boundary
with the same bounds discipline as the other transports, and formats the
native answer. It never parses human-readable output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_RESULT_SCHEMA = "mncs.test-result/1"
DIGEST_REQUEST_SCHEMA = "mncs.test-digest-request/1"
DIGEST_RESULT_SCHEMA = "mncs.test-digest/1"

MAX_TESTS = 256
MAX_FAILURE_ROWS = 32
MAX_IDENTITY_BYTES = 1024
MAX_FINGERPRINT_BYTES = 128

VERDICTS = ("PASS", "FAIL", "SKIP", "UNSUPPORTED")

FAILURE_KINDS = {
    "nofailure": "NoFailure",
    "assertion": "Assertion",
    "setup": "Setup",
    "compile": "Compile",
    "runtime": "Runtime",
    "timeout": "Timeout",
    "unsupported": "Unsupported",
    "infrastructure": "Infrastructure",
}


class DigestError(ValueError):
    """Raised for unusable digest inputs or transports (fail closed)."""


def _bounded_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise DigestError(f"digest projection requires a non-empty {label}")
    if len(value.encode("utf-8")) > limit:
        raise DigestError(f"digest projection bound exceeded: {label} over {limit} bytes")
    return value


def _bounded_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DigestError(f"digest projection requires an integer {label}")
    return value


def _bounded_count(value: object, label: str) -> int:
    number = _bounded_int(value, label)
    if number < 0:
        raise DigestError(f"digest projection requires a non-negative {label}")
    return number


def project_digest_request(result: dict, *, max_failures: int = 8) -> dict:
    """Project one test result into a native digest request.

    Every field below is re-keyed from the documented test-result schema;
    unknown verdicts, failure kinds, or shapes fail closed instead of
    guessing.
    """
    if not isinstance(result, dict):
        raise DigestError("digest input must be an object")
    if result.get("schema_version") != TEST_RESULT_SCHEMA:
        raise DigestError("digest input has an invalid schema version")
    if not isinstance(max_failures, int) or max_failures < 0:
        raise DigestError("max_failures must be a non-negative integer")
    scope = result.get("scope")
    execution = result.get("execution")
    if not isinstance(scope, dict) or not isinstance(execution, dict):
        raise DigestError("digest input requires scope and execution objects")
    tests = result.get("tests")
    if not isinstance(tests, list):
        raise DigestError("digest input requires a tests list")
    if len(tests) > MAX_TESTS:
        raise DigestError(f"digest bound exceeded: at most {MAX_TESTS} tests")
    projected = []
    for test in tests:
        if not isinstance(test, dict):
            raise DigestError("digest test entry must be an object")
        semantic = test.get("semantic")
        native = test.get("native_result")
        span = test.get("source_span")
        if not isinstance(semantic, dict) or not isinstance(native, dict):
            raise DigestError("digest test entry requires semantic and native_result objects")
        verdict = native.get("verdict")
        if verdict not in VERDICTS:
            raise DigestError(f"digest test entry has an unknown verdict: {verdict!r}")
        kind = native.get("failure_kind")
        if not isinstance(kind, str) or kind.lower() not in FAILURE_KINDS:
            raise DigestError(f"digest test entry has an unknown failure kind: {kind!r}")
        line = 0
        column = 0
        if isinstance(span, dict):
            if span.get("line") is not None:
                line = _bounded_count(span.get("line"), "source line")
            if span.get("column") is not None:
                column = _bounded_count(span.get("column"), "source column")
        projected.append(
            {
                "qualified_name": _bounded_text(
                    semantic.get("qualified_name"), "qualified name", MAX_IDENTITY_BYTES
                ),
                "verdict": verdict,
                "failure_kind": FAILURE_KINDS[kind.lower()],
                "assertions": _bounded_count(native.get("assertions"), "assertion count"),
                "failures": _bounded_count(native.get("failures"), "failure count"),
                "expected": _bounded_int(native.get("expected"), "expected value"),
                "actual": _bounded_int(native.get("actual"), "actual value"),
                "assertion_code": _bounded_count(native.get("assertion_code"), "assertion code"),
                "source": _bounded_text(test.get("source"), "test source", MAX_IDENTITY_BYTES),
                "line": line,
                "column": column,
                "semantic_fingerprint": _bounded_text(
                    semantic.get("semantic_fingerprint"),
                    "semantic fingerprint",
                    MAX_FINGERPRINT_BYTES,
                ),
            }
        )
    return {
        "schema_version": DIGEST_REQUEST_SCHEMA,
        "max_failures": min(max_failures, MAX_FAILURE_ROWS),
        "run": {
            "run_id": _bounded_text(result.get("run_id"), "run id", MAX_FINGERPRINT_BYTES),
            "module_name": _bounded_text(scope.get("module"), "scope module", MAX_IDENTITY_BYTES),
            "source": _bounded_text(scope.get("source"), "scope source", MAX_IDENTITY_BYTES),
            "backend": _bounded_text(execution.get("backend"), "backend", MAX_FINGERPRINT_BYTES),
            "artifact_identity": _bounded_text(
                execution.get("artifact_identity"), "artifact identity", MAX_IDENTITY_BYTES
            ),
        },
        "tests": projected,
    }


def evaluate_digest(
    request: dict,
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> dict:
    """Transport a digest request to the native policy and return the digest."""
    if not isinstance(request, dict):
        raise DigestError("digest request must be an object")
    if request.get("schema_version") != DIGEST_REQUEST_SCHEMA:
        raise DigestError("digest request has an invalid schema version")
    descriptor = (
        Path(__file__).resolve().parents[1] / "native-applications" / "test-digest.json"
    )
    if not descriptor.is_file():
        raise DigestError(f"digest descriptor is unavailable: {descriptor}")
    cwd = cwd.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix=".mncs-test-digest-", dir=cwd) as directory:
            directory_path = Path(directory)
            request_path = directory_path / "request.json"
            result_path = directory_path / "digest.json"
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
                raise DigestError(f"native digest failed (exit {completed.returncode}): {detail}")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DigestError(f"native digest returned no valid result: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise DigestError(f"native digest exceeded {timeout_seconds}s") from error
    if not isinstance(result, dict) or result.get("schema_version") != DIGEST_RESULT_SCHEMA:
        raise DigestError("native digest returned an invalid result schema")
    return result


def render_digest_text(digest: dict) -> str:
    """Render one native digest as compact agent-facing text."""
    if not isinstance(digest, dict):
        raise DigestError("digest must be an object")
    summary = digest.get("summary")
    run = digest.get("run")
    failures = digest.get("failures")
    if not isinstance(summary, dict) or not isinstance(run, dict):
        raise DigestError("digest requires summary and run objects")
    if not isinstance(failures, list):
        raise DigestError("digest requires a failures list")
    lines = [
        (
            f"mncs-test digest: {summary.get('verdict')} {run.get('module_name')} "
            f"({summary.get('total')} tests, {summary.get('passed')} passed, "
            f"{summary.get('failed')} failed, {summary.get('skipped')} skipped, "
            f"{summary.get('unsupported')} unsupported)"
        )
    ]
    for failure in failures:
        if not isinstance(failure, dict):
            raise DigestError("digest failure row must be an object")
        lines.append(
            f"  FAIL {failure.get('qualified_name')} "
            f"expected={failure.get('expected')} actual={failure.get('actual')} "
            f"code={failure.get('assertion_code')} "
            f"{failure.get('source')}:{failure.get('line')} "
            f"fp={str(failure.get('semantic_fingerprint'))[:16]}"
        )
    omitted = digest.get("failures_omitted", 0)
    if isinstance(omitted, int) and omitted > 0:
        lines.append(f"  ... {omitted} further failure(s) omitted (digest bound)")
    lines.append(f"run {run.get('run_id')} backend={run.get('backend')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="native test digest transport")
    parser.add_argument("result", type=Path, help="mncs.test-result/1 JSON document")
    parser.add_argument("--digest-out", type=Path, default=None)
    parser.add_argument("--max-failures", type=int, default=8)
    parser.add_argument("--mncs", default=None)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    arguments = parser.parse_args(argv)
    try:
        result = json.loads(arguments.result.read_text(encoding="utf-8"))
        request = project_digest_request(result, max_failures=arguments.max_failures)
        mncs = arguments.mncs or __import__("os").environ.get("MNCS", "mncs")
        digest = evaluate_digest(
            request,
            mncs=mncs,
            cwd=arguments.result.resolve().parent,
            environment=dict(__import__("os").environ),
            timeout_seconds=arguments.timeout_seconds,
        )
    except (OSError, json.JSONDecodeError, DigestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if arguments.digest_out is not None:
        arguments.digest_out.write_text(json.dumps(digest, indent=2) + "\n", encoding="utf-8")
    if arguments.format == "json":
        print(json.dumps(digest, indent=2))
    else:
        print(render_digest_text(digest), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
