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
import mncs_test_native  # noqa: E402
from mncs_test_native import NativeTransportError, run_test_inventory_filter  # noqa: E402


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


def _reference(search_texts: list[str], filters: list[str]) -> list[int]:
    if not filters:
        return list(range(len(search_texts)))
    return [
        index for index, text in enumerate(search_texts)
        if any(pattern in text for pattern in filters)
    ]


def _select(tmp_path: Path, search_texts: list[str], filters: list[str]) -> dict[str, object]:
    return run_test_inventory_filter(
        search_texts,
        filters,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
    )


def test_native_inventory_filter_matches_independent_reference(tmp_path: Path) -> None:
    search_texts = [
        "tests.math arithmetic",
        "tests.string string_add",
        "long identity " + ("x" * 90) + "needle" + ("y" * 90),
        "caf\u00e9 operations",
        "",
    ]
    filters = ["arithmetic", "needle", "f\u00e9", "missing"]

    result = _select(tmp_path, search_texts, filters)

    assert result["selected_indices"] == _reference(search_texts, filters)
    assert result["calls"] == 1
    assert result["case_count"] == len(search_texts)
    assert result["filter_count"] == len(filters)


@pytest.mark.parametrize("filters", [[], [""]])
def test_empty_filters_preserve_all_inventory_records(
    tmp_path: Path, filters: list[str],
) -> None:
    search_texts = ["", "one", "two"]

    result = _select(tmp_path, search_texts, filters)

    assert result["selected_indices"] == [0, 1, 2]
    assert result["selected_indices"] == _reference(search_texts, filters)


def test_runner_does_not_start_the_native_kernel_without_filter_terms(
    tmp_path: Path,
) -> None:
    tests = [{"id": "one"}, {"id": "two"}]
    summary: dict[str, object] = {}

    selected = mncs_test.select_tests(
        tests,
        [],
        mncs="not-invoked-without-filter-terms",
        cwd=tmp_path,
        environment={},
        timeout_seconds=1,
        selection_summary=summary,
    )

    assert selected is tests
    assert summary["status"] == "not_required"
    assert summary["calls"] == 0


def test_candidate_and_filter_batches_merge_native_matches_in_inventory_order(
    tmp_path: Path,
) -> None:
    search_texts = [f"record-{index}" for index in range(65)]
    filters = [f"absent-{index}" for index in range(64)] + ["record-64"]

    result = _select(tmp_path, search_texts, filters)

    assert result["selected_indices"] == [64]
    assert result["calls"] == 25


def test_native_filter_rejects_text_outside_its_declared_bound(tmp_path: Path) -> None:
    with pytest.raises(NativeTransportError, match="1024-byte bound"):
        _select(tmp_path, ["record"], ["x" * 1025])


def test_native_filter_refuses_adversarial_scan_cost_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mncs_test_native,
        "run_native_app",
        lambda *args, **kwargs: pytest.fail("over-budget filtering must be rejected before execution"),
    )
    with pytest.raises(NativeTransportError, match="step-budget work estimate"):
        _select(
            tmp_path,
            ["a" * 1024] * 16,
            [("a" * 449) + "b" + ("a" * 450)] * 16,
        )


def test_runner_uses_adaptive_native_batches_for_endpoint_mismatches(
    tmp_path: Path,
) -> None:
    tests = [{"id": "a" * 1000, "entry": ""} for _ in range(16)]
    filters = ["a" * 900 + "b"] * 16
    summary: dict[str, object] = {}

    selected = mncs_test.select_tests(
        tests,
        filters,
        mncs=str(MNCS),
        cwd=tmp_path,
        environment=_environment(),
        timeout_seconds=120,
        selection_summary=summary,
    )

    assert selected == []
    assert summary["authority"] == "mncs.test.inventory_filter_v1"
    assert summary["status"] == "returned"
    assert summary["calls"] == 2


def test_runner_reports_host_fallback_when_native_call_limit_is_exceeded(
    tmp_path: Path,
) -> None:
    tests = [{"id": "record"}]
    filters = ["absent"] * 1025
    summary: dict[str, object] = {}

    selected = mncs_test.select_tests(
        tests,
        filters,
        mncs="not-invoked-for-native-call-limit",
        cwd=tmp_path,
        environment={},
        timeout_seconds=1,
        selection_summary=summary,
    )

    assert selected == []
    assert summary["status"] == "host_fallback_call_limit"
    assert summary["fallback_reason"] == "native_batch_call_limit_exceeded"


def test_runner_preserves_overbound_filter_behavior_with_explicit_host_fallback(
    tmp_path: Path,
) -> None:
    tests = [{"id": "needle" + ("x" * 1025), "entry": "long_name"}]
    summary: dict[str, object] = {}

    selected = mncs_test.select_tests(
        tests,
        ["needle"],
        mncs="not-invoked-for-overbound-input",
        cwd=tmp_path,
        environment={},
        timeout_seconds=1,
        selection_summary=summary,
    )

    assert selected == tests
    assert summary["status"] == "host_fallback_input_bound"
    assert summary["fallback_reason"] == "input_exceeds_native_1024_byte_bound"


def test_runner_reports_host_fallback_for_native_work_estimate(tmp_path: Path) -> None:
    tests = [{"id": "a" * 800, "entry": ""}]
    filters = [("a" * 350) + "b" + ("a" * 349)]
    summary: dict[str, object] = {}

    selected = mncs_test.select_tests(
        tests,
        filters,
        mncs="not-invoked-for-overbound-input",
        cwd=tmp_path,
        environment={},
        timeout_seconds=1,
        selection_summary=summary,
    )

    assert selected == []
    assert summary["status"] == "host_fallback_work_estimate"
    assert summary["fallback_reason"] == "estimated_work_exceeds_native_step_budget"
