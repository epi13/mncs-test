from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test_native import run_native_app  # noqa: E402
from mncs_test_verify import (  # noqa: E402
    SELECTION_REQUEST_SCHEMA,
    SELECTION_RESULT_SCHEMA,
    evaluate_selection_admissions,
)

LANGUAGE_ROOT = ROOT.parent / "mncs-language"
_default_binary = LANGUAGE_ROOT / "target/release/mncs"
if not _default_binary.is_file():
    _default_binary = LANGUAGE_ROOT / "target/debug/mncs"
MNCS = Path(os.environ.get("MNCS_BINARY", _default_binary))


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else ROOT.parent / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib), str(ROOT / "native")))
    return environment


def _recorded(values: list[str] | None, *, present: bool = True) -> dict:
    if not present:
        return {"present": False, "valid": False, "count": 0, "identities": []}
    ordered = sorted(values or [], key=lambda value: value.encode("utf-8"))
    return {"present": True, "valid": True, "count": len(ordered), "identities": ordered}


def _pattern(value: object) -> dict:
    if isinstance(value, str):
        return {"is_text": True, "identity": value}
    return {"is_text": False, "identity": ""}


def _run_native(
    patterns: list[object],
    inventory: list[str],
    selected: list[str] | None,
    *,
    inventory_complete: bool = True,
    candidates: list[list[str]] | None = None,
) -> dict:
    candidate_sets = candidates or []
    scan = {
        "patterns_bounded": True,
        "pattern_count": len(patterns),
        "patterns": [_pattern(value) for value in patterns],
        "inventory_complete": inventory_complete,
        "inventory_count": len(inventory),
        "inventory": inventory,
        "inventory_sorted": sorted(inventory, key=lambda value: value.encode("utf-8")),
        "head": _recorded(selected),
        "candidate_count": len(candidate_sets),
        "candidates": [_recorded(values) for values in candidate_sets],
    }
    request = {"schema_version": SELECTION_REQUEST_SCHEMA, "scan_count": 1, "scans": [scan]}
    result = run_native_app(
        "test-verification-selection.json",
        request,
        request_schema=SELECTION_REQUEST_SCHEMA,
        result_schema=SELECTION_RESULT_SCHEMA,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )
    return result["scans"][0]


def test_wildcard_and_exact_selection_resolve_in_mncs() -> None:
    wildcard = _run_native(["*"], ["suite.two", "suite.one"], ["suite.one", "suite.two"])
    assert wildcard["provable"] is True
    assert wildcard["required"][:wildcard["required_count"]] == ["suite.two", "suite.one"]
    assert wildcard["head_matches"] is True

    exact = _run_native(
        ["suite.two", "suite.one", "suite.two"],
        ["suite.one", "suite.two", "suite.three"],
        ["suite.one", "suite.two"],
    )
    assert exact["provable"] is True
    assert exact["required"][:exact["required_count"]] == ["suite.two", "suite.one"]
    assert exact["head_matches"] is True


@pytest.mark.parametrize("pattern", ["suite.*", "suite.missing"])
def test_unknown_and_glob_patterns_are_unprovable(pattern: str) -> None:
    result = _run_native([pattern], ["suite.one", "suite.two"], ["suite.one", "suite.two"])
    assert result["provable"] is False
    assert result["head_matches"] is False


def test_wildcard_short_circuit_and_invalid_pattern_order() -> None:
    wildcard_first = _run_native(
        ["*", "suite.missing"], ["suite.one", "suite.two"], ["suite.one", "suite.two"]
    )
    assert wildcard_first["provable"] is True
    assert wildcard_first["head_matches"] is True

    invalid_first = _run_native(
        [17, "*"], ["suite.one", "suite.two"], ["suite.one", "suite.two"]
    )
    assert invalid_first["provable"] is False
    assert invalid_first["head_matches"] is False


def test_empty_patterns_are_wildcard_and_incomplete_inventory_fails_closed() -> None:
    empty = _run_native([], ["suite.one", "suite.two"], ["suite.one", "suite.two"])
    assert empty["provable"] is True
    assert empty["required"][:empty["required_count"]] == ["suite.one", "suite.two"]

    truncated = _run_native(
        ["*"], ["suite.one", "suite.two"], ["suite.one", "suite.two"],
        inventory_complete=False,
    )
    assert truncated["provable"] is False
    assert truncated["head_matches"] is False


def test_selection_coverage_preserves_duplicates_and_checks_candidates() -> None:
    result = _run_native(
        ["*"],
        ["suite.one", "suite.two"],
        ["suite.one", "suite.one"],
        candidates=[["suite.two", "suite.one"], ["suite.one"]],
    )
    assert result["head_matches"] is False
    assert result["candidate_matches"][:2] == [True, False]


def test_verifier_consumer_uses_native_selection_application() -> None:
    receipt = {"selected_test_identities": ["suite.two", "suite.one"]}
    world = {
        "obligation": {"executor": {"declaration_identities": ["suite.one"]}},
        "inventory_identities": ["suite.one", "suite.two"],
        "inventory_truncated": False,
        "presented": receipt,
        "receipt": receipt,
        "history": [],
    }
    admission = evaluate_selection_admissions(
        [world],
        include_heads=[True],
        include_candidates=[False],
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )[0]
    assert admission["provable"] is True
    assert admission["required_test_identities"] == ["suite.one"]
    assert admission["head_matches"] is False
