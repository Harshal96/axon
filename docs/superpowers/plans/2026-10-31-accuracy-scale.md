# Graph accuracy and large-repository implementation plan

The supplied technical plan is the implementation specification. All examples,
fixtures, diagnostics and benchmark inputs use generic service names.

Work is divided by ownership to permit independent implementation and review:

- [x] Symbolic URL fallback, agreement and mapping suggestions: linker and evals.
- [x] Java/Go declaration ranges and ownership: parsers and parser evals.
- [x] Normalized incremental state, warning recovery, durable jobs and warmup.
- [x] Delta publication, bounded Neo4j generations and actionable memory errors.
- [x] Onboarding boundaries, curated refresh and shared-root diagnostics.
- [x] Accurate inherited framework handlers and presentation.
- [x] Seeded source-evidence sampling, reviewed golden export and scale benchmark.
- [x] Integrate documentation, review changes and run infrastructure-free gates.

Each implementation adds focused regression tests before production changes.
Integration checks include pytest, parser eval harness, Ruff, mypy and web
checks. Neo4j checks require an available development database; lack of one must
be reported explicitly. Benchmark results describe measured runs only.

Implementation decisions: preserve public logical node IDs; keep unsafe or
ambiguous linking visible; never execute target repository code; never silently
overwrite curated configuration. Independent agents share the checkout with
explicit file ownership; no commits or publishing are requested.

Review corrections cover stale verdict identity, duplicate call-site evidence,
glob-owned refresh roots, service/shared precedence, malformed state columns,
durable history retention, event-loop registry locking, query scoping and shared
external placeholder publication. Live Neo4j and low-cap acceptance remain
unverified because no test database or Docker daemon is available. A 3,000-file
measurement is recorded in docs/benchmarks; the 30,000-file workflow has not run
in this session.
