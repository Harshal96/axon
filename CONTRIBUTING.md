# Contributing to Axon

Thanks for helping improve Axon. Keep changes focused, include the evidence
behind them, and update the relevant documentation in the same pull request.

By contributing, you agree that your contribution is licensed under the
project's MIT License.

## Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 22 or newer and npm, for the web UI
- Docker, only for Neo4j-backed runs and legacy integration tests

## Set up

```bash
git clone https://github.com/Harshal96/axon.git
cd axon
uv sync --dev
cd web && npm ci && cd ..
```

Run the in-memory API and web UI:

```bash
# terminal 1
uv run axon-server

# terminal 2
cd web && npm run dev
```

Then index a repository that contains `axon.yaml`:

```bash
uv run axon index --repo /path/to/repo
```

The web UI is at `http://127.0.0.1:5174`. If the API is not running, the
main build says "API unreachable" and shows no data. For a backend-free
look at the UI, run the separate demo build (`cd web && npm run dev:demo`);
demo data is never bundled into the main build.

## Fast checks

Run the infrastructure-free gate before opening a pull request. Plain
`uv run pytest` is equivalent to the explicit test command below because the
default `testpaths` cover the active v2, CLI, and eval suites.

```bash
uv run pytest tests/v2 tests/cli tests/evals
uv run python -m tests.evals.harness
uv run ruff check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
uv run ruff format --check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
uv run mypy axon/v2 tests/evals
uv run --project axon-mcp --frozen pytest axon-mcp/tests
cd web && npm run typecheck && npm test && npm run build
```

Changes to packaging or command entry points also need an installed-wheel
smoke test:

```bash
uv build
cd "$(mktemp -d)"
uv run --no-project --with /path/to/axon/dist/axon-*-py3-none-any.whl axon --help
```

Replace `/path/to/axon` with your checkout path.

## Integration tests

The wider legacy suites are not part of the fast gate or the default
`testpaths`. Invoke them by explicit path when a change touches legacy code.
Their root fixtures in `tests/conftest.py` expect:

- Neo4j at `TEST_NEO4J_URI` (default `bolt://localhost:7688`)
- the API at `TEST_API_URL` (default `http://localhost:8081`)
- the development bearer token in `TEST_API_TOKEN` when the API runs with
  `API_AUTH_MODE=dev`

The test Compose file provides those services:

```bash
docker compose -f docker-compose.test.yml up -d --build --wait
uv run pytest tests -m "tier1 or regression"
docker compose -f docker-compose.test.yml down
```

Tier2 tests are legacy scenario tests and tier3 tests are slower system
tests. Run the tier that matches the legacy behavior you changed:

```bash
uv run pytest tests/tier2_scenario -m tier2
uv run pytest tests/tier3_system -m tier3
```

These legacy tiers are also available as the manual legacy jobs in
`.github/workflows/ci.yml`; they are not part of the active push and pull
request gate.

Do not treat a missing local Neo4j or API as a passing integration result.
Say which tiers you ran and which you could not run.

## Change guidelines

- For bugs, reproduce the failure first when practical and add or update the
  smallest useful regression test.
- For parser changes, run the language/framework evals in `tests/evals/`.
  A new language or framework needs a fixture, an eval case, and a matrix
  row in the same pull request; see `docs/evals.md`. Do not update an eval
  expectation from observed output without verifying the fixture intent.
- For new graph behavior, keep inference honest: preserve evidence, report
  unresolved calls instead of dropping them, and return "unknown" when the
  graph proves nothing.
- For API or CLI changes, update `README.md` or `docs/v2.md` with the new
  contract.
- For web changes, verify the real interface, not only typechecking and unit
  tests. Preserve reduced-motion behavior and do not add invented runtime
  telemetry.
- Files under `tests/fixtures/` are parser inputs. They are excluded from
  pytest collection and may intentionally contain incomplete applications.
- Use short conventional commit subjects, for example
  `fix(ci): keep fixture apps out of pytest collection`. Lowercase after the
  prefix and no trailing period.

## Reporting bugs

Open a GitHub issue with:

- what you ran, including the exact command or API request
- what happened and what you expected
- Axon version or commit, operating system, and Python/Node versions
- the smallest repository or fixture shape that reproduces it, if you can
  share one safely

Do not include secrets, access tokens, or private source code in public
issues. For vulnerabilities, follow `SECURITY.md` instead of opening a
public issue.
