# Multi-repository Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Resolve, retain and explain API calls between independently indexed repositories in one Axon instance.

**Architecture:** Preserve repo-qualified graph identities and repository-local parsing. Persist prepared caller evidence, resolve against an immutable shared endpoint catalog, and atomically publish repo structure plus a globally versioned outbound-link projection. Reconcile unchanged callers on every repository publication without opening their checkouts.

**Tech Stack:** Existing Python/Pydantic facts and graph code, MemoryStore/Neo4j, FastAPI/Typer, React/TypeScript and the MCP adapter; no new runtime dependency planned.

**Spec:** `docs/superpowers/specs/2026-11-15-multi-repo-design.md` (implementation authorized 2026-11-16).

**Confirmed scope (2026-11-16):** One shared Axon instance. Federation between
separate instances is outside the first release. Detailed design approval and
implementation were authorized by the subsequent "implement" request.

## Global Constraints

- One Axon server/store; one server process/writer per database.
- Ownership comes only from each repository's `axon.yaml`.
- Omitted target repo means the caller repo; explicit missing targets never fall through.
- Local symbol `CALLS` remain repository-local; integration occurs at `CALLS_API` boundaries.
- Foreign path/operation matches without a target or host binding are diagnostic candidates, not resolved edges.
- Unbound concrete absolute HTTP(S) authorities stay external before caller-local
  fallback too. Only relative/symbolic/URL-less weak fallback stays caller-local;
  same-repo absolute calls need a service/client/manual binding. Stable authorities
  may have templated call paths/queries; dynamic authorities/base declarations
  never become literal ownership.
- Every edge retains source evidence, matching rule, confidence and binding trace; no runtime telemetry.
- Published snapshots are immutable. Reconcile/compare/publish is serialized globally after parsing.
- New fields are additive with defaults; public node IDs and existing API response fields remain stable.
- Do not require unchanged caller checkouts, disposable caches or `state.db` for relinking.
- Existing untracked `.worktrees/` and unrelated `feat/ask-accuracy` work are outside this change.

## Review Focus

1. Foreign placeholder/candidate repo IDs must never select repositories for replacement (Task 1).
2. Duplicate operation IDs and parameter routes in same-named services must be scoped before uniqueness tests (Task 2).
3. A staged relationship between unchanged active nodes must remain invisible to hydration and Cypher (Task 4).
4. Target-only updates, same-commit caller skips, restarts and absent caller checkouts must still reconcile links (Tasks 3–5).
5. Reused service IDs and source paths must remain distinct in Ask, web aggregation and impact (Tasks 6–7).

## File responsibilities and sequence

New focused modules:

- `axon/v1/link_inputs.py`: typed, versioned replay inputs, legacy pinned inputs and codecs.
- `axon/v1/endpoint_catalog.py`: immutable catalog and repo/service-scoped lookup.
- `axon/v1/repository_links.py`: pure preparation/reconciliation and per-repo reports.
- `axon/v1/link_publication.py`: Neo4j projection rows, visibility and staging helpers used by `store.py`.
- `tests/v1/test_multirepo_linking.py`: hand-authored two/three-repo functional contracts.
- `tests/v1/test_link_publication.py`: projection publication/visibility/recovery contracts.

Keep existing extraction in `indexer.py`, structural/delta ownership in
`graph_delta.py`, scheduling in `incremental.py`/`jobs.py`, and HTTP composition
in `api.py`. Do not make `store.py` responsible for parsing source.

Tasks 1–2 define independent data/catalog boundaries. Task 3 uses both. Task 4
persists Task 3 results. Task 5 wires scheduling and lifecycle. Tasks 6–7 consume
the published graph and can proceed independently once their API contracts are
fixed. Each task uses a failing regression, implementation, passing focused
checks and a short commit; commits are execution steps, not part of planning.

## Task 1: Persist caller-owned inputs and make replacement scope explicit

**Files:** Create `axon/v1/link_inputs.py`, `tests/v1/test_link_inputs.py`; modify
`axon/v1/graph.py`, `graph_delta.py`, `manual_corrections.py`,
`tests/v1/test_graph_delta.py`, `test_compat.py`, `test_manual_corrections.py`.

**Interfaces:**

- `PreparedCallInput`: typed record with `caller_id: str`, `file: str`,
  `owner_kind: str`, `owner_id: str`, `argument_count: int`,
  `static_skip_reason: str`, and `fact: HttpFact`.
