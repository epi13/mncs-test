from __future__ import annotations

import os
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

import sys

sys.path.insert(0, str(ROOT / "tools"))
import mncs_test  # noqa: E402

pytestmark = pytest.mark.skipif(
    not MNCS.is_file() or not os.access(MNCS, os.X_OK),
    reason="a built sibling MNCS executable is required",
)


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    explicit = environment.get("MNCS_STDLIB_ROOT")
    stdlib = Path(explicit) / "library" if explicit else WORKSPACE_ROOT / "mncs-stdlib/library"
    if not stdlib.is_dir():
        stdlib = LANGUAGE_ROOT / "library"
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib), str(ROOT / "native")))
    return environment


def _select(
    tmp_path: Path, tests: list[dict[str, object]], requested: list[str],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    summary: dict[str, object] = {}
    selected = mncs_test.select_exact_test_identities(
        tests,
        requested,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        timeout_seconds=120.0,
        selection_summary=summary,
    )
    return selected, summary


def _reference(
    tests: list[dict[str, object]], requested: list[str],
) -> list[dict[str, object]]:
    by_id = {str(test.get("id")): test for test in tests if test.get("id")}
    return [by_id[identity] for identity in sorted(requested)]


def test_native_selection_preserves_sorted_exact_identity_and_last_row_semantics(
    tmp_path: Path,
) -> None:
    tests = [
        {"id": "tests.math::zeta", "entry": "first zeta"},
        {"id": "tests.math::alpha", "entry": "alpha"},
        {"id": "tests.math::zeta", "entry": "last zeta"},
        {"id": "tests.math::alphabet", "entry": "alphabet"},
    ]
    requested = ["tests.math::zeta", "tests.math::alpha"]

    selected, summary = _select(tmp_path, tests, requested)

    assert selected == _reference(tests, requested)
    assert selected == [tests[1], tests[2]]
    assert summary == {
        "authority": "mncs.test.exact_identity_selection_v2",
        "mode": "exact_test_identity",
        "status": "returned",
        "calls": 1,
        "case_count": 4,
        "identity_count": 2,
        "selected_count": 2,
    }


@pytest.mark.parametrize("requested", [[], ["duplicate", "duplicate"]])
def test_empty_or_duplicate_requests_fail_before_native_execution(
    tmp_path: Path, requested: list[str],
) -> None:
    with pytest.raises(mncs_test.ManifestError, match="non-empty unique list"):
        _select(tmp_path, [{"id": "present"}], requested)


def test_missing_exact_identities_are_reported_in_sorted_order(tmp_path: Path) -> None:
    with pytest.raises(mncs_test.ManifestError) as error:
        _select(tmp_path, [{"id": "present"}], ["missing-z", "missing-a"])

    assert str(error.value).endswith("missing-a, missing-z")


def test_substring_is_not_accepted_as_an_exact_identity(tmp_path: Path) -> None:
    with pytest.raises(mncs_test.ManifestError, match="absent from the current compiler inventory"):
        _select(tmp_path, [{"id": "tests.math::arithmetic"}], ["arithmetic"])


def test_native_selection_batches_inventory_and_identity_requests(
    tmp_path: Path,
) -> None:
    tests = [{"id": f"test-case-{index:02d}"} for index in range(65)]
    requested = [str(test["id"]) for test in reversed(tests)]

    selected, summary = _select(tmp_path, tests, requested)

    assert selected == _reference(tests, requested)
    assert summary["calls"] == 25
    assert summary["case_count"] == 65
    assert summary["identity_count"] == 65
    assert summary["selected_count"] == 65


def test_last_duplicate_candidate_wins_across_batch_boundary(tmp_path: Path) -> None:
    tests = [{"id": f"candidate-{index:02d}"} for index in range(17)]
    tests[0] = {"id": "repeat", "entry": "first"}
    tests[-1] = {"id": "repeat", "entry": "last"}

    selected, summary = _select(tmp_path, tests, ["repeat"])

    assert selected == [tests[-1]]
    assert summary["authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["calls"] == 2


def test_identity_over_512_bytes_executes_natively(
    tmp_path: Path,
) -> None:
    long_identity = "x" * 513
    tests = [{"id": long_identity}]

    selected, summary = _select(tmp_path, tests, [long_identity])

    assert selected == tests
    assert summary["authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["status"] == "returned"
    assert summary["calls"] == 1


def test_multibyte_identity_is_segmented_at_utf8_boundaries(tmp_path: Path) -> None:
    identity = "a" * 511 + "é" * 300
    tests = [{"id": identity}]

    selected, summary = _select(tmp_path, tests, [identity])

    assert selected == tests
    assert summary["authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["status"] == "returned"
    assert summary["calls"] == 1


def test_identity_at_8192_byte_native_bound_executes_natively(tmp_path: Path) -> None:
    identity = "x" * 8192
    tests = [{"id": identity}]

    selected, summary = _select(tmp_path, tests, [identity])

    assert selected == tests
    assert summary["authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["status"] == "returned"
    assert summary["calls"] == 1


def test_adaptive_batches_keep_overbudget_identity_work_native(
    tmp_path: Path,
) -> None:
    identities = ["x" * 255 + f"{index:02d}" for index in range(16)]
    tests = [{"id": identity} for identity in identities]

    selected, summary = _select(tmp_path, tests, identities)

    assert selected == _reference(tests, identities)
    assert summary["authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["status"] == "returned"
    assert summary["calls"] == 2


def test_identity_over_8192_bytes_uses_reported_host_fallback(tmp_path: Path) -> None:
    identity = "x" * 8193
    tests = [{"id": identity}]

    selected, summary = _select(tmp_path, tests, [identity])

    assert selected == tests
    assert summary["authority"] == "mncs-test.python_identity_fallback"
    assert summary["requested_native_authority"] == "mncs.test.exact_identity_selection_v2"
    assert summary["status"] == "host_fallback_input_bound"
    assert summary["fallback_reason"] == "input_exceeds_native_8192_byte_bound"


def test_native_call_limit_uses_reported_host_fallback(tmp_path: Path) -> None:
    tests = [{"id": f"candidate-{index:04d}"} for index in range(1025)]
    requested = [str(tests[0]["id"])]

    selected, summary = _select(tmp_path, tests, requested)

    assert selected == [tests[0]]
    assert summary["authority"] == "mncs-test.python_identity_fallback"
    assert summary["status"] == "host_fallback_call_limit"
    assert summary["fallback_reason"] == "native_batch_call_limit_exceeded"
