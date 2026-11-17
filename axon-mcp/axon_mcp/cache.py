"""TTL cache partitioned by server identity, with observed-publication epochs."""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def get_cache_db_path() -> Path:
    path = Path(os.path.expanduser("~/.axon/cache.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_cache_db_path())
    # Separate tables deliberately retire unscoped legacy cache entries: their
    # server identity cannot be recovered safely during an adapter upgrade.
    conn.execute("""CREATE TABLE IF NOT EXISTS scoped_response_cache (
        namespace TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
        expires_at REAL NOT NULL, PRIMARY KEY (namespace, key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS observed_publication (
        namespace TEXT PRIMARY KEY, publication_id TEXT NOT NULL, epoch INTEGER NOT NULL)""")
    conn.commit()
    return conn


def _observation(conn: sqlite3.Connection, namespace: str) -> tuple[str, int]:
    row = conn.execute(
        "SELECT publication_id, epoch FROM observed_publication WHERE namespace = ?", (namespace,)
    ).fetchone()
    return (str(row[0]), int(row[1])) if row else ("", 0)


def cache_epoch(namespace: str) -> int:
    conn = get_connection()
    try:
        return _observation(conn, namespace)[1]
    finally:
        conn.close()


def observe_publication(
    namespace: str, publication_id: str, *, expected_epoch: int | None = None
) -> tuple[int, bool]:
    """Fence conflicting responses; only the current epoch may adopt an identity."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous, epoch = _observation(conn, namespace)
        if expected_epoch is not None and expected_epoch != epoch:
            if publication_id and publication_id != previous:
                # This response is exposed to its caller, but could be older OR
                # newer. Evict incompatible data and fence all current refills,
                # without adopting the rejected response's unordered identity.
                epoch += 1
                conn.execute("DELETE FROM scoped_response_cache WHERE namespace = ?", (namespace,))
                conn.execute(
                    "UPDATE observed_publication SET epoch = ? WHERE namespace = ?",
                    (epoch, namespace),
                )
                conn.commit()
            return epoch, False
        if publication_id and publication_id != previous:
            epoch += 1
            # Includes expired entries used by degraded fallback, and entries
            # made before this server first advertised a publication.
            conn.execute("DELETE FROM scoped_response_cache WHERE namespace = ?", (namespace,))
            conn.execute(
                "INSERT OR REPLACE INTO observed_publication VALUES (?, ?, ?)",
                (namespace, publication_id, epoch),
            )
        conn.commit()
        return epoch, True
    finally:
        conn.close()


def set_cache(
    key: str,
    data: Any,
    ttl_seconds: int = 300,
    *,
    namespace: str = "",
    expected_epoch: int | None = None,
) -> bool:
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if expected_epoch is not None and _observation(conn, namespace)[1] != expected_epoch:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO scoped_response_cache VALUES (?, ?, ?, ?)",
            (namespace, key, json.dumps(data), time.time() + ttl_seconds),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _get(key: str, namespace: str, *, expired: bool = False) -> Any | None:
    conn = get_connection()
    try:
        sql = "SELECT data FROM scoped_response_cache WHERE namespace = ? AND key = ?"
        args: tuple[Any, ...] = (namespace, key)
        if not expired:
            sql += " AND expires_at > ?"
            args += (time.time(),)
        row = conn.execute(sql, args).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def get_cache(key: str, *, namespace: str = "") -> Any | None:
    return _get(key, namespace)


def get_cache_ignore_expiry(key: str, *, namespace: str = "") -> Any | None:
    return _get(key, namespace, expired=True)


def invalidate_all() -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM scoped_response_cache")
        # Advancing epochs also fences requests already in flight.
        conn.execute("UPDATE observed_publication SET epoch = epoch + 1")
        conn.commit()
    finally:
        conn.close()


def purge_expired() -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM scoped_response_cache WHERE expires_at <= ?", (time.time(),))
        conn.commit()
    finally:
        conn.close()
