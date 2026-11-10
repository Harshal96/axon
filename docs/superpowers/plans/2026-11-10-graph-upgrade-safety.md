# Graph upgrade safety plan

Goal: a graph built with one Axon version still loads after an upgrade, or stops
early with an error that names the next step. New versions get trialled next to
the live graph, never on top of the only copy.

## Where things stand

Checked 2026-11-10 against the code at 0.2.0. The full test run passes (564
passed, 1 skipped), and no test in it loads an artifact written by an older
version.

- The disposable layers already fail safe. `SCHEMA_VERSION = 2` in
  `axon/v1/indexstate.py` makes old `state.db` files a cache miss, the facts
  cache carries a format version in `axon/v1/factscache.py`, and parser and
  indexer fingerprints in `axon/v1/fingerprints.py` force one full rebuild
  after an upgrade. The CLI reports that rebuild as `indexer-changed`.
- The durable layers carry no version. Snapshot JSON from
  `save_snapshot_file` in `axon/v1/store.py` has only nodes, edges, unresolved,
  diagnostics and commit. Neo4j rows store no schema version either. Renaming a
  node kind or edge kind breaks hydration with a raw `ValidationError`.
  Renaming a `props` key in `axon/v1/models.py` loads fine and returns empty
  answers.
- `version` in `axon.yaml` is never checked. `ServiceMap` in
  `axon/v1/servicemap.py` accepts `version: 99` today (checked by running the
  loader). Golden files are the one strict reader: `axon/v1/golden.py`
  requires `version: 1`.
- Starting `axon-server` with `AXON_STORE=neo4j` writes to the database before
  any index run, because `make_store` calls `ensure_schema()` and drops legacy
  constraints. Publishing stages a new generation and flips
  `_AxonPublication`, then deletes the old generation, so there is nothing to
  flip back to. No backup, restore, rollback or read-only command exists.
- There are no git tags yet, and `CHANGELOG.md` is one Unreleased block, so
  "the previous version" has no fixed meaning today.

## Decisions

These are the defaults this plan builds on. Change them here first if they are
wrong.

- The graph is derived data. The repo plus `axon.yaml` is the source of truth,
  and manual corrections live in `axon.yaml`, so a re-index recreates the whole
  graph. Rebuild is the default migration. In-database migration is for cases
  where a rebuild cannot recreate the data, and there are none today.
- A stale but readable graph serves reads and rebuilds in full at the next
  index. That matches the current `axon_build` stamp behavior. A graph that is
  unreadable or newer than the binary stops before any write, with an error
  that names both versions and the fix.
- Support window before 1.0: the current version reads artifacts written by
  the previous minor release. Older than that means re-index.
- Inside one schema version, format changes stay additive. Renaming or
  removing a node kind, edge kind, node ID format, `props` key, snapshot field
  or `axon.yaml` field needs a schema version bump, a new frozen fixture and a
  changelog line in the same PR.
- One writer per Neo4j database. Old and new binaries never share a database
  at the same time. Trials write to snapshot files or a cloned database.
- Destructive cleanup ships at least one release after the format change it
  cleans up: expand first, contract later.

## Do now

In order. Item 1 lands first so the fixtures capture the format before item 2
touches it. Item 3 is tests only.

- [x] 1. Freeze the current format as schema 1 fixtures, before any format
  change. Generate them with the code as it is: one snapshot JSON from a small
  fixture repo, one `state.db`, and fake Neo4j rows that include a legacy row
  with no `_axon_generation` and a relationship with no `_axon_key`. Put data
  under `tests/fixtures/compat/` and the tests in `tests/v1/test_compat.py` so
  the default `testpaths` picks them up. Write the rule in the test module:
  a committed fixture is never edited, a schema bump adds a new one. Verify
  each fixture by hand against its source repo before freezing it, the same
  rule `tests/evals/` uses for eval cases. Tag `v0.2.0` in the same step so
  fixture provenance stays checkable later.
  Check: fixtures load on unmodified main, `uv run pytest tests/v1/test_compat.py`.
