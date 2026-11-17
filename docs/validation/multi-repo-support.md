# Multi-repository qualification

Local qualification on 2026-11-17 for `feat/multi-repo-support`, based on
`b2dac5d` plus Task 7. This records observed evidence and its limits; it does not
qualify production deployments, a 30,000-file workload or live LLM providers.

## Infrastructure-free gates

Python 3.14.4 and Node 24.14.1 match the inspected CI runtime family. Commands ran
with `rtk`; a private temporary `UV_CACHE_DIR`, `AXON_STATE_DIR` and
`AXON_FACTS_CACHE_DIR` isolated writable tool/state files.

- `uv run pytest tests/v1 tests/cli tests/evals -o addopts='' --tb=short -q`:
  **909 passed, 10 skipped in 59.42s** (combined Task7 WIP + reviewed
  Task7a integration gate, independently reported by that worker). The skips are opt-in dedicated-live tests,
  not passing database qualifications.
- `uv run python -m tests.evals.harness`: **35/35 cases, 90/90 endpoints,
  43/43 links**. Parser source/contracts and coverage matrix unchanged; six reviewed service
  base declarations preserve intended internal fixture bindings. The Task7a
  final covering suite passed 181 tests in 2.03s.
- AGENTS/CI Ruff check and format check: **passed, 134 files formatted**.
  Mypy: **passed, 60 source files**. One new-test import-order issue was fixed
  before the final passing Ruff run.
- Frozen MCP suite: **18 passed**. Covers scoped cache invalidation, first ID
  after legacy mode, expired entries, same/empty IDs, server separation, old
  in-flight responses, preserved full IDs/evidence, missing-target diagnostics,
  old-server TTL, array-shaped degraded fallback and publication headers on a
  removed-target 404. A stale in-flight 200 or 404 cannot overwrite an observation.
- Web typecheck and **44 tests passed**; main build, main-bundle demo-data
  exclusion and separate demo build **passed**. Repo-aware same-name aggregation,
  qualified citation routing before catalog load and correction transport are
  covered by pure/HTTP-client tests. External ID fallback does not invent repo
  ownership; Ask cards reuse the existing graph layout dimensions to fit both
  lines and align arrows. A follow-up external-owner RED had 2 failures/42 passes.

The first intended web RED had 3 failures (32 passing): qualified service
citations lacked repo identity, aggregation collapsed two ledger services, and
the citation route helper was absent. Isolated MCP cache RED had 2 failures
(4 passing) for missing publication observation; proxy RED had 2 failures for
retained stale data and old overlapping responses refilling cache. Header RED
had 2 failures (1 passing): health lacked publication and helpers loaded different
snapshots. The model-empty-header test pins an existing conservative boundary.
A follow-up RED exposed 404 generation observations retaining stale lists
(1 failed, 2 overlap cases passed); all 18 MCP tests pass after the fix.
Initial default cache/state attempts hit sandbox-protected user directories;
final tests used isolated paths and did not write user state.

The initial Task7-only full gate passed 890 tests/10 live skips in 59.95s. It is
superseded by the combined 909-test result above; no production changes followed
that full run. Task7a fixes an actual runtime finding: removing a declared target
must not let its concrete-host call resolve to a caller-local same-path endpoint.
Same-repo absolute URLs also need declared service/client/manual binding now;
relative/symbolic/URL-less fallback remains local. Qualified service aliases prefer
exact spelling before case-insensitive convenience matching.

## Browser and installed-artifact checks

The root agent used an isolated browser at Vite 15174/API 18080 with a dedicated
Neo4j 5.20 container on Bolt 17687 and three synthetic repository checkouts.
Actual Ask UI checks explicitly selected Deterministic.

- Indexed A/orders then B/ledger through the Services Index card. A's literal
  outbound remained external before B and resolved after B. Clicking A's target
  card opened B's exact repository/API URL; B displayed qualified incoming callers.
- Added C/ledger with the same method/path as B. Qualified C::ledger→B::ledger Ask
  displayed one row with source/target IDs, evidence, interpretation and a real
  two-node/one-edge graph. The corrected 56px cards were visually inspected: both
  repository subtitles fit and the arrow stays centered. Enter on a focused remote
  citation navigated to B. With reduced-motion=true, six SVG arrowheads remained
  and zero animations ran; the API graph showed Flow off.
- Added-call preview retained target repository B as `to.repo` in the YAML diff;
  suppression preview retained `to_repo` B. No Apply was used and C's configuration
  remained unchanged. Detail/list/health body and header publication IDs agreed.
- Changing only B's route made both A and C external with target-endpoint-missing.
  The first removal test exposed C's false local self-call; reviewed Task7a fixed
  it. Fresh corrected removal now demotes both A and C to external, preserves
  body/header publication agreement and leaves A's index generation unchanged.

- Hid both A/C caller checkouts, removed their incremental state and all facts
  cache, then restarted the backend cold. Both calls stayed external. Indexing
  **B only** replayed two retained sites and restored both exact B targets, with
  A's source index generation unchanged. Caller checkouts were restored afterward;
  configurations remained unchanged.
- Repeated Enter navigation with reduced motion after restart: exact B URL,
  two incoming callers, six API graph arrowheads and zero animations. Qualified
  Deterministic Ask after restart retained both repo labels, one call, one
  arrowhead and zero animations. Final browser console: **zero errors**.

