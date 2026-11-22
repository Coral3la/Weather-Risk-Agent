"""SQLite storage: the API response cache and the score snapshots (PLAN.md §9)."""

import json
import sqlite3
from datetime import datetime, timezone

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
    source       TEXT NOT NULL,   -- e.g. 'open_meteo', 'fema_nri'
    cache_key    TEXT NOT NULL,   -- hub id (open_meteo) or county FIPS (fema_nri)
    fetched_at   TEXT NOT NULL,   -- UTC, ISO 8601
    payload_json TEXT NOT NULL,   -- the raw API response
    PRIMARY KEY (source, cache_key)
);
CREATE TABLE IF NOT EXISTS score_snapshots (
    hub_id         TEXT NOT NULL,
    computed_at    TEXT NOT NULL, -- UTC, ISO 8601
    overall        REAL NOT NULL,
    breakdown_json TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Open the database, creating the file and tables if they don't exist."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def save_response(conn: sqlite3.Connection, source: str, cache_key: str, payload: dict) -> None:
    """Store one API response, replacing any earlier one for the same key.

    Does not commit: the caller commits once, so a refresh is all-or-nothing.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO api_cache (source, cache_key, fetched_at, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (source, cache_key, fetched_at, json.dumps(payload)),
    )


def load_response(conn: sqlite3.Connection, source: str, cache_key: str) -> dict:
    """Return one cached API response. Raises if refresh hasn't been run."""
    row = conn.execute(
        "SELECT payload_json FROM api_cache WHERE source = ? AND cache_key = ?",
        (source, cache_key),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"No cached {source} data for '{cache_key}'. Run: python -m app.refresh"
        )
    return json.loads(row[0])


def save_snapshot(conn: sqlite3.Connection, hub_id: str, computed_at: str, breakdown: dict) -> None:
    """Append one hub's score to the history. Does not commit."""
    conn.execute(
        "INSERT INTO score_snapshots (hub_id, computed_at, overall, breakdown_json) "
        "VALUES (?, ?, ?, ?)",
        (hub_id, computed_at, breakdown["overall"], json.dumps(breakdown)),
    )
