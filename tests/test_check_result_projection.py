from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = next(
    (path for path in (ROOT.parent, *ROOT.parents) if (path / "mncs-language").is_dir()),
    ROOT.parent,
)
LANGUAGE_ROOT = WORKSPACE_ROOT / "mncs-language"
_default_mncs = LANGUAGE_ROOT / "target/release/mncs"
if not _default_mncs.is_file():
    _default_mncs = LANGUAGE_ROOT / "target/debug/mncs"
MNCS = Path(os.environ.get("MNCS_BINARY", os.environ.get("MNCS", str(_default_mncs))))

sys.path.insert(0, str(ROOT / "tools"))
import mncs_test  # noqa: E402


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib), str(ROOT / "native")))
    return environment


pytestmark = pytest.mark.skipif(
    not MNCS.is_file() or not os.access(MNCS, os.X_OK),
    reason="a built sibling MNCS executable is required",
)


def test_native_check_result_projection_matches_family_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_identity = "mncs:test-case:" + ("long-identity-" * 23)
    assert 256 < len(long_identity.encode("utf-8")) <= 512
    result_digest = "d" * 64
    source_digest = "a" * 64
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": {"manifest": "mncs-test.toml"},
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": "the native test application is unavailable"},
        "summary": {"passed": 12, "failed": 2, "skipped": 3},
        "tests": [
            {"id": long_identity, "verdict": "FAIL"},
            {"id": "mncs:test-case:later", "verdict": "FAIL"},
        ],
        "provenance": {"runner": {"source_sha256": source_digest}},
        "run_id": "run-native-17",
        "selection": {
            "selected_test_identities": ["mncs:test-case:execution"],
            "future": {"label": "opaque value 🛰️", "flags": [True, None]},
        },
        "test_inventory": {"inventory_identity": "b" * 64},
        "experiment": {"run_identity": "run-family-17"},
    }
    test_result_document = mncs_test.json.dumps(result, ensure_ascii=False).encode("utf-8")
    test_result_document = test_result_document.replace(
        b"mncs:test-case:execution",
        b"mncs:test-case:\\u0065xecution",
    )

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append(response)
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        result_digest,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        check_id="family:exact:check",
        claim="The exact repository-owned behavioral family check was executed by mncs-test",
        test_result_document=test_result_document,
    )

    digest = f"sha256:{result_digest}"
    uri = "urn:mncs-test:run-native-17"
    expected_summary = (
        f"unsupported: 12 passed, 2 failed, 3 skipped; failing_identity={long_identity}"
    )
    assert projected == {
        "schema_version": "mncs.check-result/1",
        "id": "family:exact:check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": "mncs-test.toml",
        "claim": "The exact repository-owned behavioral family check was executed by mncs-test",
        "summary": expected_summary,
        "contract_revision": "mncs.test-result/1",
        "producer_revision": f"sha256:{source_digest}",
        "digest": digest,
        "unresolved": ["the native test application is unavailable"],
        "references": [
            {"kind": "mncs-test-result", "uri": uri, "digest": digest}
        ],
        "result_ref": {
            "kind": "mncs-test-result",
            "uri": uri,
            "digest": digest,
            "detail": "retrieve the retained result artifact when per-test detail is required",
        },
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "selection": result["selection"],
    }
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True
    family_native = native_responses[0]["family_metadata"]
    assert family_native["supported"] is True
    assert family_native["test_result_schema_valid"] is True
    assert family_native["verdict"] == "UNKNOWN"
    assert family_native["selected_test_identities"] == ["mncs:test-case:execution"]
    assert family_native["inventory_identity_present"] is True
    assert family_native["inventory_identity"] == "b" * 64
    assert family_native["run_identity_present"] is True
    assert family_native["run_identity"] == "run-family-17"
    selection_bytes = test_result_document[
        family_native["selection_start"] :
        family_native["selection_start"] + family_native["selection_length"]
    ]
    assert b"\\u0065" in selection_bytes

    decoded_inputs: list[object] = []
    original_loads = mncs_test.json.loads

    def observe_loads(payload, *args, **kwargs):
        decoded_inputs.append(payload)
        return original_loads(payload, *args, **kwargs)

    monkeypatch.setattr(mncs_test.json, "loads", observe_loads)
    family = mncs_test._family_check_metadata_from_native(
        native_responses[0], test_result_document
    )
    assert family is not None
    assert family["selected_test_identities"] == ["mncs:test-case:execution"]
    assert family["selection"] == result["selection"]
    assert decoded_inputs == [selection_bytes]


