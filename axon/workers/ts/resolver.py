"""
Resolves TypeScript/JavaScript import module specifiers to either:
  - A BazelTarget label (for internal workspace packages and local files)
  - A PnpmPackage node id (for external npm packages)
  - None (for Node.js built-ins like 'fs', 'path', 'os')

Resolution cascade (stops at first hit):

1. Built-in / virtual: starts with 'node:' or is a known Node.js module.
   → Return None (no node created)

2. Relative import: starts with './' or '../'
   → Resolve against the importing file's directory.
   → Look for a SourceFile node in Neo4j with the resolved path.
   → If not found (file not yet indexed), create a pending reference.

3. Internal workspace package: matches a PnpmPackage.name where is_internal=True.
   → Return the PnpmPackage node AND the associated BazelTarget
     (via PnpmPackage.bazel_label).

4. Known external package: matches a PnpmPackage.name where is_internal=False.
   → Return the PnpmPackage node.

5. Scoped package path: '@company/ui-components/Button' — strip the subpath
   and resolve '@company/ui-components'.

6. Unresolved: log at DEBUG level, return (None, None).
"""

from __future__ import annotations

import logging
import os

from axon.db.connection import async_session

logger = logging.getLogger(__name__)

# Node.js built-in module names (does not need to be exhaustive; just common ones)
_NODE_BUILTINS = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

# Extensions to try when resolving relative imports without explicit extension
_RESOLUTION_EXTENSIONS = [
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    "/index.ts",
    "/index.tsx",
    "/index.js",
]


def _is_builtin(specifier: str) -> bool:
    base = specifier.removeprefix("node:")
    return base in _NODE_BUILTINS


def _strip_subpath(specifier: str) -> str:
    """
    '@company/ui-components/Button' → '@company/ui-components'
    'lodash/merge'                  → 'lodash'
    """
    if specifier.startswith("@"):
        # Scoped: keep the first two parts
        parts = specifier.split("/")
        return "/".join(parts[:2])
    return specifier.split("/")[0]


async def resolve_import(
    specifier: str,
    importing_file_path: str,
    workspace_root: str,
) -> tuple[str | None, str | None, str]:
    """
    Returns (target_id, pnpm_node_id, confidence).

    target_id: Neo4j id of a BazelTarget or SourceFile (for relative imports)
    pnpm_node_id: Neo4j id of a PnpmPackage (for external/workspace packages)
    confidence: 'EXTRACTED' | 'INFERRED' | 'AMBIGUOUS' | 'SKIP'
    """

    # ── 1. Built-in ───────────────────────────────────────────────────────────
    if _is_builtin(specifier):
        return None, None, "SKIP"

    # ── 2. Relative import ────────────────────────────────────────────────────
    if specifier.startswith("."):
        return await _resolve_relative(specifier, importing_file_path, workspace_root)

    # ── 3 & 4. Package import (workspace or external) ─────────────────────────
    return await _resolve_package(specifier)


async def _resolve_relative(
    specifier: str,
    importing_file_path: str,
    workspace_root: str,
) -> tuple[str | None, str | None, str]:
    """Resolve './Button' or '../utils/format' to a SourceFile node id."""
    base_dir = os.path.dirname(os.path.join(workspace_root, importing_file_path))
    raw = os.path.normpath(os.path.join(base_dir, specifier))
    rel = os.path.relpath(raw, workspace_root)

    # Try exact path first, then with extension candidates
    candidates = [rel] + [rel + ext for ext in _RESOLUTION_EXTENSIONS]
    # Also try path without extension (for imports like './Button' → 'Button.tsx')
    if "." in os.path.basename(rel):
        candidates = [rel]
    else:
        candidates = [rel + ext for ext in _RESOLUTION_EXTENSIONS]

    async with async_session() as session:
        for candidate in candidates:
            result = await session.run(
                "MATCH (sf:SourceFile {path: $path}) RETURN sf.id AS id LIMIT 1",
                path=candidate,
            )
            record = await result.single()
            if record:
                return f"file::{candidate}", None, "EXTRACTED"

    # File not yet in graph (not yet indexed — will be resolved on next full index)
    logger.debug("Relative import unresolved: %s from %s", specifier, importing_file_path)
    return None, None, "AMBIGUOUS"


async def _resolve_package(
    specifier: str,
) -> tuple[str | None, str | None, str]:
    """Resolve a package name to a PnpmPackage node and optionally a BazelTarget."""
    package_name = _strip_subpath(specifier)

    async with async_session() as session:
        result = await session.run(
            """
            MATCH (p:PnpmPackage {name: $name})
            RETURN p.id AS pkg_id, p.is_internal AS is_internal,
                   p.bazel_label AS bazel_label
            LIMIT 1
            """,
            name=package_name,
        )
        record = await result.single()

    if not record:
        return None, None, "AMBIGUOUS"

    pkg_id = record["pkg_id"]
    is_internal = record.get("is_internal", False)
    bazel_label = record.get("bazel_label")

    if is_internal and bazel_label:
        return f"target::{bazel_label}", pkg_id, "EXTRACTED"

    return None, pkg_id, "EXTRACTED"
