"""Base graph seed used by contract and regression integration tests."""

# ruff: noqa: E501

from __future__ import annotations

import pytest

from axon.db.connection import sync_session

SEED_GRAPH = """
MERGE (payments:Team {id: 'team::payments-team'})
SET payments.name = 'payments-team',
    payments.codeowners_patterns = ['services/payment/**', 'clients/payment/**'];

MERGE (checkout_team:Team {id: 'team::checkout-team'})
SET checkout_team.name = 'checkout-team',
    checkout_team.codeowners_patterns = ['services/checkout/**'];

MERGE (data_team:Team {id: 'team::data-team'})
SET data_team.name = 'data-team',
    data_team.codeowners_patterns = ['internal/models/**'];

MERGE (payment:BazelTarget {id: 'target:://services/payment:payment_lib'})
SET payment.label = '//services/payment:payment_lib',
    payment.kind = 'PY_LIBRARY',
    payment.name = 'payment_lib',
    payment.package = '//services/payment',
    payment.build_file_path = 'services/payment/BUILD',
    payment.visibility = ['//...'],
    payment.tags = [],
    payment.testonly = false,
    payment.is_genrule = false,
    payment.team_owner = 'payments-team',
    payment.has_swagger_src = false,
    payment.has_maven_src = false,
    payment.has_pnpm_src = false,
    payment.genrule_srcs = [],
    payment.genrule_outs = [];

MERGE (models:BazelTarget {id: 'target:://internal/models:models'})
SET models.label = '//internal/models:models',
    models.kind = 'PY_LIBRARY',
    models.name = 'models',
    models.package = '//internal/models',
    models.build_file_path = 'internal/models/BUILD',
    models.visibility = ['//...'],
    models.tags = [],
    models.testonly = false,
    models.is_genrule = false,
    models.team_owner = 'data-team',
    models.has_swagger_src = false,
    models.has_maven_src = false,
    models.has_pnpm_src = false,
    models.genrule_srcs = [],
    models.genrule_outs = [];

MERGE (gateway:BazelTarget {id: 'target:://services/payment:payment_gateway_java'})
SET gateway.label = '//services/payment:payment_gateway_java',
    gateway.kind = 'GENRULE',
    gateway.name = 'payment_gateway_java',
    gateway.package = '//services/payment',
    gateway.build_file_path = 'services/payment/BUILD',
    gateway.visibility = ['//services/payment:__pkg__'],
    gateway.tags = [],
    gateway.testonly = false,
    gateway.is_genrule = true,
    gateway.team_owner = 'payments-team',
    gateway.has_swagger_src = false,
    gateway.has_maven_src = true,
    gateway.has_pnpm_src = false,
    gateway.genrule_srcs = ['//services/payment:pom.xml'],
    gateway.genrule_outs = ['payment_gateway.jar'];

MERGE (client_rule:BazelTarget {id: 'target:://services/payment:generate_payment_client_py'})
SET client_rule.label = '//services/payment:generate_payment_client_py',
    client_rule.kind = 'GENRULE',
    client_rule.name = 'generate_payment_client_py',
    client_rule.package = '//services/payment',
    client_rule.build_file_path = 'services/payment/BUILD',
    client_rule.visibility = ['//services/checkout:__pkg__', '//clients/payment:__pkg__'],
    client_rule.tags = [],
    client_rule.testonly = false,
    client_rule.is_genrule = true,
    client_rule.team_owner = 'payments-team',
    client_rule.has_swagger_src = true,
    client_rule.has_maven_src = false,
    client_rule.has_pnpm_src = false,
    client_rule.genrule_srcs = ['//services/payment:swagger.json'],
    client_rule.genrule_outs = ['generated/payment_client.py'];

MERGE (checkout:BazelTarget {id: 'target:://services/checkout:checkout_lib'})
SET checkout.label = '//services/checkout:checkout_lib',
    checkout.kind = 'PY_LIBRARY',
    checkout.name = 'checkout_lib',
    checkout.package = '//services/checkout',
    checkout.build_file_path = 'services/checkout/BUILD',
    checkout.visibility = ['//...'],
    checkout.tags = [],
    checkout.testonly = false,
    checkout.is_genrule = false,
    checkout.team_owner = 'checkout-team',
    checkout.has_swagger_src = false,
    checkout.has_maven_src = false,
    checkout.has_pnpm_src = false,
    checkout.genrule_srcs = [],
    checkout.genrule_outs = [];

MERGE (payment_test:BazelTarget {id: 'target:://services/payment:payment_test'})
SET payment_test.label = '//services/payment:payment_test',
    payment_test.kind = 'PY_TEST',
    payment_test.name = 'payment_test',
    payment_test.package = '//services/payment',
    payment_test.build_file_path = 'services/payment/BUILD',
    payment_test.visibility = ['//...'],
    payment_test.tags = [],
    payment_test.testonly = true,
    payment_test.is_genrule = false,
    payment_test.team_owner = 'payments-team',
    payment_test.has_swagger_src = false,
    payment_test.has_maven_src = false,
    payment_test.has_pnpm_src = false,
    payment_test.genrule_srcs = [],
    payment_test.genrule_outs = [];

MERGE (checkout_test:BazelTarget {id: 'target:://services/checkout:checkout_test'})
SET checkout_test.label = '//services/checkout:checkout_test',
    checkout_test.kind = 'PY_TEST',
    checkout_test.name = 'checkout_test',
    checkout_test.package = '//services/checkout',
    checkout_test.build_file_path = 'services/checkout/BUILD',
    checkout_test.visibility = ['//...'],
    checkout_test.tags = [],
    checkout_test.testonly = true,
    checkout_test.is_genrule = false,
    checkout_test.team_owner = 'checkout-team',
    checkout_test.has_swagger_src = false,
    checkout_test.has_maven_src = false,
    checkout_test.has_pnpm_src = false,
    checkout_test.genrule_srcs = [],
    checkout_test.genrule_outs = [];

MATCH (payment:BazelTarget {label: '//services/payment:payment_lib'})
MATCH (models:BazelTarget {label: '//internal/models:models'})
MERGE (payment)-[:DEPENDS_ON {dep_type: 'DIRECT', declared_in: 'services/payment/BUILD'}]->(models);

MATCH (payment:BazelTarget {label: '//services/payment:payment_lib'})
MATCH (gateway:BazelTarget {label: '//services/payment:payment_gateway_java'})
MERGE (payment)-[:DEPENDS_ON {dep_type: 'DIRECT', declared_in: 'services/payment/BUILD'}]->(gateway);

MATCH (checkout:BazelTarget {label: '//services/checkout:checkout_lib'})
MATCH (client_rule:BazelTarget {label: '//services/payment:generate_payment_client_py'})
MERGE (checkout)-[:DEPENDS_ON {dep_type: 'DIRECT', declared_in: 'services/checkout/BUILD'}]->(client_rule);

MATCH (payment_test:BazelTarget {label: '//services/payment:payment_test'})
MATCH (payment:BazelTarget {label: '//services/payment:payment_lib'})
MERGE (payment_test)-[:DEPENDS_ON {dep_type: 'DIRECT', declared_in: 'services/payment/BUILD'}]->(payment);

MATCH (checkout_test:BazelTarget {label: '//services/checkout:checkout_test'})
MATCH (checkout:BazelTarget {label: '//services/checkout:checkout_lib'})
MERGE (checkout_test)-[:DEPENDS_ON {dep_type: 'DIRECT', declared_in: 'services/checkout/BUILD'}]->(checkout);

MATCH (t:BazelTarget)
WHERE t.team_owner IS NOT NULL
MATCH (team:Team {name: t.team_owner})
MERGE (t)-[:OWNED_BY {ownership_source: 'CODEOWNERS'}]->(team);

MERGE (payment_file:SourceFile {id: 'file::services/payment/src/payment.py'})
SET payment_file.path = 'services/payment/src/payment.py',
    payment_file.language = 'PYTHON',
    payment_file.sha256 = 'seed-payment',
    payment_file.line_count = 22,
    payment_file.is_generated = false;

MERGE (checkout_file:SourceFile {id: 'file::services/checkout/src/checkout.py'})
SET checkout_file.path = 'services/checkout/src/checkout.py',
    checkout_file.language = 'PYTHON',
    checkout_file.sha256 = 'seed-checkout',
    checkout_file.line_count = 10,
    checkout_file.is_generated = false;

MERGE (generated_file:SourceFile {id: 'file::services/payment/generated/payment_client.py'})
SET generated_file.path = 'services/payment/generated/payment_client.py',
    generated_file.language = 'PYTHON',
    generated_file.sha256 = 'seed-generated',
    generated_file.line_count = 20,
    generated_file.is_generated = true;

MATCH (payment:BazelTarget {label: '//services/payment:payment_lib'})
MATCH (payment_file:SourceFile {path: 'services/payment/src/payment.py'})
MERGE (payment)-[:BUILDS {confidence: 'EXTRACTED'}]->(payment_file);

MATCH (checkout:BazelTarget {label: '//services/checkout:checkout_lib'})
MATCH (checkout_file:SourceFile {path: 'services/checkout/src/checkout.py'})
MERGE (checkout)-[:BUILDS {confidence: 'EXTRACTED'}]->(checkout_file);

MATCH (client_rule:BazelTarget {label: '//services/payment:generate_payment_client_py'})
MATCH (generated_file:SourceFile {path: 'services/payment/generated/payment_client.py'})
MERGE (client_rule)-[:PRODUCES {confidence: 'EXTRACTED'}]->(generated_file);

MERGE (svc:Symbol {id: 'symbol::services/payment/src/payment.py::PaymentService'})
SET svc.symbol_id = 'services/payment/src/payment.py::PaymentService',
    svc.name = 'PaymentService',
    svc.qualified_name = 'payment.PaymentService',
    svc.kind = 'CLASS',
    svc.language = 'PYTHON',
    svc.file_path = 'services/payment/src/payment.py',
    svc.line_start = 4,
    svc.line_end = 20,
    svc.docstring = 'Handles charge and refund operations.',
    svc.signature = 'class PaymentService',
    svc.is_public = true,
    svc.is_test = false;

MERGE (charge:Symbol {id: 'symbol::services/payment/src/payment.py::PaymentService.charge'})
SET charge.symbol_id = 'services/payment/src/payment.py::PaymentService.charge',
    charge.name = 'charge',
    charge.qualified_name = 'payment.PaymentService.charge',
    charge.kind = 'METHOD',
    charge.language = 'PYTHON',
    charge.file_path = 'services/payment/src/payment.py',
    charge.line_start = 8,
    charge.line_end = 12,
    charge.docstring = 'Process a payment charge.',
    charge.signature = '(amount: float, currency: str) -> ChargeResult',
    charge.is_public = true,
    charge.is_test = false;

MATCH (payment_file:SourceFile {path: 'services/payment/src/payment.py'})
MATCH (svc:Symbol {qualified_name: 'payment.PaymentService'})
MERGE (payment_file)-[:DEFINES {confidence: 'EXTRACTED'}]->(svc);

MATCH (payment_file:SourceFile {path: 'services/payment/src/payment.py'})
MATCH (charge:Symbol {qualified_name: 'payment.PaymentService.charge'})
MERGE (payment_file)-[:DEFINES {confidence: 'EXTRACTED'}]->(charge);

MERGE (swagger:SwaggerSchema {id: 'swagger::services/payment/swagger.json'})
SET swagger.path = 'services/payment/swagger.json',
    swagger.service_label = '//services/payment:payment_lib',
    swagger.openapi_version = '3.0.0',
    swagger.content_hash = 'seed-swagger',
    swagger.endpoint_count = 3;

MERGE (endpoint:ApiEndpoint {id: 'endpoint:://services/payment:payment_lib::POST:/v1/charges'})
SET endpoint.endpoint_id = '//services/payment:payment_lib::POST:/v1/charges',
    endpoint.http_method = 'POST',
    endpoint.path = '/v1/charges',
    endpoint.service_label = '//services/payment:payment_lib',
    endpoint.operation_id = 'createCharge',
    endpoint.summary = 'Create a charge',
    endpoint.swagger_source = 'services/payment/swagger.json',
    endpoint.schema_hash = 'seed-endpoint',
    endpoint.is_deprecated = false;

MERGE (client:GeneratedClient {id: 'genclient:://services/payment:generate_payment_client_py'})
SET client.bazel_label = '//services/payment:generate_payment_client_py',
    client.source_swagger = 'services/payment/swagger.json',
    client.target_language = 'PYTHON',
    client.generator_tool = 'openapi-generator',
    client.output_paths = ['generated/payment_client.py'];

MATCH (swagger:SwaggerSchema {path: 'services/payment/swagger.json'})
MATCH (endpoint:ApiEndpoint {path: '/v1/charges'})
MERGE (swagger)-[:DEFINES_ENDPOINT {confidence: 'EXTRACTED'}]->(endpoint);

MATCH (client:GeneratedClient {bazel_label: '//services/payment:generate_payment_client_py'})
MATCH (swagger:SwaggerSchema {path: 'services/payment/swagger.json'})
MERGE (client)-[:GENERATED_FROM {confidence: 'EXTRACTED'}]->(swagger);

MATCH (payment:BazelTarget {label: '//services/payment:payment_lib'})
MATCH (endpoint:ApiEndpoint {path: '/v1/charges'})
MERGE (payment)-[:EXPOSES_API {confidence: 'EXTRACTED'}]->(endpoint);

MATCH (checkout:BazelTarget {label: '//services/checkout:checkout_lib'})
MATCH (endpoint:ApiEndpoint {path: '/v1/charges'})
MERGE (checkout)-[:CONSUMES_API {confidence: 'INFERRED'}]->(endpoint);

MERGE (commit:CommitSnapshot {sha: 'seed000000000000000000000000000000000000'})
SET commit.id = 'seed000000000000000000000000000000000000',
    commit.index_success = true,
    commit.branch = 'main',
    commit.author = 'seed@axon.local',
    commit.changed_targets = ['//services/payment:payment_lib'],
    commit.breaking_api_changes = 0,
    commit.index_completed_at = datetime();
"""


def seed_neo4j() -> None:
    with sync_session() as session:
        for statement in SEED_GRAPH.split(";"):
            statement = statement.strip()
            if statement:
                session.run(statement)


@pytest.fixture
def seed_graph() -> None:
    seed_neo4j()
