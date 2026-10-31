# Generic graph correctness and indexing progress

Implemented: structured subscript parsing/eval; ownership-scoped module, binding
and constant resolution; conservative operation matching and ambiguity evidence;
DRF inherited action locations and serializer selection/hooks; golden verification;
configuration overlays/schema; doctor; dirty/config skip invalidation; coverage
buckets; committed generic registry-generator/runbook and updated onboarding skill.

Indexing now includes Git inventory, bounded client-discovery caching, atomic
snapshot publication, complete-evidence graph deltas in both stores, rollback-safe
Neo4j writes, measured persistence, SQLite state, separate raw/synthesized facts,
conservative per-service incremental resolution, full outbound re-linking, queued
request coalescing, repository identity locking, publication revision guards,
automatic golden checks, read-only shadow audit, and post-merge/nightly examples.
Differential tests cover scripted changes and an update of every eval fixture.

## Explicit remaining limits

- Neo4j full replacement is one managed transaction, not versioned shadow
  generations; its memory use needs live database validation.
- Shared/unowned/client source and metadata changes conservatively use full
  resolution. Fine-grained resolver-read dependency scheduling, process-pool
  resolution and background rebuild cost prediction are not implemented.
- No production rebuild or latency budget is claimed. The examples do not
  install a hook, schedule a job, pull a checkout or merge changes automatically.
- Source-specific cases and adapters are deliberately outside this generic change.

Review found and regression-tested generated-suffix generic matches, scoped
ambiguity losing candidates, unknown dirty state permitting skips, clean HEAD
movement during indexing, duplicate checkout identities racing publication, and
orphan external nodes surviving the final caller. The current implementation
retains conservative fallbacks instead of assuming those dependencies are safe.
