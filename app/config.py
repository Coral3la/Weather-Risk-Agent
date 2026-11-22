"""All paths, thresholds, caps and weights in one place (PLAN.md §3, §6).

Nothing here is computed from the data at run time: every value is fixed, so a
hub's score only changes when the data or this file changes.
"""

import os
from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
HUBS_PATH = ROOT / "data" / "hubs.json"
# The database is data/cache.db unless the CACHE_DB_PATH environment variable names
# another file (e.g. the copy made by scripts/simulate_alert.py). A relative path
# is taken from the repo root.
DEFAULT_DB_PATH = ROOT / "data" / "cache.db"
SIMULATED_DB_PATH = ROOT / "data" / "simulated.db"
DB_PATH = ROOT / (os.environ.get("CACHE_DB_PATH") or DEFAULT_DB_PATH)
WEB_PAGE_PATH = ROOT / "web" / "index.html"

# --- Observation window (§3) -------------------------------------------------
# Weather metrics in the score are yearly averages over these full calendar years.
FIRST_YEAR = 2021
LAST_YEAR = 2025
SCORE_YEARS = list(range(FIRST_YEAR, LAST_YEAR + 1))

# --- Weather metrics (Open-Meteo daily data) ----------------------------------
# Each metric counts the days where `variable` passes `threshold`.
# Metrics with a `cap` feed the score: min(days per year / cap, 1) x 100.
# The caps are judgment calls: the days-per-year level we treat as "maximum
# exposure" (score 100). Approved at Gate 2.
WEATHER_METRICS = {
    # "Day with snowfall" (§3): 0.25 cm = 0.1 in, the NWS threshold for measurable
    # snowfall. Used for weather statistics only, not for the score.
    "snowfall_days": {
        "label": "days with snowfall >= 0.25 cm",
        "variable": "snowfall_sum", "compare": ">=", "threshold": 0.25, "unit": "cm",
    },
    # 1 cm: snow that plausibly disrupts operations (§6). Our judgment call.
    # Cap 30: about two snow days a week through a four-month winter.
    "snow_days": {
        "label": "snow days (>= 1 cm)",
        "variable": "snowfall_sum", "compare": ">=", "threshold": 1.0, "unit": "cm",
        "cap": 30,
    },
    # -18 C = 0 F. Cap 30: a full month of such days per year.
    "severe_cold_days": {
        "label": "severe-cold days (Tmin <= -18 C)",
        "variable": "temperature_2m_min", "compare": "<=", "threshold": -18.0, "unit": "C",
        "cap": 30,
    },
    # 50 mm (about 2 in) of precipitation in one day. Cap 5: the wettest hub
    # (Houston) averages 4.2 such days per year.
    "heavy_rain_days": {
        "label": "heavy-rain days (>= 50 mm)",
        "variable": "precipitation_sum", "compare": ">=", "threshold": 50.0, "unit": "mm",
        "cap": 5,
    },
    # 38 C = 100 F. Cap 30: a full month of such days per year.
    "extreme_heat_days": {
        "label": "extreme-heat days (Tmax >= 38 C)",
        "variable": "temperature_2m_max", "compare": ">=", "threshold": 38.0, "unit": "C",
        "cap": 30,
    },
}

# --- NRI hazards (FEMA National Risk Index, annualized frequency) -------------
NRI_FIELD_TYPE = "AFREQ"  # decided at Gate 1 (§6)

NRI_HAZARD_NAMES = {
    "HRCN": "hurricane", "SWND": "strong wind", "IFLD": "inland flooding",
    "CFLD": "coastal flooding", "WNTW": "winter weather", "ISTM": "ice storm",
    "CWAV": "cold wave", "TRND": "tornado", "HAIL": "hail", "HWAV": "heat wave",
}