- `PinnedCallInput`: full legacy `GraphEdge`, expected logical target ID and
  target display metadata; no reconstructed `HttpFact`.
- `RepoLinkInputs`: `format_version=1`, `repo_id`, `source_generation`,
  `source_commit`, `service_map: ServiceMap`, `manual_sources: dict[str,str]`,
  prepared/pinned input lists and static source suppression counts.
- `RepoLinkReport`: repo-owned unresolved strings, link diagnostics and
  per-service coverage. Parser diagnostics retain their existing separate role.
- Add default-empty `GraphSnapshot.link_inputs: dict[str, RepoLinkInputs]` and
  `link_reports: dict[str, RepoLinkReport]`, plus `publication_id: str = ""`.
- Extend `GraphDelta` metadata copying/merging so input/report entries are
  replaced or removed by explicit source repo, and publication IDs survive
  snapshot copy, diff/apply, import and export.
- `replace_repositories(base, incoming, *, repo_ids: set[str] | None = None)`:
  explicit set for all new callers; compatibility default derives only actual
  `Repo` nodes. Replace edges by source ownership and validate final targets;
  target replacement alone does not erase unchanged incoming logical edges.

- [x] Add failing tests: foreign missing-call placeholders do not replace B;
  replacing B retains A→B when the endpoint ID exists; empty B removes its old
  rows; snapshot input/report round trips; schema-1 fixture loads empty inputs;
  copying a snapshot does not share mutable input/report containers.
- [x] Run `rtk uv run pytest tests/v1/test_link_inputs.py tests/v1/test_graph_delta.py tests/v1/test_manual_corrections.py tests/v1/test_compat.py -q`; verify new assertions fail for intended reasons.
- [x] Implement the interfaces and codecs. Placeholder storage ownership is
  global/empty; expected remote identity is in `target_repo`/`target_service`.
  Preserve evidence-bearing edge keys and existing public IDs.
- [x] Rerun the same checks; expect all pass. Commit `feat: retain outbound linking inputs`.

## Task 2: Build an immutable, scoped endpoint catalog

**Files:** Create `axon/v1/endpoint_catalog.py`,
`tests/v1/test_endpoint_catalog.py`; modify `axon/v1/linker.py`,
`servicemap.py`, `manual_corrections.py`, `tests/v1/test_path_fallback.py`,
`test_manual_corrections.py`.

**Interfaces:**

- `ServiceRef = tuple[str, str]` in `(repo_id, service_id)` order.
- `EndpointCatalog.from_snapshot(snapshot: GraphSnapshot) -> EndpointCatalog`:
  includes prospective endpoints and literal service base declarations from
  retained inputs; computes specificity without writing node properties.
- Catalog methods `by_method_path(method, path, *, scopes: frozenset[ServiceRef])`,
  `by_operation(call_name, *, scopes)`, `literal_parameter(method, path, *, scopes)`
  and `by_path_suffix(method, path, *, scopes)` scope before candidate algorithms.
- `literal_base_scopes(url: str) -> frozenset[ServiceRef]` uses exact normalized
  authority and longest segment-boundary base path; equal specificity competes.
- `get_node(node_id: str) -> GraphNode | None` provides read-only target lookup.
- Add `Suppression.to_repo: str = ""`; matching uses the caller repo when empty.

- [x] Add failing cases: B/api and C/api each having one `list_items` must not
  make B's `list_items_1` a duplicate-operation match; explicit B scope can bind
  `/v1/items/123` despite C's identical template; constructing the catalog leaves
  input nodes byte-for-byte unchanged. Pin authority/port/base-path precedence,
  host collisions, local symbolic-base isolation and repo-scoped suppression.
- [x] Run `rtk uv run pytest tests/v1/test_endpoint_catalog.py tests/v1/test_path_fallback.py tests/v1/test_manual_corrections.py -q`; confirm intended failures.
- [x] Implement catalog-backed matching. Keep an `EndpointIndex` compatibility
  wrapper while migrating callers, using explicit local scope for relative,
  symbolic and URL-less weak fallback. Concrete unbound absolute authorities stop
  before weak local matching; add declarations to migrate prior coincident paths.
  Never add foreign candidate nodes to a repository's structural snapshot.
