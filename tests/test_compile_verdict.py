from __future__ import annotations

import os
import json
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


def _verdict(
    actual: list[str],
    expected: list[str],
    *,
    compiler_exit_code: int = 1,
    compiler_result_is_object: bool = True,
    valid_field_present: bool = True,
    valid_field_is_true: bool = False,
    expects_failure: bool = True,
) -> str:
    return mncs_test.native_compile_verdict(
        [{"code": code} for code in actual],
        expected,
        compiler_exit_code=compiler_exit_code,
        compiler_result_is_object=compiler_result_is_object,
        valid_field_present=valid_field_present,
        valid_field_is_true=valid_field_is_true,
        expects_failure=expects_failure,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )


def _document_verdict(
    document: bytes,
    expected: list[str],
    *,
    compiler_exit_code: int = 1,
    expects_failure: bool = True,
) -> str | None:
    return mncs_test.native_compile_diagnostic_document_verdict(
        document,
        expected,
        compiler_exit_code=compiler_exit_code,
        expects_failure=expects_failure,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )


def _reference_verdict(
    actual: list[str],
    expected: list[str],
    *,
    compiler_exit_code: int,
    compiler_result_is_object: bool,
    valid_field_present: bool,
    valid_field_is_true: bool,
    expects_failure: bool,
) -> str:
    """Keep the compile-only contract independent from the native implementation."""
    compiled = (
        compiler_exit_code == 0
        and compiler_result_is_object
        and (not valid_field_present or valid_field_is_true)
    )
    if expects_failure:
        if compiled:
            return "compile_expectation_mismatch"
        matched = (
            bool(actual)
            if not expected
            else all(any(code.startswith(prefix) for code in actual) for prefix in expected)
        )
        return "expected_failure" if matched else "diagnostic_mismatch"
    return "compile_pass" if compiled else "compile_failure"


def test_native_compile_verdict_matches_independent_contract() -> None:
    cases = [
        (["MNE041", "MNE122"], ["MNE04", "MNE1"], 1, True, True, False, True),
        (["MNE041"], ["MNE04", "MNE1"], 1, True, True, False, True),
        (["MNE041"], ["MNE999"], 1, True, True, False, True),
        (["MNE041"], ["MNE041"], 1, True, True, False, True),
        (["MNE041"], [], 1, True, True, False, True),
        ([], [], 1, True, True, False, True),
        ([], [], 0, True, True, True, True),
        ([], [], 0, True, False, False, False),
        ([], [], 0, False, False, False, False),
    ]
    for actual, expected, exit_code, is_object, valid_present, valid_true, expects_failure in cases:
        kwargs = {
            "compiler_exit_code": exit_code,
            "compiler_result_is_object": is_object,
            "valid_field_present": valid_present,
            "valid_field_is_true": valid_true,
            "expects_failure": expects_failure,
        }
        assert _verdict(actual, expected, **kwargs) == _reference_verdict(
            actual,
            expected,
            **kwargs,
        )


def _reference_document_verdict(
    document: bytes,
    expected: list[str],
    *,
    compiler_exit_code: int,
    expects_failure: bool,
) -> str:
    decoded = json.loads(document)
    if isinstance(decoded, list):
        diagnostics = [item for item in decoded if isinstance(item, dict)]
    elif isinstance(decoded, dict):
        items = decoded.get("errors")
        if not isinstance(items, list):
            items = decoded.get("diagnostics")
        diagnostics = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    else:
        diagnostics = []
    actual = [str(item.get("code", "")) for item in diagnostics]
    return _reference_verdict(
        actual,
        expected,
        compiler_exit_code=compiler_exit_code,
        compiler_result_is_object=isinstance(decoded, dict),
        valid_field_present=isinstance(decoded, dict) and "valid" in decoded,
        valid_field_is_true=isinstance(decoded, dict) and decoded.get("valid") is True,
        expects_failure=expects_failure,
    )


