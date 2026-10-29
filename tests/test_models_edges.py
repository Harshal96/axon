from __future__ import annotations

from axon.models.edges import (
    BuildsEdge,
    CallsEdge,
    ConsumesApiEdge,
    ConsumesFileEdge,
    DefinesEdge,
    DependsOnEdge,
    ExposesApiEdge,
    ExtendsEdge,
    GeneratedFromEdge,
    GeneratesClientFromEdge,
    ImplementsEdge,
    ImportsEdge,
    IntroducedByEdge,
    MavenDepEdge,
    OverridesEdge,
    OwnedByEdge,
    PnpmDepEdge,
    ProducesEdge,
    SwaggerDefinesEndpointEdge,
)
from axon.models.enums import (
    DependencyType,
    EdgeConfidence,
    ImportType,
    MavenScope,
)


def test_calls_edge_defaults() -> None:
    edge = CallsEdge(from_id="symbol::a", to_id="symbol::b")
    assert edge.confidence == EdgeConfidence.EXTRACTED
    assert edge.confidence_score == 1.0
    assert edge.call_count == 1
    assert edge.is_dynamic is False
    assert edge.call_sites == []


def test_calls_edge_with_sites() -> None:
    edge = CallsEdge(
        from_id="symbol::a",
        to_id="symbol::b",
        call_sites=[{"line": 42, "file": "foo.py"}],
        call_count=3,
        is_dynamic=True,
        confidence=EdgeConfidence.INFERRED,
        confidence_score=0.6,
    )
    assert len(edge.call_sites) == 1
    assert edge.call_count == 3
    assert edge.is_dynamic is True
    assert edge.confidence == EdgeConfidence.INFERRED


def test_defines_edge() -> None:
    edge = DefinesEdge(from_id="file::foo.py", to_id="symbol::foo.bar")
    assert edge.from_id == "file::foo.py"
    assert edge.to_id == "symbol::foo.bar"


def test_imports_edge() -> None:
    edge = ImportsEdge(
        from_id="file::a.py",
        to_id="file::b.py",
        import_type=ImportType.DYNAMIC,
        import_names=["SomeClass"],
    )
    assert edge.import_type == ImportType.DYNAMIC
    assert "SomeClass" in edge.import_names


def test_extends_edge() -> None:
    edge = ExtendsEdge(from_id="symbol::child", to_id="symbol::parent")
    assert edge.confidence == EdgeConfidence.EXTRACTED


def test_implements_edge() -> None:
    edge = ImplementsEdge(from_id="symbol::class", to_id="symbol::interface")
    assert edge.from_id == "symbol::class"


def test_overrides_edge() -> None:
    edge = OverridesEdge(from_id="symbol::sub.method", to_id="symbol::base.method")
    assert edge.to_id == "symbol::base.method"


def test_depends_on_edge() -> None:
    edge = DependsOnEdge(
        from_id="target::a",
        to_id="target::b",
        dep_type=DependencyType.TRANSITIVE,
        declared_in="services/payment/BUILD",
    )
    assert edge.dep_type == DependencyType.TRANSITIVE
    assert edge.declared_in == "services/payment/BUILD"


def test_builds_edge() -> None:
    edge = BuildsEdge(from_id="target::lib", to_id="file::src.py")
    assert edge.from_id == "target::lib"


def test_produces_edge() -> None:
    edge = ProducesEdge(from_id="target::gen", to_id="file::out.py")
    assert edge.to_id == "file::out.py"


def test_consumes_file_edge() -> None:
    edge = ConsumesFileEdge(from_id="target::gen", to_id="file::swagger.json")
    assert edge.from_id == "target::gen"


def test_owned_by_edge() -> None:
    edge = OwnedByEdge(
        from_id="target::lib",
        to_id="team::payments",
        ownership_source="BAZEL_ATTR",
    )
    assert edge.ownership_source == "BAZEL_ATTR"


def test_exposes_api_edge() -> None:
    edge = ExposesApiEdge(from_id="target::svc", to_id="endpoint::POST:/v1/foo")
    assert edge.confidence_score == 1.0


def test_consumes_api_edge() -> None:
    edge = ConsumesApiEdge(
        from_id="target::consumer",
        to_id="endpoint::POST:/v1/foo",
        via_client="//clients/foo:foo_client_py",
        client_language="PYTHON",
    )
    assert edge.via_client is not None
    assert edge.client_language == "PYTHON"


def test_generates_client_from_edge() -> None:
    edge = GeneratesClientFromEdge(from_id="target::gen", to_id="swagger::spec.json")
    assert edge.from_id.startswith("target::")


def test_generated_from_edge() -> None:
    edge = GeneratedFromEdge(from_id="genclient::client", to_id="swagger::spec.json")
    assert edge.to_id.startswith("swagger::")


def test_swagger_defines_endpoint_edge() -> None:
    edge = SwaggerDefinesEndpointEdge(from_id="swagger::spec.json", to_id="endpoint::GET:/v1/bar")
    assert edge.from_id.startswith("swagger::")


def test_maven_dep_edge() -> None:
    edge = MavenDepEdge(
        from_id="target::lib",
        to_id="maven::org.apache:commons:3.2",
        scope=MavenScope.TEST,
        declared_in="pom.xml",
    )
    assert edge.scope == MavenScope.TEST


def test_pnpm_dep_edge() -> None:
    edge = PnpmDepEdge(
        from_id="target::ui",
        to_id="pnpm::lodash@4.17.21",
        declared_in="packages/ui/package.json",
    )
    assert edge.declared_in.endswith("package.json")


def test_introduced_by_edge() -> None:
    edge = IntroducedByEdge(
        from_id="symbol::foo",
        to_id="commit::abc123",
        author="alice",
    )
    assert edge.author == "alice"


def test_confidence_score_bounds() -> None:
    edge = CallsEdge(from_id="a", to_id="b", confidence_score=0.0)
    assert edge.confidence_score == 0.0
    edge2 = CallsEdge(from_id="a", to_id="b", confidence_score=1.0)
    assert edge2.confidence_score == 1.0


def test_temporal_validity() -> None:
    edge = DefinesEdge(
        from_id="file::a.py",
        to_id="symbol::a.foo",
        valid_from_commit="abc123",
        valid_until_commit="def456",
    )
    assert edge.valid_from_commit == "abc123"
    assert edge.valid_until_commit == "def456"
