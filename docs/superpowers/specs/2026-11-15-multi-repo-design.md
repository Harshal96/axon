# Multi-repository API call resolution

Status: approved for implementation on 2026-11-16, drafted 2026-11-15. The user
confirmed one shared Axon instance for the first release and requested
implementation. Execution decisions and validation are tracked in the plan.

## Intent and scope

A service in repository A should recognize an API it calls in repository B.
Index either repository first; adding, updating or removing the target must
reconcile the unchanged caller. Outbound navigation, incoming callers, Ask and
PR impact should retain both repository identities and source evidence.

First release: repositories indexed into one Axon server/store, with an
independent `axon.yaml` and stable `repo:` ID per repository. Use existing
HTTP/OpenAPI/generated-client extraction and manual corrections. Local symbol
`CALLS` remain repository-local; integration occurs at `CALLS_API` boundaries.
Different commits can be indexed independently. The graph describes those
indexed revisions, not a claim that they are deployed together.

Out of scope: federation between Axon servers, cloning/authentication for Git
hosts, branch/environment variants of one repository ID, package-source symbol
linking, new gRPC/message-broker parsers, and runtime telemetry. Cross-repo
`GENERATED_FROM` navigation is separate from recognizing generated-client API
calls and is deferred; it must not become a dangling structural relationship.

## Current evidence

Inspected on `feat/ask-accuracy`; existing untracked `.worktrees/` is unrelated.

- `models.py` already qualifies service, file, symbol and endpoint IDs by repo.
  `servicemap.py` already has `ClientDecl.target_repo`, `ManualLink.to_repo`,
  `DeclaredCall.to.repo`, and client specs written as `repo::path`.
- `incremental.py:index_and_publish` loads the current store, but builds an
  isolated repo with `index_repo`; `_link_http` builds `EndpointIndex(snap)`.
  Repository graphs are combined only after resolution.
- A disposable two-repo test using `MemoryStore` and `index_and_publish`, both
  index orders, and force-reindexing the caller left explicit client/manual
  cross-repo targets external. Giving `index_repo` both graphs resolves them,
  but mutates the supplied snapshot and is unsafe for publication.
- `graph_delta.py:replace_repositories` infers replacement scope from every
  incoming node's `repo_id` and deletes edges incident to replaced nodes. This
  can erase incoming cross-repo edges. A missing declared-call placeholder
  currently carries its target repo ID, so it can even incorrectly select that
  target repository for replacement.
- Neo4j relationships bind physical generation IDs. Keeping a logical edge in
  memory does not make an old relationship point to a replacement endpoint.
  Node visibility filters alone also cannot hide staged links between two
  unchanged, already visible nodes.
- Scope must precede candidate counting. Two disposable resolver checks found
  cross-repo pollution in numeric operation-ID duplicate detection and in
  literal-to-parameter matching when repositories share a service ID.
- API navigation mostly preserves full IDs already. Ask aggregation drops repo
  identity in several places. `/impact` and CLI `pr-impact` build only a local
  graph even though `compute_impact` can traverse a combined graph.

Paths in this section are under `axon/v1/` unless otherwise qualified.

## Approaches

| Approach | Benefit | Cost / limitation |
| --- | --- | --- |
| Shared catalog plus retained caller evidence and publish-time relinking | Independent repo updates; links survive index order, restart and target churn | Durable link inputs and generation-safe link publication |
| Reindex every checkout as one workspace batch | Simple complete context | Requires every checkout on every update; couples lifecycles and reparses callers |
| Federate independent Axon instances | Separate hosting boundaries | Remote catalogs, credentials, trust, availability and synchronization |

Recommend the shared catalog. Retain current repo-qualified public node IDs and
the API-first interface. No global repository graph page is needed.

## Identity, configuration and matching

Repository IDs must be explicit, stable and unique within a store. Detect two
different checkouts claiming an existing ID and return a conflict; allow an
explicit replacement of its recorded source path, never silently rename it.
Do not infer repository identity from the remote Git URL or service name.

