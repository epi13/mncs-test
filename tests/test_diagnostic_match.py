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


def _matches(actual: list[str], expected: list[str]) -> bool:
    return mncs_test.native_diagnostic_matches(
        [{"code": code} for code in actual],
        expected,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )


def _reference_match(actual: list[str], expected: list[str]) -> bool:
    """Preserve the documented Python behavior as an independent test oracle."""
    if not expected:
        return bool(actual)
    return all(any(code.startswith(prefix) for code in actual) for prefix in expected)


def test_native_match_requires_every_expected_prefix() -> None:
    cases = [
        (["MNE041", "MNE122"], ["MNE04", "MNE1"]),
        (["MNE041"], ["MNE04", "MNE1"]),
        (["MNE041"], ["MNE999"]),
        (["MNE041"], ["MNE041"]),
    ]
    for actual, expected in cases:
        assert _matches(actual, expected) == _reference_match(actual, expected)


def test_empty_expectations_require_at_least_one_diagnostic() -> None:
    assert _matches(["MNE041"], [])
    assert not _matches([], [])


def test_maximum_supported_prefix_uses_the_native_byte_contract() -> None:
    prefix = "A" * 512
    assert _matches([prefix + "suffix"], [prefix])


def test_matching_state_carries_across_expected_and_actual_batches(monkeypatch) -> None:
    original = mncs_test.run_native_app
    native_calls = 0

    def count_calls(*args, **kwargs):
        nonlocal native_calls
        native_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mncs_test, "run_native_app", count_calls)
    expected = [f"MNE{index:03d}" for index in range(17)]
    actual = [f"X{index:03d}" for index in range(32)] + expected

    assert _matches(actual, expected) == _reference_match(actual, expected)
    assert native_calls == 4


def test_native_prefix_and_invocation_bounds_fail_closed() -> None:
    with pytest.raises(mncs_test.ManifestError, match="512-byte"):
        _matches(["MNE041"], ["M" * 513])

    with pytest.raises(mncs_test.AdapterError, match="invocation limit"):
        _matches(["MNE"], ["MNE041"] * 2_100)
