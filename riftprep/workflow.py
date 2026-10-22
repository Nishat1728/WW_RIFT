"""End-to-end raw-first workflow for general RIFT input preparation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import nearest_points, unary_union

from .artifacts import (
    ProvenanceLedger, TimingLedger, prepare_case_directory, redact_sensitive,
)
from .config import GridSpec, ProjectPaths
from .drainage import build_drainage_parameter_table, rasterize_drainage_features
from .managers import (
    RawCatalog,
    RawProduct,
    acquire_all_raw,
    acquire_raw_product,
    resolve_boundary,
)
from .input_qc import InputQCResult, check_raw_product, qc_results_table
from .plotting import (
    plot_dem_domain,
    plot_drainage,
    plot_hydrographs,
    plot_outlet_diagnostics,
    plot_river_bathymetry,
    save_figure,
)
from .processing import (
    plot_aorc,
    plot_nlcd,
    plot_soils,
    prepare_aorc_event,
    prepare_nlcd,
    prepare_ssurgo,
    prepare_surge_grids,
    prepare_synthetic_drainage,
)
from .spatial import (
    align_raster,
    apply_closed_huc_wall,
    build_grid,
    burn_river_bathymetry,
    carve_outlets,
    create_domain_masks,
)
from .spec import CaseSpec, ProductSpec, load_case, save_case
from .run_manifest import RunManifestWriter
from .validation import ValidationReport, validate_case
from .writers import (
    build_rift_config,
    write_esri_ascii,
    write_hydrograph_bundle,
    write_manifest,
    write_run_instructions,
)


@dataclass(frozen=True)
class CaseResult:
    case_root: Path
    config_path: Path
    manifest_path: Path
    qc_json_path: Path
    timing_csv_path: Path
    raw_catalog_path: Path
    ok: bool

    @property
    def run_manifest_path(self) -> Path:
        return self.case_root / "quality_control" / "provenance" / "run_manifest.json"


@dataclass(frozen=True)
class RawDownloadResult:
    """Saved raw-input phase that can be inspected before RIFT processing."""

    spec: CaseSpec
    paths: ProjectPaths
    grid: GridSpec
    catalog: RawCatalog
    summary_path: Path
    timing_csv_path: Path
    raw_catalog_path: Path
    run_id: str | None = None
    run_started_utc: str | None = None

    @property
    def acquisition_events_path(self) -> Path:
        """Additive provenance path without changing the constructor contract."""
        return self.paths.provenance / "acquisition_events.json"

    @property
    def run_manifest_path(self) -> Path:
        return self.paths.provenance / "run_manifest.json"


def _rel(root: Path, path: str | Path) -> str:
    return Path(path).resolve().relative_to(root.resolve()).as_posix()


def _save_plots(fig, paths: ProjectPaths, stem: str, spec: CaseSpec) -> None:
    save_figure(fig, paths.figures / stem, formats=spec.plots.formats, dpi=spec.plots.dpi)
    plt.close(fig)


def _configure_case_http_cache(paths: ProjectPaths) -> None:
    """Keep provider HTTP state inside this case's guarded directory."""
    http_cache = paths.provider_cache / "http"
    http_cache.mkdir(parents=True, exist_ok=True)
    os.environ["HYRIVER_CACHE_NAME"] = str(http_cache / "aiohttp_cache.sqlite")
    os.environ["HYRIVER_CACHE_NAME_HTTP"] = str(http_cache / "http_cache.sqlite")


def _query_geometry(grid: GridSpec) -> gpd.GeoDataFrame:
    # ``GeoDataFrame.to_crs()`` transforms polygon vertices, not curved edges.
    # On regional projected grids, transforming a four-corner box directly can
    # turn a curved geographic edge into a chord and omit kilometres of the
    # requested grid.  Densified transform_bounds() produces a conservative
    # geographic envelope for every domain size and CRS.
    from rasterio.warp import transform_bounds

    span_m = max(grid.xmax - grid.xmin, grid.ymax - grid.ymin)
    densify_points = max(64, min(512, int(np.ceil(span_m / 10_000.0))))
    xmin, ymin, xmax, ymax = transform_bounds(
        grid.crs,
        "EPSG:4326",
        grid.xmin,
        grid.ymin,
        grid.xmax,
        grid.ymax,
        densify_pts=densify_points,
    )
    return gpd.GeoDataFrame(
        {"query": ["densified_rift_grid_extent"]},
        geometry=[box(xmin, ymin, xmax, ymax)],
        crs="EPSG:4326",
    )


