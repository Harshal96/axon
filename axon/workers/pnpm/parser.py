"""
Parses package.json files to extract pnpm workspace dependencies.
Detects workspace:* protocol for internal packages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ParsedPnpmDep:
    """A single pnpm/npm dependency."""

    name: str
    version: str
    is_internal: bool = False  # True if workspace:* protocol
    is_dev: bool = False


@dataclass
class ParsedPackage:
    """Complete parse result for one package.json."""

    name: str
    version: str
    path: str  # repo-relative path to package.json
    workspace_path: str | None  # directory containing the package.json
    dependencies: list[ParsedPnpmDep]


def parse_package_json(file_path: str, repo_root: str) -> ParsedPackage | None:
    """
    Parse a package.json file and extract dependency info.
    Returns None if file doesn't exist or can't be parsed.
    """
    abs_path = Path(repo_root) / file_path
    if not abs_path.exists():
        logger.warning("package.json not found: %s", file_path)
        return None

    try:
        raw = abs_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s: %s", file_path, e)
        return None

    return parse_package_json_string(raw, file_path)


def parse_package_json_string(raw: str, file_path: str = "") -> ParsedPackage | None:
    """Parse a package.json string."""
    try:
        pkg = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Could not parse package.json: %s", e)
        return None

    if not isinstance(pkg, dict):
        return None

    name = pkg.get("name", "")
    version = pkg.get("version", "0.0.0")

    # Determine workspace path from file path
    workspace_path = str(Path(file_path).parent) if file_path else None
    if workspace_path == ".":
        workspace_path = None

    deps: list[ParsedPnpmDep] = []

    # Production dependencies
    for dep_name, dep_version in (pkg.get("dependencies") or {}).items():
        deps.append(
            ParsedPnpmDep(
                name=dep_name,
                version=dep_version,
                is_internal=_is_workspace_dep(dep_version),
                is_dev=False,
            )
        )

    # Dev dependencies
    for dep_name, dep_version in (pkg.get("devDependencies") or {}).items():
        deps.append(
            ParsedPnpmDep(
                name=dep_name,
                version=dep_version,
                is_internal=_is_workspace_dep(dep_version),
                is_dev=True,
            )
        )

    logger.info("Parsed %d deps from %s", len(deps), file_path or "string")
    return ParsedPackage(
        name=name,
        version=version,
        path=file_path,
        workspace_path=workspace_path,
        dependencies=deps,
    )


def _is_workspace_dep(version: str) -> bool:
    """Check if a version uses the pnpm workspace: protocol."""
    return version.startswith("workspace:")
