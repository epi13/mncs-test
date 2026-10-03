#!/usr/bin/env python3
"""Provider-owned family verification: `mncs-test-verify`.

One repository's verification obligations, evaluated the native way::

    obligation inventory
            |
    host measures the world (git, digests, compiler inventory; no execution)
            |
    native coherence policy decides current / queued / deferred / excluded
            |
    queued native suites execute through `mncs test` (identity-bound)
            |
    native digest renders each outcome; receipts bind world -> verdict
            |
    Store admits the receipts; the agent gets one compact report

Soundness rules (never violated to look fast):

* The native coherence module owns every reuse decision; this file only
  measures and transports. It never reuses a result the native policy did
  not call current.
* Anything unmeasurable (missing toolchain, missing git, invalid suite,
  oversized dependency closure, unknown vocabulary) is UNKNOWN, never
  queued blindly and never green.
* Only ``native_first_class_test`` obligations with an explicit runnable
  binding (exactly one source) execute. Every other queued obligation is
  reported with its reason and left unexecuted.
* A reused FAIL stays a FAIL (current knowledge), and a changed test
  selection always re-executes even when the bound world matches.
* Receipts are content-addressed and written atomically; concurrent runs
  can duplicate work but can never corrupt a receipt or the Store.

This module imports nothing from ``mncs_test`` (the frozen
explicit-compatibility oracle). The native-first transports it needs live
in ``mncs_test_native`` and ``mncs_test_digest``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mncs_test_digest import (  # noqa: E402
    DigestError,
    evaluate_digest,
    project_digest_request,
    render_digest_text,
)
from mncs_test_native import NativeTransportError, run_native_app  # noqa: E402

VERIFY_VERSION = "mncs-test-verify/0.3.0"

OBLIGATION_INVENTORY_SCHEMA = "mncs-family.verification-obligation-inventory/v1"
COHERENCE_REQUEST_SCHEMA = "mncs.test-verification-coherence-request/1"
COHERENCE_RESULT_SCHEMA = "mncs.test-verification-coherence/1"
RECEIPT_SCHEMA = "mncs.test-receipt/1"
REPORT_SCHEMA = "mncs.test-verify-report/1"
STORE_RECEIPT_SCHEMA = b"mncs.test-receipt/1"

MAX_OBLIGATIONS = 32
MAX_IDENTITIES = 64
MAX_IDENTITY_BYTES = 1024
MAX_FINGERPRINT_BYTES = 128
MAX_PATTERNS = 8
MAX_DEP_FILES = 512
MAX_DEP_BYTES = 8 * 1024 * 1024
MAX_STATUS_BYTES = 65536
MAX_DIAGNOSTIC_TEXT = 2048
MAX_STDERR_TEXT = 4096
STORE_CAS_RETRIES = 3
ARTIFACT_KEEP = 20

NATIVE_KIND = "native_first_class_test"

LIFECYCLES = ("permanent", "transitional", "scheduled", "reference_only", "retired")
EXECUTOR_KINDS = (
    "native_first_class_test",
    "compile_experiment",
    "diagnostic_test",
    "backend_check",
    "differential_oracle",
    "migration_parity",
    "external_integration",
)
EVIDENCE_VERDICTS = ("PASS", "FAIL", "UNKNOWN")


class VerifyError(ValueError):
    """Raised for unusable verify inputs (fail closed)."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bound_text(value: object, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise VerifyError(f"verify requires a non-empty {label}")
    if len(value.encode("utf-8")) > limit:
        raise VerifyError(f"verify bound exceeded: {label} over {limit} bytes")
    return value


# ---------------------------------------------------------------------------
# Toolchain and repository measurement.
# ---------------------------------------------------------------------------


def find_mncs(explicit: str | None, repo: Path) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    for variable in ("MNCS", "MNCS_BINARY"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)
    sibling = repo.parent / "mncs-language"
    candidates.append(str(sibling / "target" / "debug" / "mncs"))
    candidates.append(str(sibling / "target" / "release" / "mncs"))
    located = shutil.which("mncs")
    if located:
        candidates.append(located)
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise VerifyError(
        "no executable mncs toolchain found (set MNCS or pass --mncs)"
    )


def toolchain_identity(mncs: str, cache_dir: Path) -> tuple[str, str, bool]:
    """Identify the toolchain binary as version + content digest (cached).

    Returns (identity, version, probed): probed is True when the version
    subprocess actually ran (a cache hit runs nothing).
    """
    binary = Path(mncs)
    try:
        stat = binary.stat()
    except OSError as error:
        raise VerifyError(f"toolchain is unavailable: {error}") from error
    cache_path = cache_dir / ".toolchain.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("path") == str(binary)
        and cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and isinstance(cached.get("identity"), str)
        and isinstance(cached.get("version"), str)
    ):
        return cached["identity"], cached["version"], False
    try:
        completed = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerifyError(f"toolchain version probe failed: {error}") from error
    version = (completed.stdout.strip() or completed.stderr.strip() or "mncs-unknown")[:128]
    digest = hashlib.sha256()
    try:
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise VerifyError(f"toolchain digest failed: {error}") from error
    identity = sha256_hex(f"{version}\n{digest.hexdigest()}".encode())
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache_path, json.dumps(
        {
            "path": str(binary),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "version": version,
            "identity": identity,
        }
    ))
    return identity, version, True