- [x] Rerun the focused checks; expect pass. Commit `feat: scope endpoint resolution across repositories`.

## Task 3: Extract pure replay and shared-graph reconciliation

**Files:** Create `axon/v1/repository_links.py`,
`tests/v1/test_multirepo_linking.py`; modify `axon/v1/indexer.py`, `linker.py`,
`manual_corrections.py`, `graph_delta.py`, `tests/v1/test_indexer.py`,
`test_client_factory_returns.py`, `test_client_resolve.py`.

**Interfaces:**

- `prepare_link_inputs(repo_snapshot, service_map, resolved_files,
  manual_sources) -> RepoLinkInputs`: capture sites before catalog-dependent
  filtering, including possible real zero-argument accessor operations.
- `resolve_repo_links(structure: GraphSnapshot, inputs: RepoLinkInputs,
  catalog: EndpointCatalog) -> RepoLinkResult`: typed result of edges,
  placeholders and a `RepoLinkReport`; no IO or source parsing.
- `reconcile_repositories(current: GraphSnapshot, replacements: GraphSnapshot,
  *, replace_repo_ids: set[str], remove_repo_ids: frozenset[str] = frozenset())
  -> GraphSnapshot`: constructs complete prospective structure/catalog, replays
  all caller inputs and assembles the new graph/report/input containers.
- `index_repo` keeps its current public return type. It attaches prepared inputs
  and resolves its isolated local view for local/`--out` compatibility.

- [x] Hand-author A/orders, B/ledger and C/ledger fixtures with generated-client,
  direct HTTP and manual calls. Add failing tests for both index orders, explicit
  target missing/arriving, target route/service removal, host-ownership collision,
  caller reindex removal of a call, and idempotent replay. Assert exact IDs,
  confidence, evidence lines, matching rules, binding traces and candidate IDs.
- [x] Add tests proving absent remote bindings stay diagnostic candidates;
  a declared missing target never resolves to C; currently suppressed accessor
  candidates can become real operations after B arrives; reports for unchanged
  repos survive another repo's publication. Legacy pinned links retain evidence,
  show `link-inputs-missing`, and become placeholders if their targets disappear.
- [x] Run `rtk uv run pytest tests/v1/test_multirepo_linking.py -q`; verify new contracts fail before wiring replay.
- [x] Extract `_link_http`, consensus, manual calls and suppression into pure
  replay using catalog lookup. Clone facts before assigning bound/base fields.
  Regenerate all outbound coverage and per-repo link reports; aggregate legacy
  `unresolved` output without overwriting other repos' reports. SDKs remain
  external dependencies, outside unresolved inter-service coverage.
- [x] Rerun the focused test plus `rtk uv run pytest tests/v1/test_indexer.py tests/v1/test_client_factory_returns.py tests/v1/test_client_resolve.py -q`; expect pass. Commit `feat: reconcile calls against the shared repository catalog`.

## Task 4: Publish structure and link projection atomically in both stores

Execution split (2026-11-16): 4a implements and reviews the MemoryStore
publication coordinator; 4b implements and reviews the complete Neo4j protocol
against that interface. All requirements below remain in scope. Task 4 is
complete only when both parts and live validation pass.

**Files:** Create `axon/v1/link_publication.py`,
`tests/v1/test_link_publication.py`; modify `axon/v1/store.py`,
`graph_delta.py`, `tests/v1/test_store.py`, `test_neo4j_publication.py`,
`test_compat.py`, `test_upgrade.py`.

**Interfaces:**

- Extend `GraphStore` with `publish_repository(replacement: GraphSnapshot, *,
  repo_id: str, validate: Callable[[GraphSnapshot], None] | None = None)
  -> GraphSnapshot` and `remove_repository(repo_id: str) -> GraphSnapshot`.
  These own lock/reload/reconcile/compare/commit/swap. Run the caller-owned
  validator on the reconciled candidate before any final marker switch;
  validator failure leaves the old publication live.
- Existing `save`/`apply_delta` stay adapters for imports and local callers;
  they cannot bypass link projection publication or explicit ownership.
- Neo4j `_AxonLinkPublication {id: "global", generation, prev_generation}`
  selects `_axon_link_generation` on all `CALLS_API` relationships and external
  nodes. `_AxonLinkInput` rows use repo structural generation ownership, with
  separate manifest/report records. Hide these internal rows from public reads.
