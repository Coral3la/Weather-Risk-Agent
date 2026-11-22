"""National Weather Service (NWS) API client: active alerts at a point (PLAN.md §5, §11).

Docs: https://www.weather.gov/documentation/services-web-api
Free, no API key. The API requires a User-Agent header that identifies the app.
"""

import requests

SOURCE = "nws"
URL = "https://api.weather.gov/alerts/active"
HEADERS = {
    "User-Agent": "weather-risk-agent (take-home demo)",
    "Accept": "application/geo+json",
}
TIMEOUT_SECONDS = 30


def fetch_active_alerts(hub: dict) -> dict:
    """Fetch the alerts active right now at one hub's point. Returns the raw API response."""
    params = {
        "point": f"{hub['lat']},{hub['lon']}",
        "status": "actual",              # no tests or exercises
        "message_type": "alert,update",  # no cancellations
    }
    response = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    if not isinstance(data.get("features"), list):
        raise ValueError(f"NWS response for {hub['id']} has no 'features' list")
    return data
