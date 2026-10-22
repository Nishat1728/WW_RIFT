"""Raw-first source managers patterned after Watershed Workflow managers.

Provider/source-grid snapshots are retained in ``raw/``. Optional adapters may
also return reproducible normalized products in ``processed/``. No final RIFT
file is written here; source resolution and model resolution stay separate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable
from urllib.parse import urljoin

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import requests
import xarray as xr
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.merge import merge as merge_rasters
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rasterio.transform import from_origin
from shapely.geometry import box

from .artifacts import ProvenanceLedger
from .config import ProjectPaths
from .providers import get_huc_boundary, get_usgs_3dhp_flowlines, load_vector
from .source_adapters import (
    download_copernicus_glo30,
    download_daymet_hourly,
    download_era5_land_hourly,
    download_mrms_hourly,
    download_soilgrids_hydraulic,
)
from .spec import CaseSpec, ProductSpec


def _ssurgo_request_details(args, kwargs) -> dict[str, Any]:
    """Describe one SDM request without retaining credentials or cookies."""
    endpoint = str(args[0] if args else kwargs.get("url", ""))
    data = kwargs.get("data") or {}
    query = str(data.get("QUERY", "")) if isinstance(data, dict) else ""
    geometry_match = re.search(
        r"STGeomFromText\('(.+?)',\s*4326\)", query, flags=re.DOTALL
    )
    return {
        "endpoint": endpoint,
        "method": "POST",
        "format": data.get("FORMAT") if isinstance(data, dict) else None,
        "timeout_seconds": kwargs.get("timeout"),
        "query_bytes": len(query.encode("utf-8")),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "geometry_wkt_epsg4326": (
            geometry_match.group(1) if geometry_match else None
        ),
        "query": query,
    }


@contextmanager
def _requests_post_timeout(
    seconds: float, diagnostics_directory: Path | None = None
):
    """Bound third-party POST calls and preserve malformed SSURGO responses.

    Watershed Workflow's NRCS adapter calls ``requests.post()`` directly
    without a timeout. Soil tiles are downloaded concurrently, so patch the
    shared function once around the whole tile pool instead of racing a patch
    in each worker. When a diagnostics directory is supplied, the untouched
    provider body is saved immediately before Watershed Workflow decodes JSON.
    Valid responses containing ``Table`` are not retained, limiting storage.
    """
    original_post = requests.post
    diagnostics_directory = (
        Path(diagnostics_directory) if diagnostics_directory is not None else None
    )
    sequence_lock = Lock()
    sequence = 0

    def reserve_stem() -> str:
        nonlocal sequence
        with sequence_lock:
            sequence += 1
            return f"ssurgo_response_{sequence:06d}"

    def save_metadata(
        stem: str,
        details: dict[str, Any],
        *,
        response: requests.Response | None = None,
        raw_path: Path | None = None,
        elapsed_seconds: float,
        anomaly: str,
        error: Exception | None = None,
    ) -> Path | None:
        if diagnostics_directory is None:
            return None
        diagnostics_directory.mkdir(parents=True, exist_ok=True)
        query = details.pop("query", "")
        query_path = diagnostics_directory / f"{stem}_request.sql"
        query_path.write_text(query, encoding="utf-8")
        record: dict[str, Any] = {
            "schema": "riftprep-ssurgo-provider-response-v1",
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            **details,
            "request_query_path": str(query_path.resolve()),
            "elapsed_seconds": elapsed_seconds,
            "anomaly": anomaly,
            "raw_body_saved_before_json_decode": raw_path is not None,
            "raw_body_path": str(raw_path.resolve()) if raw_path else None,
        }
        if response is not None:
            raw = response.content
            record.update(
                {
                    "http_status": response.status_code,
                    "reason": response.reason,
                    "response_url": response.url,
                    "content_type": response.headers.get("Content-Type", ""),
                    "response_bytes": len(raw),
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                    "body_preview_utf8": raw[:1000].decode(
                        "utf-8", errors="replace"
                    ),
                    "response_headers": {
                        key.lower(): value
                        for key, value in response.headers.items()
                        if key.lower()
                        in {
                            "content-type", "content-length", "date", "server",
                            "retry-after", "x-powered-by", "x-aspnet-version",
                            "x-aspnetmvc-version",
                        }
                    },
                }
            )
        if error is not None:
            record.update(
                {"error_type": type(error).__name__, "error": str(error)}
            )
        metadata_path = diagnostics_directory / f"{stem}_metadata.json"
        metadata_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return metadata_path

    def post_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        details = _ssurgo_request_details(args, kwargs)
        endpoint = details["endpoint"].lower()
        observe = (
            diagnostics_directory is not None
            and "sdmdataaccess" in endpoint
            and "/tabular/post.rest" in endpoint
        )
        started = time.perf_counter()
        try:
            response = original_post(*args, **kwargs)
        except requests.RequestException as error:
            if observe:
                save_metadata(
                    reserve_stem(), details, elapsed_seconds=time.perf_counter() - started,
                    anomaly="transport_error", error=error,
                )
            raise
        elapsed = time.perf_counter() - started
        if not observe:
            return response

        stem = reserve_stem()
        if not response.ok:
            raw_path = diagnostics_directory / f"{stem}_body.bin"
            diagnostics_directory.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(response.content)
            save_metadata(
                stem, details, response=response, raw_path=raw_path,
                elapsed_seconds=elapsed, anomaly="http_error",
            )
            return response

        original_json = response.json

        def observed_json(*json_args, **json_kwargs):
            diagnostics_directory.mkdir(parents=True, exist_ok=True)
            pending_path = diagnostics_directory / f"{stem}_body.pending.bin"
            pending_path.write_bytes(response.content)
            try:
                payload = original_json(*json_args, **json_kwargs)
            except (requests.exceptions.JSONDecodeError, json.JSONDecodeError) as error:
                raw_path = diagnostics_directory / f"{stem}_body.bin"
                pending_path.replace(raw_path)
                save_metadata(
                    stem, dict(details), response=response, raw_path=raw_path,
                    elapsed_seconds=elapsed, anomaly="invalid_json", error=error,
                )
                raise
            if not isinstance(payload, dict) or "Table" not in payload:
                raw_path = diagnostics_directory / f"{stem}_body.bin"
                pending_path.replace(raw_path)
                save_metadata(
                    stem, dict(details), response=response, raw_path=raw_path,
                    elapsed_seconds=elapsed, anomaly="missing_table",
                )
            else:
                pending_path.unlink(missing_ok=True)
            return payload

        response.json = observed_json
        return response

    requests.post = post_with_timeout
    try:
        yield
    finally:
        requests.post = original_post


@dataclass
class RawProduct:
    name: str
    provider: str
    files: dict[str, Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawCatalog:
    boundary: gpd.GeoDataFrame
    products: dict[str, RawProduct]

    def require(self, name: str) -> RawProduct:
        try:
            return self.products[name]
        except KeyError as error:
            raise KeyError(f"Raw product {name!r} was not acquired.") from error


def _ww_sources():
    """Import lazily so local-only workflows do not require Watershed Workflow."""
    try:
        from watershed_workflow.sources.manager_3dep import Manager3DEP
        from watershed_workflow.sources.manager_aorc import ManagerAORC
        from watershed_workflow.sources.manager_nlcd import ManagerNLCD
        from watershed_workflow.sources.manager_nrcs import ManagerNRCS
        from watershed_workflow.utils.config import setDataDirectory
    except ImportError as error:
        raise ImportError(
            "This provider needs Watershed Workflow. Install the optional "
            "'watershed-workflow' dependency or select a local provider."
        ) from error
    return Manager3DEP, ManagerAORC, ManagerNLCD, ManagerNRCS, setDataDirectory


def resolve_boundary(spec: CaseSpec, paths: ProjectPaths, ledger: ProvenanceLedger) -> gpd.GeoDataFrame:
    raw_dir = paths.raw / "boundary"
    raw_dir.mkdir(parents=True, exist_ok=True)
    domain = spec.domain
    path = raw_dir / "selected_domain.gpkg"
    provider = {
        "huc": "usgs_wbd",
        "shapefile": "local_vector",
        "rectangle": "user_rectangle",
    }[domain.mode]
    started_utc = datetime.now(timezone.utc).isoformat()
    before = _raw_file_state(raw_dir)
    query_parameters = {
        "mode": domain.mode,
        "huc_code": domain.huc_code,
        "huc_level": domain.huc_level,
        "vector_path": domain.vector_path,
        "vector_layer": domain.vector_layer,
        "bounds": domain.bounds,
        "bounds_crs": domain.bounds_crs,
    }
    try:
        if spec.cache.mode == "reuse" and path.is_file():
            boundary = gpd.read_file(path)
        elif domain.mode == "huc":
            boundary = get_huc_boundary(str(domain.huc_code), domain.huc_level)
        elif domain.mode == "shapefile":
            boundary = load_vector(domain.vector_path, layer=domain.vector_layer)
        else:
            boundary = gpd.GeoDataFrame(
                {"domain_id": [spec.case_name]},
                geometry=[box(*domain.bounds)],
                crs=domain.bounds_crs,
            )
        if not (spec.cache.mode == "reuse" and path.is_file()):
            boundary.to_file(path, driver="GPKG")
        reference = _artifact_spatial_reference(path)
        ledger.register(
            "boundary", provider, path, {"mode": domain.mode},
            spatial_reference=reference,
        )
    except Exception as error:
        ledger.finalize_acquisition(
            product="boundary", provider=provider, files={}, raw_root=paths.raw,
            cache_mode=spec.cache.mode, status="failed",
            started_utc=started_utc,
            completed_utc=datetime.now(timezone.utc).isoformat(),
            query_parameters=query_parameters, provider_metadata={},
            partial_artifacts=_partial_artifact_facts(raw_dir, before),
            error=error,
        )
        raise
    current_boundary_state = (path.stat().st_size, path.stat().st_mtime_ns)
    reused = before.get(str(path.resolve())) == current_boundary_state
    ledger.finalize_acquisition(
        product="boundary", provider=provider, files={"vector": path},
        raw_root=paths.raw, cache_mode=spec.cache.mode,
        status="reused" if spec.cache.mode == "reuse" and reused else "succeeded",
        started_utc=started_utc,
        completed_utc=datetime.now(timezone.utc).isoformat(),
        query_parameters=query_parameters,
        provider_metadata={
            "feature_count": int(len(boundary)),
            "geometry_types": sorted(set(boundary.geometry.geom_type.dropna())),
        },
        spatial_references={"vector": reference},
    )
    return boundary


def _copy_local(
    source: str | Path,
    destination: Path,
    *,
    reuse_existing: bool = False,
) -> Path:
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if reuse_existing and destination.is_file():
        return destination
    shutil.copy2(source, destination)
    return destination


def _raw_file_state(directory: str | Path) -> dict[str, tuple[int, int]]:
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    return {
        str(path.resolve()): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    }


def _partial_artifact_facts(
    directory: str | Path,
    before: dict[str, tuple[int, int]],
) -> tuple[dict[str, Any], ...]:
    """Inventory new or changed raw files left by an unsuccessful request."""
    facts = []
    for path_text, state in _raw_file_state(directory).items():
        if before.get(path_text) == state:
            continue
        path = Path(path_text)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        facts.append({
            "path": path_text,
            "bytes": state[0],
            "sha256": digest.hexdigest(),
        })
    return tuple(sorted(facts, key=lambda item: item["path"]))


def _artifact_spatial_reference(path: str | Path) -> dict[str, Any]:
    """Read only embedded CRS metadata; never infer a CRS from coordinates."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in {".tif", ".tiff", ".img", ".vrt"}:
            with rasterio.open(path) as source:
                crs = source.crs
        elif suffix in {".gpkg", ".shp", ".geojson"}:
            crs = gpd.read_file(path, rows=1).crs
        elif suffix in {".nc", ".nc4", ".cdf"}:
            with xr.open_dataset(path, decode_cf=False) as source:
                value = source.attrs.get("crs") or source.attrs.get("source_crs")
                crs = CRS.from_user_input(value) if value else None
                if crs is None:
                    for variable in source.data_vars.values():
                        mapping = variable.attrs.get("grid_mapping")
                        if mapping and mapping in source:
                            crs = CRS.from_cf(dict(source[mapping].attrs))
                            break
        else:
            return {"status": "not_applicable", "value": None}
    except Exception as error:
        return {
            "status": "unavailable", "value": None,
            "reason": f"embedded CRS could not be read: {type(error).__name__}",
        }
    if crs is None:
        return {
            "status": "unavailable", "value": None,
            "reason": "artifact contains no embedded CRS metadata",
        }
    resolved = CRS.from_user_input(crs)
    return {
        "status": "known",
        "value": resolved.to_string(),
        "wkt": resolved.to_wkt(),
    }


