"""Data_Layer architecture smoke test (task 3.10, Req 16.1).

A single-execution architectural check that:

1. A distinct abstract interface exists per persisted record type — the seven
   Data_Layer store interfaces, each an ``ABC`` with abstract methods.
2. Agent/tool business-logic code depends only on those interfaces, never on a
   concrete storage backend (``data_layer.memory`` / ``data_layer.dynamodb``).
   The composition roots that wire a concrete backend (``app.py``,
   ``deployment/*``, ``dashboard/bff.py``) are the only permitted importers.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import clinic_front_desk
from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    CallSessionStore,
    ClinicKnowledgeBaseStore,
    DecisionStore,
    EscalationStore,
    PatientStore,
    WaitlistStore,
)

pytestmark = pytest.mark.smoke

# One interface per persisted record type behind the Data_Layer (Req 16.1).
_STORE_INTERFACES = [
    AppointmentStore,
    PatientStore,
    WaitlistStore,
    DecisionStore,
    ClinicKnowledgeBaseStore,
    CallSessionStore,
    EscalationStore,
]

# Business-logic packages: agents, tools, orchestration, detectors, config.
# These must depend only on the interfaces, never on a storage implementation.
_AGENT_TOOL_PACKAGES = ["tools", "voice", "intelligence", "config"]

# Composition roots legitimately wire a concrete backend and are exempt.
_COMPOSITION_ROOTS = {"app.py", "runtime.py"}
_EXEMPT_DIRS = {"deployment", "data_layer"}
_FORBIDDEN_IMPORT_FRAGMENTS = ("data_layer.memory", "data_layer.dynamodb")


def test_distinct_interface_per_record_type() -> None:
    """Seven distinct abstract store interfaces exist, one per record type."""
    assert len(_STORE_INTERFACES) == 7
    assert len({iface.__name__ for iface in _STORE_INTERFACES}) == 7
    for iface in _STORE_INTERFACES:
        assert inspect.isabstract(iface), f"{iface.__name__} must be abstract (ABC)"
        assert iface.__abstractmethods__, f"{iface.__name__} must declare abstract methods"


def _iter_agent_tool_sources() -> list[Path]:
    root = Path(clinic_front_desk.__file__).parent
    files: list[Path] = []
    for package in _AGENT_TOOL_PACKAGES:
        pkg_dir = root / package
        if pkg_dir.is_dir():
            files.extend(p for p in pkg_dir.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def _imports_concrete_storage(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(frag in node.module for frag in _FORBIDDEN_IMPORT_FRAGMENTS):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(frag in alias.name for frag in _FORBIDDEN_IMPORT_FRAGMENTS):
                    return True
    return False


def test_agent_tool_code_depends_only_on_interfaces() -> None:
    """No agent/tool business-logic module imports a concrete storage backend."""
    sources = _iter_agent_tool_sources()
    assert sources, "expected to find agent/tool source files to scan"

    offenders = [
        str(path)
        for path in sources
        if _imports_concrete_storage(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "agent/tool code must depend only on Data_Layer interfaces, but these "
        f"modules import a concrete storage backend: {offenders}"
    )
