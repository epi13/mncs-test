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


def _reference_lineage_hashes(
    rows: list[tuple[int, str, str, object, object]],
) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    for index, run_identity, test_case_identity, status, verdict in rows:
        execution_payload = json.dumps(
            {
                "index": index,
                "run": run_identity,
                "test_case": test_case_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        execution_digest = hashlib.sha256(execution_payload).hexdigest()
        observation_payload = json.dumps(
            {
                "execution": execution_digest,
                "status": status,
                "verdict": verdict,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected.append(
            (execution_digest, hashlib.sha256(observation_payload).hexdigest())
        )
    return expected


def test_native_result_identity_hash_matches_independent_sha256(tmp_path: Path) -> None:
    payloads = [
        b"",
        b"a",
        b"a" * 55,
        b"a" * 56,
        b"a" * 63,
        b"a" * 64,
        b"a" * 65,
        b"a" * 119,
        b"a" * 120,
        b"a" * 127,
        b"a" * 128,
        bytes(range(256)) * 4,
        mncs_test.compact_json(
            {
                "control": "\b\t\n\f\r\u0000",
                "unicode": "Lúna 東京",
                "quote": '" and backslash \\',
            }
        ).encode("utf-8"),
        bytes((index * 37 + 11) % 256 for index in range(4097)),
    ]
    actual = mncs_test.result_identity_sha256_batch(
        payloads,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_native_environment(),
    )

    assert actual == [hashlib.sha256(payload).hexdigest() for payload in payloads]


def test_many_native_result_identity_hashes_split_by_payload_and_step_bounds(
    tmp_path: Path,
) -> None:
    payloads = [f"lineage-{index}".encode() for index in range(17)]
    payloads.extend(bytes((index + 1,)) * 1600 for index in range(9))
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual = mncs_test.result_identity_sha256_many(
            payloads,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == [hashlib.sha256(payload).hexdigest() for payload in payloads]
    assert [call.args[1]["payload_count"] for call in native_app.call_args_list] == [16, 9, 1]


def test_many_result_identity_hashes_keep_oversized_payload_host_fallback(
    tmp_path: Path,
) -> None:
    payloads = [b"before", b"x" * (mncs_test.TEST_RESULT_IDENTITY_MAX_BYTES + 1), b"after"]
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual = mncs_test.result_identity_sha256_many(
            payloads,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == [hashlib.sha256(payload).hexdigest() for payload in payloads]
    assert [call.args[1]["payload_count"] for call in native_app.call_args_list] == [1, 1]


@pytest.mark.parametrize(
    "size",
    [mncs_test.TEST_RESULT_IDENTITY_MAX_BYTES + 1, 20_000],
    ids=["byte-limit", "step-limit"],
)
def test_over_bound_result_identity_preserves_host_compatibility_hash(size: int) -> None:
    payload = b"x" * size
    with mock.patch.object(mncs_test, "run_native_app") as native_app:
        actual = mncs_test.result_identity_sha256(
            payload,
            mncs="unused",
            cwd=ROOT,
            environment={},
        )

    assert actual == hashlib.sha256(payload).hexdigest()
    native_app.assert_not_called()


def test_native_lineage_identity_matches_python_canonical_json(tmp_path: Path) -> None:
    rows = [
        (
            0,
            "mncs:run:\u0001",
            'case:"\\\u0000\tLúna 東京',
            "\b\t\n\f\r\u0000\u0001",
            "PASS",
        ),
        (19, "mncs:run:東京", 'case:backslash\\quote"', None, None),
        (2, "mncs:run:empty", "case:empty", "", "UNKNOWN"),
    ]
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual = mncs_test.lineage_identity_sha256_many(
            rows,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == _reference_lineage_hashes(rows)
    assert [call.args[0] for call in native_app.call_args_list] == [
        "test-lineage-identity.json"
    ]


def test_native_lineage_identity_batches_rows_by_application_bounds(tmp_path: Path) -> None:
    rows = [
        (index, f"mncs:run:{index}", f"test:case:{index}", "returned", "PASS")
        for index in range(17)
    ]
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual = mncs_test.lineage_identity_sha256_many(
            rows,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == _reference_lineage_hashes(rows)
    requests = [
        call.args[1]
        for call in native_app.call_args_list
        if call.args[0] == "test-lineage-identity.json"
    ]
    assert len(requests) >= 2
    assert sum(request["row_count"] for request in requests) == len(rows)
    assert all(request["row_count"] <= mncs_test.TEST_LINEAGE_IDENTITY_MAX_ROWS for request in requests)


def test_lineage_identity_uses_exact_fallback_for_out_of_contract_fields(
    tmp_path: Path,
) -> None:
    rows = [
        (0, "mncs:run:normal", "test:case:normal", "returned", "PASS"),
        (1, "mncs:run:wide", "x" * 513, {"legacy": True}, "UNKNOWN"),
    ]
    with mock.patch.object(
        mncs_test, "run_native_app", wraps=mncs_test.run_native_app
    ) as native_app:
        actual = mncs_test.lineage_identity_sha256_many(
            rows,
            mncs=str(MNCS),
            cwd=tmp_path,
            environment=_native_environment(),
        )

    assert actual == _reference_lineage_hashes(rows)
    application_names = [call.args[0] for call in native_app.call_args_list]
    assert "test-lineage-identity.json" in application_names
    assert "test-result-identity.json" in application_names
