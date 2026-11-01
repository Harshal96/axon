# Axon MCP Adapter

`axon-mcp` is a small stdio MCP server that proxies the Axon `/api/v1`
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


`axon_function_apis` still requires only `q`. Optional `repo`, `used_in` (array
of `repo/service` keys) and `defined_in` (array of `repo/kind/id` owner keys)
restrict the lookup. Each result retains `symbol` and `apis` and adds `owner`
and `package`. Reach is static CALLS reach from indexed API handlers, bounded
to 12 hops; it does not measure runtime usage. Facet keys can be obtained from
`GET /api/v1/functions`.

```json
{"q":"helper","repo":"sample","used_in":["sample/alpha"],"defined_in":["sample/shared/libraries"]}
```

`axon_ask` retains `answer` and `citations` for text clients. Responses also
include `summary`, `rows` (source, target, caller citation, evidence rule and
confidence), `blind_spots`, and `bindings` explaining entity resolution. Confidence
uses the graph's existing enum strings, such as `INFERRED` and `AMBIGUOUS`.
Ask service relationships with “which APIs in scheduler call directory”; use a
full service ID or display name. Ambiguous names request clarification. Quote
code symbols or use “function <name>” to distinguish them from ordinary words.

The obsolete `axon-mcp-listener` command has been removed. The current API
does not expose the old graph-update WebSocket endpoint; responses use the
cache TTL and explicit invalidation documented above.