Caller configuration, using existing fields:

```yaml
# checkout: orders-backend/axon.yaml
version: 1
repo: orders-backend
services:
  - id: orders
    roots: [src/orders]
    clients:
      - id: ledger-sdk
        packages: [ledger_sdk]
        target_repo: finance-backend
        target_service: ledger
        base_urls: [env:LEDGER_URL]
```

```yaml
# checkout: finance-backend/axon.yaml
version: 1
repo: finance-backend
services:
  - id: ledger
    roots: [src/ledger]
    base_urls: [https://ledger.internal.example]
```

The SDK's operation name/source-visible transport path resolves inside
`finance-backend::ledger`. Direct HTTP can resolve through the caller's
declared client base mapping or a matching target service literal base URL.
No endpoint is invented from the declaration: the target must be indexed.

Matching policy for the first release:

1. Scoped manual rules retain precedence. Exact `to_repo`/`to.repo` and client
   `target_repo` select `(repo, service)` before path, operation normalization,
   duplicate counting, parameter matching or ambiguity decisions.
2. An omitted target repo continues to mean the caller repo. A missing explicit
   target never falls through to another service or repository.
3. Export service literal base URLs into the catalog. Normalize scheme, host,
   effective port and base path; preserve path case and segment boundaries.
   Match authorities exactly and use the longest matching base path. Equally
   specific competing service declarations remain ambiguous. Do not equate
   different schemes or evaluate environment variables.
4. `env:`, `setting:` and receiver/attribute bases are caller-scoped. The same
   variable spelling in another repo never establishes ownership. Caller client
   declarations can explicitly map those bases to remote services.
5. Preserve weak path/operation fallback within the caller repo only for
   relative, symbolic and URL-less inputs. An unbound absolute HTTP(S) URL with
   a concrete authority stays external before local path, literal-parameter or
   operation fallback too. Foreign path/operation matches without a target or
   host binding are diagnostic candidates, not resolved edges. A stable concrete
   authority may accompany a templated call path/query; dynamic authorities and
   templated service/client base declarations cannot establish literal ownership.
   Existing same-repo absolute calls previously inferred only from coincident
   path/operation require service `base_urls`, an applicable caller client map or
   a manual correction. Adding/removing repositories must never convert an
   unbound host into an unrelated caller-local endpoint.
6. Preserve existing method, catch-all, unresolved-path, gateway-prefix and
   confidence restrictions. Numeric generated-operation deduplication and
   literal-parameter matching operate inside the selected scope.

Use full endpoint IDs in candidate lists. Add `Suppression.to_repo`: omitted
means local, explicit selects the remote repo. Existing suppression behavior
for local-only graphs stays unchanged. Expose `--to-repo` for manual link/call/
suppression CLI commands and target repo in the web correction form.

## Durable linking inputs and catalog

Create an immutable catalog from the prospective complete published graph,
replacing the indexed repo's old endpoints before matching. It includes endpoint
identity, method/path, operation IDs, spec aliases, specificity and literal host
ownership. Catalog construction must not mutate shared nodes.

Persist caller-owned link inputs independently of resolved edges and disposable
`state.db`/facts caches. Keep both resolved and unresolved sites, including
catalog-dependent accessor candidates that are currently suppressed. Capture
the prepared `HttpFact`, caller symbol ID, source file/line, source owner kind,
argument count and static eligibility information before endpoint matching.
Also keep the effective service-map declarations, manual correction provenance,
source generation/commit, and per-repo link reports. No source checkout should
be required to relink an unchanged caller.

Use versioned typed inputs in snapshot JSON and generation-owned internal Neo4j
rows; use existing facts codecs for `HttpFact`. Config/editor source paths are
provenance, not permission to read another checkout during relinking. Keep
matching rules and binding traces separately; one must not replace the other.

