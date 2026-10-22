"""Read-only integrity preflight for resuming the partial HUC2 notebook case."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


CANONICAL_RELATIVE_PATHS = (
    "raw/boundary/selected_domain.gpkg",
    "raw/dem/3dep_30m_native.nc",
    "raw/dem/3dep_30m_native.tif",
    "raw/initial_state/initial_state_definition.json",
    "raw/hydrography/usgs_3dhp_flowlines.gpkg",
    "raw/land_cover/nlcd_L48_2021_native.nc",
    "raw/land_cover/nlcd_cover_2021_native.tif",
    "raw/land_cover/nlcd_impervious_2021_native.tif",
    "raw/land_cover/nlcd_canopy_2021_native.tif",
)
QC_PRODUCTS = ("dem", "initial_state", "hydrography", "land_cover")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preflight(case_root: Path) -> dict:
    case_root = case_root.expanduser().resolve()
    marker = case_root / ".riftprep-case"
    provenance_path = case_root / "quality_control/provenance/raw_products.json"
    records = json.loads(provenance_path.read_text(encoding="utf-8"))
    by_path = {str(Path(item["path"]).resolve()): item for item in records}

    def inspect(relative: str) -> dict:
        path = (case_root / relative).resolve()
        ledger = by_path.get(str(path))
        result = {
            "relative_path": relative,
            "path": str(path),
            "exists": path.is_file(),
            "ledger_record_present": ledger is not None,
        }
        if path.is_file():
            result["bytes"] = path.stat().st_size
            result["sha256"] = sha256(path)
        if ledger is not None:
            result["ledger_bytes"] = int(ledger["bytes"])
            result["ledger_sha256"] = ledger["sha256"]
        result["size_matches"] = bool(
            path.is_file() and ledger and result["bytes"] == result["ledger_bytes"]
        )
        result["checksum_matches"] = bool(
            path.is_file()
            and ledger
            and result["sha256"] == result["ledger_sha256"]
        )
        return result

    with ThreadPoolExecutor(max_workers=4) as executor:
        artifacts = list(executor.map(inspect, CANONICAL_RELATIVE_PATHS))

    qc = {}
    for name in QC_PRODUCTS:
        path = case_root / f"quality_control/input_qc/{name}.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        qc[name] = {
            "path": str(path),
            "exists": path.is_file(),
            "status": payload.get("status"),
        }

    soil_cache = case_root / "raw/provider_cache/soil"
    soil_raw = case_root / "raw/soil/ssurgo_native.gpkg"
    shapes = list(soil_cache.rglob("shapes.gpkg")) if soil_cache.is_dir() else []
    properties = list(soil_cache.rglob("properties.csv")) if soil_cache.is_dir() else []
    empty_directories = (
        [path for path in soil_cache.rglob("*") if path.is_dir() and not any(path.iterdir())]
        if soil_cache.is_dir()
        else []
    )
    passed = (
        marker.is_file()
        and provenance_path.is_file()
        and all(item["checksum_matches"] for item in artifacts)
        and all(item["status"] == "PASSED" for item in qc.values())
        and not soil_raw.exists()
        and len(shapes) == len(properties)
    )
    return {
        "schema": "riftprep-huc2-resume-preflight-v1",
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "case_root": str(case_root),
        "case_marker_present": marker.is_file(),
        "provenance_path": str(provenance_path),
        "canonical_artifacts": artifacts,
        "existing_input_qc": qc,
        "soil_cache": {
            "path": str(soil_cache),
            "completed_shapes": len(shapes),
            "completed_properties": len(properties),
            "empty_query_directories": len(empty_directories),
            "final_raw_soil_exists": soil_raw.is_file(),
        },
        "resume_cache_mode": "reuse",
        "no_existing_data_modified": True,
        "status": "PASSED" if passed else "FAILED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = preflight(args.case_root)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
