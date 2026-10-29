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
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        (str(stdlib), str(ROOT / "native"))
    )

    actual = mncs_test.result_identity_sha256_batch(
        payloads,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=environment,
    )

    assert actual == [hashlib.sha256(payload).hexdigest() for payload in payloads]


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