def _resolve_outlet_spec(
    outlet_spec,
    boundary: gpd.GeoDataFrame,
    rivers: gpd.GeoDataFrame,
    dem: np.ndarray,
    grid: GridSpec,
    paths: ProjectPaths,
):
    """Resolve an explicitly requested hydrography/DEM outlet to coordinates."""
    if outlet_spec is None or outlet_spec.source != "auto_hydrography_lowest":
        return outlet_spec
    if rivers is None or rivers.empty:
        (paths.quality_control / "derived_outlet.json").write_text(json.dumps({
            "method": "auto_hydrography_lowest",
            "status": "NO_HYDROGRAPHY",
            "candidate_count": 0,
            "action": "Outlet carving skipped because the provider-confirmed hydrography product is empty.",
            "user_action": "Provide coordinate outlets if an explicit outlet is required.",
        }, indent=2), encoding="utf-8")
        return None
    domain_edge = unary_union(boundary.to_crs(grid.crs).geometry).boundary
    river_data = rivers.to_crs(grid.crs)
    candidates = []
    for geometry in river_data.geometry:
        if geometry is None or geometry.is_empty:
            continue
        if geometry.distance(domain_edge) <= 2 * grid.resolution:
            edge_point, _ = nearest_points(domain_edge, geometry)
            if grid.xmin <= edge_point.x <= grid.xmax and grid.ymin <= edge_point.y <= grid.ymax:
                column = int(np.clip(round((edge_point.x - grid.xmin) / grid.resolution), 0, grid.nx))
                row = int(np.clip(round((grid.ymax - edge_point.y) / grid.resolution), 0, grid.ny))
                if np.isfinite(dem[row, column]):
                    candidates.append((float(dem[row, column]), edge_point))
    if not candidates:
        raise ValueError("No hydrography line approaches the domain boundary for automatic outlet selection.")
    elevation, point = min(candidates, key=lambda item: item[0])
    resolved = replace(
        outlet_spec,
        source="coordinates",
        coordinates=({"x": float(point.x), "y": float(point.y),
                      "width_m": float(outlet_spec.default_width_m)},),
        crs=grid.crs,
    )
    (paths.quality_control / "derived_outlet.json").write_text(json.dumps({
        "method": "auto_hydrography_lowest",
        "candidate_count": len(candidates),
        "x": float(point.x), "y": float(point.y),
        "sampled_dem_elevation_m": elevation,
        "crs": grid.crs,
    }, indent=2), encoding="utf-8")
    return resolved


def _find_nwis_series(
    raw_catalog: RawCatalog,
    product_name: str,
    site: str,
    parameter: str,
    requested_time_series_id: str | None = None,
):
    product = raw_catalog.require(product_name)
    normalized_site = site.removeprefix("USGS-")
    modern_groups: dict[str, list[dict[str, Any]]] = {}
    modern_unit: dict[str, str] = {}
    location_feature: dict[str, Any] | None = None
    for key, path in product.files.items():
        if path.suffix.lower() != ".json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if key == f"location_{normalized_site}":
            location_feature = payload
        for feature in payload.get("features", []):
            props = feature.get("properties", {})
            feature_site = str(props.get("monitoring_location_id", "")).removeprefix("USGS-")
            feature_parameter = str(props.get("parameter_code", ""))
            if feature_site != normalized_site or feature_parameter != parameter:
                continue
            series_id = str(props.get("time_series_id", ""))
            modern_groups.setdefault(series_id, []).append(props)
            modern_unit[series_id] = str(props.get("unit_of_measure", "unknown"))
        for series in payload.get("value", {}).get("timeSeries", []):
            series_site = series.get("sourceInfo", {}).get("siteCode", [{}])[0].get("value")
            series_parameter = series.get("variable", {}).get("variableCode", [{}])[0].get("value")
            if str(series_site).removeprefix("USGS-") == normalized_site and series_parameter == parameter:
                return series
    if modern_groups:
        available = sorted(modern_groups)
        if requested_time_series_id is None:
            if len(available) != 1:
                raise ValueError(
                    f"USGS Water Data returned multiple time_series_id values for "
                    f"site={site}, parameter={parameter}: {available}. Select one in YAML."
                )
            selected = available[0]
        else:
            selected = str(requested_time_series_id)
            if selected not in modern_groups:
                raise LookupError(
                    f"time_series_id={selected!r} was not returned; available={available}."
                )
        observations = sorted(modern_groups[selected], key=lambda item: str(item.get("time", "")))
        geometry = (location_feature or {}).get("geometry", {})
        if not geometry.get("coordinates"):
            raise ValueError(f"USGS monitoring location {site} has no usable point geometry.")
        longitude, latitude = geometry["coordinates"][:2]
        return {
            "sourceInfo": {
                "siteCode": [{"value": normalized_site}],
                "geoLocation": {"geogLocation": {
                    "longitude": longitude, "latitude": latitude,
                }},
            },
            "variable": {
                "variableCode": [{"value": parameter}],
                "unit": {"unitCode": modern_unit[selected]},
            },
            "values": [{"value": [
                {"dateTime": item.get("time"), "value": item.get("value")}
                for item in observations
            ]}],
            "time_series_id": selected,
        }
    raise LookupError(
        f"Raw USGS catalog has no site={site}, parameter={parameter} series."
    )


def _series_to_hourly(
    series: dict[str, Any],
    start_utc: str,
    end_utc: str,
    max_gap_hours: int,
) -> tuple[pd.DatetimeIndex, np.ndarray, str]:
    points = [point for block in series.get("values", []) for point in block.get("value", [])]
    if not points:
        raise ValueError("The selected NWIS series contains no observations.")
    frame = pd.DataFrame({
        "time": pd.to_datetime([point["dateTime"] for point in points], utc=True),
        "value": pd.to_numeric([point["value"] for point in points], errors="coerce"),
    }).dropna().drop_duplicates("time", keep="last").set_index("time").sort_index()
    index = pd.date_range(start_utc, end_utc, freq="1h", inclusive="both")
    hourly = frame.value.resample("1h").mean().reindex(index)
    hourly = hourly.interpolate(method="time", limit=max_gap_hours, limit_area="inside")
    if hourly.isna().any():
        missing = hourly.index[hourly.isna()]
        raise ValueError(f"NWIS series has {len(missing)} unfilled hourly values; first={missing[0]}")
    unit = str(series.get("variable", {}).get("unit", {}).get("unitCode", "unknown"))
    return index, hourly.to_numpy(dtype=float), unit


def _source_location(series: dict[str, Any]) -> tuple[float, float]:
    location = series.get("sourceInfo", {}).get("geoLocation", {}).get("geogLocation", {})
    return float(location["longitude"]), float(location["latitude"])


