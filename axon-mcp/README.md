# Axon MCP Adapter

`axon-mcp` is a small stdio MCP server that proxies the Axon `/api/v2`
surface to MCP clients. It does not index code itself: run the Axon API
first, index a repository, then start the adapter.

## Tools

| Tool | Purpose |
|---|---|
| `axon_search` | Search indexed APIs, functions, and services |
| `axon_services` | List services and API counts, optionally filtered by repo |
| `axon_api_graph` | Fetch one API's callers, handler code, and outbound APIs |
| `axon_function_apis` | Find which APIs include a function or method |
| `axon_ask` | Ask a graph-grounded natural-language question with citations |

## Run

From the repository root:

```bash
# terminal 1: Axon API
uv run axon-server

# terminal 2: save adapter settings, then run the stdio server
uv run --project axon-mcp axon-mcp configure --api-url http://localhost:8080
uv run --project axon-mcp axon-mcp serve
```

`configure` writes `~/.axon/config.json` and adds an `axon` MCP server to
`~/.claude.json` for Claude Code. If the Axon API is running with
`API_AUTH_MODE=dev`, pass its token too:

```bash
uv run --project axon-mcp axon-mcp configure \
  --api-url http://localhost:8080 \
  --token "$API_DEV_TOKEN"
```

For another MCP client, configure the same stdio command manually:

```json
{
  "mcpServers": {
    "axon": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/path/to/axon/axon-mcp",
        "axon-mcp",
        "serve"
      ]
    }
  }
}
```

GET responses are cached locally for a short period. If the API becomes
unreachable, an expired cached GET response may be returned with a
`STALE_FALLBACK` warning rather than presented as fresh data.

## Development

```bash
uv run --project axon-mcp --frozen pytest axon-mcp/tests
```

The adapter intentionally stays on the MCP Python SDK 1.x low-level
`Server` API; `pyproject.toml` pins `mcp<2` until it is migrated.
