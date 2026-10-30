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
def test_artifact_store_streams_medium_payload_and_file_hashes_in_mncs(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = mncs_test.ArtifactStore(
        artifact_root,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    payload = bytes((index * 19 + 7) % 256 for index in range(20_000))
    file_payload = bytes((index * 23 + 11) % 256 for index in range(20_000))
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
    assert native_app.call_count == 4
    assert [call.args[0] for call in native_app.call_args_list] == [
        "test-result-identity-stream.json",
        "test-result-identity-stream.json",
        "test-result-identity-stream.json",
        "test-result-identity-stream.json",
    ]
    requests = [call.args[1] for call in native_app.call_args_list]
    assert [sum(len(chunk) for chunk in request["chunks"]) for request in requests] == [
        12_288,
        7_712,
        12_288,
        7_712,
    ]
    assert [request["has_state"] for request in requests] == [False, True, False, True]
    assert [request["finalize"] for request in requests] == [False, True, False, True]
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
    payload = bytes((index * 37 + 5) % 256 for index in range(131_073))
    file_payload = bytes((index * 41 + 13) % 256 for index in range(196_619))
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
        store.write("stdout/large.bin", payload, "stdout")
        store.record_existing(path, "compiler-output")

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

    with (
        mock.patch.object(
            mncs_test, "run_native_app", wraps=mncs_test.run_native_app
        ) as native_app,
        mock.patch.object(
            mncs_test, "sha256_bytes", wraps=mncs_test.sha256_bytes
        ) as host_digest,
    ):
        for index, small_payload in enumerate((b"first", b"second", b"third")):
            store.digest_payload(small_payload)
        digest = store.digest_payload(payload)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert native_app.call_count == 3
    assert host_digest.call_count == 1
    assert store._identity_hasher is None


def test_artifact_store_requires_a_complete_native_context(tmp_path: Path) -> None:
    with pytest.raises(mncs_test.AdapterError, match="requires MNCS, cwd, and environment"):
        mncs_test.ArtifactStore(tmp_path / "artifacts", mncs=str(MNCS))