Reconciliation builds all `CALLS_API` edges, including same-repo, manual and SDK
edges, plus placeholders, candidate lists, coverage and link diagnostics.
Initially replay all retained sites whenever a repo graph/config changes.
This avoids incomplete dependency invalidation. Measure sites replayed, link
time and rows staged; optimize invalidation only after correctness is pinned.

Unresolved reasons distinguish target repo absent, target service absent,
endpoint/operation absent, ambiguous target, and unsupported dynamic evidence.
Keep expected target identity and evidence on the edge. Placeholders have no
repository storage ownership; target IDs belong in `target_repo` and
`target_service` properties. Shared placeholder pruning is reference-based.

## Atomic publication

Separate structural repo generations from one globally versioned outbound-link
projection. Structural generations contain repo-owned nodes and non-`CALLS_API`
edges; the projection owns all `CALLS_API` relationships and `ExternalApi` nodes.

Under the store's single publication lock:

1. Reload the latest snapshot after parsing; merge the prepared repo structure
   and inputs using an explicit replacement-repo set, never remote candidates.
2. Build the prospective catalog and replay retained callers against it. Keep
   previous reader snapshots immutable. Validate graph endpoints, reports and
   indexed repo golden expectations before publishing.
3. Stage changed structural generations and the complete new link projection
   in bounded transactions. Resolve relationship endpoints through a candidate
   logical-to-physical map: staged IDs for replaced nodes, current IDs elsewhere.
4. In one final Neo4j transaction, flip the repo publication marker(s) and
   `_AxonLinkPublication` marker. Publish the memory snapshot pointer only after
   commit. A failure or cancellation before this point leaves the old graph live.
5. Clean obsolete generations in bounded transactions, retaining the previous
   structural/input generations and link projection for recovery.

Hydration and model-generated Cypher must filter relationship link generations
as well as node generations. Name anonymous bounded relationship patterns where
needed; preserve `OPTIONAL MATCH` and existing predicates; unsupported query
forms continue to fail closed. Hide every internal input/publication label from
schema cards, generic matches and public graph hydration.

MemoryStore follows the same reconcile-and-swap semantics. A parsing job may
run concurrently with another repo's parsing, but reconcile/compare/publish is
serialized globally, with fresh store state. Python locks support one server
process/writer per database; multi-process writers are outside this release.
Same-commit skips are allowed only after required link reconciliation; target
changes must refresh callers even when callers' source generations stay fixed.

Golden checks gate the repository currently being indexed, using a caller-owned
validation callback over the reconciled prospective graph before publication.
The store does not read source/configuration itself. Do not load or claim to
revalidate unchanged callers' golden files during a target-only update. Add an
optional `repo` golden target selector: omitted means the caller repo for
service/path selectors; URL-only external selectors retain URL matching unless
repo is explicit. Explicit repo matches endpoint ownership or a placeholder's
expected `target_repo`. A same-named C/ledger can never satisfy an expectation
for B/ledger. Update golden export and config cascade matching consistently.
An expected remote target absent from the prospective graph fails the indexed
caller's golden and blocks its publication, as existing golden guards do.
Index-order independence applies when no golden demands the not-yet-indexed
target; explicit expected-target guards are intentionally stronger.

Rollback loads the retained repo structure **and matching inputs**, computes a
fresh projection against all other currently published repos, and switches
both markers atomically. Do not restore an old global link projection over
newer unrelated repos. Safety snapshots, imports and graph comparison include
link inputs/reports. Structural-only small delta writes can remain only where
the complete structure/link update fits one transaction; otherwise stage.

## Legacy and removal behavior

Old snapshots remain readable with default-empty link inputs. Bootstrap their
existing `CALLS_API` rows as opaque pinned legacy inputs: preserve full edge
evidence and target logical identity, without reconstructing guessed HTTP facts.
If a pinned target remains present, retain its previous relationship with
`relink_status: pinned-legacy`; if it disappears, produce a placeholder. Emit
`link-inputs-missing` and a reindex-required blind spot for those callers.
Do not claim fresh matching, ambiguity checks or complete coverage for them.
Reindexing each caller replaces its pinned inputs with complete prepared sites.

