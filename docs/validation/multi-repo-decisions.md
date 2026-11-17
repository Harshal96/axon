# Multi-repository implementation decisions

These are the execution rulings made while implementing the approved shared-instance design. The ledger order and repeated entries are preserved. The concrete-authority refinement supersedes the narrower preceding rule. Each entry states the consequence if the decision is wrong or its tradeoff matters. Qualification evidence is recorded in [multi-repo-support.md](multi-repo-support.md).

1. Ruling: User's implement request approves the proposed design for execution — explicit shared-instance scope and plan already supplied — no further approval question.

   Cost / tradeoff: The implementation would need rework if the shared-instance scope was misunderstood.

2. Ruling: Managed worktree creation and local branch are reversible implementation setup — authorized by implement request — original checkout stays intact.

   Cost / tradeoff: Only the isolated feature branch needs to be removed or recreated; the original checkout remains intact.

3. Ruling: Task 5 introduces repo-qualified golden selector matching as needed by its validator; Task 6 owns export and cascade support — prevents an intermediate validation gap — costs minor task boundary overlap.

   Cost / tradeoff: Incorrect task coordination could temporarily leave exported golden cases inconsistent with validation.

4. Ruling: Do not change documented parser behavior for this feature; retain and replay extracted facts — limits scope — parser omissions remain explicit unknowns.

   Cost / tradeoff: Unsupported extraction remains unknown and may need a later parser feature.

5. Ruling: Task 1 may update test_store.py synthetic snapshots with real Repo markers and the indexer fingerprint fixture — the explicit ownership contract intentionally changes their inputs — original test assertions/foreign-node safety remain required.

   Cost / tradeoff: Fixture changes could hide an ownership regression; the original assertions and foreign-node checks remain required.

6. Ruling: split Task4 into4a memory coordinator and4b complete Neo4j protocol, each implemented/reviewed separately — limits risky mixed changes and gives stable interface — Task4 stays incomplete until4b; no feature scope dropped.

   Cost / tradeoff: Additional integration work is needed if the memory and persistent implementations diverge.

7. Ruling:4b is split into4b1 generation-safe readers/bounded metadata codec, then4b2 complete writer/adapters/rollback/tombstones — gives a concrete tested read contract before changing writes — all parent requirements remain;4b is not complete until both and live tests pass.

   Cost / tradeoff: The writer may need rework if the reader contract is wrong; completion still requires both and live tests.

8. Ruling: graph impact accepts optional expected_commit and rejects known mismatches409, missing repo404 — a server cannot prove checkout freshness from changed paths alone — clients omitting it receive indexed-graph analysis with explicit limits, while CLI can compare its chosen revision.

   Cost / tradeoff: Callers omitting expected_commit can analyze a stale indexed revision; the response reports that limit.

9. Ruling: retain historical pinned legacy edges/counts and add pinned_legacy/fresh_matching_unknown plus reindex blind spots — preserves graph-row count compatibility without claiming fresh matching/complete coverage — consumers must read the qualification; reindex replaces pins with prepared inputs.

   Cost / tradeoff: Historical pins can remain incomplete until each caller is reindexed.

10. Ruling: extend Task6 to sanitize query execution error boundary in store/Ask and add focused regressions/live readonly check — confirmed private value leak violates binding invariant — safe category/code feedback may be less detailed, but raw database values/generated scoped query cannot leave public/model boundaries. Original Task4 query-result sanitization remains; no parallel fixer.

   Cost / tradeoff: Safe error messages provide less diagnostic detail than raw database errors.

11. Ruling: deterministic Ask stamps its exact input snapshot publication; model-used responses leave publication_id empty with explicit unknown — drafting can await through a publication, and list query adapter does not capture exact query version — loses model-response generation observation but prevents falsely stamping new query rows with old prompt snapshot, no guessed later load.

   Cost / tradeoff: Model responses cannot drive publication-aware cache invalidation until execution metadata is available.

12. Ruling: narrowly change hand-authored Ask eval expectation to unbound-target, retain fixture/rows/counts/no-handler assertions — substring of host/name is not ownership evidence — loses old heuristic phrasing intentionally; no parser eval/matrix change, source-faithful unknown replaces guessed service scope.

   Cost / tradeoff: The older heuristic phrasing is intentionally lost; users must supply actual ownership evidence.

13. Ruling: Model engine may draft queries only after explicit service ownership ambiguity/missing scope is handled — engine choice does not authorize guessing repo identity — ambiguous users must qualify the service before a model query, retaining free model drafting for unambiguous/nonscoped questions.

   Cost / tradeoff: Ambiguous or missing service scopes require a qualified question before model drafting.

14. Ruling: impact limitations must conditionally expose missing legacy caller inputs/reindex need too — static impact can use historical pins or omit unseen callers, so indexed/baseline/runtime limits alone do not qualify matching completeness — adds a legacy qualification without changing existing counts or inventing participation; no new impact field required.

   Cost / tradeoff: Clients ignoring impact limitations could overstate completeness for legacy callers.

