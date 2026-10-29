"""
SwaggerWorker: parses swagger/openapi specs from genrule targets with
has_swagger_src=true, writes SwaggerSchema, ApiEndpoint, GeneratedClient
nodes and their connecting edges.

Steps:
  1. Query Neo4j for genrule targets with has_swagger_src=true
  2. Parse each swagger spec file
  3. Write SwaggerSchema nodes
  4. Write ApiEndpoint nodes
  5. Write GeneratedClient nodes
  6. Write DEFINES_ENDPOINT edges (SwaggerSchema → ApiEndpoint)
  7. Write GENERATED_FROM edges (GeneratedClient → SwaggerSchema)
"""

from __future__ import annotations

import argparse
import logging
import sys

from axon.db import writers
from axon.db.connection import sync_session
from axon.models.enums import EdgeConfidence, Language
from axon.workers.base import BaseWorker, WorkerResult
from axon.workers.swagger.parser import parse_swagger_file

logger = logging.getLogger(__name__)

# Patterns to infer language from genrule output file extensions
_LANG_EXT_MAP = {
    ".py": Language.PYTHON,
    ".java": Language.JAVA,
    ".ts": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT,
}


def _infer_language(outs: list[str]) -> str:
    """Infer the target language from genrule output paths."""
    for out in outs:
        for ext, lang in _LANG_EXT_MAP.items():
            if out.endswith(ext):
                return lang.value
    return Language.GENERATED.value


def _infer_generator(cmd: str) -> str:
    """Infer the generator tool from the genrule cmd."""
    if not cmd:
        return "unknown"
    cmd_lower = cmd.lower()
    if "openapi-generator" in cmd_lower or "openapi_generator" in cmd_lower:
        return "openapi-generator"
    if "swagger-codegen" in cmd_lower:
        return "swagger-codegen"
    if "protoc" in cmd_lower:
        return "protoc"
    return "unknown"


def _find_swagger_src(srcs: list[str]) -> str | None:
    """Find the swagger.json path from genrule srcs."""
    for src in srcs:
        if any(p in src.lower() for p in ("swagger", "openapi")):
            # Strip Bazel label prefix: //services/payment:swagger.json → services/payment/swagger.json
            if src.startswith("//"):
                src = src.lstrip("/")
                src = src.replace(":", "/")
            return src
    return None


class SwaggerWorker(BaseWorker):
    """Indexes Swagger/OpenAPI specs from genrule targets."""

    def __init__(
        self,
        workspace_root: str,
        commit_sha: str | None = None,
    ) -> None:
        super().__init__(workspace_root)
        self.commit_sha = commit_sha

    def run(self) -> WorkerResult:
        result = WorkerResult(worker_name="SwaggerWorker")
        logger.info("SwaggerWorker starting (workspace=%s)", self.workspace_root)

        try:
            # Step 1: fetch swagger genrules
            targets = self._get_swagger_targets()
            logger.info("Step 1: found %d swagger genrule targets", len(targets))

            schema_dicts = []
            endpoint_dicts = []
            client_dicts = []
            defines_ep_edges = []
            generated_from_edges = []

            for target in targets:
                label = target["label"]
                srcs = target.get("genrule_srcs") or []
                outs = target.get("genrule_outs") or []

                swagger_path = _find_swagger_src(srcs)
                if not swagger_path:
                    result.warnings.append(f"No swagger src found for {label}")
                    continue

                # Step 2: parse the swagger file
                parsed = parse_swagger_file(swagger_path, self.workspace_root)
                if not parsed:
                    result.warnings.append(f"Could not parse {swagger_path}")
                    continue

                # Step 3: SwaggerSchema node
                schema_id = f"swagger::{parsed.path}"
                schema_dicts.append(
                    {
                        "id": schema_id,
                        "path": parsed.path,
                        "service_label": label,
                        "openapi_version": parsed.openapi_version,
                        "content_hash": parsed.content_hash,
                        "endpoint_count": len(parsed.endpoints),
                    }
                )

                # Step 4: ApiEndpoint nodes
                for ep in parsed.endpoints:
                    ep_id = f"{label}::{ep.method}:{ep.path}"
                    endpoint_dicts.append(
                        {
                            "id": f"endpoint::{ep_id}",
                            "endpoint_id": ep_id,
                            "http_method": ep.method,
                            "path": ep.path,
                            "service_label": label,
                            "operation_id": ep.operation_id,
                            "summary": ep.summary,
                            "swagger_source": parsed.path,
                            "schema_hash": ep.schema_hash,
                            "is_deprecated": ep.deprecated,
                        }
                    )

                    # Step 6: DEFINES_ENDPOINT edge
                    defines_ep_edges.append(
                        {
                            "from_id": schema_id,
                            "to_id": f"endpoint::{ep_id}",
                            "confidence": EdgeConfidence.EXTRACTED,
                            "valid_from_commit": self.commit_sha,
                        }
                    )

                # Step 5: GeneratedClient node
                client_id = f"genclient::{label}"
                client_dicts.append(
                    {
                        "id": client_id,
                        "bazel_label": label,
                        "source_swagger": parsed.path,
                        "target_language": _infer_language(outs),
                        "generator_tool": "unknown",
                        "output_paths": outs,
                    }
                )

                # Step 7: GENERATED_FROM edge
                generated_from_edges.append(
                    {
                        "from_id": client_id,
                        "to_id": schema_id,
                        "confidence": EdgeConfidence.EXTRACTED,
                        "valid_from_commit": self.commit_sha,
                    }
                )

            # Write all nodes
            result.nodes_written += writers.write_swagger_schemas(schema_dicts)
            result.nodes_written += writers.write_api_endpoints(endpoint_dicts)
            result.nodes_written += writers.write_generated_clients(client_dicts)

            # Write all edges
            result.edges_written += writers.write_defines_endpoint_edges(defines_ep_edges)
            result.edges_written += writers.write_generated_from_edges(generated_from_edges)

        except Exception as e:
            logger.exception("SwaggerWorker failed: %s", e)
            result.errors.append(str(e))
            return result.finish(success=False)

        logger.info(
            "SwaggerWorker complete: %d nodes, %d edges",
            result.nodes_written,
            result.edges_written,
        )
        return result.finish(success=True)

    def _get_swagger_targets(self) -> list[dict]:
        with sync_session() as session:
            return session.run("""
                MATCH (t:BazelTarget)
                WHERE t.has_swagger_src = true AND t.is_genrule = true
                RETURN t.id AS id, t.label AS label,
                       t.genrule_srcs AS genrule_srcs,
                       t.genrule_outs AS genrule_outs
            """).data()


def cli_main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    ap = argparse.ArgumentParser(description="axon: index Swagger/OpenAPI specs")
    ap.add_argument("--workspace", required=True, help="Path to the monorepo root")
    ap.add_argument("--commit", default=None, help="Git commit SHA")
    args = ap.parse_args()

    worker = SwaggerWorker(workspace_root=args.workspace, commit_sha=args.commit)
    result = worker.run()

    status = "✓" if result.success else "✗"
    print(f"\n{status} SwaggerWorker {'succeeded' if result.success else 'FAILED'}")
    print(f"  Nodes written : {result.nodes_written}")
    print(f"  Edges written : {result.edges_written}")
    print(f"  Duration      : {result.duration_ms} ms")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}")
        sys.exit(1)