- [x] 2. Stamp the graph schema and gate the readers. Add
  `GRAPH_SCHEMA_VERSION = 1` for the format as it exists now. Write it in
  three places: a `schema_version` field on `GraphSnapshot` in
  `axon/v1/graph.py` (default 1, so unstamped snapshot files still load), the
  `_AxonPublication` marker rows in `axon/v1/store.py`, and Repo node `props`
  next to `axon_build` in `axon/v1/indexer.py`. Readers accept versions up to
  the binary's own and stop above it. Wrap the unknown-kind `ValidationError`
  in hydration and snapshot loading so the error says what to do. Add the
  missing check in `axon/v1/servicemap.py`: reject an `axon.yaml` version
  above the supported one. Keep `extra="allow"` so new fields stay readable
  by design. Golden files keep their existing strict check.
  Check: old fixtures from item 1 load as schema 1, a fixture edited to
  version 99 fails with the new error in snapshot, Neo4j (fake session) and
  `axon.yaml` readers. Persistence changed, so also run the live Neo4j steps
  in `CONTRIBUTING.md` and report them separately.
- [x] 3. Pin the breaks that fail silently. Add tests that fail CI when these
  drift: exact strings from the node ID builders in `axon/v1/models.py`; the
  set of `props` keys per node kind, gathered by indexing fixture repos and
  compared to a committed JSON file, the same review-gate shape as
  `tests/v1/build_fingerprints.json`; response field sets for the main
  `/api/v1` GET routes, extending `tests/v1/test_api_version.py`. Add
  Hypothesis round trips for `GraphSnapshot` dump and load and for
  `facts_to_dict` / `facts_from_dict` in `axon/v1/facts.py`, where old cache
  payloads with missing fields must load or fall into the state store rebuild
  path. Hypothesis is already a dev dependency.
  Check: rename one `props` key on a scratch branch and confirm the new tests
  fail, then revert.
- [x] 4. Split connecting from migrating, then add a read-only preflight.
  `make_store` in `axon/v1/store.py` learns to open Neo4j without calling
  `ensure_schema()`. A new `axon upgrade-check` command in `cli/app.py` (name
  follows `verify-index`) reports the stored graph schema, `axon_build`
  against the current indexer fingerprint, `state.db` version and the
  `fallback_reason` verdict, plus the snapshot schema when `--snapshot` is
  passed. It writes nothing: no `ensure_schema`, no state save.
  Check: run it against the compose Neo4j before and after `axon-init` and
  confirm `SHOW CONSTRAINTS` output is unchanged by the check itself. Add
  fake-store coverage in `tests/cli/`.
- [x] 5. Report versions in health and warn on skew. `/api/v1/health` adds the
  server package version (via `importlib.metadata`, there is no
  `__version__` today) and the graph schema version. The CLI client and
  `axon-mcp` warn when the server reports a different version or graph schema
  than their own.
  Bundled skills already carry `axon-version`, so they stay consistent with
  the same value.
  Check: extend `tests/v1/test_api_version.py`, run
  `uv run --project axon-mcp --frozen pytest axon-mcp/tests`.
- [x] 6. Write the upgrade runbook and walk it once. Short section in
  `README.md`, detail in the Stores and cache compatibility parts of
  `docs/v1.md`. Contents: export the old graph with
  `axon index --repo . --out graph-old.json` (copy any existing snapshot aside
  first, `--out` overwrites); trial the new version with a throwaway
  `AXON_STATE_DIR`, writing only a new snapshot; check it with
  `axon verify --repo . --golden axon.golden.yaml --snapshot graph-new.json`,
  `axon ask --snapshot` and node and edge counts; trial against a second
  Neo4j instance only when store behavior changed (the compose file runs
  Community, one user database, fixed container names and ports, so override
  them); promote with one writer; roll back by restoring a dump or by
  re-indexing with the old binary. State the bake rule: keep the old binary,
  snapshot and dump until the new version has re-indexed and passed golden
  verify against the live graph.
  Check: follow the runbook end to end with the `v0.2.0` tag in one checkout
  and main in another, on a fixture repo. Fix the doc wherever a command
  differs from real behavior.

## Long term

After the items above, in this order.

- [x] Keep the previous generation and add rollback. `_publish_generation` in
  `axon/v1/store.py` stops deleting the generation it replaces and records it
  as `prev_generation` on `_AxonPublication`. A rollback command flips the
  marker back and prints both generation IDs. Pruning the kept generation is
  explicit, and publishing a third generation prunes the oldest. Note the
  cost in `docs/v1.md`: about twice the storage until prune.
  Check: extend `tests/v1/test_neo4j_publication.py` for publish, rollback,
  prune, and run one live rollback drill per the runbook.
- [x] Take a safety snapshot before an upgrade publish. When the stored
  `axon_build` or graph schema differs from the binary, the first publish
  writes the outgoing graph to a snapshot JSON under the state dir and logs
  the path, with an opt-out flag for very large graphs.
  Check: fake-store test where the fingerprint changes between saves.
