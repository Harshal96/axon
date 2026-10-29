import sqlite3
import json
import time
from pathlib import Path
import os
from typing import Optional, Any

def get_cache_db_path() -> Path:
    path = Path(os.path.expanduser("~/.axon/cache.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_cache_db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS response_cache (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn

def set_cache(key: str, data: Any, ttl_seconds: int = 300) -> None:
    conn = get_connection()
    expires_at = time.time() + ttl_seconds
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (key, data, expires_at) VALUES (?, ?, ?)",
        (key, json.dumps(data), expires_at)
    )
    conn.commit()
    conn.close()

def get_cache(key: str) -> Optional[Any]:
    conn = get_connection()
    cursor = conn.execute(
        "SELECT data FROM response_cache WHERE key = ? AND expires_at > ?",
        (key, time.time())
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def get_cache_ignore_expiry(key: str) -> Optional[Any]:
    conn = get_connection()
    cursor = conn.execute(
        "SELECT data FROM response_cache WHERE key = ?",
        (key,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def invalidate_all() -> None:
    conn = get_connection()
    conn.execute("DELETE FROM response_cache")
    conn.commit()
    conn.close()

def purge_expired() -> None:
    conn = get_connection()
    conn.execute("DELETE FROM response_cache WHERE expires_at <= ?", (time.time(),))
    conn.commit()
    conn.close()