def _query_parameters(
    product: ProductSpec,
    geometry: gpd.GeoDataFrame,
) -> dict[str, Any]:
    return {
        "selected_options": product.options,
        "spatial_context": {
            "bounds": [float(value) for value in geometry.total_bounds],
            "crs": str(geometry.crs) if geometry.crs is not None else None,
        },
    }


def _finalize_product_acquisition(
    *,
    ledger: ProvenanceLedger,
    result: RawProduct | None,
    product_name: str,
    product: ProductSpec,
    geometry: gpd.GeoDataFrame,
    paths: ProjectPaths,
    cache_mode: str,
    started_utc: str,
    before: dict[str, tuple[int, int]],
    error: BaseException | None = None,
) -> None:
    completed_utc = datetime.now(timezone.utc).isoformat()
    files = result.files if result is not None else {}
    raw_root = paths.raw
    raw_files = {
        key: path for key, path in files.items()
        if Path(path).resolve().is_relative_to(raw_root.resolve())
    }
    reused = bool(raw_files) and all(
        before.get(str(Path(path).resolve()))
        == (Path(path).stat().st_size, Path(path).stat().st_mtime_ns)
        for path in raw_files.values()
    )
    status = "failed" if error is not None else (
        "reused" if cache_mode == "reuse" and reused else "succeeded"
    )
    ledger.finalize_acquisition(
        product=product_name,
        provider=product.provider,
        files=files,
        raw_root=raw_root,
        cache_mode=cache_mode,
        status=status,
        started_utc=started_utc,
        completed_utc=completed_utc,
        query_parameters=_query_parameters(product, geometry),
        provider_metadata=result.metadata if result is not None else {},
        spatial_references={
            key: _artifact_spatial_reference(path) for key, path in raw_files.items()
        },
        partial_artifacts=(
            _partial_artifact_facts(raw_root / product_name, before)
            if error is not None else ()
        ),
        error=error,
    )


def _register_raw_files(
    ledger: ProvenanceLedger,
    product: str,
    provider: str,
    files: dict[str, Path],
    raw_root: Path,
    options: dict[str, Any],
    source_url: str | None = None,
    source_urls: dict[str, str] | None = None,
) -> None:
    """Register only acquisition artifacts owned by ``raw/``."""
    raw_root = raw_root.resolve()
    for key, artifact in files.items():
        path = Path(artifact).resolve()
        if path.is_relative_to(raw_root):
            ledger.register(
                product, provider, path, {**options, "artifact": key},
                source_url=(source_urls or {}).get(key, source_url),
            )


def _xy_names(data: xr.DataArray) -> tuple[str, str]:
    candidates = (("x", "y"), ("longitude", "latitude"), ("lon", "lat"))
    for x_name, y_name in candidates:
        if x_name in data.coords and y_name in data.coords:
            return x_name, y_name
    raise ValueError(f"Cannot identify horizontal coordinates in {list(data.coords)}")


def dataset_variable_to_geotiff(
    dataset: xr.Dataset,
    variable: str,
    destination: str | Path,
    fallback_crs: str,
    nodata: float = -9999.0,
    invalid_dimension_constant: float | None = None,
) -> Path:
    """Export one native, regularly gridded source variable without resampling."""
    data = dataset[variable].squeeze(drop=True)
    x_name, y_name = _xy_names(data)
    if data.ndim != 2:
        if invalid_dimension_constant is None:
            raise ValueError(
                f"{variable} must be 2-D after squeeze; got dimensions {data.dims} "
                f"with sizes {dict(data.sizes)}. No automatic correction was made. "
                f"After reviewing the source, either set "
                f"land_cover.options.manual_dimension_constants.{variable} explicitly "
                "or provide user-modified rasters with land_cover.provider='local'."
            )
        constant = float(invalid_dimension_constant)
        if not np.isfinite(constant):
            raise ValueError(f"Manual {variable} dimension constant must be finite.")
        if variable == "cover" and (not constant.is_integer() or not 0 <= constant <= 255):
            raise ValueError("Manual cover dimension constant must be an integer from 0 to 255.")
        if variable in {"impervious", "canopy"} and not 0 <= constant <= 100:
            raise ValueError(
                f"Manual {variable} dimension constant must be between 0 and 100 percent."
            )
        values = np.full(
            (int(data.sizes[y_name]), int(data.sizes[x_name])), constant, dtype=np.float32
        )
    else:
        data = data.transpose(y_name, x_name)
        values = np.asarray(data)
    x = np.asarray(data[x_name], dtype=float)
    y = np.asarray(data[y_name], dtype=float)
    if x.size < 2 or y.size < 2:
        raise ValueError(f"{variable} has insufficient spatial coordinates.")
    dx = float(np.median(np.diff(x)))
    dy = float(np.median(np.diff(y)))
    if dx < 0:
        x, values, dx = x[::-1], values[:, ::-1], -dx
    if dy > 0:
        y, values, dy = y[::-1], values[::-1, :], -dy
    crs = fallback_crs
    try:
        if data.rio.crs is not None:
            crs = data.rio.crs
    except (AttributeError, ImportError):
        pass
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": values.shape[0],
        "width": values.shape[1],
        "count": 1,
        "dtype": str(values.dtype),
        "crs": crs,
        "transform": from_origin(x[0] - dx / 2, y[0] + abs(dy) / 2, dx, abs(dy)),
        "nodata": nodata,
        "compress": "deflate",
        "tiled": values.shape[0] >= 16 and values.shape[1] >= 16,
    }
    with rasterio.open(destination, "w", **profile) as output:
        output.write(np.where(np.isfinite(values), values, nodata), 1)
    return destination


