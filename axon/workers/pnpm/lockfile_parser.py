"""
Parses pnpm-lock.yaml in both v6 and v9 format.

pnpm v6 lockfile structure (yaml):
  lockfileVersion: 6.0
  importers:
    .:                               # root workspace
      dependencies:
        react: 18.2.0
      devDependencies:
        typescript: 5.0.4
    packages/ui-components:          # workspace package
      dependencies:
        '@company/tokens': link:../tokens   # internal workspace dep

pnpm v9 lockfile structure (yaml):
  lockfileVersion: '9.0'
  importers:
    .:
      dependencies:
        react: { specifier: ^18.2.0, version: 18.2.0 }
    packages/ui-components:
      dependencies:
        '@company/tokens': { specifier: workspace:*, version: link:../tokens }

Extracts:
  - All workspace package paths (keys under importers that are not '.')
  - For each workspace package: its direct dependencies with resolved versions
  - Whether each dep is an internal workspace dep (link: prefix) or external
  - All unique external packages across the entire lockfile
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LockfileDep:
    name: str
    version: str
    is_workspace: bool = False  # True if link: or workspace: protocol
    workspace_path: str | None = None  # relative path for workspace deps


@dataclass
class WorkspacePackage:
    path: str  # relative to monorepo root, e.g. 'packages/ui-components'
    name: str  # from package.json in that directory, e.g. '@company/ui-components'
    dependencies: list[LockfileDep] = field(default_factory=list)
    dev_dependencies: list[LockfileDep] = field(default_factory=list)


@dataclass
class ParsedLockfile:
    lockfile_version: str
    workspace_packages: list[WorkspacePackage] = field(default_factory=list)
    all_external_packages: list[LockfileDep] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def _is_workspace_dep(version_str: str) -> tuple[bool, str | None]:
    """
    Returns (is_workspace, workspace_path).
    Handles both v6 'link:../tokens' and v9 'link:../tokens' formats.
    """
    if version_str.startswith("link:"):
        path = version_str[5:]  # strip 'link:'
        return True, path
    if version_str.startswith("workspace:"):
        # workspace:* or workspace:../path
        suffix = version_str[len("workspace:") :]
        if suffix.startswith("."):
            return True, suffix
        return True, None
    return False, None


def _parse_dep_map_v6(deps: dict) -> list[LockfileDep]:
    """Parse the flat {name: version_string} format used in pnpm v6."""
    result = []
    for name, version_val in deps.items():
        version_str = str(version_val) if not isinstance(version_val, str) else version_val
        is_ws, ws_path = _is_workspace_dep(version_str)
        result.append(
            LockfileDep(
                name=name,
                version=version_str if not is_ws else "",
                is_workspace=is_ws,
                workspace_path=ws_path,
            )
        )
    return result


def _parse_dep_map_v9(deps: dict) -> list[LockfileDep]:
    """Parse the {name: {specifier: ..., version: ...}} format used in pnpm v9."""
    result = []
    for name, dep_info in deps.items():
        if isinstance(dep_info, str):
            # Some v9 files still use flat format in certain sections
            return _parse_dep_map_v6({name: dep_info})
        version_str = str(dep_info.get("version", ""))
        is_ws, ws_path = _is_workspace_dep(version_str)
        result.append(
            LockfileDep(
                name=name,
                version=version_str if not is_ws else "",
                is_workspace=is_ws,
                workspace_path=ws_path,
            )
        )
    return result


def parse_lockfile(lockfile_path: str) -> ParsedLockfile:
    """
    Parse a pnpm-lock.yaml file. Returns a ParsedLockfile.
    Never raises; logs errors into ParsedLockfile.parse_errors.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — pnpm lockfile parsing unavailable. Run: uv add pyyaml")
        return ParsedLockfile(lockfile_version="unknown", parse_errors=["PyYAML not installed"])

    try:
        with open(lockfile_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        msg = f"Failed to parse {lockfile_path}: {e}"
        logger.warning(msg)
        return ParsedLockfile(lockfile_version="unknown", parse_errors=[msg])

    version = str(data.get("lockfileVersion", "unknown"))
    result = ParsedLockfile(lockfile_version=version)

    is_v9 = version.startswith("9") or version == "'9.0'"
    parse_dep_map = _parse_dep_map_v9 if is_v9 else _parse_dep_map_v6

    importers = data.get("importers", {})
    for importer_path, importer_data in importers.items():
        if not isinstance(importer_data, dict):
            continue

        deps = parse_dep_map(importer_data.get("dependencies", {}))
        dev_deps = parse_dep_map(importer_data.get("devDependencies", {}))

        # Root (importer_path == '.') is the monorepo root, not a workspace pkg
        if importer_path == ".":
            # Still record all external deps from root
            for d in deps + dev_deps:
                if not d.is_workspace:
                    result.all_external_packages.append(d)
            continue

        result.workspace_packages.append(
            WorkspacePackage(
                path=importer_path,
                name="",  # populated by workspace_resolver.py from package.json
                dependencies=deps,
                dev_dependencies=dev_deps,
            )
        )

    # Collect unique external packages from all workspace packages
    seen = set()
    for wp in result.workspace_packages:
        for dep in wp.dependencies + wp.dev_dependencies:
            if not dep.is_workspace and dep.name not in seen:
                seen.add(dep.name)
                result.all_external_packages.append(dep)

    logger.debug(
        "Lockfile %s: version=%s workspace_packages=%d external=%d",
        lockfile_path,
        version,
        len(result.workspace_packages),
        len(result.all_external_packages),
    )
    return result
