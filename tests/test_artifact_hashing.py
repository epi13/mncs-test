from __future__ import annotations

import hashlib
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


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_artifact_store_hashes_bounded_payloads_with_native_sha256(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = mncs_test.ArtifactStore(
        artifact_root,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    written_payload = b'{"kind":"execution-output","value":17}'
    existing_payload = b"compiler-created" * 127
    existing_path = artifact_root / "compiler" / "artifact.json"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(existing_payload)

    with (
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_digest,
    ):
        written_path = store.write("stdout/result.json", written_payload, "stdout")
        existing_relative = store.record_existing(existing_path, "compiler-output")

    expected = [
        hashlib.sha256(written_payload).hexdigest(),
        hashlib.sha256(existing_payload).hexdigest(),
    ]
    assert written_path == "stdout/result.json"
    assert existing_relative == "compiler/artifact.json"
    assert (artifact_root / written_path).read_bytes() == written_payload
    assert [item["sha256"] for item in store.items] == expected
    assert native_app.call_count == 2
    assert all(
        call.args[0] == "test-result-identity.json"
        for call in native_app.call_args_list
    )
    assert host_digest.call_count == 0


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_artifact_store_hashes_native_stream_limit_in_five_updates(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = mncs_test.ArtifactStore(
        artifact_root,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    payload = bytes(
        (index * 19 + 7) % 256
        for index in range(mncs_test.TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES)
    )
    file_payload = bytes(
        (index * 23 + 11) % 256
        for index in range(mncs_test.TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES)
    )
    path = artifact_root / "compiler" / "large.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(file_payload)

    with (
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_file", wraps=mncs_test.sha256_file
        ) as host_file_digest,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_bytes_digest,
    ):
        written = store.write("stdout/large.bin", payload, "stdout")
        relative = store.record_existing(path, "compiler-output")

    assert written == "stdout/large.bin"
    assert relative == "compiler/large.bin"
    assert store.items[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert store.items[1]["sha256"] == hashlib.sha256(file_payload).hexdigest()
    assert native_app.call_count == 10
    assert [call.args[0] for call in native_app.call_args_list] == [
        "test-result-identity-stream.json"
    ] * 10
    requests = [call.args[1] for call in native_app.call_args_list]
    assert [sum(len(chunk) for chunk in request["chunks"]) for request in requests] == [
        14_336, 14_336, 14_336, 14_336, 8_192,
        14_336, 14_336, 14_336, 14_336, 8_192,
    ]
    assert [len(request["chunks"]) for request in requests] == [14, 14, 14, 14, 8] * 2
    assert [request["has_state"] for request in requests] == [False, True, True, True, True] * 2
    assert [request["finalize"] for request in requests] == [False, False, False, False, True] * 2
    assert all(
        call.kwargs["step_budget"] <= mncs_test.TEST_RESULT_IDENTITY_MAX_STEP_BUDGET
        for call in native_app.call_args_list
    )
    assert host_file_digest.call_count == 0
    assert host_bytes_digest.call_count == 0


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_artifact_store_hashes_large_payloads_in_a_retained_mncs_session(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = mncs_test.ArtifactStore(
        artifact_root,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    payload = bytes((index * 37 + 5) % 256 for index in range(512 * 1024))
    file_payload = bytes((index * 41 + 13) % 256 for index in range(384 * 1024 + 17))
    path = artifact_root / "compiler" / "large.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(file_payload)

    with (
        store,
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_file", wraps=mncs_test.sha256_file
        ) as host_file_digest,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_bytes_digest,
    ):
        for index, small_payload in enumerate((b"first", b"second", b"third")):
            store.write(f"stdout/{index}.bin", small_payload, "stdout")
        hasher = store._retained_identity_hasher()
        assert hasher is not None and hasher.session is not None
        with mock.patch.object(
            hasher.session,
            "call_batch",
            wraps=hasher.session.call_batch,
        ) as embedded_call_batch:
            empty_stream_digest = hasher.digest_chunks([])
            exact_window = bytes(range(256)) * 56
            exact_window_digest = hasher.digest_chunks([exact_window])
            store.write("stdout/large.bin", payload, "stdout")
            store.record_existing(path, "compiler-output")

        function_batches = [
            [item.get("function") for item in call.args[0]]
            for call in embedded_call_batch.call_args_list
        ]
        requests = [
            call.args[0][0]["typed_args"][0]
            for call in embedded_call_batch.call_args_list
        ]
        finalize_values = [
            request["record"]["fields"]["finalize"]["boolean"]["value"]
            for request in requests
        ]
        request_fields = [request["record"]["fields"] for request in requests]
        window_byte_counts = [
            sum(
                len(chunk["sequence"]["values"])
                for chunk in fields["chunks"]["sequence"]["values"]
            )
            for fields in request_fields
        ]
        window_counts = [
            (size + mncs_test.TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES - 1)
            // mncs_test.TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES
            for size in (len(payload), len(file_payload))
        ]
        expected_windows = sum(window_counts)
        assert expected_windows == 65
        assert max(window_byte_counts) == mncs_test.TEST_RESULT_IDENTITY_STREAM_CHUNK_BYTES
        assert all(
            len(fields["chunks"]["sequence"]["values"])
            <= mncs_test.TEST_RESULT_IDENTITY_STREAM_CHUNKS_PER_REQUEST
            for fields in request_fields
        )
        assert function_batches.count(["update_stream_state"]) == expected_windows + 2
        assert len(function_batches) == expected_windows + 2
        assert all(len(batch) == 1 for batch in function_batches)
        assert "init" not in [batch[0] for batch in function_batches]
        assert "finalize_standard" not in [batch[0] for batch in function_batches]
        assert finalize_values[:2] == [True, True]
        assert finalize_values[1 + window_counts[0]] is True
        assert finalize_values[-1] is True
        assert sum(finalize_values) == 4
        assert empty_stream_digest == hashlib.sha256(b"").hexdigest()
        assert exact_window_digest == hashlib.sha256(exact_window).hexdigest()

        assert store._identity_hasher is not None
        assert store._identity_hasher.session is not None
        assert [item["sha256"] for item in store.items] == [
            hashlib.sha256(b"first").hexdigest(),
            hashlib.sha256(b"second").hexdigest(),
            hashlib.sha256(b"third").hexdigest(),
            hashlib.sha256(payload).hexdigest(),
            hashlib.sha256(file_payload).hexdigest(),
        ]
        assert native_app.call_count == 3
        assert host_file_digest.call_count == 0
        assert host_bytes_digest.call_count == 0

    assert store._identity_hasher is not None
    assert store._identity_hasher.session is not None
    assert store._identity_hasher.session.handle is None


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_manifest_provenance_hashes_bounded_sources_with_mncs(
    tmp_path: Path,
) -> None:
    compiler = tmp_path / "mncs-fixture"
    compiler.write_text(
        "#!/bin/sh\nprintf 'mncs-test-fixture 1\\n'\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    source = tmp_path / "sample.mncs"
    source.write_text("module sample;\n", encoding="utf-8")
    manifest_path = tmp_path / "sample.json"
    manifest_path.write_text('{"profile":"mncs 0.18"}\n', encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    library_source = library / "nested.mncs"
    library_bytes = b"module shared;\n"
    library_source.write_bytes(library_bytes)

    store = mncs_test.ArtifactStore(
        tmp_path / "artifacts",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    with store:
        for payload in (b"first", b"second", b"third"):
            store.digest_payload(payload)
        hasher = store._retained_identity_hasher()
        assert hasher is not None and hasher.session is not None

        with (
            mock.patch.object(store, "digest_file", wraps=store.digest_file) as native_file_hash,
            mock.patch.object(
                store, "digest_payload", wraps=store.digest_payload
            ) as native_payload_hash,
            mock.patch.object(hasher, "digest", wraps=hasher.digest) as native_digest,
            mock.patch.object(
                mncs_test, "sha256_file", wraps=mncs_test.sha256_file
            ) as host_file_digest,
            mock.patch.object(
                mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
            ) as host_payload_digest,
            mock.patch.object(
                mncs_test, "native_runner_policy", return_value=("success", 0)
            ),
            mock.patch.object(
                mncs_test, "build_run_identity_sha256", return_value="a" * 64
            ),
        ):
            result = mncs_test.make_result(
                manifest={
                    "manifest_path": str(manifest_path),
                    "source_path": str(source),
                    "profile": "mncs 0.18",
                    "name": "native-provenance-fixture",
                    "module": "sample",
                    "suite": None,
                    "tests": [],
                    "timeout_seconds": 30,
                },
                source_text=source.read_text(encoding="utf-8"),
                mncs=str(compiler),
                cwd=tmp_path,
                environment=_native_environment(),
                library_paths=[library],
                artifacts=store,
                test_results=[],
                suite_result=None,
                suite_summary={
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "unsupported": 0,
                    "assertion_failures": 0,
                    "setup_failures": 0,
                    "compile_failures": 0,
                    "runtime_failures": 0,
                    "timeout_failures": 0,
                    "infrastructure_failures": 0,
                    "assertion_count": 0,
                    "failure_count": 0,
                    "verdict": "PASS",
                },
                test_inventory=None,
                execution=None,
                classification="success",
                failure_class="none",
                message=None,
                failure_details=None,
                allow_unsupported=False,
            )
        provenance = result["provenance"]

        expected = {
            "compiler": hashlib.sha256(compiler.read_bytes()).hexdigest(),
            "runner": hashlib.sha256(Path(mncs_test.__file__).read_bytes()).hexdigest(),
            "source": hashlib.sha256(source.read_bytes()).hexdigest(),
            "manifest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
        library_leaf_digest = hashlib.sha256(library_bytes).hexdigest()
        expected_library_material = f"nested.mncs\0{library_leaf_digest}\n".encode()
        expected_library = hashlib.sha256(expected_library_material).hexdigest()

        assert provenance["compiler"]["sha256"] == expected["compiler"]
        assert provenance["runner"]["source_sha256"] == expected["runner"]
        assert provenance["source"]["sha256"] == expected["source"]
        assert provenance["manifest"]["sha256"] == expected["manifest"]
        assert provenance["libraries"][0]["sha256"] == expected_library
        assert native_file_hash.call_count == 0
        assert native_payload_hash.call_count == 3
        assert native_digest.call_count == 3
        assert host_file_digest.call_count == 2
        assert host_payload_digest.call_count == 1


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_artifact_store_bounds_native_streaming_to_64_kib(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = mncs_test.ArtifactStore(
        artifact_root,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    payload = bytes(
        (index * 31 + 3) % 256
        for index in range(mncs_test.TEST_RESULT_IDENTITY_NATIVE_STREAM_MAX_BYTES + 1)
    )
    path = artifact_root / "compiler" / "over-bound.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    with (
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_file", wraps=mncs_test.sha256_file
        ) as host_file_digest,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_bytes_digest,
    ):
        written = store.write("stdout/over-bound.bin", payload, "stdout")
        relative = store.record_existing(path, "compiler-output")

    expected = hashlib.sha256(payload).hexdigest()
    assert written == "stdout/over-bound.bin"
    assert relative == "compiler/over-bound.bin"
    assert [item["sha256"] for item in store.items] == [expected, expected]
    assert native_app.call_count == 0
    assert host_bytes_digest.call_count == 1
    assert host_file_digest.call_count == 1


@pytest.mark.skipif(not MNCS.is_file(), reason="a built MNCS compiler is required")
def test_artifact_store_keeps_large_files_on_host_after_session_warmup(
    tmp_path: Path,
) -> None:
    store = mncs_test.ArtifactStore(
        tmp_path / "artifacts",
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    payload = bytes(
        (index * 47 + 19) % 256
        for index in range(mncs_test.TEST_RESULT_IDENTITY_EMBED_STREAM_MAX_BYTES + 1)
    )
    path = tmp_path / "large.bin"
    path.write_bytes(payload)

    with (
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_digest,
        mock.patch.object(
            mncs_test, "sha256_file", wraps=mncs_test.sha256_file
        ) as host_file_digest,
    ):
        for index, small_payload in enumerate((b"first", b"second", b"third")):
            store.digest_payload(small_payload)
        digest = store.digest_payload(payload)
        file_digest = store.digest_file(path)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert file_digest == hashlib.sha256(payload).hexdigest()
    assert native_app.call_count == 3
    assert host_digest.call_count == 1
    assert host_file_digest.call_count == 1
    assert store._identity_hasher is None


def test_artifact_store_requires_a_complete_native_context(tmp_path: Path) -> None:
    with pytest.raises(mncs_test.AdapterError, match="requires MNCS, cwd, and environment"):
        mncs_test.ArtifactStore(tmp_path / "artifacts", mncs=str(MNCS))