def _save_dataset(dataset: xr.Dataset, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_netcdf(path)
    return path


def _write_dem_mosaic(
    tile_rasters: list[Path], destination: Path, resolution: float
) -> Path:
    """Mosaic converted source-grid tiles into a clearly derived raster."""
    sources = [rasterio.open(path) for path in tile_rasters]
    try:
        mosaic, transform = merge_rasters(
            sources,
            res=(float(resolution), float(resolution)),
            nodata=-9999.0,
            target_aligned_pixels=True,
            resampling=Resampling.bilinear,
        )
        profile = sources[0].profile.copy()
    finally:
        for source in sources:
            source.close()
    values = mosaic[0].astype(np.float32, copy=False)
    profile.update(
        driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype="float32", transform=transform,
        nodata=-9999.0, compress="deflate",
        tiled=values.shape[0] >= 16 and values.shape[1] >= 16,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(destination, "w", **profile) as output:
        output.write(values, 1)
    return destination


def _plot_native_raster(path: Path, destination: Path, title: str, label: str, dpi: int) -> Path:
    with rasterio.open(path) as source:
        factor = max(1, int(max(source.height, source.width) / 2500))
        data = source.read(1, out_shape=(max(1, source.height // factor), max(1, source.width // factor)))
        if source.nodata is not None:
            data = np.where(np.isclose(data, source.nodata), np.nan, data)
        bounds = source.bounds
    fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
    image = ax.imshow(
        data,
        extent=(bounds.left, bounds.right, bounds.bottom, bounds.top),
        origin="upper",
        cmap="terrain" if "elevation" in label.lower() else "viridis",
    )
    ax.set(title=title, xlabel="Source x coordinate", ylabel="Source y coordinate", aspect="equal")
    fig.colorbar(image, ax=ax, label=label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return destination


def fetch_dem(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "dem"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    source_url: str | None = None
    if product.provider == "watershed_workflow_3dep":
        source_url = "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WMSServer"
        resolution = int(product.options.get("resolution_m", 30))
        tile_degrees = float(product.options.get("tile_degrees", 1.0))
        max_attempts = max(1, int(product.options.get("max_attempts", 5)))
        if tile_degrees <= 0:
            raise ValueError("dem.options.tile_degrees must be positive.")
        legacy_nc = raw_dir / f"3dep_{resolution}m_native.nc"
        legacy_raster = raw_dir / f"3dep_{resolution}m_native.tif"
        using_legacy_cache = (
            paths.uses_legacy_input_directory
            and reuse_existing
            and legacy_nc.is_file()
            and legacy_raster.is_file()
        )
        provider_responses: list[dict[str, Any]] = []
        if using_legacy_cache:
            # Preserve validated legacy cases exactly as they are.  These names predate
            # the directory contract and are never created for a new acquisition.
            files.update(netcdf=legacy_nc, raster=legacy_raster)
            with xr.open_dataset(legacy_nc) as dataset:
                variable = next(iter(dataset.data_vars))
            tile_count = int(product.options.get("cached_tile_count", 1))
            acquisition_layout = "legacy_cache_unchanged"
        else:
            Manager3DEP, _, _, _, set_data_directory = _ww_sources()
            set_data_directory(str(paths.provider_cache / "dem"))

            def get_dataset(geometry):
                for attempt in range(1, max_attempts + 1):
                    try:
                        manager = Manager3DEP(resolution)
                        return manager.getDataset(geometry, variables=["DEM"])
                    except Exception as error:
                        retryable = (
                            isinstance(error, (requests.RequestException, TimeoutError))
                            or type(error).__name__ == "ServiceUnavailableError"
                            or "service is currently not available" in str(error).lower()
                        )
                        if not retryable or attempt == max_attempts:
                            raise
                        time.sleep(min(30.0, 3.0 * 2 ** (attempt - 1)))
                raise AssertionError("unreachable")

            query = boundary.to_crs("EPSG:4326")
            xmin, ymin, xmax, ymax = query.total_bounds
            processed_dir = paths.processed / "dem" / "watershed_workflow_3dep"
            processed_dir.mkdir(parents=True, exist_ok=True)
            if max(xmax - xmin, ymax - ymin) <= tile_degrees:
                response_path = raw_dir / f"3dep_{resolution}m_provider_response.nc"
                raster_path = processed_dir / f"3dep_{resolution}m_source_grid.tif"
                if reuse_existing and response_path.is_file():
                    with xr.open_dataset(response_path) as cached:
                        dataset = cached.load()
                else:
                    dataset = get_dataset(query)
                    _save_dataset(dataset, response_path)
                variable = next(iter(dataset.data_vars))
                provider_responses.append({
                    "artifact_key": "provider_response",
                    "path": str(response_path.resolve()),
                    "query_bounds_epsg4326": [
                        float(value) for value in query.total_bounds
                    ],
                    "dimensions": {name: int(size) for name, size in dataset.sizes.items()},
                })
                if not (reuse_existing and raster_path.is_file()):
                    dataset_variable_to_geotiff(
                        dataset, variable, raster_path, "EPSG:5070"
                    )
                dataset.close()
                files.update(provider_response=response_path, raster=raster_path)
                tile_count = 1
                acquisition_layout = "single_provider_response"
            else:
                domain = query.geometry.union_all()
                raw_tile_dir = raw_dir / "provider_responses"
                processed_tile_dir = processed_dir / "source_tiles"
                raw_tile_dir.mkdir(parents=True, exist_ok=True)
                processed_tile_dir.mkdir(parents=True, exist_ok=True)
                tile_rasters: list[Path] = []
                tile_count = 0
                for x0 in np.arange(xmin, xmax, tile_degrees):
                    for y0 in np.arange(ymin, ymax, tile_degrees):
                        clipped = domain.intersection(
                            box(
                                float(x0), float(y0),
                                float(min(x0 + tile_degrees, xmax)),
                                float(min(y0 + tile_degrees, ymax)),
                            )
                        )
                        if clipped.is_empty:
                            continue
                        tile_count += 1
                        tile = gpd.GeoDataFrame(
                            {"tile": [tile_count]}, geometry=[clipped], crs="EPSG:4326"
                        )
                        response_path = (
                            raw_tile_dir
                            / f"3dep_{resolution}m_provider_response_{tile_count:04d}.nc"
                        )
                        tile_raster = (
                            processed_tile_dir
                            / f"3dep_{resolution}m_source_grid_{tile_count:04d}.tif"
                        )
                        if reuse_existing and response_path.is_file():
                            with xr.open_dataset(response_path) as cached:
                                dataset = cached.load()
                        else:
                            dataset = get_dataset(tile)
                            _save_dataset(dataset, response_path)
                        variable = next(iter(dataset.data_vars))
                        provider_responses.append({
                            "artifact_key": f"provider_response_{tile_count:04d}",
                            "path": str(response_path.resolve()),
                            "query_bounds_epsg4326": [
                                float(value) for value in tile.total_bounds
                            ],
                            "dimensions": {
                                name: int(size) for name, size in dataset.sizes.items()
                            },
                        })
                        if not (reuse_existing and tile_raster.is_file()):
                            dataset_variable_to_geotiff(
                                dataset, variable, tile_raster, "EPSG:5070"
                            )
                        dataset.close()
                        files[f"provider_response_{tile_count:04d}"] = response_path
                        tile_rasters.append(tile_raster)
                if not tile_rasters:
                    raise LookupError("3DEP returned no DEM tiles for the selected domain.")
                raster_path = processed_dir / f"3dep_{resolution}m_source_mosaic.tif"
                if not (reuse_existing and raster_path.is_file()):
                    _write_dem_mosaic(tile_rasters, raster_path, resolution)
                files["raster"] = raster_path
                acquisition_layout = "tiled_provider_responses"
        metadata = {
            "resolution_m": resolution, "variable": variable,
            "tile_degrees": tile_degrees, "tile_count": tile_count,
            "max_attempts": max_attempts,
            "product": f"USGS 3DEP {resolution} m DEM",
            "acquisition_layout": acquisition_layout,
            "raw_artifact_semantics": (
                "Immutable NetCDF snapshot of the dataset returned by the "
                "Watershed Workflow Manager3DEP/py3dep request"
            ),
            "provider_processing_disclosure": (
                "The upstream USGS 3DEP WMS/py3dep request returns the selected "
                "10 m or 30 m source grid in EPSG:5070 and py3dep masks it to the "
                "request geometry before riftprep receives it. riftprep performs no "
                "RIFT-grid alignment before saving the raw response snapshot."
            ),
            "derived_raster": str(Path(files["raster"]).resolve()),
            "raw_product_modified": False,
            "provider_responses": provider_responses,
        }
    elif product.provider == "copernicus_glo30":
        source_url = "https://copernicus-dem-30m.s3.amazonaws.com/"
        files, metadata = download_copernicus_glo30(
            boundary,
            raw_dir,
            processed_dir=paths.processed / "dem" / "copernicus_glo30",
            reuse_existing=reuse_existing,
            max_attempts=int(product.options.get("max_attempts", 5)),
        )
    elif product.provider == "local":
        source = product.options["path"]
        files["raster"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        metadata = {
            "source_path": str(Path(source).expanduser().resolve()),
            "raw_artifact_semantics": "Byte-preserved copy of the user-selected DEM",
            "raw_product_modified": False,
        }
    else:
        raise ValueError(f"Unsupported DEM provider: {product.provider}")
    _register_raw_files(
        ledger, "dem", product.provider, files, paths.raw, product.options,
        source_url=source_url,
        source_urls=metadata.get("source_urls"),
    )
    if plot:
        resolution_text = f" ({metadata['resolution_m']} m product)" if "resolution_m" in metadata else ""
        files["plot"] = _plot_native_raster(
            files["raster"],
            paths.figures / "raw_dem_selected_product.png",
            f"Selected DEM before RIFT-grid alignment{resolution_text}",
            "Elevation (m)",
            dpi,
        )
    return RawProduct("dem", product.provider, files, metadata)


def fetch_land_cover(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "land_cover"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "watershed_workflow_nlcd":
        year = int(product.options.get("year", 2021))
        location = str(product.options.get("location", "L48"))
        variables = list(product.options.get("variables", ["cover", "impervious", "canopy"]))
        manual_dimension_constants = dict(
            product.options.get("manual_dimension_constants", {})
        )
        unknown_manual_variables = set(manual_dimension_constants) - set(variables)
        if unknown_manual_variables:
            raise ValueError(
                "land_cover.options.manual_dimension_constants contains unselected variables: "
                f"{sorted(unknown_manual_variables)}"
            )
        tile_degrees = float(product.options.get("tile_degrees", 1.0))
        tile_overlap_degrees = float(product.options.get("tile_overlap_degrees", 0.02))
        workers = max(1, int(product.options.get("download_workers", 4)))
        max_attempts = max(1, int(product.options.get("max_attempts", 5)))
        if tile_degrees <= 0:
            raise ValueError("land_cover.options.tile_degrees must be positive.")
        if tile_overlap_degrees < 0 or tile_overlap_degrees >= tile_degrees / 2:
            raise ValueError(
                "land_cover.options.tile_overlap_degrees must be nonnegative and "
                "smaller than half tile_degrees."
            )
        nc_path = raw_dir / f"nlcd_{location}_{year}_native.nc"
        raster_paths = {var: raw_dir / f"nlcd_{var}_{year}_native.tif" for var in variables}
        if reuse_existing and nc_path.is_file() and all(path.is_file() for path in raster_paths.values()):
            files = {"netcdf": nc_path, **raster_paths}
            tile_count = int(product.options.get("cached_tile_count", 1))
        else:
            _, _, ManagerNLCD, _, set_data_directory = _ww_sources()
            set_data_directory(str(paths.provider_cache / "land_cover"))
            # pygeohydro/async-retriever uses a shared SQLite HTTP cache.  Concurrent
            # ManagerNLCD calls can contend for that database on large tiled domains
            # and fail with ``sqlite3.OperationalError: database is locked``.  Keep
            # provider access serial while allowing tile serialization/conversion to
            # proceed in worker threads after each download completes.
            provider_download_lock = Lock()

            def get_dataset(geometry):
                for attempt in range(1, max_attempts + 1):
                    try:
                        with provider_download_lock:
                            return ManagerNLCD(location=location, year=year).getDataset(
                                geometry, variables=variables
                            )
                    except Exception as error:
                        message = str(error).lower()
                        retryable = (
                            isinstance(error, (requests.RequestException, TimeoutError, OSError))
                            or type(error).__name__ == "ServiceUnavailableError"
                            or any(marker in message for marker in (
                                "service is currently not available", "temporarily unavailable",
                                "timed out", "timeout", "connection reset", "connection aborted",
                                "database is locked", "502", "503", "504",
                            ))
                        )
                        if not retryable or attempt == max_attempts:
                            raise
                        time.sleep(min(30.0, 3.0 * 2 ** (attempt - 1)))
                raise AssertionError("unreachable")

            query = boundary.to_crs("EPSG:4326")
            xmin, ymin, xmax, ymax = query.total_bounds
            if max(xmax - xmin, ymax - ymin) <= tile_degrees:
                dataset = get_dataset(query)
                files["netcdf"] = _save_dataset(dataset, nc_path)
                for variable in variables:
                    files[variable] = dataset_variable_to_geotiff(
                        dataset, variable, raster_paths[variable], "EPSG:4326",
                        invalid_dimension_constant=manual_dimension_constants.get(variable),
                    )
                dataset.close()
                tile_count = 1
            else:
                domain = query.geometry.union_all()
                tile_queries: list[tuple[int, gpd.GeoDataFrame]] = []
                for x0 in np.arange(xmin, xmax, tile_degrees):
                    for y0 in np.arange(ymin, ymax, tile_degrees):
                        clipped = domain.intersection(
                            box(
                                float(max(xmin, x0 - tile_overlap_degrees)),
                                float(max(ymin, y0 - tile_overlap_degrees)),
                                float(min(x0 + tile_degrees + tile_overlap_degrees, xmax)),
                                float(min(y0 + tile_degrees + tile_overlap_degrees, ymax)),
                            )
                        )
                        if clipped.is_empty:
                            continue
                        index = len(tile_queries) + 1
                        tile_queries.append((
                            index,
                            gpd.GeoDataFrame(
                                {"tile": [index]}, geometry=[clipped], crs="EPSG:4326"
                            ),
                        ))
                tile_count = len(tile_queries)
                tile_dir = raw_dir / "tiles"
                tile_dir.mkdir(parents=True, exist_ok=True)

                def download_tile(item):
                    index, tile = item
                    dataset = get_dataset(tile)
                    tile_files: dict[str, Path] = {}
                    try:
                        tile_nc = tile_dir / f"nlcd_{location}_{year}_tile_{index:04d}.nc"
                        tile_files["netcdf"] = _save_dataset(dataset, tile_nc)
                        for variable in variables:
                            tile_tif = tile_dir / f"nlcd_{variable}_{year}_tile_{index:04d}.tif"
                            tile_files[variable] = dataset_variable_to_geotiff(
                                dataset, variable, tile_tif, "EPSG:4326",
                                invalid_dimension_constant=manual_dimension_constants.get(variable),
                            )
                    finally:
                        dataset.close()
                    return index, tile_files

                tile_results: dict[int, dict[str, Path]] = {}
                with ThreadPoolExecutor(max_workers=min(workers, tile_count)) as executor:
                    futures = [executor.submit(download_tile, item) for item in tile_queries]
                    for future in as_completed(futures):
                        index, tile_files = future.result()
                        tile_results[index] = tile_files

                tile_rasters: dict[str, list[Path]] = {variable: [] for variable in variables}
                for index in sorted(tile_results):
                    for artifact, path in tile_results[index].items():
                        files[f"tile_{index:04d}_{artifact}"] = path
                        if artifact in tile_rasters:
                            tile_rasters[artifact].append(path)

                mosaic_variables: dict[str, tuple[tuple[str, str], np.ndarray]] = {}
                common_transform = None
                common_shape = None
                for variable in variables:
                    sources = [rasterio.open(path) for path in tile_rasters[variable]]
                    try:
                        mosaic, transform = merge_rasters(
                            sources,
                            nodata=-9999.0,
                            target_aligned_pixels=True,
                            resampling=Resampling.nearest,
                        )
                        profile = sources[0].profile.copy()
                    finally:
                        for source in sources:
                            source.close()
                    values = mosaic[0]
                    if common_transform is None:
                        common_transform = transform
                        common_shape = values.shape
                    elif transform != common_transform or values.shape != common_shape:
                        raise RuntimeError("NLCD tile mosaics do not share one native grid.")
                    profile.update(
                        driver="GTiff", height=values.shape[0], width=values.shape[1],
                        count=1, dtype=str(values.dtype), transform=transform,
                        nodata=-9999.0, compress="deflate",
                        tiled=values.shape[0] >= 16 and values.shape[1] >= 16,
                    )
                    with rasterio.open(raster_paths[variable], "w", **profile) as output:
                        output.write(values, 1)
                    files[variable] = raster_paths[variable]
                    mosaic_variables[variable] = (("y", "x"), values)
                    del mosaic

                assert common_transform is not None and common_shape is not None
                x = common_transform.c + (np.arange(common_shape[1]) + 0.5) * common_transform.a
                y = common_transform.f + (np.arange(common_shape[0]) + 0.5) * common_transform.e
                mosaic_dataset = xr.Dataset(
                    mosaic_variables,
                    coords={"x": x, "y": y},
                    attrs={
                        "product": f"NLCD {year} {location}",
                        "source": "pygeohydro MRLC tiled native-product mosaic",
                    },
                )
                files["netcdf"] = _save_dataset(mosaic_dataset, nc_path)
                mosaic_dataset.close()
        metadata = {
            "year": year, "location": location, "variables": variables,
            "tile_degrees": tile_degrees,
            "tile_overlap_degrees": tile_overlap_degrees,
            "tile_count": tile_count,
            "download_workers": workers, "max_attempts": max_attempts,
            "manual_dimension_constants": manual_dimension_constants,
        }
    elif product.provider == "local":
        for variable, source in product.options["rasters"].items():
            files[variable] = _copy_local(
                source, raw_dir / Path(source).name,
                reuse_existing=reuse_existing,
            )
        metadata = {"variables": sorted(product.options["rasters"])}
    else:
        raise ValueError(f"Unsupported land-cover provider: {product.provider}")
    for key, path in files.items():
        ledger.register("land_cover", product.provider, path, {**product.options, "artifact": key})
    if plot and "cover" in files:
        files["plot"] = _plot_native_raster(
            files["cover"], paths.figures / "raw_land_cover_selected_product.png",
            "Selected raw land-cover product", "Land-cover class", dpi,
        )
    return RawProduct("land_cover", product.provider, files, metadata)


def fetch_soil(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "soil"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "watershed_workflow_ssurgo":
        path = raw_dir / "ssurgo_native.gpkg"
        tile_degrees = float(product.options.get("tile_degrees", 0.25))
        workers = max(1, int(product.options.get("download_workers", 4)))
        max_attempts = max(1, int(product.options.get("max_attempts", 5)))
        request_timeout_seconds = float(
            product.options.get("request_timeout_seconds", 120.0)
        )
        max_subdivision_depth = max(
            0, int(product.options.get("max_subdivision_depth", 2))
        )
        if tile_degrees <= 0:
            raise ValueError("soil.options.tile_degrees must be positive.")
        if request_timeout_seconds <= 0:
            raise ValueError("soil.options.request_timeout_seconds must be positive.")
        tile_count = 1
        tiles_with_soil = 1
        provider_response_diagnostic_count = 0
        terminal_empty_queries: list[list[float]] = []
        if reuse_existing and path.is_file():
            soil = gpd.read_file(path)
            tile_count = int(product.options.get("cached_tile_count", 1))
            tiles_with_soil = int(product.options.get("cached_tiles_with_soil", tile_count))
        else:
            _, _, _, ManagerNRCS, set_data_directory = _ww_sources()
            set_data_directory(str(paths.provider_cache / "soil"))
            query = boundary.to_crs("EPSG:4326")
            xmin, ymin, xmax, ymax = query.total_bounds

            def download_tile(
                tile: gpd.GeoDataFrame, subdivision_depth: int = 0
            ) -> gpd.GeoDataFrame:
                last_error: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        manager = ManagerNRCS(force_download=not reuse_existing)
                        return manager.getShapesByGeometry(tile)
                    except Exception as error:
                        last_error = error
                        missing_table = (
                            isinstance(error, KeyError) and error.args == ("Table",)
                        )
                        retryable = (
                            isinstance(error, (requests.RequestException, TimeoutError))
                            or missing_table
                            or type(error).__name__ == "ServiceUnavailableError"
                            or "service is currently not available" in str(error).lower()
                        )
                        if not retryable:
                            raise
                        # A missing Table commonly means this exact query is too
                        # complex (or contains no mapped soil). Two retries are
                        # enough before querying four exact sub-regions.
                        if missing_table and attempt >= min(2, max_attempts):
                            break
                        if attempt == max_attempts:
                            break
                        time.sleep(min(30.0, 3.0 * 2 ** (attempt - 1)))

                if subdivision_depth < max_subdivision_depth:
                    geometry = tile.geometry.union_all()
                    left, bottom, right, top = geometry.bounds
                    xmid = (left + right) / 2.0
                    ymid = (bottom + top) / 2.0
                    children = []
                    for child_box in (
                        box(left, bottom, xmid, ymid),
                        box(xmid, bottom, right, ymid),
                        box(left, ymid, xmid, top),
                        box(xmid, ymid, right, top),
                    ):
                        clipped = geometry.intersection(child_box)
                        if not clipped.is_empty:
                            children.append(
                                gpd.GeoDataFrame(
                                    {"subdivision_depth": [subdivision_depth + 1]},
                                    geometry=[clipped], crs=tile.crs,
                                )
                            )
                    child_results = [
                        download_tile(child, subdivision_depth + 1)
                        for child in children
                    ]
                    nonempty = [piece for piece in child_results if not piece.empty]
                    if nonempty:
                        combined = gpd.GeoDataFrame(
                            pd.concat(nonempty, ignore_index=True), crs=nonempty[0].crs
                        )
                        if "mukey" in combined.columns:
                            combined = combined.dissolve(
                                by="mukey", as_index=False, aggfunc="first"
                            )
                        return combined
                    return gpd.GeoDataFrame(
                        {"mukey": pd.Series(dtype="int64")},
                        geometry=gpd.GeoSeries([], crs=tile.crs),
                        crs=tile.crs,
                    )

                # Only a repeatedly missing JSON Table at the smallest query
                # scale is interpreted as a documented no-data response.
                if isinstance(last_error, KeyError) and last_error.args == ("Table",):
                    terminal_empty_queries.append(
                        [float(value) for value in tile.total_bounds]
                    )
                    return gpd.GeoDataFrame(
                        {"mukey": pd.Series(dtype="int64")},
                        geometry=gpd.GeoSeries([], crs=tile.crs),
                        crs=tile.crs,
                    )
                assert last_error is not None
                if isinstance(
                    last_error,
                    (requests.exceptions.JSONDecodeError, json.JSONDecodeError),
                ):
                    diagnostic_records = sorted(
                        (raw_dir / "provider_response_diagnostics").glob(
                            "*_metadata.json"
                        ),
                        key=lambda item: item.stat().st_mtime,
                    )
                    latest = diagnostic_records[-1] if diagnostic_records else None
                    raise RuntimeError(
                        "SSURGO returned malformed JSON after all configured retries "
                        f"for EPSG:4326 bounds {tile.total_bounds.tolist()}. "
                        "The response was not interpreted as no-data and no fallback "
                        "soil values were substituted. "
                        + (
                            f"Inspect the preserved provider response at {latest}."
                            if latest is not None
                            else "No provider-response diagnostic was available."
                        )
                    ) from last_error
                raise last_error

            provider_diagnostics = raw_dir / "provider_response_diagnostics"
            with _requests_post_timeout(
                request_timeout_seconds, provider_diagnostics
            ):
                if max(xmax - xmin, ymax - ymin) <= tile_degrees:
                    soil = download_tile(query)
                else:
                    domain = query.geometry.union_all()
                    tile_queries: list[tuple[int, gpd.GeoDataFrame]] = []
                    x_edges = np.arange(xmin, xmax, tile_degrees)
                    y_edges = np.arange(ymin, ymax, tile_degrees)
                    for x0 in x_edges:
                        for y0 in y_edges:
                            clipped = domain.intersection(
                                box(
                                    float(x0), float(y0),
                                    float(min(x0 + tile_degrees, xmax)),
                                    float(min(y0 + tile_degrees, ymax)),
                                )
                            )
                            if clipped.is_empty:
                                continue
                            index = len(tile_queries) + 1
                            tile = gpd.GeoDataFrame(
                                {"tile": [index]}, geometry=[clipped], crs="EPSG:4326"
                            )
                            tile_queries.append((index, tile))
                    tile_count = len(tile_queries)
                    tile_results: dict[int, gpd.GeoDataFrame] = {}
                    with ThreadPoolExecutor(max_workers=min(workers, tile_count)) as executor:
                        futures = {
                            executor.submit(download_tile, tile): index
                            for index, tile in tile_queries
                        }
                        for future in as_completed(futures):
                            tile_results[futures[future]] = future.result()

            if max(xmax - xmin, ymax - ymin) > tile_degrees:
                tile_dir = raw_dir / "tiles"
                tile_dir.mkdir(parents=True, exist_ok=True)
                pieces = []
                for index in sorted(tile_results):
                    piece = tile_results[index]
                    if not piece.empty:
                        tile_path = tile_dir / f"ssurgo_tile_{index:04d}.gpkg"
                        piece.to_file(tile_path, driver="GPKG")
                        files[f"tile_{index:04d}"] = tile_path
                        pieces.append(piece)
                if not pieces:
                    raise LookupError("SSURGO returned no soil polygons for any domain tile.")
                tiles_with_soil = len(pieces)
                soil = gpd.GeoDataFrame(
                    pd.concat(pieces, ignore_index=True), crs=pieces[0].crs
                )
                if "mukey" in soil.columns:
                    soil = soil.dissolve(by="mukey", as_index=False, aggfunc="first")
            if terminal_empty_queries:
                empty_response_path = raw_dir / "ssurgo_terminal_empty_queries.json"
                empty_response_path.write_text(
                    json.dumps(
                        {
                            "description": (
                                "Bounds of smallest exact subqueries that returned no "
                                "JSON Table after retries; retained as transparent no-data."
                            ),
                            "bounds_epsg4326": terminal_empty_queries,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                files["terminal_empty_queries"] = empty_response_path
            diagnostic_metadata = sorted(provider_diagnostics.glob("*_metadata.json"))
            provider_response_diagnostic_count = len(diagnostic_metadata)
            if diagnostic_metadata:
                diagnostic_index = raw_dir / "ssurgo_provider_response_diagnostics.json"
                diagnostic_index.write_text(
                    json.dumps(
                        {
                            "description": (
                                "Malformed, failed, or Table-free SSURGO provider "
                                "responses. Raw bodies were preserved before JSON parsing."
                            ),
                            "count": len(diagnostic_metadata),
                            "records": [str(item.resolve()) for item in diagnostic_metadata],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                files["provider_response_diagnostics"] = diagnostic_index
            soil.to_file(path, driver="GPKG")
        files["vector"] = path
        metadata = {
            "features": int(len(soil)), "columns": list(soil.columns),
            "tile_degrees": tile_degrees, "tile_count": tile_count,
            "tiles_with_soil": tiles_with_soil, "download_workers": workers,
            "max_attempts": max_attempts,
            "request_timeout_seconds": request_timeout_seconds,
            "max_subdivision_depth": max_subdivision_depth,
            "terminal_empty_query_count": len(terminal_empty_queries),
            "provider_response_diagnostic_count": (
                provider_response_diagnostic_count
            ),
        }
    elif product.provider == "soilgrids":
        files, metadata = download_soilgrids_hydraulic(
            boundary, product.options, raw_dir,
            processed_dir=paths.processed / "soil" / "soilgrids",
            reuse_existing=reuse_existing,
        )
    elif product.provider == "local":
        source = product.options["path"]
        files["vector"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        metadata = {"source_path": str(Path(source).expanduser().resolve())}
    else:
        raise ValueError(f"Unsupported soil provider: {product.provider}")
    _register_raw_files(
        ledger, "soil", product.provider, files, paths.raw, product.options
    )
    if plot:
        if product.provider == "soilgrids":
            files["plot"] = _plot_native_raster(
                files["porosity"],
                paths.figures / "raw_soil_selected_product.png",
                "Selected SoilGrids-derived hydraulic product: porosity",
                "Porosity (-)", dpi,
            )
        else:
            soil = gpd.read_file(files["vector"])
            field_name = str(product.options.get("plot_field", "porosity [-]"))
            fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
            if field_name in soil:
                soil.plot(column=field_name, ax=ax, legend=True, cmap="YlGnBu", missing_kwds={"color": "0.85"})
            else:
                soil.plot(ax=ax, color="0.75", edgecolor="0.25", linewidth=0.2)
            ax.set(title=f"Selected raw soil product: {field_name}", aspect="equal")
            plot_path = paths.figures / "raw_soil_selected_product.png"
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            files["plot"] = plot_path
    return RawProduct("soil", product.provider, files, metadata)


def fetch_rainfall(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "rainfall"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "watershed_workflow_aorc":
        variables = list(product.options.get("variables", ["APCP_surface"]))
        max_attempts = max(1, int(product.options.get("max_attempts", 5)))
        nc_path = raw_dir / "aorc_native_hourly.nc"
        if reuse_existing and nc_path.is_file():
            dataset = xr.open_dataset(nc_path)
            files["netcdf"] = nc_path
        else:
            _, ManagerAORC, _, _, set_data_directory = _ww_sources()
            set_data_directory(str(paths.provider_cache / "rainfall"))
            # ManagerAORC accepts date-only bounds and slices at midnight.  Ask
            # for complete provider days, then perform the exact hourly slice
            # only after the untouched provider response has been saved.
            start_stamp = pd.Timestamp(product.options["start_utc"])
            end_stamp = pd.Timestamp(product.options["end_utc"])
            request_start = start_stamp.strftime("%Y-%m-%d")
            request_end_stamp = end_stamp.normalize()
            if end_stamp != request_end_stamp:
                request_end_stamp += pd.Timedelta(days=1)
            request_end = request_end_stamp.strftime("%Y-%m-%d")
            for attempt in range(1, max_attempts + 1):
                try:
                    dataset = ManagerAORC().getDataset(
                        boundary,
                        start=request_start,
                        end=request_end,
                        variables=variables,
                    )
                    break
                except Exception as error:
                    retryable = (
                        isinstance(error, (requests.RequestException, TimeoutError, OSError))
                        or type(error).__name__ == "ServiceUnavailableError"
                        or "service is currently not available" in str(error).lower()
                    )
                    if not retryable or attempt == max_attempts:
                        raise
                    time.sleep(min(30.0, 3.0 * 2 ** (attempt - 1)))
            else:
                raise AssertionError("unreachable")
            files["netcdf"] = _save_dataset(dataset, nc_path)
        metadata = {
            "variables": variables,
            "start_utc": product.options["start_utc"],
            "end_utc": product.options["end_utc"],
            "time_records": int(dataset.sizes.get("time", 0)),
            "max_attempts": max_attempts,
        }
        dataset.close()
    elif product.provider == "daymet":
        files, metadata = download_daymet_hourly(
            boundary, product.options, raw_dir,
            processed_dir=paths.processed / "rainfall" / "daymet",
            reuse_existing=reuse_existing,
        )
    elif product.provider == "mrms":
        files, metadata = download_mrms_hourly(
            boundary, product.options, raw_dir,
            processed_dir=paths.processed / "rainfall" / "mrms",
            reuse_existing=reuse_existing,
        )
    elif product.provider == "era5_land":
        files, metadata = download_era5_land_hourly(
            boundary, product.options, raw_dir,
            processed_dir=paths.processed / "rainfall" / "era5_land",
            reuse_existing=reuse_existing,
        )
    elif product.provider == "local":
        source = product.options["path"]
        files["netcdf"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        metadata = {"source_path": str(Path(source).expanduser().resolve())}
    else:
        raise ValueError(f"Unsupported rainfall provider: {product.provider}")
    _register_raw_files(
        ledger, "rainfall", product.provider, files, paths.raw, product.options
    )
    if plot:
        with xr.open_dataset(files["netcdf"]) as source:
            variable = str(product.options.get("precipitation_variable", "APCP_surface"))
            start = pd.Timestamp(product.options["start_utc"]).tz_convert(None)
            end = pd.Timestamp(product.options["end_utc"]).tz_convert(None)
            data = source[variable].sel(time=slice(start + pd.Timedelta(hours=1), end))
            accumulation = data.sum("time") if "time" in data.dims else data
            x_name, y_name = _xy_names(accumulation)
            fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
            image = ax.pcolormesh(
                np.asarray(accumulation[x_name]), np.asarray(accumulation[y_name]),
                np.asarray(accumulation), shading="auto", cmap="Blues",
            )
            ax.set(title="Selected raw rainfall product: event accumulation", aspect="equal")
            fig.colorbar(image, ax=ax, label="Accumulation (mm)")
            plot_path = paths.figures / "raw_rainfall_selected_product.png"
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            files["plot"] = plot_path
    return RawProduct("rainfall", product.provider, files, metadata)


def fetch_hydrography(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "hydrography"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "usgs_3dhp":
        path = raw_dir / "usgs_3dhp_flowlines.gpkg"
        tile_degrees = float(product.options.get("tile_degrees", 1.0))
        workers = max(1, int(product.options.get("download_workers", 4)))
        if tile_degrees <= 0:
            raise ValueError("hydrography.options.tile_degrees must be positive.")
        if reuse_existing and path.is_file():
            rivers = gpd.read_file(path)
            tile_count = int(product.options.get("cached_tile_count", 1))
        else:
            query = boundary.to_crs("EPSG:4326")
            xmin, ymin, xmax, ymax = query.total_bounds
            if max(xmax - xmin, ymax - ymin) <= tile_degrees:
                rivers = get_usgs_3dhp_flowlines(query)
                tile_count = 1
            else:
                domain = query.geometry.union_all()
                tile_queries: list[tuple[int, gpd.GeoDataFrame]] = []
                for x0 in np.arange(xmin, xmax, tile_degrees):
                    for y0 in np.arange(ymin, ymax, tile_degrees):
                        clipped = domain.intersection(
                            box(
                                float(x0), float(y0),
                                float(min(x0 + tile_degrees, xmax)),
                                float(min(y0 + tile_degrees, ymax)),
                            )
                        )
                        if clipped.is_empty:
                            continue
                        index = len(tile_queries) + 1
                        tile_queries.append((
                            index,
                            gpd.GeoDataFrame(
                                {"tile": [index]}, geometry=[clipped], crs="EPSG:4326"
                            ),
                        ))
                tile_count = len(tile_queries)
                tile_results: dict[int, gpd.GeoDataFrame] = {}

                def download_tile(item):
                    index, tile = item
                    try:
                        return index, get_usgs_3dhp_flowlines(tile)
                    except LookupError:
                        return index, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

                with ThreadPoolExecutor(max_workers=min(workers, tile_count)) as executor:
                    futures = [executor.submit(download_tile, item) for item in tile_queries]
                    for future in as_completed(futures):
                        index, result = future.result()
                        tile_results[index] = result
                tile_dir = raw_dir / "tiles"
                tile_dir.mkdir(parents=True, exist_ok=True)
                pieces = []
                for index in sorted(tile_results):
                    piece = tile_results[index]
                    if piece.empty:
                        continue
                    tile_path = tile_dir / f"usgs_3dhp_tile_{index:04d}.gpkg"
                    piece.to_file(tile_path, driver="GPKG")
                    files[f"tile_{index:04d}"] = tile_path
                    pieces.append(piece)
                if pieces:
                    rivers = gpd.GeoDataFrame(
                        pd.concat(pieces, ignore_index=True), crs=pieces[0].crs
                    )
                    lookup = {str(column).lower(): column for column in rivers.columns}
                    identifier = next(
                        (
                            lookup[name] for name in (
                                "permanent_identifier", "nhdplusid", "objectid", "globalid"
                            ) if name in lookup
                        ),
                        None,
                    )
                    if identifier is not None:
                        rivers = rivers.drop_duplicates(subset=[identifier], keep="first")
                    else:
                        geometry_key = rivers.geometry.to_wkb(hex=True)
                        rivers = rivers.loc[~geometry_key.duplicated()].copy()
                else:
                    # Every queried tile returned a provider-confirmed empty
                    # feature set. Preserve that fact as a valid raw product.
                    rivers = gpd.GeoDataFrame(
                        {"geometry": gpd.GeoSeries([], crs="EPSG:4326")},
                        geometry="geometry", crs="EPSG:4326",
                    )
            rivers.to_file(path, driver="GPKG")
        files["vector"] = path
        metadata = {
            "features": int(len(rivers)), "tile_degrees": tile_degrees,
            "tile_count": tile_count, "download_workers": workers,
            "empty_domain_confirmed": bool(rivers.empty),
        }
    elif product.provider == "local":
        source = product.options["path"]
        files["vector"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        rivers = gpd.read_file(files["vector"])
        metadata = {"features": int(len(rivers))}
    else:
        raise ValueError(f"Unsupported hydrography provider: {product.provider}")
    for key, artifact in files.items():
        ledger.register(
            "hydrography", product.provider, artifact,
            {**product.options, "artifact": key},
        )
    if plot:
        fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
        boundary.to_crs(rivers.crs).boundary.plot(ax=ax, color="black", linewidth=0.8)
        if not rivers.empty:
            rivers.plot(ax=ax, color="#0072B2", linewidth=0.5)
        ax.set(title="Selected raw hydrography product", aspect="equal")
        plot_path = paths.figures / "raw_hydrography_selected_product.png"
        fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        files["plot"] = plot_path
    return RawProduct("hydrography", product.provider, files, metadata)


def fetch_drainage(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    raw_dir = paths.raw / "drainage"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "local_vector":
        source = product.options["path"]
        files["vector"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        data = gpd.read_file(files["vector"])
        ledger.register("drainage", product.provider, files["vector"], product.options)
        if plot:
            fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
            boundary.to_crs(data.crs).boundary.plot(ax=ax, color="black", linewidth=0.8)
            data.plot(ax=ax, color="#CC79A7", linewidth=0.7)
            ax.set(title="Selected raw drainage product", aspect="equal")
            plot_path = paths.figures / "raw_drainage_selected_product.png"
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            files["plot"] = plot_path
        return RawProduct("drainage", product.provider, files, {"features": int(len(data))})
    if product.provider == "local_raster":
        source = product.options["path"]
        files["raster"] = _copy_local(
            source, raw_dir / Path(source).name,
            reuse_existing=reuse_existing,
        )
        ledger.register("drainage", product.provider, files["raster"], product.options)
        if plot:
            files["plot"] = _plot_native_raster(
                files["raster"], paths.figures / "raw_drainage_selected_product.png",
                "Selected raw drainage product", "Drainage class", dpi,
            )
        return RawProduct("drainage", product.provider, files)
    if product.provider == "synthetic_nlcd":
        return RawProduct(
            "drainage", product.provider, {},
            {"raw_dependency": "land_cover/impervious", "note": "derived product; no independent download"},
        )
    raise ValueError(f"Unsupported drainage provider: {product.provider}")


def fetch_initial_state(
    product: ProductSpec,
    boundary: gpd.GeoDataFrame,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool,
    dpi: int,
    reuse_existing: bool = False,
) -> RawProduct:
    """Save the selected initial-depth source before RIFT-grid processing."""
    raw_dir = paths.raw / "initial_state"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    if product.provider == "local":
        source = Path(product.options["path"])
        files["raster"] = _copy_local(
            source, raw_dir / source.name,
            reuse_existing=reuse_existing,
        )
    elif product.provider in {"dry_zero", "constant"}:
        depth_m = 0.0 if product.provider == "dry_zero" else float(
            product.options.get("depth_m", 0.0)
        )
        if not np.isfinite(depth_m) or depth_m < 0:
            raise ValueError("Initial water depth must be a finite nonnegative value.")
        definition = raw_dir / "initial_state_definition.json"
        definition.write_text(json.dumps({
            "provider": product.provider,
            "depth_m": depth_m,
            "description": "User-selected initial water-depth definition",
        }, indent=2), encoding="utf-8")
        files["definition"] = definition
    else:
        raise ValueError(
            f"Unsupported initial_state provider: {product.provider}. "
            "Use dry_zero, constant, or local."
        )
    for key, artifact in files.items():
        ledger.register(
            "initial_state", product.provider, artifact,
            {**product.options, "artifact": key},
        )
    if plot:
        plot_path = paths.figures / "raw_initial_state_selected_product.png"
        if product.provider == "local":
            files["plot"] = _plot_native_raster(
                files["raster"], plot_path,
                "Selected raw initial water-depth product", "Water depth (m)", dpi,
            )
        else:
            selected_depth = (
                0.0 if product.provider == "dry_zero"
                else float(product.options.get("depth_m", 0.0))
            )
            fig, ax = plt.subplots(figsize=(7.2, 5.2), constrained_layout=True)
            boundary.plot(
                ax=ax, facecolor="#DCEEFF", edgecolor="#005A9C", linewidth=0.9
            )
            ax.set(
                title=f"Selected initial water-depth definition: {selected_depth:g} m",
                aspect="equal",
            )
            ax.text(
                0.5, 0.04, f"Uniform water depth = {selected_depth:g} m",
                transform=ax.transAxes, ha="center", va="bottom",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "0.6"},
            )
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            plt.close(fig)
            files["plot"] = plot_path
    metadata = dict(product.options)
    if product.provider in {"dry_zero", "constant"}:
        metadata["depth_m"] = (
            0.0 if product.provider == "dry_zero"
            else float(product.options.get("depth_m", 0.0))
        )
    return RawProduct("initial_state", product.provider, files, metadata)


def _download_json(
    url: str,
    params: dict[str, Any] | None,
    destination: Path,
    headers: dict[str, str] | None = None,
) -> Path:
    request_headers = {"User-Agent": "riftprep/0.3"}
    request_headers.update(headers or {})
    response = requests.get(
        url, params=params, timeout=600, headers=request_headers
    )
    response.raise_for_status()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)
    json.loads(response.content)
    return destination


def fetch_nwis_legacy(
    name: str,
    product: ProductSpec,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool = True,
    dpi: int = 180,
    reuse_existing: bool = False,
) -> RawProduct:
    """Download explicitly selected sites from the retired NWIS WaterServices API."""
    raw_dir = paths.raw / name
    files: dict[str, Path] = {}
    stations = list(product.options.get("stations", []))
    if not stations:
        raise ValueError(f"{name}.options.stations must list explicit NWIS sites.")
    grouped: dict[tuple[str, str], list[str]] = {}
    for station in stations:
        service = str(station.get("service", "iv"))
        default_parameter = "00060" if name == "inflows" else "00065"
        parameter = str(station.get("parameter_code", default_parameter))
        grouped.setdefault((service, parameter), []).append(str(station["site"]))
    for (service, parameter), sites in grouped.items():
        params = {
            "format": "json", "sites": ",".join(sorted(set(sites))), "parameterCd": parameter,
            "startDT": product.options["start_utc"], "endDT": product.options["end_utc"],
            "siteStatus": "all",
        }
        path = raw_dir / f"nwis_{service}_{parameter}.json"
        if not (reuse_existing and path.is_file()):
            path = _download_json(
                f"https://waterservices.usgs.gov/nwis/{service}/", params, path,
            )
        key = f"{service}_{parameter}"
        files[key] = path
        ledger.register(
            name, "usgs_nwis_legacy", path,
            {**product.options, "sites": sorted(set(sites))},
        )
    if plot:
        fig, ax = plt.subplots(figsize=(7.2, 3.6), constrained_layout=True)
        plotted = 0
        for key, path in list(files.items()):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for series in payload.get("value", {}).get("timeSeries", []):
                points = [point for block in series.get("values", []) for point in block.get("value", [])]
                if not points:
                    continue
                times = pd.to_datetime([point["dateTime"] for point in points], utc=True)
                values = pd.to_numeric([point["value"] for point in points], errors="coerce")
                site = series.get("sourceInfo", {}).get("siteCode", [{}])[0].get("value", key)
                variable = series.get("variable", {}).get("variableCode", [{}])[0].get("value", "")
                ax.plot(times, values, linewidth=0.9, label=f"{site} ({variable})")
                plotted += 1
        if plotted:
            ax.set(title=f"Selected raw {name} time series", xlabel="UTC", ylabel="Provider value")
            ax.legend(ncol=min(3, plotted), fontsize=7)
            plot_path = paths.figures / f"raw_{name}_selected_product.png"
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            files["plot"] = plot_path
        plt.close(fig)
    return RawProduct(name, "usgs_nwis_legacy", files, {"stations": stations})


def _waterdata_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "riftprep/0.3"})
    api_key = os.environ.get("USGS_WATERDATA_API_KEY")
    if api_key:
        session.headers.update({"X-Api-Key": api_key})
    return session


def _waterdata_site_id(site: str) -> str:
    site = str(site).strip()
    return site if site.upper().startswith("USGS-") else f"USGS-{site}"


def _load_completed_waterdata_download(
    manifest_path: Path,
    raw_dir: Path,
    expected: list[dict[str, str]],
    start_utc: str,
    end_utc: str,
) -> dict[str, Path] | None:
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("requests") != expected
            or manifest.get("start_utc") != start_utc
            or manifest.get("end_utc") != end_utc
        ):
            return None
        names = list(manifest["files"])
        paths: dict[str, Path] = {}
        for filename in names:
            stem = Path(filename).stem.removeprefix("waterdata_")
            if stem.startswith("location_"):
                key = stem
            else:
                key = f"observations_{stem.replace('_page_', '_')}"
            paths[key] = raw_dir / filename
        if not all(path.is_file() for path in paths.values()):
            return None
        paths["download_manifest"] = manifest_path
        return paths
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def fetch_usgs_waterdata(
    name: str,
    product: ProductSpec,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    plot: bool = True,
    dpi: int = 180,
    reuse_existing: bool = False,
) -> RawProduct:
    """Download current USGS Water Data OGC observations and station metadata.

    Each response page is retained byte-for-byte in ``raw/``. A completion
    manifest makes cache reuse explicit and prevents reuse of partial downloads.
    """
    raw_dir = paths.raw / name
    raw_dir.mkdir(parents=True, exist_ok=True)
    stations = list(product.options.get("stations", []))
    if not stations:
        raise ValueError(f"{name}.options.stations must list explicit USGS sites.")
    default_collection = str(product.options.get("collection", "continuous"))
    expected = sorted(
        [
            {
                "site": _waterdata_site_id(station["site"]),
                "parameter_code": str(station.get(
                    "parameter_code", "00060" if name in {"inflows", "spinup_baseflow"} else "00065"
                )),
                "collection": str(station.get("collection", default_collection)),
                **({"statistic_id": "00003", **(
                    {"time_series_id": str(station["time_series_id"])}
                    if station.get("time_series_id") else {}
                )} if station.get("collection", default_collection) == "daily" else {}),
            }
            for station in stations
        ],
        key=lambda item: (item["collection"], item["site"], item["parameter_code"]),
    )
    manifest_path = raw_dir / "waterdata_download_complete.json"
    files = (
        _load_completed_waterdata_download(
            manifest_path, raw_dir, expected,
            str(product.options["start_utc"]), str(product.options["end_utc"]),
        )
        if reuse_existing else None
    )
    if files is None:
        files = {}
        session = _waterdata_session()
        base = "https://api.waterdata.usgs.gov/ogcapi/v1/collections"
        downloaded_names: list[str] = []
        for request in expected:
            site = request["site"]
            bare_site = site.removeprefix("USGS-")
            parameter = request["parameter_code"]
            collection = request["collection"]
            location_path = raw_dir / f"waterdata_location_{bare_site}.json"
            if location_path.name not in downloaded_names:
                response = session.get(
                    f"{base}/monitoring-locations/items/{site}",
                    params={"f": "json"}, timeout=600,
                )
                location_path.write_bytes(response.content)
                response.raise_for_status()
                json.loads(response.content)
                files[f"location_{bare_site}"] = location_path
                downloaded_names.append(location_path.name)

            url = f"{base}/{collection}/items"
            params: dict[str, Any] | None = {
                "f": "json",
                "limit": 10000,
                "monitoring_location_id": site,
                "parameter_code": parameter,
                "datetime": (
                    f"{product.options['start_utc']}/{product.options['end_utc']}"
                ),
            }
            if collection == "daily":
                params.pop("datetime")
                params["time"] = f"{product.options['start_utc'][:10]}/{product.options['end_utc'][:10]}"
                params["statistic_id"] = request["statistic_id"]
                if request.get("time_series_id"):
                    params["time_series_id"] = request["time_series_id"]
            page = 1
            visited = set()
            while url:
                if url in visited or page > 1000:
                    raise RuntimeError("USGS pagination repeated a URL or exceeded 1000 pages.")
                visited.add(url)
                page_path = raw_dir / (
                    f"waterdata_{collection}_{bare_site}_{parameter}_page_{page:03d}.json"
                )
                response = session.get(url, params=params, timeout=600)
                page_path.write_bytes(response.content)
                response.raise_for_status()
                payload = response.json()
                files[
                    f"observations_{collection}_{bare_site}_{parameter}_{page:03d}"
                ] = page_path
                downloaded_names.append(page_path.name)
                next_links = [
                    link.get("href") for link in payload.get("links", [])
                    if link.get("rel") == "next" and link.get("href")
                ]
                url = urljoin(str(response.url), next_links[0]) if next_links else ""
                params = None
                page += 1
        manifest = {
            "provider": "usgs_waterdata",
            "api_base": base,
            "requests": expected,
            "start_utc": product.options["start_utc"],
            "end_utc": product.options["end_utc"],
            "files": downloaded_names,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        files["download_manifest"] = manifest_path

    for key, path in files.items():
        ledger.register(
            name, "usgs_waterdata", path,
            {**product.options, "artifact": key},
        )

    if plot:
        fig, ax = plt.subplots(figsize=(7.2, 3.6), constrained_layout=True)
        plotted = 0
        series_points: dict[tuple[str, str, str], list[tuple[str, Any]]] = {}
        for key, path in files.items():
            if not key.startswith("observations_"):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            for feature in payload.get("features", []):
                props = feature.get("properties", {})
                series_key = (
                    str(props.get("monitoring_location_id", "")),
                    str(props.get("parameter_code", "")),
                    str(props.get("time_series_id", "")),
                )
                series_points.setdefault(series_key, []).append(
                    (props.get("time"), props.get("value"))
                )
        for (site, parameter, series_id), points in series_points.items():
            frame = pd.DataFrame(points, columns=["time", "value"])
            frame["time"] = pd.to_datetime(frame["time"], utc=True, errors="coerce")
            frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
            frame = frame.dropna().sort_values("time")
            if frame.empty:
                continue
            suffix = f", {series_id}" if len(series_points) > 1 else ""
            ax.plot(
                frame["time"], frame["value"], linewidth=0.9,
                label=f"{site} ({parameter}{suffix})",
            )
            plotted += 1
        if plotted:
            ax.set(
                title=f"Selected raw {name} time series",
                xlabel="UTC", ylabel="Provider value",
            )
            ax.legend(ncol=min(3, plotted), fontsize=7)
            plot_path = paths.figures / f"raw_{name}_selected_product.png"
            fig.savefig(plot_path, dpi=dpi, bbox_inches="tight")
            files["plot"] = plot_path
        plt.close(fig)
    return RawProduct(name, "usgs_waterdata", files, {"stations": stations})


def acquire_raw_product(
    name: str,
    spec: CaseSpec,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    boundary: gpd.GeoDataFrame,
    timing_stage,
    query_geometry: gpd.GeoDataFrame | None = None,
) -> RawProduct:
    """Acquire one explicitly selected raw product for a staged notebook workflow."""
    query = boundary if query_geometry is None else query_geometry
    fetchers = {
        "dem": fetch_dem,
        "hydrography": fetch_hydrography,
        "land_cover": fetch_land_cover,
        "soil": fetch_soil,
        "rainfall": fetch_rainfall,
        "drainage": fetch_drainage,
        "initial_state": fetch_initial_state,
    }
    if name in fetchers:
        product = getattr(spec, name)
        if not product.enabled:
            raise ValueError(f"Raw product {name!r} is disabled in the case configuration.")
        with timing_stage(f"download_raw_{name}"):
            source_geometry = (
                boundary
                if name in {"soil", "hydrography"} or (
                    name == "dem" and spec.domain.outside_dem_mode == "wall"
                )
                else query
            )
            started_utc = datetime.now(timezone.utc).isoformat()
            before = _raw_file_state(paths.raw / name)
            try:
                result = fetchers[name](
                    product, source_geometry, paths, ledger,
                    spec.plots.raw_products, spec.plots.dpi,
                    spec.cache.mode == "reuse",
                )
            except Exception as error:
                _finalize_product_acquisition(
                    ledger=ledger, result=None, product_name=name,
                    product=product, geometry=source_geometry, paths=paths,
                    cache_mode=spec.cache.mode, started_utc=started_utc,
                    before=before, error=error,
                )
                raise
            _finalize_product_acquisition(
                ledger=ledger, result=result, product_name=name,
                product=product, geometry=source_geometry, paths=paths,
                cache_mode=spec.cache.mode, started_utc=started_utc,
                before=before,
            )
            return result
    if name in {"inflows", "coastal", "spinup_baseflow"}:
        product = getattr(spec, name)
        if not product.enabled:
            raise ValueError(f"Raw product {name!r} is disabled in the case configuration.")
        with timing_stage(f"download_raw_{name}"):
            started_utc = datetime.now(timezone.utc).isoformat()
            before = _raw_file_state(paths.raw / name)
            try:
                if name == "spinup_baseflow" and product.provider == "manual_constant":
                    directory = paths.raw / name
                    directory.mkdir(parents=True, exist_ok=True)
                    definition = directory / "user_baseflow_definition.json"
                    definition.write_text(json.dumps(product.options, indent=2), encoding="utf-8")
                    ledger.register(name, "manual_constant", definition, product.options)
                    result = RawProduct(name, "manual_constant", {"definition": definition},
                                        {"method": "explicit_user_constant", "stations": product.options["stations"]})
                elif product.provider == "usgs_waterdata":
                    result = fetch_usgs_waterdata(
                        name, product, paths, ledger, spec.plots.raw_products,
                        spec.plots.dpi, spec.cache.mode == "reuse",
                    )
                elif product.provider in {"usgs_nwis", "usgs_nwis_legacy"}:
                    result = fetch_nwis_legacy(
                        name, product, paths, ledger, spec.plots.raw_products,
                        spec.plots.dpi, spec.cache.mode == "reuse",
                    )
                else:
                    raise ValueError(
                        f"Unsupported {name} provider: {product.provider}. "
                        "Use usgs_waterdata or usgs_nwis_legacy."
                    )
            except Exception as error:
                _finalize_product_acquisition(
                    ledger=ledger, result=None, product_name=name,
                    product=product, geometry=boundary, paths=paths,
                    cache_mode=spec.cache.mode, started_utc=started_utc,
                    before=before, error=error,
                )
                raise
            _finalize_product_acquisition(
                ledger=ledger, result=result, product_name=name,
                product=product, geometry=boundary, paths=paths,
                cache_mode=spec.cache.mode, started_utc=started_utc,
                before=before,
            )
            return result
    raise KeyError(f"Unknown raw product: {name!r}")


def acquire_all_raw(
    spec: CaseSpec,
    paths: ProjectPaths,
    ledger: ProvenanceLedger,
    boundary: gpd.GeoDataFrame,
    timing_stage,
    query_geometry: gpd.GeoDataFrame | None = None,
) -> RawCatalog:
    """Acquire every enabled source before any processing begins."""
    products: dict[str, RawProduct] = {}
    for name in (
        "dem", "hydrography", "land_cover", "soil", "rainfall",
        "drainage", "inflows", "coastal", "spinup_baseflow",
        "initial_state",
    ):
        if getattr(spec, name).enabled:
            products[name] = acquire_raw_product(
                name, spec, paths, ledger, boundary, timing_stage, query_geometry
            )
    return RawCatalog(boundary=boundary, products=products)
