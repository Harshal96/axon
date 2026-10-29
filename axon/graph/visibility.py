"""
visibility_check: determines if from_label is allowed to depend on to_label
based on the to_label target's Bazel visibility list.
"""

from __future__ import annotations

import logging
import time

from axon.db.connection import async_session
from axon.graph.models import VisibilityCheckResult, Warning
from axon.graph.staleness import get_graph_meta

logger = logging.getLogger(__name__)


def _is_visibility_allowed(
    from_label: str,
    to_visibility: list[str],
    to_package: str,
) -> tuple[bool, str]:
    """
    Check if `from_label` matches any entry in `to_visibility`.

    Bazel visibility patterns:
      "//visibility:public"              → anyone can depend
      "//visibility:private"             → only same package
      "//pkg:__pkg__"                    → only targets in //pkg
      "//pkg:__subpackages__"            → //pkg and all sub-packages
      "//pkg:target"                     → specific target only
    """
    from_package = from_label.rsplit(":", 1)[0] if ":" in from_label else from_label

    for vis in to_visibility:
        if vis == "//visibility:public" or vis == "//...":
            return True, "Target has public visibility"

        if vis == "//visibility:private":
            if from_package == to_package:
                return True, "Same package (private visibility)"
            continue

        if vis.endswith(":__pkg__"):
            vis_pkg = vis.replace(":__pkg__", "")
            if from_package == vis_pkg:
                return True, f"From-package matches visibility grant {vis}"
            continue

        if vis.endswith(":__subpackages__"):
            vis_pkg = vis.replace(":__subpackages__", "")
            if from_package == vis_pkg or from_package.startswith(vis_pkg + "/"):
                return True, f"From-package is under {vis_pkg}"
            continue

        # Exact target match
        if from_label == vis:
            return True, f"Exact visibility grant for {vis}"

    return False, (
        f"Target visibility [{', '.join(to_visibility)}] does not include {from_package}"
    )


async def visibility_check(from_label: str, to_label: str) -> VisibilityCheckResult:
    """
    Check whether a proposed Bazel dependency from from_label to to_label
    is allowed by to_label's visibility rules.
    """
    start = time.monotonic()
    meta = await get_graph_meta()

    async with async_session() as session:
        result = await session.run(
            """
            MATCH (t:BazelTarget {label: $to_label})
            RETURN t.visibility AS visibility, t.package AS package
        """,
            to_label=to_label,
        )
        rec = await result.single()

    warnings: list[Warning] = []

    if rec is None:
        duration_ms = int((time.monotonic() - start) * 1000)
        meta.query_duration_ms = duration_ms
        meta.nodes_traversed = 0
        return VisibilityCheckResult(
            meta=meta,
            from_label=from_label,
            to_label=to_label,
            is_allowed=False,
            reason=f"Target not found: {to_label}",
            to_visibility=[],
            suggestion=f"Verify that {to_label} exists in the BUILD file.",
        )

    visibility_list = rec["visibility"] or []
    to_package = rec["package"] or ""

    # Default visibility (empty list) = private to own package
    if not visibility_list:
        visibility_list = ["//visibility:private"]
        warnings.append(
            Warning(
                code="DEFAULT_VISIBILITY",
                message=f"{to_label} has no explicit visibility — defaults to private.",
                affected_nodes=[to_label],
            )
        )

    is_allowed, reason = _is_visibility_allowed(from_label, visibility_list, to_package)

    suggestion = None
    if not is_allowed:
        from_pkg = from_label.rsplit(":", 1)[0] if ":" in from_label else from_label
        suggestion = (
            f'Add "{from_pkg}:__pkg__" to {to_label}\'s visibility list, '
            f'or use "//visibility:public" if this should be widely visible.'
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    meta.query_duration_ms = duration_ms
    meta.nodes_traversed = 1

    return VisibilityCheckResult(
        meta=meta,
        from_label=from_label,
        to_label=to_label,
        is_allowed=is_allowed,
        reason=reason,
        to_visibility=visibility_list,
        suggestion=suggestion,
        warnings=warnings,
    )
