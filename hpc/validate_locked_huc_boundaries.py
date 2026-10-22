"""Acquire and validate WBD boundaries for a locked HUC selection manifest.

This is a boundary-only validation gate.  It never constructs a RIFT case,
downloads terrain/forcing, or invokes the RIFT Input Builder workflow.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import requests
from pyproj import CRS, Geod
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity


SCHEMA = "riftprep-locked-huc-boundary-validation-v1"
WBD_FEATURE_SERVER = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/FeatureServer"
LEVELS = (2, 4, 8, 12)
EXPECTED_COUNTS = {2: 2, 4: 4, 8: 6, 12: 10}
EXPECTED_CASES = sum(EXPECTED_COUNTS.values())
PROJECT_CRS = "EPSG:5070"
SOURCE_CRS = "EPSG:4326"
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
RESPONSE_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 6
MAX_WORKERS_LIMIT = 8
AREA_RELATIVE_TOLERANCE = 0.02
ROUNDTRIP_HAUSDORFF_TOLERANCE_DEGREES = 1.0e-7


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def atomic_json(path: Path, value: Any) -> Path:
    return atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    keep = {"content-type", "content-length", "etag", "last-modified", "date", "server"}
    return {key.lower(): value for key, value in headers.items() if key.lower() in keep}


def selected_cases(selection_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for level in LEVELS:
        rows = selection_manifest.get("selected", {}).get(str(level), [])
        if len(rows) != EXPECTED_COUNTS[level]:
            raise ValueError(
                f"Locked manifest contains {len(rows)} HUC{level} rows; "
                f"expected {EXPECTED_COUNTS[level]}."
            )
        for row in rows:
            code = str(row.get("huc", ""))
            if int(row.get("level", -1)) != level or len(code) != level or not code.isdigit():
                raise ValueError(f"Invalid locked HUC{level} selection row: {row}")
            cases.append(dict(row))
    codes = [row["huc"] for row in cases]
    if len(codes) != len(set(codes)):
        raise ValueError("Locked selection contains duplicate HUC codes.")
    if len(cases) != sum(EXPECTED_COUNTS.values()):
        raise ValueError(
            f"Locked selection does not contain exactly {EXPECTED_CASES} cases."
        )
    return cases


def locked_selection_digest(cases: Iterable[dict[str, Any]]) -> str:
    text = "".join(f"{row['level']}:{row['huc']}\n" for row in cases)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def validate_selection_bundle(selection_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[tuple[int, str], dict[str, Any]], dict[int, set[str]]]:
    selection_path = selection_path.expanduser().resolve()
    manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "riftprep-conus-huc-selection-v2":
        raise ValueError(f"Unexpected selection manifest schema: {manifest.get('schema')}")
    if int(manifest.get("fixed_seed", -1)) != 20261014:
        raise ValueError("The locked selection seed is not 20261014.")
    if not all(manifest.get("validation", {}).values()):
        raise ValueError("The locked selection manifest contains a failed validation flag.")
    selection_dir = selection_path.parent
    for key in ("source_metadata", "candidate_metadata"):
        record = manifest[key]
        path = selection_dir / record["path"]
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            raise ValueError(f"Locked {key} checksum mismatch: {path}")
    source_record = manifest["source_catalog"]
    source_path = selection_dir / source_record["path"]
    if not source_path.is_file() or sha256_file(source_path) != source_record["sha256"]:
        raise ValueError(f"Locked source catalog checksum mismatch: {source_path}")

    cases = selected_cases(manifest)
    wanted = {(int(row["level"]), row["huc"]) for row in cases}
    catalog: dict[tuple[int, str], dict[str, Any]] = {}
    all_codes: dict[int, set[str]] = {level: set() for level in LEVELS}
    with gzip.open(source_path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            level = int(row["level"])
            code = str(row["huc"])
            if level in all_codes:
                all_codes[level].add(code)
            key = (level, code)
            if key in wanted:
                if key in catalog:
                    raise ValueError(f"Duplicate selected code in source catalog: HUC{level} {code}")
                catalog[key] = row
    missing = sorted(wanted - set(catalog))
    if missing:
        raise ValueError(f"Selected HUCs missing from locked authoritative catalog: {missing}")
    return manifest, cases, catalog, all_codes


def request_fingerprint(url: str, params: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json({"url": url, "params": params}).encode("utf-8"))


def fetch_layer_metadata(level: int, output_root: Path, cache_mode: str) -> dict[str, Any]:
    url = f"{WBD_FEATURE_SERVER}/{level // 2}"
    params = {"f": "pjson"}
    path = output_root / "raw" / "source_layer_metadata" / f"huc{level}.json"
    provenance_path = path.with_suffix(".provenance.json")
    fingerprint = request_fingerprint(url, params)
    if cache_mode == "reuse" and path.is_file() and provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("request_fingerprint_sha256") != fingerprint:
            raise ValueError(f"Cached HUC{level} metadata query fingerprint mismatch.")
        if provenance.get("response_sha256") != sha256_file(path):
            raise ValueError(f"Cached HUC{level} metadata checksum mismatch.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        reused = True
    else:
        response = requests.get(url, params=params, timeout=RESPONSE_TIMEOUT_SECONDS, headers={"User-Agent": "riftprep-boundary-validation/1"})
        raw = response.content
        atomic_bytes(path, raw)
        response.raise_for_status()
        payload = json.loads(raw)
        if "error" in payload:
            raise RuntimeError(f"WBD HUC{level} metadata error: {payload['error']}")
        provenance = {
            "schema": f"{SCHEMA}-layer-source",
            "retrieved_utc": utc_now(),
            "authority": "U.S. Geological Survey Watershed Boundary Dataset",
            "url": url,
            "params": params,
            "request_fingerprint_sha256": fingerprint,
            "http_status": response.status_code,
            "response_headers": safe_headers(response.headers),
            "response_path": str(path.relative_to(output_root)),
            "response_bytes": len(raw),
            "response_sha256": sha256_bytes(raw),
            "raw_product_modified": False,
        }
        atomic_json(provenance_path, provenance)
        reused = False
    fields = {field["name"].lower() for field in payload.get("fields", [])}
    huc_field = f"huc{level}"
    required = {"objectid", "tnmid", "areasqkm", "states", "name", huc_field}
    if not required.issubset(fields):
        raise ValueError(f"WBD HUC{level} metadata lacks fields: {sorted(required - fields)}")
    if payload.get("geometryType") != "esriGeometryPolygon":
        raise ValueError(f"WBD HUC{level} layer is not polygonal: {payload.get('geometryType')}")
    return {
        "level": level,
        "layer_id": level // 2,
        "layer_name": payload.get("name"),
        "layer_url": url,
        "object_id_field": payload.get("objectIdField"),
        "geometry_type": payload.get("geometryType"),
        "source_spatial_reference": payload.get("sourceSpatialReference") or payload.get("extent", {}).get("spatialReference"),
        "metadata_path": str(path.relative_to(output_root)),
        "metadata_sha256": sha256_file(path),
        "request_fingerprint_sha256": fingerprint,
        "reused": reused,
    }


def fetch_boundary(case: dict[str, Any], output_root: Path, cache_mode: str) -> dict[str, Any]:
    level = int(case["level"])
    code = case["huc"]
    case_relative = Path("huc_boundaries") / f"huc{level}" / code
    case_root = output_root / "raw" / case_relative
    accepted_path = case_root / "raw_boundary.geojson"
    provenance_path = case_root / "provenance.json"
    url = f"{WBD_FEATURE_SERVER}/{level // 2}/query"
    field = f"huc{level}"
    params = {
        "where": f"{field}='{code}'",
        "outFields": "*",
        "returnGeometry": "true",
        "returnZ": "false",
        "returnM": "false",
        "outSR": "4326",
        "orderByFields": f"{field} ASC,objectid ASC",
        "resultRecordCount": "2",
        "f": "geojson",
    }
    fingerprint = request_fingerprint(url, params)
    if cache_mode == "reuse" and accepted_path.is_file() and provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("request_fingerprint_sha256") != fingerprint:
            raise ValueError(f"Cached boundary query fingerprint mismatch for HUC{level} {code}.")
        if provenance.get("response_sha256") != sha256_file(accepted_path):
            raise ValueError(f"Cached raw boundary checksum mismatch for HUC{level} {code}.")
        return {
            "case": case,
            "raw_path": accepted_path,
            "provenance_path": provenance_path,
            "reused": True,
            "attempts": provenance.get("attempts", []),
        }

    attempts = []
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = utc_now()
        response = None
        raw = b""
        try:
            response = requests.get(
                url,
                params=params,
                timeout=RESPONSE_TIMEOUT_SECONDS,
                headers={"User-Agent": "riftprep-boundary-validation/1"},
            )
            raw = response.content
            attempt_path = case_root / "attempts" / f"attempt-{attempt:02d}.response"
            atomic_bytes(attempt_path, raw)
            attempt_record = {
                "attempt": attempt,
                "started_utc": started,
                "completed_utc": utc_now(),
                "http_status": response.status_code,
                "response_headers": safe_headers(response.headers),
                "response_path": str(attempt_path.relative_to(output_root)),
                "response_bytes": len(raw),
                "response_sha256": sha256_bytes(raw),
            }
            attempts.append(attempt_record)
            if response.status_code in TRANSIENT_STATUS:
                raise RuntimeError(f"transient HTTP {response.status_code}")
            response.raise_for_status()
            payload = json.loads(raw)
            if "error" in payload:
                raise RuntimeError(f"ArcGIS error: {payload['error']}")
            if not payload.get("features"):
                raise LookupError("empty feature collection")
            atomic_bytes(accepted_path, raw)
            provenance = {
                "schema": f"{SCHEMA}-raw-boundary-provenance",
                "authority": "U.S. Geological Survey Watershed Boundary Dataset",
                "retrieved_utc": utc_now(),
                "requested_huc": code,
                "requested_level": level,
                "url": url,
                "params": params,
                "request_fingerprint_sha256": fingerprint,
                "attempts": attempts,
                "accepted_attempt": attempt,
                "response_path": str(accepted_path.relative_to(output_root)),
                "response_bytes": len(raw),
                "response_sha256": sha256_bytes(raw),
                "raw_product_modified": False,
                "cache_mode": cache_mode,
            }
            atomic_json(provenance_path, provenance)
            return {
                "case": case,
                "raw_path": accepted_path,
                "provenance_path": provenance_path,
                "reused": False,
                "attempts": attempts,
            }
        except Exception as error:
            last_error = error
            if attempts:
                attempts[-1]["error"] = f"{type(error).__name__}: {error}"
            deterministic_http_error = response is not None and response.status_code not in TRANSIENT_STATUS and response.status_code >= 400
            if deterministic_http_error:
                break
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(30.0, 2.0 ** (attempt - 1)))
    failure = {
        "schema": f"{SCHEMA}-raw-boundary-provenance",
        "authority": "U.S. Geological Survey Watershed Boundary Dataset",
        "requested_huc": code,
        "requested_level": level,
        "url": url,
        "params": params,
        "request_fingerprint_sha256": fingerprint,
        "attempts": attempts,
        "status": "FAILED",
        "error": f"{type(last_error).__name__}: {last_error}",
        "raw_product_modified": False,
        "cache_mode": cache_mode,
    }
    atomic_json(provenance_path, failure)
    raise RuntimeError(f"HUC{level} {code} boundary acquisition failed: {last_error}")


def _polygon_parts(geometry: Any) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        parts: list[Polygon] = []
        for item in geometry.geoms:
            parts.extend(_polygon_parts(item))
        return parts
    return []


def _properties_casefold(feature: dict[str, Any]) -> dict[str, Any]:
    return {str(key).lower(): value for key, value in feature.get("properties", {}).items()}


def validate_boundary(
    fetched: dict[str, Any],
    output_root: Path,
    catalog_row: dict[str, Any],
    all_codes: dict[int, set[str]],
) -> dict[str, Any]:
    case = fetched["case"]
    level = int(case["level"])
    code = case["huc"]
    raw_path = fetched["raw_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    features = payload.get("features") or []
    if not features:
        raise ValueError("The saved raw response contains no features.")
    properties = [_properties_casefold(feature) for feature in features]
    returned_codes = sorted({str(item.get(f"huc{level}", "")) for item in properties})
    returned_object_ids = sorted({int(item["objectid"]) for item in properties if item.get("objectid") is not None})
    returned_tnmids = sorted({str(item.get("tnmid", "")) for item in properties})
    raw_geometries = [shape(feature["geometry"]) for feature in features if feature.get("geometry")]
    raw_types = sorted({geometry.geom_type for geometry in raw_geometries})
    raw_valid = [geometry.is_valid for geometry in raw_geometries]
    raw_validity_details = [explain_validity(geometry) for geometry in raw_geometries]
    repair_performed = not all(raw_valid)
    repaired_parts: list[Polygon] = []
    for geometry in raw_geometries:
        candidate = geometry if geometry.is_valid else make_valid(geometry)
        repaired_parts.extend(_polygon_parts(candidate))
    if not repaired_parts:
        raise ValueError("No polygonal geometry remained after deterministic validity repair.")
    final_wgs84 = unary_union(repaired_parts)
    if not final_wgs84.is_valid:
        final_wgs84 = make_valid(final_wgs84)
        final_parts = _polygon_parts(final_wgs84)
        if not final_parts:
            raise ValueError("Boundary could not be repaired to a polygonal geometry.")
        final_wgs84 = unary_union(final_parts)
        repair_performed = True
    source = gpd.GeoDataFrame(
        [{"huc": code, "huc_level": level, "name": case.get("name")}],
        geometry=[final_wgs84],
        crs=SOURCE_CRS,
    )
    if source.crs is None:
        raise ValueError("Source boundary CRS is missing.")
    projected = source.to_crs(PROJECT_CRS)
    projected_geometry = projected.geometry.iloc[0]
    processed_path = output_root / "processed" / "huc_boundaries" / f"huc{level}" / code / "boundary_epsg5070.gpkg"
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = processed_path.with_name(processed_path.stem + ".tmp.gpkg")
    if temporary.exists():
        temporary.unlink()
    projected.to_file(temporary, layer="boundary", driver="GPKG")
    temporary.replace(processed_path)

    geod = Geod(ellps="WGS84")
    geodesic_area_sqkm = abs(geod.geometry_area_perimeter(final_wgs84)[0]) / 1_000_000.0
    projected_area_sqkm = float(projected_geometry.area) / 1_000_000.0
    catalog_area_sqkm = float(catalog_row["area_sqkm"])
    area_relative_difference = abs(projected_area_sqkm - catalog_area_sqkm) / catalog_area_sqkm
    source_bounds = tuple(float(value) for value in final_wgs84.bounds)
    projected_bounds = tuple(float(value) for value in projected_geometry.bounds)
    roundtrip = projected.to_crs(SOURCE_CRS).geometry.iloc[0]
    representative_point = final_wgs84.representative_point()
    parent_levels = [parent for parent in LEVELS if parent < level]
    parent_prefixes = {str(parent): code[:parent] for parent in parent_levels}
    parents_present = all(prefix in all_codes[parent] for parent, prefix in ((p, code[:p]) for p in parent_levels))
    checks = {
        "requested_huc_identity": returned_codes == [code],
        "requested_huc_level": len(code) == level and code.isdigit(),
        "exactly_one_authoritative_feature": len(features) == 1,
        "selection_object_id_matches": returned_object_ids == [int(case["object_id"])],
        "catalog_object_id_matches": returned_object_ids == [int(catalog_row["object_id"])],
        "selection_tnmid_matches": returned_tnmids == [str(case["tnmid"])],
        "catalog_tnmid_matches": returned_tnmids == [str(catalog_row["tnmid"])],
        "nonempty_geometry": bool(raw_geometries) and not final_wgs84.is_empty,
        "valid_or_repaired_geometry": final_wgs84.is_valid,
        "polygonal_geometry": final_wgs84.geom_type in {"Polygon", "MultiPolygon"},
        "source_crs_present": source.crs is not None,
        "source_crs_is_epsg4326": source.crs == CRS.from_epsg(4326),
        "project_crs_is_epsg5070": projected.crs == CRS.from_epsg(5070),
        "finite_source_bounds": all(math.isfinite(value) for value in source_bounds),
        "finite_projected_bounds": all(math.isfinite(value) for value in projected_bounds),
        "positive_geodesic_area": math.isfinite(geodesic_area_sqkm) and geodesic_area_sqkm > 0,
        "positive_projected_area": math.isfinite(projected_area_sqkm) and projected_area_sqkm > 0,
        "catalog_area_consistent": area_relative_difference <= AREA_RELATIVE_TOLERANCE,
        "authoritative_parent_prefixes_present": parents_present,
        "geometry_covers_representative_point": final_wgs84.covers(representative_point),
        "projected_roundtrip_covers_representative_point": roundtrip.buffer(1.0e-10).covers(representative_point),
        "projected_roundtrip_within_tolerance": final_wgs84.hausdorff_distance(roundtrip) <= ROUNDTRIP_HAUSDORFF_TOLERANCE_DEGREES,
        "raw_response_checksum_matches_provenance": sha256_file(raw_path) == json.loads(fetched["provenance_path"].read_text(encoding="utf-8"))["response_sha256"],
        "processed_boundary_checksum_present": processed_path.is_file() and processed_path.stat().st_size > 0,
    }
    result = {
        "schema": f"{SCHEMA}-case",
        "status": "PASSED" if all(checks.values()) else "FAILED",
        "level": level,
        "huc": code,
        "name": case.get("name"),
        "states": case.get("states"),
        "checks": checks,
        "identity": {
            "returned_codes": returned_codes,
            "returned_object_ids": returned_object_ids,
            "returned_tnmids": returned_tnmids,
            "selection_object_id": case.get("object_id"),
            "catalog_object_id": catalog_row.get("object_id"),
            "selection_tnmid": case.get("tnmid"),
            "catalog_tnmid": catalog_row.get("tnmid"),
            "parent_prefixes": parent_prefixes,
        },
        "geometry": {
            "raw_feature_count": len(features),
            "raw_geometry_types": raw_types,
            "raw_valid": raw_valid,
            "raw_validity_details": raw_validity_details,
            "repair_performed": repair_performed,
            "final_geometry_type": final_wgs84.geom_type,
            "final_valid": final_wgs84.is_valid,
            "source_crs": source.crs.to_string(),
            "project_crs": projected.crs.to_string(),
            "source_bounds": source_bounds,
            "projected_bounds": projected_bounds,
            "geodesic_area_sqkm": geodesic_area_sqkm,
            "projected_area_sqkm": projected_area_sqkm,
            "catalog_area_sqkm": catalog_area_sqkm,
            "catalog_area_relative_difference": area_relative_difference,
            "roundtrip_hausdorff_degrees": final_wgs84.hausdorff_distance(roundtrip),
            "canonical_geometry_wkb_sha256": sha256_bytes(final_wgs84.wkb),
        },
        "artifacts": {
            "raw_boundary": str(raw_path.relative_to(output_root)),
            "raw_boundary_bytes": raw_path.stat().st_size,
            "raw_boundary_sha256": sha256_file(raw_path),
            "provenance": str(fetched["provenance_path"].relative_to(output_root)),
            "processed_boundary": str(processed_path.relative_to(output_root)),
            "processed_boundary_bytes": processed_path.stat().st_size,
            "processed_boundary_sha256": sha256_file(processed_path),
            "raw_product_modified": False,
            "cache_reused": fetched["reused"],
        },
    }
    result_path = output_root / "quality_control" / "cases" / f"huc{level}_{code}.json"
    atomic_json(result_path, result)
    result["case_report"] = str(result_path.relative_to(output_root))
    return result


def failed_result(case: dict[str, Any], error: Exception, output_root: Path) -> dict[str, Any]:
    result = {
        "schema": f"{SCHEMA}-case",
        "status": "FAILED",
        "level": int(case["level"]),
        "huc": case["huc"],
        "name": case.get("name"),
        "error_type": type(error).__name__,
        "error": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        "selection_preserved": True,
        "replacement_selected": False,
    }
    result_path = output_root / "quality_control" / "cases" / f"huc{case['level']}_{case['huc']}.json"
    atomic_json(result_path, result)
    result["case_report"] = str(result_path.relative_to(output_root))
    return result


def write_matrix_csv(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "level", "huc", "name", "status", "repair_performed", "source_crs",
        "project_crs", "geodesic_area_sqkm", "projected_area_sqkm",
        "catalog_area_sqkm", "catalog_area_relative_difference", "raw_boundary_sha256",
        "processed_boundary_sha256", "cache_reused", "case_report", "error",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            geometry = result.get("geometry", {})
            artifacts = result.get("artifacts", {})
            writer.writerow({
                "level": result["level"],
                "huc": result["huc"],
                "name": result.get("name"),
                "status": result["status"],
                "repair_performed": geometry.get("repair_performed"),
                "source_crs": geometry.get("source_crs"),
                "project_crs": geometry.get("project_crs"),
                "geodesic_area_sqkm": geometry.get("geodesic_area_sqkm"),
                "projected_area_sqkm": geometry.get("projected_area_sqkm"),
                "catalog_area_sqkm": geometry.get("catalog_area_sqkm"),
                "catalog_area_relative_difference": geometry.get("catalog_area_relative_difference"),
                "raw_boundary_sha256": artifacts.get("raw_boundary_sha256"),
                "processed_boundary_sha256": artifacts.get("processed_boundary_sha256"),
                "cache_reused": artifacts.get("cache_reused"),
                "case_report": result.get("case_report"),
                "error": result.get("error"),
            })
    temporary.replace(path)


def run(selection_path: Path, output_root: Path, cache_mode: str, max_workers: int) -> dict[str, Any]:
    selection_path = selection_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if cache_mode == "fresh" and output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(
            f"Fresh boundary validation requires a missing or empty output directory: {output_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    started_utc = utc_now()
    selection_sha_before = sha256_file(selection_path)
    manifest, cases, catalog, all_codes = validate_selection_bundle(selection_path)
    manifest_copy = output_root / "provenance" / "locked_selection_manifest.json"
    atomic_bytes(manifest_copy, selection_path.read_bytes())
    layer_metadata = [fetch_layer_metadata(level, output_root, cache_mode) for level in LEVELS]

    fetched: dict[tuple[int, str], dict[str, Any]] = {}
    fetch_errors: dict[tuple[int, str], Exception] = {}
    workers = max(1, min(int(max_workers), MAX_WORKERS_LIMIT, len(cases)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wbd-boundary") as executor:
        futures = {
            executor.submit(fetch_boundary, case, output_root, cache_mode): case
            for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            key = (int(case["level"]), case["huc"])
            try:
                fetched[key] = future.result()
            except Exception as error:
                fetch_errors[key] = error

    results: list[dict[str, Any]] = []
    for case in cases:
        key = (int(case["level"]), case["huc"])
        if key in fetch_errors:
            results.append(failed_result(case, fetch_errors[key], output_root))
            continue
        try:
            results.append(validate_boundary(fetched[key], output_root, catalog[key], all_codes))
        except Exception as error:
            results.append(failed_result(case, error, output_root))
    results.sort(key=lambda result: (int(result["level"]), result["huc"]))
    selection_sha_after = sha256_file(selection_path)
    passed = sum(result["status"] == "PASSED" for result in results)
    failed = len(results) - passed
    matrix = {
        "schema": SCHEMA,
        "status": (
            "PASSED" if failed == 0 and len(results) == EXPECTED_CASES else "FAILED"
        ),
        "started_utc": started_utc,
        "completed_utc": utc_now(),
        "scope": "locked HUC boundary acquisition and validation only",
        "workflow_stages_launched": [],
        "dem_downloaded": False,
        "soil_downloaded": False,
        "forcing_downloaded": False,
        "rift_workflow_launched": False,
        "cache_mode": cache_mode,
        "max_workers": workers,
        "authority": "U.S. Geological Survey Watershed Boundary Dataset",
        "service_url": WBD_FEATURE_SERVER,
        "project_crs": PROJECT_CRS,
        "fixed_seed": manifest["fixed_seed"],
        "selection_manifest": {
            "source_path": (
                Path("..") / selection_path.parent.name / selection_path.name
            ).as_posix(),
            "copied_path": str(manifest_copy.relative_to(output_root)),
            "sha256_before": selection_sha_before,
            "sha256_after": selection_sha_after,
            "preserved": selection_sha_before == selection_sha_after,
            "locked_case_digest_sha256": locked_selection_digest(cases),
            "case_count": len(cases),
            "replacement_allowed": False,
            "replacement_performed": False,
        },
        "layer_metadata": layer_metadata,
        "validation_thresholds": {
            "catalog_area_relative_tolerance": AREA_RELATIVE_TOLERANCE,
            "roundtrip_hausdorff_tolerance_degrees": ROUNDTRIP_HAUSDORFF_TOLERANCE_DEGREES,
        },
        "summary": {
            "expected_cases": EXPECTED_CASES,
            "result_cases": len(results),
            "passed": passed,
            "failed": failed,
            "counts_by_level": {
                str(level): sum(result["level"] == level for result in results)
                for level in LEVELS
            },
        },
        "results": results,
    }
    matrix_path = output_root / "quality_control" / "boundary_validation_matrix.json"
    csv_path = output_root / "quality_control" / "boundary_validation_matrix.csv"
    write_matrix_csv(csv_path, results)
    source_metadata = {
        "schema": f"{SCHEMA}-source",
        "authority": matrix["authority"],
        "service_url": WBD_FEATURE_SERVER,
        "selection_manifest_sha256": selection_sha_before,
        "locked_case_digest_sha256": matrix["selection_manifest"]["locked_case_digest_sha256"],
        "layer_metadata": layer_metadata,
        "raw_boundary_count": len(list(
            (output_root / "raw" / "huc_boundaries").glob("huc*/*/raw_boundary.geojson")
        )),
    }
    source_metadata_path = output_root / "provenance" / "source_metadata.json"
    atomic_json(source_metadata_path, source_metadata)
    matrix["artifacts"] = {
        "matrix_json": str(matrix_path.relative_to(output_root)),
        "matrix_csv": str(csv_path.relative_to(output_root)),
        "matrix_csv_sha256": sha256_file(csv_path),
        "source_metadata": str(source_metadata_path.relative_to(output_root)),
        "source_metadata_sha256": sha256_file(source_metadata_path),
        "artifact_manifest": "provenance/artifact_manifest.json",
    }
    atomic_json(matrix_path, matrix)
    artifact_manifest_path = output_root / "provenance" / "artifact_manifest.json"
    artifact_manifest = {
        "schema": f"{SCHEMA}-artifacts",
        "created_utc": utc_now(),
        "selection_manifest_sha256": selection_sha_before,
        "matrix_json": {
            "path": str(matrix_path.relative_to(output_root)),
            "bytes": matrix_path.stat().st_size,
            "sha256": sha256_file(matrix_path),
        },
        "matrix_csv": {
            "path": str(csv_path.relative_to(output_root)),
            "bytes": csv_path.stat().st_size,
            "sha256": sha256_file(csv_path),
        },
        "source_metadata": {
            "path": str(source_metadata_path.relative_to(output_root)),
            "bytes": source_metadata_path.stat().st_size,
            "sha256": sha256_file(source_metadata_path),
        },
    }
    atomic_json(artifact_manifest_path, artifact_manifest)
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection-manifest", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--cache-mode", choices=("fresh", "reuse"), default="fresh")
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()
    result = run(
        Path(args.selection_manifest), Path(args.output_root), args.cache_mode, args.max_workers
    )
    print(json.dumps({
        "schema": result["schema"],
        "status": result["status"],
        "summary": result["summary"],
        "selection_manifest": result["selection_manifest"],
        "artifacts": result["artifacts"],
    }, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
