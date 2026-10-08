from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from .config import GridSpec


PROVIDERS: dict[str, dict[str, dict[str, Any]]] = {
    "boundary": {
        "usgs_wbd": {"coverage": "United States", "mode": "automatic", "formats": "HUC2-HUC16"},
        "nldi": {"coverage": "United States", "mode": "adapter", "formats": "pour-point basin"},
        "hydrobasins": {"coverage": "global", "mode": "catalog", "formats": "polygon"},
        "local_vector": {"coverage": "any", "mode": "automatic", "formats": "SHP/GPKG/GeoJSON"},
    },
    "dem": {
        "usgs_3dep": {"coverage": "United States", "mode": "py3dep/local", "formats": "GeoTIFF"},
        "copernicus_glo30": {"coverage": "global", "mode": "automatic", "formats": "COG"},
        "nasadem_srtm": {"coverage": "near-global", "mode": "catalog", "formats": "GeoTIFF/HGT"},
        "fabdem": {"coverage": "near-global", "mode": "catalog", "formats": "GeoTIFF"},
        "merit_dem": {"coverage": "global", "mode": "catalog", "formats": "GeoTIFF"},
        "alos_aw3d30": {"coverage": "global", "mode": "catalog", "formats": "GeoTIFF"},
        "local_raster": {"coverage": "any", "mode": "automatic", "formats": "GeoTIFF/NetCDF/ASC"},
        "url": {"coverage": "any", "mode": "automatic", "formats": "downloadable raster"},
    },
    "rivers": {
        "usgs_3dhp": {"coverage": "United States", "mode": "service", "formats": "vector"},
        "nhdplus_hr": {"coverage": "United States", "mode": "legacy", "formats": "GDB/vector"},
        "openstreetmap": {"coverage": "global", "mode": "adapter", "formats": "vector"},
        "dem_derived": {"coverage": "any", "mode": "derived", "formats": "vector/raster"},
        "local_vector": {"coverage": "any", "mode": "automatic", "formats": "SHP/GPKG/GeoJSON"},
    },
    "land_cover": {
        "nlcd": {"coverage": "United States", "mode": "catalog", "formats": "raster"},
        "landfire": {"coverage": "United States", "mode": "catalog", "formats": "raster"},
        "esa_worldcover": {"coverage": "global", "mode": "catalog", "formats": "COG"},
        "copernicus_landcover": {"coverage": "global", "mode": "catalog", "formats": "raster"},
        "dynamic_world": {"coverage": "global", "mode": "catalog", "formats": "raster"},
        "corine": {"coverage": "Europe", "mode": "catalog", "formats": "raster"},
        "local_raster": {"coverage": "any", "mode": "automatic", "formats": "raster"},
    },
    "soil": {
        "ssurgo": {"coverage": "United States", "mode": "service", "formats": "vector/tabular"},
        "gssurgo": {"coverage": "United States", "mode": "download", "formats": "GDB/raster"},
        "statsgo2": {"coverage": "United States", "mode": "download", "formats": "vector/tabular"},
        "soilgrids": {"coverage": "global", "mode": "service", "formats": "COG/WCS"},
        "polaris": {"coverage": "CONUS", "mode": "catalog", "formats": "raster"},
        "openlandmap": {"coverage": "global", "mode": "catalog", "formats": "COG"},
        "hwsd": {"coverage": "global", "mode": "catalog", "formats": "raster"},
        "local_raster": {"coverage": "any", "mode": "automatic", "formats": "raster/constant"},
    },
    "rainfall": {
        "mrms": {"coverage": "United States", "mode": "automatic", "formats": "GRIB2"},
        "hrrr": {"coverage": "CONUS", "mode": "adapter", "formats": "GRIB2"},
        "aorc": {"coverage": "CONUS", "mode": "adapter", "formats": "NetCDF"},
        "stage_iv": {"coverage": "CONUS", "mode": "catalog", "formats": "GRIB"},
        "nldas": {"coverage": "North America", "mode": "catalog", "formats": "NetCDF"},
        "era5_land": {"coverage": "global", "mode": "automatic/authenticated", "formats": "NetCDF"},
        "gpm_imerg": {"coverage": "global", "mode": "adapter", "formats": "HDF5/NetCDF"},
        "daymet": {"coverage": "North America", "mode": "automatic", "formats": "NetCDF"},
        "prism": {"coverage": "CONUS", "mode": "download", "formats": "BIL"},
        "gauge_csv": {"coverage": "any", "mode": "automatic", "formats": "CSV"},
        "local_grids": {"coverage": "any", "mode": "automatic", "formats": "NetCDF/GRIB/GeoTIFF/HDF5"},
    },
    "inflows": {
        "usgs_waterdata": {"coverage": "United States", "mode": "service", "formats": "OGC API JSON"},
        "usgs_nwis_legacy": {"coverage": "United States", "mode": "legacy", "formats": "WaterML JSON/RDB"},
        "national_water_model": {"coverage": "United States", "mode": "adapter", "formats": "NetCDF"},
        "model_output": {"coverage": "any", "mode": "automatic", "formats": "HEC-HMS/SWAT/CSV"},
        "local_csv": {"coverage": "any", "mode": "automatic", "formats": "CSV"},
    },
    "drainage": {
        "municipal_gis": {"coverage": "local", "mode": "automatic", "formats": "SHP/GPKG/GDB/REST/WFS"},
        "swmm_inp": {"coverage": "local", "mode": "adapter", "formats": "INP"},
        "openstreetmap": {"coverage": "global/variable", "mode": "adapter", "formats": "vector"},
        "synthetic_impervious": {"coverage": "any", "mode": "derived", "formats": "raster"},
        "local_raster": {"coverage": "any", "mode": "automatic", "formats": "ASC/GeoTIFF"},
        "external_series": {"coverage": "any", "mode": "automatic", "formats": "CSV/raster series"},
    },
    "surge": {
        "stofs_estofs": {"coverage": "coastal United States", "mode": "adapter", "formats": "NetCDF"},
        "adcirc": {"coverage": "model dependent", "mode": "adapter", "formats": "NetCDF"},
        "observed_stage": {"coverage": "site dependent", "mode": "adapter", "formats": "CSV"},
        "local_grids": {"coverage": "any", "mode": "automatic", "formats": "NetCDF/GeoTIFF/ASC"},
    },
}