def test_native_projection_uses_external_check_id_when_source_id_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "schema_version": "mncs.test-result/1",
        "provider": "mncs-test",
        "verdict": "PASS",
        "classification": "pass",
        "failure_class": "none",
        "summary": {},
        "tests": [],
    }
    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append(response)
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "8" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        check_id="family:external-id",
        test_result_document=mncs_test.json.dumps(result, separators=(",", ":")).encode(),
    )

    assert projected["id"] == "family:external-id"
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True


def test_native_projection_rejects_duplicate_unrecognized_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "schema_version": "mncs.test-result/1",
        "provider": "mncs-test",
        "verdict": "PASS",
        "classification": "pass",
        "failure_class": "none",
        "summary": {},
        "tests": [],
    }
    document = (
        b'{"schema_version":"mncs.test-result/1","provider":"mncs-test",'
        b'"verdict":"PASS","classification":"pass","failure_class":"none",'
        b'"summary":{},"tests":[],"extra":1,"\\u0065xtra":2}'
    )
    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append(response)
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    mncs_test.check_result(
        result,
        "7" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        check_id="family:duplicate-key",
        test_result_document=document,
    )

    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is False


def test_native_source_projection_decodes_escaped_values_and_utf8_failure_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = 'mncs:test-case:quoted-"-duck-🦆-é'
    message_prefix = ('line one\nquoted "failure" 🦆 café ' * 10) + "end"
    message = message_prefix + "x" * (1024 - len(message_prefix.encode("utf-8")))
    assert len(message.encode("utf-8")) == 1024
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": {"manifest": "mncs-test.toml"},
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 12, "failed": 2, "skipped": 3},
        "tests": [{"id": identity, "verdict": "FAIL"}],
        "provenance": {"runner": {"source_sha256": "f" * 64}},
        "run_id": "run-native-escaped-19",
        "selection": {"selected_test_identities": [identity]},
    }

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append(response)
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "9" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=mncs_test.json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
    )

    assert projected["summary"] == (
        f"unsupported: 12 passed, 2 failed, 3 skipped; failing_identity={identity}"
    )
    assert projected["unresolved"] == [message]
    assert projected["producer_revision"] == f"sha256:{'f' * 64}"
    assert projected["selection"] == result["selection"]
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True


def test_native_message_decoder_carries_surrogate_escape_across_block_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = ("x" * 1020) + "🦆"
    assert len(message.encode("utf-8")) == 1024
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-surrogate-boundary",
    }
    document = mncs_test.json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    assert "\\ud83e\\udd86" in document

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append({
                "supported": response.get("supported"),
                "chunks": list(response.get("projection", {}).get("unresolved_chunks", [])),
            })
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=document.encode("utf-8"),
    )

    assert projected["unresolved"] == [message]
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True
    assert "".join(native_responses[0]["chunks"]) == message


def test_native_check_result_projection_preserves_defaults_and_verdict_policy(
    tmp_path: Path,
) -> None:
    result_digest = "b" * 64
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "summary": {},
        "tests": [{"id": "not-a-failure", "verdict": "PASS"}],
        "run_id": "",
    }

    projected = mncs_test.check_result(
        result,
        result_digest,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    digest = f"sha256:{result_digest}"
    uri = "urn:mncs-test:"
    assert projected == {
        "schema_version": "mncs.check-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "scope": "mncs-test",
        "claim": "MNCS-native tests were executed under the compiler-owned inventory or declared compatibility fixture",
        "summary": "unsupported: 0 passed, 0 failed, 0 skipped",
        "contract_revision": "mncs.test-result/1",
        "producer_revision": "sha256:",
        "digest": digest,
        "unresolved": ["unsupported capability"],
        "references": [
            {"kind": "mncs-test-result", "uri": uri, "digest": digest}
        ],
        "result_ref": {
            "kind": "mncs-test-result",
            "uri": uri,
            "digest": digest,
            "detail": "retrieve the retained result artifact when per-test detail is required",
        },
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "selection": None,
    }


def test_native_check_result_projection_reassembles_long_utf8_failure_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = ("failure-🦆é" * 438) + ("x" * 12)
    assert len(message.encode("utf-8")) == 6144
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-utf8",
    }

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append({
                "supported": response.get("supported"),
                "projection": {
                    "unresolved_chunks": list(
                        response.get("projection", {}).get("unresolved_chunks", [])
                    )
                },
            })
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=mncs_test.json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
    )

    assert projected["unresolved"] == [message]
    assert projected["result_ref"]["uri"] == "urn:mncs-test:run-utf8"
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True
    chunks = native_responses[0]["projection"]["unresolved_chunks"]
    assert len(chunks) > 1
    assert "".join(chunks) == message
    assert all(0 < len(chunk.encode("utf-8")) <= 1024 for chunk in chunks)


