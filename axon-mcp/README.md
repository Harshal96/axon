# Axon MCP Adapter

`axon-mcp` is a small stdio MCP server that proxies the Axon `/api/v1`
surface to MCP clients. It does not index code itself: run the Axon API
first, index a repository, then start the adapter. On its first call the
adapter checks `/api/v1/health` and logs a warning when the server reports a
graph schema version it does not support; upgrade the older side in that case.

## Tools

| Tool | Purpose |
|---|---|
| `axon_search` | Search indexed APIs, functions, and services |
| `axon_services` | List services and API counts, optionally filtered by repo |
| `axon_api_graph` | Fetch one API's callers, handler code, and outbound APIs |
| `axon_function_apis` | Find which APIs include a function or method |
| `axon_ask` | Ask a graph-grounded natural-language question with citations |
| `axon_api_explain` | Explain outbound resolution, missing targets and candidate evidence |
| `axon_client_show` | Inspect a client using its full qualified node ID when ambiguous |
| `axon_refs` | Read graph references for one repository entity |

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

Schema 2 is supported. GETs use a 300-second default TTL, partitioned by configured
server URL and auth scope. Health on the first call per server, response headers
(`X-Axon-Publication-Id`) and object `publication_id` fields supply observations.
A changed nonempty ID clears that server's current and expired entries; the first
ID clears legacy/no-ID entries too. Empty IDs preserve known observations. Cache
hits do not poll health, so TTL semantics apply between actual server requests.
Overlapping old responses cannot refill cache after a newer observation.
An overlapping response with a conflicting nonempty ID also evicts and fences
cached entries before returning, since publication IDs have no ordering. The
next uncached server response revalidates the cache; error headers follow the
same rule. Old
servers without IDs retain TTL behavior; upgrading the adapter retires its old
unscoped cache entries.

If the API becomes unreachable, an expired cached GET may be returned. Object
responses gain a `STALE_FALLBACK` warning; array responses keep their compatible
shape and emit that warning to the adapter log. POST Ask is never cached.
Deterministic Ask identifies its input publication. Model Ask leaves publication
unknown because query execution publication is not captured; the adapter never
assigns a drafting snapshot's ID to those results.

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
qualified service name such as `finance::ledger`; bare duplicate names clarify. Ambiguous names request clarification. Quote
code symbols or use “function <name>” to distinguish them from ordinary words.

The obsolete `axon-mcp-listener` command has been removed. The current API
does not expose the old graph-update WebSocket endpoint; responses use the
cache TTL and explicit invalidation documented above.


Cross-repository examples (index both checkouts into the same server first):

```json
{"name":"axon_services","arguments":{"repo":"finance"}}
{"name":"axon_api_graph","arguments":{"endpoint_id":"endpoint::finance::ledger::GET:/entries"}}
{"name":"axon_ask","arguments":{"question":"Which APIs in orders::orders call finance::ledger?"}}
{"name":"axon_api_explain","arguments":{"repo":"orders","service":"orders","api":"GET /run"}}
```

Full graph IDs, qualified citations, confidence, file/line evidence, typed
missing-target reports and candidate nodes pass through unchanged. Legacy caller
inputs retain their explicit reindex/fresh-matching blind spots even with zero
historical calls; cache invalidation
does not turn historical evidence into fresh inference.
