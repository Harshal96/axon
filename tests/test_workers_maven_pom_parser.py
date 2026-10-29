from __future__ import annotations

from pathlib import Path

from axon.workers.java.maven.pom_parser import parse_pom

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_coordinates() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    assert result.group_id == "com.company.payment"
    assert result.artifact_id == "payment-gateway"
    assert result.version == "1.0.0-SNAPSHOT"


def test_parse_direct_deps_count() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    assert len(result.direct_dependencies) == 3


def test_parse_commons_collections() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    cc = next(d for d in result.direct_dependencies if d.artifact_id == "commons-collections")
    assert cc.group_id == "org.apache.commons"
    assert cc.version == "3.2.1"  # resolved from ${commons.version}
    assert cc.scope == "compile"


def test_parse_test_scope() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    junit = next(d for d in result.direct_dependencies if d.artifact_id == "junit")
    assert junit.scope == "test"


def test_property_resolution() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    cc = next(d for d in result.direct_dependencies if d.artifact_id == "commons-collections")
    assert "${" not in cc.version  # property was resolved


def test_no_parse_errors() -> None:
    result = parse_pom(str(FIXTURES / "pom.xml"))
    assert result.parse_errors == []
