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

Commit hand-curated names, base URLs, factories, ignores, and manual links in
`axon.overrides.yaml`. Initialize it with `{}`. Generated `axon.yaml` loads
that file through `overrides`. Store reviewed API expectations and forbidden
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
