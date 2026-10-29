# AGENTS.md

Guidance for coding agents and contributors working on Axon itself. For the
user-facing setup, start with `README.md`. For contribution workflow and test
tiers, see `CONTRIBUTING.md`.

## What Axon is

Axon is an API-first code graph. It parses source code directly, uses
`axon.yaml` for service ownership, and lets users inspect one API at a time:
handler code, outbound calls, in-graph callers, PR impact, and graph-grounded
answers with citations.

The active implementation is v2. Read `docs/v2.md` before changing graph,
parsing, linking, storage, ask, or impact behavior.

## Repository map

- `axon/v2/` — active graph model, parsers, indexer, linker, stores, API
  routes, ask, impact, and onboarding.
- `axon/api/app.py` — FastAPI composition root; mounts health and `/api/v2`.
- `cli/` — the `axon` command-line interface.
- `axon-mcp/` — separately packaged MCP stdio adapter for `/api/v2`.
- `web/` — React/Vite service and API catalog plus Ask agent page. See
  `web/README.md` for its `base/`, `components/`, and `src/` layers.
- `tests/v2/` and `tests/cli/` — infrastructure-free tests for the active
  implementation.
- `tests/evals/` — per-language/framework parser contract evals. See
  `docs/evals.md`; `matrix.json` is the coverage source of truth.
- `tests/tier1_contract/`, `tests/tier2_scenario/`, `tests/tier3_system/`,
  `tests/regression/`, and most top-level `tests/test_*.py` — legacy and
  integration suites. Many need Neo4j and a running API.
- `docs/v2.md` — v2 design document.
- `examples/` — copyable integrations for target repositories.

Legacy v1 packages remain under `axon/graph`, `axon/ops`, `axon/history`,
`axon/pipeline`, `axon/breaking`, `axon/workers`, and most of
`axon/api/routes`. They are not wired into the current app except where v2
explicitly reuses a component, such as the Swagger parser. Do not extend or
delete legacy code in an unrelated change.

## Design invariants

- One API endpoint in one service is the unit of navigation.
- Ownership comes from `axon.yaml`, never from Bazel or another build graph.
- Answers must be grounded in graph rows. If the graph proves nothing, say
  "unknown" and name the static-analysis blind spot. Never fill gaps from
  model memory.
- Inferred edges should retain confidence, the rule that produced them, and
  file/line evidence where the model supports it.
- Unresolvable outbound calls become `ExternalApi` placeholders and are
  reported; do not silently drop them.
- A model may draft a read-only Cypher query only. Keep query validation,
  execution, and answer synthesis separate.
- Axon has no runtime telemetry. Do not invent latency, traffic, error-rate,
  health, or usage numbers. Web signals must be derived from graph data.

## Local commands

Install dependencies:

```bash
uv sync --dev
cd web && npm ci
```

Run the API and web UI:

```bash
uv run axon-server
cd web && npm run dev
```

Useful checks that do not need external services:

```bash
uv run pytest tests/v2 tests/cli tests/evals
uv run python -m tests.evals.harness
uv run ruff check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
uv run ruff format --check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
uv run mypy axon/v2 tests/evals
uv run --project axon-mcp --frozen pytest axon-mcp/tests
cd web && npm run typecheck && npm test && npm run build
```

Plain `uv run pytest` runs the same infrastructure-free suite because
`pyproject.toml` sets `testpaths` to `tests/v2`, `tests/cli`, and
`tests/evals`. Legacy
integration suites are opt-in by explicit path; their root fixtures need
Neo4j, and some also need a running API. See `CONTRIBUTING.md` for setup.

## Web rules

- Follow `web/README.md`: tokens first, classes before inline styles, and
  primitives before bespoke markup.
- App and component code should import through `@base` and `@components`.
- Keep pure `web/base/*.ts` and `web/src/lib/*.ts` modules on relative imports
  so Node's test runner can load them directly.
- Preserve keyboard access, visible focus, and `prefers-reduced-motion`
  behavior. Call direction must remain readable with animation paused.

## Fixtures and generated files

- Files under `tests/fixtures/` are parser inputs, not tests to collect.
  Some are deliberately incomplete applications. Do not "fix" imports there
  unless the fixture's parser contract itself is changing.
- Parser changes must pass `tests/evals/`. A new parser language or
  framework needs a fixture, a case in `tests/evals/cases/`, and a row in
  `tests/evals/matrix.json` in the same change; the coverage guards fail
  otherwise. Eval cases are hand-authored contracts, not snapshots to
  regenerate from observed output.
- Update `uv.lock` when Python dependencies change and `web/package-lock.json`
  when web dependencies change.
- Do not commit `.env`, secrets, `dist/`, `node_modules/`, virtualenvs, or
  tool caches.

## Documentation

- User-visible CLI, API, graph, or workflow changes should update the README
  or `docs/v2.md` in the same change.
- Web structure changes should update `web/README.md`.
- MCP tool or configuration changes should update `axon-mcp/README.md` and,
  when relevant, the MCP section in the root README.

## Security posture

Axon is local-first. The API default has no authentication, permissive CORS,
and endpoints that can index repository paths readable by the server process.
Do not present the default server or Compose credentials as safe for public
internet exposure. Development only credentials in examples and Compose files
must stay clearly labeled as such.