Use explicit repo removal through `DELETE /api/v1/repos/{repo_id}` and
`axon repo remove --repo-id ID`: remove only that indexed graph/input state,
reconcile other callers and publish atomically. It never deletes a checkout or
edits other repositories. Removing a service/route through normal reindexing
has the same caller demotion behavior. Empty/zero-endpoint repos still replace
their old graph because replacement scope is explicit.
Keep a publication tombstone for a removed repo until its legacy/staged rows
are safely pruned; deleting its marker must not reactivate old unversioned rows.

All new public fields have defaults; preserve node IDs and existing response
fields. The durable relationship-generation visibility contract requires graph
schema 2, because older readers do not filter link generations. Continue reading
schema 1 artifacts, add a frozen schema 2 fixture and update version gates and
fingerprints. Follow `CONTRIBUTING.md` for separate state DDL/codec bumps when
those formats change. Older binaries must use an old snapshot/database restore;
they must not read or write the migrated store concurrently with the new binary.

The first persistent implementation stages the changed source generations and
complete outbound projection. In-place structural-delta optimization is deferred
until it can preserve atomic link visibility and retained rollback inputs.
Measure and document that structural write cost without promising a latency bound.

## Consumers

- API detail/ego graphs use the published combined snapshot for remote outbound
  endpoints and incoming callers. Destination navigation uses full IDs and
  shows repo-qualified labels when necessary.
- Ask binds `repo::service`, preserves ambiguity for duplicate unqualified
  names, and groups/cites by `(repo, service)`. Web Ask aggregation must not
  collapse same-named services or discard their cross-repo edges.
- Add a read-only `/impact/graph` request taking `repo_id` and `changed_files`.
  It uses the published combined graph; it never indexes or edits a checkout.
  CLI `pr-impact --server --repo-id ID` collects changed paths locally and uses
  this route. Existing local impact remains explicit. Report the indexed commit
  and retain the limitation that deleted symbols/endpoints require a baseline
  graph; this release does not implement a full base/head PR graph comparison.
- CLI `ask`/`apis` gain an explicit server mode; combined `--snapshot` remains
  supported. MCP existing graph/Ask tools need contract coverage and docs.
  Return a publication ID in relevant responses; do not let MCP cache entries
  survive an observed publication change. Independent clients may retain their
  documented cache TTL until their next server request.

## Acceptance and risks

Use three small checkouts: A/orders calls B/ledger; C also has a service named
ledger, overlapping operation IDs, routes and source paths. Hand-author expected
targets and evidence. Test both index orders, restart without caller checkout or
cache, missing/arriving/changed/deleted targets, changed host ownership, duplicate
operations, scoped suppression, idempotence, concurrent publishers, staging
failure, rollback/import, qualified Ask/navigation and cross-repo impact.

Primary risks are full outbound replay/write cost, Cypher visibility leaks,
legacy inputs with incomplete evidence, and overclaiming independent revisions
as one deployment. Measure the first; fail closed for the second; expose the
last two. No scale or live Neo4j claims are established by this planning work.


## Implementation policy clarification (2026-11-17)

The reviewed authority/identity correction (`b2dac5d`) applies the conservative
absolute-URL rule to local and foreign targets alike. Source extraction contracts
are unchanged; six existing parser fixtures gained reviewed service base URL
declarations while their source, hand-authored eval cases and matrix stayed fixed.
Qualified Ask service lookup prefers exact `repo::service` spelling, then falls
back case-insensitively only when no exact match exists. Bare duplicates and
nonexact case-folded collisions remain ambiguous. Source ownership golden
selectors require actual API endpoints; missing-target placeholders diagnose
intent and cannot satisfy remote ownership expectations.
