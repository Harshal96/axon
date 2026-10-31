# Call-resolution validation

Validation date: 2026-10-31. Changes remain uncommitted.

## Evaluation of the proposed plan

The proposed order was useful: establish failures, improve bounded binding flow,
remove non-operation noise, then add URL ownership, dispatch, and framework flow.
The implementation keeps the following precision constraints:

- Graph properties remain strings. External endpoint candidates are encoded as
  JSON in `props.candidates`, rather than changing the storage/API property type.
- A client's identity includes its owning service when synthesizing calls. Reused
  client IDs cannot bypass package or target-service scope.
- Literal `urljoin` follows Python URL replacement semantics; it is not string
  concatenation. Symbolic URLs require transport evidence, so cache/dictionary
  `.get(self.key)` does not become an outbound HTTP call.
- Framework hooks depend on the action and source-visible delegation to a generic
  framework body. Plain APIView handlers do not acquire queryset/object/serializer
  calls just because methods with those names exist.
- Imported conditional constants retain alternatives. Ambiguous candidates are
  not resolved by choosing the last visited assignment.
- Signals require a source-resolved Django model sender, and receivers are indexed
  once rather than scanned across every file for each save/delete call.
- Suppression diagnostics are sampled per file while retaining a full count.

Eight new hand-authored eval contracts cover constructor injection, dictionary
arguments, factory/thread dispatch, symbolic HTTP URLs, base URL mappings, DRF
hooks, Django signals, and Java typed receivers. Focused regression tests also
cover cycle/depth safety, dataclass/property flow, inherited attributes, kwargs
spreads, imported constants and alternatives, context managers, URL join semantics,
client identity, permission scope, method fields, and ambiguous dispatch.

## Generic snapshot audit

The baseline uses the original HEAD implementation in an isolated temporary
archive and the same eight new fixture repositories as the final implementation.
No application-specific identifiers were added to source, fixtures, or examples.

| CALLS_API bucket | Before | After |
| --- | ---: | ---: |
| Explicit client resolutions | 2 | 4 |
| Explicit environment-base resolutions | 0 | 7 |
| Unresolved constructor calls | 3 | 0 |
| Unresolved requests without a URL template | 4 | 0 |
| Unscoped method/path matches | 5 | 2 |
| Other unresolved requests | 1 | 2 |

The two remaining unresolved requests intentionally retain an unknown environment
base and a third-party host. Fewer unscoped matches reflect stronger base URL
ownership evidence. Edge counts alone do not establish precision; positive and
negative contracts pin the intended targets.

Run an audit on any saved snapshot:

```sh
uv run python -m tests.evals.tools.resolution_audit snapshot.json --out audit.json
```

Raw local comparison artifacts are `/tmp/axon-resolution-audit.json` and the
before/after snapshot directories beside it. These temporary artifacts are not
part of the source change.

## Synthetic timing check

Five fresh, cache-disabled indexes of a generic repository containing 5,000
calls to one declared generated-client operation; median phase timings:

| Phase | Original HEAD | Changed code | Change |
| --- | ---: | ---: | ---: |
| call_through_s | 28.58 ms | 29.94 ms | +4.8% |
| link_http_s | 49.02 ms | 51.78 ms | +5.6% |
| total_s | 152.31 ms | 158.38 ms | +4.0% |

Both measured phases stay below the proposed 20% limit on this synthetic workload.
This is not a production-repository benchmark; tiny fixture timings are dominated
by overhead and are not an appropriate percentage-regression gate.

## Checks

- Infrastructure-free Python suite, including parser evals.
- Standalone eval harness: 24 cases, 57 endpoints, 24 outbound links.
- Ruff lint and formatting over active Python code/tests.
- Mypy over `axon/v2` and `tests/evals`, with a fresh non-incremental check.
- Frontend type checking, 16 tests, and production build.
- Diff whitespace validation.

## Limits of this validation

The separate investigation repository and its temporary reproduction were not
available on this machine. Its live graph, forced server re-index, a 20-edge
production precision review, and production phase timings were therefore not
verified. No running server or other repository was modified.

The implementation remains bounded static analysis: runtime-generated factories,
serializer classes/fields, arbitrary monkey-patching, dynamic router construction,
and middleware are not a complete runtime call graph. Unindexed API-class/version
collisions without source or spec evidence remain unresolved rather than guessed.
