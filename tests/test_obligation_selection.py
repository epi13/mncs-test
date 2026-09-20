from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test import apply_obligation_plan  # noqa: E402


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

    selected, summary = apply_obligation_plan(tests, plan)

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
    )

    assert selected == []
    assert summary["selection_unresolved"] == ["compile-obligation"]
