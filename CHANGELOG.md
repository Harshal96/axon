# Changelog

## Unreleased

- Expand overloaded symbol spans to cover every declaration and record their count.
- Deduplicate Go Handle and HandleFunc route facts for the same registration.
- Exempt annotation-only route-to-handler gaps from verify-sample review flags.

- Recover DRF router URL-list mounts and Java annotation string/constant paths.
- Add conservative path specificity, literal parameter binding, scoped base
  consensus and generated operation/source identity rules.
- Recognize Java generated HTTP transports, locate declared client sources,
  suppress model/transport noise and report SDK dependencies separately.
- Replace manual-only cache keys with AST parser/build/state fingerprints.
  Existing graphs require one automatic full rebuild after upgrading; stale
  same-commit graphs and resolved facts are not reused.
- Keep informational parse warnings compatible with incremental indexing;
  retry degraded files and reserve full fallback for unsound graph synthesis.
