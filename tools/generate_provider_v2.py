#!/usr/bin/env python3
"""Generate the compiler-inventory-bound mncs-test provider surface.

The compiler's test inventory is the only input to test-case dispatch.  The
provider module remains handwritten policy (batch validation, result
folding, and artifact publication); this generator owns the repetitive
identity-to-test binding so a declaration cannot be added to the inventory
without regenerating the executable provider surface.
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
GENERATED_SOURCE = ROOT / "native/mncs/test/provider/v2_inventory.mncs"
GENERATED_METADATA = ROOT / "native/mncs/test/provider/v2_inventory.metadata.json"
PROVIDER_SOURCE = ROOT / "native/mncs/test/provider/v2.mncs"
GENERATOR_IDENTITY = "mncs-test:provider-generator:v2-inventory/1"


def compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def digest_hex(value: object) -> str:
    return hashlib.sha256(compact_json(value)).hexdigest()


def inventory_identity(inventory: dict[str, object]) -> str:
    tests = inventory.get("tests", [])
    assert isinstance(tests, list)
    material = [
        inventory.get("subject_identity"),
        inventory.get("subject_fingerprint"),
        [item.get("test_case_identity") for item in tests if isinstance(item, dict)],
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
    tests = inventory.get("tests", [])
    material = [
        GENERATOR_IDENTITY,
        policy_identity,
        identity,
        inventory.get("source_artifact_identity"),
        [
            {
                "test_case_identity": item.get("test_case_identity"),
                "function_identity": item.get("function_identity"),
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
    binary: Path, source: Path, libraries: list[Path]
) -> dict[str, object]:
    environment = dict(os.environ)
    if libraries:
        environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
            str(path.resolve()) for path in libraries
        )
    completed = subprocess.run(
        [str(binary), "test-inventory", str(source)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or "compiler test inventory failed")
    document = json.loads(completed.stdout)
    if not document.get("valid") or not isinstance(document.get("inventory"), dict):
        raise SystemExit(json.dumps(document, sort_keys=True))
    return document["inventory"]


def generate_source(inventory: dict[str, object], identity: str, revision: str) -> str:
    tests = inventory["tests"]
    assert isinstance(tests, list)
    lines = [
        "mncs 0.18;",
        "",
        "// GENERATED FILE: do not edit by hand.",
        f"// generator: {GENERATOR_IDENTITY}",
        f"// compiler inventory identity: {identity}",
        f"// provider revision identity: {revision}",
        "// source: tests/self_suite.mncs; regenerate with tools/generate_provider_v2.py",
        "module mncs.test.provider.v2_inventory;",
        "",
        "use mncs.core.sequences.v1 as sequences;",
        "use mncs.test.assertions.v1;",
        "use tests.self_suite;",
        "",
        "fn inventory_identity() -> (result: [byte; 32]) {",
        f"    return {identity_literal(identity)};",
        "}",
        "",
        "fn revision_identity() -> (result: [byte; 32]) {",
        f"    return {identity_literal(revision)};",
        "}",
        "",
    ]
    matcher_names: list[str] = []
    for index, item in enumerate(tests):
        assert isinstance(item, dict)
        name = item["name"]
        assert isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        matcher = f"matches_{index}_{name}"
        matcher_names.append(matcher)
        identity_bytes = str(item["test_case_identity"]).encode()
        lines.extend(
            [
                f"fn {matcher}(value: [byte; up_to 1024]) -> (result: bool) {{",
                "    let expected: [byte; up_to 1024] = "
                f"{byte_literal(identity_bytes)};",
                "    return sequences.equals_byte_view<1024>(value, expected[0..expected.len]);",
                "}",
                "",
            ]
        )
    lines.extend(
        [
            "fn known_identity(value: [byte; up_to 1024]) -> (result: bool) {",
            "    return " + " || ".join(f"{name}(value)" for name in matcher_names) + ";",
            "}",
            "",
            "fn run_one(selector: [byte; up_to 1024]) -> (result: TestResult) {",
        ]
    )
    for item, matcher in zip(tests, matcher_names):
        assert isinstance(item, dict)
        name = item["name"]
        lines.extend(
            [
                f"    if {matcher}(selector) {{",
                f"        return {name}();",
                "    }",
            ]
        )
    lines.extend(["    return unsupported(9001);", "}", ""])
    return "\n".join(lines)


def replace_simple_function(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"fn {re.escape(name)}\(\) -> \(result: \[byte; 32\]\) \{{\n.*?\n\}}\n",
        re.DOTALL,
    )
    updated, count = pattern.subn(replacement.rstrip() + "\n", source, count=1)
    if count != 1:
        raise SystemExit(f"could not locate {name} in provider source")
    return updated


def update_provider_source(source: str) -> str:
    source = source.replace(
        "// The compiler owns the inventory and its identities. This module is the\n"
        "// generated/bound dispatch surface for that inventory; Actions supplies only\n"
        "// a typed bounded selection and never names a host function or a nearby test.\n"
        "// Compiler inventory identity: f5c2c5664b24e27efdbe5f4e78ac888e96655b550631f8b19caa9731912715db\n"
        "// Bound test identities are compiler inventory output:\n"
        "// mncs:0.2:test-case:tests.self_suite::arithmetic::f678e5ec3041e45e78e5ebefe4e87172fd2fbdd385846a2641a8e9cfd212b911\n"
        "// mncs:0.2:test-case:tests.self_suite::boolean::aed88ee42cd04283f1a415c36c95a3ef6e1db7c20cc63d6b7b28104f90f28500\n"
        "// mncs:0.2:test-case:tests.self_suite::native_runner_policy::a182f0bf43d22cd641fd735631b9580da645cdd8760b030c422095944e02c2e4\n"
        "// mncs:0.2:test-case:tests.self_suite::property_replay::53a735b2f4e53cc0371ebab68a1ff986254356d4b60fbcdea6829c94e1d9213c\n"
        "// mncs:0.2:test-case:tests.self_suite::skipped::5a80bc02aaf043e5158f6bb89b1a779c18fdf0fd1fec366f31902a2c2e487759\n"
        "// mncs:0.2:test-case:tests.self_suite::snapshot_witness::1b56036ec6b10b5bbeffa0ce8074815673c9b18b79ea2a5609a4ca3af3caf9a7\n"
        "// mncs:0.2:test-case:tests.self_suite::task_lifecycle::acd1365f993616c64578e506fc793f07297b9049c77a9a1f0a3c3da188892dae\n",
        "// Compiler inventory dispatch is generated into v2_inventory.mncs.\n"
        "// Actions supplies only a typed bounded selection and never names a\n"
        "// host function or a nearby test.\n",
    )
    source = source.replace("use tests.self_suite;\n", "")
    if "use mncs.test.provider.v2_inventory as generated;" not in source:
        source = source.replace(
            "use mncs.core.sequences.v1 as sequences;\n",
            "use mncs.core.sequences.v1 as sequences;\n"
            "use mncs.test.provider.v2_inventory as generated;\n",
            1,
        )
    source = replace_simple_function(
        source,
        "inventory_identity",
        "fn inventory_identity() -> (result: [byte; 32]) {\n    return generated.inventory_identity();\n}",
    )
    source = replace_simple_function(
        source,
        "revision_identity",
        "fn revision_identity() -> (result: [byte; 32]) {\n    return generated.revision_identity();\n}",
    )
    if "fn is_arithmetic(" in source:
        dispatch_start = source.index("fn is_arithmetic(")
        dispatch_end = source.index("fn known_identity(", dispatch_start)
        source = source[:dispatch_start] + source[dispatch_end:]
    source = re.sub(
        r"fn known_identity\(value: \[byte; up_to 1024\]\) -> \(result: bool\) \{.*?\n\}\n",
        "fn known_identity(value: [byte; up_to 1024]) -> (result: bool) {\n"
        "    return generated.known_identity(value);\n}\n",
        source,
        count=1,
        flags=re.DOTALL,
    )
    source = re.sub(
        r"fn run_one\(selector: \[byte; up_to 1024\]\) -> \(result: TestResult\) \{.*?\n\}\n",
        "fn run_one(selector: [byte; up_to 1024]) -> (result: TestResult) {\n"
        "    return generated.run_one(selector);\n}\n",
        source,
        count=1,
        flags=re.DOTALL,
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mncs", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=Path("tests/self_suite.mncs"))
    parser.add_argument("--library", action="append", type=Path, default=[])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    inventory = inventory_from_compiler(args.mncs, args.source, args.library)
    identity = inventory_identity(inventory)
    policy_identity = provider_policy_identity()
    revision = revision_identity(inventory, identity, policy_identity)
    generated = generate_source(inventory, identity, revision)
    metadata = {
        "schema_version": "mncs-test.generated-provider/v2",
        "generator": GENERATOR_IDENTITY,
        "input": {
            "source": str(args.source),
            "source_artifact_identity": inventory["source_artifact_identity"],
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
            raise SystemExit("generated provider inventory source is stale; run generate_provider_v2.py")
        if json.loads(GENERATED_METADATA.read_text(encoding="utf-8")) != metadata:
            raise SystemExit("generated provider inventory metadata is stale")
        provider = PROVIDER_SOURCE.read_text(encoding="utf-8")
        if "use mncs.test.provider.v2_inventory as generated;" not in provider:
            raise SystemExit("provider v2 is not bound to generated inventory dispatch")
    else:
        GENERATED_SOURCE.write_text(generated, encoding="utf-8")
        GENERATED_METADATA.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        PROVIDER_SOURCE.write_text(update_provider_source(PROVIDER_SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
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
