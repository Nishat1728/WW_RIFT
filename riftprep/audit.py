from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .providers import PROVIDERS


SOURCE_ENDPOINTS = {
    ("boundary", "usgs_wbd"): "https://txgeo.usgs.gov/arcgis/rest/services/NationalBaseData/WBD/MapServer/2",
    ("boundary", "nldi"): "https://api.water.usgs.gov/nldi/",
    ("boundary", "hydrobasins"): "https://www.hydrosheds.org/products/hydrobasins",
    ("dem", "usgs_3dep"): "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
    ("dem", "copernicus_glo30"): "https://copernicus-dem-30m.s3.amazonaws.com/",
    ("dem", "nasadem_srtm"): "https://lpdaac.usgs.gov/products/nasadem_hgtv001/",
    ("dem", "fabdem"): "https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn",
    ("dem", "merit_dem"): "https://hydro.iis.u-tokyo.ac.jp/~yamadai/MERIT_DEM/",
    ("dem", "alos_aw3d30"): "https://www.eorc.jaxa.jp/ALOS/en/dataset/aw3d30/",
    ("rivers", "usgs_3dhp"): "https://3dhp.nationalmap.gov/arcgis/rest/services/usgs_3dhp_all/FeatureServer/50",
    ("rivers", "nhdplus_hr"): "https://www.usgs.gov/national-hydrography/nhdplus-high-resolution",
    ("rivers", "openstreetmap"): "https://overpass.kumi.systems/api/interpreter",
    ("land_cover", "nlcd"): "https://www.mrlc.gov/data",
    ("land_cover", "landfire"): "https://www.landfire.gov/data",
    ("land_cover", "esa_worldcover"): "https://planetarycomputer.microsoft.com/api/stac/v1/collections/esa-worldcover",
    ("land_cover", "copernicus_landcover"): "https://land.copernicus.eu/en/products/global-dynamic-land-cover",
    ("land_cover", "dynamic_world"): "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1",
    ("land_cover", "corine"): "https://land.copernicus.eu/en/products/corine-land-cover",
    ("soil", "ssurgo"): "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest",
    ("soil", "gssurgo"): "https://www.nrcs.usda.gov/resources/data-and-reports/gridded-soil-survey-geographic-gssurgo-database",
    ("soil", "statsgo2"): "https://www.nrcs.usda.gov/resources/data-and-reports/description-of-statsgo2-database",
    ("soil", "soilgrids"): "https://maps.isric.org/mapserv?map=/map/sand.map&SERVICE=WCS&REQUEST=GetCapabilities",
    ("soil", "polaris"): "http://hydrology.cee.duke.edu/POLARIS/PROPERTIES/v1.0/",
    ("soil", "openlandmap"): "https://openlandmap.org/",
    ("soil", "hwsd"): "https://www.fao.org/soils-portal/data-hub/soil-maps-and-databases/harmonized-world-soil-database-v12/en/",
    ("rainfall", "mrms"): "https://noaa-mrms-pds.s3.amazonaws.com/index.html",
    ("rainfall", "hrrr"): "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/index.html",
    ("rainfall", "aorc"): "https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/index.html",
    ("rainfall", "stage_iv"): "https://water.noaa.gov/about/precipitation-data-access",
    ("rainfall", "nldas"): "https://disc.gsfc.nasa.gov/datasets?keywords=NLDAS",
    ("rainfall", "era5_land"): "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land",
    ("rainfall", "gpm_imerg"): "https://gpm.nasa.gov/data/imerg",
    ("rainfall", "daymet"): "https://opendap.earthdata.nasa.gov/collections/C2532426483-ORNL_CLOUD/granules/Daymet_Daily_V4R1.daymet_v4_daily_na_prcp_2024.nc",
    ("rainfall", "prism"): "https://prism.oregonstate.edu/recent/",
    ("inflows", "usgs_waterdata"): "https://api.waterdata.usgs.gov/ogcapi/v1/collections/monitoring-locations/items",
    ("inflows", "usgs_nwis_legacy"): "https://waterservices.usgs.gov/nwis/site/",
    ("inflows", "national_water_model"): "https://noaa-nwm-pds.s3.amazonaws.com/index.html",
    ("drainage", "openstreetmap"): "https://overpass.kumi.systems/api/interpreter",
    ("surge", "stofs_estofs"): "https://noaa-nos-stofs3d-pds.s3.amazonaws.com/index.html",
    ("surge", "adcirc"): "https://adcirc.org/",
    ("surge", "observed_stage"): "https://api.tidesandcurrents.noaa.gov/api/prod/",
}


