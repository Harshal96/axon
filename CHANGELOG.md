# Changelog

## Unreleased

- Link independently indexed repositories in one atomic schema-2 publication,
  with retained caller replay on target churn/removal and durable Neo4j restart.
  Explicit target repos and literal authority bindings keep same-named services
  distinct; missing/ambiguous targets retain graph evidence and diagnostics.
- Keep unbound absolute HTTP(S) authorities external, including same-repo path
  coincidences. Existing internal absolute calls may need service `base_urls`,
  caller client mappings or manual corrections. Relative/symbolic/URL-less weak
  fallback stays local; stable authorities support templated call paths/queries.
  Exact qualified service spelling wins before case-insensitive alias matching.
- Add qualified Ask/citation navigation and service graph aggregation, target repo
  fields in API corrections, indexed cross-repo impact (`--server`, `/impact/graph`),
  repository removal and source-path replacement. Local web impact stays head-only.
- Expose same-snapshot publication observations through health and HTTP headers.
  MCP supports schema 2 and server-scoped generation invalidation with TTL fallback
  and overlapping-request guards. Model query execution publication remains unknown.
- Schema 2 requires stopping old binaries and a one-time caller reindex for fresh
  replay evidence. Legacy pins remain explicitly incomplete. Changed source plus
  the full outbound projection is staged; synthetic scale results do not qualify
  production capacity. See `docs/validation/multi-repo-support.md`.

- Ask: prefer a primary self-hosted OpenAI-compatible generative
  endpoint with an optional generative fallback used only on
  connection, timeout, rate-limit, or 5xx failures
  (`LLM_FALLBACK_*`), plus an `LLM_ALLOWED_MODELS` allowlist. Drafts
  now include a schema card derived from the actual snapshot and are
  repaired with validator/executor feedback (up to two repairs).
  Ask evals score complex questions by executed evidence, pin repair
  recovery, and label the expected route per case.

- Open another API's page straight from a request-flow graph: non-centre API
  cards get an open button, and the node inspector Info tab gets the same
  action. Single click still selects and inspects.
- Add clone-free deployment: prebuilt `ghcr.io/harshal96/axon` and
  `axon-web` images (published by GitHub Actions on `main` and `v*`
  tags), an image-based `deploy/docker-compose.yml` with an `axon-init`
  one-shot service and a read-only `/repos` mount for indexing, and a
  `deploy/setup.sh` one-command installer that generates secrets.
  See `deploy/README.md`.
- Fix parse diagnostics on Python 3.12 and newer: the invalid escape warning
  changed wording there, so it leaked to stderr and never reached the graph.
  Messages are stored in the pre-3.12 wording on every version. Help output
  tests no longer break when the runner forces ANSI color.

- Version persisted graphs: snapshot JSON, Neo4j publication markers and Repo
  nodes carry a graph schema version, readers reject newer graphs with the fix
  named, and `axon.yaml` rejects a version above the supported one. Schema-1
  compatibility artifacts remain readable; schema-2 migration and caller reindex
  requirements are described above.
- Add read-only `axon upgrade-check`, previous-generation retention with
  `axon graph-rollback`, a safety snapshot before upgrade publishes,
  `axon snapshot-import`, and `axon graph-compare`. Health reports the server
  and graph schema versions, and the CLI and MCP adapter warn on schema skew.
- Freeze schema 1 artifacts under `tests/fixtures/compat/` and pin node ID
  formats, `props` keys per node kind, and API response shapes in tests.

- Split static graph/core, CLI with bundled agent skills, and server into
  separate uv workspace distributions; slim CLI/core imports do not require
  FastAPI or Neo4j. Package publishing remains a separate release step.
- Add local dry-run configuration maintenance, source tracking, generated-file
  tombstones, opt-in scoped team overlays and repo-wide client id validation.
- Add evidence-backed client proposals, scoped manual graph corrections,
  diagnosis/reference views, isolated impact previews and service-fresh indexing.
- Ship onboarding/maintenance skills with versioned, guarded installation and
  expose read-only MCP diagnosis tools/prompts. Static-analysis gaps remain
  explicit; configuration writes stay in the local CLI.

- Expand overloaded symbol spans to cover every declaration and record their count.
- Deduplicate Go Handle and HandleFunc route facts for the same registration.
- Exempt annotation-only route-to-handler gaps from verify-sample review flags.

- Recover DRF router URL-list mounts and Java annotation string/constant paths.
- Add conservative path specificity, literal parameter binding, scoped base
  consensus and generated operation/source identity rules.
- Recognize Java generated HTTP transports, locate declared client sources,
  suppress model/transport noise and report SDK dependencies separately.
- Replace manual-only cache keys with AST parser/build/state fingerprints.
  Existing graphs require one automatic full rebuild after upgrading; stale
  same-commit graphs and resolved facts are not reused.
- Keep informational parse warnings compatible with incremental indexing;
  retry degraded files and reserve full fallback for unsound graph synthesis.