- Use existing `NEO4J_BATCH_SIZE` for bounded staging. Final transaction changes
  structural marker(s) and global link marker together; cache swap follows commit.

- [x] Add failing fake-store tests: staged links between unchanged nodes are
  invisible; target B full-generation replacement leaves A→B live after hydration;
  staged failure/cancellation leaves the prior graph and inputs intact; concurrent
  A/B publications retain both; old reader snapshots remain unchanged.
- [x] Add Cypher-scoper tests for named/anonymous bounded relationships,
  `OPTIONAL MATCH`, preexisting `WHERE`, relationship predicates, no internal-node
  leakage, and unsupported forms failing closed. Test snapshot import and legacy
  pinned bootstrap with no inferred HTTP facts.
- [x] Run `rtk uv run pytest tests/v1/test_link_publication.py tests/v1/test_store.py tests/v1/test_neo4j_publication.py -q`; confirm intended failures.
- [x] Implement staging with prospective logical→physical IDs and relationship
  generation filters in hydration and `scope_active_generations`. Store prepared
  inputs in generation-owned bounded rows; avoid one giant per-repo JSON property.
  Persist same-repo/manual/SDK edges in the same link projection. Distinguish an
  absent legacy global marker from a newly staged inactive link generation.
- [x] Implement rollback as retained structure+inputs followed by fresh replay
  against current other repos; test rollback B after A has independently changed.
  Preserve safety snapshot and bounded cleanup behavior.
- [x] Rerun focused checks; expect pass. Execute live Neo4j validation from
  `CONTRIBUTING.md`, forcing multi-batch structural and link stages with a small
  test batch size. Compare cached/rehydrated/JSON/Cypher results, then restart and
  repeat rollback/failure checks. Record live results separately; fake tests do
  not satisfy this gate. Commit `feat: publish repository links atomically`.

## Task 5: Integrate index jobs, freshness, identity and repo removal

**Files:** Modify `axon/v1/incremental.py`, `jobs.py`, `api.py`, `indexstate.py`,
`scoped_index.py`, `fingerprints.py`, `cli/app.py`, `cli/api_client.py`,
`tests/v1/test_incremental.py`, `test_jobs.py`, `test_api.py`,
`test_scoped_index.py`, `test_build_fingerprints.py`,
`tests/cli/test_cli_app.py`, `tests/v1/build_fingerprints.json`.

**Interfaces:**

- After parsing, `index_and_publish` calls `store.publish_repository` and uses
  the returned publication's repo reports for job results. Preserve existing
  return compatibility by returning the indexed repo view with current resolved
  outbound targets, not a stale pre-reconciliation local result.
- Index results add `publication_id`, `link_sites_replayed`, `link_reconcile_s`,
  `link_rows_staged`, `relinked_repos` and `link_inputs_missing`.
- `DELETE /api/v1/repos/{repo_id}` returns removed ID, new publication ID and
  affected caller reports. `axon repo remove --repo-id ID` invokes it.
- Index requests add `replace_source: bool = false` for explicit source-path
  reassignment of an existing repo ID; conflicting paths fail with HTTP 409.

- [x] Add failing integration tests: A parsed before B publishes must reconcile
  against B's latest catalog; unchanged-commit A does not bypass required link
  freshness; service/API scoped indexing retains full target context; two paths
  claiming one repo ID conflict unless explicitly reassigned; zero-service/empty
  target replacement and DELETE demote callers without touching checkout files.
  Pin that deletion does not reactivate legacy/staged rows: retain a publication
  tombstone while obsolete rows remain, including after cold hydration.
- [x] Remove caller checkout/cache/state and restart a persisted new-format
  graph; index B only and assert unchanged A's edge/report updates. Pin state
  audit to structural/source generation so unrelated link publications do not
  cause false graph-state mismatches or unnecessary reparsing.
- [x] Run `rtk uv run pytest tests/v1/test_incremental.py tests/v1/test_jobs.py tests/v1/test_scoped_index.py tests/v1/test_api.py tests/cli/test_cli_app.py -q`; confirm failures before wiring.
- [x] Route every publication path through the store coordinator, including
  synchronous index, jobs, correction recheck and imports. Preserve cancellation
  before final commit; report committed results as committed even if cancellation
  arrives afterward. Invalidate snapshot traversal/Ask indexes by pointer swap.
  Update fingerprints and state schema only where the format actually changes.
