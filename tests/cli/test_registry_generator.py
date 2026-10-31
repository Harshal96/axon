"""The example generator must be deterministic and must never import a registry."""

import runpy
from pathlib import Path

import pytest


def test_registry_generator_is_static_and_reproducible(tmp_path):
    generate = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "examples/registry-onboarding/gen_axon_yaml.py")
    )["generate"]
    (tmp_path / "services/app").mkdir(parents=True)
    (tmp_path / "libraries/common").mkdir(parents=True)
    registry = tmp_path / "registry.py"
    registry.write_text(
        """raise RuntimeError('must never execute')
Service(id='app', path='services/app')
Library(path='libraries/common')
"""
    )
    first = generate(tmp_path, registry, "abc", "2026-01-01")
    assert first == generate(tmp_path, registry, "abc", "2026-01-01")
    assert "services/app" in first
    assert "libraries/common" in first
    assert "do not hand-edit" in first
    registry.write_text("Service(id='app', path=dynamic_path())\n")
    with pytest.raises(ValueError, match="literal"):
        generate(tmp_path, registry, "abc", "2026-01-01")
