from __future__ import annotations

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

pytestmark = pytest.mark.skipif(
    not MNCS.is_file() or not os.access(MNCS, os.X_OK),
    reason="a built sibling MNCS executable is required",
)


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


def _request() -> dict[str, str]:
    return {
        "schema_version": mncs_test.FAMILY_CHECK_REQUEST_SCHEMA,
        "check_identity": "mncs-test:verification-plan-contract",
        "contract_identity": "mncs.verification-plan/1",
        "contract_revision": "1",
        "verification_plan_id": "a" * 64,
        "family_graph_identity": "b" * 64,
        "edge_fingerprint": "c" * 64,
        "source_change_sha256": "d" * 64,
    }


def _project(path: Path) -> dict[str, str] | None:
    return mncs_test._native_family_check_request_projection(
        path,
        mncs=str(MNCS),
        cwd=path.parent,
        environment=_native_environment(),
    )


def _write(path: Path, payload: dict[str, object]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return raw


def test_native_request_projection_returns_only_the_consumed_scalars(tmp_path: Path) -> None:
    expected = _request()
    path = tmp_path / "request.json"
    _write(path, expected)

    projected = _project(path)

    assert projected == expected
    assert projected is not None
    assert all(isinstance(value, str) for value in projected.values())


def test_family_request_dispatch_skips_the_python_tree_loader_for_supported_input(
    tmp_path: Path,
) -> None:
    expected = _request()
    path = tmp_path / "request.json"
    _write(path, expected)

    with mock.patch.object(
        mncs_test,
        "load_family_check_request",
        side_effect=AssertionError("supported request should stay on the native path"),
    ):
        projected = mncs_test._load_family_check_request(
            path,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert projected == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            None,
            {
                **_request(),
                "future_metadata": {"owner": "another consumer", "flags": [True, None]},
            },
        ),
        (
            b'{"schema_version":"mncs.family-check-request/1","check_identity":"mncs-test:verification-plan-contract","contract_identity":"mncs.verification-plan/1","contract_revision":"1\\u002e0","verification_plan_id":"'
            + b"a" * 64
            + b'","family_graph_identity":"'
            + b"b" * 64
            + b'","edge_fingerprint":"'
            + b"c" * 64
            + b'","source_change_sha256":"'
            + b"d" * 64
            + b'"}',
            {**_request(), "contract_revision": "1.0"},
        ),
        (
            b'{"schema_version":"mncs.family-check-request/1","check_identity":"old","check_identity":"mncs-test:verification-plan-contract","contract_identity":"mncs.verification-plan/1","contract_revision":"1","verification_plan_id":"'
            + b"a" * 64
            + b'","family_graph_identity":"'
            + b"b" * 64
            + b'","edge_fingerprint":"'
            + b"c" * 64
            + b'","source_change_sha256":"'
            + b"d" * 64
            + b'"}',
            _request(),
        ),
    ],
)
def test_forward_compatible_or_escaped_forms_keep_the_host_fallback(
    tmp_path: Path,
    raw: bytes | None,
    expected: dict[str, object],
) -> None:
    path = tmp_path / "request.json"
    if raw is None:
        _write(path, expected)
    else:
        path.write_bytes(raw)

    assert _project(path) is None
    fallback = mncs_test.load_family_check_request(path)
    assert fallback == expected
    routed = mncs_test._load_family_check_request(
        path,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )
    assert routed == expected


def test_invalid_digest_still_uses_the_existing_request_validation(tmp_path: Path) -> None:
    invalid = {**_request(), "verification_plan_id": "G" * 64}
    path = tmp_path / "request.json"
    _write(path, invalid)

    assert _project(path) is None
    with pytest.raises(mncs_test.ManifestError, match="verification_plan_id"):
        mncs_test._load_family_check_request(
            path,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )


def test_unreadable_and_overbound_requests_fall_back_without_native_tree_projection(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    assert _project(missing) is None
    with pytest.raises(mncs_test.ManifestError, match="cannot read family check request"):
        mncs_test._load_family_check_request(
            missing,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    path = tmp_path / "large.json"
    payload = {**_request(), "future_padding": "x" * mncs_test.FAMILY_CHECK_REQUEST_NATIVE_MAX_BYTES}
    _write(path, payload)
    assert _project(path) is None
    assert mncs_test._load_family_check_request(
        path,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    ) == payload