def _support_xy(station: dict[str, Any], series: dict[str, Any], grid: GridSpec) -> tuple[float, float]:
    support = dict(station.get("support", {}))
    if support:
        x, y = float(support["x"]), float(support["y"])
        crs = str(support.get("crs", "EPSG:4326"))
    else:
        x, y = _source_location(series)
        crs = "EPSG:4326"
    return Transformer.from_crs(crs, grid.crs, always_xy=True).transform(x, y)


def _unit_factor(unit: str, station: dict[str, Any], purpose: str) -> float:
    if "unit_to_si" in station:
        return float(station["unit_to_si"])
    normalized_unit = unit.strip().lower().replace(" ", "").replace("³", "3")
    defaults = {
        ("discharge", "ft3/s"): 0.028316846592,
        ("discharge", "ft^3/s"): 0.028316846592,
        ("discharge", "cfs"): 0.028316846592,
        ("stage", "ft"): 0.3048,
        ("stage", "m"): 1.0,
    }
    try:
        return defaults[(purpose, normalized_unit)]
    except KeyError as error:
        raise ValueError(
            f"No safe SI conversion for NWIS unit {unit!r}; add unit_to_si to the station."
        ) from error


def _prepare_inflows(
    product: ProductSpec,
    raw: RawCatalog,
    grid: GridSpec,
    paths: ProjectPaths,
) -> tuple[Path, dict[str, tuple[np.ndarray, np.ndarray]]]:
    sources = []
    plotted: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for station in product.options["stations"]:
        site = str(station["site"])
        parameter = str(station.get("parameter_code", "00060"))
        series = _find_nwis_series(
            raw, "inflows", site, parameter, station.get("time_series_id")
        )
        index, values, unit = _series_to_hourly(
            series,
            product.options["start_utc"], product.options["end_utc"],
            int(station.get("max_gap_hours", 6)),
        )
        values = (
            values * _unit_factor(unit, station, "discharge")
            * float(station.get("multiplier", 1.0))
            + float(station.get("offset_si", 0.0))
        )
        if np.any(values < 0):
            raise ValueError(f"Converted inflow {station.get('id', site)} contains negative discharge.")
        x, y = _support_xy(station, series, grid)
        elapsed = (index - index[0]).total_seconds().to_numpy(dtype=float)
        source = {
            "id": str(station.get("id", site)),
            "time_s": elapsed,
            "discharge_m3_s": values,
            "xmin_m": x,
            "ymin_m": y,
        }
        width = float(station.get("support_width_m", 0.0))
        if width > 0:
            source.update({"xmin_m": x - width / 2, "xmax_m": x + width / 2,
                           "ymin_m": y - width / 2, "ymax_m": y + width / 2})
        sources.append(source)
        plotted[source["id"]] = (elapsed, values)
    table = write_hydrograph_bundle(paths.simulation_ready / "hydrographs", sources)
    return table, plotted


def _prepare_coastal(
    product: ProductSpec,
    raw: RawCatalog,
    grid: GridSpec,
    paths: ProjectPaths,
    dem: np.ndarray,
    node_mask: np.ndarray,
) -> tuple[pd.DataFrame, Path]:
    stations = list(product.options["stations"])
    if len(stations) != 1:
        raise ValueError("The current RIFT coastal boundary supports exactly one explicit stage station.")
    station = stations[0]
    site = str(station["site"])
    parameter = str(station.get("parameter_code", "00065"))
    series = _find_nwis_series(
        raw, "coastal", site, parameter, station.get("time_series_id")
    )
    index, values, unit = _series_to_hourly(
        series, product.options["start_utc"], product.options["end_utc"],
        int(station.get("max_gap_hours", 6)),
    )
    stage_m = values * _unit_factor(unit, station, "stage") + float(station.get("datum_offset_m", 0.0))
    frame = pd.DataFrame({
        "time_utc": index,
        "time_s": (index - index[0]).total_seconds().to_numpy(dtype=float),
        "stage_m": stage_m,
    })
    stage_csv = paths.processed / "coastal_stage_si.csv"
    frame.to_csv(stage_csv, index=False)
    x, y = _support_xy(station, series, grid)
    surrogate = frame.rename(columns={"stage_m": "stage_m_navd88"})
    sampled, _, _ = prepare_surge_grids(
        surrogate, dem, node_mask, (x, y), grid, paths.simulation_ready / "surge",
        interval_hours=int(product.options.get("interval_hours", 1)),
        coastal_radius_m=float(product.options.get("coastal_radius_m", 3000.0)),
    )
    return sampled, stage_csv


def _prepare_drainage(
    product: ProductSpec,
    raw: RawCatalog,
    grid: GridSpec,
    node_mask: np.ndarray,
    impervious: np.ndarray | None,
    paths: ProjectPaths,
):
    if product.provider == "synthetic_nlcd":
        if impervious is None:
            raise ValueError("synthetic_nlcd drainage requires selected land-cover imperviousness.")
        return prepare_synthetic_drainage(
            impervious, node_mask, grid, paths.simulation_ready,
            float(product.options.get("impervious_threshold_pct", 80.0)),
        )
    controls = build_drainage_parameter_table(product.options["controls"])
    if product.provider == "local_vector":
        data = gpd.read_file(raw.require("drainage").files["vector"])
        drainage_map, audit = rasterize_drainage_features(
            data, grid, node_mask,
            type_field=str(product.options.get("type_field", "type")),
            priority=tuple(product.options.get("priority", [5, 2, 1, 3, 4])),
        )
        audit.to_csv(paths.quality_control / "drainage_rasterization.csv", index=False)
    elif product.provider == "local_raster":
        drainage_map = np.rint(
            align_raster(raw.require("drainage").files["raster"], grid, "node", "nearest")
        ).astype(np.int32)
        drainage_map[~node_mask] = 0
    else:
        raise ValueError(f"Unsupported processed drainage provider: {product.provider}")
    from .writers import write_esri_ascii
    map_path = write_esri_ascii(
        paths.simulation_ready / "urban_drain_type.asc", drainage_map, grid
    )
    table_path = paths.simulation_ready / "urban_hydraulic_controls.csv"
    controls.to_csv(table_path, index=False)
    return drainage_map, controls, map_path, table_path


