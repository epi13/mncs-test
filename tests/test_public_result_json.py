from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

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
from mncs_test_native import NativeBudgetExhaustedError  # noqa: E402


def _native_environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        (str(stdlib), str(ROOT / "native"))
    )
    return environment


def _assert_native_raw_token_route(applications: list[str]) -> None:
    assert applications
    assert "test-public-result-json-value-input.json" not in applications
    if "test-public-result-json-native-token-tape.json" in applications:
        assert "test-public-result-json-token-tape.json" in applications or (
            "test-public-result-json-token.json" in applications
        )
        if "test-public-result-json-token-tape.json" in applications:
            assert "test-public-result-json-source-digest.json" in applications
            assert applications[-1] == "test-public-result-json-source-digest.json"


def _assert_native_document_route(
    observed: list[tuple[str, dict[str, object]]] | list[str],
) -> None:
    assert observed
    applications = [
        item if isinstance(item, str) else item[0]
        for item in observed
    ]
    assert "test-public-result-json-token.json" in applications
    assert not {
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-native-token-tape-window.json",
        "test-public-result-json-token-tape.json",
    }.intersection(applications)
    assert "test-public-result-json-value-input.json" not in applications


def _python_result_bytes(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sample_result() -> dict[str, object]:
    return {
        "Ω": "unicode key order",
        "z": {
            "unicode": "Lúna 東京",
            "controls": '\b\t\n\f\r\x00\x01\\"',
        },
        "a": [None, True, False, -17, 3.14159, -0.0, 1e20],
        "timing": {"wall_time_ms": 12.375},
        "": {"empty_array": [], "empty_object": {}},
    }


def test_native_request_transports_one_compact_json_document() -> None:
    prepared = mncs_test._native_public_result_request(
        {"κλειδί": 'Lúna 東京\n"'}
    )
    assert prepared is not None
    request, source_length = prepared

    compact = mncs_test.compact_json({"κλειδί": 'Lúna 東京\n"'})
    assert "".join(chunk["bytes"] for chunk in request["document"]["chunks"]) == compact
    assert source_length == request["document"]["byte_length"]
    assert request["input_sha256"] == hashlib.sha256(compact.encode("utf-8")).hexdigest()
    assert "nodes" not in request
    assert "edges" not in request


def test_native_public_result_retires_python_typed_value_projection(
    tmp_path: Path,
) -> None:
    value = {
        f"field-{index:02d}": index
        for index in reversed(range(40))
    }
    value["κ\n\"key"] = [
        "Lúna 東京\n\x00\\\"",
        None,
        True,
        False,
        -(2**64 - 1),
        2**80,
        ("tuple", {"z": 0, "a": 1}),
    ]
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
    ):
        actual, digest = mncs_test._native_public_result_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert observed[0][0] == "test-public-result-json-key-order.json"
    assert observed[0][1]["supported"] is True
    _assert_native_document_route(observed)
    assert not hasattr(mncs_test, "_native_public_result_value_request")
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_native_public_result_token_route_emits_finite_floats_differentially(
    tmp_path: Path,
) -> None:
    value = {
        "whole": 1.0,
        "negative_zero": -0.0,
        "fractional": [12.375, -0.00012],
        "exponents": {
            "large": 1e20,
            "small": 1e-7,
            "subnormal": 5e-324,
            "max_finite": 1.7976931348623157e308,
        },
    }
    expected = _python_result_bytes(value)
    observed: list[str] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append(name)
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
    ):
        actual, digest = mncs_test._native_public_result_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert observed[0] == "test-public-result-json-key-order.json"
    _assert_native_document_route(observed)
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_native_public_result_token_route_accepts_large_python_integers(
    tmp_path: Path,
) -> None:
    value = {"large": 2**80}
    expected = _python_result_bytes(value)

    actual, digest = mncs_test._native_public_result_payload(
        value,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()


def test_native_raw_tokenizer_reuses_transport_source_digest(tmp_path: Path) -> None:
    source = b'{"b":[2,1],"a":"native"}'
    digest = mock.Mock(wraps=hashlib.sha256)
    host_hashlib = mock.Mock(sha256=digest)
    with mock.patch.object(mncs_test, "hashlib", host_hashlib):
        actual = mncs_test._native_public_result_token_tape(
            source,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    expected = mncs_test.json_bytes(json.loads(source))
    assert actual == (expected, hashlib.sha256(expected).hexdigest())
    assert digest.call_count == 1


def test_native_token_formatter_rejects_out_of_range_packed_token_word(
    tmp_path: Path,
) -> None:
    result = mncs_test.run_native_app(
        "test-public-result-json-token-tape.json",
        {
            "schema_version": mncs_test.PUBLIC_RESULT_JSON_TOKEN_TAPE_REQUEST_SCHEMA,
            "input_sha256": "0" * 64,
            "tape": {
                "token_words": [2**53],
                "chunks": [{"bytes": "1", "length": 1}],
                "byte_length": 1,
            },
        },
        request_schema=mncs_test.PUBLIC_RESULT_JSON_TOKEN_TAPE_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_TOKEN_TAPE_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )

    assert result["supported"] is False
    assert result["request_valid"] is False
    assert result["tape_shape_valid"] is False
    assert result["tape_shape_code"] == 9


def test_native_key_order_reuses_transport_source_digest(tmp_path: Path) -> None:
    source = b'{"b":2,"a":1}'
    digest = mock.Mock(wraps=hashlib.sha256)
    host_hashlib = mock.Mock(sha256=digest)
    with mock.patch.object(mncs_test, "hashlib", host_hashlib):
        actual = mncs_test._native_public_result_key_order(
            source,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == b'{"a":1,"b":2}'
    assert digest.call_count == 1


@pytest.mark.parametrize(
    ("source", "digest_matches"),
    [(b'{"a":1}', False), (b'{"a":', True)],
    ids=["wrong-digest", "malformed-json"],
)
def test_native_key_order_rejects_unverified_documents(
    source: bytes, digest_matches: bool, tmp_path: Path,
) -> None:
    packed = mncs_test._native_public_result_document_request(source)
    assert packed is not None
    start_request, _source_length = packed
    request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_KEY_ORDER_REQUEST_SCHEMA,
        "document": start_request["document"],
        "input_sha256": start_request["input_sha256"] if digest_matches else "0" * 64,
    }

    result = mncs_test.run_native_app(
        "test-public-result-json-key-order.json",
        request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_KEY_ORDER_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_KEY_ORDER_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )

    assert result["supported"] is False
    assert result["document_length"] == 0
    assert result["chunk_count"] == 0


def test_native_document_serializer_reuses_transport_source_digest(
    tmp_path: Path,
) -> None:
    source = b'{"value":"native"}'
    digest = mock.Mock(wraps=hashlib.sha256)
    host_hashlib = mock.Mock(sha256=digest)
    with mock.patch.object(mncs_test, "hashlib", host_hashlib):
        actual = mncs_test._native_public_result_document_payload(
            source,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    expected = _python_result_bytes(json.loads(source))
    assert actual == (expected, hashlib.sha256(expected).hexdigest())
    assert digest.call_count == 2
    assert digest.call_args_list[0].args[0] == source
    assert digest.call_args_list[1].args[0] == expected


def test_native_continuation_rejects_a_changed_source_document(tmp_path: Path) -> None:
    prepared = mncs_test._native_public_result_request(
        {"payload": ["x" * 500 for _ in range(20)]}
    )
    assert prepared is not None
    start_request, _source_length = prepared
    first = mncs_test.run_native_app(
        "test-public-result-json-token.json",
        start_request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )
    assert first["supported"] is True
    assert first["done"] is False

    document = json.loads(json.dumps(start_request["document"]))
    input_chunk_index = first["input_chunk_index"]
    input_chunk_offset = first["input_chunk_offset"]
    current_chunk = document["chunks"][input_chunk_index]["bytes"]
    replacement = "X" if current_chunk[input_chunk_offset] != "X" else "Y"
    document["chunks"][input_chunk_index]["bytes"] = (
        current_chunk[:input_chunk_offset]
        + replacement
        + current_chunk[input_chunk_offset + 1:]
    )
    window_request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA,
        "document": document,
        "input_sha256": start_request["input_sha256"],
        "source_hash": first["source_hash"],
        "output_window_bytes": start_request["output_window_bytes"],
        "scan_state": first["scan_state"],
        "input_offset": first["input_offset"],
        "input_chunk_index": first["input_chunk_index"],
        "input_chunk_offset": first["input_chunk_offset"],
        "window_start": first["next_offset"],
        "cursor_state": first["cursor_state"],
        "pending_open": first["pending_open"],
        "value_count": first["value_count"],
        "child_counts": first["child_counts"],
        "key_counts": first["key_counts"],
        "hash_state": first["hash_state"],
        "hash_buffer": first["hash_buffer"],
        "hash_buffered": first["hash_buffered"],
    }
    continued = first
    for _ in range(
        mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES
        // mncs_test.PUBLIC_RESULT_JSON_WINDOW_BYTES + 2
    ):
        if not continued["supported"] or continued["done"]:
            break
        window_request.update({
            "scan_state": continued["scan_state"],
            "input_offset": continued["input_offset"],
            "input_chunk_index": continued["input_chunk_index"],
            "input_chunk_offset": continued["input_chunk_offset"],
            "window_start": continued["next_offset"],
            "source_hash": continued["source_hash"],
            "cursor_state": continued["cursor_state"],
            "pending_open": continued["pending_open"],
            "value_count": continued["value_count"],
            "child_counts": continued["child_counts"],
            "key_counts": continued["key_counts"],
            "hash_state": continued["hash_state"],
            "hash_buffer": continued["hash_buffer"],
            "hash_buffered": continued["hash_buffered"],
        })
        continued = mncs_test.run_native_app(
            "test-public-result-json-token-window.json",
            window_request,
            request_schema=mncs_test.PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA,
            result_schema=mncs_test.PUBLIC_RESULT_JSON_RESULT_SCHEMA,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
            step_budget=8_000_000,
        )
    assert continued["supported"] is False
    assert continued["input_offset"] == start_request["document"]["byte_length"]


def test_native_raw_tokenizer_rejects_changed_source_between_windows(
    tmp_path: Path,
) -> None:
    prepared = mncs_test._native_public_result_document_request(
        b"[" + b",".join([b'"' + b"x" * 40 + b'"' for _ in range(100)]) + b"]"
    )
    assert prepared is not None
    start_request, _source_length = prepared
    request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        "document": start_request["document"],
        "input_sha256": start_request["input_sha256"],
    }
    assert set(request) == {"schema_version", "document", "input_sha256"}
    first = mncs_test.run_native_app(
        "test-public-result-json-native-token-tape.json",
        request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )
    assert first["supported"] is True
    assert first["complete"] is False

    document = json.loads(json.dumps(start_request["document"]))
    first_chunk = document["chunks"][0]["bytes"]
    replacement = "y" if first_chunk[10] != "y" else "z"
    document["chunks"][0]["bytes"] = first_chunk[:10] + replacement + first_chunk[11:]
    continued = mncs_test.run_native_app(
        "test-public-result-json-native-token-tape-window.json",
        {
            "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_REQUEST_SCHEMA,
            "document": document,
            "input_sha256": start_request["input_sha256"],
            "chunk_index": first["next_chunk_index"],
            "scan_state": first["scan_state"],
            "cursor_state": first["cursor_state"],
            "token_words": first["token_words"],
            "token_count": first["token_count"],
        },
        request_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )
    assert continued["supported"] is True, continued
    assert continued["request_valid"] is True
    assert continued["scan_valid"] is True
    assert continued["cursor_valid"] is True
    assert continued["complete"] is True
    assert mncs_test._native_public_result_source_digest(
        start_request,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    ) is True
    assert mncs_test._native_public_result_source_digest(
        {"document": document, "input_sha256": start_request["input_sha256"]},
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    ) is False


def test_write_output_uses_native_json_and_matches_python_golden(tmp_path: Path) -> None:
    value = _sample_result()
    path = tmp_path / "artifacts" / "result.json"
    expected = _python_result_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        digest = mncs_test.write_output(
            path,
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert path.read_bytes() == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    applications = [call.args[0] for call in native_app.call_args_list]
    _assert_native_document_route(applications)


def test_print_result_uses_preformatted_json_without_host_reserialization(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = b'{"native":"bytes"}\n'
    with mock.patch.object(
        mncs_test.json,
        "dumps",
        side_effect=AssertionError("JSON output must reuse the serialized bytes"),
    ):
        mncs_test.print_result(
            {"native": "tree"},
            "json",
            serialized_json=payload,
        )

    assert capsys.readouterr().out.encode("utf-8") == payload


def test_family_check_error_response_uses_native_json_serializer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = mncs_test.argparse.Namespace(
        request=str(tmp_path / "missing-request.json"),
        checks=str(tmp_path / "unused-checks.json"),
        repository_id="mncs-test",
        mncs=str(MNCS),
        library=[],
        embed_library=None,
        timeout_seconds=10,
    )
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        exit_code = mncs_test.run_family_check(args)

    output = capsys.readouterr().out
    document = json.loads(output)
    assert exit_code == mncs_test.EXIT_INFRASTRUCTURE_FAILURE
    assert document["schema_version"] == mncs_test.FAMILY_CHECK_RESPONSE_SCHEMA
    assert document["verdict"] == "UNKNOWN"
    assert document["failure"]["class"] == "infrastructure_failure"
    assert output.encode("utf-8") == (
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_discover_json_uses_native_public_result_serializer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "mncs-test.toml"
    manifest_path.write_text("", encoding="utf-8")
    manifest = {
        "name": "sample-suite",
        "source_path": tmp_path / "sample.mncs",
        "module": "sample",
        "suite": None,
        "tests": [],
        "library_paths": [],
    }

    with mock.patch.dict(os.environ, _native_environment()), mock.patch.object(
        mncs_test, "load_manifest", return_value=manifest
    ), mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        exit_code = mncs_test.main(
            ["discover", "--root", str(tmp_path), "--mncs", str(MNCS), "--format", "json"]
        )

    output = capsys.readouterr().out.encode("utf-8")
    document = json.loads(output)
    assert exit_code == mncs_test.EXIT_SUCCESS
    assert output == _python_result_bytes(document)
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_validate_manifest_json_uses_native_serializer_for_ascii_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch.dict(os.environ, _native_environment()), mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        exit_code = mncs_test.main(
            [
                "validate-manifest",
                "--manifest",
                str(ROOT / "mncs-test.toml"),
                "--mncs",
                str(MNCS),
            ]
        )

    output = capsys.readouterr().out.encode("utf-8")
    document = json.loads(output)
    assert exit_code == mncs_test.EXIT_SUCCESS
    assert output == _python_result_bytes(document)
    applications = [call.args[0] for call in native_app.call_args_list]
    _assert_native_raw_token_route(applications)


def test_ascii_json_mode_keeps_escaped_unicode_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {
        "message": "Lúna 東京 😀",
        "🗝": "escaped=\\u03bb; control=\n",
        "z": {"a": "é", "b": "\U0001f9ed"},
    }
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        mncs_test.print_cli_json(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
            ensure_ascii=True,
        )

    output = capsys.readouterr().out.encode("utf-8")
    expected = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert output == expected
    assert output.isascii()
    assert "test-public-result-json-ascii-escape.json" in [
        call.args[0] for call in native_app.call_args_list
    ]


def test_ascii_json_native_escaper_handles_utf8_at_input_chunk_boundary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_document = _python_result_bytes({"text": ""})
    string_start = empty_document.index(b'""') + 1
    value = {"text": "a" * (1023 - string_start) + "é😀"}
    source = _python_result_bytes(value)
    chunks = mncs_test._public_result_input_chunks(source)
    assert source.index("é".encode("utf-8")) == 1023
    assert chunks[0].endswith(b"a")
    assert chunks[1].startswith("é😀".encode("utf-8"))

    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        mncs_test.print_cli_json(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
            ensure_ascii=True,
        )

    output = capsys.readouterr().out.encode("utf-8")
    expected = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert output == expected
    assert output.isascii()
    assert native_app.call_args_list[-1].args[0] == "test-public-result-json-ascii-escape.json"


def test_ascii_json_mode_keeps_bounded_expansion_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {"text": "Ω" * 11000}
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        mncs_test.print_cli_json(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
            ensure_ascii=True,
        )

    output = capsys.readouterr().out.encode("utf-8")
    expected = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert output == expected
    assert output.isascii()
    applications = [call.args[0] for call in native_app.call_args_list]
    assert "test-public-result-json-value-input.json" not in applications
    assert "test-public-result-json-key-order.json" in applications
    assert applications[-1] == "test-public-result-json-ascii-escape.json"


def test_native_public_result_digest_accepts_byte_array_continuation(
    tmp_path: Path,
) -> None:
    payload = mncs_test.json_bytes({"text": "Ω" * 11000})
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        digest = mncs_test._native_public_result_output_digest(
            payload,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert digest == hashlib.sha256(payload).hexdigest()
    applications = [call.args[0] for call in native_app.call_args_list]
    assert applications.count("test-public-result-json-source-digest.json") >= 2


def test_cli_json_falls_back_when_native_formatter_is_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {"valid": True, "tests": ["alpha", "beta"]}
    with mock.patch.object(
        mncs_test,
        "run_native_app",
        side_effect=mncs_test.AdapterError("native JSON formatter unavailable"),
    ) as native_app:
        mncs_test.print_cli_json(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert capsys.readouterr().out.encode("utf-8") == _python_result_bytes(value)
    native_app.assert_called_once()


def test_artifact_store_json_uses_native_serializer_and_keeps_golden_digest(
    tmp_path: Path,
) -> None:
    value = {
        "schema_version": "mncs.test-inventory/1",
        "tests": [
            {"name": "compiler.case.alpha", "source": "alpha.mncs"},
            {"name": "compiler.case.beta", "source": "βeta.mncs"},
        ],
    }
    expected = _python_result_bytes(value)
    store = mncs_test.ArtifactStore(
        tmp_path / "artifacts",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        relative = store.write_json(
            "compiler/direct-test-inventory.json",
            value,
            "compiler-test-inventory",
        )

    artifact = tmp_path / "artifacts" / relative
    assert relative == "compiler/direct-test-inventory.json"
    assert artifact.read_bytes() == expected
    assert store.items == [
        {
            "path": relative,
            "kind": "compiler-test-inventory",
            "sha256": hashlib.sha256(expected).hexdigest(),
        }
    ]
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_native_key_order_matches_unicode_and_escaped_key_golden(tmp_path: Path) -> None:
    value = {
        "z": 0,
        "Ω": 1,
        "é": 2,
        "😀": 3,
        "\n": 4,
        "a": 5,
        '"': 6,
        "\\": 7,
        "nested": {"z": [3, 2], "a": {"Ω": 1, "A": 2}},
    }
    expected = _python_result_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_native_key_order_prefix_matches_utf8_and_zero_padding(
    tmp_path: Path,
) -> None:
    value = {
        "\x01\xff": 0,
        "\x02": 1,
        "\x01": 2,
        "\x00\x01": 3,
        "\x00": 4,
        "\x00\x00": 5,
        "": 6,
        "a\x00": 7,
        "a": 8,
        "abcdefgh\x01": 9,
        "abcdefgh": 10,
        "abcdefgh\x00": 11,
        "é": 12,
        "😀": 13,
    }
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    key_order = next(
        result for name, result in observed
        if name == "test-public-result-json-key-order.json"
    )
    assert key_order["supported"] is True
    _assert_native_raw_token_route([name for name, _result in observed])


@pytest.mark.parametrize(
    "item_count", [520, 1021], ids=["past-legacy-bound", "at-native-bound"]
)
def test_native_key_order_handles_values_past_the_legacy_token_bound(
    item_count: int,
    tmp_path: Path,
) -> None:
    value = {"z": list(range(item_count)), "a": True}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_native_key_order_sorts_object_descendants_in_large_arrays(
    tmp_path: Path,
) -> None:
    value = {"z": [{"z": 0, "a": 1}, *range(519)], "a": True}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_native_key_order_handles_exact_value_bound_with_object_in_array(
    tmp_path: Path,
) -> None:
    value = {"z": [{"z": 0}, *range(1019)], "a": True}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_native_public_result_key_order_handles_128_object_members(
    tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": [index] for index in range(128)}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)


def test_native_public_result_key_order_handles_129_members(
    tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": [index] for index in range(129)}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)


@pytest.mark.parametrize(
    "indices",
    [tuple(range(256)), tuple(reversed(range(256)))],
    ids=["ascending-input", "descending-input"],
)
def test_native_public_result_key_order_handles_256_object_members(
    indices: tuple[int, ...], tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": [index] for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_document_route(observed)


def test_native_public_result_key_order_handles_257_members(
    tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": [index] for index in range(257)}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    _assert_native_document_route(observed)
@pytest.mark.parametrize(
    "indices",
    [tuple(range(512)), tuple(reversed(range(512)))],
    ids=["ascending-input", "descending-input"],
)
def test_native_public_result_key_order_handles_512_scalar_members(
    indices: tuple[int, ...], tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": index for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    _assert_native_document_route(observed)


@pytest.mark.parametrize(
    "indices",
    [tuple(range(640)), tuple(reversed(range(640)))],
    ids=["ascending-input", "descending-input"],
)
def test_native_public_result_key_order_handles_640_scalar_members(
    indices: tuple[int, ...], tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": index for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    _assert_native_document_route(observed)


@pytest.mark.parametrize(
    "indices",
    [
        tuple(range(641)),
        tuple(reversed(range(641))),
        tuple(range(642)),
        tuple(reversed(range(642))),
    ],
    ids=[
        "641-ascending-input",
        "641-descending-input",
        "642-ascending-input",
        "642-descending-input",
    ],
)
def test_native_public_result_key_order_handles_641_and_642_scalar_members(
    indices: tuple[int, ...], tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": index for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    key_order = next(
        result for name, result in observed
        if name == "test-public-result-json-key-order.json"
    )
    assert key_order["supported"] is True
    _assert_native_document_route(observed)


@pytest.mark.parametrize(
    "count", [642], ids=["maximum-bound"]
)
def test_native_public_result_key_order_handles_unsorted_members_at_maximum_bound(
    count: int, tmp_path: Path,
) -> None:
    indices = tuple((index * 73) % count for index in range(count))
    value = {f"field-{index:03d}": index for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    budget_exhaustions: list[str] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        try:
            result = original_run_native_app(name, request, **kwargs)
        except NativeBudgetExhaustedError:
            budget_exhaustions.append(name)
            raise
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert budget_exhaustions == []
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    key_order = next(
        result for name, result in observed
        if name == "test-public-result-json-key-order.json"
    )
    assert key_order["supported"] is True
    _assert_native_document_route(observed)


@pytest.mark.parametrize(
    "count", [597, 598, 599, 600, 610, 611, 613],
    ids=[
        "previous-bound",
        "new-bound",
        "max-flat-sample",
        "first-indexed",
        "indexed-sample",
        "new-native-boundary",
        "max-indexed-sample",
    ],
)
def test_native_public_result_key_order_handles_unsorted_members_within_budget(
    count: int, tmp_path: Path,
) -> None:
    indices = tuple((index * 73) % count for index in range(count))
    value = {f"field-{index:03d}": index for index in indices}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    budget_exhaustions: list[str] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        try:
            result = original_run_native_app(name, request, **kwargs)
        except NativeBudgetExhaustedError:
            budget_exhaustions.append(name)
            raise
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert budget_exhaustions == []
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    key_order = next(
        result for name, result in observed
        if name == "test-public-result-json-key-order.json"
    )
    assert key_order["supported"] is True
    _assert_native_document_route(observed)


def test_native_public_result_key_order_merges_unsorted_256_members(
    tmp_path: Path,
) -> None:
    indices = tuple((index * 73) % 256 for index in range(256))
    value = {f"field-{index:03d}": index for index in indices}
    source = json.dumps(value, separators=(",", ":"), sort_keys=False).encode()
    expected = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    actual = mncs_test._native_public_result_key_order(
        source,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual == expected


def test_native_public_result_key_order_merges_unsorted_596_members(
    tmp_path: Path,
) -> None:
    indices = tuple((index * 73) % 596 for index in range(596))
    value = {f"field-{index:03d}": index for index in indices}
    source = json.dumps(value, separators=(",", ":"), sort_keys=False).encode()
    expected = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    actual = mncs_test._native_public_result_key_order(
        source,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual == expected


def test_native_public_result_key_order_handles_513_members(
    tmp_path: Path,
) -> None:
    value = {f"field-{index:03d}": index for index in range(513)}
    expected = _python_result_bytes(value)
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(
        name: str, request: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        result = original_run_native_app(name, request, **kwargs)
        observed.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ),
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]
    _assert_native_document_route(observed)


def test_native_public_result_merge_sort_rejects_duplicate_keys_after_128_members(
    tmp_path: Path,
) -> None:
    members = [(f"field-{index:03d}", str(index)) for index in range(129)]
    members.append(("field-127", "999"))
    source = b"{" + b",".join(
        json.dumps(key).encode("utf-8") + b":" + value.encode("ascii")
        for key, value in members
    ) + b"}"

    actual = mncs_test._native_public_result_key_order(
        source,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual is None


def test_native_key_order_compacts_whitespace_around_scalar_array(
    tmp_path: Path,
) -> None:
    source = b'{ "z" : [1, 2, 3], "a" : true }'
    actual = mncs_test._native_public_result_key_order(
        source,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual == b'{"a":true,"z":[1,2,3]}'


def test_native_document_route_handles_key_order_decline_from_encoded_document(
    tmp_path: Path,
) -> None:
    value = {"nested": {"z": [3, 2, 1], "a": {"Ω": 1, "A": 2}}, "float": 2.5}
    expected = _python_result_bytes(value)
    with (
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_INPUT_BYTES", 1),
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_VALUES", 1),
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    applications = [call.args[0] for call in native_app.call_args_list]
    assert applications[0] == "test-public-result-json-native-token-stream.json"
    assert "test-public-result-json-source-digest.json" in applications
    assert not {
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-native-token-tape-window.json",
        "test-public-result-json-token-tape.json",
        "test-public-result-json-token.json",
    }.intersection(applications)
    stream_request = native_app.call_args_list[0].args[1]
    assert "token_words" not in stream_request
    assert "tape" not in stream_request


def test_native_windowed_token_stream_handles_a_medium_document_without_host_tokens(
    tmp_path: Path,
) -> None:
    value = {
        f"field-{index:02d}": chr(65 + index) * 150
        for index in reversed(range(40))
    }
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    assert 4096 < len(source) <= mncs_test.PUBLIC_RESULT_JSON_NATIVE_TOKEN_STREAM_WINDOWED_MAX_BYTES
    expected = _python_result_bytes(value)

    with (
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_INPUT_BYTES", 1),
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_VALUES", 1),
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    applications = [call.args[0] for call in native_app.call_args_list]
    assert "test-public-result-json-native-token-stream-windowed.json" in applications
    assert "test-public-result-json-source-digest.json" in applications
    assert not {
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-native-token-tape-window.json",
        "test-public-result-json-token-tape.json",
        "test-public-result-json-token.json",
    }.intersection(applications)
    windowed_request = next(
        call.args[1]
        for call in native_app.call_args_list
        if call.args[0] == "test-public-result-json-native-token-stream-windowed.json"
    )
    assert set(windowed_request) == {"schema_version", "document", "input_sha256"}
    assert "token_words" not in windowed_request
    assert "tape" not in windowed_request


def test_native_windowed_token_stream_handles_extended_bound_without_host_tokens(
    tmp_path: Path,
) -> None:
    value = {
        f"field-{index:02d}": chr(65 + index % 26) * 154
        for index in reversed(range(46))
    }
    value["field-00"] = "x" * 145 + '"\\' + "東京🦆"
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    assert 7168 < len(source) <= 7800
    assert len(mncs_test._public_result_input_chunks(source)) == 8
    expected = _python_result_bytes(value)
    original_run_native_app = mncs_test.run_native_app
    windowed_result: dict[str, Any] = {}

    def record_windowed_result(application: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_run_native_app(application, *args, **kwargs)
        if application == "test-public-result-json-native-token-stream-windowed.json":
            windowed_result.update(result)
        return result

    with (
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_INPUT_BYTES", 1),
        mock.patch.object(mncs_test, "PUBLIC_RESULT_JSON_KEY_ORDER_MAX_VALUES", 1),
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=record_windowed_result
        ) as native_app,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert windowed_result["supported"] is True
    applications = [call.args[0] for call in native_app.call_args_list]
    assert applications[0] == "test-public-result-json-native-token-stream-windowed.json"
    windowed_request = native_app.call_args_list[0].args[1]
    assert set(windowed_request) == {"schema_version", "document", "input_sha256"}
    assert "token_words" not in windowed_request
    assert "tape" not in windowed_request


def test_native_windowed_token_stream_rejects_a_tampered_source_digest(
    tmp_path: Path,
) -> None:
    value = {
        f"field-{index:02d}": chr(65 + index) * 150
        for index in reversed(range(40))
    }
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    prepared = mncs_test._native_public_result_document_request(source)
    assert prepared is not None
    start_request, _source_length = prepared
    request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        "document": start_request["document"],
        "input_sha256": "0" * 64,
    }

    result = mncs_test.run_native_app(
        "test-public-result-json-native-token-stream-windowed.json",
        request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_TOKEN_TAPE_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )

    assert result["supported"] is False
    assert result["request_valid"] is False


@pytest.mark.parametrize("bad_escape", [b"\\q", b"\\uD800x"])
def test_native_windowed_token_stream_rejects_malformed_string_contents(
    bad_escape: bytes, tmp_path: Path,
) -> None:
    source = b'{"payload":"' + b"x" * 4_100 + bad_escape + b'"}'
    assert len(source) <= mncs_test.PUBLIC_RESULT_JSON_NATIVE_TOKEN_STREAM_WINDOWED_MAX_BYTES
    prepared = mncs_test._native_public_result_document_request(source)
    assert prepared is not None
    start_request, _source_length = prepared
    request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        "document": start_request["document"],
        "input_sha256": start_request["input_sha256"],
    }

    result = mncs_test.run_native_app(
        "test-public-result-json-native-token-stream-windowed.json",
        request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_START_REQUEST_SCHEMA,
        result_schema=mncs_test.PUBLIC_RESULT_JSON_TOKEN_TAPE_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
        step_budget=8_000_000,
    )

    assert result["request_valid"] is True
    assert result["supported"] is False


def test_native_key_order_sorts_documents_larger_than_legacy_bound(tmp_path: Path) -> None:
    value = {f"key-{index:02d}": chr(65 + index) * 600 for index in reversed(range(10))}
    expected = _python_result_bytes(value)
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    assert 4096 < len(source) <= mncs_test.PUBLIC_RESULT_JSON_KEY_ORDER_MAX_INPUT_BYTES
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_native_key_order_sorts_documents_larger_than_eight_kibibytes(tmp_path: Path) -> None:
    value = {f"field-{index:02d}": "v" * 800 for index in reversed(range(12))}
    expected = _python_result_bytes(value)
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    assert 8192 < len(source) <= mncs_test.PUBLIC_RESULT_JSON_KEY_ORDER_MAX_INPUT_BYTES
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    calls = [call.args[0] for call in native_app.call_args_list]
    _assert_native_raw_token_route(calls)


def test_native_packed_token_route_handles_a_33_kibibyte_object_after_key_order_declines(
    tmp_path: Path,
) -> None:
    value = {
        f"field-{index:02d}": chr(65 + index) * 800
        for index in reversed(range(40))
    }
    expected = _python_result_bytes(value)
    source = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    assert 32_000 < len(source) < 65_536

    native_results: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        result = original_run_native_app(name, request, **kwargs)
        native_results.append((name, result))
        return result

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", side_effect=observe_native_app
        ) as native_app,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    calls = [call.args[0] for call in native_app.call_args_list]
    assert calls[0] == "test-public-result-json-key-order.json"
    assert "test-public-result-json-native-token-stream-windowed.json" not in calls
    assert native_results[0][0] == "test-public-result-json-native-token-tape.json"
    _assert_native_raw_token_route(calls)
    assert "test-public-result-json-token-tape.json" in [
        name for name, _result in native_results
    ]
    requests = {
        call.args[0]: call.args[1]
        for call in native_app.call_args_list
    }
    continuation = requests["test-public-result-json-native-token-tape-window.json"]
    assert isinstance(continuation["token_words"], list)
    assert "tokens" not in continuation
    assert all(type(word) is int and 0 <= word < 2**53 for word in continuation["token_words"])
    tape_request = requests["test-public-result-json-token-tape.json"]
    tape = tape_request["tape"]
    assert isinstance(tape, dict)
    assert "tokens" not in tape
    assert isinstance(tape["token_words"], list)
    assert all(type(word) is int and 0 <= word < 2**53 for word in tape["token_words"])
    assert all(set(chunk) == {"bytes", "length"} for chunk in tape["chunks"])
    assert compact.call_args_list == [
        mock.call(value, sort_keys=False),
    ]


def test_non_string_mapping_keys_keep_python_sort_semantics(tmp_path: Path) -> None:
    value = {10: "ten", 2: "two"}
    expected = _python_result_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    assert [call.args[0] for call in native_app.call_args_list] == [
        "test-public-result-json-token.json",
    ]


def test_family_check_response_sends_nested_results_as_raw_native_tokens(
    tmp_path: Path,
) -> None:
    request = {
        "check_identity": "mncs-test:family-check",
        "contract_identity": "mncs.test-contract/1",
        "contract_revision": "0.18",
        "verification_plan_id": "a" * 64,
        "family_graph_identity": "b" * 64,
        "edge_fingerprint": "c" * 64,
        "source_change_sha256": "d" * 64,
    }
    result = {
        "schema_version": mncs_test.RESULT_SCHEMA,
        "verdict": "PASS",
        "nested": {"items": ["東京", 7, True]},
    }
    check_result = {
        "schema_version": mncs_test.CHECK_SCHEMA,
        "id": request["check_identity"],
        "verdict": "PASS",
    }
    result_document = mncs_test.json_bytes(result)
    check_document = mncs_test.json_bytes(check_result)
    expected = {
        "schema_version": mncs_test.FAMILY_CHECK_RESPONSE_SCHEMA,
        "check_identity": request["check_identity"],
        "contract_identity": request["contract_identity"],
        "contract_revision": request["contract_revision"],
        "runner": "mncs-test",
        "verdict": "PASS",
        "test_result": result,
        "check_result": check_result,
        "execution": {
            "run_identity": "run-1",
            "test_case_identities": ["case-a", "case-b"],
            "inventory_identity": "c" * 64,
            "runner_version": mncs_test.RUNNER_VERSION,
            "result_sha256": "d" * 64,
            "check_result_sha256": "e" * 64,
            "check_definition_identity": "f" * 64,
        },
        "family_binding": {
            "verification_plan_id": request["verification_plan_id"],
            "family_graph_identity": request["family_graph_identity"],
            "edge_fingerprint": request["edge_fingerprint"],
            "source_change_sha256": request["source_change_sha256"],
        },
    }
    observed: list[tuple[str, dict[str, object]]] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, native_request: dict[str, object], **kwargs: object):
        observed.append((name, native_request))
        return original_run_native_app(name, native_request, **kwargs)

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual = mncs_test._native_family_check_response_payload(
            request=request,
            result_document=result_document,
            check_result_document=check_document,
            verdict="PASS",
            run_identity="run-1",
            test_case_identities=["case-a", "case-b"],
            inventory_identity="c" * 64,
            runner_version=mncs_test.RUNNER_VERSION,
            result_sha256="d" * 64,
            check_result_sha256="e" * 64,
            check_definition_identity="f" * 64,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    expected_bytes = mncs_test.json_bytes(expected)
    assert actual == (expected_bytes, hashlib.sha256(expected_bytes).hexdigest())
    assert observed[0][0] == "test-public-result-json-token.json"
    source_request = observed[0][1]
    document = source_request["document"]
    source = "".join(chunk["bytes"] for chunk in document["chunks"]).encode("utf-8")
    assert result_document.rstrip() in source
    assert check_document.rstrip() in source


def test_family_check_response_with_256_id_envelope_uses_native_formatter(
    tmp_path: Path,
) -> None:
    request = {
        "check_identity": "mncs-test:family-check",
        "contract_identity": "mncs.test-contract/1",
        "contract_revision": "0.18",
        "verification_plan_id": "a" * 64,
        "family_graph_identity": "b" * 64,
        "edge_fingerprint": "c" * 64,
        "source_change_sha256": "d" * 64,
    }
    selected = [f"case-{index:03d}" for index in range(256)]
    result = {
        "schema_version": mncs_test.RESULT_SCHEMA,
        "verdict": "PASS",
        "test_case_identities": selected,
    }
    check_result = {
        "schema_version": mncs_test.CHECK_SCHEMA,
        "id": request["check_identity"],
        "selected_test_case_identities": selected,
        "verdict": "PASS",
    }
    observed: list[str] = []
    value_counts: list[int] = []
    window_limits: list[int] = []
    original_run_native_app = mncs_test.run_native_app

    def observe_native_app(name: str, native_request: dict[str, object], **kwargs: object):
        result = original_run_native_app(name, native_request, **kwargs)
        observed.append(name)
        value_counts.append(result.get("value_count", 0))
        if name in {
            "test-public-result-json-token.json",
            "test-public-result-json-token-window.json",
        }:
            window_limits.append(native_request["output_window_bytes"])
        return result

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ):
        actual = mncs_test._native_family_check_response_payload(
            request=request,
            result_document=mncs_test.json_bytes(result),
            check_result_document=mncs_test.json_bytes(check_result),
            verdict="PASS",
            run_identity="run-1",
            test_case_identities=selected,
            inventory_identity="e" * 64,
            runner_version=mncs_test.RUNNER_VERSION,
            result_sha256="f" * 64,
            check_result_sha256="0" * 64,
            check_definition_identity="1" * 64,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    expected = {
        "schema_version": mncs_test.FAMILY_CHECK_RESPONSE_SCHEMA,
        "check_identity": request["check_identity"],
        "contract_identity": request["contract_identity"],
        "contract_revision": request["contract_revision"],
        "runner": "mncs-test",
        "verdict": "PASS",
        "test_result": result,
        "check_result": check_result,
        "execution": {
            "run_identity": "run-1",
            "test_case_identities": selected,
            "inventory_identity": "e" * 64,
            "runner_version": mncs_test.RUNNER_VERSION,
            "result_sha256": "f" * 64,
            "check_result_sha256": "0" * 64,
            "check_definition_identity": "1" * 64,
        },
        "family_binding": {
            "verification_plan_id": request["verification_plan_id"],
            "family_graph_identity": request["family_graph_identity"],
            "edge_fingerprint": request["edge_fingerprint"],
            "source_change_sha256": request["source_change_sha256"],
        },
    }
    expected_bytes = mncs_test.json_bytes(expected)
    assert actual == (expected_bytes, hashlib.sha256(expected_bytes).hexdigest())
    assert observed[0] == "test-public-result-json-token.json"
    assert "test-public-result-json-native-token-tape.json" not in observed
    assert max(value_counts) > 512
    assert window_limits and set(window_limits) == {4096}


def test_native_json_chunks_preserve_utf8_across_boundaries(tmp_path: Path) -> None:
    value = {
        "payload": "x" * 1010 + "東京" + "y" * 4,
        "nested": {"z": [0, True, None, 2.25]},
    }
    expected = _python_result_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert len(expected) > 1024
    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def _large_result_splitting_utf8_at_native_chunk() -> list[str]:
    prefix = ["x" * 600]
    suffix = ["z" * 600 for _ in range(26)]
    for padding in range(1022):
        value = [*prefix, "a" * padding + "東" + "y" * 10, *suffix]
        if _python_result_bytes(value).index("東".encode("utf-8")) == 1_023:
            return value
    raise AssertionError("test fixture could not place a UTF-8 scalar across an output chunk boundary")


def test_native_json_supports_results_larger_than_legacy_window(tmp_path: Path) -> None:
    value = _large_result_splitting_utf8_at_native_chunk()
    expected = _python_result_bytes(value)
    assert len(expected) > 16_300
    assert len(expected) <= mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES
    assert expected.index("東".encode("utf-8")) == 1_023
    destination = tmp_path / "public-result.json"

    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        digest = mncs_test.write_output(
            destination,
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    actual = destination.read_bytes()
    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    calls = [call.args[0] for call in native_app.call_args_list]
    _assert_native_raw_token_route(calls)


def test_public_result_preflights_utf8_input_that_needs_a_65th_chunk(
    tmp_path: Path,
) -> None:
    value: dict[str, str] = {"padding": ""}
    base = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")
    remaining = mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES - len(base)
    pattern = "failure-🦆é"
    pattern_bytes = len(pattern.encode("utf-8"))
    value["padding"] = pattern * (remaining // pattern_bytes)
    value["padding"] += "x" * (remaining - len(value["padding"].encode("utf-8")))
    compact = mncs_test.compact_json(value, sort_keys=False).encode("utf-8")

    assert len(compact) == mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES
    assert len(mncs_test._public_result_input_chunks(compact)) == 65
    assert mncs_test._native_public_result_document_request(compact) is None

    expected = _python_result_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    native_app.assert_not_called()


def test_results_through_expanded_native_window_use_native_json(
    tmp_path: Path,
) -> None:
    value: object = ["x" * 1000 for _ in range(29)]
    for _ in range(50):
        value = [value]
    expected = mncs_test.json_bytes(value)
    assert len(expected) > mncs_test.PUBLIC_RESULT_JSON_V1_MAX_OUTPUT_BYTES
    assert len(expected) <= mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES

    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    calls = [call.args[0] for call in native_app.call_args_list]
    assert calls[0] == "test-public-result-json-token.json"
    assert set(calls[1:]) == {"test-public-result-json-token-window.json"}
    assert len(calls) >= (
        len(expected) + mncs_test.PUBLIC_RESULT_JSON_WINDOW_BYTES - 1
    ) // mncs_test.PUBLIC_RESULT_JSON_WINDOW_BYTES


def test_large_array_uses_native_json_without_python_typed_token_projection(
    tmp_path: Path,
) -> None:
    value = ["x" * 600 for _ in range(55)]
    expected = mncs_test.json_bytes(value)
    assert len(expected) > 32_684
    assert len(expected) <= mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES

    with (
        mock.patch.object(
            mncs_test, "compact_json", wraps=mncs_test.compact_json
        ) as compact,
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
    ):
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    calls = [call.args[0] for call in native_app.call_args_list]
    _assert_native_raw_token_route(calls)
    assert compact.call_args_list == [mock.call(value, sort_keys=False)]


def test_results_above_expanded_native_window_keep_compatibility_json(
    tmp_path: Path,
) -> None:
    value = ["x" * 600 for _ in range(110)]
    expected = mncs_test.json_bytes(value)
    assert len(expected) > mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES

    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    native_app.assert_not_called()


def test_native_json_parser_preserves_long_string_keys_and_values(
    tmp_path: Path,
) -> None:
    value = {"text": "outside-native-string-bound-" * 40}
    path = tmp_path / "compatibility.json"
    expected = mncs_test.json_bytes(value)
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        digest = mncs_test.write_output(
            path,
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert path.read_bytes() == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    _assert_native_raw_token_route([call.args[0] for call in native_app.call_args_list])


def test_native_budget_exhaustion_is_classified_separately(tmp_path: Path) -> None:
    value = {"a": [True, False, None]}
    prepared = mncs_test._native_public_result_request(value)
    assert prepared is not None
    request, _output_bound = prepared

    with pytest.raises(NativeBudgetExhaustedError, match="exhausted its execution budget"):
        mncs_test.run_native_app(
            "test-public-result-json-token.json",
            request,
            request_schema=mncs_test.PUBLIC_RESULT_JSON_REQUEST_SCHEMA,
            result_schema=mncs_test.PUBLIC_RESULT_JSON_RESULT_SCHEMA,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
            step_budget=1,
        )


def test_budget_exhaustion_preserves_compatibility_json_for_single_and_window_paths(
    tmp_path: Path,
) -> None:
    values = [
        {"small": [True, False, None]},
        {
            f"field-{index:02d}": chr(65 + index) * 150
            for index in reversed(range(40))
        },
        _large_result_splitting_utf8_at_native_chunk(),
    ]
    expected_applications = [
        "test-public-result-json-key-order.json",
        "test-public-result-json-native-token-stream.json",
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-token.json",
        "test-public-result-json-key-order.json",
        "test-public-result-json-native-token-stream-windowed.json",
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-token.json",
        "test-public-result-json-token.json",
    ]

    with mock.patch.object(
        mncs_test,
        "run_native_app",
        side_effect=NativeBudgetExhaustedError("native application exhausted its execution budget"),
    ) as native_app:
        for value in values:
            expected = mncs_test.json_bytes(value)
            actual, digest = mncs_test._public_result_json_payload(
                value,
                mncs=str(MNCS),
                cwd=tmp_path,
                environment=_native_environment(),
            )
            assert actual == expected
            assert digest == hashlib.sha256(expected).hexdigest()

    assert [call.args[0] for call in native_app.call_args_list] == expected_applications


def test_compile_failure_runner_keeps_medium_public_result_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not MNCS.is_file() or not os.access(MNCS, os.X_OK):
        pytest.skip("a built sibling mncs compiler is required")

    commons = WORKSPACE_ROOT / "MNCS-Commons"
    common_library = commons / "src/mncs_commons/mesh/mncs"
    environment = _native_environment()
    roots = environment["MNCS_LIBRARY_PATH"].split(os.pathsep)
    if common_library.is_dir():
        roots.append(str(common_library))
    monkeypatch.setenv("MNCS_LIBRARY_PATH", os.pathsep.join(dict.fromkeys(roots)))
    monkeypatch.setenv("MNCS_COMMONS_ROOT", str(commons))

    app_calls: list[dict[str, object]] = []
    compatibility_documents: list[str] = []
    original_run_native_app = mncs_test.run_native_app
    original_json_bytes = mncs_test.json_bytes

    def observe_native_app(name: str, request: dict, **kwargs: object) -> dict:
        call: dict[str, object] = {"application": name, "status": "running"}
        document = request.get("document")
        if isinstance(document, dict):
            call["document_bytes"] = document.get("byte_length", 0)
        tape = request.get("tape")
        if isinstance(tape, dict):
            call["tape_bytes"] = tape.get("byte_length", 0)
        text = request.get("text")
        if isinstance(text, dict):
            call["text_bytes"] = text.get("byte_length", 0)
        node_tape = request.get("node_tape")
        if isinstance(node_tape, list):
            call["node_tape_bytes"] = sum(
                item.get("length", 0)
                for item in node_tape
                if isinstance(item, dict)
            )
        call["input_bytes"] = sum(
            call.get(field, 0)
            for field in (
                "document_bytes",
                "tape_bytes",
                "text_bytes",
                "node_tape_bytes",
            )
            if isinstance(call.get(field, 0), int)
        )
        call["has_typed_tree"] = "nodes" in request or "edges" in request
        app_calls.append(call)
        try:
            result = original_run_native_app(name, request, **kwargs)
        except NativeBudgetExhaustedError:
            call["status"] = "budget_exhausted"
            raise
        except Exception:
            call["status"] = "failed"
            raise
        call["status"] = "success"
        return result

    def observe_compatibility_serializer(value: object) -> bytes:
        if isinstance(value, dict) and value.get("schema_version") in {
            mncs_test.RESULT_SCHEMA,
            mncs_test.CHECK_SCHEMA,
        }:
            compatibility_documents.append(str(value["schema_version"]))
        return original_json_bytes(value)

    result_path = tmp_path / "result.json"
    argv = [
        "run",
        "--manifest",
        str(ROOT / "tests/fixtures/compile-fail.toml"),
        "--mncs",
        str(MNCS),
        "--library",
        str(Path(environment["MNCS_LIBRARY_PATH"].split(os.pathsep)[0])),
        "--result",
        str(result_path),
        "--check-result",
        str(tmp_path / "check.json"),
        "--artifacts",
        str(tmp_path / "artifacts"),
        "--format",
        "json",
    ]
    embed_library = MNCS.parent / "libmncs_embed.so"
    if embed_library.is_file():
        argv.extend(["--embed-library", str(embed_library)])

    with mock.patch.object(
        mncs_test, "run_native_app", side_effect=observe_native_app
    ), mock.patch.object(
        mncs_test, "json_bytes", side_effect=observe_compatibility_serializer
    ):
        exit_code = mncs_test.main(argv)
    captured = capsys.readouterr()

    document = json.loads(result_path.read_text(encoding="utf-8"))
    public_calls = [
        call
        for call in app_calls
        if str(call["application"]).startswith("test-public-result-json")
    ]
    assert exit_code == 0
    assert document["verdict"] == "PASS"
    assert document["tests"][0]["status"] == "expected_failure"
    assert document["tests"][0]["diagnostics"]
    assert result_path.read_bytes() == _python_result_bytes(document)
    assert captured.out.encode("utf-8") == result_path.read_bytes()
    assert public_calls
    assert all(call["status"] == "success" for call in public_calls)
    assert all(call.get("input_bytes", 0) > 0 for call in public_calls)
    assert any(
        call.get("input_bytes", 0) > 4096
        for call in public_calls
    )
    assert all(not call.get("has_typed_tree") for call in public_calls)
    assert compatibility_documents == []

