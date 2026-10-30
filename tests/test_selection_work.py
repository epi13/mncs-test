"""Native comparison-work policy against independent integer expectations."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = next(path for path in ROOT.parents if (path / "mncs-language").is_dir())
sys.path.insert(0, str(ROOT / "tools"))
import mncs_test_verify as verify


def execution():
    environment = dict(os.environ)
    stdlib = Path(environment.get("MNCS_STDLIB_ROOT", WORKSPACE / "mncs-stdlib"))
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join((str(stdlib / "library"), str(ROOT / "native")))
    return {"mncs": environment.get("MNCS_BINARY", str(WORKSPACE / "mncs-language/target/release/mncs")),
            "cwd": ROOT, "environment": environment}


def record(values=(), present=True, valid=True):
    return {"present": present, "valid": valid, "identities": list(values)}


@pytest.mark.parametrize("patterns,inventory,expected,bounded", [
    ([], [], 0, True),
    (["*"], ["ab", "cd"], 2, True),
    (["a"], ["a", "bb", "ccc"], 18, True),
    (["a", "b"], ["a", "bb", "ccc"], 39, True),
    (["a", "*", "ignored"], ["a", "bb", "ccc"], 18, True),
    ([None, "ignored"], ["a", "bb", "ccc"], 6, True),
    (["é"], ["é", "😀"], 16, True),
    (["x"] * 64, ["x" * 1024] * 64, None, False),
])
def test_work_costs_execute_natively(patterns, inventory, expected, bounded):
    scan = {"patterns": [{"is_text": value is not None, "identity": value or ""} for value in patterns],
            "inventory": inventory, "head": record(inventory)}
    result = verify._selection_work([(scan, [record(inventory), record(["a"], valid=False),
                                           record(["a"], present=False)])], **execution())[0]
    assert result["bounded"] is bounded
    if expected is not None:
        assert result["base_work"] == expected
    assert result["head_work"] == sum(len(value.encode()) for value in inventory)
    assert result["candidate_work"][:3] == [result["head_work"], 0, 0]


def test_verification_consumer_uses_native_costs_and_preserves_exact_admission():
    worlds = [
        {"obligation": {"executor": {"declaration_identities": ["*"]}},
         "inventory_identities": ["one", "two"], "inventory_truncated": False,
         "receipt": {"selected_test_identities": selected}}
        for selected in (["two", "one"], ["one"])
    ]
    stats = {"subprocesses": 0, "selection_runs": 0}
    result = verify.evaluate_selection_admissions(worlds, include_heads=[True, True],
                include_candidates=[False, False], stats=stats, **execution())
    assert [value["head_matches"] for value in result] == [True, False]
    assert all(value["provable"] for value in result)
    assert stats["selection_work_runs"] == 1
    assert stats["selection_runs"] == 1


def test_oversized_native_work_never_admits_a_receipt():
    inventory = ["x" * 1024] * 64
    world = {"obligation": {"executor": {"declaration_identities": ["x"] * 64}},
             "inventory_identities": inventory, "inventory_truncated": False,
             "receipt": {"selected_test_identities": inventory}}
    stats = {"subprocesses": 0, "selection_runs": 0}
    result = verify.evaluate_selection_admissions([world], include_heads=[True],
                include_candidates=[False], stats=stats, **execution())
    assert result[0]["provable"] is False
    assert result[0]["head_matches"] is False
    assert stats == {"subprocesses": 1, "selection_runs": 0, "selection_work_runs": 1}


def test_maximum_preflight_batch_executes_with_the_existing_fuel_limit():
    inventory = ["x" * 1024] * 64
    scan = {"patterns": [{"is_text": True, "identity": "*"}],
            "inventory": inventory, "head": record(inventory)}
    candidates = [record(inventory) for _ in range(64)]
    costs = verify._selection_work([(scan, candidates)] * 8, **execution())
    assert len(costs) == 8
    assert all(cost["bounded"] and cost["base_work"] == 63 * 1024
               and cost["head_work"] == 65536
               and cost["candidate_work"] == [65536] * 64 for cost in costs)