@pytest.mark.parametrize(
    ("document", "expected", "exit_code", "expects_failure"),
    [
        (b'[{"code":"MNE041"},null,{"code":"MNE122"}]', ["MNE04", "MNE1"], 2, True),
        (
            b'{"valid":false,"errors":[{"code":"MNE\\u0031"}],"diagnostics":[{"code":"wrong"}]}',
            ["MNE1"],
            2,
            True,
        ),
        (b'{"valid":false,"errors":7,"diagnostics":[{"code":"MNE041"}]}', ["MNE04"], 2, True),
        (b'{"valid":true,"errors":[]}', ["MNE"], 0, True),
        (b'{"valid":false,"errors":[]}', [], 2, True),
        (b'{"valid":true}', [], 0, False),
        (b'[{"code":"MNE041"}]', [], 2, True),
    ],
)
def test_native_diagnostic_document_verdict_matches_independent_json_expectations(
    document: bytes,
    expected: list[str],
    exit_code: int,
    expects_failure: bool,
) -> None:
    actual = _document_verdict(
        document,
        expected,
        compiler_exit_code=exit_code,
        expects_failure=expects_failure,
    )

    assert actual is not None
    assert actual == _reference_document_verdict(
        document,
        expected,
        compiler_exit_code=exit_code,
        expects_failure=expects_failure,
    )


def test_native_diagnostic_result_exposes_typed_codes_to_the_test_consumer() -> None:
    document = b'{"valid":false,"errors":[{"code":"MNE\\u0031"},{"code":"MNE042"}]}'
    result = mncs_test.native_compile_diagnostic_document_result(
        document,
        [],
        compiler_exit_code=2,
        expects_failure=True,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )

    assert result is not None
    assert result["diagnostic_codes"] == ["MNE1", "MNE042"]
    assert result["diagnostic_code_count"] == 2


def test_native_diagnostic_document_declines_out_of_bound_or_unsupported_inputs() -> None:
    large = b'{"valid":false,"errors":[],"padding":"' + b"x" * 16_400 + b'"}'
    too_many_diagnostics = json.dumps([
        {"code": f"MNE{index:03d}"} for index in range(33)
    ]).encode()
    too_many_expected = [f"MNE{index:03d}" for index in range(17)]
    wide_result = {f"other_{index:02d}": index for index in range(32)}
    wide_result.update({"valid": False, "errors": []})

    assert _document_verdict(large, ["MNE"]) is None
    assert _document_verdict(too_many_diagnostics, ["MNE"]) is None
    assert _document_verdict(b"[", ["MNE"]) is None
    assert _document_verdict(b'{"valid":false}', too_many_expected) is None
    assert _document_verdict(json.dumps(wide_result).encode(), []) is None
    assert _document_verdict(b'[{"code":7}]', ["7"]) is None


def test_maximum_supported_prefix_uses_the_native_byte_contract() -> None:
    prefix = "A" * 512
    assert _verdict([prefix + "suffix"], [prefix]) == "expected_failure"


def test_verdict_state_carries_across_expected_and_actual_batches(monkeypatch) -> None:
    original = mncs_test.run_native_app
    native_calls = 0

    def count_calls(*args, **kwargs):
        nonlocal native_calls
        native_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mncs_test, "run_native_app", count_calls)
    expected = [f"MNE{index:03d}" for index in range(17)]
    actual = [f"X{index:03d}" for index in range(32)] + expected

    assert _verdict(actual, expected) == "expected_failure"
    assert native_calls == 4


def test_native_prefix_and_invocation_bounds_fail_closed() -> None:
    with pytest.raises(mncs_test.ManifestError, match="512-byte"):
        _verdict(["MNE041"], ["M" * 513])

    with pytest.raises(mncs_test.AdapterError, match="invocation limit"):
        _verdict(["MNE"], ["MNE041"] * 2_100)
