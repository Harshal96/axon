"""
JSX edge extractor: resolves JSX tag usages to CALLS edges.
"""

from __future__ import annotations

from collections.abc import Iterable

from axon.models.enums import EdgeConfidence


def extract_jsx_call_edges(
    symbols: Iterable[dict],
    name_to_sym_ids: dict[str, list[str]],
) -> list[dict]:
    """
    Given a list of symbol dictionaries (each MUST contain a 'jsx_tags_used' list)
    and a mapping of symbol name to a list of symbol IDs,
    returns a list of CALLS edge dictionaries for resolved JSX components.
    """
    edges = []
    for sym in symbols:
        tags = sym.get("jsx_tags_used", [])
        for tag in tags:
            # Simple heuristic: exact name match
            target_ids = name_to_sym_ids.get(tag, [])
            for target_id in target_ids:
                edges.append(
                    {
                        "from_id": sym["id"],
                        "to_id": target_id,
                        "confidence": EdgeConfidence.INFERRED.value,
                        "confidence_score": 0.8,
                    }
                )
    return edges
