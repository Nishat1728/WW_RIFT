"""Run one member of the fresh six-domain, true-30-m Storrs matrix.

Every case selects the native 30 m 3DEP product and uses a 30 m projected
RIFT grid.  Results are written independently so Slurm jobs cannot overwrite
one another's timing evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riftprep.providers import get_huc_boundary
from riftprep.workflow import run_case


EVENT_START = "2023-06-10T00:00:00Z"
EVENT_END = "2023-06-10T06:00:00Z"
NATIVE_DEM_PRODUCT_M = 30
RIFT_RESOLUTION_M = 30.0

# Explicit user-authorized constants used only where the selected NLCD source
# is null or structurally invalid. Valid downloaded cells are never replaced.
HUC4_MANUAL_LAND_COVER = {
    "manual_fill_values": {"cover": 43, "canopy": 50},
}
HUC8_MANUAL_LAND_COVER = {
    "manual_fill_values": {"canopy": 50},
}
HUC2_MANUAL_LAND_COVER = {
    "manual_fill_values": {"cover": 43, "impervious": 0, "canopy": 50},
    "manual_dimension_constants": {"cover": 43, "impervious": 0, "canopy": 50},
}

# Explicit benchmark-operator choices used only at active SSURGO null cells.
# These are visible in every generated YAML and coverage report; the workflow
# itself never selects or derives fallback soil values.
BENCHMARK_MANUAL_SOIL = {
    "permeability": 1.0e-12,
    "porosity": 0.42,
    "residual": 0.08,
    "alpha": 2.0e-4,
}

COMMON = {
    "dem": {
        "enabled": True,
        "provider": "watershed_workflow_3dep",
        "options": {"resolution_m": 30, "tile_degrees": 0.5, "max_attempts": 8},
    },
    "hydrography": {
        "enabled": True,
        "provider": "usgs_3dhp",
        "options": {"tile_degrees": 1.0, "download_workers": 4},
    },
    "land_cover": {
        "enabled": True,
        "provider": "watershed_workflow_nlcd",
        "options": {
            "year": 2021,
            "location": "L48",
            "variables": ["cover", "impervious", "canopy"],
            "tile_degrees": 1.0,
            "tile_overlap_degrees": 0.02,
            "download_workers": 4,
            "max_attempts": 8,
        },
    },
    "soil": {
        "enabled": True,
        "provider": "watershed_workflow_ssurgo",
        "options": {
            "initial_saturation": 0.5,
            "residual_porosity_tolerance": 0.1,
            "root_depth_m": 0.5,
            "drainage_time_s": 172800,
            "tile_degrees": 0.25,
            "download_workers": 4,
            "max_attempts": 8,
            "request_timeout_seconds": 120,
            "max_subdivision_depth": 2,
            "manual_fill_values": BENCHMARK_MANUAL_SOIL,
        },
    },
    "rainfall": {
        "enabled": True,
        "provider": "watershed_workflow_aorc",
        "options": {
            "start_utc": EVENT_START,
            "end_utc": EVENT_END,
            "variables": ["APCP_surface"],
            "max_attempts": 8,
            "uncovered_policy": "zero",
        },
    },
    "inflows": {"enabled": False, "provider": "none"},
    "coastal": {"enabled": False, "provider": "none"},
    "drainage": {"enabled": False, "provider": "none"},
    "river_bathymetry": {"apply_bathymetry": False},
    "cache": {"mode": "fresh"},
    # Selected native products are plotted. Full 30 m processed grids are not
    # rendered because HUC2 contains roughly 530 million RIFT nodes.
    "plots": {
        "raw_products": True,
        "processed_products": False,
        "formats": ["png"],
        "dpi": 140,
    },
    "simulation": {
        "start_time_s": 0,
        "end_time_s": 21600,
        "output_interval_s": 3600,
    },
}

HUC_CASES = {
    "huc2_new_england": ("01", 2),
    "huc4_connecticut": ("0108", 4),
    "huc8_lower_connecticut": ("01080205", 8),
    "huc12_blackledge": ("010802050801", 12),
}

CASE_KEYS = [
    "huc2_new_england",
    "huc4_connecticut",
    "huc8_lower_connecticut",
    "huc12_blackledge",
    "user_shapefile_blackledge",
    "coordinate_box_storrs",
]


def _walled_domain(mode: str, **domain_options):
    return {
        "mode": mode,
        "target_crs": "EPSG:5070",
        "cell_size_m": RIFT_RESOLUTION_M,
        "download_buffer_cells": 2,
        "boundary_rule": "cell_center",
        "outside_dem_mode": "wall",
        "wall_elevation_m": 9999,
        **domain_options,
    }


def _outlet():
    return {
        "source": "auto_hydrography_lowest",
        "method": "open_corridor",
        "elevation_mode": "terrain_matched",
        "default_width_m": 120,
    }


def make_case(case_key: str, output_root: Path) -> dict:
    if case_key in HUC_CASES:
        code, level = HUC_CASES[case_key]
        case = {
            "case_name": f"fresh30_{case_key}",
            "domain": _walled_domain("huc", huc_code=code, huc_level=level),
            "outlets": _outlet(),
        }
    elif case_key == "user_shapefile_blackledge":
        # This deliberately performs a fresh WBD request to construct the
        # supplied polygon; its time is included in total_wall_seconds.
        supplied_dir = output_root / "supplied_domains"
        supplied_dir.mkdir(parents=True, exist_ok=True)
        supplied_path = supplied_dir / "blackledge_huc12_user_polygon.gpkg"
        if supplied_path.exists():
            supplied_path.unlink()
        boundary = get_huc_boundary("010802050801", 12).to_crs("EPSG:4326")
        geometry = (
            boundary.geometry.union_all()
            if hasattr(boundary.geometry, "union_all")
            else boundary.geometry.unary_union
        )
        gpd.GeoDataFrame(
            {"domain_id": ["blackledge_user_polygon"]},
            geometry=[geometry], crs="EPSG:4326",
        ).to_file(supplied_path, driver="GPKG")
        case = {
            "case_name": "fresh30_user_shapefile_blackledge",
            "domain": _walled_domain(
                "shapefile", vector_path=str(supplied_path), vector_layer=None
            ),
            "outlets": _outlet(),
        }
    elif case_key == "coordinate_box_storrs":
        case = {
            "case_name": "fresh30_coordinate_box_storrs",
            "domain": {
                "mode": "rectangle",
                "bounds": [-72.31, 41.77, -72.21, 41.85],
                "bounds_crs": "EPSG:4326",
                "target_crs": "auto_utm",
                "cell_size_m": RIFT_RESOLUTION_M,
                "download_buffer_cells": 2,
                "boundary_rule": "cell_center",
                "outside_dem_mode": "real_terrain",
            },
        }
    else:
        raise KeyError(case_key)
    payload = {**COMMON, **case, "output_root": str(output_root)}
    if case_key in {"huc8_lower_connecticut", "huc4_connecticut", "huc2_new_england"}:
        manual = {
            "huc8_lower_connecticut": HUC8_MANUAL_LAND_COVER,
            "huc4_connecticut": HUC4_MANUAL_LAND_COVER,
            "huc2_new_england": HUC2_MANUAL_LAND_COVER,
        }[case_key]
        payload["land_cover"] = {
            **COMMON["land_cover"],
            "options": {**COMMON["land_cover"]["options"], **manual},
        }
    return payload


def _directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _result_paths(output_root: Path, case_key: str):
    results = output_root / "benchmark_results"
    results.mkdir(parents=True, exist_ok=True)
    return results / f"{case_key}.json", results / f"{case_key}.csv"


def _validate_completed_case(case_root: Path) -> dict:
    timing_path = case_root / "quality_control" / "timings" / "wall_clock.csv"
    raw_catalog_path = (
        case_root / "quality_control" / "provenance" / "raw_products.json"
    )
    qc_path = case_root / "quality_control" / "qc_report.json"
    land_coverage_path = case_root / "quality_control" / "land_cover_coverage.json"
    timings = pd.read_csv(timing_path)
    grid = json.loads(
        (case_root / "quality_control" / "grid.json").read_text(encoding="utf-8")
    )
    if float(grid["resolution"]) != RIFT_RESOLUTION_M:
        raise AssertionError(f"RIFT grid is not 30 m: {grid['resolution']}")
    raw_catalog = json.loads(raw_catalog_path.read_text(encoding="utf-8"))
    land_coverage = json.loads(land_coverage_path.read_text(encoding="utf-8"))
    if not land_coverage.get("complete"):
        raise AssertionError(f"Land-cover coverage is incomplete: {land_coverage}")
    cover_class_quality = land_coverage.get("cover_class_quality", {})
    if cover_class_quality.get("raw_product_modified"):
        raise AssertionError("The raw NLCD cover product was modified")
    percentage_quality = land_coverage.get("percent_range_quality", {})
    if any(details.get("raw_product_modified") for details in percentage_quality.values()):
        raise AssertionError("A raw land-cover percentage product was modified")
    correction_maps = land_coverage.get("percentage_correction_policy", {}).get(
        "before_after_maps", {}
    )
    for stage in ("before", "after"):
        map_path = correction_maps.get(stage)
        if not map_path or not Path(map_path).is_file():
            raise AssertionError(
                f"Land-cover percentage {stage} correction map is missing: {map_path}"
            )
    canonical_name = f"3dep_{NATIVE_DEM_PRODUCT_M}m_native.tif"
    raw_dem_records = [
        item for item in raw_catalog
        if item["product"] == "dem" and Path(item["path"]).name == canonical_name
    ]
    if len(raw_dem_records) != 1:
        raise AssertionError(
            f"Expected one canonical raw DEM named {canonical_name}, "
            f"got {len(raw_dem_records)}"
        )
    raw_dem_path = Path(raw_dem_records[0]["path"])
    with rasterio.open(raw_dem_path) as source:
        raw_dem = {
            "path": str(raw_dem_path),
            "rows": source.height,
            "columns": source.width,
            "x_spacing": abs(float(source.transform.a)),
            "y_spacing": abs(float(source.transform.e)),
            "crs": str(source.crs),
        }
    dem_header = {}
    with (case_root / "rift_inputs" / "dem.asc").open("r", encoding="utf-8") as stream:
        for _ in range(6):
            key, value = stream.readline().split(maxsplit=1)
            dem_header[key.lower()] = value.strip()
    if float(dem_header["cellsize"]) != RIFT_RESOLUTION_M:
        raise AssertionError(f"RIFT DEM header is not 30 m: {dem_header}")
    return {
        "measured_stage_seconds": float(timings.wall_seconds.sum()),
        "boundary_seconds": float(
            timings.loc[timings.stage == "resolve_boundary", "wall_seconds"].sum()
        ),
        "raw_download_seconds": float(
            timings.loc[
                timings.stage.str.startswith("download_raw_"), "wall_seconds"
            ].sum()
        ),
        "processing_seconds": float(
            timings.loc[
                ~timings.stage.str.startswith("download_raw_")
                & (timings.stage != "resolve_boundary"),
                "wall_seconds",
            ].sum()
        ),
        "raw_artifacts": len(raw_catalog),
        "raw_dem": raw_dem,
        "rift_dem_header": dem_header,
        "rift_grid_rows": int(grid["ny"]) + 1,
        "rift_grid_columns": int(grid["nx"]) + 1,
        "rift_grid_nodes": (int(grid["ny"]) + 1) * (int(grid["nx"]) + 1),
        "case_bytes": _directory_bytes(case_root),
        "case_root": str(case_root),
        "timing_csv": str(timing_path),
        "qc_json": str(qc_path),
        "land_cover_coverage_json": str(land_coverage_path),
        "land_cover_invalid_cover_classes": {
            "invalid_active_nodes": int(
                cover_class_quality.get("invalid_active_nodes", 0)
            ),
            "unsupported_active_class_counts": cover_class_quality.get(
                "unsupported_active_class_counts", {}
            ),
            "non_integer_active_value_counts": cover_class_quality.get(
                "non_integer_active_value_counts", {}
            ),
            "raw_product_modified": cover_class_quality.get(
                "raw_product_modified"
            ),
        },
        "land_cover_over_100_corrections": {
            name: int(details.get("over_100_corrected_active_nodes", 0))
            for name, details in percentage_quality.items()
        },
        "land_cover_percentage_correction_maps": correction_maps,
    }


def run_one(case_key: str, output_root: Path) -> int:
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    json_path, csv_path = _result_paths(output_root, case_key)
    record = {
        "case": case_key,
        "status": "failed",
        "started_utc": started_utc,
        "native_dem_product_m": NATIVE_DEM_PRODUCT_M,
        "rift_grid_resolution_m": RIFT_RESOLUTION_M,
        "cache_mode": "fresh",
        "event_start_utc": EVENT_START,
        "event_end_utc": EVENT_END,
    }
    try:
        payload = make_case(case_key, output_root)
        record["manual_land_cover_options"] = {
            key: value
            for key, value in payload["land_cover"]["options"].items()
            if key in {"manual_fill_values", "manual_dimension_constants"}
        }
        config_dir = output_root / "matrix_configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"{case_key}.yaml"
        config_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        result = run_case(config_path)
        record.update({
            "status": "passed",
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "total_wall_seconds": time.perf_counter() - started,
            **_validate_completed_case(result.case_root),
        })
    except BaseException as error:
        record.update({
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "total_wall_seconds": time.perf_counter() - started,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        })
    json_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    pd.DataFrame([{key: json.dumps(value) if isinstance(value, dict) else value
                   for key, value in record.items()}]).to_csv(csv_path, index=False)
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "passed" else 1


def revalidate_existing(case_key: str, output_root: Path) -> int:
    json_path, csv_path = _result_paths(output_root, case_key)
    record = json.loads(json_path.read_text(encoding="utf-8"))
    case_root = output_root / f"fresh30_{case_key}"
    record.update(_validate_completed_case(case_root))
    record["status"] = "passed"
    record["post_run_revalidated_utc"] = datetime.now(timezone.utc).isoformat()
    for key in ("error_type", "error", "traceback"):
        record.pop(key, None)
    json_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    pd.DataFrame([{
        key: json.dumps(value) if isinstance(value, dict) else value
        for key, value in record.items()
    }]).to_csv(csv_path, index=False)
    print(json.dumps(record, indent=2))
    return 0


def aggregate(output_root: Path) -> int:
    result_dir = output_root / "benchmark_results"
    records = []
    for case_key in CASE_KEYS:
        path = result_dir / f"{case_key}.json"
        if path.is_file():
            records.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            records.append({"case": case_key, "status": "missing"})
    (output_root / "six_case_30m_summary.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8"
    )
    flat = []
    for record in records:
        flat.append({
            key: json.dumps(value) if isinstance(value, dict) else value
            for key, value in record.items() if key != "traceback"
        })
    pd.DataFrame(flat).to_csv(output_root / "six_case_30m_summary.csv", index=False)
    print(pd.DataFrame(flat).to_string(index=False))
    return 0 if all(item.get("status") == "passed" for item in records) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--case", choices=[*CASE_KEYS, "aggregate"], required=True)
    parser.add_argument("--revalidate-existing", action="store_true")
    args = parser.parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if args.case == "aggregate":
        return aggregate(output_root)
    if args.revalidate_existing:
        return revalidate_existing(args.case, output_root)
    return run_one(args.case, output_root)


if __name__ == "__main__":
    raise SystemExit(main())
