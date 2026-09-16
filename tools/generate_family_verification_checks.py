#!/usr/bin/env python3
"""Refresh compiler-bound selectors in the family verification manifest.

The check identity and selected test identities remain repository-owned
architectural intent.  The compiler owns the inventory identity that binds
those tests.  This adapter only asks the trusted compiler for its bounded
inventory and updates that generated fact; it never chooses a shell command
or evaluates a test result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


CHECKS_SCHEMA = "commons.mncs.family-verification-checks/v1"
INVENTORY_SCHEMA = "mncs.test-inventory/1"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def semantic_inventory_identity(inventory: dict[str, Any]) -> str:
    """Match the runner's semantic inventory identity projection.

    Transport metadata (schema, source path, diagnostics, and future fields)
    must not change a family selector's binding.  The native runner and the
    compatibility adapter both bind the subject and ordered test identities.
    """

    material = [
        inventory.get("subject_identity"),
        inventory.get("subject_fingerprint"),
        [
            entry.get("test_case_identity")
            for entry in inventory.get("tests", [])
            if isinstance(entry, dict)
        ],
    ]
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def inventory_for(
    *,
    mncs: str,
    source: Path,
    libraries: list[Path],
) -> dict[str, Any]:
    environment = dict(os.environ)
    if libraries:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            str(path.resolve()) for path in libraries
        )
    completed = subprocess.run(
        [mncs, "test-inventory", str(source.resolve())],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=60,
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"compiler test-inventory emitted non-JSON output: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        ) from error
    if (
        completed.returncode != 0
        or document.get("schema_version") != INVENTORY_SCHEMA
        or document.get("valid") is not True
        or not isinstance(document.get("inventory"), dict)
    ):
        raise ValueError(
            f"compiler did not establish a valid test inventory: "
            f"{completed.stderr.strip() or document}"
        )
    return document["inventory"]


def regenerate(
    *,
    checks_path: Path,
    manifest_path: Path,
    mncs: str,
    libraries: list[Path],
) -> dict[str, Any]:
    checks = load_json(checks_path, "family verification checks")
    if checks.get("schema_version") != CHECKS_SCHEMA:
        raise ValueError("family verification checks have an unsupported schema")
    if checks.get("repository_id") != "mncs-test":
        raise ValueError("family verification checks must belong to mncs-test")
    with manifest_path.open("rb") as handle:
        manifest = tomllib.load(handle)
    source_value = manifest.get("source")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("mncs-test manifest does not name a source")
    source = manifest_path.parent / source_value
    if not source.is_file():
        raise ValueError(f"mncs-test source is unavailable: {source}")

    inventory = inventory_for(mncs=mncs, source=source, libraries=libraries)
    tests = inventory.get("tests")
    if not isinstance(tests, list):
        raise ValueError("compiler inventory tests must be a list")
    available = {
        test.get("test_case_identity")
        for test in tests
        if isinstance(test, dict) and isinstance(test.get("test_case_identity"), str)
    }
    generated = json.loads(json.dumps(checks))
    behavioral = [
        check
        for check in generated["checks"]
        if isinstance(check, dict) and check.get("runner") == "mncs-test"
    ]
    if not behavioral:
        raise ValueError("expected at least one mncs-test behavioral family check")
    inventory_identity = semantic_inventory_identity(inventory)
    for check in behavioral:
        selector = check.get("selector")
        if not isinstance(selector, dict):
            raise ValueError(
                f"mncs-test behavioral family check {check.get('identity')} has no selector"
            )
        selected = selector.get("test_identities")
        if not isinstance(selected, list) or not selected or not all(
            isinstance(identity, str) and identity for identity in selected
        ):
            raise ValueError(
                f"mncs-test behavioral selector {check.get('identity')} must name test identities"
            )
        missing = sorted(set(selected) - available)
        if missing:
            raise ValueError(
                f"mncs-test behavioral selector {check.get('identity')} names tests absent from the compiler inventory: "
                + ", ".join(missing)
            )
        selector["inventory_identity"] = inventory_identity
    return generated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mncs", required=True, help="trusted mncs compiler executable")
    parser.add_argument("--library", action="append", default=[], help="compiler library directory")
    parser.add_argument("--checks", type=Path, default=Path("family-verification-checks-v1.json"))
    parser.add_argument("--manifest", type=Path, default=Path("mncs-test.toml"))
    parser.add_argument("--check", action="store_true", help="fail if generated output is stale")
    args = parser.parse_args()
    checks_path = args.checks.resolve()
    manifest_path = args.manifest.resolve()
    try:
        generated = regenerate(
            checks_path=checks_path,
            manifest_path=manifest_path,
            mncs=args.mncs,
            libraries=[Path(value) for value in args.library],
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"family verification check generation failed: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(generated, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if checks_path.read_text(encoding="utf-8") != rendered:
            print(f"stale generated family verification checks: {checks_path}", file=sys.stderr)
            return 1
        print(f"current: {checks_path}")
        return 0
    checks_path.write_text(rendered, encoding="utf-8")
    print(f"generated: {checks_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
