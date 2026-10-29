from __future__ import annotations

import os
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
