"""Fetch all external data into the SQLite cache.

Run: python -m app.refresh

This is the only code that calls external APIs. It always fetches fresh data;
everything else in the app reads from the cache.
"""

import json
from pathlib import Path

from app import db
from app.sources import fema_nri, open_meteo

HUBS_PATH = Path(__file__).resolve().parent.parent / "data" / "hubs.json"


def load_hubs() -> list[dict]:
    return json.loads(HUBS_PATH.read_text())


def refresh() -> None:
    hubs = load_hubs()

    # Fetch everything first, so a failed request leaves the existing cache untouched.
    responses = []  # (source, cache_key, payload)
    for hub in hubs:
        print(f"Fetching {hub['id']} ...", flush=True)
        responses.append((open_meteo.SOURCE, hub["id"], open_meteo.fetch_daily(hub)))
        responses.append((fema_nri.SOURCE, hub["fips"], fema_nri.fetch_county(hub["fips"])))

    conn = db.connect()
    with conn:  # one transaction
        for source, cache_key, payload in responses:
            db.save_response(conn, source, cache_key, payload)
    conn.close()
    print(f"Cached {len(responses)} responses for {len(hubs)} hubs in {db.DB_PATH}")


if __name__ == "__main__":
    refresh()
