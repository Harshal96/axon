---
name: axon-maintain
description: Use when maintaining an existing Axon API graph, removing or renaming services or SDK clients, reindexing services, diagnosing missing calls, or correcting a wrong API edge.
---

# Axon Maintain

Make local, reviewable changes through `axon` commands. Never hand-edit Axon
YAML, including overlays and golden files. A server or MCP connection is read-only
for configuration: run the CLI in the repository working tree.

## Required workflow

1. Inspect `axon --help` and the relevant subcommand's `--help` for installed
   capabilities. Use `--repo <path> --json` for structured output. Read the code
   evidence and cite file:line for every configuration decision.
2. Diagnose before editing: `axon service show <id>`, `axon client show <id>`,
   `axon refs service|client|endpoint <id>`, or
   `axon api explain "METHOD /path" --service <id>`.
3. Preview the smallest correction. Mutations default to dry-run; pass
   `--dry-run --impact` to request a local shadow index and graph delta. Inspect
   every file diff, reference cascade, warning, and unresolved target.
4. Exit 2 means invalid input. Exit 3 means ambiguous: show candidates and ask
   the user to choose. Exit 4 means a guard blocked the edit: show file:line
   references and ask for the specific override. Never choose an exit 3/4 option
   just to finish faster; urgency and previous work do not authorize it.
5. Apply the reviewed operation with `--apply`. Regenerate the preview after
   any intervening config change. The CLI selects the owning overlay or source,
   uses tombstones for generated inputs, and preserves comments.
6. Reindex with `axon index --repo <path> --service <id>` (repeat `--service`
   for multiple callers), or add `--local --out <snapshot>` for a local result.
   This guarantees fresh parsing of selected files; linking and publication
   remain repository-wide, and shared/generated code can require full resolution.
7. Run `axon doctor --repo <path>` and
   `axon verify --repo <path> --golden <reviewed-golden-file>`; use `--snapshot`
   when checking local output. If no reviewed golden file exists, report that
   verification is pending and propose evidence-backed cases for review. Never
   fabricate expectations or hand-write a golden file to make verification pass.
8. Report changed files, graph delta, verification results, remaining unknowns,
   and the static-analysis blind spot behind each unknown. Do not commit or push
   unless the user explicitly asked.

## Intent to command

| Intent | Commands and decision |
|---|---|
| Remove service X | `service show X`, `refs service X`, then `service remove X --dry-run --impact`. If roots exist, ask between `--ignore-code` and `--leave-unowned`. Ask between `--move-clients-to <svc>` and `--drop-clients` when clients have other consumers. `--cascade-clients` deletes incoming SDK declarations instead of clearing their target. |
| Rename service or client | `service rename <old> <new>` or `client rename <old> <new>`; inspect all rewritten references before apply. |
| Missed client C | `client add C --dry-run --impact`; inspect per-field evidence. Supply `--service <consumer>` or `--target <svc>` only from evidence/user choice. A package conflict calls for `client patch <existing> --add-package <package>`, not a duplicate client. |
| Removed client C | `client remove C --dry-run`; on exit 4 show remaining imports/calls and ask before `--force`. Review `--cascade-links` and `--cascade-golden` effects before using either. |
| Move client C | `client move C --to <service>`; review inherited/generated declarations and references. |
| Missing calls A to B | Explain A first. Prefer client declarations, then evidenced `service patch --set 'base_urls=["..."]'`, then a scoped link. Use a declared call only with user-confirmed source evidence. |
| Wrong target | `link add <id> --service <caller> --call-name <call> --to-service <target> --to-method POST --to-path /path`; use `--from-client` to narrow further. |
| False edge | `suppress add <id> --service <caller> --from-api "POST /path" --to-service <target> --reason <reason>`; propose a reviewed golden forbid. |
| Parser missed endpoint/handler | `endpoint add <id> --service <svc> --method GET --path /path --handler <qualified>` or `handler set <id> --service <svc> --api "GET /path" --handler <qualified>`. |
| Reindex service | `index --service <id>`; summarize focused coverage, delta and unresolved calls without promising speed. |

For a declared call, use `call add <id> --service <caller> --from-api "POST /a"`
with `--to-service <target> --to-method POST --to-path /b --evidence file:line`.
Read that exact line and obtain user confirmation; the CLI captures its text.
Use `link|suppress|call|endpoint remove <id> --service <svc>` to remove
an obsolete correction. Generic service/client patches take `--set key=JSON`.

## Limits

The graph proves static relationships, not runtime activity. Dynamic imports,
reflection, unresolved factories and incomplete specs remain unknown. Inspect
actual command output; do not claim that every planned resolver, cascade or
preview feature exists in an older CLI. If a command cannot express the required
edit or a golden expectation needs authoring, report the gap for human review
instead of bypassing the command with a YAML editor.
