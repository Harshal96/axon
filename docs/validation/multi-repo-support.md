# Multi-repository qualification

Local qualification on 2026-11-17 for `feat/multi-repo-support`, with final
reviewed source `0f1c001`. Earlier browser and scale observations below identify
their tested core; the final post-fix gates are recorded separately. This does not
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

Task7 scoped review found that the existing <=600px rule hid `.ask-service` and
made same-method/path endpoint rows visually identical. The narrow responsive
fix keeps complete ownership on wrapping second lines. Root rechecked the same
retained C::ledger→B::ledger answer at **390px** with the existing sidebar
collapsed: both full labels fit the viewport (right edges 310.5/324.7px), with zero
document horizontal overflow; the screenshot was visually inspected. Labels also
remain visible at 1200px. Reduced motion kept one Ask arrowhead and zero running
animations. Tab/ShiftTab gave the finance target a visible 2px accent focus outline;
Enter selected its exact repo URL and full endpoint ID.

This responsive follow-up deliberately kept the backend stopped and DB empty.
Enter's detail/graph GETs therefore failed through the proxy with 500 as expected:
it qualifies labels, focus and route selection only. Earlier live checks above
qualify data loading and the zero-error console. No Ask/Model/provider request ran
in this follow-up. Covering web typecheck, all 44 tests and the main build passed;
Python/MCP/parser/full gates were not repeated for the CSS-only fix.

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

The whole-branch review of `c3e6342..37a7471` requested I1–I4 and M1 fixes.
Combined fix `0f1c001` passed scoped re-review: all five findings are addressed,
with no new Critical, Important or Minor issues and no open or parked findings.
The reviewer inspected metadata-only input adoption, atomic pointer publication,
cold hydration and retained rollback input ownership. Production and regression
sources stayed frozen through the final local gates below; only records changed
afterward. Earlier deterministic browser and synthetic scale observations remain
scoped to the recorded reviewed core above and were not repeated for this patch.
Upstream scheduled CI, live model providers and production/30,000-file
qualification remain unrun.

The feature branch/worktree are kept for integration; no merge, push, pull
request or deployment was performed.

## Final review fixes and qualification

The fix preserves explicit client ownership through path consensus; records
legacy input origin/completeness independently of pin count; evicts and fences
MCP entries on conflicting overlapping publication observations; puts qualified
expected ownership on declared-call edges; and retains structural diagnostics
with each source/input pair. Correction preview, diagnosis and replay share the
same target ownership helper. Clean reindex/removal drops only the affected
repository's source warnings, including when other repositories share paths.

Defaulted fields retain schema 2/private input format 1 compatibility. The
reviewed indexer fingerprint is
`534d502153c5ebddf920f7313b9d7b2870a4de9cea225106307b97cc8a2744e6`;
the state fingerprint is
`95193739eb44c148cc2e11f8d4504c09c6e10322894d52d2f6d8a7ad106bf305`.
Parser fingerprints, fixture source/contracts and the coverage matrix are unchanged.

Local final gates on 2026-11-17, using private temporary Axon state/facts paths:

- `.venv/bin/python -m pytest tests/v1 tests/cli tests/evals -o addopts='' -q --tb=short`:
  **923 passed, 11 skipped in 65.59s**. This was one full infrastructure-free
  run after the final source freeze; live skips are qualified separately below.
- Focused covering Python/CLI gate: **405 passed in 43.41s**. Final regression
  and fingerprint gate: **19 passed in 0.82s**. Initial core RED had 11 failures
  and one passing complete-zero-site control; MCP RED had 5 failures/15 passes.
- `uv run --project axon-mcp --frozen pytest axon-mcp/tests -o addopts='' -q --tb=short`:
  **23 passed in 0.46s**, including both directions of conflicting late responses,
  a third overlapping publication and header-only 404 observations. Cache hits
  retain TTL semantics without polling health.
- Required root Ruff check plus changed MCP Python files: **passed**; format
  check: **138 files already formatted**. Mypy: **60 source files passed**.
  An exploratory all-MCP Ruff scope reported eight pre-existing import-order,
  mode and line-length violations in unchanged files; it is not a passing gate.
- `python -m tests.evals.harness`: **35/35 cases, 90/90 endpoints, 43/43 links**.

The root agent separately reran the three dedicated Neo4j files against an
initially empty local Neo4j 5.20 database: **11 passed in 8.80s**, exit 0 and zero
remaining nodes. The added test starts from zero-pin schema-1 physical rows,
bootstraps through a target-only publication, cold-loads/replays without caller
preparation, then prepares and cold-rolls-back the caller; incomplete origin and
reindex reports survive. Reproduce only against the disposable dedicated database:

```bash
# Set AXON_TEST_NEO4J_URI=bolt://localhost:17687 and private synthetic
# AXON_TEST_NEO4J_USER / AXON_TEST_NEO4J_PASSWORD first. Tests refuse other URIs
# and nonempty databases. Use temporary AXON_STATE_DIR/AXON_FACTS_CACHE_DIR.
rtk proxy .venv/bin/python -m pytest tests/v1/test_neo4j_link_readers.py \
  tests/v1/test_neo4j_publication.py tests/v1/test_neo4j_multirepo.py \
  -o addopts='' --tb=short -q
```

Source-warning ownership additionally has JSON/private-codec and real producer
regressions: metadata-only adoption stages a new input pointer while preserving
the active source generation and retained previous source/input rollback pair.
The live test does not independently assert source-warning persistence. Older
aggregate warnings with no recoverable owner remain conservatively retained.

Root rebuilt all four workspace wheels/sdists with
`rtk proxy env UV_CACHE_DIR=/private/tmp/axon-task7-uv-cache uv build --all-packages --out-dir /private/tmp/axon-multirepo-final-wheels.GDlUTo/wheels`
and installed the four explicit new wheel paths outside the checkout with
`PYTHONPATH` unset and `uv run --no-project --with <each-wheel>`.
**12 installed modules matched current source bytes; six CLI/help/schema/default
and fingerprint checks passed; no editable-source imports**. Build and helper
exited 0. Installation emitted benign no-project and nonfatal metadata
normalization (`>= '2.7'` to `>= 2.7`) notices and downloaded four dependencies;
no dependency files changed. No web source changed or web suite was rerun.