def _special_request(category: str, provider: str, bbox, timeout: int):
    west, south, east, north = bbox
    if (category, provider) == ("boundary", "usgs_wbd"):
        url = SOURCE_ENDPOINTS[(category, provider)] + "/query"
        params = {"where": "1=1", "geometry": f"{west},{south},{east},{north}", "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnCountOnly": "true", "f": "json"}
        return requests.get(url, params=params, timeout=timeout), "API count query"
    if (category, provider) == ("dem", "usgs_3dep"):
        url = SOURCE_ENDPOINTS[(category, provider)] + "/exportImage"
        params = {"bbox": f"{west},{south},{east},{north}", "bboxSR": 4326, "imageSR": 4326, "size": "64,64", "format": "tiff", "pixelType": "F32", "f": "json"}
        return requests.get(url, params=params, timeout=timeout), "64x64 subset request"
    if (category, provider) == ("rivers", "usgs_3dhp"):
        url = SOURCE_ENDPOINTS[(category, provider)] + "/query"
        params = {"where": "1=1", "geometry": f"{west},{south},{east},{north}", "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnCountOnly": "true", "f": "json"}
        return requests.get(url, params=params, timeout=timeout), "API count query"
    if (category, provider) == ("inflows", "usgs_waterdata"):
        params = {"f": "json", "bbox": f"{west},{south},{east},{north}", "limit": 1}
        return requests.get(SOURCE_ENDPOINTS[(category, provider)], params=params, timeout=timeout), "OGC monitoring-location query"
    if (category, provider) == ("inflows", "usgs_nwis_legacy"):
        params = {"format": "rdb", "bBox": f"{west},{south},{east},{north}", "siteType": "ST", "siteStatus": "all"}
        return requests.get(SOURCE_ENDPOINTS[(category, provider)], params=params, timeout=timeout), "site query"
    if provider == "openstreetmap":
        query = f'[out:json][timeout:25];nwr["waterway"]({south},{west},{north},{east});out count;'
        return requests.get(SOURCE_ENDPOINTS[(category, provider)], params={"data": query}, timeout=timeout), "Overpass feature count"
    if (category, provider) == ("soil", "ssurgo"):
        payload = {
            "query": "SELECT TOP 1 areasymbol FROM sacatalog WHERE areasymbol LIKE 'CT%'",
            "format": "JSON+COLUMNNAME",
        }
        return requests.post(SOURCE_ENDPOINTS[(category, provider)], json=payload, timeout=timeout), "Soil Data Access query"
    return None, None


def audit_provider_catalog(
    bbox: tuple[float, float, float, float],
    output_directory: str | Path,
    timeout: int = 45,
) -> pd.DataFrame:
    """Attempt every catalog source and record success, access constraints, or failure."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for category, sources in PROVIDERS.items():
        for provider, metadata in sources.items():
            started = time.time()
            endpoint = SOURCE_ENDPOINTS.get((category, provider))
            if provider.startswith("local_") or provider in {"gauge_csv", "model_output", "municipal_gis", "swmm_inp", "external_series"}:
                rows.append({"category": category, "provider": provider, "status": "USER_FILE_REQUIRED", "attempt": "not a remote source", "http": None, "endpoint": endpoint, "detail": metadata["formats"], "seconds": 0.0})
                continue
            if provider in {"dem_derived", "synthetic_impervious"}:
                rows.append({"category": category, "provider": provider, "status": "DERIVED_AVAILABLE", "attempt": "local derivation", "http": None, "endpoint": endpoint, "detail": metadata["formats"], "seconds": 0.0})
                continue
            if endpoint is None:
                status = "USER_URL_REQUIRED" if provider == "url" else "ADAPTER_NOT_IMPLEMENTED"
                rows.append({"category": category, "provider": provider, "status": status, "attempt": "catalog entry", "http": None, "endpoint": None, "detail": metadata["mode"], "seconds": 0.0})
                continue
            try:
                response, attempt = _special_request(category, provider, bbox, timeout)
                if response is None:
                    response = requests.get(endpoint, timeout=timeout, stream=True, allow_redirects=True)
                    attempt = "catalog/service reachability"
                code = response.status_code
                if code in (401, 403):
                    status = "AUTH_REQUIRED"
                elif 200 <= code < 300:
                    status = "DATA_QUERY_SUCCESS" if attempt not in {"catalog/service reachability"} else "CATALOG_REACHABLE"
                else:
                    status = "HTTP_ERROR"
                content_type = response.headers.get("content-type", "")
                detail = content_type[:120]
                if "json" in content_type:
                    try:
                        payload = response.json()
                        detail = json.dumps(payload)[:500]
                    except ValueError:
                        pass
                response.close()
            except Exception as error:
                code, status, attempt, detail = None, "FAILED", "network/API attempt", f"{type(error).__name__}: {error}"[:500]
            rows.append({"category": category, "provider": provider, "status": status, "attempt": attempt, "http": code, "endpoint": endpoint, "detail": detail, "seconds": round(time.time() - started, 3)})
    table = pd.DataFrame(rows)
    table.to_csv(output_directory / "provider_audit.csv", index=False)
    (output_directory / "provider_audit.json").write_text(table.to_json(orient="records", indent=2), encoding="utf-8")
    return table
