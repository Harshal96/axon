from __future__ import annotations

from axon.models.enums import (
    BazelTargetKind,
    HttpMethod,
    Language,
    SymbolKind,
)
from axon.models.nodes import (
    ApiEndpoint,
    BazelTarget,
    CommitSnapshot,
    GeneratedClient,
    MavenArtifact,
    PnpmPackage,
    SourceFile,
    SwaggerSchema,
    Symbol,
    Team,
)


def test_source_file_node_id() -> None:
    sf = SourceFile(
        path="services/payment/src/payment.py",
        language=Language.PYTHON,
        sha256="abc123",
    )
    assert sf.node_id == "file::services/payment/src/payment.py"


def test_bazel_target_node_id() -> None:
    bt = BazelTarget(
        label="//services/payment:payment_lib",
        kind=BazelTargetKind.PY_LIBRARY,
        name="payment_lib",
        package="//services/payment",
        build_file_path="services/payment/BUILD",
    )
    assert bt.node_id == "target:://services/payment:payment_lib"


def test_symbol_node_id() -> None:
    sym = Symbol(
        symbol_id="services/payment/src/payment.py::PaymentService.charge",
        name="charge",
        qualified_name="payment.PaymentService.charge",
        kind=SymbolKind.METHOD,
        language=Language.PYTHON,
        file_path="services/payment/src/payment.py",
        line_start=34,
        line_end=68,
    )
    assert "PaymentService.charge" in sym.node_id


def test_api_endpoint_node_id() -> None:
    ep = ApiEndpoint(
        endpoint_id="//services/payment::POST:/v1/charges",
        http_method=HttpMethod.POST,
        path="/v1/charges",
        service_label="//services/payment",
        swagger_source="services/payment/swagger.json",
        schema_hash="def456",
    )
    assert ep.node_id.startswith("endpoint::")


def test_swagger_schema_node_id() -> None:
    sw = SwaggerSchema(
        path="services/payment/swagger.json",
        service_label="//services/payment",
        openapi_version="3.0.1",
        content_hash="xyz789",
    )
    assert sw.node_id == "swagger::services/payment/swagger.json"


def test_generated_client_node_id() -> None:
    gc = GeneratedClient(
        bazel_label="//clients/payment:payment_client_py",
        source_swagger="services/payment/swagger.json",
        target_language=Language.PYTHON,
        generator_tool="openapi-generator",
    )
    assert "clients/payment" in gc.node_id


def test_maven_artifact_node_id() -> None:
    ma = MavenArtifact(
        group_id="org.apache.commons",
        artifact_id="commons-collections",
        version="3.2.1",
    )
    assert ma.node_id == "maven::org.apache.commons:commons-collections:3.2.1"


def test_pnpm_package_internal() -> None:
    pkg = PnpmPackage(
        name="@company/ui-components",
        version="1.0.0",
        workspace_path="packages/ui-components",
        is_internal=True,
    )
    assert pkg.is_internal is True
    assert pkg.node_id == "pnpm::@company/ui-components@1.0.0"


def test_team_node_id() -> None:
    team = Team(name="payments-team", codeowners_patterns=["services/payment/**"])
    assert team.node_id == "team::payments-team"


def test_commit_snapshot_node_id() -> None:
    cs = CommitSnapshot(
        sha="abc123def456",
        graph_version_id="v1-abc123",
    )
    assert cs.node_id == "commit::abc123def456"
