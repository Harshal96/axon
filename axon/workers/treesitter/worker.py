"""
TreeSitterWorker: parses Python source files for BazelTarget nodes
already in the graph and writes SourceFile, Symbol, and code-level edges.

Steps:
  1. Query Neo4j for PY_LIBRARY / PY_BINARY / PY_TEST targets
  2. For each target, resolve .py files from the package directory
  3. Parse each .py file with tree-sitter-python
  4. Write SourceFile nodes
  5. Write BUILDS edges (BazelTarget → SourceFile)
  6. Write Symbol nodes
  7. Write DEFINES edges (SourceFile → Symbol)
  8. Write IMPORTS edges (SourceFile → SourceFile)
  9. Write CALLS edges (Symbol → Symbol)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from axon.db import writers
from axon.db.connection import sync_session
from axon.models.enums import EdgeConfidence, Language
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.treesitter.base import ParsedFile
from axon.workers.treesitter.python import PythonParser

logger = logging.getLogger(__name__)


class TreeSitterWorker(BaseWorker):
    """Indexes Python source files for BazelTargets already in the graph."""

    def __init__(
        self,
        workspace_root: str,
        commit_sha: str | None = None,
        packages: list[str] | None = None,
    ) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha
        self.packages = packages

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="TreeSitterWorker")
        logger.info(
            "TreeSitterWorker starting (workspace=%s, commit=%s)",
            self.workspace_root,
            self.commit_sha,
        )

        try:
            # Step 1: fetch Python targets from Neo4j
            targets = self._get_python_targets()
            logger.info("Step 1: found %d Python targets", len(targets))

            parser = PythonParser()
            all_parsed: list[ParsedFile] = []

            # Step 2+3: resolve files and parse
            for target in targets:
                package = target["package"]  # e.g. '//services/payment'
                py_files = self._resolve_py_files(package)
                for py_file in py_files:
                    parsed = parser.parse_file(py_file, self.workspace_root)
                    if parsed:
                        # Tag which target owns this file
                        parsed._target_id = target["id"]  # type: ignore[attr-defined]
                        all_parsed.append(parsed)

            logger.info("Step 2-3: parsed %d Python files", len(all_parsed))

            # Step 4: write SourceFile nodes
            file_dicts = [self._file_to_dict(pf) for pf in all_parsed]
            files_written = writers.write_source_files(file_dicts)
            result.nodes_written += files_written
            logger.info("Step 4: wrote %d SourceFile nodes", files_written)

            # Step 5: write BUILDS edges (BazelTarget → SourceFile)
            builds_dicts = []
            for pf in all_parsed:
                builds_dicts.append(
                    {
                        "from_id": pf._target_id,  # type: ignore[attr-defined]
                        "to_id": f"file::{pf.path}",
                        "confidence": EdgeConfidence.EXTRACTED,
                        "valid_from_commit": self.commit_sha,
                    }
                )
            builds_written = writers.write_builds_edges(builds_dicts)
            result.edges_written += builds_written
            logger.info("Step 5: wrote %d BUILDS edges", builds_written)

            # Step 6: write Symbol nodes
            symbol_dicts = []
            for pf in all_parsed:
                for sym in pf.symbols:
                    symbol_dicts.append(self._symbol_to_dict(sym, pf.path))
            symbols_written = writers.write_symbols(symbol_dicts)
            result.nodes_written += symbols_written
            logger.info("Step 6: wrote %d Symbol nodes", symbols_written)

            # Step 7: write DEFINES edges (SourceFile → Symbol)
            defines_dicts = []
            for pf in all_parsed:
                for sym in pf.symbols:
                    defines_dicts.append(
                        {
                            "from_id": f"file::{pf.path}",
                            "to_id": f"symbol::{sym.symbol_id}",
                            "confidence": EdgeConfidence.EXTRACTED,
                            "valid_from_commit": self.commit_sha,
                        }
                    )
            defines_written = writers.write_defines_edges(defines_dicts)
            result.edges_written += defines_written
            logger.info("Step 7: wrote %d DEFINES edges", defines_written)

            # Step 8: write IMPORTS edges (SourceFile → SourceFile)
            imports_dicts = self._build_import_edges(all_parsed)
            imports_written = writers.write_imports_edges(imports_dicts)
            result.edges_written += imports_written
            logger.info("Step 8: wrote %d IMPORTS edges", imports_written)

            # Step 9: write CALLS edges (Symbol → Symbol)
            calls_dicts = self._build_call_edges(all_parsed)
            calls_written = writers.write_calls_edges(calls_dicts)
            result.edges_written += calls_written
            logger.info("Step 9: wrote %d CALLS edges", calls_written)

        except Exception as e:
            logger.exception("TreeSitterWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "TreeSitterWorker complete: %d nodes, %d edges",
            result.nodes_written,
            result.edges_written,
        )
        return result.finish(success=True)

    def _get_python_targets(self) -> list[dict]:
        """Fetch Python BazelTarget nodes from Neo4j."""
        pkg_filter = ""
        params: dict = {}
        if self.packages:
            pkg_filter = "AND t.package IN $packages"
            params["packages"] = self.packages

        with sync_session() as session:
            records = session.run(
                f"""
                MATCH (t:BazelTarget)
                WHERE t.kind IN ['PY_LIBRARY', 'PY_BINARY', 'PY_TEST']
                {pkg_filter}
                RETURN t.id AS id, t.label AS label, t.package AS package
                """,
                **params,
            ).data()
        return records

    def _resolve_py_files(self, package: str) -> list[str]:
        """
        Given a package like '//services/payment', find all .py files
        in that directory. The synthetic integration monorepo keeps sources
        under src/, so recurse within the package while skipping build output.
        Returns repo-relative paths.
        """
        pkg_path = package.lstrip("/")
        abs_dir = Path(self.workspace_root) / pkg_path
        if not abs_dir.is_dir():
            return []
        py_files = []
        for f in abs_dir.rglob("*.py"):
            if any(part in {"__pycache__", ".venv", "bazel-bin"} for part in f.parts):
                continue
            if f.is_file() and not f.name.startswith("__"):
                py_files.append(str(f.relative_to(self.workspace_root)))
        return sorted(py_files)

    def _build_import_edges(self, all_parsed: list[ParsedFile]) -> list[dict]:
        """
        Resolve import statements to SourceFile → SourceFile edges.
        We build a lookup of known file paths and try to match module paths.
        """
        known_files: set[str] = {pf.path for pf in all_parsed}
        edges = []
        for pf in all_parsed:
            for imp in pf.imports:
                # Try to resolve module path → file path
                # e.g. 'services.payment.payment' → 'services/payment/payment.py'
                candidate = imp.module_path.replace(".", "/") + ".py"
                if candidate in known_files and candidate != pf.path:
                    edges.append(
                        {
                            "from_id": f"file::{pf.path}",
                            "to_id": f"file::{candidate}",
                            "import_names": imp.names,
                            "confidence": EdgeConfidence.EXTRACTED,
                            "valid_from_commit": self.commit_sha,
                        }
                    )
        return edges

    def _build_call_edges(self, all_parsed: list[ParsedFile]) -> list[dict]:
        """
        Resolve call expressions to Symbol → Symbol edges.
        We build a symbol name → symbol_id lookup and match calls by name.
        """
        # Build lookup: short name → list of symbol_ids
        name_to_ids: dict[str, list[str]] = {}
        for pf in all_parsed:
            for sym in pf.symbols:
                name_to_ids.setdefault(sym.name, []).append(sym.symbol_id)
                # Also register qualified_name suffix for method calls
                if sym.parent_class:
                    dotted = f"{sym.parent_class}.{sym.name}"
                    name_to_ids.setdefault(dotted, []).append(sym.symbol_id)

        edges = []
        for pf in all_parsed:
            for call in pf.calls:
                # Try direct match on called_name
                called = call.called_name
                # Strip common patterns: self.method() → method
                if called.startswith("self."):
                    called = called[5:]

                targets = name_to_ids.get(called, [])
                if not targets:
                    # Try the last component: obj.method → method
                    parts = called.rsplit(".", 1)
                    if len(parts) == 2:
                        targets = name_to_ids.get(parts[-1], [])

                confidence = (
                    EdgeConfidence.EXTRACTED if len(targets) == 1 else EdgeConfidence.AMBIGUOUS
                )

                for target_id in targets:
                    # Don't create self-edges
                    if target_id == call.caller_symbol_id:
                        continue
                    edges.append(
                        {
                            "from_id": f"symbol::{call.caller_symbol_id}",
                            "to_id": f"symbol::{target_id}",
                            "call_line": call.line,
                            "confidence": confidence,
                            "valid_from_commit": self.commit_sha,
                        }
                    )
        return edges

    @staticmethod
    def _file_to_dict(pf: ParsedFile) -> dict:
        return {
            "id": f"file::{pf.path}",
            "path": pf.path,
            "language": pf.language.value,
            "sha256": pf.sha256,
            "line_count": pf.line_count,
            "is_generated": False,
        }

    @staticmethod
    def _symbol_to_dict(sym, file_path: str) -> dict:
        return {
            "id": f"symbol::{sym.symbol_id}",
            "symbol_id": sym.symbol_id,
            "name": sym.name,
            "qualified_name": sym.qualified_name,
            "kind": sym.kind.value,
            "language": Language.PYTHON.value,
            "file_path": file_path,
            "line_start": sym.line_start,
            "line_end": sym.line_end,
            "docstring": sym.docstring,
            "signature": sym.signature,
            "is_public": sym.is_public,
            "is_test": sym.is_test,
            "parent_class": sym.parent_class,
        }


def cli_main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    ap = argparse.ArgumentParser(description="axon: index Python source files")
    ap.add_argument("--workspace", required=True, help="Path to the monorepo root")
    ap.add_argument("--commit", default=None, help="Git commit SHA being indexed")
    ap.add_argument("--packages", nargs="*", help="Limit to specific packages")
    args = ap.parse_args()

    worker = TreeSitterWorker(
        workspace_root=args.workspace,
        commit_sha=args.commit,
        packages=args.packages,
    )
    result = worker.run()

    status = "✓" if result.success else "✗"
    print(f"\n{status} TreeSitterWorker {'succeeded' if result.success else 'FAILED'}")
    print(f"  Nodes written : {result.nodes_written}")
    print(f"  Edges written : {result.edges_written}")
    print(f"  Duration      : {result.duration_ms} ms")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
        sys.exit(1)
