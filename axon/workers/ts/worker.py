"""
TSWorker: indexes TypeScript and JavaScript source files.

Run order:
  1. Query Neo4j for all JS/TS BazelTargets (created by Phase 2 BazelWorker)
  2. Locate source files on disk for each target
  3. Parse each file with tree-sitter (parser.py)
  4. Write SourceFile nodes for any new files
  5. Write Symbol nodes for all extracted symbols
  6. Write DEFINES edges (SourceFile → Symbol)
  7. Resolve imports → write IMPORTS edges (SourceFile → SourceFile or BazelTarget)
  8. Write RE_EXPORTS edges for barrel files (export { X } from './y')
  9. Write CALLS edges for JSX tag usages (Symbol → Symbol)
  10. Write cross-language edges: TS files that import swagger-generated clients
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from axon.db import writers
from axon.db.connection import sync_session
from axon.models.enums import EdgeConfidence, Language, SymbolKind
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.ts.parser import TSSymbol, parse_ts_file
from axon.workers.ts.resolver import resolve_import

logger = logging.getLogger(__name__)

_JS_TARGET_KINDS = ("JS_LIBRARY", "JS_BINARY")

_FETCH_JS_TARGETS = """
MATCH (t:BazelTarget)
WHERE t.kind IN $kinds
RETURN t.label AS label, t.package AS package,
       t.genrule_outs AS genrule_outs, t.is_genrule AS is_genrule
"""

# Genrule targets that produce .js/.ts output (not swagger-generated — those are Phase 4)
_FETCH_GENRULE_JS = """
MATCH (t:BazelTarget)
WHERE t.is_genrule = true
  AND t.has_pnpm_src = true
  AND t.has_swagger_src = false
