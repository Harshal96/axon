"""Distribution boundaries and optional server dependency import contract."""

import json
import subprocess
import sys
from pathlib import Path


def test_core_and_cli_import_without_server_dependencies():
    script = """
import importlib, importlib.abc, sys
class BlockServer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"fastapi", "neo4j"}:
            raise AssertionError("server dependency imported: " + fullname)
sys.meta_path.insert(0, BlockServer())
import pkgutil, axon.v1
for module in pkgutil.iter_modules(axon.v1.__path__, "axon.v1."):
    if module.name not in {"axon.v1.api", "axon.v1.jobs"}:
        importlib.import_module(module.name)
import cli.app
from axon.v1.store import MemoryStore
assert not MemoryStore().load().nodes
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_split_distribution_metadata():
    import tomllib

    root = Path(__file__).resolve().parents[2]
    core = tomllib.loads((root / "axon-core/pyproject.toml").read_text())
    cli = tomllib.loads((root / "axon-cli/pyproject.toml").read_text())
    assert core["project"]["name"] == "axon-core"
    assert cli["project"]["scripts"]["axon"] == "cli.app:main"
    dependencies = json.dumps(core["project"]["dependencies"]).lower()
    assert "fastapi" not in dependencies and "neo4j" not in dependencies
    assert "neo4j" in cli["project"]["optional-dependencies"]
