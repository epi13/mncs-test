from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mncs_test_verify import (  # noqa: E402
    VERIFY_VERSION,
    evaluate_selection_admissions,
    evaluate_semantic_recall,
    find_recall_candidates,
)


def _workspace_root() -> Path:
    for candidate in (ROOT, *ROOT.parents):
        if all(
            (candidate / name).is_dir()
            for name in ("mncs-language", "mncs-stdlib", "MNCS-Commons")
        ):
            return candidate
    raise AssertionError("MNCS workspace siblings are unavailable")


WORKSPACE = _workspace_root()
MNCS = os.environ.get(
    "MNCS_BINARY",
    str(WORKSPACE / "mncs-language" / "target" / "release" / "mncs"),
)


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        [
            str(WORKSPACE / "mncs-stdlib" / "library"),
            str(ROOT / "native"),
            str(WORKSPACE / "MNCS-Commons" / "src" / "mncs_commons" / "mesh"),
            str(WORKSPACE / "MNCS-Commons" / "src" / "mncs_commons" / "mesh" / "mncs"),
        ]
    )
    return environment


def _bound(**overrides: object) -> dict:
    value = {
        "definition_identity": "definition",
        "subject_identity": "obligation",
        "subject_fingerprint": "subject-fingerprint",
        "executor_identity": "executor",
        "invalidation_identity": "invalidation",
        "toolchain_identity": "toolchain",
        "inventory_identity": "inventory",
        "repository_revision": "revision-1",
        "repository_fingerprint": "fingerprint-1",
        "closure_identity": "closure-identity",
        "closure_trusted": True,
        "closure_fileset": "mncs+manifests/1",
    }
    value.update(overrides)
    return value


def _history_evidence(evidence_id: str, bound: dict, previous: str | None) -> dict:
    return {
        "evidence_id": evidence_id,
        "verdict": "PASS",
        "bound": bound,
        "producer": VERIFY_VERSION,
        "selected_test_identities": ["test-1"],
        "previous_evidence_id": previous,
    }


def test_native_recall_matches_trusted_semantic_closures() -> None:
    current = _bound(repository_revision="revision-current",
                     repository_fingerprint="fingerprint-current")
    candidates = [
        _bound(repository_revision="revision-old",
               repository_fingerprint="fingerprint-old"),
        *[
            _bound(**{field: "changed"})
            for field in (
                "definition_identity",
                "subject_identity",
                "subject_fingerprint",
                "executor_identity",
                "invalidation_identity",
                "toolchain_identity",
                "inventory_identity",
                "closure_identity",
            )
        ],
        _bound(closure_fileset="mncs+manifests/2"),
        _bound(closure_trusted=False),
        _bound(closure_identity=""),
    ]
    expected = [True, *([False] * (len(candidates) - 1))]
    assert evaluate_semantic_recall(
        [
            (current, candidates),
            (_bound(), [_bound(closure_identity="another-closure")]),
        ],
        mncs=MNCS,
        cwd=ROOT,
        environment=_environment(),
    ) == [expected, [False]]
    assert evaluate_semantic_recall(
        [(_bound(closure_trusted=False), [_bound()])],
        mncs=MNCS,
        cwd=ROOT,
        environment=_environment(),
    ) == [[False]]
    assert evaluate_semantic_recall(
        [(_bound(closure_identity=""), [_bound()])],
        mncs=MNCS,
        cwd=ROOT,
        environment=_environment(),
    ) == [[False]]


def test_recall_path_selects_newest_native_match() -> None:
    current = _bound(repository_revision="revision-current",
                     repository_fingerprint="fingerprint-current")
    oldest = _history_evidence(
        "oldest", _bound(inventory_identity="different-inventory"), "missing-parent"
    )
    older = _history_evidence(
        "older",
        _bound(repository_revision="revision-old",
               repository_fingerprint="fingerprint-old"),
        "oldest",
    )
    newest = _history_evidence(
        "newest", _bound(toolchain_identity="different-toolchain"), "older"
    )
    head = _history_evidence("head", _bound(closure_identity="head-only"), "newest")
    world = {
        "obligation": {
            "identity": "obligation-1",
            "lifecycle": "permanent",
            "executor": {
                "kind": "native_first_class_test",
                "source_paths": ["src/suite.mncs"],
                "declaration_identities": ["*"],
            },
        },
        "bound": current,
        "inventory_identities": ["test-1"],
        "inventory_truncated": False,
        "receipt": head,
        "history": [head, newest, older, oldest],
    }

    no_match_world = dict(world, bound=_bound(toolchain_identity="third-toolchain"))
    admissions = evaluate_selection_admissions(
        [world, no_match_world],
        include_heads=[False, False],
        include_candidates=[True, True],
        mncs=MNCS,
        cwd=ROOT,
        environment=_environment(),
    )
    world["selection_admission"] = admissions[0]
    no_match_world["selection_admission"] = admissions[1]
    candidates = find_recall_candidates(
        [world, no_match_world], mncs=MNCS, cwd=ROOT, environment=_environment()
    )
    candidate = candidates[0]
    assert candidate is not None
    assert candidate["evidence_id"] == "older"
    assert candidates[1] is None
