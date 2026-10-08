"""Print a small modern USGS Water Data API response for adapter diagnostics."""

import json

import requests


url = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items"
params = {
    "f": "json",
    "limit": 2,
    "monitoring_location_id": "USGS-01172010",
    "parameter_code": "00060",
    "datetime": "2023-06-10T00:00:00Z/2023-06-10T06:00:00Z",
}
response = requests.get(url, params=params, timeout=120)
print(response.status_code)
print(response.url)
response.raise_for_status()
payload = response.json()
print(payload.keys())
print(payload.get("numberMatched"), payload.get("numberReturned"))
print(json.dumps(payload.get("features", [])[:1], indent=2)[:8000])

location = requests.get(
    "https://api.waterdata.usgs.gov/ogcapi/v1/collections/monitoring-locations/items/USGS-01172010",
    params={"f": "json"}, timeout=120,
)
print(location.status_code)
location.raise_for_status()
print(json.dumps(location.json(), indent=2)[:8000])
