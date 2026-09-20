from __future__ import annotations

import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test import apply_obligation_plan  # noqa: E402


LANGUAGE_ROOT = ROOT.parent / "mncs-language"
COMMONS_ROOT = ROOT.parent / "MNCS-Commons"
MNCS = Path(os.environ.get("MNCS_BINARY", LANGUAGE_ROOT / "target/debug/mncs"))


def compiler_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = ":".join(
        [
            str(LANGUAGE_ROOT / "library"),
            str(COMMONS_ROOT / "src/mncs_commons/mesh"),
            str(ROOT / "native"),
            str(ROOT),
        ]
    )
    return environment


def test_reused_obligation_is_not_executed_and_new_obligation_is_exact() -> None:
    tests = [
        {"id": "case-a", "entry": "a"},
        {"id": "case-b", "entry": "b"},
    ]
    plan = {
        "obligation_plan_id": "plan-1",
        "_selected_obligation_identities": ["obligation-current", "obligation-new"],
        "_reused_obligation_identities": ["obligation-current"],
        "_new_execution_obligation_identities": ["obligation-new"],
        "obligations": [
            {"identity": "obligation-current", "status": "current", "test_case_identities": ["case-a"]},
            {"identity": "obligation-new", "status": "new_execution_required", "test_case_identities": ["case-b"]},
        ],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }

    selected, summary = apply_obligation_plan(tests, plan, native=False)

    assert [item["id"] for item in selected] == ["case-b"]
    assert summary["reused_obligation_identities"] == ["obligation-current"]
    assert summary["new_execution_obligation_identities"] == ["obligation-new"]


def test_unresolved_non_test_obligation_is_reported_without_host_selection() -> None:
    selected, summary = apply_obligation_plan(
        [{"id": "case-a"}],
        {
            "obligation_plan_id": "plan-2",
            "_selected_obligation_identities": ["compile-obligation"],
            "_reused_obligation_identities": [],
            "_new_execution_obligation_identities": ["compile-obligation"],
            "obligations": [
                {"identity": "compile-obligation", "status": "new_execution_required", "test_case_identities": []}
            ],
            "evidence": [],
            "stop": {"sufficient_to_stop": False},
        },
        native=False,
    )

    assert selected == []
    assert summary["selection_unresolved"] == ["compile-obligation"]


def test_native_selection_kernel_matches_the_oracle_projection() -> None:
    tests = [
        {"id": "case-a", "entry": "a"},
        {"id": "case-b", "entry": "b"},
    ]
    plan = {
        "obligation_plan_id": "plan-native",
        "_selected_obligation_identities": ["obligation-current", "obligation-new"],
        "_reused_obligation_identities": ["obligation-current"],
        "_new_execution_obligation_identities": ["obligation-new"],
        "obligations": [
            {"identity": "obligation-current", "status": "current", "test_case_identities": ["case-a"]},
            {"identity": "obligation-new", "status": "new_execution_required", "test_case_identities": ["case-b"]},
        ],
        "evidence": [],
        "stop": {"sufficient_to_stop": False},
    }
    native_selected, native_summary = apply_obligation_plan(
        tests,
        plan,
        mncs=str(MNCS),
        cwd=ROOT,
        environment=compiler_environment(),
    )
    oracle_selected, oracle_summary = apply_obligation_plan(tests, plan, native=False)
    assert [item["id"] for item in native_selected] == [item["id"] for item in oracle_selected]
    assert native_summary["selected_obligation_identities"] == ["obligation-current", "obligation-new"]
    assert native_summary["reused_obligation_identities"] == oracle_summary["reused_obligation_identities"]
    assert native_summary["new_execution_obligation_identities"] == oracle_summary["new_execution_obligation_identities"]
