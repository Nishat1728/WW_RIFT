"""Read-only preflight for the locked 22-case RIFT input-generation matrix.

The script writes configs and a preflight report only.  It never creates a
case output directory and never submits a Slurm job.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riftprep.spec import load_case


SCHEMA = "riftprep-locked-22-input-preflight-v1"
LEVELS = (2, 4, 8, 12)
EXPECTED_COUNTS = {2: 2, 4: 4, 8: 6, 12: 10}
EXPECTED_CASES = sum(EXPECTED_COUNTS.values())
EVENT_START = "2023-06-10T00:00:00Z"
EVENT_END = "2023-06-10T06:00:00Z"
CELL_SIZE_M = 30.0
DOWNLOAD_BUFFER_CELLS = 2
STORAGE_MARGIN = 1.25
STORAGE_RESERVE_BYTES = 10 * 1024**3
CONSERVATIVE_BYTES_PER_NODE = 751.30

# These values are explicit, previously authorized benchmark choices.  They
# apply only at source-null/unsupported cells; valid downloaded cells remain.
MANUAL_LAND_COVER = {
    "cover": 43,
    "impervious": 0,
    "canopy": 50,
}
MANUAL_SOIL = {
    "permeability": 1.0e-12,
    "porosity": 0.42,
    "residual": 0.08,
    "alpha": 2.0e-4,
}

BASELINES = {
    "huc12_blackledge": {
        "nodes": 243_225,
        "case_bytes": 182_735_687,
        "max_rss_bytes": 2_034_056 * 1024,
        "wall_seconds": 76.99855082994327,
    },
    "huc8_lower_connecticut": {
        "nodes": 5_948_470,
        "case_bytes": 3_234_281_554,
        "max_rss_bytes": 8_053_540 * 1024,
        "wall_seconds": 437.824802279938,
    },
    "huc4_connecticut": {
        "nodes": 64_947_672,
        "case_bytes": 39_471_732_212,
        "max_rss_bytes": 53_771_940 * 1024,
        "wall_seconds": 5_594.396128389984,
    },
    "huc2_new_england_resume": {
        "nodes": 488_541_888,
        "case_bytes": 249_360_676_731,
        "max_rss_bytes": 142_459_684 * 1024,
        "wall_seconds": 20_215.874765661545,
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)
    return path


def atomic_json(path: Path, value: Any) -> Path:
    return atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def verify_boundary_bundle(boundary_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    boundary_root = boundary_root.resolve()
    artifact_path = boundary_root / "provenance" / "artifact_manifest.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    for key in ("matrix_json", "matrix_csv", "source_metadata"):
        record = artifact[key]
        path = boundary_root / record["path"]
        if path.stat().st_size != int(record["bytes"]) or sha256_file(path) != record["sha256"]:
            raise ValueError(f"Boundary artifact checksum mismatch: {path}")
    matrix_path = boundary_root / artifact["matrix_json"]["path"]
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if (
        matrix.get("status") != "PASSED"
        or matrix.get("summary", {}).get("passed") != EXPECTED_CASES
    ):
        raise ValueError(
            f"Locked boundary matrix is not a {EXPECTED_CASES}-case PASSED result."
        )
    if matrix.get("selection_manifest", {}).get("replacement_performed"):
        raise ValueError("Boundary matrix reports a replacement selection.")
    results = matrix["results"]
    if len(results) != EXPECTED_CASES or any(
        result.get("status") != "PASSED" for result in results
    ):
        raise ValueError("Boundary matrix has a missing or failed case.")
    return matrix, results


def source_check(
    name: str,
    output_root: Path,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    validator: Callable[[requests.Response, bytes], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = utc_now()
    path = output_root / "source_checks" / f"{name}.response"
    attempts = []
    last_error: Exception | None = None
    for attempt in range(1, 4):
        response = None
        try:
            response = requests.request(
                method,
                url,
                params=params,
                data=data,
                timeout=120,
                headers={"User-Agent": "riftprep-locked-matrix-preflight/1"},
            )
            body = response.content
            attempt_path = output_root / "source_checks" / f"{name}.attempt-{attempt:02d}.response"
            atomic_bytes(attempt_path, body)
            atomic_bytes(path, body)
            attempt_record = {
                "attempt": attempt,
                "http_status": response.status_code,
                "response_path": relative(attempt_path, output_root),
                "response_bytes": len(body),
                "response_sha256": sha256_bytes(body),
            }
            attempts.append(attempt_record)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            details = validator(response, body) if validator else {}
            return {
                "name": name,
                "status": "PASSED",
                "checked_utc": started,
                "method": method,
                "url": url,
                "params": params or {},
                "request_data": data or {},
                "attempts": attempts,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "response_path": relative(path, output_root),
                "response_bytes": len(body),
                "response_sha256": sha256_bytes(body),
                "details": details,
            }
        except Exception as error:
            last_error = error
            if attempts:
                attempts[-1]["error"] = f"{type(error).__name__}: {error}"
            deterministic_http_error = (
                response is not None
                and response.status_code >= 400
                and response.status_code not in {429, 500, 502, 503, 504}
            )
            if deterministic_http_error:
                break
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    return {
        "name": name,
        "status": "FAILED",
        "checked_utc": started,
        "method": method,
        "url": url,
        "params": params or {},
        "request_data": data or {},
        "attempts": attempts,
        "response_path": relative(path, output_root) if path.is_file() else None,
        "response_bytes": path.stat().st_size if path.is_file() else 0,
        "response_sha256": sha256_file(path) if path.is_file() else None,
        "error_type": type(last_error).__name__,
        "error": str(last_error),
    }


def json_validator(required: tuple[str, ...]) -> Callable[[requests.Response, bytes], dict[str, Any]]:
    def validate(_: requests.Response, body: bytes) -> dict[str, Any]:
        payload = json.loads(body)
        if "error" in payload:
            raise RuntimeError(payload["error"])
        missing = [key for key in required if payload.get(key) is None]
        if missing:
            raise ValueError(f"Missing provider metadata fields: {missing}")
        return {key: payload.get(key) for key in required}
    return validate


def ssurgo_validator(_: requests.Response, body: bytes) -> dict[str, Any]:
    payload = json.loads(body)
    table = payload.get("Table")
    if not isinstance(table, list) or not table:
        raise ValueError("SSURGO preflight query returned no Table rows.")
    return {"table_rows": len(table), "first_row": table[0]}


def text_nonempty_validator(_: requests.Response, body: bytes) -> dict[str, Any]:
    if len(body) < 100:
        raise ValueError(f"Provider response is unexpectedly small: {len(body)} bytes")
    return {"nonempty": True}


def check_sources(preflight_root: Path) -> list[dict[str, Any]]:
    try:
        from pygeoogc import ServiceURL
        nlcd_url = ServiceURL().wms.mrlc
    except Exception:
        nlcd_url = "https://www.mrlc.gov/geoserver/mrlc_display/wms"
    return [
        source_check(
            "usgs_3dep", preflight_root, "GET",
            "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
            params={"f": "json"}, validator=json_validator(("name", "pixelType")),
        ),
        source_check(
            "usgs_3dhp", preflight_root, "GET",
            "https://3dhp.nationalmap.gov/arcgis/rest/services/usgs_3dhp_all/FeatureServer/50",
            params={"f": "json"}, validator=json_validator(("name", "geometryType")),
        ),
        source_check(
            "usgs_wbd", preflight_root, "GET",
            "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/FeatureServer",
            params={"f": "json"}, validator=json_validator(("serviceDescription", "layers")),
        ),
        source_check(
            "nlcd_wms", preflight_root, "GET", nlcd_url,
            params={"service": "WMS", "request": "GetCapabilities"},
            validator=text_nonempty_validator,
        ),
        source_check(
            "ssurgo_sda", preflight_root, "POST",
            "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest",
            data={"FORMAT": "JSON", "QUERY": "SELECT TOP 1 mukey FROM mapunit"},
            validator=ssurgo_validator,
        ),
        source_check(
            "aorc_2023_zarr", preflight_root, "GET",
            "https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/2023.zarr/.zmetadata",
            validator=text_nonempty_validator,
        ),
    ]


def package_versions() -> dict[str, str | None]:
    names = (
        "riftprep", "watershed-workflow", "geopandas", "rasterio", "pyproj",
        "py3dep", "pygeohydro", "s3fs", "xarray", "h5py", "nbclient", "nbformat",
    )
    versions = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def resource_class(nodes: int) -> dict[str, Any]:
    if nodes <= 3_000_000:
        return {"name": "small", "cpus": 16, "memory_gb": 32, "time": "04:00:00", "max_concurrent": 4}
    if nodes <= 30_000_000:
        return {"name": "medium", "cpus": 16, "memory_gb": 64, "time": "08:00:00", "max_concurrent": 2}
    if nodes <= 150_000_000:
        return {"name": "large", "cpus": 16, "memory_gb": 128, "time": "12:00:00", "max_concurrent": 1}
    return {"name": "xlarge", "cpus": 16, "memory_gb": 384, "time": "12:00:00", "max_concurrent": 1}


def make_config(case: dict[str, Any], boundary_path: Path, case_output_parent: Path, cache_mode: str) -> dict[str, Any]:
    case_name = f"locked30_huc{case['level']}_{case['huc']}"
    return {
        "case_name": case_name,
        "output_root": str(case_output_parent),
        "domain": {
            "mode": "shapefile",
            "vector_path": str(boundary_path),
            "vector_layer": "boundary",
            "target_crs": "EPSG:5070",
            "cell_size_m": 30.0,
            "download_buffer_cells": DOWNLOAD_BUFFER_CELLS,
            "boundary_rule": "cell_center",
            "outside_dem_mode": "wall",
            "wall_elevation_m": 9999,
        },
        "outlets": {
            "source": "auto_hydrography_lowest",
            "method": "open_corridor",
            "elevation_mode": "terrain_matched",
            "default_width_m": 120,
        },
        "dem": {
            "enabled": True,
            "provider": "watershed_workflow_3dep",
            "options": {"resolution_m": 30, "tile_degrees": 0.5, "max_attempts": 8},
        },
        "initial_state": {"enabled": True, "provider": "dry_zero", "options": {}},
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
                "manual_fill_values": MANUAL_LAND_COVER,
                "manual_dimension_constants": MANUAL_LAND_COVER,
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
                "manual_fill_values": MANUAL_SOIL,
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
        "cache": {"mode": cache_mode},
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


def git_head(repository: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def run(
    repository: Path,
    boundary_root: Path,
    target_output_root: Path,
    preflight_root: Path,
) -> dict[str, Any]:
    repository = repository.resolve()
    boundary_root = boundary_root.resolve()
    target_output_root = target_output_root.resolve()
    preflight_root = preflight_root.resolve()
    if preflight_root.exists() and any(preflight_root.iterdir()):
        raise FileExistsError(f"Preflight output must be missing or empty: {preflight_root}")
    preflight_root.mkdir(parents=True, exist_ok=True)
    matrix, boundary_results = verify_boundary_bundle(boundary_root)
    source_checks = check_sources(preflight_root)
    runtime_sources = {"usgs_3dep", "usgs_3dhp", "nlcd_wms", "ssurgo_sda", "aorc_2023_zarr"}
    for check in source_checks:
        check["required_for_input_generation"] = check["name"] in runtime_sources
    case_output_parent = target_output_root / "cases"
    configs_root = preflight_root / "configs"
    rows = []
    for index, result in enumerate(sorted(boundary_results, key=lambda row: (int(row["level"]), row["huc"]))):
        level = int(result["level"])
        code = result["huc"]
        boundary_path = boundary_root / result["artifacts"]["processed_boundary"]
        frame = gpd.read_file(boundary_path, layer="boundary")
        if frame.crs is None or frame.crs.to_epsg() != 5070 or frame.empty:
            raise ValueError(f"Validated boundary is not a nonempty EPSG:5070 layer: {boundary_path}")
        xmin, ymin, xmax, ymax = (float(value) for value in frame.total_bounds)
        node_columns = int(math.ceil((xmax - xmin) / CELL_SIZE_M)) + 2 * DOWNLOAD_BUFFER_CELLS + 1
        node_rows = int(math.ceil((ymax - ymin) / CELL_SIZE_M)) + 2 * DOWNLOAD_BUFFER_CELLS + 1
        nodes = node_columns * node_rows
        estimated_bytes = int(math.ceil(nodes * CONSERVATIVE_BYTES_PER_NODE))
        estimated_bytes_with_margin = int(math.ceil(estimated_bytes * STORAGE_MARGIN))
        resources = resource_class(nodes)
        case_name = f"locked30_huc{level}_{code}"
        initial_config = make_config(result, boundary_path, case_output_parent, "fresh")
        initial_path = configs_root / "initial" / f"{case_name}.yaml"
        initial_path.parent.mkdir(parents=True, exist_ok=True)
        initial_path.write_text(yaml.safe_dump(initial_config, sort_keys=False), encoding="utf-8")
        load_case(initial_path)
        expected_case_root = case_output_parent / case_name
        row = {
            "global_index": index,
            "level": level,
            "huc": code,
            "case_name": case_name,
            "boundary_path": str(boundary_path),
            "boundary_sha256": result["artifacts"]["processed_boundary_sha256"],
            "estimated_node_columns": node_columns,
            "estimated_node_rows": node_rows,
            "estimated_nodes": nodes,
            "estimated_case_bytes": estimated_bytes,
            "estimated_case_bytes_with_margin": estimated_bytes_with_margin,
            "resource_class": resources["name"],
            "cpus": resources["cpus"],
            "memory_gb": resources["memory_gb"],
            "time_limit": resources["time"],
            "max_concurrent_for_class": resources["max_concurrent"],
            "initial_config": relative(initial_path, preflight_root),
            "initial_config_sha256": sha256_file(initial_path),
            "configs_schema_validated": True,
            "expected_case_root": str(expected_case_root),
            "expected_run_manifest": str(expected_case_root / "quality_control/provenance/run_manifest.json"),
            "expected_run_manifest_history": str(expected_case_root / "quality_control/provenance/run_manifests"),
            "expected_log_pattern": str(target_output_root / "logs" / f"{case_name}_%A_%a.out"),
            "case_root_exists_before_submission": expected_case_root.exists(),
        }
        rows.append(row)

    groups = []
    for name in ("small", "medium", "large", "xlarge"):
        members = [row for row in rows if row["resource_class"] == name]
        if not members:
            continue
        group_resources = resource_class(max(row["estimated_nodes"] for row in members))
        groups.append({
            "resource_class": name,
            "case_count": len(members),
            "array_bounds": f"0-{len(members)-1}%{group_resources['max_concurrent']}",
            "cpus_per_task": group_resources["cpus"],
            "memory_gb": group_resources["memory_gb"],
            "time_limit": group_resources["time"],
            "global_indices": [row["global_index"] for row in members],
            "hucs": [row["huc"] for row in members],
        })

    target_parent = target_output_root.parent
    usage = shutil.disk_usage(target_parent)
    total_estimated = sum(row["estimated_case_bytes"] for row in rows)
    total_with_margin = sum(row["estimated_case_bytes_with_margin"] for row in rows)
    required_free = total_with_margin + STORAGE_RESERVE_BYTES
    output_isolated = not target_output_root.exists() and all(
        not row["case_root_exists_before_submission"] for row in rows
    )
    source_pass = all(
        check["status"] == "PASSED"
        for check in source_checks
        if check["required_for_input_generation"]
    )
    storage_pass = usage.free >= required_free
    package_info = package_versions()
    packages_pass = all(package_info[name] is not None for name in (
        "watershed-workflow", "geopandas", "rasterio", "pyproj", "py3dep",
        "pygeohydro", "s3fs", "xarray", "h5py", "nbclient", "nbformat",
    ))
    blockers = []
    if not storage_pass:
        blockers.append({
            "code": "INSUFFICIENT_STORAGE",
            "message": (
                f"Required free space with margin/reserve is {required_free} bytes, "
                f"but only {usage.free} bytes are available on {target_parent}."
            ),
            "required_free_bytes": required_free,
            "available_free_bytes": usage.free,
            "shortfall_bytes": required_free - usage.free,
        })
    if not source_pass:
        blockers.append({
            "code": "SOURCE_UNAVAILABLE",
            "failed_sources": [
                check["name"] for check in source_checks
                if check["required_for_input_generation"] and check["status"] != "PASSED"
            ],
        })
    if not output_isolated:
        blockers.append({"code": "OUTPUT_COLLISION", "target_output_root": str(target_output_root)})
    if not packages_pass:
        blockers.append({
            "code": "MISSING_PACKAGES",
            "packages": [name for name, version in package_info.items() if version is None],
        })

    report = {
        "schema": SCHEMA,
        "created_utc": utc_now(),
        "status": "PASSED" if not blockers else "BLOCKED",
        "submission_allowed": not blockers,
        "submission_performed": False,
        "job_ids": [],
        "scope": "complete input generation only; no RIFT simulation",
        "locked_selection": {
            "fixed_seed": matrix["fixed_seed"],
            "case_count": matrix["selection_manifest"]["case_count"],
            "case_digest_sha256": matrix["selection_manifest"]["locked_case_digest_sha256"],
            "selection_manifest_sha256": matrix["selection_manifest"]["sha256_after"],
            "replacement_allowed": False,
            "replacement_performed": False,
        },
        "validated_boundaries": {
            "root": str(boundary_root),
            "matrix_status": matrix["status"],
            "passed": matrix["summary"]["passed"],
            "failed": matrix["summary"]["failed"],
        },
        "scientific_settings": {
            "native_dem_product_m": 30,
            "rift_grid_resolution_m": 30,
            "event_start_utc": EVENT_START,
            "event_end_utc": EVENT_END,
            "initial_state": "dry_zero",
            "dem": "watershed_workflow_3dep",
            "hydrography": "usgs_3dhp",
            "land_cover": "watershed_workflow_nlcd 2021 L48",
            "soil": "watershed_workflow_ssurgo",
            "rainfall": "watershed_workflow_aorc APCP_surface",
            "rainfall_uncovered_policy": "zero",
            "manual_land_cover_values": MANUAL_LAND_COVER,
            "manual_soil_values": MANUAL_SOIL,
            "valid_source_cells_replaced": False,
            "cache_mode_for_every_case": "fresh",
            "reuse_configs_created": False,
        },
        "source_checks": source_checks,
        "source_checks_passed": source_pass,
        "packages": package_info,
        "packages_passed": packages_pass,
        "repository": {
            "path": str(repository),
            "git_head": git_head(repository),
            "notebook_sha256": sha256_file(repository / "RIFT_Input_Builder.ipynb"),
            "workflow_sha256": sha256_file(repository / "riftprep/workflow.py"),
            "config_sha256": sha256_file(repository / "riftprep/config.py"),
        },
        "output_isolation": {
            "target_output_root": str(target_output_root),
            "target_exists": target_output_root.exists(),
            "all_case_roots_absent": all(not row["case_root_exists_before_submission"] for row in rows),
            "passed": output_isolated,
        },
        "resource_model": {
            "basis": "completed fresh 30 m HUC12/HUC8/HUC4 plus HUC2 resume evidence",
            "baselines": BASELINES,
            "conservative_bytes_per_node": CONSERVATIVE_BYTES_PER_NODE,
            "storage_margin": STORAGE_MARGIN,
            "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
            "estimated_total_nodes": sum(row["estimated_nodes"] for row in rows),
            "estimated_total_case_bytes": total_estimated,
            "estimated_total_case_bytes_with_margin": total_with_margin,
            "required_free_bytes": required_free,
            "filesystem_total_bytes": usage.total,
            "filesystem_used_bytes": usage.used,
            "filesystem_free_bytes": usage.free,
            "storage_passed": storage_pass,
        },
        "scheduler_plan": {
            "partition": "general",
            "constraint": "epyc128",
            "nodes_per_task": 1,
            "ntasks_per_job": 1,
            "general_partition_max_time": "12:00:00",
            "groups": groups,
            "fail_fast_isolation": "one config, case root, log, run manifest, and history directory per HUC",
            "submission_command_executed": False,
        },
        "cases": rows,
        "blockers": blockers,
    }
    report_path = preflight_root / "preflight_report.json"
    atomic_json(report_path, report)
    csv_path = preflight_root / "array_index_map.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    artifact_manifest = {
        "schema": f"{SCHEMA}-artifacts",
        "created_utc": utc_now(),
        "preflight_report": {
            "path": report_path.name,
            "bytes": report_path.stat().st_size,
            "sha256": sha256_file(report_path),
        },
        "array_index_map": {
            "path": csv_path.name,
            "bytes": csv_path.stat().st_size,
            "sha256": sha256_file(csv_path),
        },
        "initial_config_count": len(list((configs_root / "initial").glob("*.yaml"))),
        "reuse_config_count": 0,
    }
    atomic_json(preflight_root / "artifact_manifest.json", artifact_manifest)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--boundary-root", required=True)
    parser.add_argument("--target-output-root", required=True)
    parser.add_argument("--preflight-root", required=True)
    args = parser.parse_args()
    report = run(
        Path(args.repository), Path(args.boundary_root), Path(args.target_output_root),
        Path(args.preflight_root),
    )
    print(json.dumps({
        "schema": report["schema"],
        "status": report["status"],
        "submission_allowed": report["submission_allowed"],
        "submission_performed": report["submission_performed"],
        "resource_model": report["resource_model"],
        "scheduler_plan": report["scheduler_plan"],
        "blockers": report["blockers"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