The root stopped its backend, guard-cleared only its disposable DB, and ran all
10 dedicated tests from `test_neo4j_link_readers.py`, `test_neo4j_publication.py`
and `test_neo4j_multirepo.py`: **10 passed in 7.60s**, zero nodes remaining after
cleanup. This separately qualifies local Neo4j 5.20 visibility, lifecycle, both
index orders, retained replay and legacy/rollback cases; it is not upstream CI.

Final artifact gate rebuilt all four workspace wheels/sdists after the authority
and MCP404 fixes, then installed the four wheels outside the checkout with
`PYTHONPATH` unset and `uv run --no-project --with <each-wheel>`. **12 module
imports** (including API, health, CLI and MCP), **six CLI/help/schema/default
checks**, installed-source byte equality for every checked module and the reviewed
indexer fingerprint all passed. No editable-source import was present. The only
notice was uv's expected `--no-project`/no project found message in the isolated
directory. This supersedes the earlier 10-module smoke.

No live model provider qualification is claimed. The optional Model browser
request was rejected by automatic approval review for possible external graph-context
egress; no request ran and no workaround was attempted. Fake-model tests cover
Model publication and ownership behavior.

## Synthetic publication scale

Root reran both measurements on reviewed core `b2dac5d` plus frozen Task7 WIP:
serial static extraction and target-only route changes using a
literal-host ring of four repositories. Each repo declares a service called
`ledger`; 25 Python files per repo have 60 HTTP sites each. After changing the
first endpoint in one target repo from `/items` to `/items-v2`, its predecessor's
1,500 calls become placeholders. No caller is reparsed for that publication.
Measurements used tracing, so timings include its overhead.

| Measurement | Memory store | Neo4j 5.20 |
|---|---:|---:|
| Repositories / Python files | 4 / 100 | 4 / 100 |
| Retained outbound sites / CALLS_API rows | 6,000 / 6,000 | 6,000 / 6,000 |
| Logical nodes / edges after update | 309 / 6,404 | 309 / 6,404 |
| Resolved / placeholder call rows | 4,500 / 1,500 | 4,500 / 1,500 |
| Target-only parse (outside publication) | 0.9543 s | 1.0006 s |
| Target-update publication wall time | 2.5666 s | 5.4964 s |
| Reconcile | included in wall | 2.6666 s |
| Staging | n/a | 2.2081 s |
| Marker commit / cleanup | n/a | 0.00264 s / 0.16354 s |
| Store publication total | zero stats by memory contract | 5.4541 s |
| Python peak RSS | 133,414,912 bytes | 221,593,600 bytes |
| Traced Python peak | 46,354,871 bytes | 94,372,834 bytes |

Persistent staging: **6,343 rows in 16 batches**: 77 source nodes, 101 structural
edges, 1 external node, 6,000 CALLS_API edges, 117 input chunks and 47 report
chunks. This used production-default batching on a dedicated local Neo4j 5.20
community container. Python RSS excludes container memory. The final root runner
cleaned its initially empty fixture database in `finally`, after browser
qualification and the dedicated live suite. Task7's implementer never touched
the browser database.

Reproduce the generator/measurement approach with the committed tool (no new
runtime dependency):

```bash
uv run python -m tools.benchmark_multirepo --repos 4 --files 25 --calls 60 \
  --output /tmp/multirepo-memory.json
# Optional: only an empty dedicated disposable development DB on localhost:17687.
# Set AXON_TEST_NEO4J_URI=bolt://localhost:17687 and synthetic
# AXON_TEST_NEO4J_USER / AXON_TEST_NEO4J_PASSWORD in your shell first.
uv run python -m tools.benchmark_multirepo --store neo4j \
  --repos 4 --files 25 --calls 60 --output /tmp/multirepo-neo4j.json
```

The opt-in Neo4j mode refuses any other URI and any nonempty database; it removes
its own generated graph in `finally`. Do not point it at a shared database. A
Task 7 memory smoke of the committed tool passed 2 repos / 2 files / 4 sites,
11 nodes / 14 edges, 2 resolved + 2 placeholder links. The table above records
root's final full same-approach measurements; the small tool smoke is separate.

Prioritize full replay and projection serialization/staging before optimizing
marker commit: those dominate this measured target update. These results support
neither an unmeasured latency promise nor a production/capacity claim. Source
parsing can be incremental while every retained outbound site is still replayed
and every global projected link staged.

## CI boundary

The scheduled live workflow adds a separate initially empty disposable Neo4j job
for `test_neo4j_link_readers.py`, `test_neo4j_publication.py` and
`test_neo4j_multirepo.py`, using guarded Bolt 17687 and synthetic development-only
credentials. It shares no data with the older fixture-indexed job. This workflow
edit has **not run upstream**. Local live evidence is reported separately.


## Review boundary

Required local infrastructure-free, deterministic browser, installed-artifact,
dedicated Neo4j and synthetic scale gates are recorded above. Task7 scoped review
and final whole-branch review still follow; this report does not claim either has
already approved the completed branch. Upstream scheduled CI, live model providers
and production/30,000-file qualification remain unrun.