- [x] Pass the indexed repo's golden validator through `publish_repository`'s
  `validate` callback. Add a regression where a remote endpoint satisfies the
  indexed caller's golden only after global reconciliation, and one where a
  missing expected remote endpoint blocks publication of both structure and
  links. Target-only updates do not read or claim to revalidate unchanged
  callers' golden files. Golden requirements take precedence over indexing a
  caller before its required target.
- [x] Rerun focused checks; expect pass. Commit `feat: reconcile cross-repo links in index jobs`.

## Task 6: Make Ask, diagnostics, configuration and impact repo-aware

**Files:** Modify `axon/v1/ask_entities.py`, `ask.py`, `graph.py`, `diagnose.py`,
`doctor.py`, `configedit.py`, `maintenance.py`, `impact.py`, `api.py`,
`api_fix.py`, `cli/app.py`, `cli/api_client.py`, `cli/commands/maintain.py`,
`cli/commands/diagnose.py`, `axon/v1/golden.py`, `verify_sample.py`;
extend `tests/v1/test_ask_entities.py`,
`test_ask_impact.py`, `test_diagnose.py`, `test_config_cascades.py`,
`test_api_fix.py`, `test_api.py`, `test_accuracy_feedback.py`,
`test_client_golden_cascade.py`, `tests/cli/test_maintain_commands.py`,
`test_api_client.py`, `test_cli_app.py`.

**Interfaces:**

- Ask service aliases add `repo::service`; unqualified duplicates stay ambiguous.
  Counts, client gaps, references and correction targets use `(repo, service)`.
- `POST /api/v1/impact/graph` body: `{repo_id: str, changed_files: list[str]}`.
  Add `publication_id`, `indexed_commit`, `affected_endpoint_ids` and explicit
  baseline/deletion limitations without removing current impact fields.
- CLI `pr-impact --server --repo-id ID`, `ask --server`, `apis --server` read
  the server graph; explicit local and combined-snapshot modes remain available.
- Manual link/call/suppress commands accept `--to-repo`; web/API corrections
  round-trip it. Doctor/explain exposes typed missing-target reasons, qualified
  candidates, pinned-legacy blind spots and caller-scoped base suggestions.
- Golden targets accept `repo`. Default service/path targets to the caller
  repo, preserve URL-only external matching, and compare explicit remote repo
  against real endpoint ownership; placeholders remain diagnostic only. Export and cascade
  matching retain that qualifier.

- [x] Add failing tests: qualified Ask selects B/ledger; bare `ledger` clarifies
  with B/C choices; inbound/outbound counts and citations retain repo identity;
  changing B's indexed handler reports A's endpoint/repo as impacted; reused
  relative paths in C never seed the impact. Missing repo and stale indexed
  commit are explicit. Impact requests publish no graph changes.
- [x] Test correction/rename/remove cascades preserve references targeting a
  different repo; suppress B/ledger without suppressing C/ledger; CLI server
  modes use API calls rather than silently rebuilding an isolated graph.
- [x] Pin golden matching/export/cascades with B/ledger and C/ledger sharing
  method/path: only explicit B satisfies B's expectation; omitted repo means
  local; URL-only external selectors still work. Confirm no ignored selector
  key can silently broaden a repository-qualified expectation.
- [x] Run the corresponding focused test files; confirm new failures, implement
  the listed contracts, rerun and expect pass. Keep head-only local `/impact`
  behavior documented; do not imply deleted endpoints are covered without a
  baseline. Commit `feat: expose repository-qualified graph answers and impact`.

## Task 7: Present cross-repo results and validate the complete workflow

**Files:** Modify `web/src/lib/ask-format.ts`, `ask-graph.ts`,
`web/src/pages/AskPage.tsx`, `ApiPage.tsx`, `web/components/domain/graph.tsx`,
`web/base/routing.ts`, relevant web API types/client modules,
`web/tests/ask-format.test.mjs`, `ask-graph.test.mjs`, `routing.test.mjs`,
`axon-mcp/axon_mcp/cache.py`, `tools/_base.py`,
`axon-mcp/tests/test_cache.py`, `test_server.py`, `README.md`, `docs/v1.md`,
`web/README.md`, `axon-mcp/README.md`, `CHANGELOG.md`.

