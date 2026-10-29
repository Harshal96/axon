"""
Parses a Maven pom.xml without running mvn.

Extracts:
  - groupId, artifactId, version (the artifact identity of this POM)
  - <parent> coordinates
  - <dependencies>: direct declared dependencies with scope
  - <dependencyManagement> entries (version pinning)

For transitive dependencies, maven/runner.py runs `mvn dependency:list`
and this module merges the results.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


@dataclass
class MavenCoordinate:
    group_id: str
    artifact_id: str
    version: str
    scope: str = "compile"
    is_optional: bool = False

    @property
    def mvn_id(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.version}"


@dataclass
class ParsedPom:
    path: str
    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    parent: MavenCoordinate | None = None
    direct_dependencies: list[MavenCoordinate] = field(default_factory=list)
    # Transitive dependencies — populated by runner.py after `mvn dependency:list`
    transitive_dependencies: list[MavenCoordinate] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _resolve_property(value: str, properties: dict[str, str]) -> str:
    """Resolve ${property.name} references in POM values."""
    if not value.startswith("${"):
        return value
    key = value[2:-1]
    return properties.get(key, value)


def parse_pom(pom_path: str) -> ParsedPom:
    result = ParsedPom(path=pom_path)
    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
    except ET.ParseError as e:
        result.parse_errors.append(str(e))
        return result

    # Strip namespace from tag names for simpler access
    def tag(el: ET.Element) -> str:
        return el.tag.split("}")[-1] if "}" in el.tag else el.tag

    # Build a flat element lookup
    def find(path: str) -> ET.Element | None:
        # Try with namespace first, then without
        el = root.find(f"m:{path}", _NS)
        if el is None:
            el = root.find(path)
        return el

    def findall(path: str) -> list[ET.Element]:
        els = root.findall(f"m:{path}", _NS)
        if not els:
            els = root.findall(path)
        return els

    # Properties for ${...} substitution
    properties: dict[str, str] = {}
    props_el = find("properties")
    if props_el is not None:
        for prop in props_el:
            properties[tag(prop)] = (prop.text or "").strip()

    # Identity
    result.group_id = _resolve_property(_text(find("groupId")), properties)
    result.artifact_id = _resolve_property(_text(find("artifactId")), properties)
    result.version = _resolve_property(_text(find("version")), properties)

    # Parent
    parent_el = find("parent")
    if parent_el is not None:
        pg_el = parent_el.find("m:groupId", _NS)
        if pg_el is None:
            pg_el = parent_el.find("groupId")

        pa_el = parent_el.find("m:artifactId", _NS)
        if pa_el is None:
            pa_el = parent_el.find("artifactId")

        pv_el = parent_el.find("m:version", _NS)
        if pv_el is None:
            pv_el = parent_el.find("version")

        pg = _text(pg_el)
        pa = _text(pa_el)
        pv = _text(pv_el)
        result.parent = MavenCoordinate(
            group_id=_resolve_property(pg, properties),
            artifact_id=_resolve_property(pa, properties),
            version=_resolve_property(pv, properties),
        )
        # If our own groupId/version is empty, inherit from parent
        if not result.group_id:
            result.group_id = result.parent.group_id
        if not result.version:
            result.version = result.parent.version

    # Direct dependencies
    for dep_el in root.iter():
        tag_name = tag(dep_el)
        if tag_name == "dependency":
            g_el = dep_el.find("m:groupId", _NS)
            if g_el is None:
                g_el = dep_el.find("groupId")

            a_el = dep_el.find("m:artifactId", _NS)
            if a_el is None:
                a_el = dep_el.find("artifactId")

            v_el = dep_el.find("m:version", _NS)
            if v_el is None:
                v_el = dep_el.find("version")

            scope_el = dep_el.find("m:scope", _NS)
            if scope_el is None:
                scope_el = dep_el.find("scope")

            opt_el = dep_el.find("m:optional", _NS)
            if opt_el is None:
                opt_el = dep_el.find("optional")

            g = _resolve_property(_text(g_el), properties)
            a = _resolve_property(_text(a_el), properties)
            v = _resolve_property(_text(v_el), properties)
            scope = _text(scope_el) or "compile"
            is_optional = _text(opt_el).lower() == "true"

            if g and a:  # version may be managed
                result.direct_dependencies.append(
                    MavenCoordinate(
                        group_id=g,
                        artifact_id=a,
                        version=v or "MANAGED",
                        scope=scope,
                        is_optional=is_optional,
                    )
                )

    logger.debug(
        "Parsed POM %s: %s:%s:%s direct_deps=%d",
        pom_path,
        result.group_id,
        result.artifact_id,
        result.version,
        len(result.direct_dependencies),
    )
    return result
