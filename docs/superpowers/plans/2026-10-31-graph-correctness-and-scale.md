# Graph correctness and scale implementation plan

**Goal:** Improve static API coverage, retain evidence-based call relationships, and bound API graph rendering for large repositories.

**Architecture:** Keep pure per-file parsing separate from graph resolution. Extend cached facts with imports, package, inheritance and factory return evidence; use that evidence to resolve calls conservatively. Preserve API navigation and explicit service ownership from `axon.yaml`.

**Constraints:** All fixtures, examples and documentation use generic names. Preserve existing work. Do not infer service ownership from a build graph. Keep unresolved outbound calls visible. Avoid promises about coverage or performance on repositories unavailable for verification.

## Tasks

- [x] Python extraction: reproduce annotated router, Django action/view wrapping, list mounting, import, factory and complex receiver cases; implement parsing changes and hand-authored eval contracts.
- [x] Java extraction: reproduce multiline Spring declarations, mapping attributes, JAX-RS routes, package/import/field/inheritance and call-chain cases; implement parsing and framework evals.
- [x] Resolution: reproduce dotted receiver collisions, out-of-scope bare calls, shared imports, inherited client calls and route reconciliation; replace unrestricted fallback with evidence-scoped lookup; avoid repeated call-through work; invalidate stale caches.
- [x] Traversal: reproduce multiple handlers, deep outbound paths, helper fan-out and caller fan-in; include all handlers and admit outbound paths whole within explicit graph budgets; return truncation counts.
- [x] Web: reproduce large layout bounds and catalog grouping; wrap graph columns, cap animation, memoize static layers, coalesce pan/zoom and paginate catalog tables.
- [x] Integration: run v2, CLI, parser evals, Ruff, mypy and web typecheck/tests/build; review generic-only changes and document verified limitations.

## Review focus

- Unknown receivers must not bind to unrelated domain methods sharing a leaf name.
- Imported shared libraries must remain reachable across service boundaries.
- New cached fields must tolerate old fact dictionaries and invalidate old parser results.
- Graph budgets must preserve connectivity and distinguish truncation from missing evidence.
- Large fan-in/fan-out must remain bounded without preventing keyboard or reduced-motion use.

## Validation limits

The generic regression and framework contract suites verify the supported
source shapes; they do not establish complete coverage of arbitrary dynamic
applications. No private deployment benchmark or production re-index was run.
Nested/dynamic HTTP wrapper composition, runtime route discovery, reflection,
and Flask Blueprint mount prefixes remain static-analysis limitations.