def git_head(repo: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerifyError(f"git revision measurement failed: {error}") from error
    if head.returncode != 0 or not head.stdout.strip():
        raise VerifyError("repository is not a git checkout (revision unmeasurable)")
    return head.stdout.strip()


def git_status_lines(repo: Path) -> list[str]:
    try:
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=normal"],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerifyError(f"git status measurement failed: {error}") from error
    if status.returncode != 0:
        raise VerifyError("git status measurement failed")
    return status.stdout.splitlines()


def verifier_key() -> str:
    """Digest the verifier's own implementation (tools + native + descriptors).

    The verifier is trust base: any change to the measurement, transport,
    policy-invocation, or digest implementation must invalidate prior
    receipts, even when the target worktree is untouched.
    """
    root = Path(__file__).resolve().parents[1]
    members = [
        "tools/mncs_test_verify.py",
        "tools/mncs_test_digest.py",
        "tools/mncs_test_native.py",
        "native/mncs/test/digest.mncs",
        "native/mncs/test/verification_coherence.mncs",
        "native-applications/test-digest.json",
        "native-applications/test-verification-coherence.json",
    ]
    digest = hashlib.sha256(VERIFY_VERSION.encode())
    for member in members:
        try:
            digest.update(f"\n{member}\n".encode())
            digest.update((root / member).read_bytes())
        except OSError as error:
            raise VerifyError(f"verifier implementation is unreadable: {member}") from error
    return digest.hexdigest()


def _status_paths(line: str) -> list[str]:
    body = line[3:] if len(line) > 3 else ""
    if " -> " in body:
        return [part.strip().strip('"') for part in body.split(" -> ")]
    return [body.strip().strip('"')]


def repository_fingerprint(
    repo: Path,
    inventory_relpath: str,
    outside_roots: list[str],
    stats: dict,
) -> tuple[str, str]:
    """Measure (revision, fingerprint) with irrelevant-change tolerance.

    The fingerprint binds the revision, the status of every file that can
    influence a native run (MNCS sources anywhere in the repo plus the
    verification manifests), the verifier implementation, and the content
    of library roots outside the repo. Documentation, scripts, and other
    non-source worktree changes provably cannot affect module resolution
    or suite semantics, so they preserve reuse. Anything else fails
    closed to unmeasurable.
    """
    revision = git_head(repo)
    stats["subprocesses"] += 1
    lines = git_status_lines(repo)
    stats["subprocesses"] += 1
    watched = {".mncs/project.json", inventory_relpath}
    kept = sorted(
        line
        for line in lines
        if any(path.endswith(".mncs") or path in watched for path in _status_paths(line))
    )
    filtered = "\n".join(kept)
    if len(filtered.encode("utf-8")) > MAX_STATUS_BYTES:
        raise VerifyError("relevant git status exceeds the measurement bound")
    outside_bits: list[str] = []
    byte_count = 0
    for raw in sorted(set(outside_roots)):
        root = Path(raw)
        if not root.is_dir():
            outside_bits.append(f"missing:{raw}")
            continue
        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                content = path.read_bytes()
            except OSError as error:
                raise VerifyError(f"outside library root is unreadable: {raw}") from error
            byte_count += len(content)
            if byte_count > MAX_DEP_BYTES:
                raise VerifyError("outside library roots exceed the measurement bound")
            entries.append(f"{path.relative_to(root).as_posix()}={sha256_hex(content)}")
        outside_bits.append(f"root:{raw}\n" + "\n".join(entries))
    fingerprint = sha256_hex(
        "\n".join([revision, filtered, verifier_key(), *outside_bits]).encode()
    )
    return revision, fingerprint


# ---------------------------------------------------------------------------
# Obligation inventory and dependency measurement.
# ---------------------------------------------------------------------------


def load_obligations(repo: Path) -> tuple[str, list[dict]]:
    manifest_path = repo / ".mncs" / "project.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(f"repository manifest is unavailable: {error}") from error
    if not isinstance(manifest, dict):
        raise VerifyError("repository manifest must be an object")
    repository = manifest.get("repository")
    if not isinstance(repository, str) or not repository:
        raise VerifyError("repository manifest names no repository")
    verification = manifest.get("verification")
    inventory_name = verification.get("obligation_inventory") if isinstance(verification, dict) else None
    if not isinstance(inventory_name, str) or not inventory_name:
        raise VerifyError("repository manifest names no obligation inventory")
    relative = Path(inventory_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise VerifyError("obligation inventory path escapes the repository")
    try:
        inventory = json.loads((repo / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(f"obligation inventory is unavailable: {error}") from error
    if not isinstance(inventory, dict):
        raise VerifyError("obligation inventory must be an object")
    if inventory.get("schema_version") != OBLIGATION_INVENTORY_SCHEMA:
        raise VerifyError("obligation inventory has an invalid schema version")
    if inventory.get("repository") != repository:
        raise VerifyError("obligation inventory names a different repository")
    obligations = inventory.get("obligations")
    if not isinstance(obligations, list) or not obligations:
        raise VerifyError("obligation inventory lists no obligations")
    for obligation in obligations:
        if not isinstance(obligation, dict) or not isinstance(obligation.get("identity"), str):
            raise VerifyError("obligation inventory entry must carry a string identity")
    return repository, obligations, inventory_name


def measure_invalidation(repo: Path, obligation: dict) -> str:
    """Digest the obligation's invalidation closure (bounded, content-based)."""
    dependencies = obligation.get("invalidation_dependencies", [])
    if not isinstance(dependencies, list):
        raise VerifyError("invalidation dependencies must be a list")
    entries: list[str] = []
    file_count = 0
    byte_count = 0
    for raw in dependencies:
        if not isinstance(raw, str) or not raw:
            raise VerifyError("invalidation dependency must be a non-empty path")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            entries.append(f"escape:{raw}")
            continue
        target = repo / relative
        if target.is_file():
            digest = _file_digest(target)
            byte_count += target.stat().st_size
            file_count += 1
            entries.append(f"file:{raw}={digest}")
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                name = path.relative_to(repo).as_posix()
                entries.append(f"file:{name}={_file_digest(path)}")
                byte_count += path.stat().st_size
                file_count += 1
                if file_count > MAX_DEP_FILES or byte_count > MAX_DEP_BYTES:
                    raise VerifyError("invalidation closure exceeds the measurement bound")
        else:
            entries.append(f"missing:{raw}")
        if file_count > MAX_DEP_FILES or byte_count > MAX_DEP_BYTES:
            raise VerifyError("invalidation closure exceeds the measurement bound")
    return sha256_hex("\n".join(entries).encode())


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Compiler-owned inventory measurement (no execution).
# ---------------------------------------------------------------------------


def obligation_libraries(obligation: dict, repo: Path, adapter_native: Path) -> list[str]:
    """Resolve the obligation's library roots plus the runner's native root."""
    executor = obligation.get("executor", {})
    declared = executor.get("library_paths", []) if isinstance(executor, dict) else []
    if not isinstance(declared, list):
        raise VerifyError("obligation library paths must be a list")
    roots: list[str] = []
    for raw in declared:
        if not isinstance(raw, str) or not raw:
            raise VerifyError("obligation library path must be a non-empty string")
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else (repo / candidate)
        roots.append(str(resolved))
    roots.append(str(repo))
    roots.append(str(adapter_native))
    return roots


def inventory_environment(base: dict[str, str], libraries: list[str]) -> dict[str, str]:
    environment = dict(base)
    environment["MNCS_LIBRARY_PATH"] = ":".join(libraries)
    return environment


def measure_inventory(
    mncs: str,
    repo: Path,
    source: str,
    libraries: list[str],
    base_environment: dict[str, str],
    timeout_seconds: float,
) -> dict:
    """Run `mncs test-inventory` and return the validated compiler report."""
    relative = Path(source)
    if relative.is_absolute() or ".." in relative.parts:
        raise VerifyError("obligation source path escapes the repository")
    if not (repo / relative).is_file():
        raise VerifyError(f"obligation source is missing: {source}")
    try:
        completed = subprocess.run(
            [mncs, "test-inventory", str(relative)],
            cwd=str(repo),
            env=inventory_environment(base_environment, libraries),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerifyError(f"compiler inventory failed: {error}") from error
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr.strip() or completed.stdout.strip())[:MAX_STDERR_TEXT]
        raise VerifyError(f"compiler inventory returned no report: {detail}") from None
    if not isinstance(report, dict) or not report.get("valid", False):
        diagnostics = report.get("diagnostics", []) if isinstance(report, dict) else []
        notes: list[str] = []
        if isinstance(diagnostics, list):
            for diagnostic in diagnostics[:4]:
                if isinstance(diagnostic, dict):
                    notes.append(
                        f"{diagnostic.get('code')}:{diagnostic.get('stage')}:"
                        f"{str(diagnostic.get('message', ''))[:160]}"
                    )
        raise VerifyError(
            "compiler inventory is invalid for "
            f"{source} ({'; '.join(notes) if notes else 'no diagnostics'})"
        )
    inventory = report.get("inventory")
    if not isinstance(inventory, dict):
        raise VerifyError("compiler inventory report carries no inventory")
    return report


# ---------------------------------------------------------------------------
# Receipts: content-addressed, atomically written, self-verifying.
# ---------------------------------------------------------------------------


def receipt_path(receipts_dir: Path, identity: str) -> Path:
    return receipts_dir / f"{sha256_hex(identity.encode())[:32]}.json"


def receipt_core(receipt: dict) -> dict:
    """The timeless evidence core: everything the evidence_id covers.

    Run-anchored fields (timestamps, artifact directories, transitions,
    producer labels) live only in the file receipt. The Store vaults the
    core, so identical evidence re-admits as DUPLICATE instead of
    conflicting.
    """
    return {
        "schema": RECEIPT_SCHEMA,
        "obligation_identity": receipt.get("obligation_identity"),
        "verdict": receipt.get("verdict"),
        "bound": receipt.get("bound"),
        "verifier_identity": receipt.get("verifier_identity"),
        "selected_test_identities": receipt.get("selected_test_identities"),
        "digest": receipt.get("digest"),
        "result_sha256": receipt.get("result_sha256"),
    }


def evidence_id_for(core: dict) -> str:
    return sha256_hex(canonical_json(core))


def read_receipt(receipts_dir: Path, identity: str) -> dict | None:
    """Read one receipt; corrupt or foreign receipts are ignored (re-execute)."""
    path = receipt_path(receipts_dir, identity)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        return None
    if receipt.get("obligation_identity") != identity:
        return None
    if receipt.get("verdict") not in EVIDENCE_VERDICTS:
        return None
    bound = receipt.get("bound")
    digest = receipt.get("digest")
    if not isinstance(bound, dict) or not isinstance(digest, dict):
        return None
    if receipt.get("evidence_id") != evidence_id_for(receipt_core(receipt)):
        return None
    return receipt


def write_receipt(receipts_dir: Path, receipt: dict) -> Path:
    identity = receipt.get("obligation_identity")
    if not isinstance(identity, str) or not identity:
        raise VerifyError("receipt names no obligation")
    path = receipt_path(receipts_dir, identity)
    atomic_write_text(path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------------------
# Store admission (provider-owned EmbeddedStore; degraded file-only fallback).
# ---------------------------------------------------------------------------


def load_store_api() -> tuple[object | None, str | None]:
    """Import the Store-owned API the way the environment does, or explain."""
    override = os.environ.get("MNCS_STORE_PACKAGE_DIR")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    here = Path(__file__).resolve()
    for parent in (here.parents[2], here.parents[3]):
        candidates.append(parent / "mncs-store" / "python")
    for candidate in candidates:
        if (candidate / "mncs_store" / "__init__.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                from mncs_store.embedded import EmbeddedStore  # noqa: E402
                from mncs_store.errors import StoreError, StoreResultCode  # noqa: E402
            except ImportError as error:
                return None, f"store package import failed: {error}"
            return (EmbeddedStore, StoreError, StoreResultCode), None
    return None, "mncs-store checkout is unavailable (set MNCS_STORE_PACKAGE_DIR)"


def admit_receipt_to_store(
    store_dir: Path,
    receipt: dict,
    relations: list[bytes],
    provenance: list[bytes],
) -> dict:
    """Admit one receipt to the repo-local Store with CAS retries."""
    outcome: dict = {"status": "skipped", "detail": None, "generation": None}
    loaded, reason = load_store_api()
    if loaded is None:
        outcome["detail"] = reason
        return outcome
    EmbeddedStore, StoreError, StoreResultCode = loaded
    # Content-derived domain identity over the timeless core: each
    # distinct evidence core is its own immutable object, so identical
    # evidence re-admits as DUPLICATE and re-execution never rebinds an
    # identity (which the Store correctly refuses). File receipts are the
    # mutable obligation -> evidence_id index; the Store is the vault.
    domain_identity = receipt["evidence_id"].encode()
    payload = canonical_json(receipt_core(receipt))
    descriptor = canonical_json(receipt.get("digest", {}))
    try:
        store = EmbeddedStore(store_dir)
    except Exception as error:  # Store-owned error taxonomy, transported as text.
        outcome["detail"] = f"store open failed: {error}"
        return outcome
    try:
        for _ in range(STORE_CAS_RETRIES + 1):
            try:
                expected = store.current_generation
                result = store.put_bound_object(
                    domain_schema=STORE_RECEIPT_SCHEMA,
                    domain_identity=domain_identity,
                    descriptor=descriptor,
                    payload=payload,
                    expected_generation=expected,
                    relations=relations,
                    provenance=provenance,
                )
            except StoreError as error:
                outcome["detail"] = f"store admit failed: {error}"
                return outcome
            code = result.code
            if code == StoreResultCode.COMMITTED:
                outcome["status"] = "admitted"
                outcome["generation"] = result.generation
                return outcome
            if code == StoreResultCode.DUPLICATE:
                outcome["status"] = "duplicate"
                outcome["generation"] = result.generation
                return outcome
            if code == StoreResultCode.STALE_GENERATION:
                continue
            outcome["detail"] = f"store admit returned {code}"
            return outcome
        outcome["detail"] = "store admit exhausted CAS retries"
        return outcome
    finally:
        try:
            store.close()
        except Exception:
            pass


def verify_receipt_in_store(store_dir: Path, receipt: dict) -> tuple[bool, str]:
    """Confirm the Store holds the same receipt bytes (mismatch is conflict)."""
    loaded, reason = load_store_api()
    if loaded is None:
        return True, f"store unavailable: {reason}"
    EmbeddedStore, StoreError, _codes = loaded
    try:
        store = EmbeddedStore(store_dir, read_only=True)
    except Exception as error:
        return True, f"store open failed: {error}"
    try:
        try:
            stored = store.get_bound_object(
                STORE_RECEIPT_SCHEMA, receipt["evidence_id"].encode()
            )
        except StoreError:
            return True, "no store object yet"
        except Exception as error:
            return True, f"store read failed: {error}"
        if stored.payload != canonical_json(receipt_core(receipt)):
            return False, "store object differs from the file receipt"
        return True, "store object matches"
    finally:
        try:
            store.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Coherence rows: host-measured world plus recorded evidence.
# ---------------------------------------------------------------------------


def empty_evidence() -> dict:
    return {
        "definition_identity": "",
        "subject_identity": "",
        "subject_fingerprint": "",
        "executor_identity": "",
        "verifier_identity": "",
        "invalidation_identity": "",
        "toolchain_identity": "",
        "inventory_identity": "",
        "repository_revision": "",
        "repository_fingerprint": "",
        "verdict": "UNKNOWN",
        "evidence_id": "",
    }


def empty_external_evidence() -> dict:
    return {"obligation": "", "subject_digest": "", "verdict": "UNKNOWN", "evidence_id": ""}


def build_coherence_row(
    obligation: dict,
    bound: dict[str, str],
    inventory_identities: list[str],
    inventory_truncated: bool,
    receipt: dict | None,
    provider_available: bool,
) -> dict:
    lifecycle = obligation.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        raise VerifyError(f"obligation has an unknown lifecycle: {lifecycle!r}")
    executor = obligation.get("executor", {})
    kind = executor.get("kind") if isinstance(executor, dict) else None
    if kind not in EXECUTOR_KINDS:
        raise VerifyError(f"obligation has an unknown executor kind: {kind!r}")
    patterns = executor.get("declaration_identities", ["*"]) if isinstance(executor, dict) else ["*"]
    if not isinstance(patterns, list) or not patterns:
        patterns = ["*"]
    if len(patterns) > MAX_PATTERNS:
        raise VerifyError("obligation declares more patterns than the native bound")
    for pattern in patterns:
        bound_text(pattern, "declared pattern", MAX_IDENTITY_BYTES)
    if len(inventory_identities) > MAX_IDENTITIES:
        raise VerifyError("obligation inventory exceeds the native identity bound")
    for identity in inventory_identities:
        bound_text(identity, "inventory test identity", MAX_IDENTITY_BYTES)
    evidence = empty_evidence()
    evidence_present = False
    if receipt is not None:
        stored = receipt.get("bound", {})
        for key in (
            "definition_identity",
            "subject_identity",
            "subject_fingerprint",
            "executor_identity",
            "invalidation_identity",
            "toolchain_identity",
            "inventory_identity",
            "repository_revision",
            "repository_fingerprint",
        ):
            evidence[key] = stored.get(key, "")
        evidence["verifier_identity"] = receipt.get("verifier_identity", "")
        evidence["verdict"] = receipt.get("verdict", "UNKNOWN")
        evidence["evidence_id"] = receipt.get("evidence_id", "")
        evidence_present = True
    runnable_native = kind == NATIVE_KIND and len(
        (executor.get("source_paths", []) if isinstance(executor, dict) else [])
    ) == 1
    return {
        "identity": obligation["identity"],
        "lifecycle": lifecycle,
        "executor_kind": kind,
        "runnable_native": runnable_native,
        "external_evidence_present": False,
        "external_evidence": empty_external_evidence(),
        "current": dict(bound),
        "declared_patterns": list(patterns),
        "inventory_test_identities": list(inventory_identities),
        "inventory_truncated": inventory_truncated,
        "evidence_present": evidence_present,
        "evidence": evidence,
        "evidence_conflict": False,
        "provider_available": provider_available,
    }


def evaluate_coherence(
    rows: list[dict],
    *,
    mncs: str,
    cwd: Path,
    environment: dict[str, str],
    max_executions: int,
    timeout_seconds: float,
) -> dict:
    try:
        return run_native_app(
            "test-verification-coherence.json",
            {
                "schema_version": COHERENCE_REQUEST_SCHEMA,
                "max_executions": max_executions,
                "obligations": rows,
            },
            request_schema=COHERENCE_REQUEST_SCHEMA,
            result_schema=COHERENCE_RESULT_SCHEMA,
            mncs=mncs,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
    except NativeTransportError as error:
        raise VerifyError(str(error)) from error


# ---------------------------------------------------------------------------
# Suite execution through `mncs test` (identity-bound, file-captured).
# ---------------------------------------------------------------------------


def execute_suite(
    mncs: str,
    repo: Path,
    source: str,
    libraries: list[str],
    identities: list[str],
    artifacts_dir: Path,
    timeout_seconds: float,
) -> dict:
    """Execute one suite; return the execution record (never parse stdout)."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifacts_dir / "result.json"
    command = [mncs, "test", source]
    for root in libraries:
        command.extend(("--library", root))
    for identity in identities:
        command.extend(("--test-identity", identity))
    command.extend(("--result", str(result_path), "--format", "json"))
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"outcome": "infrastructure", "detail": f"suite supervision failed: {error}"}
    record: dict = {
        "outcome": "unknown",
        "detail": None,
        "exit_code": completed.returncode,
        "result_path": str(result_path),
    }
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result = None
    if result is None or not isinstance(result, dict):
        record["outcome"] = "infrastructure"
        record["detail"] = (
            completed.stderr.strip() or completed.stdout.strip() or "no result record"
        )[:MAX_STDERR_TEXT]
        return record
    if result.get("schema_version") != "mncs.test-result/1":
        record["outcome"] = "infrastructure"
        record["detail"] = "suite returned an invalid result schema"
        return record
    summary = result.get("summary", {})
    verdict = summary.get("verdict") if isinstance(summary, dict) else None
    if completed.returncode == 0 and verdict == "PASS":
        record["outcome"] = "pass"
    elif completed.returncode == 1 and verdict == "FAIL":
        record["outcome"] = "fail"
    elif verdict == "UNSUPPORTED":
        record["outcome"] = "unsupported"
        record["detail"] = "suite verdict is UNSUPPORTED"
    else:
        record["outcome"] = "infrastructure"
        record["detail"] = (
            f"exit {completed.returncode} disagrees with verdict {verdict!r}"
        )
    record["result"] = result
    return record


def collect_garbage(artifacts_root: Path, keep: int = ARTIFACT_KEEP) -> int:
    """Retain the newest artifact runs; return the number removed."""
    if not artifacts_root.is_dir():
        return 0
    runs = sorted(
        (path for path in artifacts_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for stale in runs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Family verification orchestration.
# ---------------------------------------------------------------------------


def verify_repository(
    repo: Path,
    *,
    mncs: str,
    max_executions: int = 16,
    suite_timeout: float = 600.0,
    inventory_timeout: float = 120.0,
    coherence_timeout: float = 120.0,
    max_failures: int = 8,
    admit_store: bool = True,
) -> dict:
    repo = repo.resolve()
    receipts_dir = repo / ".mncs" / "test-receipts"
    artifacts_root = repo / ".mncs" / "test-artifacts"
    store_dir = repo / ".mncs" / "test-store"
    adapter_native = Path(__file__).resolve().parents[1] / "native"
    base_environment = dict(os.environ)
    stats = {
        "subprocesses": 0,
        "inventory_runs": 0,
        "suite_runs": 0,
        "coherence_runs": 0,
        "digest_runs": 0,
    }

    repository, obligations, inventory_relpath = load_obligations(repo)
    entries: dict[str, dict] = {}
    measurable: list[dict] = []
    for obligation in obligations:
        identity = obligation["identity"]
        if len(measurable) >= MAX_OBLIGATIONS:
            entries[identity] = {
                "identity": identity,
                "status": "beyond_bound",
                "reason": f"inventory exceeds the native bound of {MAX_OBLIGATIONS}",
                "action": "unresolved",
                "verdict": None,
            }
        else:
            measurable.append(obligation)

    try:
        toolchain, toolchain_version, toolchain_probed = toolchain_identity(mncs, receipts_dir)
        stats["subprocesses"] += 1 if toolchain_probed else 0
        provider_available = True
        toolchain_note = None
    except VerifyError as error:
        toolchain, toolchain_version = "unmeasurable", "unknown"
        provider_available = False
        toolchain_note = str(error)
    outside_roots: list[str] = []
    for obligation in obligations:
        executor = obligation.get("executor", {})
        declared = executor.get("library_paths", []) if isinstance(executor, dict) else []
        if isinstance(declared, list):
            for raw in declared:
                if not isinstance(raw, str) or not raw:
                    continue
                candidate = Path(raw)
                resolved = candidate if candidate.is_absolute() else (repo / candidate)
                try:
                    resolved.resolve().relative_to(repo)
                except ValueError:
                    outside_roots.append(str(resolved))
    try:
        revision, repo_fingerprint = repository_fingerprint(
            repo, inventory_relpath, outside_roots, stats
        )
        git_note = None
    except VerifyError as error:
        revision, repo_fingerprint = "unknown", "unmeasurable"
        git_note = str(error)

    measured: dict[str, dict] = {}
    for obligation in measurable:
        identity = obligation["identity"]
        try:
            if git_note is not None:
                raise VerifyError(git_note)
            measured[identity] = measure_obligation(
                obligation,
                repo,
                adapter_native,
                base_environment,
                mncs,
                toolchain,
                revision,
                repo_fingerprint,
                receipts_dir,
                inventory_timeout,
                stats,
            )
        except VerifyError as error:
            entries[identity] = {
                "identity": identity,
                "status": "measurement_failed",
                "reason": str(error),
                "action": "unresolved",
                "verdict": None,
            }

    rows: list[dict] = []
    row_order: list[str] = []
    for identity, world in measured.items():
        try:
            rows.append(
                build_coherence_row(
                    world["obligation"],
                    world["bound"],
                    world["inventory_identities"],
                    world["inventory_truncated"],
                    world["receipt"],
                    provider_available,
                )
            )
            row_order.append(identity)
        except VerifyError as error:
            entries[identity] = {
                "identity": identity,
                "status": "measurement_failed",
                "reason": str(error),
                "action": "unresolved",
                "verdict": None,
            }
            del measured[identity]

    coherence: dict = {"verdicts": [], "run_queue": [], "summary": {}}
    if rows:
        stats["subprocesses"] += 1
        stats["coherence_runs"] += 1
        coherence = evaluate_coherence(
            rows,
            mncs=mncs,
            cwd=repo,
            environment=base_environment,
            max_executions=max_executions,
            timeout_seconds=coherence_timeout,
        )
    verdicts = {
        item["identity"]: item
        for item in coherence.get("verdicts", [])
        if isinstance(item, dict) and isinstance(item.get("identity"), str)
    }
    run_queue = set(coherence.get("run_queue", []))

    for identity in row_order:
        world = measured[identity]
        verdict = verdicts.get(identity)
        if verdict is None:
            entries[identity] = {
                "identity": identity,
                "status": "coherence_missing",
                "reason": "native policy returned no verdict for the obligation",
                "action": "unresolved",
                "verdict": None,
            }
            continue
        entries[identity] = act_on_verdict(
            world,
            verdict,
            identity in run_queue,
            repo,
            mncs,
            receipts_dir,
            artifacts_root,
            store_dir,
            base_environment,
            suite_timeout,
            max_failures,
            admit_store,
            stats,
        )

    removed = collect_garbage(artifacts_root)
    return assemble_report(
        repository,
        revision,
        obligations,
        entries,
        measured,
        coherence.get("summary", {}),
        stats,
        removed,
        toolchain_version,
        toolchain_note,
        git_note,
        max_executions,
    )


def measure_obligation(
    obligation: dict,
    repo: Path,
    adapter_native: Path,
    base_environment: dict[str, str],
    mncs: str,
    toolchain: str,
    revision: str,
    repo_fingerprint: str,
    receipts_dir: Path,
    inventory_timeout: float,
    stats: dict,
) -> dict:
    identity = bound_text(obligation["identity"], "obligation identity", MAX_IDENTITY_BYTES)
    executor = obligation.get("executor", {})
    if not isinstance(executor, dict):
        raise VerifyError("obligation executor must be an object")
    kind = executor.get("kind")
    if kind not in EXECUTOR_KINDS:
        raise VerifyError(f"obligation has an unknown executor kind: {kind!r}")
    definition = sha256_hex(canonical_json(obligation))
    executor_identity = sha256_hex(canonical_json(executor))
    invalidation = measure_invalidation(repo, obligation)
    sources = executor.get("source_paths", [])
    if not isinstance(sources, list):
        raise VerifyError("obligation source paths must be a list")
    libraries = obligation_libraries(obligation, repo, adapter_native)
    inventory_identities: list[str] = []
    inventory_truncated = False
    test_count = 0
    if kind == NATIVE_KIND and len(sources) == 1 and isinstance(sources[0], str):
        stats["subprocesses"] += 1
        stats["inventory_runs"] += 1
        report = measure_inventory(
            mncs, repo, sources[0], libraries, base_environment, inventory_timeout
        )
        inventory = report["inventory"]
        subject_identity = bound_text(
            inventory.get("subject_identity"), "subject identity", MAX_IDENTITY_BYTES
        )
        subject_fingerprint = bound_text(
            inventory.get("subject_fingerprint"), "subject fingerprint", MAX_FINGERPRINT_BYTES
        )
        inventory_identity = sha256_hex(canonical_json(inventory))
        raw_tests = inventory.get("tests", [])
        if not isinstance(raw_tests, list):
            raise VerifyError("compiler inventory tests must be a list")
        test_count = len(raw_tests)
        for item in raw_tests[:MAX_IDENTITIES]:
            if not isinstance(item, dict):
                raise VerifyError("compiler inventory test must be an object")
            inventory_identities.append(
                bound_text(
                    item.get("test_case_identity"), "test case identity", MAX_IDENTITY_BYTES
                )
            )
        inventory_truncated = len(raw_tests) > MAX_IDENTITIES
    else:
        entrypoint = executor.get("entrypoint", kind)
        subject_identity = bound_text(
            f"executor:{kind}:{entrypoint}", "executor subject", MAX_IDENTITY_BYTES
        )
        subject_fingerprint = executor_identity[:MAX_FINGERPRINT_BYTES]
        inventory_identity = "no-native-inventory"
    bound = {
        "definition_identity": definition,
        "subject_identity": subject_identity,
        "subject_fingerprint": subject_fingerprint,
        "executor_identity": executor_identity,
        "invalidation_identity": invalidation,
        "toolchain_identity": toolchain,
        "inventory_identity": inventory_identity,
        "repository_revision": revision,
        "repository_fingerprint": repo_fingerprint,
    }
    return {
        "obligation": obligation,
        "bound": bound,
        "libraries": libraries,
        "inventory_identities": inventory_identities,
        "inventory_truncated": inventory_truncated,
        "test_count": test_count,
        "receipt": read_receipt(receipts_dir, identity),
    }


def required_selection(world: dict) -> list[str] | None:
    """Prove the currently required selection, or None when unprovable.

    Only exact reasoning is allowed: wildcard requires the full current
    inventory, and any other pattern must exactly equal one current
    inventory identity. No globbing, no substring matching.
    """
    executor = world["obligation"].get("executor", {})
    patterns = executor.get("declaration_identities", ["*"])
    if not isinstance(patterns, list) or not patterns:
        patterns = ["*"]
    inventory = world["inventory_identities"]
    if patterns == ["*"]:
        return list(inventory)
    required: list[str] = []
    for pattern in patterns:
        if pattern == "*":
            return list(inventory)
        if pattern in inventory:
            if pattern not in required:
                required.append(pattern)
        else:
            return None
    return required


def act_on_verdict(
    world: dict,
    verdict: dict,
    queued: bool,
    repo: Path,
    mncs: str,
    receipts_dir: Path,
    artifacts_root: Path,
    store_dir: Path,
    base_environment: dict[str, str],
    suite_timeout: float,
    max_failures: int,
    admit_store: bool,
    stats: dict,
) -> dict:
    obligation = world["obligation"]
    identity = obligation["identity"]
    status = verdict.get("status", "unknown")
    reason = verdict.get("reason", "unknown")
    resolved = [item for item in verdict.get("resolved_test_identities", []) if item]
    entry: dict = {
        "identity": identity,
        "status": status,
        "reason": reason,
        "action": "unresolved",
        "verdict": None,
        "verdict_source": None,
        "transition": None,
        "tests_considered": world["test_count"],
        "resolved_test_identities": resolved,
        "notes": [],
    }
    if status == "current":
        receipt = world["receipt"]
        if receipt is None:  # Native policy trusts evidence we cannot read: re-check.
            entry["notes"].append("current without a readable receipt; treating as unresolved")
            return entry
        # The verifier itself is trust base: a receipt from another producer
        # version never reuses, even when the bound world matches.
        if receipt.get("producer") != VERIFY_VERSION:
            entry["notes"].append(
                "receipt producer differs from this verifier; re-executing"
            )
            required = required_selection(world)
            if required is None:
                entry["notes"].append("required selection is unprovable; leaving unresolved")
                return entry
            return execute_bound_suite(
                world, entry, repo, mncs, receipts_dir, artifacts_root, store_dir,
                suite_timeout, max_failures, admit_store, stats, list(required),
            )
        # A `current` verdict carries no resolved selection (nothing to
        # execute), so the host proves selection equivalence itself: the
        # recorded selection must cover exactly the currently required
        # selection. Required selection is provable only for wildcard and
        # exact-identity patterns; anything else refuses reuse.
        required = required_selection(world)
        if required is None:
            entry["notes"].append(
                "declared selection uses patterns the host cannot prove; "
                "refusing reuse without re-execution"
            )
            return entry
        recorded = receipt.get("selected_test_identities")
        if not isinstance(recorded, list) or sorted(recorded) != sorted(required):
            entry["notes"].append(
                "recorded selection differs from the required selection; re-executing"
            )
            return execute_bound_suite(
                world, entry, repo, mncs, receipts_dir, artifacts_root, store_dir,
                suite_timeout, max_failures, admit_store, stats, list(required),
            )
        if admit_store:
            store_dir.mkdir(parents=True, exist_ok=True)
            match, note = verify_receipt_in_store(store_dir, receipt)
            entry["notes"].append(f"store cross-check: {note}")
            if not match:
                entry["status"] = "contradictory"
                entry["reason"] = "evidence_conflict"
                entry["notes"].append("file receipt and store object disagree; refusing reuse")
                return entry
            if note == "no store object yet":
                # A receipt that never reached the vault (crash between
                # file write and admission, or an older producer): backfill
                # it now. Admission is idempotent on the evidence core.
                entry["store"] = admit_receipt_to_store(
                    store_dir, receipt, relations=[], provenance=[]
                )
        entry["action"] = "reused"
        entry["verdict"] = receipt["verdict"]
        entry["verdict_source"] = "reused"
        entry["digest"] = receipt.get("digest")
        entry["evidence_id"] = receipt.get("evidence_id")
        return entry
    if status in ("new_execution_required", "stale") and queued and not verdict.get(
        "deferred", False
    ):
        return execute_bound_suite(
            world, entry, repo, mncs, receipts_dir, artifacts_root, store_dir,
            suite_timeout, max_failures, admit_store, stats, resolved,
        )
    if verdict.get("deferred", False):
        entry["action"] = "deferred"
        return entry
    return entry


def execute_bound_suite(
    world: dict,
    entry: dict,
    repo: Path,
    mncs: str,
    receipts_dir: Path,
    artifacts_root: Path,
    store_dir: Path,
    suite_timeout: float,
    max_failures: int,
    admit_store: bool,
    stats: dict,
    resolved: list[str],
) -> dict:
    obligation = world["obligation"]
    identity = obligation["identity"]
    executor = obligation.get("executor", {})
    kind = executor.get("kind")
    sources = executor.get("source_paths", [])
    if kind != NATIVE_KIND:
        entry["action"] = "not_executed"
        entry["notes"].append(f"executor kind {kind} is not natively executable")
        return entry
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], str):
        entry["action"] = "not_executed"
        entry["notes"].append("native obligation has no single-source runnable binding")
        return entry
    if not resolved:
        entry["action"] = "not_executed"
        entry["notes"].append("resolved selection is empty; refusing to broaden silently")
        return entry
    run_stamp = f"{time.time_ns():x}"
    artifacts_dir = artifacts_root / f"{sha256_hex(identity.encode())[:16]}-{run_stamp}"
    stats["subprocesses"] += 1
    stats["suite_runs"] += 1
    execution = execute_suite(
        mncs, repo, sources[0], world["libraries"], resolved, artifacts_dir, suite_timeout
    )
    return finish_execution(
        world, entry, execution, artifacts_dir, receipts_dir, store_dir,
        mncs, repo, max_failures, admit_store, stats, resolved,
    )


def finish_execution(
    world: dict,
    entry: dict,
    execution: dict,
    artifacts_dir: Path,
    receipts_dir: Path,
    store_dir: Path,
    mncs: str,
    repo: Path,
    max_failures: int,
    admit_store: bool,
    stats: dict,
    resolved: list[str],
) -> dict:
    obligation = world["obligation"]
    identity = obligation["identity"]
    outcome = execution.get("outcome", "unknown")
    entry["execution"] = {
        key: execution.get(key)
        for key in ("outcome", "detail", "exit_code", "result_path")
    }
    if outcome in ("pass", "fail", "unsupported"):
        result = execution["result"]
        stats["subprocesses"] += 1
        stats["digest_runs"] += 1
        try:
            request = project_digest_request(result, max_failures=max_failures)
            digest = evaluate_digest(
                request, mncs=mncs, cwd=repo,
                environment=inventory_environment(dict(os.environ), world["libraries"]),
            )
        except (DigestError, VerifyError, NativeTransportError) as error:
            entry["action"] = "unresolved"
            entry["notes"].append(f"digest failed: {error}")
            return entry
        verdict = {"pass": "PASS", "fail": "FAIL", "unsupported": "UNKNOWN"}[outcome]
        previous = world["receipt"]
        previous_verdict = previous.get("verdict") if previous else None
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "obligation_identity": identity,
            "verdict": verdict,
            "bound": world["bound"],
            "verifier_identity": obligation.get("executor", {}).get(
                "verifier_identity", VERIFY_VERSION
            ),
            "selected_test_identities": resolved,
            "digest": digest,
            "result_sha256": sha256_hex(canonical_json(result)),
            "artifact_dir": artifacts_dir.name,
            "previous_verdict": previous_verdict,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "producer": VERIFY_VERSION,
        }
        receipt["evidence_id"] = evidence_id_for(receipt_core(receipt))
        write_receipt(receipts_dir, receipt)
        entry["action"] = "executed"
        entry["verdict"] = verdict
        entry["verdict_source"] = "executed"
        entry["digest"] = digest
        entry["evidence_id"] = receipt["evidence_id"]
        if previous_verdict == "PASS" and verdict == "FAIL":
            entry["transition"] = "regression"
        elif previous_verdict == "FAIL" and verdict == "PASS":
            entry["transition"] = "fixed"
        elif previous_verdict is None:
            entry["transition"] = "new"
        else:
            entry["transition"] = "stable"
        if admit_store:
            # Bare admission: the receipt payload already carries every
            # linkage field (obligation, subject, artifact, toolchain) and
            # the descriptor carries the digest. Typed Store relations use
            # a fixed-width taxonomy this client does not forge; revisit
            # when the relation vocabulary for test evidence is defined.
            entry["store"] = admit_receipt_to_store(
                store_dir, receipt, relations=[], provenance=[]
            )
        else:
            entry["store"] = {"status": "skipped", "detail": "store admission disabled"}
        return entry
    entry["action"] = "not_executed"
    entry["verdict"] = None
    entry["notes"].append(f"suite outcome is {outcome}: {execution.get('detail')}")
    return entry


# ---------------------------------------------------------------------------
# Report assembly and rendering.
# ---------------------------------------------------------------------------


def assemble_report(
    repository: str,
    revision: str,
    obligations: list[dict],
    entries: dict[str, dict],
    measured: dict[str, dict],
    coherence_summary: dict,
    stats: dict,
    artifacts_removed: int,
    toolchain_version: str,
    toolchain_note: str | None,
    git_note: str | None,
    max_executions: int,
) -> dict:
    ordered = [entries[obligation["identity"]] for obligation in obligations]
    tests_considered = sum(item["test_count"] for item in measured.values())
    summary = {
        "obligations": len(obligations),
        "tests_considered": tests_considered,
        "reused": 0,
        "executed": 0,
        "pass": 0,
        "fail": 0,
        "unknown": 0,
        "deferred": 0,
        "not_executed": 0,
        "unresolved": 0,
        "regressions": 0,
        "fixed": 0,
    }
    store = {"admitted": 0, "duplicate": 0, "skipped": 0}
    for entry in ordered:
        action = entry.get("action")
        if action == "reused":
            summary["reused"] += 1
        elif action == "executed":
            summary["executed"] += 1
        elif action == "deferred":
            summary["deferred"] += 1
        elif action == "not_executed":
            summary["not_executed"] += 1
        else:
            summary["unresolved"] += 1
        verdict = entry.get("verdict")
        if verdict == "PASS":
            summary["pass"] += 1
        elif verdict == "FAIL":
            summary["fail"] += 1
        elif verdict == "UNKNOWN" or (action in ("unresolved", "deferred", "not_executed")):
            summary["unknown"] += 1
        if entry.get("transition") == "regression":
            summary["regressions"] += 1
        if entry.get("transition") == "fixed":
            summary["fixed"] += 1
        outcome = (entry.get("store") or {}).get("status")
        if outcome in store:
            store[outcome] += 1
    if summary["fail"] > 0:
        overall = "FAIL"
    elif summary["unknown"] > 0 or summary["unresolved"] > 0:
        overall = "INCOMPLETE"
    else:
        overall = "PASS"
    return {
        "schema_version": REPORT_SCHEMA,
        "producer": VERIFY_VERSION,
        "repository": repository,
        "revision": revision,
        "overall": overall,
        "summary": summary,
        "coherence_summary": coherence_summary,
        "store": store,
        "stats": dict(stats),
        "artifacts_removed": artifacts_removed,
        "toolchain_version": toolchain_version,
        "toolchain_note": toolchain_note,
        "git_note": git_note,
        "max_executions": max_executions,
        "obligations": ordered,
    }


def render_report_text(report: dict) -> str:
    summary = report["summary"]
    lines = [
        (
            f"mncs-test verify: {report['repository']} @ {report['revision'][:12]} "
            f"({summary['obligations']} obligations, "
            f"{summary['tests_considered']} tests considered) -> {report['overall']}"
        ),
        (
            f"  reused: {summary['reused']} · executed: {summary['executed']} "
            f"· pass: {summary['pass']} · fail: {summary['fail']} "
            f"· unknown: {summary['unknown']} · deferred: {summary['deferred']} "
            f"· not-executed: {summary['not_executed']} · unresolved: {summary['unresolved']}"
        ),
    ]
    if summary["regressions"] or summary["fixed"]:
        lines.append(
            f"  transitions: {summary['regressions']} regression(s), "
            f"{summary['fixed']} fixed"
        )
    for entry in report["obligations"]:
        action = entry.get("action")
        verdict = entry.get("verdict") or "—"
        source = entry.get("verdict_source") or action
        line = f"  [{action}] {entry['identity']}: {verdict} ({source})"
        if entry.get("transition") in ("regression", "fixed"):
            line += f" *** {entry['transition'].upper()} ***"
        if action in ("unresolved", "not_executed", "deferred"):
            line += f" [{entry.get('status')}/{entry.get('reason')}]"
        lines.append(line)
        for note in entry.get("notes", [])[:3]:
            lines.append(f"    note: {note}")
        digest = entry.get("digest") or {}
        for failure in (digest.get("failures") or [])[:8]:
            lines.append(
                f"    FAIL {failure.get('qualified_name')} "
                f"expected={failure.get('expected')} actual={failure.get('actual')} "
                f"code={failure.get('assertion_code')} "
                f"{failure.get('source')}:{failure.get('line')}"
            )
        omitted = (digest.get("failures_omitted") or 0) if isinstance(digest, dict) else 0
        if omitted:
            lines.append(f"    ... {omitted} further failure(s) omitted (digest bound)")
    store = report.get("store", {})
    lines.append(
        f"  store: {store.get('admitted', 0)} admitted · "
        f"{store.get('duplicate', 0)} duplicate · {store.get('skipped', 0)} skipped"
    )
    stats = report.get("stats", {})
    lines.append(
        f"  cost: {stats.get('subprocesses', 0)} subprocesses "
        f"({stats.get('inventory_runs', 0)} inventory, {stats.get('suite_runs', 0)} suite, "
        f"{stats.get('coherence_runs', 0)} coherence, {stats.get('digest_runs', 0)} digest)"
    )
    return "\n".join(lines) + "\n"


def exit_code_for(report: dict) -> int:
    summary = report["summary"]
    if summary["fail"] > 0:
        return 1
    if summary["unknown"] > 0 or summary["unresolved"] > 0:
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="native-first family verification")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--mncs", default=None)
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument("--suite-timeout", type=float, default=600.0)
    parser.add_argument("--inventory-timeout", type=float, default=120.0)
    parser.add_argument("--max-failures", type=int, default=8)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report-out", type=Path, default=None)
    parser.add_argument("--no-store", action="store_true")
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(VERIFY_VERSION)
        return 0
    try:
        repo = arguments.repo.resolve()
        mncs = find_mncs(arguments.mncs, repo)
        report = verify_repository(
            repo,
            mncs=mncs,
            max_executions=arguments.max_executions,
            suite_timeout=arguments.suite_timeout,
            inventory_timeout=arguments.inventory_timeout,
            max_failures=arguments.max_failures,
            admit_store=not arguments.no_store,
        )
    except VerifyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if arguments.report_out is not None:
        arguments.report_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if arguments.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report_text(report), end="")
    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())




