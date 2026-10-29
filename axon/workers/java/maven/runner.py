"""
Runs `mvn dependency:list` to get the full transitive dependency set.

Output format (per line):
  [INFO]    group:artifact:jar:version:scope
  [INFO]    org.apache.commons:commons-collections:jar:3.2.1:compile

This is far more reliable than parsing the POM recursively, because Maven
resolves version conflicts, BOMs, and parent POMs automatically.

Falls back to POM-only parsing if Maven is not installed or the build fails.
"""

from __future__ import annotations

import logging
import re
import subprocess

from axon.workers.java.maven.pom_parser import MavenCoordinate

logger = logging.getLogger(__name__)

# Pattern matching Maven's dependency:list output
_DEP_PATTERN = re.compile(
    r"\[INFO\]\s+"
    r"(?P<group>[^\s:]+):(?P<artifact>[^\s:]+):(?:jar|pom|war|aar|bundle):"
    r"(?P<version>[^\s:]+):(?P<scope>compile|test|provided|runtime|system|import)"
)


class MavenRunnerError(Exception):
    pass


def list_transitive_deps(
    pom_path: str,
    workspace_root: str,
    timeout_seconds: int = 120,
    include_test_scope: bool = False,
) -> list[MavenCoordinate]:
    """
    Runs `mvn dependency:list` in the directory containing the POM and parses
    the output into MavenCoordinate objects.

    Returns an empty list (with a warning log) if:
      - Maven is not installed
      - The build fails (dependency resolution error in the POM)
      - Timeout exceeded

    Never raises — callers should fall back to pom_parser direct_dependencies.
    """
    import os

    pom_dir = os.path.dirname(os.path.abspath(pom_path))

    cmd = [
        "mvn",
        "dependency:list",
        "-DincludeScope=compile",
        "-DoutputAbsoluteArtifactFilename=false",
        "-q",  # quiet mode: suppress download progress
        "--batch-mode",  # no interactive prompts
        "-f",
        pom_path,
    ]
    if include_test_scope:
        cmd[1] = "dependency:list"
        cmd[3] = "-DincludeScope=test"

    try:
        result = subprocess.run(
            cmd,
            cwd=pom_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        logger.warning("mvn not found — falling back to POM-only parsing")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("mvn dependency:list timed out for %s", pom_path)
        return []

    if result.returncode != 0:
        logger.warning(
            "mvn dependency:list failed for %s (exit %d): %s",
            pom_path,
            result.returncode,
            result.stderr[:300],
        )
        return []

    deps = []
    for line in result.stdout.splitlines():
        m = _DEP_PATTERN.search(line)
        if m:
            scope = m.group("scope")
            if not include_test_scope and scope == "test":
                continue
            deps.append(
                MavenCoordinate(
                    group_id=m.group("group"),
                    artifact_id=m.group("artifact"),
                    version=m.group("version"),
                    scope=scope,
                )
            )

    logger.debug("mvn dependency:list for %s: %d transitive deps", pom_path, len(deps))
    return deps
