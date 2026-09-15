from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import generate_family_verification_checks as generator  # noqa: E402


class FamilyVerificationGenerationTests(unittest.TestCase):
    def test_refreshes_every_behavioral_selector_from_one_inventory(self) -> None:
        inventory = {
            "schema_version": "mncs.test-inventory/1",
            "scope": "source_module",
            "tests": [
                {"test_case_identity": "mncs:test-case:inventory"},
                {"test_case_identity": "mncs:test-case:plan"},
            ],
        }
        checks = {
            "schema_version": generator.CHECKS_SCHEMA,
            "repository_id": "mncs-test",
            "checks": [
                {
                    "identity": "mncs-test:test-inventory-join",
                    "contract_identity": "mncs.compiler.test-inventory/1",
                    "runner": "mncs-test",
                    "surface": "compiler-inventory-to-runner",
                    "selector": {
                        "manifest": "mncs-test.toml",
                        "test_identities": ["mncs:test-case:inventory"],
                    },
                },
                {
                    "identity": "mncs-test:verification-plan-contract",
                    "contract_identity": "mncs.verification-plan/1",
                    "runner": "mncs-test",
                    "surface": "selected-consumer-behavior",
                    "selector": {
                        "manifest": "mncs-test.toml",
                        "test_identities": ["mncs:test-case:plan"],
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory(prefix="mncs-test-family-generation-") as directory:
            root = Path(directory)
            checks_path = root / "family-verification-checks-v1.json"
            manifest_path = root / "mncs-test.toml"
            source_path = root / "self.mncs"
            checks_path.write_text(json.dumps(checks), encoding="utf-8")
            manifest_path.write_text('source = "self.mncs"\n', encoding="utf-8")
            source_path.write_text("mncs 0.17;\n", encoding="utf-8")
            with patch.object(generator, "inventory_for", return_value=inventory):
                generated = generator.regenerate(
                    checks_path=checks_path,
                    manifest_path=manifest_path,
                    mncs="mncs",
                    libraries=[],
                )

        expected = hashlib.sha256(
            generator.canonical_bytes(inventory)
        ).hexdigest()
        self.assertEqual(
            [
                check["selector"]["inventory_identity"]
                for check in generated["checks"]
            ],
            [expected, expected],
        )


if __name__ == "__main__":
    unittest.main()
