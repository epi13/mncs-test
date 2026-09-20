"""Lazy locator for family-owned Commons contracts.

This file contains no verification-plan vocabulary.  It only resolves the
transport dependency at the process boundary and fails closed when Commons
is not available.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence


def commons_root() -> Path:
    configured = os.environ.get("MNCS_COMMONS_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2] / "MNCS-Commons"


def verification_plan_contract() -> ModuleType:
    root = commons_root()
    source = root / "src"
    module_path = source / "mncs_commons" / "verification_plan.py"
    if not module_path.is_file():
        raise RuntimeError(
            "canonical MNCS-Commons verification-plan contract is unavailable; "
            "set MNCS_COMMONS_ROOT to a checked-out Commons repository"
        )
    name = "_mncs_commons_verification_plan_canonical"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load canonical verification-plan module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def obligation_plan_contract() -> ModuleType:
    root = commons_root()
    source = root / "src"
    module_path = source / "mncs_commons" / "obligation_plan.py"
    if not module_path.is_file():
        raise RuntimeError(
            "canonical MNCS-Commons obligation-plan contract is unavailable; "
            "set MNCS_COMMONS_ROOT to a checked-out Commons repository"
        )
    name = "_mncs_commons_obligation_plan_canonical"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load canonical obligation-plan module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_plan(value: Any, **kwargs: Any) -> dict[str, Any]:
    return verification_plan_contract().validate_plan(value, **kwargs)


def validate_inventory(value: Any, identities: Sequence[str]) -> dict[str, Any]:
    return verification_plan_contract().validate_plan(
        value,
        inventory_test_identities=identities,
    )


def validate_obligation_plan(value: Any) -> dict[str, Any]:
    return obligation_plan_contract().validate_obligation_plan(value)


def contract_vocab() -> tuple[tuple[str, ...], set[str]]:
    module = verification_plan_contract()
    return tuple(module.VERIFICATION_LEVELS), set(module.ESCALATION_REASONS)
