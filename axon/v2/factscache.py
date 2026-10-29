"""Content-addressed per-file facts cache.

Keyed by sha256(parser_version + suffix + raw file bytes), so re-indexing
unchanged files skips parsing entirely. Best-effort by design: a miss,
a corrupt entry, or an unwritable cache dir never breaks indexing.
Delete the cache dir at any time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from axon.v2.facts import FileFacts, facts_from_dict, facts_to_dict

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1


def cache_dir() -> Path:
    override = os.environ.get("AXON_FACTS_CACHE_DIR", "")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "axon" / "facts"


def cache_key(suffix: str, version: int, raw: bytes) -> str:
    h = hashlib.sha256()
    h.update(str(version).encode())
    h.update(b"\0")
    h.update(suffix.lower().encode())
    h.update(b"\0")
    h.update(raw)
    return h.hexdigest()


def _entry_path(key: str, base: Path) -> Path:
    return base / key[:2] / f"{key}.json"


def load_facts(key: str, base: Path | None = None) -> FileFacts | None:
    path = _entry_path(key, base or cache_dir())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError:
        _unlink_quietly(path)
        return None
    try:
        if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION:
            raise ValueError("unsupported cache format")
        return facts_from_dict(data["facts"])
    except (ValueError, TypeError, KeyError):
        _unlink_quietly(path)
        return None


def store_facts(key: str, facts: FileFacts, base: Path | None = None) -> None:
    path = _entry_path(key, base or cache_dir())
    tmp = path.with_name(f"{key}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"v": FORMAT_VERSION, "facts": facts_to_dict(facts)})
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("facts cache store failed for %s", key, exc_info=True)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
