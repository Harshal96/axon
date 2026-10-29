"""
Resolves pnpm workspace packages to their Bazel target labels.

Given a workspace package at path 'packages/ui-components':
  1. Read 'packages/ui-components/package.json' to get the package name
     (e.g. '@company/ui-components')
  2. Find the BazelTarget in Neo4j whose package == '//packages/ui-components'
  3. Return the BazelTarget label

This is the critical link that allows TSWorker to resolve:
  import { Button } from '@company/ui-components'
  → PnpmPackage('@company/ui-components') → BazelTarget('//packages/ui-components:ui_components')
"""

from __future__ import annotations

import json
import logging
import os

from axon.db.connection import sync_session

logger = logging.getLogger(__name__)


def read_package_name(workspace_path: str, workspace_root: str) -> str | None:
    """
    Read the 'name' field from workspace_path/package.json.
    Returns None if the file doesn't exist or has no 'name'.
    """
    pkg_json_path = os.path.join(workspace_root, workspace_path, "package.json")
    try:
        with open(pkg_json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("name")
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Cannot read package.json at %s: %s", pkg_json_path, e)
        return None


def find_bazel_label_for_package(
    workspace_path: str,
) -> str | None:
    """
    Find the primary BazelTarget label for a workspace package path.

    Strategy:
    1. Look for a BazelTarget with package == '//' + workspace_path
    2. Prefer targets with kind JS_LIBRARY or JS_BINARY over GENRULE
    3. If multiple targets exist, prefer the one whose name matches the
       last component of the path (e.g. 'ui_components' for 'packages/ui-components')
    """
    bazel_package = "//" + workspace_path.lstrip("/")
    preferred_name = workspace_path.rsplit("/", 1)[-1].replace("-", "_")

    with sync_session() as session:
        result = session.run(
            """
            MATCH (t:BazelTarget {package: $pkg})
            WHERE t.kind IN ['JS_LIBRARY', 'JS_BINARY', 'GENRULE']
            RETURN t.label AS label, t.kind AS kind, t.name AS name
            ORDER BY
              CASE t.kind
                WHEN 'JS_LIBRARY' THEN 0
                WHEN 'JS_BINARY'  THEN 1
                ELSE 2
              END,
              CASE WHEN t.name = $preferred THEN 0 ELSE 1 END
            LIMIT 3
            """,
            pkg=bazel_package,
            preferred=preferred_name,
        )
        records = result.data()

    if not records:
        return None
    return records[0]["label"]


def build_workspace_map(
    workspace_packages: list,
    workspace_root: str,
) -> dict[str, dict]:
    """
    Given a list of WorkspacePackage objects, return a dict:
      package_name → {
          path: str,
          name: str,
          bazel_label: str | None
      }
    """
    result = {}
    for wp in workspace_packages:
        name = read_package_name(wp.path, workspace_root) or wp.name
        if not name:
            logger.warning("No package name found for workspace package at %s", wp.path)
            continue
        wp.name = name
        label = find_bazel_label_for_package(wp.path)
        result[name] = {
            "path": wp.path,
            "name": name,
            "bazel_label": label,
        }
    return result