def describe_providers(category: str | None = None):
    """Return the provider catalog as a tidy DataFrame."""
    import pandas as pd

    categories = [category] if category else list(PROVIDERS)
    rows = []
    for group in categories:
        if group not in PROVIDERS:
            raise KeyError(f"Unknown provider category: {group}")
        for name, details in PROVIDERS[group].items():
            rows.append({"category": group, "provider": name, **details})
    return pd.DataFrame(rows)


def load_vector(path: str | Path, layer: str | None = None) -> gpd.GeoDataFrame:
    data = gpd.read_file(path, layer=layer)
    if data.empty:
        raise ValueError(f"No features found in {path}")
    if data.crs is None:
        raise ValueError(f"Vector source has no CRS: {path}")
    return data


def download_url(url: str, destination: str | Path, timeout: int = 300) -> Path:
    """Download a file without hiding its source URL or changing its contents."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with destination.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    stream.write(chunk)
    return destination


def download_usgs_3dep(
    grid: GridSpec,
    destination: str | Path,
    block_size: int = 3000,
    max_tile_span_m: float = 250_000.0,
    service_url: str = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage",
) -> np.ndarray:
    """Request a DEM already aligned to the RIFT node grid from the USGS 3DEP image service.

    Large domains are requested in blocks. The returned GeoTIFF uses pixel centers at
    the RIFT node coordinates, which is why its outer bounds extend half a model cell.
    """
    height, width = grid.node_shape
    output = np.full((height, width), np.nan, dtype=np.float32)
    # ArcGIS may return an output URL and then fail with 502 when one export
    # spans a very large physical area, even if the requested pixel dimensions
    # are small. Bound both pixel count and ground span, and retry transient
    # metadata/output failures.
    spatial_block = max(1, int(max_tile_span_m / grid.resolution))
    row_block = min(block_size, spatial_block)
    col_block = min(block_size, spatial_block)
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        status=6,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    for row0 in range(0, height, row_block):
        row1 = min(row0 + row_block, height)
        for col0 in range(0, width, col_block):
            col1 = min(col0 + col_block, width)
            xmin = grid.xmin + col0 * grid.resolution - grid.resolution / 2
            xmax = grid.xmin + (col1 - 1) * grid.resolution + grid.resolution / 2
            ymax = grid.ymax - row0 * grid.resolution + grid.resolution / 2
            ymin = grid.ymax - (row1 - 1) * grid.resolution - grid.resolution / 2
            crs = rasterio.crs.CRS.from_user_input(grid.crs)
            wkid = crs.to_epsg()
            if wkid is None:
                raise ValueError("USGS 3DEP export requires a target CRS with an EPSG code.")
            params = {
                "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": wkid,
                "imageSR": wkid, "size": f"{col1-col0},{row1-row0}",
                "format": "tiff", "pixelType": "F32", "interpolation": "RSP_Bilinear",
                "f": "json",
            }
            metadata = session.get(service_url, params=params, timeout=180)
            metadata.raise_for_status()
            payload = metadata.json()
            if "href" not in payload:
                raise RuntimeError(f"USGS 3DEP export failed: {payload}")
            image = session.get(payload["href"], timeout=300)
            image.raise_for_status()
            with MemoryFile(image.content) as memory:
                with memory.open() as src:
                    tile = src.read(1).astype(np.float32)
                    nodata = src.nodata
            if tile.shape != (row1 - row0, col1 - col0):
                raise RuntimeError(f"USGS returned tile shape {tile.shape}, expected {(row1-row0, col1-col0)}")
            if nodata is not None:
                tile[np.isclose(tile, nodata)] = np.nan
            output[row0:row1, col0:col1] = tile
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "height": height, "width": width, "count": 1,
        "dtype": "float32", "crs": grid.crs,
        "transform": from_origin(
            grid.xmin - grid.resolution / 2, grid.ymax + grid.resolution / 2,
            grid.resolution, grid.resolution,
        ),
        "nodata": -9999.0, "compress": "deflate", "tiled": True,
    }
    with rasterio.open(destination, "w", **profile) as dst:
        dst.write(np.where(np.isfinite(output), output, -9999.0), 1)
    return output


def get_huc_boundary(
    huc_code: str,
    huc_level: int | None = None,
    service_url: str = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer",
    max_attempts: int = 6,
    retry_delay_seconds: float = 2.0,
) -> gpd.GeoDataFrame:
    """Download one WBD polygon with bounded transient/empty-response retries."""
    code = str(huc_code).strip()
    level = huc_level or len(code)
    if level not in (2, 4, 6, 8, 10, 12, 14, 16) or len(code) != level:
        raise ValueError("The HUC code length must match an even HUC level from 2 to 16.")
    max_attempts = max(1, int(max_attempts))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds))
    # The current USGS WBD service uses layer 0 for WBDLine, followed by
    # polygon layers 1..8 for HUC2..HUC16.  The older adapter incorrectly
    # subtracted one and therefore queried the line layer for HUC2.
    layer_id = level // 2
    # Service fields are huc2, huc4, ..., huc16 (no zero padding).
    field = f"huc{level}"
    params = {
        "where": f"{field}='{code}'",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    queries = [(f"{service_url}/{layer_id}/query", params, 120)]
    if level == 8:
        current_url = "https://txgeo.usgs.gov/arcgis/rest/services/NationalBaseData/WBD/MapServer/2/query"
        current_params = dict(params)
        current_params["where"] = f"HUC8='{code}'"
        queries.append((current_url, current_params, 180))

    payload: dict[str, Any] = {}
    last_detail = "empty feature collection"
    for attempt in range(1, max_attempts + 1):
        for query_url, query_params, timeout in queries:
            try:
                response = requests.get(
                    query_url, params=query_params, timeout=timeout
                )
                response.raise_for_status()
                candidate = response.json()
                if candidate.get("features"):
                    payload = candidate
                    break
                last_detail = str(
                    candidate.get("error") or "empty feature collection"
                )
            except (requests.RequestException, ValueError) as error:
                last_detail = f"{type(error).__name__}: {error}"
        if payload.get("features"):
            break
        if attempt < max_attempts and retry_delay_seconds:
            time.sleep(min(retry_delay_seconds * (2 ** (attempt - 1)), 30.0))
    if not payload.get("features"):
        raise LookupError(
            f"USGS WBD returned no polygon for {field}={code} after "
            f"{max_attempts} attempts ({last_detail})."
        )
    boundary = gpd.GeoDataFrame.from_features(payload["features"], crs="EPSG:4326")
    # Large WBD regions can contain self-touching rings after GeoJSON transfer.
    # Repair these deterministically before reprojection/masking, while refusing
    # to retain non-polygon fragments that make_valid() may produce.
    if not boundary.geometry.is_valid.all():
        try:
            boundary.geometry = boundary.geometry.make_valid()
        except AttributeError:
            boundary.geometry = boundary.geometry.buffer(0)
        boundary = boundary.explode(index_parts=False, ignore_index=True)
        boundary = boundary.loc[
            boundary.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        ].copy()
    if boundary.empty or not boundary.geometry.is_valid.all():
        raise ValueError(f"USGS WBD geometry for {field}={code} could not be repaired.")
    return boundary


def query_arcgis_features(
    layer_url: str,
    bounds: tuple[float, float, float, float],
    where: str = "1=1",
    out_fields: str = "*",
    page_size: int = 1000,
    timeout: int = 180,
) -> gpd.GeoDataFrame:
    """Download all ArcGIS features intersecting a WGS84 bounding box."""
    features = []
    offset = 0
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    while True:
        params = {
            "where": where, "geometry": ",".join(f"{value:.10g}" for value in bounds),
            "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields,
            "returnGeometry": "true", "outSR": 4326, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": page_size,
        }
        response = session.get(f"{layer_url.rstrip('/')}/query", params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("features", [])
        features.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    if not features:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def get_usgs_3dhp_flowlines(boundary: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Download current USGS 3DHP flowlines intersecting a boundary envelope."""
    bounds = tuple(float(value) for value in boundary.to_crs(4326).total_bounds)
    url = "https://3dhp.nationalmap.gov/arcgis/rest/services/usgs_3dhp_all/FeatureServer/50"
    flowlines = query_arcgis_features(url, bounds)
    if flowlines.empty:
        raise LookupError("USGS 3DHP returned no flowlines for the domain.")
    return flowlines
