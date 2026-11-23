"""Git provenance for an indexed checkout; failures never permit skip checks."""

import subprocess
from pathlib import Path


def checkout_dirty(root: Path) -> bool | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def checkout_commit(root: Path, revision: str = "HEAD") -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                "--end-of-options",
                revision + "^{commit}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_index_commit(root: Path, requested: str, *, head: str | None = None) -> str:
    """Resolve a revision request against the working tree used for indexing."""
    head = checkout_commit(root) if head is None else head
    if requested and head and requested != head and checkout_commit(root, requested) != head:
        raise ValueError(
            f"requested commit {requested!r} does not match checkout HEAD {head!r}; "
            "check out the requested commit or omit commit"
        )
    # Source archives may carry supplied provenance but have no Git HEAD.
    return head or requested
