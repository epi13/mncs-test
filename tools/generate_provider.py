#!/usr/bin/env python3
"""Generate the compiler-inventory data consumed by the native test provider.

The generic compiler declaration/callable inventory is the only input to the
provider's data table. Executable test calls go through mncs-embed's
compiler-owned identity dispatcher; this generator emits no per-declaration
host dispatch or callable branches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GENERATED_SOURCE = ROOT / "native/mncs/test/provider_inventory.mncs"
GENERATED_METADATA = ROOT / "native/mncs/test/provider_inventory.metadata.json"
PROVIDER_SOURCE = ROOT / "native/mncs/test/provider.mncs"
GENERATOR_IDENTITY = "mncs-test:provider-generator:callable-data/1"


def compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def digest_hex(value: object) -> str:
    return hashlib.sha256(compact_json(value)).hexdigest()


def inventory_identity(inventory: dict[str, object]) -> str:
    sources = inventory.get("sources", [])
    tests = inventory.get("tests", [])
    assert isinstance(sources, list) and isinstance(tests, list)
    material = [
        [
            {
                "source": item.get("source"),
                "module": item.get("module"),
                "source_artifact_identity": item.get("source_artifact_identity"),
                "subject_identity": item.get("subject_identity"),
                "subject_fingerprint": item.get("subject_fingerprint"),
                "declaration_inventory_identity": item.get("declaration_inventory_identity"),
            }
            for item in sources
            if isinstance(item, dict)
        ],
        [
            {
                "test_case_identity": item.get("test_case_identity"),
                "declaration_identity": item.get("declaration_identity"),
                "callable_identity": item.get("function_identity"),
                "signature_identity": item.get("signature_identity"),
                "module": item.get("module"),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    ]
    return digest_hex(material)


def provider_policy_identity() -> str:
    """Hash provider semantics without comments or revision self-reference."""
    source = PROVIDER_SOURCE.read_text(encoding="utf-8")
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(
        r"fn revision_identity\(\) -> \(result: \[byte; 32\]\) \{.*?\n\}",
        "fn revision_identity() -> (result: [byte; 32]) { REVISION }",
        source,
        count=1,
        flags=re.DOTALL,
    )
    return hashlib.sha256(re.sub(r"\s+", " ", source).strip().encode()).hexdigest()


def revision_identity(
    inventory: dict[str, object], identity: str, policy_identity: str
) -> str:
    sources = inventory.get("sources", [])
    tests = inventory.get("tests", [])
    assert isinstance(sources, list) and isinstance(tests, list)
    material = [
        GENERATOR_IDENTITY,
        policy_identity,
        identity,
        [
            {
                "source": item.get("source"),
                "source_artifact_identity": item.get("source_artifact_identity"),
            }
            for item in sources
            if isinstance(item, dict)
        ],
        [
            {
                "test_case_identity": item.get("test_case_identity"),
                "function_identity": item.get("function_identity"),
                "declaration_identity": item.get("declaration_identity"),
                "signature_identity": item.get("signature_identity"),
                "module": item.get("module"),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    ]
    return digest_hex(material)


def byte_literal(value: bytes | bytearray) -> str:
    return "[" + ", ".join(str(item) for item in value) + "]"


def identity_literal(value: str) -> str:
    return byte_literal(bytes.fromhex(value))


def inventory_from_compiler(
    binary: Path, sources: list[Path], libraries: list[Path]
) -> dict[str, object]:
    environment = dict(os.environ)
    if libraries:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            str(path.resolve()) for path in libraries
        )
    if not sources:
        raise SystemExit("at least one compiler inventory source is required")
    source_rows = []
    tests = []
    seen_sources: set[str] = set()
    seen_tests: set[str] = set()
    for source in sources:
        try:
            source_label = source.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError as error:
            raise SystemExit(f"inventory source must be within the Test repository: {source}") from error
        if source_label in seen_sources:
            raise SystemExit(f"duplicate inventory source: {source_label}")
        seen_sources.add(source_label)
        completed = subprocess.run(
            [str(binary), "declaration-inventory", str(source)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stderr or "compiler declaration inventory failed")
        document = json.loads(completed.stdout)
        if (
            document.get("schema_version") != "mncs.declaration-inventory/1"
            or not document.get("valid")
            or not isinstance(document.get("inventory"), dict)
        ):
            raise SystemExit(json.dumps(document, sort_keys=True))
        declaration_inventory = document["inventory"]
        source_rows.append(
            {
                "source": source_label,
                "module": declaration_inventory["module"],
                "source_artifact_identity": declaration_inventory["source_artifact_identity"],
                "source_profile": declaration_inventory["source_profile"],
                "subject_identity": declaration_inventory["subject_identity"],
                "subject_fingerprint": declaration_inventory["subject_fingerprint"],
                "declaration_inventory_identity": declaration_inventory["inventory_identity"],
            }
        )
        callables = declaration_inventory.get("callables", [])
        for callable_ in callables:
            if not isinstance(callable_, dict) or callable_.get("callable_kind") != "test":
                continue
            test_identity = callable_.get("test_case_identity")
            if not isinstance(test_identity, str) or not test_identity:
                raise SystemExit(
                    "compiler declaration inventory exposed a test without a test-case identity"
                )
            if test_identity in seen_tests:
                raise SystemExit(f"duplicate compiler test identity: {test_identity}")
            seen_tests.add(test_identity)
            tests.append(
                {
                    "source": source_label,
                    "declaration_identity": callable_["declaration_identity"],
                    "test_case_identity": test_identity,
                    "function_identity": callable_["callable_identity"],
                    "signature_identity": callable_["signature_identity"],
                    "module": callable_["module"],
                    "name": callable_["name"],
                    "qualified_name": callable_["qualified_name"],
                    "source_span": callable_["source_span"],
                    "profile": callable_["profile"],
                    "generic_params": callable_["generic_params"],
                    "inputs": callable_["inputs"],
                    "outputs": callable_["outputs"],
                    "effects": callable_["effects"],
                    "capabilities": callable_["capabilities"],
                    "subject_identity": declaration_inventory["subject_identity"],
                    "subject_fingerprint": declaration_inventory["subject_fingerprint"],
                    "semantic_fingerprint": test_identity,
                }
            )
    source_rows.sort(key=lambda item: str(item["source"]))
    tests.sort(key=lambda item: (item["declaration_identity"], item["qualified_name"]))
    return {
        "schema_version": "mncs.test-inventory/compatibility/1",
        "scope": "source_module_set",
        "sources": source_rows,
        "tests": tests,
    }


def generate_source(inventory: dict[str, object], identity: str, revision: str) -> str:
    sources = inventory["sources"]
    tests = inventory["tests"]
    assert isinstance(sources, list) and isinstance(tests, list)
    source_labels = ", ".join(str(item["source"]) for item in sources if isinstance(item, dict))
    lines = [
        "mncs 0.18;",
        "",
        "// GENERATED FILE: do not edit by hand.",
        f"// generator: {GENERATOR_IDENTITY}",
        f"// compiler inventory identity: {identity}",
        f"// provider revision identity: {revision}",
        f"// data projection from compiler declaration inventories: {source_labels}",
        "module mncs.test.provider_inventory;",
        "",
        "record TestCallableEntry {",
        "    test_case_identity: [byte; up_to 1024],",
        "    declaration_identity: [byte; up_to 1024],",
        "    callable_identity: [byte; up_to 1024],",
        "    signature_identity: [byte; up_to 128]",
        "}",
        "",
        "record TestCallableInventory {",
        "    entries: [TestCallableEntry; up_to 64],",
        "    count: u64",
        "}",
        "",
        "fn inventory_identity() -> (result: [byte; 32]) {",
        f"    return {identity_literal(identity)};",
        "}",
        "",
        "fn revision_identity() -> (result: [byte; 32]) {",
        f"    return {identity_literal(revision)};",
        "}",
        "",
        "fn callable_inventory() -> (result: TestCallableInventory) {",
        "    return TestCallableInventory {",
        "        entries: [",
    ]
    for index, item in enumerate(tests):
        assert isinstance(item, dict)
        lines.extend(
            [
                "            TestCallableEntry {",
                "            test_case_identity: "
                + byte_literal(str(item["test_case_identity"]).encode())
                + ",",
                "            declaration_identity: "
                + byte_literal(str(item["declaration_identity"]).encode())
                + ",",
                "            callable_identity: "
                + byte_literal(str(item["function_identity"]).encode())
                + ",",
                "            signature_identity: "
                + byte_literal(str(item["signature_identity"]).encode()),
                "            }" + ("," if index + 1 < len(tests) else ""),
            ]
        )
    lines.extend(["        ],", f"        count: {len(tests)}", "    };", "}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mncs", required=True, type=Path)
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--library", action="append", type=Path, default=[])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sources = args.source or [
        Path("tests/self_suite.mncs"),
        Path("tests/provider_cross_module.mncs"),
    ]
    inventory = inventory_from_compiler(args.mncs, sources, args.library)
    identity = inventory_identity(inventory)
    policy_identity = provider_policy_identity()
    revision = revision_identity(inventory, identity, policy_identity)
    generated = generate_source(inventory, identity, revision)
    metadata = {
        "schema_version": "mncs-test.generated-provider/1",
        "generator": GENERATOR_IDENTITY,
        "input": {
            "sources": inventory["sources"],
            "inventory_identity": identity,
        },
        "provider_revision_identity": revision,
        "provider_policy_identity": policy_identity,
        "output": {
            "path": str(GENERATED_SOURCE.relative_to(ROOT)),
            "source_sha256": hashlib.sha256(generated.encode()).hexdigest(),
        },
    }
    if args.check:
        if GENERATED_SOURCE.read_text(encoding="utf-8") != generated:
            raise SystemExit("generated provider inventory source is stale; run generate_provider.py")
        if json.loads(GENERATED_METADATA.read_text(encoding="utf-8")) != metadata:
            raise SystemExit("generated provider inventory metadata is stale")
        provider = PROVIDER_SOURCE.read_text(encoding="utf-8")
        if "generated.callable_inventory()" not in provider:
            raise SystemExit("provider is not bound to the compiler inventory data table")
        if "generated.run_one(" in provider or "return name();" in provider:
            raise SystemExit("provider still contains generated executable callable dispatch")
    else:
        GENERATED_SOURCE.write_text(generated, encoding="utf-8")
        GENERATED_METADATA.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "valid": True,
                "inventory_identity": identity,
                "provider_policy_identity": policy_identity,
                "provider_revision_identity": revision,
                "test_count": len(inventory["tests"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