def _write_qc(report: ValidationReport, paths: ProjectPaths) -> tuple[Path, Path]:
    frame = report.dataframe()
    csv_path = paths.quality_control / "qc_report.csv"
    json_path = paths.quality_control / "qc_report.json"
    frame.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps({
        "ok": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "checks": report.checks,
    }, indent=2), encoding="utf-8")
    return json_path, csv_path


class RIFTInputBuilder:
    """Manager-style, staged notebook interface modeled on Watershed Workflow."""

    product_order = (
        "dem", "initial_state", "hydrography", "land_cover", "soil", "rainfall",
        "drainage", "inflows", "coastal",
    )

    def __init__(self, spec_or_path: CaseSpec | str | Path):
        self.spec = (
            load_case(spec_or_path)
            if isinstance(spec_or_path, (str, Path))
            else spec_or_path
        )
        preexisting_case = self.spec.case_root.exists()
        self.paths = prepare_case_directory(self.spec.case_root, self.spec.cache.mode)
        _configure_case_http_cache(self.paths)
        self.ledger = ProvenanceLedger(self.paths.provenance)
        self.timings = TimingLedger(self.paths.timings)
        save_case(self.spec, self.paths.quality_control / "resolved_case.yaml")
        self.run_manifest = RunManifestWriter(
            self.spec, self.paths, preexisting_case=preexisting_case
        )
        self.boundary: gpd.GeoDataFrame | None = None
        self.grid: GridSpec | None = None
        self.products: dict[str, RawProduct] = {}
        self.input_qc: dict[str, InputQCResult] = {}

    @property
    def selected_products(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.product_order if getattr(self.spec, name).enabled
        )

    def source_table(self) -> pd.DataFrame:
        """Return the visible source-manager table used by the notebook."""
        rows = []
        for name in self.product_order:
            product = getattr(self.spec, name)
            rows.append({
                "input": name,
                "enabled": product.enabled,
                "provider": product.provider,
                "selected_options": json.dumps(product.options, sort_keys=True),
            })
        return pd.DataFrame(rows)

    def load_domain(self) -> gpd.GeoDataFrame:
        """Download/load the selected boundary and construct the requested RIFT grid."""
        if self.boundary is None:
            try:
                with self.timings.stage("resolve_boundary"):
                    self.boundary = resolve_boundary(self.spec, self.paths, self.ledger)
                    self.grid = build_grid(self.spec.domain, self.boundary)
                    (self.paths.quality_control / "grid.json").write_text(
                        json.dumps(self.grid.as_dict(), indent=2), encoding="utf-8"
                    )
            except BaseException as error:
                self.run_manifest.fail(error, "resolve_boundary")
                raise
        return self.boundary

    def download(self, name: str) -> RawProduct:
        """Run one visible raw-source manager and retain its native artifacts."""
        if name not in self.product_order:
            raise KeyError(f"Unknown input {name!r}; choose from {self.product_order}.")
        if name in self.products:
            return self.products[name]
        boundary = self.load_domain()
        assert self.grid is not None
        try:
            product = acquire_raw_product(
                name,
                self.spec,
                self.paths,
                self.ledger,
                boundary,
                self.timings.stage,
                query_geometry=_query_geometry(self.grid),
            )
            qc_path = self.paths.quality_control / "input_qc" / f"{name}.json"
            self.input_qc[name] = check_raw_product(
                name, product, getattr(self.spec, name).options, qc_path
            )
        except BaseException as error:
            self.run_manifest.fail(error, f"download_raw_{name}")
            raise
        self.products[name] = product
        return product

    def qc_table(self) -> pd.DataFrame:
        """Return one concise status row for every downloaded input."""
        return qc_results_table(self.input_qc)

    def qc_result(self, name: str) -> InputQCResult:
        """Return a downloaded input's quality result for notebook display."""
        if name not in self.input_qc:
            raise RuntimeError(f"Download and quality-check {name!r} first.")
        return self.input_qc[name]

    def download_all(self) -> RawCatalog:
        for name in self.selected_products:
            self.download(name)
        assert self.boundary is not None
        return RawCatalog(boundary=self.boundary, products=dict(self.products))

    def finish_raw_downloads(self) -> RawDownloadResult:
        """Validate that every enabled manager ran before processing can start."""
        missing = set(self.selected_products) - set(self.products)
        if missing:
            error = RuntimeError(
                "Run the visible download cells for all enabled inputs first; missing "
                f"{sorted(missing)}."
            )
            self.run_manifest.fail(error, "raw_download_incomplete")
            raise error
        assert self.grid is not None and self.boundary is not None
        catalog = RawCatalog(boundary=self.boundary, products=dict(self.products))
        summary_path = self.paths.quality_control / "raw_download_complete.json"
        summary_path.write_text(json.dumps({
            "schema": "riftprep-raw-download-v1",
            "case_name": self.spec.case_name,
            "cache_mode": self.spec.cache.mode,
            "grid": self.grid.as_dict(),
            "products": {
                name: {
                    "provider": product.provider,
                    "files": {key: str(path) for key, path in product.files.items()},
                    "metadata": product.metadata,
                }
                for name, product in catalog.products.items()
            },
            "input_quality_control": {
                name: {
                    "status": result.status,
                    "report": str(
                        self.paths.quality_control / "input_qc" / f"{name}.json"
                    ),
                }
                for name, result in self.input_qc.items()
            },
            "processing_started": False,
            "provenance": {
                "raw_products": str(self.paths.provenance / "raw_products.json"),
                "acquisition_events": str(
                    self.paths.provenance / "acquisition_events.json"
                ),
                "run_manifest": str(self.run_manifest.latest_path),
                "run_manifest_history": str(self.run_manifest.history_path),
            },
        }, indent=2), encoding="utf-8")
        self.run_manifest.raw_complete()
        return RawDownloadResult(
            spec=self.spec,
            paths=self.paths,
            grid=self.grid,
            catalog=catalog,
            summary_path=summary_path,
            timing_csv_path=self.paths.timings / "wall_clock.csv",
            raw_catalog_path=self.paths.provenance / "raw_products.json",
            run_id=self.run_manifest.run_id,
            run_started_utc=self.run_manifest.started_utc,
        )


