"""Download adapters for additional public DEM, rainfall, and soil products.

Every adapter preserves the provider response before creating a small, explicit
normalization product used by the provider-neutral RIFT processing code.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
import xarray as xr
from pyproj import CRS
from rasterio.errors import RasterioIOError
from rasterio.merge import merge as merge_rasters
from rasterio.windows import Window, from_bounds
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "riftprep/0.4 (+https://github.com/)"


def _utc_naive(value: Any) -> pd.Timestamp:
    """Return a timestamp in UTC with the timezone marker removed."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def _download(
    url: str,
    destination: str | Path,
    *,
    params: Any = None,
    max_attempts: int = 5,
    timeout_seconds: float = 600.0,
    session: requests.Session | None = None,
) -> Path:
    """Download one provider response atomically with bounded retries."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            requester = session if session is not None else requests
            with requester.get(
                url,
                params=params,
                stream=True,
                timeout=float(timeout_seconds),
                headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if temporary.stat().st_size == 0:
                raise IOError(f"Provider returned an empty response for {url}")
            os.replace(temporary, destination)
            return destination
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt >= max(1, int(max_attempts)):
                raise
            time.sleep(min(30.0, 2.0**attempt))
    raise AssertionError("unreachable")


def _earthdata_session() -> requests.Session:
    """Create an authenticated NASA Earthdata session without exposing credentials."""
    try:
        import earthaccess
    except ImportError as error:
        raise ImportError(
            "Daymet now requires NASA Earthdata authentication. Install "
            "riftprep[daymet] and configure ~/.netrc for urs.earthdata.nasa.gov."
        ) from error
    netrc_files = (Path.home() / ".netrc", Path.home() / "_netrc")
    has_netrc = any(path.is_file() for path in netrc_files)
    has_environment = bool(
        os.environ.get("EARTHDATA_TOKEN")
        or (
            os.environ.get("EARTHDATA_USERNAME")
            and os.environ.get("EARTHDATA_PASSWORD")
        )
    )
    if not has_netrc and not has_environment:
        raise RuntimeError(
            "Daymet requires NASA Earthdata credentials. Configure a protected ~/.netrc "
            "entry for urs.earthdata.nasa.gov or Earthdata environment credentials."
        )
    strategy = "netrc" if has_netrc else "environment"
    try:
        earthaccess.login(strategy=strategy)
        return earthaccess.get_requests_https_session()
    except Exception as error:
        raise RuntimeError(
            "NASA Earthdata authentication failed. Check the credentials and authorize "
            "the ORNL DAAC application before retrying Daymet."
        ) from error


def _tile_code(latitude: int, longitude: int) -> str:
    latitude_code = f"N{latitude:02d}" if latitude >= 0 else f"S{abs(latitude):02d}"
    longitude_code = f"E{longitude:03d}" if longitude >= 0 else f"W{abs(longitude):03d}"
    return f"{latitude_code}_00_{longitude_code}_00"


def download_copernicus_glo30(
    boundary: gpd.GeoDataFrame,
    raw_dir: str | Path,
    *,
    reuse_existing: bool = False,
    max_attempts: int = 5,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Download public Copernicus DEM GLO-30 COG tiles and make a native mosaic."""
    raw_dir = Path(raw_dir)
    tile_dir = raw_dir / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    xmin, ymin, xmax, ymax = boundary.to_crs("EPSG:4326").total_bounds
    longitudes = range(int(np.floor(xmin)), int(np.ceil(xmax)))
    latitudes = range(int(np.floor(ymin)), int(np.ceil(ymax)))
    files: dict[str, Path] = {}
    tile_paths: list[Path] = []
    missing_tiles: list[str] = []
    for latitude in latitudes:
        for longitude in longitudes:
            code = _tile_code(latitude, longitude)
            stem = f"Copernicus_DSM_COG_10_{code}_DEM"
            destination = tile_dir / f"{stem}.tif"
            url = (
                "https://copernicus-dem-30m.s3.amazonaws.com/"
                f"{stem}/{stem}.tif"
            )
            try:
                if not (reuse_existing and destination.is_file()):
                    _download(
                        url, destination, max_attempts=max_attempts,
                        timeout_seconds=900,
                    )
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code == 404:
                    missing_tiles.append(code)
                    continue
                raise
            files[f"source_tile_{len(tile_paths) + 1:04d}"] = destination
            tile_paths.append(destination)
    if not tile_paths:
        raise LookupError("Copernicus GLO-30 returned no public DEM tile for the domain.")

    mosaic_path = raw_dir / "copernicus_glo30_native_mosaic.tif"
    if not (reuse_existing and mosaic_path.is_file()):
        sources = [rasterio.open(path) for path in tile_paths]
        try:
            mosaic, transform = merge_rasters(
                sources,
                nodata=-9999.0,
                target_aligned_pixels=True,
                resampling=rasterio.enums.Resampling.bilinear,
            )
            profile = sources[0].profile.copy()
        finally:
            for source in sources:
                source.close()
        values = mosaic[0].astype(np.float32, copy=False)
        profile.update(
            driver="GTiff", height=values.shape[0], width=values.shape[1],
            count=1, dtype="float32", transform=transform, nodata=-9999.0,
            compress="deflate", tiled=values.shape[0] >= 16 and values.shape[1] >= 16,
        )
        with rasterio.open(mosaic_path, "w", **profile) as output:
            output.write(values, 1)
    files["raster"] = mosaic_path
    return files, {
        "product": "Copernicus DEM GLO-30 Public",
        "resolution_m": 30,
        "source_tile_count": len(tile_paths),
        "missing_public_tiles": missing_tiles,
        "raw_source_tiles_modified": False,
        "mosaic_is_derived": True,
    }


