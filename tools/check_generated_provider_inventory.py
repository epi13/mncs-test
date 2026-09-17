#!/usr/bin/env python3
"""Check the native provider's generated inventory binding against the compiler."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def inventory_identity(inventory: dict) -> str:
    material = [
        inventory.get("subject_identity"),
        inventory.get("subject_fingerprint"),
        [item.get("test_case_identity") for item in inventory.get("tests", [])],
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mncs", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=Path("tests/self_suite.mncs"))
    args = parser.parse_args()
    completed = subprocess.run(
        [str(args.mncs), "test-inventory", str(args.source)],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or "compiler inventory failed")
    inventory = json.loads(completed.stdout)["inventory"]
    expected = inventory_identity(inventory)
    source = Path(__file__).parents[1] / "native/mncs/test/provider/v2.mncs"
    text = source.read_text(encoding="utf-8")
    if expected not in text:
        raise SystemExit(f"generated provider inventory is stale: expected {expected}")
    missing = [
        item["test_case_identity"]
        for item in inventory["tests"]
        if item["test_case_identity"] not in text
    ]
    if missing:
        raise SystemExit("generated provider is missing: " + ", ".join(missing))
    print(json.dumps({"valid": True, "inventory_identity": expected, "test_count": len(inventory["tests"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
