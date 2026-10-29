"""
Parses Maven pom.xml files to extract dependency information.
Uses xml.etree.ElementTree (stdlib).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Maven POM namespace
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


@dataclass
class ParsedMavenDep:
    """A single Maven dependency."""

    group_id: str
    artifact_id: str
    version: str
    scope: str = "compile"  # compile, test, provided, runtime


def parse_pom_file(file_path: str, repo_root: str) -> list[ParsedMavenDep]:
    """
    Parse a pom.xml file and extract dependencies.
    Returns empty list if file doesn't exist or can't be parsed.
    """
    abs_path = Path(repo_root) / file_path
    if not abs_path.exists():
        logger.warning("POM file not found: %s", file_path)
        return []

    try:
        raw = abs_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s: %s", file_path, e)
        return []

    return parse_pom_string(raw)


def parse_pom_string(raw: str) -> list[ParsedMavenDep]:
    """Parse a pom.xml string and extract dependencies."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("Could not parse POM XML: %s", e)
        return []

    deps: list[ParsedMavenDep] = []

    # Try with namespace first, then without
    dep_elements = root.findall(".//m:dependencies/m:dependency", _NS)
    if not dep_elements:
        dep_elements = root.findall(".//dependencies/dependency")

    for dep in dep_elements:
        group_id = _text(dep, "groupId") or _text(dep, "m:groupId", _NS)
        artifact_id = _text(dep, "artifactId") or _text(dep, "m:artifactId", _NS)
        version = _text(dep, "version") or _text(dep, "m:version", _NS) or "MANAGED"
        scope = _text(dep, "scope") or _text(dep, "m:scope", _NS) or "compile"

        if group_id and artifact_id:
            deps.append(
                ParsedMavenDep(
                    group_id=group_id,
                    artifact_id=artifact_id,
                    version=version,
                    scope=scope.lower(),
                )
            )

    logger.info("Parsed %d Maven dependencies from POM", len(deps))
    return deps


def _text(parent: ET.Element, tag: str, ns: dict | None = None) -> str | None:
    """Extract text from a child element."""
    child = parent.find(tag, ns) if ns else parent.find(tag)
    return child.text.strip() if child is not None and child.text else None