def _daymet_crs(dataset: xr.Dataset, variable: xr.DataArray) -> str:
    grid_mapping = variable.attrs.get("grid_mapping")
    if grid_mapping and grid_mapping in dataset:
        return CRS.from_cf(dict(dataset[grid_mapping].attrs)).to_wkt()
    # Official Daymet North America Lambert Conformal Conic definition.
    return CRS.from_proj4(
        "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    ).to_wkt()


def _coordinate_in_metres(coordinate: xr.DataArray) -> np.ndarray:
    values = np.asarray(coordinate, dtype=float)
    units = str(coordinate.attrs.get("units", "")).lower()
    if units in {"km", "kilometer", "kilometers"}:
        values = values * 1000.0
    return values


def _download_daymet_opendap_subset(
    boundary: gpd.GeoDataFrame,
    year_start: pd.Timestamp,
    year_end: pd.Timestamp,
    year: int,
    region: str,
    destination: Path,
    session: requests.Session,
) -> str:
    """Read one spatial/time slice from NASA's current Daymet DAP4 collection."""
    try:
        from pydap.client import open_url
    except ImportError as error:
        raise ImportError(
            "Daymet requires PyDAP for the current NASA Earthdata OPeNDAP service. "
            "Install riftprep[daymet] or use environment.yml."
        ) from error
    url = (
        "https://opendap.earthdata.nasa.gov/collections/"
        "C2532426483-ORNL_CLOUD/granules/"
        f"Daymet_Daily_V4R1.daymet_v4_daily_{region}_prcp_{year}.nc"
    )
    try:
        remote = open_url(url, session=session, protocol="dap4")
    except Exception as error:
        raise RuntimeError(
            "Could not open the authenticated Daymet OPeNDAP granule. Confirm the "
            "ORNL DAAC application is authorized in NASA Earthdata Login."
        ) from error
    x = np.asarray(remote["x"][:].data, dtype=float).squeeze()
    y = np.asarray(remote["y"][:].data, dtype=float).squeeze()
    numeric_time = np.asarray(remote["time"][:].data).squeeze()
    time_attributes = dict(remote["time"].attributes)
    temporary_time = xr.Dataset(
        coords={
            "time": xr.Variable(
                ("time",), numeric_time,
                attrs={
                    "units": time_attributes.get("units", "days since 1950-01-01 00:00:00"),
                    "calendar": time_attributes.get("calendar", "standard"),
                },
            )
        }
    )
    decoded_time = pd.to_datetime(xr.decode_cf(temporary_time).time.values)
    time_indices = np.flatnonzero(
        (decoded_time.normalize() >= year_start.normalize())
        & (decoded_time.normalize() <= year_end.normalize())
    )
    if not time_indices.size:
        raise ValueError(
            f"Daymet granule for {year} has no records from {year_start.date()} "
            f"through {year_end.date()}."
        )
    mapping_keys = {
        "grid_mapping_name", "standard_parallel",
        "latitude_of_projection_origin", "longitude_of_central_meridian",
        "false_easting", "false_northing", "semi_major_axis",
        "inverse_flattening", "longitude_of_prime_meridian",
    }
    mapping_attributes = {
        key: value for key, value in remote["lambert_conformal_conic"].attributes.items()
        if key in mapping_keys
    }
    try:
        source_crs = CRS.from_cf(mapping_attributes)
    except Exception:
        source_crs = CRS.from_proj4(
            "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
    xmin, ymin, xmax, ymax = boundary.to_crs(source_crs).total_bounds
    x_indices = np.flatnonzero((x >= xmin) & (x <= xmax))
    y_indices = np.flatnonzero((y >= ymin) & (y <= ymax))
    if not x_indices.size:
        x_indices = np.array([int(np.nanargmin(np.abs(x - (xmin + xmax) / 2)))])
    if not y_indices.size:
        y_indices = np.array([int(np.nanargmin(np.abs(y - (ymin + ymax) / 2)))])
    time_slice = slice(int(time_indices.min()), int(time_indices.max()) + 1)
    y_slice = slice(int(y_indices.min()), int(y_indices.max()) + 1)
    x_slice = slice(int(x_indices.min()), int(x_indices.max()) + 1)
    precipitation = np.asarray(
        remote["prcp"][time_slice, y_slice, x_slice].data, dtype=np.float32
    )
    latitude = np.asarray(remote["lat"][y_slice, x_slice].data, dtype=np.float32)
    longitude = np.asarray(remote["lon"][y_slice, x_slice].data, dtype=np.float32)
    selected_time = numeric_time[time_slice]
    subset = xr.Dataset(
        {
            "prcp": (("time", "y", "x"), precipitation),
            "lat": (("y", "x"), latitude),
            "lon": (("y", "x"), longitude),
            "lambert_conformal_conic": xr.DataArray(
                np.int16(0),
                attrs=mapping_attributes,
            ),
        },
        coords={
            "time": xr.Variable(
                ("time",), selected_time,
                attrs={
                    "units": time_attributes.get("units", "days since 1950-01-01 00:00:00"),
                    "calendar": time_attributes.get("calendar", "standard"),
                },
            ),
            "x": xr.DataArray(x[x_slice], dims="x", attrs={"units": "m"}),
            "y": xr.DataArray(y[y_slice], dims="y", attrs={"units": "m"}),
        },
        attrs={
            "source": url,
            "access_method": "authenticated DAP4 spatial/time subset",
            "raw_provider_values_modified": "false",
        },
    )
    subset["prcp"].attrs.update(
        units=str(remote["prcp"].attributes.get("units", "mm/day")),
        grid_mapping="lambert_conformal_conic",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    subset.to_netcdf(destination)
    subset.close()
    return source_crs.to_wkt()


def download_daymet_hourly(
    boundary: gpd.GeoDataFrame,
    options: dict[str, Any],
    raw_dir: str | Path,
    *,
    reuse_existing: bool = False,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Download Daymet daily precipitation and explicitly distribute it over 24 hours."""
    if str(options.get("daily_distribution", "uniform_24h")) != "uniform_24h":
        raise ValueError(
            "Daymet currently requires daily_distribution='uniform_24h'; "
            "the daily-to-hourly assumption must be explicit."
        )
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = max(1, int(options.get("max_attempts", 5)))
    region = str(options.get("region", "na")).lower()
    if region not in {"na", "hi", "pr"}:
        raise ValueError("Daymet region must be 'na', 'hi', or 'pr'.")
    session = _earthdata_session()
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=max_attempts,
                connect=max_attempts,
                read=max_attempts,
                status=max_attempts,
                backoff_factor=2.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
            )
        ),
    )
    start = _utc_naive(options["start_utc"])
    end = _utc_naive(options["end_utc"])
    if end <= start or (end - start) % pd.Timedelta(hours=1):
        raise ValueError("Daymet event bounds must define a positive whole number of hours.")
    first_day = start.normalize()
    last_day = (end - pd.Timedelta(nanoseconds=1)).normalize()
    files: dict[str, Path] = {}
    pieces: list[xr.DataArray] = []
    source_crs = None
    for year in range(first_day.year, last_day.year + 1):
        year_start = max(first_day, pd.Timestamp(year=year, month=1, day=1))
        year_end = min(last_day, pd.Timestamp(year=year, month=12, day=31))
        raw_path = raw_dir / f"daymet_v4r1_{region}_prcp_{year}_subset.nc"
        if not (reuse_existing and raw_path.is_file()):
            source_crs = _download_daymet_opendap_subset(
                boundary, year_start, year_end, year, region, raw_path, session
            )
        files[f"provider_netcdf_{year}"] = raw_path
        with xr.open_dataset(raw_path) as dataset:
            if "prcp" not in dataset:
                raise KeyError(f"Daymet response lacks prcp: {raw_path}")
            data = dataset["prcp"].load()
            if "time" not in data.dims or "x" not in data.dims or "y" not in data.dims:
                raise ValueError(f"Unexpected Daymet dimensions: {data.dims}")
            data = data.assign_coords(
                x=_coordinate_in_metres(dataset["x"]),
                y=_coordinate_in_metres(dataset["y"]),
            )
            source_crs = source_crs or _daymet_crs(dataset, dataset["prcp"])
            pieces.append(data)
    daily = xr.concat(pieces, dim="time").sortby("time")
    day_lookup = {
        pd.Timestamp(value).normalize(): index
        for index, value in enumerate(pd.to_datetime(daily.time.values))
    }
    ending_times = pd.date_range(
        start=start + pd.Timedelta(hours=1), end=end, freq="1h"
    )
    hourly_arrays = []
    for ending in ending_times:
        source_day = (ending - pd.Timedelta(nanoseconds=1)).normalize()
        if source_day not in day_lookup:
            raise ValueError(f"Daymet returned no daily record for {source_day.date()}.")
        hourly_arrays.append(
            np.asarray(daily.isel(time=day_lookup[source_day]), dtype=np.float32) / 24.0
        )
    standardized = xr.Dataset(
        {
            "APCP_surface": (
                ("time", "y", "x"), np.stack(hourly_arrays).astype(np.float32)
            )
        },
        coords={"time": ending_times, "x": daily.x.values, "y": daily.y.values},
        attrs={
            "source": "Daymet V4 R1 daily precipitation",
            "source_crs": source_crs,
            "temporal_conversion": "daily total divided uniformly among 24 UTC hours",
            "raw_native_modified": "false",
        },
    )
    standardized["APCP_surface"].attrs.update(units="mm", interval="1 hour")
    normalized_path = raw_dir / "daymet_hourly_adapter_output.nc"
    standardized.to_netcdf(normalized_path)
    standardized.close()
    daily.close()
    files["netcdf"] = normalized_path
    return files, {
        "product": "Daymet V4 R1",
        "native_resolution_m": 1000,
        "native_time_step": "daily",
        "rift_adapter_time_step": "hourly",
        "daily_distribution": "uniform_24h",
        "region": region,
        "time_records": len(ending_times),
        "raw_native_modified": False,
    }


def _s3_keys(prefix: str, *, max_attempts: int = 5) -> list[str]:
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                "https://noaa-mrms-pds.s3.amazonaws.com/",
                params={"list-type": "2", "prefix": prefix},
                timeout=120,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            return [
                node.text for node in root.findall("{*}Contents/{*}Key") if node.text
            ]
        except Exception:
            if attempt == max_attempts:
                raise
            time.sleep(min(30.0, 2.0**attempt))
    raise AssertionError("unreachable")


_MRMS_TIME = re.compile(r"_(\d{8}-\d{6})\.grib2\.gz$")


def _read_mrms_subset(
    decoded: Path, query: gpd.GeoDataFrame
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Read an MRMS GRIB subset with GDAL, falling back to cfgrib/ecCodes."""
    try:
        with rasterio.open(decoded) as source:
            if source.crs is None:
                raise ValueError(f"MRMS GRIB has no CRS: {decoded}")
            bounds = query.to_crs(source.crs).total_bounds
            fractional = from_bounds(*bounds, transform=source.transform)
            col_start = max(0, int(np.floor(fractional.col_off)))
            row_start = max(0, int(np.floor(fractional.row_off)))
            col_stop = min(
                source.width, int(np.ceil(fractional.col_off + fractional.width))
            )
            row_stop = min(
                source.height, int(np.ceil(fractional.row_off + fractional.height))
            )
            if col_stop <= col_start or row_stop <= row_start:
                raise ValueError(
                    f"The selected study area does not overlap MRMS record {decoded}."
                )
            window = Window(
                col_start, row_start, col_stop - col_start, row_stop - row_start
            )
            values = (
                source.read(1, window=window, masked=True)
                .astype(np.float32)
                .filled(np.nan)
            )
            transform = source.window_transform(window)
            x = transform.c + (np.arange(values.shape[1]) + 0.5) * transform.a
            y = transform.f + (np.arange(values.shape[0]) + 0.5) * transform.e
            return values, x, y, source.crs.to_wkt()
    except RasterioIOError as gdal_error:
        try:
            with xr.open_dataset(
                decoded, engine="cfgrib", backend_kwargs={"indexpath": ""}
            ) as dataset:
                candidates = [
                    variable for variable in dataset.data_vars
                    if {"latitude", "longitude"}.issubset(dataset[variable].dims)
                ]
                if not candidates:
                    raise ValueError(
                        f"cfgrib found no latitude/longitude MRMS field in {decoded}."
                    )
                data = dataset[max(candidates, key=lambda name: dataset[name].size)]
                for dimension in set(data.dims) - {"latitude", "longitude"}:
                    if data.sizes[dimension] != 1:
                        raise ValueError(
                            f"Unsupported MRMS dimension {dimension!r} with size "
                            f"{data.sizes[dimension]}."
                        )
                    data = data.isel({dimension: 0}, drop=True)
                longitude = np.asarray(data.longitude, dtype=float)
                latitude = np.asarray(data.latitude, dtype=float)
                if longitude.ndim != 1 or latitude.ndim != 1:
                    raise ValueError("MRMS cfgrib fallback requires a regular latitude/longitude grid.")
                xmin, ymin, xmax, ymax = query.total_bounds
                if np.nanmax(longitude) > 180.0 and xmin < 0.0:
                    xmin, xmax = xmin + 360.0, xmax + 360.0
                x_indices = np.flatnonzero((longitude >= xmin) & (longitude <= xmax))
                y_indices = np.flatnonzero((latitude >= ymin) & (latitude <= ymax))
                if not x_indices.size:
                    x_indices = np.array([int(np.nanargmin(np.abs(longitude - (xmin + xmax) / 2)))])
                if not y_indices.size:
                    y_indices = np.array([int(np.nanargmin(np.abs(latitude - (ymin + ymax) / 2)))])
                subset = data.isel(latitude=y_indices, longitude=x_indices).load()
                return (
                    np.asarray(subset, dtype=np.float32),
                    longitude[x_indices], latitude[y_indices], CRS.from_epsg(4326).to_wkt(),
                )
        except (ImportError, ModuleNotFoundError) as error:
            raise ImportError(
                "MRMS requires either a GDAL GRIB driver or cfgrib with ecCodes. "
                "Install riftprep[mrms] or use environment.yml."
            ) from error
        except Exception as cfgrib_error:
            raise RuntimeError(
                f"Neither GDAL nor cfgrib could decode MRMS record {decoded}."
            ) from cfgrib_error


def download_mrms_hourly(
    boundary: gpd.GeoDataFrame,
    options: dict[str, Any],
    raw_dir: str | Path,
    *,
    reuse_existing: bool = False,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Download NOAA MRMS one-hour QPE files and create a domain subset NetCDF."""
    raw_dir = Path(raw_dir)
    compressed_dir = raw_dir / "provider_grib2_gz"
    decoded_dir = raw_dir / "decoded_grib2"
    compressed_dir.mkdir(parents=True, exist_ok=True)
    decoded_dir.mkdir(parents=True, exist_ok=True)
    start = _utc_naive(options["start_utc"])
    end = _utc_naive(options["end_utc"])
    expected = pd.date_range(start + pd.Timedelta(hours=1), end, freq="1h")
    expected_set = set(expected.to_pydatetime())
    product = str(options.get("product", "MultiSensor_QPE_01H_Pass2_00.00"))
    region = str(options.get("region", "CONUS"))
    max_attempts = int(options.get("max_attempts", 5))
    dates = pd.date_range(start.normalize(), end.normalize(), freq="1D")
    selected: list[tuple[pd.Timestamp, str]] = []
    for day in dates:
        prefix = f"{region}/{product}/{day:%Y%m%d}/"
        for key in _s3_keys(prefix, max_attempts=max_attempts):
            match = _MRMS_TIME.search(key)
            if not match:
                continue
            timestamp = pd.to_datetime(match.group(1), format="%Y%m%d-%H%M%S")
            if timestamp.to_pydatetime() in expected_set:
                selected.append((timestamp, key))
    selected.sort(key=lambda item: item[0])
    if [item[0] for item in selected] != list(expected):
        available = {item[0] for item in selected}
        missing = [str(value) for value in expected if value not in available]
        raise LookupError(
            f"MRMS archive lacks {len(missing)} expected hourly record(s): {missing[:8]}"
        )

    query = boundary.to_crs("EPSG:4326")
    xmin, ymin, xmax, ymax = query.total_bounds
    files: dict[str, Path] = {}
    arrays: list[np.ndarray] = []
    x = y = None
    source_crs = None
    reference_shape = None
    for index, (timestamp, key) in enumerate(selected, start=1):
        compressed = compressed_dir / Path(key).name
        if not (reuse_existing and compressed.is_file()):
            _download(
                f"https://noaa-mrms-pds.s3.amazonaws.com/{key}", compressed,
                max_attempts=max_attempts,
            )
        files[f"provider_grib2_gz_{index:04d}"] = compressed
        decoded = decoded_dir / compressed.name.removesuffix(".gz")
        if not (reuse_existing and decoded.is_file()):
            with gzip.open(compressed, "rb") as source, decoded.open("wb") as target:
                shutil.copyfileobj(source, target)
        values, current_x, current_y, current_crs = _read_mrms_subset(decoded, query)
        values[values < 0] = np.nan
        if reference_shape is None:
            reference_shape = values.shape
            x, y = current_x, current_y
            source_crs = current_crs
        elif values.shape != reference_shape or not (
            np.allclose(current_x, x) and np.allclose(current_y, y)
        ):
            raise ValueError("MRMS hourly records do not share one spatial grid.")
        arrays.append(values)
    standardized = xr.Dataset(
        {"APCP_surface": (("time", "y", "x"), np.stack(arrays))},
        coords={"time": expected, "x": x, "y": y},
        attrs={
            "source": f"NOAA MRMS {product}", "source_crs": source_crs,
            "raw_native_modified": "false",
        },
    )
    standardized["APCP_surface"].attrs.update(units="mm", interval="1 hour")
    normalized_path = raw_dir / "mrms_hourly_domain_subset.nc"
    standardized.to_netcdf(normalized_path)
    standardized.close()
    files["netcdf"] = normalized_path
    return files, {
        "product": product, "region": region,
        "native_resolution_degrees": 0.01,
        "time_records": len(expected), "raw_native_modified": False,
        "negative_provider_flags_mapped_to_missing": True,
    }


def download_era5_land_hourly(
    boundary: gpd.GeoDataFrame,
    options: dict[str, Any],
    raw_dir: str | Path,
    *,
    reuse_existing: bool = False,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Download ERA5-Land hourly accumulated precipitation and de-accumulate it."""
    try:
        import cdsapi
    except ImportError as error:
        raise ImportError(
            "ERA5-Land requires cdsapi. Install riftprep[era5] and configure ~/.cdsapirc."
        ) from error
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    start = _utc_naive(options["start_utc"])
    end = _utc_naive(options["end_utc"])
    request_start = start.normalize() - pd.Timedelta(days=1)
    request_end = end.normalize()
    request_days = pd.date_range(request_start, request_end, freq="1D")
    xmin, ymin, xmax, ymax = boundary.to_crs("EPSG:4326").total_bounds
    north, west, south, east = float(ymax), float(xmin), float(ymin), float(xmax)
    files: dict[str, Path] = {}
    pieces: list[xr.DataArray] = []
    client = cdsapi.Client()
    for month_start in sorted({value.to_period("M") for value in request_days}):
        days = request_days[request_days.to_period("M") == month_start]
        raw_path = raw_dir / (
            f"era5_land_accumulated_precipitation_{month_start.year:04d}"
            f"{month_start.month:02d}_native.nc"
        )
        request = {
            "variable": ["total_precipitation"],
            "year": f"{month_start.year:04d}",
            "month": f"{month_start.month:02d}",
            "day": [f"{value.day:02d}" for value in days],
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "area": [north, west, south, east],
            "data_format": "netcdf",
            "download_format": "unarchived",
        }
        if not (reuse_existing and raw_path.is_file()):
            try:
                client.retrieve("reanalysis-era5-land", request).download(str(raw_path))
            except Exception as error:
                raise RuntimeError(
                    "ERA5-Land download failed. Confirm the CDS licence is accepted and "
                    "~/.cdsapirc contains valid credentials."
                ) from error
        files[f"provider_netcdf_{month_start.year:04d}{month_start.month:02d}"] = raw_path
        with xr.open_dataset(raw_path) as dataset:
            variable = "tp" if "tp" in dataset else "total_precipitation"
            if variable not in dataset:
                raise KeyError("ERA5-Land response lacks total precipitation.")
            piece = dataset[variable]
            time_name = "valid_time" if "valid_time" in piece.coords else "time"
            if time_name not in piece.coords:
                raise ValueError("ERA5-Land response has no time/valid_time coordinate.")
            if time_name != "time":
                piece = piece.rename({time_name: "time"})
                time_name = "time"
            latitude_name = "latitude" if "latitude" in piece.coords else "lat"
            longitude_name = "longitude" if "longitude" in piece.coords else "lon"
            retained_dimensions = {time_name, latitude_name, longitude_name}
            for dimension in set(piece.dims) - retained_dimensions:
                if piece.sizes[dimension] != 1:
                    raise ValueError(
                        f"Unsupported ERA5-Land dimension {dimension!r} with size "
                        f"{piece.sizes[dimension]}."
                    )
                piece = piece.isel({dimension: 0}, drop=True)
            piece = piece.rename(
                {latitude_name: "latitude", longitude_name: "longitude"}
            ).load()
            pieces.append(piece)
    data = xr.concat(pieces, dim="time").sortby("time")
    _, unique_indices = np.unique(data.time.values, return_index=True)
    data = data.isel(time=np.sort(unique_indices))
    times = pd.to_datetime(data.time.values)
    values_m = np.asarray(data, dtype=np.float64)
    increments_m = np.full_like(values_m, np.nan)
    for index, stamp in enumerate(times):
        if pd.Timestamp(stamp).hour == 1:
            increments_m[index] = values_m[index]
        elif index > 0:
            increments_m[index] = values_m[index] - values_m[index - 1]
    tiny_negative = (increments_m < 0) & (increments_m >= -1.0e-9)
    increments_m[tiny_negative] = 0.0
    if np.any(increments_m < -1.0e-9):
        raise ValueError("ERA5-Land de-accumulation produced material negative precipitation.")
    expected = pd.date_range(start + pd.Timedelta(hours=1), end, freq="1h")
    lookup = {pd.Timestamp(value): index for index, value in enumerate(times)}
    missing = [value for value in expected if value not in lookup]
    if missing:
        raise ValueError(f"ERA5-Land response lacks expected times: {missing[:8]}")
    selected = np.stack([increments_m[lookup[value]] for value in expected]) * 1000.0
    standardized = xr.Dataset(
        {"APCP_surface": (("time", "latitude", "longitude"), selected.astype(np.float32))},
        coords={
            "time": expected,
            "latitude": data.latitude.values,
            "longitude": data.longitude.values,
        },
        attrs={
            "source": "Copernicus CDS ERA5-Land",
            "temporal_conversion": "official cumulative fields de-accumulated to hourly totals",
            "raw_native_modified": "false",
        },
    )
    standardized["APCP_surface"].attrs.update(units="mm", interval="1 hour")
    normalized_path = raw_dir / "era5_land_hourly_adapter_output.nc"
    standardized.to_netcdf(normalized_path)
    standardized.close()
    data.close()
    files["netcdf"] = normalized_path
    return files, {
        "product": "ERA5-Land hourly data",
        "native_resolution_degrees": 0.1,
        "time_records": len(expected),
        "deaccumulated": True,
        "raw_native_modified": False,
        "requires_cds_credentials": True,
    }


def _write_like(source_path: Path, destination: Path, values: np.ndarray) -> Path:
    with rasterio.open(source_path) as source:
        profile = source.profile.copy()
    profile.update(dtype="float32", nodata=-9999.0, compress="deflate")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_values = np.where(np.isfinite(values), values, -9999.0).astype(np.float32)
    with rasterio.open(destination, "w", **profile) as output:
        output.write(output_values, 1)
    return destination


def download_soilgrids_hydraulic(
    boundary: gpd.GeoDataFrame,
    options: dict[str, Any],
    raw_dir: str | Path,
    *,
    reuse_existing: bool = False,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Download SoilGrids properties and derive documented RIFT hydraulic rasters."""
    raw_dir = Path(raw_dir)
    provider_dir = raw_dir / "provider_wcs"
    derived_dir = raw_dir / "hydraulic_adapter"
    provider_dir.mkdir(parents=True, exist_ok=True)
    depth = str(options.get("depth", "0-5cm"))
    quantile = str(options.get("quantile", "Q0.5"))
    max_attempts = int(options.get("max_attempts", 5))
    # SoilGrids labels its Interrupted Goode Homolosine grid EPSG:152160, but
    # many PROJ databases do not register that service-specific identifier.
    soilgrids_crs = CRS.from_proj4(
        "+proj=igh +datum=WGS84 +units=m +no_defs +type=crs"
    )
    query = boundary.to_crs(soilgrids_crs)
    xmin, ymin, xmax, ymax = query.total_bounds
    raw_properties: dict[str, Path] = {}
    for property_name in ("sand", "clay", "bdod", "wv1500"):
        destination = provider_dir / f"soilgrids_{property_name}_{depth}_{quantile}.tif"
        params = [
            ("map", f"/map/{property_name}.map"),
            ("SERVICE", "WCS"), ("VERSION", "2.0.1"),
            ("REQUEST", "GetCoverage"),
            ("COVERAGEID", f"{property_name}_{depth}_{quantile}"),
            ("FORMAT", "GEOTIFF_INT16"),
            ("SUBSET", f"X({xmin:.3f},{xmax:.3f})"),
            ("SUBSET", f"Y({ymin:.3f},{ymax:.3f})"),
            ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/152160"),
            ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/152160"),
        ]
        if not (reuse_existing and destination.is_file()):
            _download(
                "https://maps.isric.org/mapserv", destination, params=params,
                max_attempts=max_attempts,
            )
        raw_properties[property_name] = destination
    arrays: dict[str, np.ndarray] = {}
    reference_grid = None
    for name, path in raw_properties.items():
        with rasterio.open(path) as source:
            values = source.read(1, masked=True).astype(np.float64).filled(np.nan)
            current_grid = (values.shape, source.transform, source.crs)
        if reference_grid is None:
            reference_grid = current_grid
        elif current_grid != reference_grid:
            raise ValueError("SoilGrids WCS layers do not share one grid.")
        arrays[name] = values
    sand_pct = arrays["sand"] / 10.0
    clay_pct = arrays["clay"] / 10.0
    silt_pct = np.clip(100.0 - sand_pct - clay_pct, 0.0, 100.0)
    bulk_density_g_cm3 = arrays["bdod"] / 100.0
    porosity = np.clip(1.0 - bulk_density_g_cm3 / 2.65, 0.05, 0.80)
    residual = np.clip(arrays["wv1500"] / 1000.0, 0.0, porosity - 1.0e-4)
    log10_ksat_cm_hr = -0.60 + 0.0126 * sand_pct - 0.0064 * clay_pct
    ksat_m_s = np.power(10.0, log10_ksat_cm_hr) * 0.01 / 3600.0
    permeability = ksat_m_s / (1000.0 * 9.80665 / 1.0e-3)
    air_entry_cm = np.power(
        10.0, 1.54 - 0.0095 * sand_pct + 0.0063 * silt_pct
    )
    alpha = 1.0 / np.maximum(air_entry_cm / 100.0 * 1000.0 * 9.80665, 1.0e-12)
    valid = np.logical_and.reduce([
        np.isfinite(arrays[name]) for name in ("sand", "clay", "bdod", "wv1500")
    ])
    derived = {
        "permeability": np.where(valid, permeability, np.nan),
        "porosity": np.where(valid, porosity, np.nan),
        "residual": np.where(valid, residual, np.nan),
        "alpha": np.where(valid, alpha, np.nan),
    }
    files: dict[str, Path] = {
        f"provider_{name}": path for name, path in raw_properties.items()
    }
    reference = raw_properties["sand"]
    for name, values in derived.items():
        files[name] = _write_like(
            reference, derived_dir / f"soilgrids_derived_{name}.tif", values
        )
    return files, {
        "product": "SoilGrids 2.0",
        "native_resolution_m": 250,
        "depth": depth,
        "quantile": quantile,
        "raw_native_modified": False,
        "hydraulic_conversion": (
            "Porosity from bulk density and 2.65 g/cm3 particle density; residual water "
            "content from wv1500; saturated conductivity and air-entry suction from the "
            "documented Cosby texture pedotransfer equations; intrinsic permeability and "
            "van Genuchten alpha derived in SI units."
        ),
    }
