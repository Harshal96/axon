"""Session setup for Tier 2 scenario tests."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from axon.db.init import initialize_schema
from axon.pipeline.coordinator import PipelineCoordinator
from axon.pipeline.event_schema import PushEvent

MONOREPO = os.path.join(os.path.dirname(__file__), "..", "fixtures", "monorepo")
COMMIT_SHA = "tier2aaabbbccc000111222333444555666777888"

MOCK_LABEL_KIND = """\
py_library rule //services/payment:payment_lib
genrule rule //services/payment:payment_gateway_java
genrule rule //services/payment:generate_payment_client_py
py_test rule //services/payment:payment_test
py_library rule //services/checkout:checkout_lib
py_test rule //services/checkout:checkout_test
js_library rule //packages/ui-components:ui_components
py_library rule //internal/models:models
"""

MOCK_DEPS_JSON = (
    '{"type":"RULE","rule":{"name":"//services/payment:payment_lib","ruleClass":"py_library","attribute":[{"name":"deps","type":"LABEL_LIST","stringListValue":["//internal/models:models","//services/payment:payment_gateway_java"]},{"name":"visibility","type":"LABEL_LIST","stringListValue":["//..."]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
    '{"type":"RULE","rule":{"name":"//services/payment:generate_payment_client_py","ruleClass":"genrule","attribute":[{"name":"srcs","type":"LABEL_LIST","stringListValue":["//services/payment:swagger.json"]},{"name":"outs","type":"STRING_LIST","stringListValue":["generated/payment_client.py"]},{"name":"visibility","type":"LABEL_LIST","stringListValue":["//services/checkout:__pkg__","//clients/payment:__pkg__"]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
    '{"type":"RULE","rule":{"name":"//services/payment:payment_gateway_java","ruleClass":"genrule","attribute":[{"name":"srcs","type":"LABEL_LIST","stringListValue":["//services/payment:pom.xml"]},{"name":"outs","type":"STRING_LIST","stringListValue":["payment_gateway.jar"]},{"name":"visibility","type":"LABEL_LIST","stringListValue":["//services/payment:__pkg__"]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
    '{"type":"RULE","rule":{"name":"//services/checkout:checkout_lib","ruleClass":"py_library","attribute":[{"name":"deps","type":"LABEL_LIST","stringListValue":["//services/payment:generate_payment_client_py"]},{"name":"visibility","type":"LABEL_LIST","stringListValue":["//..."]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
    '{"type":"RULE","rule":{"name":"//services/checkout:checkout_test","ruleClass":"py_test","attribute":[{"name":"deps","type":"LABEL_LIST","stringListValue":["//services/checkout:checkout_lib"]},{"name":"testonly","type":"BOOLEAN","booleanValue":true}]}}\n'
    '{"type":"RULE","rule":{"name":"//services/payment:payment_test","ruleClass":"py_test","attribute":[{"name":"deps","type":"LABEL_LIST","stringListValue":["//services/payment:payment_lib"]},{"name":"testonly","type":"BOOLEAN","booleanValue":true}]}}\n'
    '{"type":"RULE","rule":{"name":"//packages/ui-components:ui_components","ruleClass":"js_library","attribute":[{"name":"visibility","type":"LABEL_LIST","stringListValue":["//..."]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
    '{"type":"RULE","rule":{"name":"//internal/models:models","ruleClass":"py_library","attribute":[{"name":"visibility","type":"LABEL_LIST","stringListValue":["//..."]},{"name":"testonly","type":"BOOLEAN","booleanValue":false}]}}\n'
)


@pytest.fixture(scope="session", autouse=True)
def run_full_pipeline(neo4j_driver):
    initialize_schema()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")

    coordinator = PipelineCoordinator()
    event = PushEvent(
        sha=COMMIT_SHA,
        branch="main",
        author="test-engineer@company.com",
        changed_files=[],
        workspace_root=MONOREPO,
    )

    with (
        patch("axon.workers.bazel.runner.query_label_kind", return_value=MOCK_LABEL_KIND),
        patch("axon.workers.bazel.runner.query_deps_jsonproto", return_value=MOCK_DEPS_JSON),
        patch("axon.workers.bazel.runner.query_target_xml", return_value="<query></query>"),
    ):
        result = asyncio.get_event_loop().run_until_complete(coordinator.process(event))

    assert result["status"] in ("indexed", "indexed_with_errors"), result
    yield result