- [x] Add snapshot import. Today a snapshot file restores CLI and `ask` use
  but cannot refill Neo4j. An import command loads the file through the item
  2 schema gate and saves it through the existing store, which already
  replaces per repo.
  Check: export, wipe, import, then equal counts and a passing golden verify.
- [x] Add a graph compare command for trial sign-off. Generalize the delta in
  `axon/v1/shadow.py` (today it diffs config edits on an isolated copy) to any
  two snapshots: node and edge counts by kind, coverage per service, outbound
  calls added and removed, unresolved diff. The runbook and the CI job below
  both use it. Endpoint loss between versions should need an explanation, not
  pass silently.
  Check: compare output on two fixture snapshots with one known endpoint
  removed.
- [x] Run cross-version checks in CI. One job checks out the previous tag,
  indexes a fixture repo with it, then loads the snapshot and state with
  current code and runs `ask`, golden verify and the compare command. A
  separate scheduled job runs the compat and publication tests against live
  Neo4j from `docker-compose.yml`. The default PR gate stays infrastructure
  free, per `CONTRIBUTING.md`.
  Check: both jobs green on the current tag pair, failure output uploaded as
  an artifact like the eval report already is.
- [x] Write the format change playbook into `CONTRIBUTING.md`. For each
  surface (snapshot JSON, Neo4j labels and props, `axon.yaml`, `state.db`,
  facts cache, `/api/v1` responses): what counts as additive, what needs a
  version bump, which fixture and pinned test move with it, and the changelog
  line, which always states whether a full rebuild is required. Field
  removals in `axon.yaml` get one release with a `doctor` warning first.
- [ ] Set the 1.0 rules before outside users depend on the formats. Publish
  the support window, the deprecation policy, and an upgrade notes section in
  `CHANGELOG.md` per release instead of one Unreleased block. `/api/v1`
  responses stay additive; removals wait for `/api/v2`. Snapshot stability
  already matters internally: `.github/workflows/axon-impact.yml` stores
  `axon-snapshot.json` as a build artifact on every push.

## Gate for every change

From `CONTRIBUTING.md`, on each item that touches code:

- `uv run pytest`
- `uv run python -m tests.evals.harness`
- `uv run ruff check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals`
- `uv run ruff format --check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals`
- `uv run mypy axon/v1 tests/evals`
- `uv run --project axon-mcp --frozen pytest axon-mcp/tests`
- web checks only if `web/` changed

Persistence items (do-now 2 and 4, rollback, import) also need the live Neo4j
validation from `CONTRIBUTING.md`, until the scheduled CI job exists. Report
live results separately from the infrastructure-free run. That split is the
main risk in this plan: the default tests use Neo4j doubles, so constraint
and Cypher drift only shows up live.

## Non-goals

- Concurrent old and new writers on one database.
- Runtime telemetry to learn which versions users run. `AGENTS.md` rules it
  out; versions get reported by preflight and health instead.
- In-place migration of graphs older than the support window. They re-index.
- Parser or linker accuracy changes. This plan only protects the formats
  their output lands in.

## Open questions

- Confirm the two defaults above: stale-but-readable serves reads and rebuilds
  at next index, and the support window is the previous minor release. A
  wider window costs one frozen fixture set and one CI producer per extra
  version.
- Confirm keeping one previous generation in Neo4j is acceptable at about
  twice the storage until prune, or make it opt-in per repo.
- Decide the tag habit: one tag per release at minimum. Without tags the
  cross-version CI job has nothing stable to produce artifacts with.

## Sources

- [Neo4j database administration](https://neo4j.com/docs/operations-manual/current/database-administration/):
  Community Edition allows exactly one standard database, which is why item 6
  trials against a second Neo4j instance instead of a second database.

Verification: 588 Python tests passed, 1 skipped (24 new in
`tests/v1/test_compat.py`, `tests/v1/test_upgrade.py` and
`tests/cli/test_upgrade_cli.py`); eval harness 35/35; MCP tests 10 passed;
Ruff check and format clean; mypy shows only the 5 unused-ignore errors that
also occur on the untouched tree. Trial walk: `upgrade-check` on the frozen
snapshot and fixture repo reports compatible, a fresh index compares equal to
the frozen snapshot (26 nodes, 38 edges, no endpoints removed), and new
snapshots write `schema_version: 1`. Live Neo4j validation was not run (no
Docker daemon on the dev machine); the scheduled `live-neo4j` workflow runs
it. Still open: the `v0.2.0` tag (no tag was created in this change), the
live rollback drill, and the 1.0 rules below. Cross-version CI covers schema
1 through the frozen fixtures until the first release tag exists.
