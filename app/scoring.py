"""Deterministic scoring engine and weather statistics (PLAN.md §6).

Reads only from the SQLite cache. Every number the agent reports comes from here.

    NRI part       = mean over the category's hazards of min(AFREQ / cap, 1) x 100
    weather part   = mean over the category's metrics of min(days per year / cap, 1) x 100
    category score = NRI_SHARE x NRI part + (1 - NRI_SHARE) x weather part
    overall score  = weighted mean of the category scores
"""

import json
import sqlite3

from app import config, db

OPEN_METEO = "open_meteo"
FEMA_NRI = "fema_nri"


def load_hubs() -> list[dict]:
    return json.loads(config.HUBS_PATH.read_text())


# --- Small helpers -------------------------------------------------------------

def _round(value: float) -> float:
    return round(value, config.SCORE_DECIMALS)


def _scale(value: float, cap: float) -> float:
    """Scale a value against its fixed cap, onto 0-100."""
    return min(value / cap, 1.0) * 100


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _passes(value: float, metric: dict) -> bool:
    if metric["compare"] == ">=":
        return value >= metric["threshold"]
    return value <= metric["threshold"]


# --- Weather statistics ----------------------------------------------------------

def _count_days(daily: dict, metric_name: str, year: int) -> tuple[int, int]:
    """Return (days passing the metric's threshold, days with data) for one year."""
    metric = config.WEATHER_METRICS[metric_name]
    values = [
        value
        for date, value in zip(daily["time"], daily[metric["variable"]])
        if date.startswith(f"{year}-") and value is not None
    ]
    return sum(_passes(value, metric) for value in values), len(values)


def weather_stat(conn: sqlite3.Connection, hub_id: str, metric_name: str, year: int) -> dict:
    """Days in one year that pass a metric's threshold, as a count and a percentage."""
    if metric_name not in config.WEATHER_METRICS:
        raise ValueError(f"Unknown metric '{metric_name}'. Known: {list(config.WEATHER_METRICS)}")
    if year not in config.SCORE_YEARS:
        raise ValueError(f"No data for {year}. Available years: {config.SCORE_YEARS}")
    metric = config.WEATHER_METRICS[metric_name]
    daily = db.load_response(conn, OPEN_METEO, hub_id)["daily"]
    days, days_with_data = _count_days(daily, metric_name, year)
    return {
        "hub_id": hub_id,
        "metric": metric_name,
        "definition": metric["label"],
        "year": year,
        "days": days,
        "days_with_data": days_with_data,
        "percent_of_days": round(days / days_with_data * 100, 1),
    }


def average_days_per_year(daily: dict, metric_name: str) -> float:
    """Yearly average of a metric's day count over the observation window."""
    return _mean([_count_days(daily, metric_name, year)[0] for year in config.SCORE_YEARS])


# --- Scoring ---------------------------------------------------------------------

def _nri_part(nri: dict, hazards: list[str]) -> dict:
    rows = []
    for hazard in hazards:
        afreq = nri[f"{hazard}_{config.NRI_FIELD_TYPE}"]
        cap = config.NRI_AFREQ_CAPS[hazard]
        row = {
            "hazard": hazard,
            "name": config.NRI_HAZARD_NAMES[hazard],
            "afreq_per_year": None if afreq is None else round(afreq, 3),
            "cap": cap,
            "score": _scale(afreq or 0.0, cap),  # a null counts as 0 (§6)
        }
        if afreq is None:
            row["note"] = "no NRI record"
        rows.append(row)
    exact = _mean([row["score"] for row in rows])
    for row in rows:
        row["score"] = _round(row["score"])
    return {"score": _round(exact), "hazards": rows, "_exact": exact}


def _weather_part(daily: dict, metric_names: list[str]) -> dict:
    rows = []
    for name in metric_names:
        metric = config.WEATHER_METRICS[name]
        days_per_year = average_days_per_year(daily, name)
        rows.append({
            "metric": name,
            "definition": metric["label"],
            "days_per_year": round(days_per_year, 1),
            "cap": metric["cap"],
            "score": _scale(days_per_year, metric["cap"]),
        })
    exact = _mean([row["score"] for row in rows])
    for row in rows:
        row["score"] = _round(row["score"])
    return {"score": _round(exact), "metrics": rows, "_exact": exact}


def score_hub_exact(conn: sqlite3.Connection, hub: dict) -> tuple[dict, dict]:
    """Score one hub. Returns (breakdown, exact scores).

    The breakdown holds the rounded numbers that are reported. The exact scores
    ({category: score, "overall": score}, unrounded) are only for sorting, so
    two hubs that round to the same score still rank in their true order.
    """
    daily = db.load_response(conn, OPEN_METEO, hub["id"])["daily"]
    nri = db.load_response(conn, FEMA_NRI, hub["fips"])["features"][0]["attributes"]

    categories = {}
    exact_scores = {}
    for name, spec in config.CATEGORIES.items():
        nri_part = _nri_part(nri, spec["nri_hazards"])
        nri_exact = nri_part.pop("_exact")
        if spec["weather_metrics"]:
            weather_part = _weather_part(daily, spec["weather_metrics"])
            weather_exact = weather_part.pop("_exact")
            nri_share = config.NRI_SHARE
            exact = nri_share * nri_exact + (1 - nri_share) * weather_exact
        else:
            weather_part = None  # this category is NRI only
            nri_share = 1.0
            exact = nri_exact
        exact_scores[name] = exact
        categories[name] = {
            "score": _round(exact),
            "nri_share": nri_share,
            "weather_share": 1 - nri_share,
            "nri_part": nri_part,
            "weather_part": weather_part,
            "weight_in_overall": config.CATEGORY_WEIGHTS[name],
        }

    total_weight = sum(config.CATEGORY_WEIGHTS.values())
    overall = sum(exact_scores[name] * config.CATEGORY_WEIGHTS[name] for name in exact_scores) / total_weight
    exact_scores[config.OVERALL] = overall
    breakdown = {
        "hub_id": hub["id"],
        "city": hub["city"],
        "state": hub["state"],
        "region": hub["region"],
        "overall": _round(overall),
        "categories": categories,
        "observation_years": f"{config.FIRST_YEAR}-{config.LAST_YEAR}",
        "nri_county": f"{nri['COUNTY']} {nri['COUNTYTYPE']}, {nri['STATEABBRV']}",
        "nri_version": nri["NRI_VER"],
    }
    return breakdown, exact_scores


def score_hub(conn: sqlite3.Connection, hub: dict) -> dict:
    """Score one hub. Returns the full breakdown: inputs -> parts -> categories -> overall."""
    return score_hub_exact(conn, hub)[0]


def score_all(conn: sqlite3.Connection) -> list[dict]:
    """Score every hub, in hubs.json order."""
    return [score_hub(conn, hub) for hub in load_hubs()]


def score_all_exact(conn: sqlite3.Connection) -> list[tuple[dict, dict]]:
    """Score every hub, in hubs.json order: (breakdown, exact scores) per hub."""
    return [score_hub_exact(conn, hub) for hub in load_hubs()]