# Cap per hazard, in occurrences per year: min(AFREQ / cap, 1) x 100.
# Rule (approved at Gate 2): the 99th percentile (p99) of that hazard's AFREQ
# across all US counties in the December 2025 NRI release, rounded. A county in
# the national top 1% scores 100.
#
# How the p99 values were computed (one-off query, 2026-11-22; not run by the app):
#   POST https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/
#        National_Risk_Index_Counties/FeatureServer/0/query
#     where=1=1
#     outFields=STCOFIPS,HRCN_AFREQ,SWND_AFREQ,IFLD_AFREQ,CFLD_AFREQ,WNTW_AFREQ,
#               ISTM_AFREQ,CWAV_AFREQ,TRND_AFREQ,HAIL_AFREQ,HWAV_AFREQ
#     returnGeometry=false  orderByFields=STCOFIPS  f=json
#     resultRecordCount=2000, paged with resultOffset=0 and 2000  -> 3,232 counties
#   Per hazard: drop the nulls, sort, and take the 99th percentile with linear
#   interpolation between the two nearest ranks (position = (n - 1) x 0.99).
NRI_AFREQ_CAPS = {
    "HRCN": 0.32,   # p99 = 0.318  (2,485 counties with a value)
    "SWND": 7.0,    # p99 = 7.094  (3,225)
    "IFLD": 5.5,    # p99 = 5.643  (3,232)
    "CFLD": 9.0,    # p99 = 8.793  (588)
    "WNTW": 40.0,   # p99 = 40.890 (3,230)
    "ISTM": 2.0,    # p99 = 1.984  (3,001)
    "CWAV": 20.0,   # p99 = 19.492 (3,232)
    "TRND": 1.3,    # p99 = 1.281  (3,225)
    "HAIL": 10.0,   # p99 = 10.117 (3,225)
    "HWAV": 22.0,   # p99 = 22.258 (3,202)
}

# --- Categories (§6) -----------------------------------------------------------
CATEGORIES = {
    "winter": {
        "nri_hazards": ["WNTW", "ISTM", "CWAV"],
        "weather_metrics": ["snow_days", "severe_cold_days"],
    },
    "hurricane": {
        "nri_hazards": ["HRCN"],
        "weather_metrics": [],  # NRI only
    },
    "flood": {
        "nri_hazards": ["IFLD", "CFLD"],
        "weather_metrics": ["heavy_rain_days"],
    },
    "severe_storm": {
        "nri_hazards": ["TRND", "HAIL", "SWND"],
        "weather_metrics": [],  # NRI only
    },
    "heat": {
        "nri_hazards": ["HWAV"],
        "weather_metrics": ["extreme_heat_days"],
    },
}

# --- How the parts combine (§6, §14) -------------------------------------------
# Category score = NRI_SHARE x NRI part + (1 - NRI_SHARE) x weather part.
# A category with no weather metric is 100% NRI.
NRI_SHARE = 0.5

# Overall score = weighted mean of the category scores. Equal weights by default.
CATEGORY_WEIGHTS = {
    "winter": 1.0,
    "hurricane": 1.0,
    "flood": 1.0,
    "severe_storm": 1.0,
    "heat": 1.0,
}

# Scores are rounded to this many decimals when reported. Each step is computed
# from unrounded values, so re-adding rounded parts by hand can differ by 0.1.
SCORE_DECIMALS = 1

# --- Current risk and alerts (§11, bonus) ----------------------------------------
# current_risk = the highest score among the NWS alerts active at the hub's point,
# or 0 with no active alert. NWS severity is an ordered four-level scale, so the
# levels are spaced evenly. "Unknown" (severity not stated) counts as 0.
NWS_SEVERITY_SCORES = {
    "Extreme": 100,
    "Severe": 75,
    "Moderate": 50,
    "Minor": 25,
    "Unknown": 0,
}

# A refresh alerts when a hub's score moved at least this far, up or down, since
# the last snapshot.
# current_risk: 25 = one severity level, so every change of level alerts.
CURRENT_RISK_ALERT_THRESHOLD = 25
# Exposure (the overall score): it only moves when the data or this file changes.
# 1.0 is ten times the reporting precision, so rounding never alerts.
EXPOSURE_ALERT_THRESHOLD = 1.0

WEBHOOK_TIMEOUT_SECONDS = 10

# While the server runs, it checks the NWS alerts right after startup and then
# every this many minutes. 0 turns the scheduled check off. The environment
# variable of the same name overrides it for one run (a demo or a test).
ALERT_CHECK_INTERVAL_MINUTES = float(os.environ.get("ALERT_CHECK_INTERVAL_MINUTES", 60))

# --- Agent (§7) ------------------------------------------------------------------
REGIONS = ["Midwest", "South", "West", "Northeast"]  # US Census regions (§3)

# "overall" can be ranked like a category.
OVERALL = "overall"

# The LLM may ask for tools at most this many times per question.
MAX_TOOL_ROUNDS = 5

# If the final answer fails validation, ask the LLM again this many times.
ANSWER_RETRIES = 1
