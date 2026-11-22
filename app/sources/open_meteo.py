"""Open-Meteo Historical Weather API client (PLAN.md §5).

Docs: https://open-meteo.com/en/docs/historical-weather-api
Free, no API key. Data is gridded reanalysis, not station observations.
"""

import time

import requests

from app import config

SOURCE = "open_meteo"
URL = "https://archive-api.open-meteo.com/v1/archive"
START_DATE = f"{config.FIRST_YEAR}-01-01"
END_DATE = f"{config.LAST_YEAR}-12-31"
DAILY_VARIABLES = [
    "snowfall_sum",          # cm
    "precipitation_sum",     # mm
    "temperature_2m_max",    # °C
    "temperature_2m_min",    # °C
    "wind_gusts_10m_max",    # km/h
]
TIMEOUT_SECONDS = 60
# The free tier allows 600 weighted calls per minute, and one 5-year request for
# one hub counts as roughly 60. A full refresh of 14 hubs therefore hits the limit
# once; the API answers 429 with a Retry-After header, and we wait and retry.
MAX_ATTEMPTS = 3
DEFAULT_RETRY_SECONDS = 60


def fetch_daily(hub: dict) -> dict:
    """Fetch the daily weather history for one hub. Returns the raw API response."""
    params = {
        "latitude": hub["lat"],
        "longitude": hub["lon"],
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": "auto",  # days follow the hub's local time
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = requests.get(URL, params=params, timeout=TIMEOUT_SECONDS)
        if response.status_code != 429 or attempt == MAX_ATTEMPTS:
            break
        wait = int(response.headers.get("Retry-After", DEFAULT_RETRY_SECONDS)) + 1
        print(f"  Open-Meteo rate limit reached; waiting {wait}s ...", flush=True)
        time.sleep(wait)
    response.raise_for_status()
    data = response.json()

    daily = data.get("daily", {})
    missing = [name for name in ["time", *DAILY_VARIABLES] if name not in daily]
    if missing:
        raise ValueError(f"Open-Meteo response for {hub['id']} is missing: {missing}")
    return data
