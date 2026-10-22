"""Snapshot USGS WBD attributes and select a deterministic CONUS HUC matrix.

This script downloads no basin geometry and launches no workflow.  It writes the
authoritative source/candidate artifacts before writing the final selection.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHEMA_VERSION = "riftprep-conus-huc-selection-v2"
SERVICE_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/FeatureServer"
LEVELS = (2, 4, 8, 12)
SAMPLE_COUNTS = {2: 2, 4: 4, 8: 6, 12: 10}
SELECTION_ORDER = (2, 4, 8, 12)
FIXED_SEED = 20261014
PAGE_SIZE = 2000
REQUIRED_FIELDS = ("tnmid", "loaddate", "areasqkm", "states", "name")
OPTIONAL_FIELDS = ("hutype", "tohuc")
REQUESTED_FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)
# Of the levels in this matrix, the current authoritative WBD schema publishes
# HUType only on HUC12. HUC2/HUC4/HUC8 therefore cannot be screened by HUType
# without inventing or deriving a value. The limitation is recorded in every
# source/candidate manifest.
HU_TYPE_LEVELS = frozenset({12})
CONUS_JURISDICTIONS = frozenset({
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA",
    "WA", "WV", "WI", "WY", "DC",
})
ALLOWED_HU_TYPES = frozenset({"S", "C", "M", "U"})
# A transparent computational screen for eventual native-30-m matrix runs.
MAX_NOMINAL_30M_CELLS = {
    # This admits multiple fully-CONUS HUC2 candidates while excluding the
    # largest regional basins whose native 30 m grids are not feasible within
    # the Storrs 12-hour/384-GB validation envelope.
    2: 350_000_000,
    4: 50_000_000,
    8: 15_000_000,
    12: 2_000_000,
}
CELL_AREA_SQKM_30M = 0.0009
SOURCE_FILENAME = "wbd_authoritative_catalog.jsonl.gz"
ELIGIBLE_FILENAME = "eligible_conus_hucs.jsonl.gz"
SOURCE_METADATA_FILENAME = "source_metadata.json"
CANDIDATE_METADATA_FILENAME = "candidate_metadata.json"
SELECTION_FILENAME = "selection_manifest.json"


class ArcGISServiceError(RuntimeError):
    """A structured error returned by the authoritative ArcGIS service."""

    def __init__(self, endpoint: str, payload: dict[str, Any]):
        error = payload.get("error", payload)
        self.endpoint = endpoint
        self.code = error.get("code")
        self.extended_code = error.get("extendedCode")
        self.details = error.get("details") or []
        self.service_message = error.get("message")
        super().__init__(
            "ArcGIS service error "
            f"code={self.code!r} extended_code={self.extended_code!r} "
            f"message={self.service_message!r} details={self.details!r} "
            f"endpoint={endpoint}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def atomic_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def write_jsonl_gzip(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Write canonical JSONL with a deterministic gzip header and two checksums."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    uncompressed_digest = hashlib.sha256()
    count = 0
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for row in rows:
                line = (canonical_json(row) + "\n").encode("utf-8")
                uncompressed_digest.update(line)
                compressed.write(line)
                count += 1
    temporary.replace(path)
    return {
        "path": path.name,
        "records": count,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "canonical_jsonl_sha256": uncompressed_digest.hexdigest(),
        "compression": "gzip-mtime-0",
    }


def read_jsonl_gzip(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def request_json(
    url: str,
    params: dict[str, Any],
    *,
    attempts: int = 6,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    query_url = f"{url}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(query_url, headers={"User-Agent": "riftprep-huc-selection/1"})
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    payload = json.loads(response.read())
            except HTTPError as error:
                body = error.read()
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise RuntimeError(
                        f"HTTP {error.code} from {url}; response body={body[:500]!r}"
                    ) from error
                raise ArcGISServiceError(url, payload) from error
            if "error" in payload:
                raise ArcGISServiceError(url, payload)
            return payload
        except ArcGISServiceError:
            # A deterministic query/schema error is not made transient by retrying.
            raise
        except Exception as error:  # Network/provider errors are retried and then surfaced.
            last_error = error
            if attempt < attempts:
                time.sleep(min(30.0, 2.0 ** (attempt - 1)))
    raise RuntimeError(f"USGS WBD request failed after {attempts} attempts: {last_error}")


def huc_field(level: int) -> str:
    if level not in LEVELS:
        raise ValueError(f"Unsupported HUC level: {level}")
    return f"huc{level}"


def layer_url(level: int) -> str:
    return f"{SERVICE_URL}/{level // 2}"


def normalize_source_record(level: int, attributes: dict[str, Any]) -> dict[str, Any]:
    field = huc_field(level)
    return {
        "level": level,
        "object_id": attributes.get("objectid"),
        "huc": None if attributes.get(field) is None else str(attributes[field]).strip(),
        "name": attributes.get("name"),
        "states_raw": attributes.get("states"),
        "hu_type": attributes.get("hutype"),
        "area_sqkm": attributes.get("areasqkm"),
        "to_huc": attributes.get("tohuc"),
        "tnmid": attributes.get("tnmid"),
        "load_date_epoch_ms": attributes.get("loaddate"),
    }


def query_fields(level: int, metadata: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return schema-compatible fields and record unavailable optional fields."""
    available = {field["name"].lower(): field["name"] for field in metadata["fields"]}
    required = (huc_field(level), *REQUIRED_FIELDS)
    missing_required = [field for field in required if field.lower() not in available]
    if missing_required:
        raise RuntimeError(
            f"WBD HUC{level} is missing required authoritative fields: {missing_required}"
        )
    selected = [available[field.lower()] for field in required]
    selected.extend(
        available[field.lower()] for field in OPTIONAL_FIELDS if field.lower() in available
    )
    unavailable_optional = [
        field for field in OPTIONAL_FIELDS if field.lower() not in available
    ]
    return selected, unavailable_optional


