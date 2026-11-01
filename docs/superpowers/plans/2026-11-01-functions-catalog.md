# Functions catalog implementation plan

**Goal:** Implement the supplied Functions → APIs requirements using generic examples.
**Architecture:** A lazy snapshot usage index feeds a pure faceted query module and additive HTTP routes. The web catalog and inspector consume this contract, with filters persisted in the URL. Existing traversal semantics remain unchanged.
**Tech stack:** Python/FastAPI, React/TypeScript, existing CSS primitives.

## Constraints and decisions
Usage is static CALLS reach from API handlers at depth ≤ 12. Handler hiding defaults on; all symbol kinds are available by default. Used-in filtering counts only API closures. Direct callers remain separately inspectable. Owner roots come exclusively from the service map. No new dependencies, telemetry or feedback-specific service/repository names.

## Tasks
- [x] Backend: regression tests for usage parity, mutation invalidation, scoped counts/facets, sorting/paging and shortest-path evidence; implement graph usage, pure queries and compatible routes.
- [x] Ownership/MCP: test matching roots and owner_root persistence, preserve owner_of compatibility; test optional MCP filters and update schema.
- [x] Web: test URL/filter/helper behavior, implement clients and evidence-derived demo queries, accessible catalog/filter primitives and inspector, update App routing.
- [x] Integration: document routes/definitions and reindex fallback; run Python, MCP and web checks, inspect UI in light/dark and review full diff.

## Review focus
Depth boundaries and cycles; duplicate handler/API paths; colliding service IDs across repositories; stale asynchronous responses after browser navigation; older snapshots lacking ownership roots. Tests belong with the backend or web task that owns each behavior.


## Verification and review
417 backend/CLI/eval tests passed, one infrastructure-dependent skip; 34/34
parser eval cases passed. Ruff check/format and mypy passed. MCP tests passed
with their SQLite cache redirected to a temporary directory (the sandbox does
not permit writes to the normal home cache). Web unit tests, typecheck, main
and demo builds passed. Browser checks covered dark/light layout, keyboard
facets and row selection, evidence scope, grouped definitions, debounced search
and history restoration. Review corrections included scoped minimum depth,
repository-qualified caller highlighting, and dismissible loading inspectors.

Synthetic local measurement: Python 3.11, 25,000 nodes, 24,000 edges, 5,000
endpoints, 20,000 symbols, 50,000 reach entries and service-depth entries.
Cold usage index build: 0.1685 seconds; general closure cache remained empty.
This bounded-chain synthetic workload is not a production latency guarantee;
measure the deployment graph before setting its response budget.
