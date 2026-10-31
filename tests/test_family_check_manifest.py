from __future__ import annotations

import hashlib
import json
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


CHECK_IDENTITY = "mncs-test:verification-plan-contract"
CONTRACT_IDENTITY = "mncs.verification-plan/1"


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


def _project(path: Path) -> dict | None:
    return mncs_test._native_family_check_manifest_projection(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
        mncs=str(MNCS),
        cwd=ROOT,
        environment=_environment(),
    )


def test_native_family_check_manifest_matches_independent_loader() -> None:
    path = ROOT / "family-verification-checks-v1.json"
    expected = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )

    actual = _project(path)

    assert actual is not None
    assert actual["manifest"] == expected["selector"]["manifest"]
    assert actual["inventory_identity"] == expected["selector"]["inventory_identity"]
    assert actual["test_identities"] == expected["selector"]["test_identities"]
    assert actual["check_definition_identity"] == hashlib.sha256(
        mncs_test.compact_json(expected).encode("utf-8")
    ).hexdigest()


def test_native_manifest_sorts_unsorted_selector_identities(tmp_path: Path) -> None:
    source = ROOT / "family-verification-checks-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    selected = next(item for item in document["checks"] if item["identity"] == CHECK_IDENTITY)
    selected["selector"]["test_identities"].reverse()
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    compatibility = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )
    actual = _project(path)

    assert actual is not None
    assert compatibility["selector"]["test_identities"] == sorted(
        selected["selector"]["test_identities"]
    )
    assert actual["test_identities"] == compatibility["selector"]["test_identities"]
    assert actual["check_definition_identity"] == hashlib.sha256(
        mncs_test.compact_json(compatibility).encode("utf-8")
    ).hexdigest()


def test_native_manifest_sorts_unsorted_identity_list_at_native_bound(tmp_path: Path) -> None:
    source = ROOT / "family-verification-checks-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    selected = next(item for item in document["checks"] if item["identity"] == CHECK_IDENTITY)
    selected["selector"]["test_identities"] = [f"test-{index:02d}" for index in range(31, -1, -1)]
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    compatibility = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )
    actual = _project(path)

    assert actual is not None
    assert actual["test_identities"] == compatibility["selector"]["test_identities"]
    assert actual["check_definition_identity"] == hashlib.sha256(
        mncs_test.compact_json(compatibility).encode("utf-8")
    ).hexdigest()


def test_unsorted_identity_list_above_native_sort_bound_uses_loader(tmp_path: Path) -> None:
    source = ROOT / "family-verification-checks-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    selected = next(item for item in document["checks"] if item["identity"] == CHECK_IDENTITY)
    selected["selector"]["test_identities"] = [f"test-{index:02d}" for index in range(32, -1, -1)]
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    compatibility = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )

    assert _project(path) is None
    assert compatibility["selector"]["test_identities"] == sorted(
        selected["selector"]["test_identities"]
    )


def test_duplicate_selector_identities_keep_loader_validation_error(tmp_path: Path) -> None:
    source = ROOT / "family-verification-checks-v1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    selected = next(item for item in document["checks"] if item["identity"] == CHECK_IDENTITY)
    selected["selector"]["test_identities"].append(selected["selector"]["test_identities"][0])
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    assert _project(path) is None
    with pytest.raises(mncs_test.ManifestError, match="selector is invalid"):
        mncs_test.load_family_check(
            path,
            check_identity=CHECK_IDENTITY,
            contract_identity=CONTRACT_IDENTITY,
            repository_id="mncs-test",
        )


def test_native_manifest_decodes_escaped_identity_and_hashes_canonical_definition(tmp_path: Path) -> None:
    source = json.loads((ROOT / "family-verification-checks-v1.json").read_text(encoding="utf-8"))
    selected = next(item for item in source["checks"] if item["identity"] == CHECK_IDENTITY)
    selected["surface"] = 'quote " slash \\ newline\n rocket 🚀'
    escaped = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    escaped = escaped.replace(b"verification-plan-contract", b"verification-plan\\u002dcontract", 1)
    escaped = escaped.replace(b"test-case:tests", b"test\\u002dcase:tests", 1)
    escaped = escaped.replace("🚀".encode("utf-8"), b"\\ud83d\\ude80", 1)
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_bytes(escaped)

    compatibility = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )
    actual = _project(path)

    assert actual is not None
    assert compatibility["identity"] == CHECK_IDENTITY
    assert actual["test_identities"] == compatibility["selector"]["test_identities"]
    assert actual["check_definition_identity"] == hashlib.sha256(
        mncs_test.compact_json(compatibility).encode("utf-8")
    ).hexdigest()


def test_native_manifest_decodes_escaped_identity_with_raw_text_above_256_bytes(
    tmp_path: Path,
) -> None:
    document = json.loads((ROOT / "family-verification-checks-v1.json").read_text(encoding="utf-8"))
    selected = next(item for item in document["checks"] if item["identity"] == CHECK_IDENTITY)
    identity = "test-" + "a" * 50
    selected["selector"]["test_identities"] = [identity]
    escaped_identity = "".join(f"\\u{ord(value):04x}" for value in identity).encode("ascii")
    assert len(escaped_identity) == 330
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = raw.replace(identity.encode("ascii"), escaped_identity, 1)
    path = tmp_path / "family-verification-checks-v1.json"
    path.write_bytes(raw)

    compatibility = mncs_test.load_family_check(
        path,
        check_identity=CHECK_IDENTITY,
        contract_identity=CONTRACT_IDENTITY,
        repository_id="mncs-test",
    )
    actual = _project(path)

    assert actual is not None
    assert actual["test_identities"] == compatibility["selector"]["test_identities"]
    assert actual["check_definition_identity"] == hashlib.sha256(
        mncs_test.compact_json(compatibility).encode("utf-8")
    ).hexdigest()
