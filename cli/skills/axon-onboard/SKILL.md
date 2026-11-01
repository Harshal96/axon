---
name: axon-onboard
description: Use when onboarding, importing, or indexing a repository into Axon, including translating an existing service or resource registry into a reproducible service map.
---

# Axon Onboard

Build an evidence-backed service map, then verify its static graph. Use local
`axon` commands for configuration changes. Never hand-edit Axon YAML, overlays,
or golden files. Do not commit or push unless explicitly requested.

## Workflow

1. Read `axon init --help` and run `axon init --repo <path> --local` to print
   the deterministic draft. Inspect every review note. Services come from
   validated OpenAPI/Swagger specs, framework bootstraps and code signals;
   ownership always comes from declared roots, never a build dependency graph.
2. Review root boundaries and client/spec copies against code. Cite file:line
   for each retained decision. A client/test spec copy belongs to its SDK,
   not a new service. Unknown paths, fields, targets and factories remain
   review notes. If an LLM drafts suggestions, verify them against source.
3. When no service map exists, initialize with
   `axon init --repo <path> --local --write`. For an existing map, use
   `service add|patch|rename|remove` and `client add|patch|move|rename|remove`.
   Never overwrite an existing curated map with a new draft or a text editor.
   For example, preview a corrected root with
   `axon service patch payments --repo <path> --set 'roots=["services/payments"]'`.
4. Resolve SDKs with `axon client add <name> --repo <path> --dry-run --impact`.
   Review the proposal's packages, generated paths, factories, spec, consumers
   and target evidence. Use `--service`/`--target` only when source evidence or
   a user decision supports them. For an existing SDK, use
   `client patch <id> --add-package <package>` or `--set key=JSON`.
   Factories such as `settings.GET_PAYMENTS_API` must actually return that SDK.
5. Read the owning sources and overlays shown by `service show`, `client show`
   and `refs`. Mutation commands default to dry-run; inspect every diff before
   `--apply`. Exit 2 is invalid input. On exit 3 show candidates and ask. On exit
   4 show guarded file:line references and ask before any override. Never pick
   an ambiguity option to meet a deadline. If the installed CLI cannot express
   an edit, report the capability gap for human review.
6. Run `axon doctor --repo <path>`, index with `axon index --repo <path>`
   (or `--local --out <snapshot>`), and list with `axon apis --repo <path>`.
   Explain missing edges with `axon api explain "METHOD /path" --service <id>`.
   Prefer spec/operation identity and SDK declarations before scoped links.
   Use `axon-maintain` for declared calls, suppression and removal cascades.
7. Run `axon verify --repo <path> --golden <reviewed-golden-file>`; add
   `--snapshot` for local output. Propose new golden cases from reviewed source
   evidence, including forbidden targets; never invent or directly write them.
   If no reviewed file exists, report golden verification as pending.
8. Report services, API counts, client resolution rules, changed files, checks,
   review decisions, and every unresolved call with its static-analysis blind
   spot. The graph provides no runtime traffic, health or latency measurements.

## Bring your own registry

When the user names a registry (`code_registry`, resources YAML, JSON, Starlark,
CSV, textproto or another checked-in inventory), inspect its schema and the code
that consumes it. Treat entries as candidates; never execute registry imports,
BUILD rules or build queries to infer ownership.

1. Locate the actual registry. Establish which fields mean id, root, spec,
   library and SDK. Verify every path on disk; report stale paths and metadata
   with no Axon equivalent. Ask when examples leave a meaning ambiguous.
2. Adapt `examples/registry-onboarding/gen_axon_yaml.py` into a deterministic
   generator under `tools/axon/`. The generator may produce the initial
   generated source, never rewrite curated overlays. Record registry revision,
   hash/date, exact command, schema adapter, precedence and exclusions in a
   runbook. Ask before committing the generator or runbook.
3. Cross-check registry-only, scan-only and root-boundary disagreements.
   Resolve each with file:line evidence. The registry supplies a service
   candidate list; it never proves call edges. Libraries belong to `shared`.
4. Generated sources must be marked with `# axon:generated` or listed in
   `generated:` and load a non-generated overrides file. Preserve reviewed
   differences through CLI patches/tombstones. Regenerate twice and compare
   bytes; then run doctor, index and reviewed golden verification.

The canonical bundled skill is `cli/skills/axon-onboard/SKILL.md`. Install it
with `axon skills install --project --agent codex --repo <path>` (or another
supported agent). Use `axon skills update` to refresh; review local changes
before authorizing `--force`.
