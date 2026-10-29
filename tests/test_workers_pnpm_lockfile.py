from __future__ import annotations

from pathlib import Path

from axon.workers.pnpm.lockfile_parser import parse_lockfile

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_lockfile_version() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    assert result.lockfile_version.strip("'") == "9.0"
    assert not result.parse_errors


def test_workspace_package_count() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    # ui-components, tokens, services/checkout (not root '.')
    assert len(result.workspace_packages) == 3


def test_workspace_package_paths() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    paths = {wp.path for wp in result.workspace_packages}
    assert "packages/ui-components" in paths
    assert "packages/tokens" in paths
    assert "services/checkout" in paths


def test_workspace_dep_detected() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    ui = next(wp for wp in result.workspace_packages if wp.path == "packages/ui-components")
    tokens_dep = next(d for d in ui.dependencies if "@company/tokens" in d.name)
    assert tokens_dep.is_workspace is True


def test_external_packages_collected() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    ext_names = {p.name for p in result.all_external_packages}
    assert "react" in ext_names
    assert "typescript" in ext_names
    assert "chroma-js" in ext_names


def test_checkout_workspace_dep() -> None:
    result = parse_lockfile(str(FIXTURES / "pnpm-lock.yaml"))
    checkout = next(wp for wp in result.workspace_packages if wp.path == "services/checkout")
    ui_dep = next(d for d in checkout.dependencies if "@company/ui-components" in d.name)
    assert ui_dep.is_workspace is True
