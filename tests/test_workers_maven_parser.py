"""Tests for the Maven POM parser."""

from __future__ import annotations

from pathlib import Path

from axon.workers.maven.parser import parse_pom_file, parse_pom_string

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_pom_dep_count() -> None:
    deps = parse_pom_file("fixtures/sample.pom.xml", str(FIXTURES.parent))
    assert len(deps) == 3


def test_parse_pom_guava() -> None:
    deps = parse_pom_file("fixtures/sample.pom.xml", str(FIXTURES.parent))
    guava = next(d for d in deps if d.artifact_id == "guava")
    assert guava.group_id == "com.google.guava"
    assert guava.version == "32.1.3-jre"
    assert guava.scope == "compile"


def test_parse_pom_test_scope() -> None:
    deps = parse_pom_file("fixtures/sample.pom.xml", str(FIXTURES.parent))
    junit = next(d for d in deps if d.artifact_id == "junit")
    assert junit.scope == "test"


def test_parse_pom_nonexistent_returns_empty() -> None:
    deps = parse_pom_file("does/not/exist.xml", "/tmp")
    assert deps == []


def test_parse_pom_invalid_xml_returns_empty() -> None:
    deps = parse_pom_string("this is not xml")
    assert deps == []


def test_parse_pom_no_namespace() -> None:
    raw = """<project>
      <dependencies>
        <dependency>
          <groupId>org.slf4j</groupId>
          <artifactId>slf4j-api</artifactId>
          <version>2.0.9</version>
        </dependency>
      </dependencies>
    </project>"""
    deps = parse_pom_string(raw)
    assert len(deps) == 1
    assert deps[0].group_id == "org.slf4j"
