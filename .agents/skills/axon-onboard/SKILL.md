---
name: axon-onboard
description: Onboard a repository (monorepo or small repo) into Axon's API graph by drafting and refining its axon.yaml service map, then verifying the indexed APIs. Also handles bring-your-own-config: translating an existing service/resource registry (any format, e.g. a code_registry file) the user points at into axon.yaml.
---

# Axon Onboard

Use this skill when a user asks to onboard, import, or index a repository
into Axon, to fix unresolved API/client links after indexing, or to
derive `axon.yaml` (also accepted as `axon.yml`) from a registry/config
Axon does not natively understand.

## Workflow

1. Run the deterministic scan. Never hand-write the first draft from
   scratch; derive it from the scan and/or the registry translation in
   the next section.
   - `axon init --repo <path>` prints a draft `axon.yaml`, review notes,
     and per-service `evidence` (which spec, bootstrap, or marker
     produced each drafted root). Services are anchored on validated
     `swagger`/`openapi` specs at any depth, app bootstraps, and
     framework signals; hidden trees such as `.worktrees/` are skipped
     and named in the notes.
2. Read every "review:" ambiguity. Resolve each one from code evidence:
   framework entrypoints, route files, spec files, generated-client
   banners, base-url config. Cite file:line for each decision you keep.
   If an LLM is configured for the user (LLM_PROVIDER/LLM_BASE_URL/
   LLM_MODEL), `axon init --ai` may pre-draft client `target_service`
   and `spec` values; treat those as unreviewed suggestions, verify
   each against the cited code, and delete any you cannot ground.
   Pay attention to "service root … scored nearly as high" notes: the
   scan picked one boundary, but the true root may be the runner-up.
3. Refine service ids and names to the team's real names (spec
   `info.title` is the draft default). Services may live anywhere in a
   monorepo, including nested inside other services; roots are paths or
   globs, longest match wins. Put libraries under `shared:`, junk
   (`vendor/`, generated output, local checkouts) under `ignore:` —
   `.worktrees/` is ignored by default, so only add what is repo-specific.
4. For each generated or disconnected client, declare it under the
   consuming service with `packages`/`paths`, `target_service`
   (and `target_repo` when cross-repo), and `spec` when known. Specs
   the scan reported as client/test copies or duplicates belong here,
   not as services. If consumers obtain the client from a settings
   factory (for example `api = settings.GET_PAYMENTS_API()`), add
   `factories: [settings.GET_PAYMENTS_API]` with the dotted name exactly
   as called. The scan drafts `factories:` and `packages:` for you
   when it can see `settings.GET_*_API()` or `get_V1Api(<pkg>, ...)`
   call sites; verify each drafted factory actually returns this
   client and delete wrong ones before committing. Receivers assigned from it then resolve to this client
   even if the generated package is pip-installed and not in the
   repo. Calls passed through retry/wrapper helpers
   (`call_payments_api(api.method)`) need no extra config once the client
   is declared: the indexer sees through wrappers that invoke their
   callable parameter.
   Add `links:` manual overrides only when no rule can resolve a call,
   and say why in your summary.
5. Write the file only with `axon init --repo <path> --write` when no
   `axon.yaml` exists, or edit the existing file directly. The file is
   reviewed and merged by humans via PR. Never commit it silently.
6. Verify: `axon index --repo <path>` then `axon apis --repo <path>`.
   Every remaining "unresolved" line needs either a fix in `axon.yaml`
   or an explicit note to the user. Spot-check with
   `axon ask "What services does <METHOD> <path> call?" --repo <path>`.
7. Report services, API counts, linked clients (with the rule that
   linked each), and anything still unresolved.

## Bring your own config (existing service/resource registry)

Some repos already declare their services somewhere Axon cannot parse:
a `code_registry`, a `resources.yaml`, a Starlark/JSON/textproto/CSV
inventory, a Terraform or Pants/Bazel-adjacent list, a wiki-exported
table checked into the repo, etc. When the user names or points at
one — e.g. "run axon init and use `code_registry` to get the list of
services" — treat that registry as the authoritative *candidate list*
and translate it. Do not ask the user to reformat it.

1. **Locate it.** Search the target repo by the name/path fragment the
   user gave (file or directory, with any extension: `.yaml`, `.yml`,
   `.json`, `.toml`, `.py`, `.bzl`, `.csv`, `.textproto`, …). If
   several candidates match, prefer the one other tooling actually
   references (grep for its name in scripts/CI) and say which you used.
   If none match, report the closest names you found and ask.
2. **Understand its schema from samples, not assumptions.** Read a few
   entries end to end. Identify which fields carry: service/resource
   name or id; code location (path, dir, package, or glob — check
   whether it points at the service root or a subpackage); language /
   framework / runtime; spec (`swagger`/`openapi`) location;
   client/SDK locations and what they target. Fields with no Axon
   equivalent (owners, tiers, SLOs) are metadata — ignore them for the
   draft, but list them in your report. Only ask the user when a field's
   meaning is genuinely ambiguous after looking at examples.
3. **Extract mechanically.** Write a small throwaway script (Python,
   `jq`, `yq`, …) that emits one row per registry entry with exactly
   the mappable fields. Do not transcribe entries by hand. Validate
   every extracted path exists in the repo; entries whose path is gone
   go in the report as stale, not in `axon.yaml`.
4. **Cross-check against the scan.** Still run
   `axon init --repo <path>` and diff three sets:
   registry-only (add to the draft; verify each has spec/bootstrap
   evidence — a registry entry with neither is a `review:` note),
   scan-only (likely unregistered services or libraries — decide from
   code evidence, default to flagging them, not silently dropping or
   adding), and boundary disagreements (registry path ≠ scan-inferred
   root — the registry usually wins for the service list, but the
   *root* must cover the code and spec Axon found; record each
   resolved conflict with its evidence).
5. **Generate `axon.yaml` from the merged result**: `id`/`name` from
   the registry's names (slugified), `roots` from its paths corrected
   per step 4, `language`/`framework` from whichever source knows,
   `specs` from the registry paths confirmed on disk (else the scan's),
   `clients` from registry SDK/client entries. Then continue at
   workflow step 5 above (human PR, `axon index`, `axon apis`).
6. **Report the translation**: registry file used, field mapping you
   applied, counts (registry entries → drafted services), entries not
   mapped and why (stale path, no code/spec evidence, non-service
   resource), and scan-only candidates.

The registry decides *which services exist*; it never decides call
edges. Ownership in `axon.yaml` still comes from `roots`, and links are
still resolved from code/specs at index time.

## Rules

- Pure code parsing only. Never use Bazel/build queries to decide
  ownership; `axon.yaml` roots are the source of truth. A user-supplied
  registry may name the services (above), but translating a declared
  list is not a build query — do not run build tooling to expand it.
- Prefer spec identity, operationId, then method+path matching before
  manual links. Manual links are the last resort.
- Keep unknown keys already in `axon.yaml`; teams extend the file.
- Never invent services, paths, or registry fields. Anything not
  backed by the registry or code evidence becomes a `review:` note.
