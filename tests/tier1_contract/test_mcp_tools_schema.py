"""Contract tests for the v2 MCP tool surface exposed to coding agents."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "axon-mcp"))

from axon_mcp.server import TOOL_DEFINITIONS  # noqa: E402

pytestmark = pytest.mark.tier1

EXPECTED_TOOL_NAMES = {
    "axon_search",
    "axon_services",
    "axon_api_graph",
    "axon_function_apis",
    "axon_ask",
}


def test_exactly_5_tools() -> None:
    assert len(TOOL_DEFINITIONS) == 5


def test_all_expected_tools_present() -> None:
    names = {tool.name for tool in TOOL_DEFINITIONS}
    assert names == EXPECTED_TOOL_NAMES


def test_all_tools_have_descriptions() -> None:
    for tool in TOOL_DEFINITIONS:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 20


def test_required_fields_documented() -> None:
    required_params = {
        "axon_search": ["q"],
        "axon_api_graph": ["endpoint_id"],
        "axon_function_apis": ["q"],
        "axon_ask": ["question"],
    }
    tool_map = {tool.name: tool for tool in TOOL_DEFINITIONS}
    for tool_name, expected_required in required_params.items():
        schema_required = tool_map[tool_name].inputSchema.get("required", [])
        for param in expected_required:
            assert param in schema_required


def test_no_bazel_shaped_tools() -> None:
    for tool in TOOL_DEFINITIONS:
        blob = (tool.name + tool.description).lower()
        assert "bazel" not in blob
        assert "connectome" not in blob


def test_api_graph_depth_has_max() -> None:
    tool = next(t for t in TOOL_DEFINITIONS if t.name == "axon_api_graph")
    depth_schema = tool.inputSchema["properties"]["depth"]
    assert depth_schema["maximum"] <= 5
