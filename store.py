"""On-disk cache of fetched paper text, so a paper fetched once stays readable
offline and isn't re-downloaded. One SQLite file in the data directory.

Off by default for the MCP server (it keeps its per-process memory cache); the
sidecar turns it on with enable().
"""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None


def data_dir() -> str:
    d = config.DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def enable() -> None:
    """Open (creating if needed) the cache database."""
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(os.path.join(data_dir(), "cache.sqlite"),
                                    check_same_thread=False)
            _conn.execute("CREATE TABLE IF NOT EXISTS fetched ("
                          "key TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
            _conn.execute("CREATE TABLE IF NOT EXISTS responses ("
                          "key TEXT PRIMARY KEY, data TEXT, fetched_at REAL)")
            _conn.commit()


def enabled() -> bool:
    return _conn is not None


def get(key: str):
    if _conn is None or not key:
        return None
    with _lock:
        row = _conn.execute("SELECT data FROM fetched WHERE key = ?",
                            (key.lower(),)).fetchone()
    return json.loads(row[0]) if row else None


def get_response(key: str, max_age_days: float = 30):
    """A cached API response (e.g. OpenAlex), if fresher than max_age_days."""
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute("SELECT data, fetched_at FROM responses WHERE key = ?",
                            (key,)).fetchone()
    if row and time.time() - row[1] < max_age_days * 86400:
        return json.loads(row[0])
    return None


def put_response(key: str, data) -> None:
    if _conn is None:
        return
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
                      (key, json.dumps(data), time.time()))
        _conn.commit()


def put(key: str, fetched: dict) -> None:
    # Don't pin a failed fetch: a paper that had no text today may tomorrow.
    if _conn is None or not key or fetched.get("source") in (None, "none"):
        return
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO fetched VALUES (?, ?, ?)",
                      (key.lower(), json.dumps(fetched), time.time()))
        _conn.commit()
