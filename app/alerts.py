"""Current risk from NWS alerts, and alerts when a hub's risk changes (PLAN.md §11, bonus).

Two scores are watched, each with its own threshold in app/config.py:

    exposure      the overall score from app/scoring.py. Stable by design.
    current_risk  the highest severity among the NWS alerts active at the hub
                  right now, mapped to 0-100. Changes with the weather.

A refresh compares both with the hub's last snapshot. Each change at or over
its threshold is POSTed to ALERT_WEBHOOK_URL, or logged if the URL isn't set.
Like the scoring engine, this file is deterministic and reads only the cache.
"""

import logging
import os
import sqlite3

import requests
from dotenv import load_dotenv

from app import config, db
from app.sources import nws

load_dotenv()  # reads ALERT_WEBHOOK_URL from .env

log = logging.getLogger("app.alerts")


def parse_alerts(nws_response: dict) -> dict:
    """Turn one NWS response into {"score", "alerts"}.

    score is the current risk: the highest-scoring active alert, or 0 with none.
    alerts lists each active alert (event, severity, headline, score), highest first.
    """
    active = []
    for feature in nws_response["features"]:
        alert = feature["properties"]
        severity = alert["severity"]
        if severity not in config.NWS_SEVERITY_SCORES:
            raise ValueError(f"Unknown NWS severity {severity!r} in alert {alert['event']!r}")
        active.append({
            "event": alert["event"],
            "severity": severity,
            "headline": alert.get("headline"),
            "score": config.NWS_SEVERITY_SCORES[severity],
        })
    active.sort(key=lambda alert: alert["score"], reverse=True)
    return {"score": max([alert["score"] for alert in active], default=0), "alerts": active}


def current_risk(conn: sqlite3.Connection, hub: dict) -> dict:
    """One hub's current risk and active alerts, from its cached NWS response."""
    return parse_alerts(db.load_response(conn, nws.SOURCE, hub["id"]))


def _change(hub: dict, score_name: str, previous: float, current: float, threshold: float) -> dict:
    return {
        "hub_id": hub["id"],
        "city": hub["city"],
        "score": score_name,
        "previous": previous,
        "current": current,
        "change": round(current - previous, config.SCORE_DECIMALS),
        "threshold": threshold,
    }


def find_changes(previous: dict | None, hub: dict, overall: float, risk: dict) -> list[dict]:
    """Compare a hub's new scores with its last snapshot. Returns the changes to alert on.

    `previous` is {"overall": ..., "current_risk": ...}, or None if the hub has
    no snapshot. A score with no previous value is a baseline: nothing to compare.
    """
    changes = []
    if previous is None:
        return changes
    if abs(overall - previous["overall"]) >= config.EXPOSURE_ALERT_THRESHOLD:
        changes.append(_change(
            hub, "exposure", previous["overall"], overall, config.EXPOSURE_ALERT_THRESHOLD
        ))
    previous_risk = previous["current_risk"]
    if previous_risk is not None:
        if abs(risk["score"] - previous_risk) >= config.CURRENT_RISK_ALERT_THRESHOLD:
            change = _change(
                hub, "current_risk", previous_risk, risk["score"],
                config.CURRENT_RISK_ALERT_THRESHOLD,
            )
            change["active_alerts"] = risk["alerts"]
            changes.append(change)
    return changes


def send(changes: list[dict], computed_at: str) -> None:
    """Deliver each change: POST to ALERT_WEBHOOK_URL, or log it if the URL isn't set.

    Adds `computed_at` and `delivered` ("webhook", "log" or "webhook_failed") to
    each change. A webhook that fails is logged and doesn't stop the refresh.
    """
    url = os.environ.get("ALERT_WEBHOOK_URL")
    for change in changes:
        change["computed_at"] = computed_at
        line = (
            f"ALERT {change['city']} ({change['hub_id']}): {change['score']} "
            f"{change['previous']} -> {change['current']} "
            f"(change {change['change']:+}, threshold {change['threshold']})"
        )
        if not url:
            change["delivered"] = "log"
            log.warning(line)
            continue
        try:
            requests.post(url, json=change, timeout=config.WEBHOOK_TIMEOUT_SECONDS).raise_for_status()
            change["delivered"] = "webhook"
        except requests.RequestException as error:
            change["delivered"] = "webhook_failed"
            # Only the error type: the message would contain the webhook URL.
            log.warning(f"{line} [webhook failed: {type(error).__name__}]")
