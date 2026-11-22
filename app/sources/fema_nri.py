"""FEMA National Risk Index (NRI) client, county level (PLAN.md §5).

Queries FEMA's ArcGIS feature layer by county FIPS code. Free, no API key.
Field list: https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/National_Risk_Index_Counties/FeatureServer/0
"""

import requests

SOURCE = "fema_nri"
URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)
EXPECTED_VERSION = "December 2025"
HAZARDS = ["HRCN", "SWND", "IFLD", "CFLD", "WNTW", "ISTM", "CWAV", "TRND", "HAIL", "HWAV"]
# The three candidate field types compared at Gate 1:
# RISKS = hazard-type risk score, AFREQ = annualized frequency, EALS = expected-annual-loss score.
FIELD_TYPES = ["RISKS", "AFREQ", "EALS"]
ID_FIELDS = ["STCOFIPS", "COUNTY", "COUNTYTYPE", "STATEABBRV", "NRI_VER"]
OUT_FIELDS = ID_FIELDS + [f"{hazard}_{kind}" for hazard in HAZARDS for kind in FIELD_TYPES]
TIMEOUT_SECONDS = 60


def fetch_county(fips: str) -> dict:
    """Fetch the NRI row for one county. Returns the raw API response."""
    params = {
        "where": f"STCOFIPS = '{fips}'",
        "outFields": ",".join(OUT_FIELDS),
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(URL, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    # ArcGIS reports query errors inside a 200 response.
    if "error" in data:
        raise ValueError(f"NRI query failed for FIPS {fips}: {data['error']}")
    features = data.get("features", [])
    if len(features) != 1:
        raise ValueError(f"NRI returned {len(features)} rows for FIPS {fips}, expected 1")
    version = features[0]["attributes"].get("NRI_VER")
    if version != EXPECTED_VERSION:
        raise ValueError(
            f"NRI version for FIPS {fips} is {version!r}, expected {EXPECTED_VERSION!r}"
        )
    return data
