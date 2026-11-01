# Contributing to Axon

Thanks for helping improve Axon. Keep changes focused, include the evidence
behind them, and update the relevant documentation in the same pull request.

By contributing, you agree that your contribution is licensed under the
project's MIT License.

## Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 22 or newer and npm, for the web UI
- Docker, only for Neo4j-backed runs

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
default `testpaths` cover the active v1, CLI, and eval suites.

```bash
uv run pytest tests/v1 tests/cli tests/evals
uv run python -m tests.evals.harness
uv run ruff check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals
uv run ruff format --check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals
uv run mypy axon/v1 tests/evals
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

## Neo4j validation

Default tests use in-memory stores and Neo4j test doubles. For persistence
changes, also validate against local Neo4j using `docker compose up -d neo4j`,
`AXON_STORE=neo4j uv run axon-init`, and `AXON_STORE=neo4j uv run axon-server`.
Report live validation separately from infrastructure-free checks.

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
- For API or CLI changes, update `README.md` or `docs/v1.md` with the new
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
