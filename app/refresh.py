"""Fetch all external data into the SQLite cache, then compute and store the scores.

Run: python -m app.refresh

This is the only code that calls external APIs. It always fetches fresh data;
everything else in the app reads from the cache.
"""

from datetime import datetime, timezone

from app import db, scoring
from app.sources import fema_nri, open_meteo


def refresh() -> None:
    hubs = scoring.load_hubs()

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
    print(f"Cached {len(responses)} responses for {len(hubs)} hubs in {db.DB_PATH}")

    # Compute the scores from the fresh cache and add one snapshot row per hub.
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    breakdowns = scoring.score_all(conn)
    with conn:
        for breakdown in breakdowns:
            db.save_snapshot(conn, breakdown["hub_id"], computed_at, breakdown)
    conn.close()
    print(f"Stored {len(breakdowns)} score snapshots (computed at {computed_at})")


if __name__ == "__main__":
    refresh()
