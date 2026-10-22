"""Audit completed Storrs cases at the artifact level."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import pandas as pd
import rasterio
import xarray as xr

from riftprep.artifacts import sha256
from riftprep.config import resolve_simulation_ready_directory


def read_ascii_cellsize(path: Path) -> float:
    with path.open(encoding="utf-8") as stream:
        header = [next(stream).split() for _ in range(6)]
    values = {key.lower(): value for key, value in header}
    return float(values["cellsize"])


def audit_case(case_root: Path) -> dict:
    manifest = json.loads((case_root / "case_manifest.json").read_text(encoding="utf-8"))
    qc = json.loads((case_root / "quality_control/qc_report.json").read_text(encoding="utf-8"))
    raw = json.loads((case_root / "quality_control/provenance/raw_products.json").read_text(encoding="utf-8"))
    timing = pd.read_csv(case_root / "quality_control/timings/wall_clock.csv")
    failures = []
    for record in raw:
        path = Path(record["path"])
        if not path.is_file() or sha256(path) != record["sha256"]:
            failures.append(f"raw checksum: {path}")
    raw_end = timing.loc[timing.stage.str.startswith("download_raw_"), "started_utc"].index.max()
    process_start = timing.loc[timing.stage == "build_domain_masks"].index.min()
    if not raw_end < process_start:
        failures.append("processing began before all raw stages completed")
    dem_path = Path(manifest["raw_products"]["dem"]["files"]["raster"])
    with rasterio.open(dem_path) as source:
        source_dem_resolution = [abs(float(value)) for value in source.res]
        source_dem_shape = [source.height, source.width]
    simulation_ready = resolve_simulation_ready_directory(case_root)
    model_resolution = read_ascii_cellsize(simulation_ready / "dem.asc")
    raw_plots = {
        product: Path(details["files"]["plot"]).is_file()
        for product, details in manifest["raw_products"].items()
        if "plot" in details["files"]
    }
    expected_plots = {"dem", "hydrography", "land_cover", "soil", "rainfall"}
    if set(raw_plots) != expected_plots or not all(raw_plots.values()):
        failures.append(f"raw plots missing: {raw_plots}")
    rainfall_raw = Path(manifest["raw_products"]["rainfall"]["files"]["netcdf"])
    with xr.open_dataset(rainfall_raw) as source:
        raw_rainfall_records = int(source.sizes["time"])
    with h5py.File(simulation_ready / "rainfall_ats.h5") as source:
        rift_rainfall_records = len(source["precipitation rain [m s^-1]"])
        rift_rainfall_times = len(source["time [s]"])
    if rift_rainfall_records != 7 or rift_rainfall_times != 7:
        failures.append("unexpected six-hour RIFT rainfall record count")
    if not qc["ok"]:
        failures.append("QC report is not OK")
    if manifest["status"] != "complete":
        failures.append("manifest is not complete")
    if not (case_root / "rift_case.cfg").is_file():
        failures.append("RIFT configuration missing")
    return {
        "case": case_root.name,
        "ok": not failures,
        "failures": failures,
        "raw_artifacts_verified": len(raw),
        "raw_plots": raw_plots,
        "source_dem_resolution": source_dem_resolution,
        "source_dem_shape": source_dem_shape,
        "rift_cell_size_m": model_resolution,
        "raw_rainfall_records": raw_rainfall_records,
        "rift_rainfall_records": rift_rainfall_records,
        "qc_checks": len(qc["checks"]),
        "qc_warnings": qc["warnings"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    summary = pd.read_csv(root / "fresh_validation_summary.csv")
    records = [audit_case(root / case_name) for case_name in summary.case_name]
    output = root / "artifact_verification.json"
    output.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps(records, indent=2))
    return 0 if all(item["ok"] for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