def download_raw_inputs(spec_or_path: CaseSpec | str | Path) -> RawDownloadResult:
    """Download, save, checksum, and optionally plot every selected raw input."""
    builder = RIFTInputBuilder(spec_or_path)
    builder.download_all()
    return builder.finish_raw_downloads()


def build_rift_inputs(downloaded: RawDownloadResult) -> CaseResult:
    """Build RIFT files from the exact raw catalog saved by ``download_raw_inputs``."""
    try:
        result = run_case(downloaded.spec, _downloaded=downloaded)
    except BaseException as error:
        payload = json.loads(downloaded.summary_path.read_text(encoding="utf-8"))
        payload["processing_started"] = True
        payload["processing_complete"] = False
        payload["processing_failure"] = {
            "type": type(error).__name__,
            "message": redact_sensitive(str(error)),
        }
        downloaded.summary_path.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        raise
    save_case(downloaded.spec, downloaded.paths.quality_control / "resolved_case.yaml")
    payload = json.loads(downloaded.summary_path.read_text(encoding="utf-8"))
    payload["processing_started"] = True
    payload["processing_complete"] = result.ok
    payload["run_manifest"] = str(result.run_manifest_path)
    downloaded.summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return result


def _run_case_impl(
    spec_or_path: CaseSpec | str | Path,
    *,
    _downloaded: RawDownloadResult | None = None,
    _paths: ProjectPaths | None = None,
) -> CaseResult:
    """Download all selected raw products, then build and validate RIFT inputs."""
    spec = load_case(spec_or_path) if isinstance(spec_or_path, (str, Path)) else spec_or_path
    if _downloaded is None:
        paths = _paths or prepare_case_directory(spec.case_root, spec.cache.mode)
        _configure_case_http_cache(paths)
        ledger = ProvenanceLedger(paths.provenance)
        timings = TimingLedger(paths.timings)
        save_case(spec, paths.quality_control / "resolved_case.yaml")

        with timings.stage("resolve_boundary"):
            boundary = resolve_boundary(spec, paths, ledger)
            grid = build_grid(spec.domain, boundary)
            (paths.quality_control / "grid.json").write_text(
                json.dumps(grid.as_dict(), indent=2), encoding="utf-8"
            )

        raw = acquire_all_raw(
            spec, paths, ledger, boundary, timings.stage,
            query_geometry=_query_geometry(grid),
        )
        for name, product in raw.products.items():
            check_raw_product(
                name,
                product,
                getattr(spec, name).options,
                paths.quality_control / "input_qc" / f"{name}.json",
            )
    else:
        if spec != _downloaded.spec:
            raise ValueError("The processing specification must match the downloaded raw phase.")
        paths = _downloaded.paths
        grid = _downloaded.grid
        raw = _downloaded.catalog
        boundary = raw.boundary
        timings = TimingLedger(paths.timings)
        save_case(spec, paths.quality_control / "resolved_case.yaml")

    with timings.stage("build_domain_masks"):
        node_mask, cell_mask = create_domain_masks(boundary, grid, spec.domain.boundary_rule)

    with timings.stage("process_dem"):
        raw_dem = raw.require("dem")
        dem = align_raster(raw_dem.files["raster"], grid, "node", "bilinear")
        if np.any(~np.isfinite(dem[node_mask])):
            raise ValueError("Selected raw DEM does not cover every active RIFT node.")
        processed_dem_dir = paths.processed / "dem"
        aligned_dem_path = write_esri_ascii(
            processed_dem_dir / "dem_aligned_to_rift_grid.asc", dem, grid
        )
        derived_dem_paths: dict[str, Path] = {
            "aligned_to_rift_grid": aligned_dem_path,
        }
        rivers = None
        burn_depth = np.zeros_like(dem)
        needs_rivers = (
            spec.river_bathymetry.apply_bathymetry
            or (spec.outlets is not None and spec.outlets.source == "auto_hydrography_lowest")
        )
        if needs_rivers:
            rivers = gpd.read_file(raw.require("hydrography").files["vector"])
        if spec.river_bathymetry.apply_bathymetry:
            dem, burn_depth = burn_river_bathymetry(
                dem, rivers, node_mask, grid, spec.river_bathymetry
            )
            derived_dem_paths["after_river_bathymetry"] = write_esri_ascii(
                processed_dem_dir / "dem_after_river_bathymetry.asc", dem, grid
            )
        original_dem = dem.copy()
        carved = np.zeros_like(node_mask, dtype=bool)
        outlet_features = None
        resolved_outlets = None
        if spec.domain.outside_dem_mode == "wall":
            final_dem = apply_closed_huc_wall(dem, node_mask, spec.domain.wall_elevation_m)
            if spec.outlets is not None:
                resolved_outlets = _resolve_outlet_spec(
                    spec.outlets, boundary, rivers, original_dem, grid, paths
                )
                if resolved_outlets is not None:
                    final_dem, outlet_features, carved = carve_outlets(
                        final_dem, original_dem, node_mask, grid, resolved_outlets,
                        spec.domain.wall_elevation_m,
                    )
        else:
            if not np.isfinite(dem).all():
                raise ValueError("outside_dem_mode='real_terrain' requires DEM coverage of the entire grid.")
            final_dem = dem
        dem_path = write_esri_ascii(
            paths.simulation_ready / "dem.asc", final_dem, grid
        )
        raw_dem_paths = {
            name: str(Path(path).resolve())
            for name, path in raw_dem.files.items()
            if name != "plot"
            and Path(path).resolve().is_relative_to(paths.raw.resolve())
        }
        (paths.quality_control / "dem_lineage.json").write_text(
            json.dumps({
                "schema": "riftprep-dem-lineage-v1",
                "provider": raw_dem.provider,
                "raw_provider_artifacts": raw_dem_paths,
                "source_raster_used_for_alignment": str(
                    Path(raw_dem.files["raster"]).resolve()
                ),
                "source_raster_is_raw": Path(raw_dem.files["raster"]).resolve().is_relative_to(
                    paths.raw.resolve()
                ),
                "derived_intermediates": {
                    name: str(path.resolve()) for name, path in derived_dem_paths.items()
                },
                "simulation_ready_dem": str(dem_path.resolve()),
                "rift_grid_crs": grid.crs,
                "rift_grid_cell_size_m": grid.resolution,
                "processing": {
                    "alignment_resampling": "bilinear",
                    "river_bathymetry_applied": spec.river_bathymetry.apply_bathymetry,
                    "outside_dem_mode": spec.domain.outside_dem_mode,
                    "outlet_carving_applied": bool(carved.any()),
                },
            }, indent=2),
            encoding="utf-8",
        )
        initial_source = raw.require("initial_state")
        if spec.initial_state.provider == "local":
            initial_depth = align_raster(
                initial_source.files["raster"], grid, "node",
                str(spec.initial_state.options.get("resampling", "bilinear")),
            )
        else:
            depth_m = 0.0 if spec.initial_state.provider == "dry_zero" else float(
                spec.initial_state.options.get("depth_m", 0.0)
            )
            initial_depth = np.full(grid.node_shape, depth_m, dtype=float)
        invalid_initial = node_mask & (
            ~np.isfinite(initial_depth) | (initial_depth < 0)
        )
        initial_qc_path = paths.quality_control / "initial_state_coverage.json"
        initial_qc_path.write_text(json.dumps({
            "schema": "riftprep-initial-state-v1",
            "status": "FAILED" if invalid_initial.any() else "PASSED",
            "provider": spec.initial_state.provider,
            "invalid_active_nodes": int(np.count_nonzero(invalid_initial)),
            "criterion": "finite water depth >= 0 m at every active node",
        }, indent=2), encoding="utf-8")
        if invalid_initial.any():
            raise ValueError(
                "Initial water-depth source has null, infinite, or negative active nodes. "
                f"Inspect {initial_qc_path} and provide a corrected local raster."
            )
        initial_depth = np.where(node_mask, initial_depth, 0.0)
        initial_depth_path = write_esri_ascii(
            paths.simulation_ready / "initial_water_depth_m.asc", initial_depth, grid
        )

    cover = impervious = canopy = manning = None
    land_paths: dict[str, Path] = {}
    if spec.land_cover.enabled:
        with timings.stage("process_land_cover"):
            raw_lc = raw.require("land_cover")
            missing = {"cover", "impervious", "canopy"} - set(raw_lc.files)
            if missing:
                raise ValueError(f"RIFT land processing needs cover, impervious, canopy; missing {sorted(missing)}")
            cover, impervious, canopy, manning, land_paths = prepare_nlcd(
                raw_lc.files["cover"], raw_lc.files["impervious"], raw_lc.files["canopy"],
                grid, node_mask, paths.simulation_ready,
                year=raw_lc.metadata.get("year", "selected"),
                coverage_report_path=paths.quality_control / "land_cover_coverage.json",
                manual_fill_values=spec.land_cover.options.get("manual_fill_values", {}),
            )

    ksat = porosity = suction = initial_saturation = None
    soil_paths: dict[str, Path] = {}
    soil_scalars: dict[str, float] = {}
    if spec.soil.enabled:
        with timings.stage("process_soil"):
            soil_raw = raw.require("soil")
            soil_source = (
                {
                    key: soil_raw.files[key]
                    for key in ("permeability", "porosity", "residual", "alpha")
                }
                if spec.soil.provider == "soilgrids"
                else soil_raw.files["vector"]
            )
            ksat, porosity, suction, initial_saturation, soil_paths, soil_scalars = prepare_ssurgo(
                soil_source, grid, node_mask, paths.simulation_ready,
                float(spec.soil.options.get("initial_saturation", 0.5)),
                coverage_report_path=paths.quality_control / "soil_coverage.json",
                manual_fill_values=spec.soil.options.get("manual_fill_values", {}),
                residual_porosity_tolerance=float(
                    spec.soil.options.get("residual_porosity_tolerance", 0.0)
                ),
            )

    rainfall_path = None
    rainfall_summary = accumulation = rainfall_times = None
    if spec.rainfall.enabled:
        with timings.stage("process_rainfall"):
            rainfall_path = paths.simulation_ready / "rainfall_ats.h5"
            rainfall_summary, accumulation, rainfall_times = prepare_aorc_event(
                raw.require("rainfall").files["netcdf"],
                paths.processed / "rainfall_selected_event.nc",
                rainfall_path, grid, cell_mask,
                spec.rainfall.options["start_utc"], spec.rainfall.options["end_utc"],
                coverage_report_path=paths.quality_control / "rainfall_coverage.json",
                uncovered_policy=str(
                    spec.rainfall.options.get("uncovered_policy", "zero")
                ),
                precipitation_variable=str(
                    spec.rainfall.options.get("precipitation_variable", "APCP_surface")
                ),
            )

    inflow_table = None
    inflow_plot_data = None
    if spec.inflows.enabled:
        with timings.stage("process_inflows"):
            inflow_table, inflow_plot_data = _prepare_inflows(spec.inflows, raw, grid, paths)

    surge_sampled = surge_stage_path = None
    if spec.coastal.enabled:
        with timings.stage("process_coastal"):
            surge_sampled, surge_stage_path = _prepare_coastal(
                spec.coastal, raw, grid, paths, original_dem, node_mask
            )

    drainage_map = drainage_controls = drainage_map_path = drainage_table_path = None
    if spec.drainage.enabled:
        with timings.stage("process_drainage"):
            drainage_map, drainage_controls, drainage_map_path, drainage_table_path = _prepare_drainage(
                spec.drainage, raw, grid, node_mask, impervious, paths
            )

    if spec.plots.processed_products:
        with timings.stage("plot_processed_products"):
            _save_plots(
                plot_dem_domain(original_dem, final_dem, node_mask, grid, spec.domain.wall_elevation_m),
                paths, "processed_dem", spec,
            )
            fig, ax = plt.subplots(figsize=(7.2, 5.4), constrained_layout=True)
            shown_initial = np.where(node_mask, initial_depth, np.nan)
            image = ax.imshow(
                shown_initial,
                extent=(grid.xmin, grid.xmax, grid.ymin, grid.ymax),
                origin="upper", cmap="Blues", vmin=0,
            )
            ax.set(
                title="QC-approved initial water depth received by RIFT",
                xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal",
            )
            fig.colorbar(image, ax=ax, label="Water depth (m)")
            _save_plots(fig, paths, "processed_initial_state", spec)
            if spec.river_bathymetry.apply_bathymetry:
                _save_plots(
                    plot_river_bathymetry(original_dem + burn_depth, original_dem, burn_depth, grid),
                    paths, "processed_river_bathymetry", spec,
                )
            if carved.any():
                _save_plots(
                    plot_outlet_diagnostics(
                        final_dem, carved, outlet_features, grid, spec.domain.wall_elevation_m
                    ),
                    paths, "processed_outlet", spec,
                )
            if manning is not None:
                _save_plots(plot_nlcd(cover, impervious, canopy, manning, grid, node_mask), paths, "processed_land_cover", spec)
            if ksat is not None:
                _save_plots(plot_soils(ksat, porosity, suction, initial_saturation, grid, node_mask), paths, "processed_soil", spec)
            if rainfall_summary is not None:
                _save_plots(plot_aorc(rainfall_summary, accumulation, grid), paths, "processed_rainfall", spec)
            if inflow_plot_data:
                _save_plots(
                    plot_hydrographs(inflow_plot_data), paths, "processed_inflows", spec
                )
            if surge_sampled is not None:
                fig, ax = plt.subplots(figsize=(6.4, 3.8), constrained_layout=True)
                ax.plot(
                    surge_sampled["time_s"].to_numpy(dtype=float) / 3600.0,
                    surge_sampled["stage_m_navd88"].to_numpy(dtype=float),
                    color="#0072B2",
                )
                ax.set(
                    title="QC-approved coastal-stage forcing received by RIFT",
                    xlabel="Time (h)", ylabel="Stage (m, NAVD88)",
                )
                _save_plots(fig, paths, "processed_coastal_stage", spec)
            if drainage_map is not None:
                _save_plots(plot_drainage(drainage_map, drainage_controls, grid), paths, "processed_drainage", spec)

    with timings.stage("validate_rift_inputs"):
        extra = {}
        if manning is not None:
            extra["Manning roughness"] = manning
        if ksat is not None:
            extra.update({"saturated conductivity": ksat, "porosity": porosity,
                          "suction": suction, "initial saturation": initial_saturation})
        report = validate_case(
            grid, final_dem, node_mask,
            wall_elevation_m=spec.domain.wall_elevation_m if spec.domain.outside_dem_mode == "wall" else None,
            rainfall_path=rainfall_path,
            inflow_table=inflow_table,
            drainage_map=drainage_map,
            drainage_parameters=drainage_controls,
            carved_outlets=carved,
            extra_rasters=extra,
        )
        qc_json_path, _ = _write_qc(report, paths)
        report.raise_for_errors()

    with timings.stage("write_rift_configuration"):
        inputs: dict[str, str | float | int | None] = {
            "domain.coordinates": "projected_m",
            "domain.square_cells": 1,
            "domain.dem": _rel(paths.root, dem_path),
            "initial.water_depth": _rel(paths.root, initial_depth_path),
            "surface.manning_n": _rel(paths.root, land_paths["manning"]) if land_paths else None,
            "soil.saturated_conductivity": _rel(paths.root, soil_paths["ksat"]) if soil_paths else None,
            "soil.suction_head_m": soil_scalars.get("soil.suction_head_m"),
            "soil.moisture_deficit": soil_scalars.get("soil.moisture_deficit"),
            "soil.initial_saturation": _rel(paths.root, soil_paths["initial_saturation"]) if soil_paths else None,
            "soil.root_depth_m": float(spec.soil.options.get("root_depth_m", 0.5)) if spec.soil.enabled else None,
            "soil.drainage_time_s": float(spec.soil.options.get("drainage_time_s", 172800)) if spec.soil.enabled else None,
            "soil.residual_saturation": soil_scalars.get("soil.residual_saturation"),
            "forcing.rainfall_hdf5": _rel(paths.root, rainfall_path) if rainfall_path else None,
            "forcing.rainfall_group": "precipitation rain [m s^-1]" if rainfall_path else None,
            "forcing.rainfall_time_dataset": "time [s]" if rainfall_path else None,
            "forcing.rainfall_interpolation": "step" if rainfall_path else None,
            "forcing.rainfall_relative_time": 1 if rainfall_path else None,
            "inflows.table": _rel(paths.root, inflow_table) if inflow_table else None,
            "drainage.infrastructure_map": _rel(paths.root, drainage_map_path) if drainage_map_path else None,
            "drainage.parameter_table": _rel(paths.root, drainage_table_path) if drainage_table_path else None,
            "forcing.surge_grid_prefix": (
                _rel(paths.root, paths.simulation_ready / "surge" / "surge")
                if surge_sampled is not None else None
            ),
            "forcing.surge_grid_interval_s": int(spec.coastal.options.get("interval_hours", 1)) * 3600 if surge_sampled is not None else None,
            "forcing.surge_grid_end_time_s": float(surge_sampled.time_s.iloc[-1]) if surge_sampled is not None else None,
        }
        modules = {
            "rainfall": rainfall_path is not None,
            "inflows": inflow_table is not None,
            "infiltration": bool(soil_paths),
            "dynamic_soil": bool(soil_paths),
            "urban_drainage": drainage_map_path is not None,
            "external_drainage": False,
            "surge": surge_sampled is not None,
        }
        config_path = build_rift_config(paths.root / "rift_case.cfg", spec.simulation, inputs, modules)
        write_run_instructions(paths.root / "RUN.md", config_path.name)

    input_status_rows = []
    for name, product in raw.products.items():
        native_report = paths.quality_control / "input_qc" / f"{name}.json"
        status = "PASSED"
        processed_report = None
        if name == "land_cover":
            processed_report = paths.quality_control / "land_cover_coverage.json"
        elif name == "soil":
            processed_report = paths.quality_control / "soil_coverage.json"
        elif name == "rainfall":
            processed_report = paths.quality_control / "rainfall_coverage.json"
        elif name == "initial_state":
            processed_report = paths.quality_control / "initial_state_coverage.json"
        if processed_report is not None and processed_report.is_file():
            status = json.loads(processed_report.read_text(encoding="utf-8")).get(
                "status", status
            )
        input_status_rows.append({
            "input": name,
            "provider": product.provider,
            "status": status,
            "native_qc_report": str(native_report),
            "processed_qc_report": str(processed_report) if processed_report else None,
        })
    input_status_json = paths.quality_control / "input_status_summary.json"
    input_status_json.write_text(
        json.dumps(input_status_rows, indent=2), encoding="utf-8"
    )
    pd.DataFrame(input_status_rows).to_csv(
        paths.quality_control / "input_status_summary.csv", index=False
    )

    manifest_path = write_manifest(paths.root / "case_manifest.json", {
        "schema": "riftprep-case-v2",
        "case_name": spec.case_name,
        "status": "complete",
        "configuration_schema": "rift-uconn-v1",
        "grid": grid.as_dict(),
        "raw_products": {
            name: {"provider": item.provider, "files": {k: str(v) for k, v in item.files.items()},
                   "metadata": item.metadata}
            for name, item in raw.products.items()
        },
        "rift_config": str(config_path),
        "qc_report": str(qc_json_path),
        "input_status_summary": str(input_status_json),
        "wall_clock": str(paths.timings / "wall_clock.csv"),
        "raw_product_provenance": str(paths.provenance / "raw_products.json"),
        "acquisition_events": str(paths.provenance / "acquisition_events.json"),
        "run_provenance_manifest": str(
            paths.provenance / "run_manifest.json"
        ),
        "directory_contract": paths.directory_contract(),
    })
    return CaseResult(
        paths.root, config_path, manifest_path, qc_json_path,
        paths.timings / "wall_clock.csv",
        paths.provenance / "raw_products.json",
        True,
    )


def run_case(
    spec_or_path: CaseSpec | str | Path,
    *,
    _downloaded: RawDownloadResult | None = None,
) -> CaseResult:
    """Run one provenance-tracked workflow attempt and preserve terminal state."""
    spec = (
        load_case(spec_or_path)
        if isinstance(spec_or_path, (str, Path))
        else spec_or_path
    )
    if _downloaded is not None:
        paths = _downloaded.paths
        if _downloaded.run_id and _downloaded.run_started_utc:
            run_manifest = RunManifestWriter.reopen(
                spec, paths, _downloaded.run_id, _downloaded.run_started_utc
            )
        else:
            run_manifest = RunManifestWriter(
                spec, paths, preexisting_case=True
            )
    else:
        preexisting_case = spec.case_root.exists()
        paths = prepare_case_directory(spec.case_root, spec.cache.mode)
        run_manifest = RunManifestWriter(
            spec, paths, preexisting_case=preexisting_case
        )
    try:
        result = _run_case_impl(
            spec, _downloaded=_downloaded, _paths=paths
        )
    except BaseException as error:
        run_manifest.fail(error)
        raise
    run_manifest.complete()
    return result