def fetch_level(level: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    field = huc_field(level)
    endpoint = layer_url(level)
    metadata = request_json(endpoint, {"f": "json"})
    fields, unavailable_optional = query_fields(level, metadata)
    object_id_field = metadata.get("objectIdField")
    if not object_id_field:
        raise RuntimeError(f"WBD HUC{level} metadata does not declare an object ID field.")
    if object_id_field.lower() not in {field.lower() for field in fields}:
        fields.insert(0, object_id_field)
    capabilities = metadata.get("advancedQueryCapabilities", {})
    if not capabilities.get("supportsPagination"):
        raise RuntimeError(f"WBD HUC{level} does not advertise pagination support.")
    if not capabilities.get("supportsOrderBy"):
        raise RuntimeError(f"WBD HUC{level} does not advertise ordered queries.")
    query_endpoint = f"{endpoint}/query"
    count_before = int(request_json(query_endpoint, {
        "where": "1=1", "returnCountOnly": "true", "f": "json",
    })["count"])
    ids_before = request_json(query_endpoint, {
        "where": "1=1", "returnIdsOnly": "true", "f": "json",
    }).get("objectIds") or []
    if len(ids_before) != count_before or len(set(ids_before)) != count_before:
        raise RuntimeError(
            f"WBD HUC{level} count/ID mismatch before pagination: "
            f"count={count_before}, ids={len(ids_before)}, unique_ids={len(set(ids_before))}"
        )
    rows: list[dict[str, Any]] = []
    page_lengths: list[int] = []
    offset = 0
    page_size = min(PAGE_SIZE, int(metadata.get("maxRecordCount") or PAGE_SIZE))
    while offset < count_before:
        payload = request_json(query_endpoint, {
            "where": "1=1",
            "outFields": ",".join(fields),
            "returnGeometry": "false",
            "orderByFields": f"{field} ASC,{object_id_field} ASC",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        })
        features = payload.get("features", [])
        if not features:
            break
        page_lengths.append(len(features))
        rows.extend(normalize_source_record(level, item["attributes"]) for item in features)
        offset += len(features)
    count_after = int(request_json(query_endpoint, {
        "where": "1=1", "returnCountOnly": "true", "f": "json",
    })["count"])
    ids_after = request_json(query_endpoint, {
        "where": "1=1", "returnIdsOnly": "true", "f": "json",
    }).get("objectIds") or []
    row_ids = [row["object_id"] for row in rows]
    if (
        count_before != count_after
        or len(rows) != count_before
        or len(ids_after) != count_after
        or len(set(row_ids)) != count_before
        or set(row_ids) != set(ids_before)
        or set(ids_before) != set(ids_after)
    ):
        raise RuntimeError(
            f"WBD HUC{level} changed or pagination was incomplete: "
            f"before={count_before}, ids_before={len(ids_before)}, rows={len(rows)}, "
            f"unique_row_ids={len(set(row_ids))}, after={count_after}, "
            f"ids_after={len(ids_after)}"
        )
    codes = [row["huc"] for row in rows]
    if codes != sorted(codes, key=lambda value: "" if value is None else value):
        raise RuntimeError(f"WBD HUC{level} response was not deterministically ordered.")
    metadata_summary = {
        "level": level,
        "layer_id": level // 2,
        "layer_url": endpoint,
        "layer_name": metadata.get("name"),
        "service_current_version": metadata.get("currentVersion"),
        "service_last_edit_date_epoch_ms": (
            metadata.get("editingInfo", {}).get("lastEditDate")
        ),
        "object_id_field": metadata.get("objectIdField"),
        "max_record_count": metadata.get("maxRecordCount"),
        "supports_pagination": capabilities.get("supportsPagination"),
        "supports_order_by": capabilities.get("supportsOrderBy"),
        "count_before": count_before,
        "count_after": count_after,
        "ids_before": len(ids_before),
        "ids_after": len(ids_after),
        "unique_paged_object_ids": len(set(row_ids)),
        "page_count": len(page_lengths),
        "page_lengths": page_lengths,
        "pagination_complete_and_stable": True,
        "schema_fields": [item["name"] for item in metadata["fields"]],
        "requested_fields": list(REQUESTED_FIELDS),
        "queried_fields": fields,
        "unavailable_optional_fields": unavailable_optional,
        "hu_type_rule_applicable": level in HU_TYPE_LEVELS,
        "query": {
            "where": "1=1",
            "outFields": fields,
            "returnGeometry": False,
            "orderByFields": f"{field} ASC,{object_id_field} ASC",
            "page_size": page_size,
        },
        "attribute_rows_canonical_sha256": hashlib.sha256(
            "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
        ).hexdigest(),
        "metadata_sha256": hashlib.sha256(
            canonical_json(metadata).encode("utf-8")
        ).hexdigest(),
    }
    return rows, metadata_summary


def parse_states(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(sorted({part.strip().upper() for part in str(raw).split(",") if part.strip()}))


def nominal_30m_cells(area_sqkm: float) -> int:
    return int(math.ceil(area_sqkm / CELL_AREA_SQKM_30M))


def eligibility_reasons(
    row: dict[str, Any], all_codes: dict[int, set[str]]
) -> list[str]:
    level = int(row["level"])
    code = row.get("huc")
    reasons: list[str] = []
    if not isinstance(code, str) or len(code) != level or not code.isdigit():
        reasons.append("invalid_huc_code")
    states = parse_states(row.get("states_raw"))
    if not states:
        reasons.append("missing_states")
    elif not set(states).issubset(CONUS_JURISDICTIONS):
        reasons.append("not_entirely_conus")
    if level in HU_TYPE_LEVELS and row.get("hu_type") not in ALLOWED_HU_TYPES:
        reasons.append("excluded_hu_type")
    try:
        area = float(row.get("area_sqkm"))
    except (TypeError, ValueError):
        area = math.nan
    if not math.isfinite(area) or area <= 0:
        reasons.append("invalid_area")
    elif nominal_30m_cells(area) > MAX_NOMINAL_30M_CELLS[level]:
        reasons.append("exceeds_nominal_30m_cell_ceiling")
    if isinstance(code, str) and code.isdigit() and len(code) == level:
        for parent_level in LEVELS:
            if parent_level >= level:
                break
            if code[:parent_level] not in all_codes[parent_level]:
                reasons.append(f"missing_huc{parent_level}_parent")
    return reasons


def build_candidates(
    source_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_codes = {
        level: {
            row["huc"] for row in source_rows
            if row["level"] == level and isinstance(row.get("huc"), str)
        }
        for level in LEVELS
    }
    duplicates = {
        level: sorted(code for code, count in Counter(
            row["huc"] for row in source_rows if row["level"] == level
        ).items() if count > 1)
        for level in LEVELS
    }
    if any(duplicates.values()):
        raise ValueError(f"Authoritative WBD snapshot contains duplicate HUC codes: {duplicates}")
    eligible = []
    exclusion_counts: dict[int, Counter] = {level: Counter() for level in LEVELS}
    for row in source_rows:
        reasons = eligibility_reasons(row, all_codes)
        if reasons:
            exclusion_counts[int(row["level"])].update(reasons)
            continue
        area = float(row["area_sqkm"])
        eligible.append({
            **row,
            "states": list(parse_states(row.get("states_raw"))),
            "area_sqkm": area,
            "nominal_30m_cells": nominal_30m_cells(area),
        })
    eligible.sort(key=lambda row: (int(row["level"]), row["huc"]))
    metadata = {
        "schema": f"{SCHEMA_VERSION}-candidates",
        "eligibility_rules": {
            "valid_code": "ASCII decimal digits with exact HUC level length",
            "unique_code_within_level": True,
            "states": {
                "rule": "nonempty WBD states, all members in the 48 states or DC",
                "allowed": sorted(CONUS_JURISDICTIONS),
            },
            "hu_type": {
                "allowed": sorted(ALLOWED_HU_TYPES),
                "excluded_meanings": ["Frontal", "Island", "Water", "Indeterminate"],
                "applies_to_levels": sorted(HU_TYPE_LEVELS),
                "unavailable_levels": sorted(set(LEVELS) - set(HU_TYPE_LEVELS)),
                "schema_limitation": (
                    "The authoritative WBD FeatureServer does not publish HUType "
                    "for HUC4 or HUC8; no value is invented or derived."
                ),
            },
            "area_sqkm": "finite and > 0",
            "authoritative_parent_prefix_required": True,
            "nominal_30m_cell_ceiling": MAX_NOMINAL_30M_CELLS,
            "nominal_cell_area_sqkm": CELL_AREA_SQKM_30M,
        },
        "source_counts": {
            str(level): sum(row["level"] == level for row in source_rows)
            for level in LEVELS
        },
        "eligible_counts": {
            str(level): sum(row["level"] == level for row in eligible)
            for level in LEVELS
        },
        "exclusion_counts": {
            str(level): dict(sorted(exclusion_counts[level].items()))
            for level in LEVELS
        },
    }
    return eligible, metadata


def selection_score(seed: int, level: int, huc: str) -> str:
    return hashlib.sha256(f"{seed}:{level}:{huc}".encode("ascii")).hexdigest()


def select_hucs(
    eligible: list[dict[str, Any]], seed: int = FIXED_SEED
) -> dict[int, list[dict[str, Any]]]:
    selected: dict[int, list[dict[str, Any]]] = {}
    selected_codes: list[str] = []
    for level in SELECTION_ORDER:
        candidates = [row for row in eligible if int(row["level"]) == level]
        ranked = sorted(
            candidates,
            key=lambda row: (selection_score(seed, level, row["huc"]), row["huc"]),
        )
        nonoverlapping = [
            row for row in ranked
            if not any(
                row["huc"].startswith(previous) or previous.startswith(row["huc"])
                for previous in selected_codes
            )
        ]
        needed = SAMPLE_COUNTS[level]
        if len(nonoverlapping) < needed:
            raise RuntimeError(
                f"Only {len(nonoverlapping)} non-overlapping eligible HUC{level} "
                f"records are available; {needed} are required."
            )
        selected[level] = [
            {**row, "selection_score_sha256": selection_score(seed, level, row["huc"])}
            for row in nonoverlapping[:needed]
        ]
        selected_codes.extend(row["huc"] for row in selected[level])
    return selected


def validate_selection(
    selected: dict[int, list[dict[str, Any]]],
    eligible: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    eligible_codes = {
        level: {row["huc"] for row in eligible if row["level"] == level}
        for level in LEVELS
    }
    flattened = [row for level in LEVELS for row in selected[level]]
    codes = [row["huc"] for row in flattened]
    checks = {
        "exact_counts": all(len(selected[level]) == SAMPLE_COUNTS[level] for level in LEVELS),
        "exact_code_lengths": all(len(row["huc"]) == row["level"] for row in flattened),
        "decimal_codes": all(row["huc"].isdigit() for row in flattened),
        "unique_within_and_across_levels": len(codes) == len(set(codes)),
        "all_selected_are_eligible": all(
            row["huc"] in eligible_codes[row["level"]] for row in flattened
        ),
        "no_selected_ancestor_descendant_overlap": all(
            not (left.startswith(right) or right.startswith(left))
            for index, left in enumerate(codes)
            for right in codes[index + 1:]
        ),
        "seeded_scores_match": all(
            row["selection_score_sha256"]
            == selection_score(seed, row["level"], row["huc"])
            for row in flattened
        ),
        "deterministic_recomputation_matches": select_hucs(eligible, seed) == selected,
    }
    if not all(checks.values()):
        raise ValueError(f"Selection validation failed: {checks}")
    return checks


def build(output_directory: Path) -> Path:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    layer_metadata_directory = output_directory / "source_layer_metadata"
    layer_metadata_directory.mkdir(exist_ok=True)
    retrieved_utc = utc_now()
    source_rows: list[dict[str, Any]] = []
    layer_summaries = []
    for level in LEVELS:
        rows, summary = fetch_level(level)
        source_rows.extend(rows)
        layer_summaries.append(summary)
        atomic_json(layer_metadata_directory / f"huc{level}.json", summary)
    source_rows.sort(key=lambda row: (int(row["level"]), row.get("huc") or ""))
    source_artifact = write_jsonl_gzip(
        output_directory / SOURCE_FILENAME, source_rows
    )
    source_metadata = {
        "schema": f"{SCHEMA_VERSION}-source",
        "retrieved_utc": retrieved_utc,
        "authority": "U.S. Geological Survey Watershed Boundary Dataset",
        "service_url": SERVICE_URL,
        "geometry_requested": False,
        "levels": list(LEVELS),
        "layer_summaries": layer_summaries,
        "source_artifact": source_artifact,
        "source_stability_check": "feature count equal before and after every paged layer query",
    }
    source_metadata_path = atomic_json(
        output_directory / SOURCE_METADATA_FILENAME, source_metadata
    )

    eligible, candidate_metadata = build_candidates(source_rows)
    eligible_artifact = write_jsonl_gzip(
        output_directory / ELIGIBLE_FILENAME, eligible
    )
    candidate_metadata.update({
        "created_utc": utc_now(),
        "source_metadata_path": source_metadata_path.name,
        "source_metadata_sha256": sha256(source_metadata_path),
        "source_artifact_sha256": source_artifact["sha256"],
        "eligible_artifact": eligible_artifact,
    })
    candidate_metadata_path = atomic_json(
        output_directory / CANDIDATE_METADATA_FILENAME, candidate_metadata
    )

    selected = select_hucs(eligible, FIXED_SEED)
    checks = validate_selection(selected, eligible, FIXED_SEED)
    selection_manifest = {
        "schema": SCHEMA_VERSION,
        "created_utc": utc_now(),
        "fixed_seed": FIXED_SEED,
        "selection_algorithm": (
            "For each level in HUC2,HUC4,HUC8,HUC12 order, rank eligible codes "
            "by SHA256('<seed>:<level>:<huc>') ascending and take the requested "
            "count after excluding ancestor/descendant overlaps with earlier levels."
        ),
        "replacement": False,
        "cross_level_ancestor_descendant_overlap_allowed": False,
        "sample_counts": {str(key): value for key, value in SAMPLE_COUNTS.items()},
        "source_metadata": {
            "path": source_metadata_path.name,
            "sha256": sha256(source_metadata_path),
        },
        "candidate_metadata": {
            "path": candidate_metadata_path.name,
            "sha256": sha256(candidate_metadata_path),
        },
        "source_catalog": source_artifact,
        "eligible_catalog": eligible_artifact,
        "selected": {
            str(level): selected[level] for level in reversed(LEVELS)
        },
        "validation": checks,
        "artifacts_written_before_selection_manifest": [
            SOURCE_METADATA_FILENAME,
            SOURCE_FILENAME,
            CANDIDATE_METADATA_FILENAME,
            ELIGIBLE_FILENAME,
        ],
        "workflow_jobs_launched": False,
    }
    return atomic_json(output_directory / SELECTION_FILENAME, selection_manifest)


def diagnose() -> dict[str, Any]:
    """Read all required layers without writing artifacts or selecting HUCs."""
    summaries = []
    for level in LEVELS:
        _, summary = fetch_level(level)
        summaries.append(summary)
    return {
        "schema": f"{SCHEMA_VERSION}-source-diagnostic",
        "status": "PASSED",
        "workflow_jobs_launched": False,
        "selection_performed": False,
        "layers": summaries,
    }


def verify(output_directory: Path) -> dict[str, Any]:
    output_directory = output_directory.expanduser().resolve()
    source_metadata_path = output_directory / SOURCE_METADATA_FILENAME
    candidate_metadata_path = output_directory / CANDIDATE_METADATA_FILENAME
    selection_path = output_directory / SELECTION_FILENAME
    source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
    candidate_metadata = json.loads(candidate_metadata_path.read_text(encoding="utf-8"))
    manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"Unexpected selection schema: {manifest.get('schema')}")
    if sha256(source_metadata_path) != manifest["source_metadata"]["sha256"]:
        raise ValueError("Source metadata checksum does not match the selection manifest.")
    if sha256(candidate_metadata_path) != manifest["candidate_metadata"]["sha256"]:
        raise ValueError("Candidate metadata checksum does not match the selection manifest.")
    source_path = output_directory / SOURCE_FILENAME
    eligible_path = output_directory / ELIGIBLE_FILENAME
    if sha256(source_path) != source_metadata["source_artifact"]["sha256"]:
        raise ValueError("Authoritative source catalog checksum mismatch.")
    if sha256(eligible_path) != candidate_metadata["eligible_artifact"]["sha256"]:
        raise ValueError("Eligible catalog checksum mismatch.")
    source_rows = read_jsonl_gzip(source_path)
    eligible, rebuilt_metadata = build_candidates(source_rows)
    if rebuilt_metadata["eligible_counts"] != candidate_metadata["eligible_counts"]:
        raise ValueError("Rebuilt eligible counts differ from recorded candidate metadata.")
    recorded = {
        level: manifest["selected"][str(level)] for level in LEVELS
    }
    checks = validate_selection(recorded, eligible, int(manifest["fixed_seed"]))
    if checks != manifest["validation"]:
        raise ValueError("Recomputed validation differs from selection manifest.")
    return {
        "schema": SCHEMA_VERSION,
        "status": "PASSED",
        "source_records": len(source_rows),
        "eligible_records": len(eligible),
        "selected_records": sum(len(rows) for rows in recorded.values()),
        "fixed_seed": manifest["fixed_seed"],
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("diagnose")
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--output-directory", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()
    if args.command == "diagnose":
        print(json.dumps(diagnose(), indent=2))
        return 0
    output_directory = Path(args.output_directory)
    if args.command == "build":
        selection_path = build(output_directory)
        print(selection_path)
        return 0
    result = verify(output_directory)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
