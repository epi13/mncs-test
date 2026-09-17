from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


def test_inventory_generator_check_is_authoritative() -> None:
    completed = subprocess.run(
        [
            "python3",
            str(ROOT / "tools/check_generated_provider_inventory.py"),
            "--mncs",
            str(MNCS),
        ],
        cwd=ROOT,
        env=compiler_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["valid"] is True
    assert report["test_count"] == 7
    assert report["provider_revision_identity"]


def test_native_provider_dispatches_inventory_test_and_separates_check_identity() -> None:
    request = json.loads(
        (ROOT / "examples/provider-batch/valid-request.json").read_text(
            encoding="utf-8"
        )
    )
    with tempfile.TemporaryDirectory(prefix=".phase6-provider-", dir=ROOT) as directory:
        artifact_root = Path(directory)
        (artifact_root / "request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        relative = artifact_root.relative_to(ROOT)
        completed = subprocess.run(
            [
                str(MNCS),
                "run-app",
                str(ROOT / "native-applications/test-provider-batch.json"),
                "--grant-structured",
                "provider_artifact",
                "--grant-structured",
                "provider_digest",
                "--",
                str(relative / "request.json"),
                str(relative / "result.json"),
                str(relative / "check.json"),
            ],
            cwd=ROOT,
            env=compiler_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads((artifact_root / "result.json").read_text(encoding="utf-8"))
        check = json.loads((artifact_root / "check.json").read_text(encoding="utf-8"))
    assert result["native_result"]["verdict"] == "PASS"
    assert result["selected_test_identities"] == request["selected_test_identities"]
    assert result["result_identity"] != check["result_identity"]
    assert check["test_result_identity"] == result["result_identity"]


def test_native_application_refuses_mismatched_interface_descriptor() -> None:
    descriptor = json.loads(
        (ROOT / "native-applications/test-provider-batch.json").read_text(
            encoding="utf-8"
        )
    )
    descriptor["interface_identity"] = "00" * 32
    temporary = tempfile.NamedTemporaryFile(
        prefix=".phase6-provider-descriptor-",
        suffix=".json",
        dir=ROOT / "native-applications",
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    path = Path(temporary.name)
    try:
        temporary.write(json.dumps(descriptor))
        temporary.close()
        completed = subprocess.run(
            [
                str(MNCS),
                "run-app",
                str(path),
                "--grant-structured",
                "provider_artifact",
                "--grant-structured",
                "provider_digest",
                "--",
            ],
            cwd=ROOT,
            env=compiler_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    finally:
        path.unlink()
    assert completed.returncode == 3
    assert "interface" in completed.stderr.lower()
