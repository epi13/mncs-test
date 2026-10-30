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


def test_native_request_keeps_byte_fields_as_packed_utf8_strings() -> None:
    prepared = mncs_test._native_public_result_request(
        {"κλειδί": 'Lúna 東京\n"'}
    )
    assert prepared is not None
    request, _output_bound = prepared

    assert request["nodes"][0]["key"] == ""
    assert request["nodes"][1]["key"] == "κλειδί"
    assert request["nodes"][1]["text"] == 'Lúna 東京\n"'
    assert isinstance(request["nodes"][1]["key"], str)
    assert isinstance(request["nodes"][1]["text"], str)


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
    assert native_app.call_count == 1
    assert native_app.call_args.args[0] == "test-public-result-json.json"


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
    assert native_app.call_count == 1


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
    window_calls = [name for name in calls if name == "test-public-result-json-window.json"]
    assert 1 < len(window_calls) <= 32
    assert calls == window_calls


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
    assert 1 < len(calls) <= 32
    assert set(calls) == {"test-public-result-json-window.json"}


def test_packed_text_keeps_large_public_result_on_native_windows(
    tmp_path: Path,
) -> None:
    value = ["x" * 600 for _ in range(55)]
    expected = mncs_test.json_bytes(value)
    assert len(expected) > 32_684
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
    assert 1 < len(calls) <= 32
    assert set(calls) == {"test-public-result-json-window.json"}


def test_results_above_expanded_native_window_keep_compatibility_json(
    tmp_path: Path,
) -> None:
    value = ["x" * 600 for _ in range(110)]
    expected = mncs_test.json_bytes(value)
    assert len(expected) > mncs_test.PUBLIC_RESULT_JSON_MAX_OUTPUT_BYTES

    with mock.patch.object(mncs_test, "run_native_app") as native_app:
        actual, digest = mncs_test._public_result_json_payload(
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    native_app.assert_not_called()


def test_out_of_bound_result_keeps_compatibility_json_without_native_call(
    tmp_path: Path,
) -> None:
    value = {"text": "outside-native-string-bound-" * 40}
    path = tmp_path / "compatibility.json"
    expected = mncs_test.json_bytes(value)
    with mock.patch.object(mncs_test, "run_native_app") as native_app:
        digest = mncs_test.write_output(
            path,
            value,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert path.read_bytes() == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    native_app.assert_not_called()


def test_native_budget_exhaustion_is_classified_separately(tmp_path: Path) -> None:
    value = {"a": [True, False, None]}
    prepared = mncs_test._native_public_result_request(value)
    assert prepared is not None
    request, _output_bound = prepared

    with pytest.raises(NativeBudgetExhaustedError, match="exhausted its execution budget"):
        mncs_test.run_native_app(
            "test-public-result-json.json",
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
        "test-public-result-json.json",
        "test-public-result-json-window.json",
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
        nodes = request.get("nodes")
        if isinstance(nodes, list):
            call["node_count"] = len(nodes)
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
    capsys.readouterr()

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
    assert public_calls
    assert all(call["status"] == "success" for call in public_calls)
    assert any(call.get("node_count", 0) >= 128 for call in public_calls)
    assert compatibility_documents == []

