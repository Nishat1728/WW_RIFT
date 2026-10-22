"""Probe one USDA SSURGO spatial request without modifying workflow data.

The raw response body and request geometry are written before JSON decoding.
This is intended for diagnosing transient or malformed SDM Data Access
responses independently of the RIFT notebook and Watershed Workflow cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely.geometry import box, mapping
from watershed_workflow.sources import manager_nrcs
from watershed_workflow.sources import utils as source_utils


DEFAULT_ENDPOINT = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"


def _safe_headers(headers: requests.structures.CaseInsensitiveDict) -> dict[str, str]:
    """Retain diagnostic response headers without storing cookies."""
    keep = {
        "content-type",
        "content-length",
        "date",
        "server",
        "retry-after",
        "x-powered-by",
        "x-aspnet-version",
        "x-aspnetmvc-version",
    }
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in keep
    }


def probe(
    bounds: tuple[float, float, float, float],
    output_directory: Path,
    timeout_seconds: float,
    endpoint: str = DEFAULT_ENDPOINT,
) -> dict:
    output_directory.mkdir(parents=True, exist_ok=True)
    geometry = box(*bounds)
    query = manager_nrcs._query_template_shapes.format(geometry.wkt)
    request_record = {
        "schema": "riftprep-ssurgo-http-probe-v1",
        "requested_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "method": "POST",
        "format": "JSON",
        "bounds_epsg4326": list(bounds),
        "geometry_wkt_epsg4326": geometry.wkt,
        "geometry_geojson_epsg4326": mapping(geometry),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "timeout_seconds": timeout_seconds,
    }
    (output_directory / "request.json").write_text(
        json.dumps(request_record, indent=2), encoding="utf-8"
    )

    started = time.perf_counter()
    try:
        response = requests.post(
            endpoint,
            data={"FORMAT": "JSON", "QUERY": query},
            timeout=timeout_seconds,
            verify=source_utils.getVerifyOption(),
        )
    except requests.RequestException as error:
        failure = {
            **request_record,
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.perf_counter() - started,
            "transport_error_type": type(error).__name__,
            "transport_error": str(error),
        }
        (output_directory / "response_metadata.json").write_text(
            json.dumps(failure, indent=2), encoding="utf-8"
        )
        return failure

    elapsed = time.perf_counter() - started
    raw = response.content
    raw_path = output_directory / "response_body.bin"
    raw_path.write_bytes(raw)

    content_type = response.headers.get("Content-Type", "")
    metadata = {
        **request_record,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "http_status": response.status_code,
        "reason": response.reason,
        "response_url": response.url,
        "response_headers": _safe_headers(response.headers),
        "content_type": content_type,
        "response_bytes": len(raw),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_body_path": str(raw_path.resolve()),
        "raw_body_saved_before_json_decode": True,
        "body_preview_utf8": raw[:1000].decode("utf-8", errors="replace"),
    }

    # Decode only after the untouched provider bytes and HTTP metadata exist.
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        metadata.update(
            {
                "json_valid": False,
                "json_error_type": type(error).__name__,
                "json_error": str(error),
            }
        )
    else:
        metadata.update(
            {
                "json_valid": True,
                "json_top_level_type": type(payload).__name__,
                "json_keys": sorted(payload) if isinstance(payload, dict) else [],
                "table_present": isinstance(payload, dict) and "Table" in payload,
                "table_rows": (
                    len(payload["Table"])
                    if isinstance(payload, dict)
                    and isinstance(payload.get("Table"), list)
                    else None
                ),
            }
        )

    (output_directory / "response_metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bounds",
        type=float,
        nargs=4,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        required=True,
        help="Exact EPSG:4326 bounding box used by the SSURGO request.",
    )
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="Number of independent requests; attempts are saved separately.",
    )
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")
    root = args.output_directory.expanduser().resolve()
    results = []
    for attempt in range(1, args.attempts + 1):
        attempt_directory = (
            root if args.attempts == 1 else root / f"attempt_{attempt:03d}"
        )
        results.append(
            probe(
                tuple(args.bounds),
                attempt_directory,
                args.timeout_seconds,
                args.endpoint,
            )
        )
    summary = {
        "schema": "riftprep-ssurgo-http-probe-summary-v1",
        "attempts": args.attempts,
        "results": results,
    }
    if args.attempts > 1:
        root.mkdir(parents=True, exist_ok=True)
        (root / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary if args.attempts > 1 else results[0], indent=2))
    if any("transport_error" in result for result in results):
        return 2
    return 0 if all(result.get("http_status") == 200 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
