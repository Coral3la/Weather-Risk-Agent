"""Fetch external data into the SQLite cache, then compute and store the scores.

Run: python -m app.refresh

    refresh()            fetches everything: weather history, NRI and NWS alerts.
    refresh(full=False)  fetches only the NWS alerts, the one source that changes
                         from hour to hour. Used by POST /scores/refresh.

Both then recompute the exposure score and the current risk of every hub, store
a snapshot, and alert on changes since the last snapshot (app/alerts.py).

This is the only code that calls the data APIs. It always fetches fresh data;
everything else in the app reads from the cache.
"""

import sqlite3
from datetime import datetime, timezone

from app import alerts, db, scoring
from app.sources import fema_nri, nws, open_meteo


def _fetch(hubs: list[dict], full: bool) -> list[tuple]:
    """Fetch the raw responses: (source, cache_key, payload) for each."""
    responses = []
    for hub in hubs:
        if full:
            print(f"Fetching {hub['id']} ...", flush=True)
            responses.append((open_meteo.SOURCE, hub["id"], open_meteo.fetch_daily(hub)))
            responses.append((fema_nri.SOURCE, hub["fips"], fema_nri.fetch_county(hub["fips"])))
        responses.append((nws.SOURCE, hub["id"], nws.fetch_active_alerts(hub)))
    return responses


def update_scores(conn: sqlite3.Connection) -> dict:
    """Recompute both scores from the cache, store a snapshot per hub, alert on changes."""
    previous = db.load_latest_snapshots(conn)
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    breakdowns = {breakdown["hub_id"]: breakdown for breakdown in scoring.score_all(conn)}

    summary = {"computed_at": computed_at, "hubs": [], "changes": []}
    with conn:  # one transaction
        for hub in scoring.load_hubs():
            breakdown = breakdowns[hub["id"]]
            risk = alerts.current_risk(conn, hub)
            summary["changes"] += alerts.find_changes(
                previous.get(hub["id"]), hub, breakdown["overall"], risk
            )
            db.save_snapshot(conn, hub["id"], computed_at, breakdown, risk["score"])
            summary["hubs"].append({
                "hub_id": hub["id"],
                "exposure": breakdown["overall"],
                "current_risk": risk["score"],
                "active_alerts": risk["alerts"],
            })
    alerts.send(summary["changes"], computed_at)
    return summary


def refresh(full: bool = True) -> dict:
    hubs = scoring.load_hubs()

    # Fetch everything first, so a failed request leaves the existing cache untouched.
    responses = _fetch(hubs, full)

    conn = db.connect()
    try:
        with conn:  # one transaction
            for source, cache_key, payload in responses:
                db.save_response(conn, source, cache_key, payload)
        print(f"Cached {len(responses)} responses for {len(hubs)} hubs in {db.DB_PATH}")
        summary = update_scores(conn)
    finally:
        conn.close()
    print(
        f"Stored {len(summary['hubs'])} score snapshots (computed at {summary['computed_at']}), "
        f"{len(summary['changes'])} alert(s)"
    )
    return summary


if __name__ == "__main__":
    for row in refresh()["hubs"]:
        events = ", ".join(alert["event"] for alert in row["active_alerts"]) or "no active alerts"
        print(f"  {row['hub_id']}: current risk {row['current_risk']} ({events})")
