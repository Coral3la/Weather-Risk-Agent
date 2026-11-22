"""Demo: make an alert fire with a SIMULATED weather alert, on a copy of the database.

Run: python -m scripts.simulate_alert

    1. Copies data/cache.db to data/simulated.db.
    2. In the copy, replaces one hub's cached NWS response with one alert that is
       clearly labeled SIMULATED.
    3. Runs the same compare-and-alert step as a refresh, on the copy.

The real database is only read, never written. The alert is always written to
the log, never sent to ALERT_WEBHOOK_URL: a simulated alert must not reach a
real channel.

To chat with the simulated data, start the server on the copy, with the scheduled
check off (it would replace the simulated alert with the real NWS data):

    CACHE_DB_PATH=data/simulated.db ALERT_CHECK_INTERVAL_MINUTES=0 uvicorn app.main:app
"""

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "cache.db"
SIMULATED_DB = ROOT / "data" / "simulated.db"

# Set before the app is imported: app/config.py reads CACHE_DB_PATH on import,
# and app/alerts.py reads ALERT_WEBHOOK_URL when it sends.
os.environ["CACHE_DB_PATH"] = str(SIMULATED_DB)
os.environ["ALERT_WEBHOOK_URL"] = ""  # log only

from app import db, refresh, scoring  # noqa: E402
from app.sources import nws  # noqa: E402

HUB_ID = "houston"
SIMULATED_RESPONSE = {
    "type": "FeatureCollection",
    "features": [{
        "properties": {
            "event": "SIMULATED Flash Flood Warning",
            "severity": "Severe",
            "headline": "SIMULATED alert for a demo. This is not a real NWS alert.",
        },
    }],
}


def main() -> None:
    if not REAL_DB.exists():
        raise SystemExit(f"{REAL_DB} not found. Run: python -m app.refresh")
    assert db.DB_PATH == SIMULATED_DB != REAL_DB  # every write below goes to the copy
    shutil.copy(REAL_DB, SIMULATED_DB)
    print(f"Copied {REAL_DB.name} to {SIMULATED_DB}")

    hub = next(hub for hub in scoring.load_hubs() if hub["id"] == HUB_ID)
    conn = db.connect()
    try:
        before = db.load_latest_snapshots(conn)[HUB_ID]
        print(f"{hub['city']}, last real snapshot: current_risk {before['current_risk']}")
        with conn:
            db.save_response(conn, nws.SOURCE, HUB_ID, SIMULATED_RESPONSE)
        print(f"Put a SIMULATED alert into {hub['city']}'s cached NWS response (in the copy)")
        summary = refresh.update_scores(conn)  # compare with the last snapshot, alert
    finally:
        conn.close()
    print(f"{len(summary['changes'])} alert(s) fired, delivered to the log (the ALERT line)")


if __name__ == "__main__":
    main()
