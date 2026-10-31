# Index accuracy implementation plan

Goal: Implement the supplied route extraction, linking accuracy, noise reduction and index lifecycle design generically.
Spec: User-provided route extraction, linking accuracy, noise reduction and index lifecycle design.
Constraints: No target repository or service names, runtime claims or config rewrites. Preserve verbs and evidence.

- [x] Lifecycle: AST parser/build/state fingerprints, same-commit and incremental guards, golden test.
- [x] Extraction: DRF mounting expressions; Java folding, arrays, partial diagnostics and generated transports.
- [x] Link safety: placeholders, specificity, strict literal binding, consensus, operation variants and source identity.
- [x] Noise: declared client-source discovery, return/model/factory evidence, SDK catalog, URL validation and counts.
- [x] Diagnostics: retry degraded files; only graph-unsound diagnostics force full indexing.
- [x] Verify synthetic regressions and eval contracts, infrastructure-free suite, lint/types; update docs and review.

Optional dirty-checkout incremental work is excluded; existing clean-checkout safeguards remain.


Review: fixed cross-instance attribute consensus, Java replacement postfix
truncation, and comment-safe expression folding with regression coverage.
Implementation uses existing source CALLS traversal for literal identity,
avoiding duplicate shortcut CALLS_API edges. Optional dirty-tree incremental
work remains excluded as specified above.
