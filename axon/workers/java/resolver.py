"""
Resolves Java import statements to Bazel target labels.

The problem: tree-sitter gives us "import com.company.payment.PaymentGateway"
but what we need is "//services/payment:payment_gateway_java" — the Bazel
target that produces the JAR containing that class.

Resolution strategy (in order, stops at first hit):

1. Direct match: look up the Neo4j graph for a Symbol node with
   qualified_name = "com.company.payment.PaymentGateway" and return its
   owning BazelTarget.

2. Package match: look for any Symbol in the same package and return that
   target's label.

3. Genrule JAR name heuristic: look for a GENRULE BazelTarget whose
   genrule_outs contains a file ending in .jar and whose name matches the
   last component of the package (e.g. "payment_gateway" ≈ "payment.gateway").

4. Unresolved: return None and mark the IMPORTS edge as AMBIGUOUS confidence.
"""

from __future__ import annotations

import logging

from axon.db.connection import async_session

logger = logging.getLogger(__name__)


async def resolve_import_to_target(
    qualified_name: str,
    is_wildcard: bool,
) -> tuple[str, str] | None:
    """
    Returns (bazel_label, confidence) or None.
    confidence is one of: EXTRACTED | INFERRED | AMBIGUOUS
    """

    # ── Strategy 1: exact symbol lookup ───────────────────────────────────────
    if not is_wildcard:
        async with async_session() as session:
            result = await session.run(
                """
                MATCH (sym:Symbol {qualified_name: $qn})
                MATCH (sf:SourceFile {path: sym.file_path})
                MATCH (sf)<-[:BUILDS|PRODUCES]-(t:BazelTarget)
                RETURN t.label AS label LIMIT 1
            """,
                qn=qualified_name,
            )
            record = await result.single()
            if record:
                return record["label"], "EXTRACTED"

    # ── Strategy 2: package prefix match ──────────────────────────────────────
    # For "com.company.payment.PaymentGateway", look for targets whose
    # symbols share the package "com.company.payment"
    package = qualified_name.rsplit(".", 1)[0] if "." in qualified_name else qualified_name
    async with async_session() as session:
        result = await session.run(
            """
            MATCH (sym:Symbol)
            WHERE sym.qualified_name STARTS WITH $pkg
              AND sym.kind = 'CLASS'
            MATCH (sf:SourceFile {path: sym.file_path})
            MATCH (sf)<-[:BUILDS|PRODUCES]-(t:BazelTarget)
            RETURN t.label AS label, count(sym) AS sym_count
            ORDER BY sym_count DESC LIMIT 1
        """,
            pkg=package + ".",
        )
        record = await result.single()
        if record:
            return record["label"], "INFERRED"

    # ── Strategy 3: genrule JAR heuristic ────────────────────────────────────
    # Last resort: find a genrule that produces a .jar file with a similar name
    last_pkg_component = package.rsplit(".", 1)[-1].lower().replace("_", "")
    async with async_session() as session:
        result = await session.run(
            """
            MATCH (t:BazelTarget)
            WHERE t.is_genrule = true
              AND any(out IN t.genrule_outs WHERE out ENDS WITH '.jar')
              AND toLower(replace(t.name, '_', '')) CONTAINS $hint
            RETURN t.label AS label LIMIT 1
        """,
            hint=last_pkg_component,
        )
        record = await result.single()
        if record:
            return record["label"], "AMBIGUOUS"

    return None


async def resolve_cross_language_edges(
    consumer_target_label: str,
    java_target_label: str,
    java_symbols: list[dict],
) -> list[dict]:
    """
    When a Python target (consumer_target_label) depends on a Java genrule
    (java_target_label) via Bazel, create cross-language IMPORTS edges between
    the Python SourceFiles and the Java target.

    This is called by the JavaWorker after it detects that:
      python_target -[:DEPENDS_ON]-> java_genrule_target

    Returns a list of edge dicts suitable for write_cross_lang_edges().
    """
    edges = []

    # Find all Python SourceFiles owned by the consumer target
    async with async_session() as session:
        result = await session.run(
            """
            MATCH (t:BazelTarget {label: $consumer})
            MATCH (t)-[:BUILDS]->(sf:SourceFile)
            WHERE sf.language = 'PYTHON'
            RETURN sf.path AS path
        """,
            consumer=consumer_target_label,
        )
        records = await result.data()

    for rec in records:
        edges.append(
            {
                "from_id": f"file::{rec['path']}",
                "to_id": f"target::{java_target_label}",
                "confidence": "INFERRED",
                "confidence_score": 0.65,
                "is_cross_language": True,
                "consumer_language": "PYTHON",
                "provider_language": "JAVA",
            }
        )

    return edges
