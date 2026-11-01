# Changelog

## Unreleased

- Split static graph/core, CLI with bundled agent skills, and server into
  separate uv workspace distributions; slim CLI/core imports do not require
  FastAPI or Neo4j. Package publishing remains a separate release step.
- Add local dry-run configuration maintenance, source tracking, generated-file
  tombstones, opt-in scoped team overlays and repo-wide client id validation.
- Add evidence-backed client proposals, scoped manual graph corrections,
  diagnosis/reference views, isolated impact previews and service-fresh indexing.
- Ship onboarding/maintenance skills with versioned, guarded installation and
  expose read-only MCP diagnosis tools/prompts. Static-analysis gaps remain
  explicit; configuration writes stay in the local CLI.

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