**Interfaces:** Web Ask nodes carry repo ID and service ID; service aggregation
uses complete `service::{repo}::{service}` IDs. Citations carry repo through
navigation. MCP cache stores observed publication ID and invalidates on a new
one; retain documented TTL semantics between observations.

- [x] Add failing web tests for two `ledger` services, a real edge between
  same-named services, foreign endpoint clicks before catalog load, and qualified
  service citations. Add MCP tests for unchanged full IDs/evidence, missing-target
  reports and cache invalidation after observing a new publication.
- [x] Run `rtk npm test` in `web` and `rtk uv run --project axon-mcp --frozen pytest axon-mcp/tests -q`; confirm intended failures.
- [x] Implement qualified labels, repo-aware grouping and correction target repo
  fields using existing primitives/tokens. Preserve keyboard/focus/reduced-motion
  behavior and API-first navigation. Do not add a whole-repo graph view.
- [x] Document two-repo YAML and indexing commands, no-order requirement, host
  versus symbolic binding rules, missing/ambiguous diagnostics, one-time legacy
  reindex, indexed revisions, server/local impact modes, removal, restart and
  rollback. Update MCP examples and web structure docs if changed.
- [x] Run infrastructure-free release gates from `CONTRIBUTING.md` with `rtk`:
  pytest v1/CLI/evals; eval harness; Ruff check/format; mypy; MCP tests; web
  typecheck/tests/build. No parser change is planned; if extraction changes,
  add the required hand-authored eval fixture/case/matrix row.
- [x] In a real browser, index A then B, follow A→B, inspect B's incoming caller,
  ask a qualified question, remove/change B and verify A updates. Repeat in
  reduced-motion mode and after server restart. Record browser and live Neo4j
  results separately from unit tests.
- [x] Measure replay/staging work using the existing scale benchmark approach
  with several repos and a high outbound-call count. Record nodes, sites,
  link rows, replay time, persistence time and peak memory; set optimization
  priorities from measured results, without an unmeasured latency promise.
- [x] Commit `feat: present cross-repository call evidence` and hand back for
  Task7 scoped review.
- [ ] After scoped review, root requests whole-branch review focused on publication
  visibility, target lifecycle and legacy behavior.

## Completion boundary

The feature is complete when new-format inputs support both index orders,
target-only churn, durable restart relinking, qualified navigation/Ask and
cross-repo impact; no published reader or Cypher query sees half a publication.
All required infrastructure-free, live Neo4j and browser gates must have
recorded results. Legacy pinned callers remain explicitly incomplete until
reindexed. Federation and broad unscoped foreign path inference are separate
future work.


## Reviewed integration correction (Task 7a)

`b2dac5d` (`fix: preserve URL authority and exact service identity`) closes the
real lifecycle failure found during Task7 browser qualification: after removing
B, a C caller's B-host URL must not resolve to C's same-path endpoint. This also
intentionally tightens same-repo absolute calls lacking declared host/client/manual
scope. Exact qualified service spelling precedes case-insensitive aliases.
Combined frozen Task 7 WIP + Task 7a gate: 909 passed, 10 dedicated live skips in
59.42s; final covering suite 181 passed, 35/35 parser evals (90 endpoints / 43
links), Ruff/mypy clean.
Only six reviewed fixture service base declarations changed; source/eval/matrix
contracts stayed fixed. Task7 final runtime/benchmark/artifact evidence and final
whole-branch review are recorded separately in the validation report.


## Local implementation and qualification status (2026-11-17)

Tasks 1–7 and reviewed integration Task7a are implemented. Required local gates:
909 Python passes / 10 opt-in live skips, 10 actual dedicated Neo4j passes,
35 parser cases (90 endpoints / 43 links), 18 MCP tests, 44 web tests, Ruff/mypy,
web typecheck/main+demo builds/demo exclusion, deterministic browser lifecycle/
keyboard/reduced-motion, four installed wheels with 12 module/source/fingerprint
checks and six CLI checks. Final four-repo/6,000-site memory and Neo4j scale
measurements are preserved with reproduction in
[`docs/validation/multi-repo-support.md`](../../validation/multi-repo-support.md).

Task7 commit is handed back for scoped review; root will request the final
whole-branch review afterward. Those reviews have not yet approved this branch.
Upstream CI, live model providers, production and 30,000-file qualification remain
unrun. Legacy pinned callers remain explicitly incomplete until reindexed.