15. Ruling: interject a reviewed Task7a core integration fix before resuming Task7 docs/commit — actual target-removal false self-call and previously recorded exact-case alias issue violate qualification — no Task7 scope dropped; keep its WIP frozen and no parallel implementation. Preserve weak local fallback where no literal binding contradicts it; explicit concrete authority declarations must not be discarded by method/path fallback. Costs stricter precision when a literal address conflicts with local declarations; no parser/schema change planned.

   Cost / tradeoff: Stricter binding can leave previously inferred absolute calls external; migration declarations may be needed.

16. Ruling refinement: any concrete absolute HTTP authority must be retained, not just ones contradicted by a local declaration — otherwise a different undeclared local endpoint can still absorb a removed target — unbound absolute URLs stay external until a declared host/client/manual scope exists. Relative/symbolic/URL-less weak local fallback remains. This tightens legacy absolute-host/path inference and requires explicit base mapping for those old calls; document migration, inspect fixture intent before any contract change. No parser/schema/ID change.

   Cost / tradeoff: Existing same-repo absolute URLs need service bases, an applicable client map or manual bindings.

17. Ruling: add exact literal base_urls to those6 fixture service declarations (go_app payments/http://payments; go-router billing/http://billing; java_spring_annotations catalog/http://catalog; java-spring-webclients ledger/http://ledger; monorepo payment/http://payment; python-fastapi-router catalog/http://catalog), preserving source and all hand-authored eval expectations/matrix/frozen compat artifacts — intended internal host ownership now explicit — no parser semantics changed. Own narrow wrapper unit serviceOther base https://example.invalid for its intended literal wrapper test.

   Cost / tradeoff: A wrong declaration could mask intended fixture ambiguity; source and hand-authored eval contracts were preserved and reviewed.

18. Ruling: accept stable concrete authority with templated call path/query for literal host scoping, but reject templated authority and keep base declaration prefixes literal — path parameters do not erase a known host — fix shared URL helper without treating dynamic base declarations as literal proof.

   Cost / tradeoff: An overly broad URL helper could assign false ownership; dynamic authorities and declarations remain ineligible.

19. Ruling: new durable relationship-generation semantics use graph schema2 in4b2, retain schema1 reading and frozen fixtures — old normal hydration must fail instead of interpreting new links without generation guards — older binaries require upgrade or old snapshot/database restore.

   Cost / tradeoff: Old binaries require upgrade or restoration of an older snapshot/database; mixed old/new binaries are unsupported.

20. Ruling:4b2 initially stages complete changed source generations plus global outbound projection, deferring unsafe in-place optimization — establishes atomic history/rollback first — structural write cost must be measured/documented, no latency promise.

   Cost / tradeoff: Every publication pays full replay and projection-staging costs; the measured synthetic workload is not a production latency promise.

21. Ruling: graph_delta.replace_repositories stays a structural helper; store coordinator will explicitly invoke pure reconcile — avoids recursive/hidden linking — Task4 must wire all publish paths.

   Cost / tradeoff: A caller bypassing the store coordinator would fail to reconcile; all publish paths must use it.

22. Ruling: evidence.rule is matching rule, factory provenance retained in props.binding_rule; update legacy assertion to check both — spec requires separate traces — downstream formatting must preserve binding evidence.

   Cost / tradeoff: Consumers reading only one trace could omit binding provenance; both fields must be preserved.

23. Ruling: Task2 adds endpoint_catalog to indexer fingerprint dependencies now — moved matching must invalidate caches on future changes — Task5 need not repeat it.

   Cost / tradeoff: The cache fingerprint changes, requiring cache rebuilding after this upgrade.

24. Ruling: Task1 may update synthetic test_store snapshots to actual Repo markers and the indexer fingerprint golden — mandated ownership semantics changed — preserves original fixture intent.

   Cost / tradeoff: The same fixture adjustment could conceal a test regression; the original ownership and assertion intent was retained.

25. Ruling: conservatively evict and fence cached rows on a conflicting overlapping publication without ordering UUIDs or adopting the rejected identity — a returned publication must not leave incompatible rows usable — may cause an extra HTTP request instead of a TTL hit.

   Cost / tradeoff: An extra HTTP request may replace a cache hit; UUID ordering is never assumed.

26. Ruling: retain possible-owner contributions for legacy aggregate warnings that lack recoverable repository ownership, until each possible source is replaced or removed — exact old ownership cannot be reconstructed honestly — an unchanged legacy source can show an unrelated historical warning; reindex removes the uncertainty and new prepared sources do not inherit it.

   Cost / tradeoff: An unchanged legacy source can show an unrelated historical warning until it is reindexed or removed.

27. Ruling: keep feat/multi-repo-support and its managed worktree intact after implementation — the user authorized implementation but did not request merge, push or PR — integration remains a separate user action; no unsolicited approval menu is needed.

   Cost / tradeoff: The user still needs a separate integration action; no merge, push or pull request is performed.
