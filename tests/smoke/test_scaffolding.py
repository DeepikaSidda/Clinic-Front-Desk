"""Scaffolding smoke test (task 1.1).

Verifies the package layout imports cleanly and the pytest marker conventions
are registered. This is intentionally free of domain logic; it exists so the
foundational setup has a green anchor and the src layout is proven importable.
"""

from __future__ import annotations

import importlib

import pytest

_MODULES = [
    "clinic_front_desk",
    "clinic_front_desk.models",
    "clinic_front_desk.data_layer",
    "clinic_front_desk.data_layer.interfaces",
    "clinic_front_desk.data_layer.memory",
    "clinic_front_desk.data_layer.dynamodb",
    "clinic_front_desk.data_layer.events",
    "clinic_front_desk.config",
    "clinic_front_desk.tools",
    "clinic_front_desk.voice",
    "clinic_front_desk.intelligence",
    "clinic_front_desk.dashboard",
]


@pytest.mark.smoke
@pytest.mark.parametrize("module_name", _MODULES)
def test_package_module_imports(module_name: str) -> None:
    """Every declared package/sub-package imports without error."""
    assert importlib.import_module(module_name) is not None


@pytest.mark.smoke
def test_package_exposes_version() -> None:
    """The top-level package exposes a version string."""
    import clinic_front_desk

    assert isinstance(clinic_front_desk.__version__, str)
    assert clinic_front_desk.__version__


@pytest.mark.smoke
def test_core_dependencies_importable() -> None:
    """The core runtime + test dependencies are installed and importable."""
    for dep in ("strands", "boto3", "hypothesis"):
        assert importlib.import_module(dep) is not None
