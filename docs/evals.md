# Language and framework evals

The evals in `tests/evals/` are semantic regression contracts for the v2
parsers. Each case indexes a small fixture repository with `index_repo()`
and compares the graph with a human-authored expectation: services,
endpoints, handler names, framework tags, OpenAPI `source`, outbound links,
service-map aggregation, source-file count, and unresolved-call count.

They are not generated snapshots. When a parser change breaks a case, fix
the parser or update the contract in the same change with a reason. Never
copy observed output into a case without reading the fixture and deciding
that the new behavior is the intended contract.

## Coverage matrix

`tests/evals/matrix.json` is the machine-readable source of truth.

| Language | Framework | Cases | What is covered |
| --- | --- | --- | --- |
| Python | FastAPI | `python-fastapi-monorepo`, `python-fastapi-router` | Decorators, `APIRouter` prefix, PUT/DELETE, generated client linking, OpenAPI code/spec/both reconciliation, unresolved external call |
| Python | Django | `python-django-urls` | `path()` converters, `url()` with a literal route, `ANY` methods, external `httpx` call |
| Java | Spring | `java-spring-basic`, `java-spring-webclients` | Class `@RequestMapping` prefix, GET/POST/PUT/PATCH/DELETE, `RestTemplate` and WebClient links |
| Java | Vert.x | `java-vertx-basic`, `java-vertx-router` | Router registrations, handler method references, WebClient links |
| Go | net/http and router style | `go-net-http`, `go-router` | `HandleFunc`, method-prefixed patterns, router verbs, `:id` normalization, `http.Post` and `http.NewRequest` links |
| TypeScript | Express | `typescript-express-known-gap` | Known gap. The case pins the current result: the service exists in `axon.yaml`, but v2 parses zero TypeScript source files and zero endpoints |

Known limitations are still documented in `docs/v2.md`. In particular:
FastAPI prefixes from cross-file `include_router()` composition, Django REST
framework viewsets, generic Spring `RequestMapping` method extraction, most
Vert.x lambda handlers, and Go routes built from variables are not claimed
by these evals.

## Run them

The cases run as part of the default infrastructure-free pytest gate:

```bash
uv run pytest
```

For a compact per-case report, run the harness directly:

```bash
uv run python -m tests.evals.harness
uv run python -m tests.evals.harness python-fastapi-router
uv run python -m tests.evals.harness --json eval-report.json
make evals
```

CI runs the same cases through pytest, then runs the harness with `--json`
and uploads the report as the `axon-language-evals` artifact.

## Case format

Each file in `tests/evals/cases/` contains:

- `id`, `language`, `framework`, and `repo` (a path under `tests/fixtures/`)
- `variant` — the parser behavior the case exists to pin
- `services` — exact service ids expected in the graph
- `endpoints` — exact `service`, `method`, and `path`, plus `handler`,
  `framework`, `source`, and `operation_id` when they are part of the contract
- `links` — endpoint-to-endpoint or endpoint-to-`external:` outbound refs
- `service_map` — aggregated service edges and counts
- `source_files` and `unresolved_count` — guard against silent parser drops

Endpoint refs use `service:METHOD /path`. Unresolved external targets use
`external:METHOD /path`.

## Add a language or framework

1. Add the parser to `axon/v2/indexer.py` and a minimal fixture repo under
   `tests/fixtures/evals/<language>-<framework>/`.
2. Add at least one case JSON covering route extraction, at least two HTTP
   methods, one linked call or documented unresolved call, and one edge the
   framework makes easy to break (prefix, path parameter, or client style).
3. Add the language/framework row to `tests/evals/matrix.json`.
4. Update this file and the supported-framework notes in `docs/v2.md`.
5. Run `make evals` and the full fast gate.

Two guard tests fail if coverage drifts:
`test_eval_matrix_covers_every_supported_framework` ties the matrix to the
case files, and `test_every_parser_language_has_eval_coverage` ties the
parser registry in `axon/v2/indexer.py` to the eval languages. Adding a
parser extension without evals is therefore a failing build.

A `known-gap` case is allowed only when the framework is explicitly not
supported. Its expected graph must state the current behavior exactly, so
support cannot appear (or regressions hide) without someone updating the
matrix and turning the gap into a real case.
