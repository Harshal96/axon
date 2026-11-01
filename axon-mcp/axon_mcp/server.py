import argparse
import asyncio
import json
import logging
from importlib.resources import files

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import GetPromptResult, Prompt, PromptMessage, TextContent, Tool

from axon_mcp import tools
from axon_mcp.config import configure_claude, load_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("axon-mcp")

TOOL_TO_MODULE = {
    "axon_api_explain": "api_explain",
    "axon_client_show": "client_show",
    "axon_refs": "refs",
    "axon_search": "search",
    "axon_services": "services",
    "axon_api_graph": "api_graph",
    "axon_function_apis": "function_apis",
    "axon_ask": "ask",
}

TOOL_DEFINITIONS = [
    Tool(
        name="axon_search",
        description="Search indexed APIs, functions, and services in the Axon v1 graph.",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "minLength": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["q"],
        },
    ),
    Tool(
        name="axon_services",
        description="List services (per repo) with their API counts.",
        inputSchema={
            "type": "object",
            "properties": {"repo": {"type": "string"}},
        },
    ),
    Tool(
        name="axon_api_graph",
        description="Per-API dependency graph: callers, handler code, outbound APIs.",
        inputSchema={
            "type": "object",
            "properties": {
                "endpoint_id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["endpoint_id"],
        },
    ),
    Tool(
        name="axon_function_apis",
        description="Which indexed APIs reach a function/method? Includes its owner and package.",
        inputSchema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "minLength": 2},
                "repo": {"type": "string"},
                "used_in": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repository/service facet keys.",
                },
                "defined_in": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Owner facet keys from the functions catalog.",
                },
            },
            "required": ["q"],
        },
    ),
    Tool(
        name="axon_ask",
        description="Ask a natural-language question; answers are graph-grounded with citations.",
        inputSchema={
            "type": "object",
            "properties": {"question": {"type": "string", "minLength": 3}},
            "required": ["question"],
        },
    ),
]

for name, description, fields in [
    (
        "axon_api_explain",
        "Explain graph-grounded handlers and outbound gaps.",
        ["repo", "api", "service"],
    ),
    (
        "axon_client_show",
        "Show an indexed SDK client and its attributed consumers.",
        ["repo", "client"],
    ),
    (
        "axon_refs",
        "Inspect read-only snapshot references to a service, client, or endpoint.",
        ["repo", "kind", "entity"],
    ),
]:
    TOOL_DEFINITIONS.append(
        Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": {field: {"type": "string"} for field in fields},
                "required": [field for field in fields if field != "service"],
            },
        )
    )

TOOL_DEFS = {tool.name: tool for tool in TOOL_DEFINITIONS}

PROMPTS = {
    name: files("axon_mcp").joinpath("prompts", name + ".md").read_text(encoding="utf-8")
    for name in ("axon-maintain", "axon-onboard")
}


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [Prompt(name=name, description="Axon IDE-agent workflow") for name in PROMPTS]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
    if name not in PROMPTS:
        raise ValueError(f"unknown prompt: {name}")
    return GetPromptResult(
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=PROMPTS[name]))]
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_DEFINITIONS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    config = load_config()

    try:
        module_name = TOOL_TO_MODULE.get(name, name)
        module = getattr(tools, module_name)
        result = await module.handle(config, arguments, None, None)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        logger.exception(f"Error executing tool {name}")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def run_server():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def cli_main():
    parser = argparse.ArgumentParser("axon-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve")

    configure_parser = subparsers.add_parser("configure")
    configure_parser.add_argument("--api-url", default="http://localhost:8080")
    configure_parser.add_argument("--token", default="")

    subparsers.add_parser("status")

    args = parser.parse_args()

    if args.command == "serve":
        asyncio.run(run_server())
    elif args.command == "configure":
        configure_claude(args.api_url, args.token)
        print("Configured MCP adapter")
    elif args.command == "status":
        print("✓ Connectome service: ok")
