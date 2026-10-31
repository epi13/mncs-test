from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
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
    assert applications[-1] == "test-public-result-json-token-tape.json"
    assert applications[-2] == "test-public-result-json-source-digest.json"
    assert applications[0] == "test-public-result-json-native-token-tape.json"
    assert set(applications[:-1]) == {
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-source-digest.json",
    }


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
    first_chunk = document["chunks"][0]["bytes"]
    replacement = "X" if first_chunk[0] != "X" else "Y"
    document["chunks"][0]["bytes"] = replacement + first_chunk[1:]
    window_request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_WINDOW_REQUEST_SCHEMA,
        "document": document,
        "input_sha256": start_request["input_sha256"],
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


def test_native_raw_tokenizer_rejects_changed_source_between_windows(
    tmp_path: Path,
) -> None:
    prepared = mncs_test._native_public_result_document_request(
        b"[" + b",".join([b'"' + b"x" * 40 + b'"' for _ in range(100)]) + b"]"
    )
    assert prepared is not None
    start_request, _source_length = prepared
    scan_state, cursor_state = mncs_test._initial_native_json_tokenizer_state()
    request = {
        "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_REQUEST_SCHEMA,
        "document": start_request["document"],
        "input_sha256": start_request["input_sha256"],
        "chunk_index": 0,
        "scan_state": scan_state,
        "cursor_state": cursor_state,
        "tokens": [],
        "token_count": 0,
    }
    first = mncs_test.run_native_app(
        "test-public-result-json-native-token-tape.json",
        request,
        request_schema=mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_REQUEST_SCHEMA,
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
        "test-public-result-json-native-token-tape.json",
        {
            "schema_version": mncs_test.PUBLIC_RESULT_JSON_NATIVE_TAPE_REQUEST_SCHEMA,
            "document": document,
            "input_sha256": start_request["input_sha256"],
            "chunk_index": first["next_chunk_index"],
            "scan_state": first["scan_state"],
            "cursor_state": first["cursor_state"],
            "tokens": first["tokens"],
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
    assert applications[-3:] == [
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-source-digest.json",
        "test-public-result-json-token-tape.json",
    ]


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
    assert applications[-3:] == [
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-source-digest.json",
        "test-public-result-json-token-tape.json",
    ]


def test_ascii_json_mode_keeps_escaped_unicode_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {"message": "Lúna 東京"}
    with mock.patch.object(mncs_test, "run_native_app") as native_app:
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
    native_app.assert_not_called()


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


def test_native_key_order_sorts_a_33_kibibyte_object_with_one_host_transport_pass(
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
    assert native_results[0][0] == "test-public-result-json-native-token-tape.json"
    assert native_results[0][1]["supported"] is True
    calls = [call.args[0] for call in native_app.call_args_list]
    _assert_native_raw_token_route(calls)
    compact.assert_called_once_with(value, sort_keys=False)


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
    assert 2 < len(calls) <= 13
    assert calls[0] == "test-public-result-json-native-token-tape.json"
    assert set(calls[1:]) == {
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-source-digest.json",
        "test-public-result-json-token-tape.json",
        "test-public-result-json-token.json",
        "test-public-result-json-token-window.json",
    }


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
    compact.assert_called_once_with(value, sort_keys=False)


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
        _large_result_splitting_utf8_at_native_chunk(),
    ]
    expected_applications = [
        "test-public-result-json-native-token-tape.json",
        "test-public-result-json-key-order.json",
        "test-public-result-json-token.json",
        "test-public-result-json-native-token-tape.json",
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
    assert all(
        call.get("document_bytes", 0) > 0 or call.get("tape_bytes", 0) > 0
        for call in public_calls
    )
    assert any(
        max(call.get("document_bytes", 0), call.get("tape_bytes", 0)) > 4096
        for call in public_calls
    )
    assert all(not call.get("has_typed_tree") for call in public_calls)
    assert compatibility_documents == []

