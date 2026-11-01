# Reproducible registry onboarding

Copy `gen_axon_yaml.py` into the target repository as
`tools/axon/gen_axon_yaml.py`, and commit its schema adapter and this runbook.
This example recognizes literal `Service`, `Library`, and `Client` declarations;
it is a pattern to adapt after reading the actual registry, not an adapter for
every registry format. Registry code and BUILD files must never be executed.

Record the registry path, commit, constructor types, id and path fields, and
the precedence of competing path fields. Verify paths on disk; do not infer
ownership from BUILD dependencies. Record exclusions explicitly. Translate
libraries to `shared`, not services. Inspect client metadata and specifications
to establish each client target; flag ambiguous ownership instead of guessing.

Keep reviewed names, base URLs, factories, ignores, and manual links in
`axon.overrides.yaml`. After creating the initial generated map, use local
`axon service|client|link|suppress|call|endpoint|handler` commands for changes;
never hand-edit Axon YAML. If a needed field has no CLI command, report the gap
for human review. Generated `axon.yaml` loads that file through `overrides`.
Mark the generated source with `# axon:generated` in its first five lines or
list its path in the root map's `generated:` field (adapt the generator, not
the generated output). The CLI then writes editable overlay patches and
`{id: service-id, +delete: true}` tombstones instead of changing the source.
Tombstones survive regeneration and remove inherited keyed entries; patching
an inherited service changes only the specified fields. Store reviewed API expectations and forbidden
targets in `axon.golden.yaml`, including source file and line evidence.

Pin every input for a byte-for-byte regeneration:

```sh
python tools/axon/gen_axon_yaml.py --root . --registry registry.py \
  --revision <registry-commit> --date <pinned-date> > axon.yaml
axon init --repo . --local > /tmp/axon-scan.yaml
axon doctor --repo .
axon index --repo . --local --out /tmp/axon-snapshot.json --timings
axon verify --repo . --snapshot /tmp/axon-snapshot.json --golden axon.golden.yaml
```

Compare registry-only, scan-only, and root-boundary disagreements. Record each
decision with source evidence in this runbook. Run generation twice and compare
bytes. Inspect the source snippets printed by verification, and review remaining
unresolved-rule buckets. Do not claim runtime coverage, traffic, or performance
from a static graph.

Reusable agent prompt: “Read the committed generator and runbook. Inspect the
registry schema and code evidence before changing field mappings. Regenerate
deterministically, preserve manual overrides, cross-check the static scan, run
doctor and golden verification, and report unsupported expressions and unresolved
calls. Never guess roots, targets, base URLs, factories, or source evidence.”


For an existing map, preview changes with `--dry-run --impact`, inspect every
reference cascade and per-file diff, then use `--apply`. Exit 3 requires a user
choice (for example ignored roots versus unowned code); exit 4 reports a guard
that must be explicitly resolved. After apply, run `axon index --service <id>`,
doctor and reviewed golden verification. Config edits and indexing are separate.
Per-team files are loaded only when root `overrides` explicitly enables a glob
such as `services/**/axon.service.yaml`; glob-loaded files cannot modify another
service or expand their own roots.
