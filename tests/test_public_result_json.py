from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

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