def test_native_check_result_projection_streams_failure_message_at_native_span_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pattern = "failure-🦆é"
    message_bytes = 10240
    message = pattern * (message_bytes // len(pattern.encode("utf-8")))
    message += "x" * (message_bytes - len(message.encode("utf-8")))
    assert len(message.encode("utf-8")) == message_bytes
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-message-span-bound",
    }
    document = mncs_test.json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(document) < 65536

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append({
                "supported": response.get("supported"),
                "chunks": list(response.get("projection", {}).get("unresolved_chunks", [])),
            })
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=document,
    )

    assert projected["unresolved"] == [message]
    assert len(native_responses) == 1
    assert native_responses[0]["supported"] is True
    chunks = native_responses[0]["chunks"]
    assert len(chunks) > 8
    assert "".join(chunks) == message
    assert all(0 < len(chunk.encode("utf-8")) <= 1024 for chunk in chunks)


def test_native_check_result_projection_falls_back_above_native_message_span_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "x" * 10241
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-message-over-bound",
    }
    document = mncs_test.json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(document) <= mncs_test.CHECK_RESULT_JSON_NATIVE_MAX_DOCUMENT_BYTES

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append({"supported": response.get("supported")})
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=document,
    )

    assert projected["unresolved"] == [message]
    assert native_responses == [{"supported": False}]


def test_native_check_result_projection_uses_native_execution_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": "native bounded message"},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-document-bound",
        "padding": "",
    }
    base_document = mncs_test.json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    padding_bytes = mncs_test.CHECK_RESULT_JSON_NATIVE_MAX_DOCUMENT_BYTES - len(base_document)
    pattern = "failure-🦆é"
    pattern_bytes = len(pattern.encode("utf-8"))
    padding = pattern * (padding_bytes // pattern_bytes)
    padding += "x" * (padding_bytes - len(padding.encode("utf-8")))
    result["padding"] = padding
    document = mncs_test.json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(document) == mncs_test.CHECK_RESULT_JSON_NATIVE_MAX_DOCUMENT_BYTES
    assert len(mncs_test._public_result_input_chunks(document)) <= 64

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append({"supported": response.get("supported")})
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=document,
    )

    assert projected["unresolved"] == ["native bounded message"]
    assert native_responses == [{"supported": True}]


def test_native_check_result_projection_preserves_full_document_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = ("failure-🦆é" * 5000) + "end"
    result = {
        "schema_version": "mncs.test-result/1",
        "id": "runner-check",
        "provider": "mncs-test",
        "verdict": "UNKNOWN",
        "classification": "unsupported",
        "failure_class": "unsupported_capability",
        "failure": {"message": message},
        "summary": {"passed": 0, "failed": 0, "skipped": 0},
        "tests": [],
        "run_id": "run-utf8-over-bound",
    }

    document = mncs_test.json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(document) > 65536

    native_responses: list[dict] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        response = original_run_native_app(name, request, **kwargs)
        if name == "test-check-result-json-native.json":
            native_responses.append(response)
        return response

    monkeypatch.setattr(mncs_test, "run_native_app", observe_native_app)
    projected = mncs_test.check_result(
        result,
        "c" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        test_result_document=document,
    )

    assert projected["unresolved"] == [message]
    assert native_responses == []


def test_native_check_result_projection_uses_string_scope_for_minimal_result(
    tmp_path: Path,
) -> None:
    result = mncs_test.minimal_result(
        classification="unsupported",
        message="native capability is unavailable",
        cwd=tmp_path,
    )

    projected = mncs_test.check_result(
        result,
        "e" * 64,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )

    assert projected["scope"] == "mncs-test"
