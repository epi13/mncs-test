#!/usr/bin/env python3
"""Transport for the native test digest (`mncs.test.digest`).

A full ``mncs.test-result/1`` record is provenance-grade evidence: typed
value dumps, artifact receipts, execution lineage. Agents usually need the
opposite: which tests ran, what failed, and where. This module wraps one
test result for run-app and renders the returned native digest as compact
text.

Relevance policy, result validation, and typed projection live in
``native/mncs/test/digest.mncs``. This file only wraps the source document,
fills absent optional source-span locations with zeroes for the typed
boundary, moves JSON across the process boundary, and formats the native
answer. It never parses human-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mncs_test_native import NativeTransportError, run_native_app

DIGEST_REQUEST_SCHEMA = "mncs.test-digest-request/1"
DIGEST_RESULT_SCHEMA = "mncs.test-digest/1"

MAX_U64 = (1 << 64) - 1


class DigestError(ValueError):
    """Raised for unusable digest inputs or transports (fail closed)."""


def _digest_input(result: dict, max_failures: int) -> dict:
    """Wrap the source document without interpreting its result fields."""
    if not isinstance(result, dict):
        raise DigestError("digest input must be an object")
    if isinstance(max_failures, bool) or not isinstance(max_failures, int) or max_failures < 0:
        raise DigestError("max_failures must be a non-negative integer")

    # `source_span` is optional in mncs.test-result/1. Keep the original
    # fields and values, adding only the zero defaults the native typed view
    # needs for absent or null line/column values.
    normalized = dict(result)
    scope = result.get("scope")
    if isinstance(scope, dict) and "module" in scope:
        normalized_scope = dict(scope)
        # `module` is a reserved MNCS token and cannot be a record member
        # name in the typed wire view.
        normalized_scope["module_name"] = scope["module"]
        normalized["scope"] = normalized_scope
    tests = result.get("tests")
    if isinstance(tests, list):
        normalized_tests = []
        for test in tests:
            if not isinstance(test, dict):
                normalized_tests.append(test)
                continue
            normalized_test = dict(test)
            span = test.get("source_span")
            if isinstance(span, dict):
                normalized_span = dict(span)
                for field in ("line", "column"):
                    if normalized_span.get(field) is None:
                        normalized_span[field] = 0
            else:
                normalized_span = {"line": 0, "column": 0}
            normalized_test["source_span"] = normalized_span
            normalized_tests.append(normalized_test)
        normalized["tests"] = normalized_tests

    return {
        "schema_version": DIGEST_REQUEST_SCHEMA,
        "max_failures": min(max_failures, MAX_U64),
        "result": normalized,
    }


def evaluate_digest(
    result: dict,
    *,
    max_failures: int = 8,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float = 120.0,
) -> dict:
    """Transport a test result to the native policy and return the digest."""
    try:
        return run_native_app(
            "test-digest.json",
            _digest_input(result, max_failures),
            request_schema=DIGEST_REQUEST_SCHEMA,
            result_schema=DIGEST_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
            result_filename="digest.json",
        )
    except NativeTransportError as error:
        raise DigestError(str(error)) from error


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
        mncs = arguments.mncs or __import__("os").environ.get("MNCS", "mncs")
        digest = evaluate_digest(
            result,
            max_failures=arguments.max_failures,
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