RETURN t.label AS label, t.package AS package
"""

_TS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})

_KIND_MAP = {
    "CLASS": SymbolKind.CLASS,
    "INTERFACE": SymbolKind.INTERFACE,
    "TYPE": SymbolKind.CLASS,  # store types as CLASS kind in graph
    "ENUM": SymbolKind.ENUM,
    "FUNCTION": SymbolKind.FUNCTION,
    "ARROW_FUNCTION": SymbolKind.FUNCTION,
    "REACT_COMPONENT": SymbolKind.CLASS,  # components as CLASS for blast radius
    "METHOD": SymbolKind.METHOD,
    "GETTER": SymbolKind.METHOD,
    "SETTER": SymbolKind.METHOD,
    "CONSTRUCTOR": SymbolKind.FUNCTION,
    "VARIABLE": SymbolKind.VARIABLE,
}


def _sym_id(file_path: str, sym: TSSymbol) -> str:
    parent = f"{sym.parent_class}." if sym.parent_class else ""
    return f"symbol::{file_path}::{parent}{sym.name}"


def _sym_to_dict(file_path: str, sym: TSSymbol) -> dict:
    kind = _KIND_MAP.get(sym.kind, SymbolKind.FUNCTION)
    sym_id = _sym_id(file_path, sym)
    lang = Language.TYPESCRIPT if file_path.endswith((".ts", ".tsx")) else Language.JAVASCRIPT
    return {
        "id": sym_id,
        "symbol_id": f"{file_path}::{sym.name}",
        "name": sym.name,
        "qualified_name": sym.name,
        "kind": kind.value,
        "language": lang.value,
        "file_path": file_path,
        "line_start": sym.line_start,
        "line_end": sym.line_end,
        "is_public": sym.is_exported,
        "is_test": "test" in file_path.lower() or "spec" in file_path.lower(),
        "is_entry_point": False,
        "signature": None,
        "docstring": None,
        "parent_class": sym.parent_class,
        "cyclomatic_complexity": None,
        "jsx_tags_used": sym.jsx_tags_used,
        "call_sites": sym.call_sites,
    }


class TSWorker(BaseWorker):
    def __init__(
        self,
        workspace_root: str,
        commit_sha: str | None = None,
        file_paths: list[str] | None = None,  # None = index all
    ) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha
        self.file_paths = file_paths

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="TSWorker")
        logger.info("TSWorker starting (workspace=%s)", self.workspace_root)

        try:
            # ── Step 1: find JS/TS targets ────────────────────────────────────
            targets = self._fetch_js_targets()
            logger.info("Step 1: %d JS/TS targets", len(targets))

            all_sf_dicts: list[dict] = []
            all_sym_dicts: list[dict] = []
            all_defines_edges: list[dict] = []
            all_imports_edges: list[dict] = []
            all_re_export_edges: list[dict] = []
            all_calls_edges: list[dict] = []

            # symbol_name → list of symbol_ids (for JSX tag resolution)
            name_to_sym_id: dict[str, list[str]] = {}

            # ── Step 2–8: parse each target's source files ────────────────────
            for target_rec in targets:
                label = target_rec["label"]
                ts_files = self._find_ts_files(target_rec)
                if not ts_files:
                    continue

                for ts_file in ts_files:
                    if self.file_paths and ts_file not in self.file_paths:
                        continue

                    parsed = parse_ts_file(os.path.join(self.workspace_root, ts_file))
                    # Normalize path to repo-relative
                    rel_path = ts_file

                    if parsed.has_parse_errors:
                        result.warnings.append(f"Parse errors in {rel_path}")

                    lang = (
                        Language.TYPESCRIPT
                        if rel_path.endswith((".ts", ".tsx"))
                        else Language.JAVASCRIPT
                    )

                    # ── SourceFile node ───────────────────────────────────────
                    all_sf_dicts.append(
                        {
                            "id": f"file::{rel_path}",
                            "path": rel_path,
                            "language": lang.value,
                            "sha256": "",  # Phase 3 will fill this in
                            "is_generated": False,
                            "line_count": 0,
                        }
                    )

                    # ── Symbol nodes ──────────────────────────────────────────
                    for sym in parsed.symbols:
                        sd = _sym_to_dict(rel_path, sym)
                        all_sym_dicts.append(sd)
                        name_to_sym_id.setdefault(sym.name, []).append(sd["id"])

                        all_defines_edges.append(
                            {
                                "from_id": f"file::{rel_path}",
                                "to_id": sd["id"],
                                "confidence": EdgeConfidence.EXTRACTED.value,
                                "confidence_score": 1.0,
                            }
                        )

                    # ── Import resolution ─────────────────────────────────────
                    loop = asyncio.new_event_loop()
                    for imp in parsed.imports:
                        if imp.is_type_only:
                            continue
                        try:
                            target_id, pkg_id, confidence = loop.run_until_complete(
                                resolve_import(
                                    imp.module_specifier,
                                    rel_path,
                                    self.workspace_root,
                                )
                            )
                        except Exception as e:
                            result.warnings.append(
                                f"Import resolution failed for "
                                f"'{imp.module_specifier}' in {rel_path}: {e}"
                            )
                            continue

                        if confidence == "SKIP":
                            continue

                        dest_id = target_id or (f"pnpm::{pkg_id}" if pkg_id else None)
                        if dest_id:
                            all_imports_edges.append(
                                {
                                    "from_id": f"file::{rel_path}",
                                    "to_id": dest_id,
                                    "import_type": "STATIC",
                                    "confidence": confidence,
                                    "confidence_score": {
                                        "EXTRACTED": 1.0,
                                        "INFERRED": 0.75,
                                        "AMBIGUOUS": 0.4,
                                    }.get(confidence, 0.5),
                                }
                            )

                    loop.close()

                    # ── Re-export edges ───────────────────────────────────────
                    for exp in parsed.exports:
                        if exp.is_re_export and exp.source_module:
                            all_re_export_edges.append(
                                {
                                    "from_file_id": f"file::{rel_path}",
                                    "source_module": exp.source_module,
                                    "exported_names": exp.exported_names,
                                    "base_dir": os.path.dirname(rel_path),
                                    "workspace_root": self.workspace_root,
                                }
                            )

            # ── Step 9: JSX and Normal CALLS edges ───────────────────────────────
            from axon.workers.ts.jsx import extract_jsx_call_edges

            all_calls_edges.extend(extract_jsx_call_edges(all_sym_dicts, name_to_sym_id))

            for sym_dict in all_sym_dicts:
                for call in sym_dict.get("call_sites", []):
                    callee_name = call["callee"]
                    if callee_name in name_to_sym_id:
                        for target_id in name_to_sym_id[callee_name]:
                            all_calls_edges.append(
                                {
                                    "from_id": sym_dict["id"],
                                    "to_id": target_id,
                                    "confidence": EdgeConfidence.INFERRED.value,
                                    "confidence_score": 0.8,
                                }
                            )

            # Clean up the dictionaries so they match the schema (no jsx_tags_used/call_sites)
            for sd in all_sym_dicts:
                sd.pop("jsx_tags_used", None)
                sd.pop("call_sites", None)

            # ── Step 10: write all nodes and edges ────────────────────────────
            logger.info("Writing %d SourceFile nodes ...", len(all_sf_dicts))
            result.nodes_written += writers.write_source_files(all_sf_dicts)

            logger.info("Writing %d Symbol nodes ...", len(all_sym_dicts))
            result.nodes_written += writers.write_symbols(all_sym_dicts)

            logger.info("Writing %d DEFINES edges ...", len(all_defines_edges))
            result.edges_written += writers.write_defines_edges(all_defines_edges)

            logger.info("Writing %d IMPORTS edges ...", len(all_imports_edges))
            result.edges_written += writers.write_imports_edges(all_imports_edges)

            logger.info("Writing %d RE_EXPORTS edges ...", len(all_re_export_edges))
            result.edges_written += writers.write_re_export_edges(all_re_export_edges)

            logger.info("Writing %d CALLS edges ...", len(all_calls_edges))
            result.edges_written += writers.write_calls_edges(all_calls_edges)

        except Exception as e:
            logger.exception("TSWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "TSWorker complete: %d nodes, %d edges, %d warnings",
            result.nodes_written,
            result.edges_written,
            len(result.warnings),
        )
        return result.finish(success=True)

    def _fetch_js_targets(self) -> list[dict]:
        with sync_session() as session:
            return session.run(_FETCH_JS_TARGETS, kinds=list(_JS_TARGET_KINDS)).data()

    def _find_ts_files(self, target_rec: dict) -> list[str]:
        """Find .ts/.tsx/.js/.jsx files for a Bazel JS target."""
        package_path = target_rec["package"].lstrip("/")
        candidate_roots = [
            os.path.join(self.workspace_root, package_path, "src"),
            os.path.join(self.workspace_root, package_path),
        ]
        ts_files = []
        for root in candidate_roots:
            if os.path.isdir(root):
                for dirpath, dirnames, filenames in os.walk(root):
                    # Skip node_modules, dist, build, .next directories
                    dirnames[:] = [
                        d
                        for d in dirnames
                        if d
                        not in ("node_modules", "dist", "build", ".next", "__pycache__", ".cache")
                    ]
                    for fn in filenames:
                        if Path(fn).suffix.lower() in _TS_EXTENSIONS:
                            full = os.path.join(dirpath, fn)
                            rel = os.path.relpath(full, self.workspace_root)
                            ts_files.append(rel)
                break
        return ts_files
