#!/usr/bin/env python3
"""Small real-provider smoke test for the optional multi-source adapters."""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from riftprep.source_adapters import (
    download_copernicus_glo30,
    download_daymet_hourly,
    download_era5_land_hourly,
    download_mrms_hourly,
    download_soilgrids_hydraulic,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=False)
    boundary = gpd.GeoDataFrame(
        {"name": ["Storrs provider smoke test"]},
        geometry=[box(-72.27, 41.78, -72.25, 41.80)],
        crs="EPSG:4326",
    )
    cases = {
        "copernicus_glo30": lambda path: download_copernicus_glo30(
            boundary, path, reuse_existing=False, max_attempts=3
        ),
        "daymet": lambda path: download_daymet_hourly(
            boundary,
            {
                "start_utc": "2020-06-01T00:00:00Z",
                "end_utc": "2020-06-02T00:00:00Z",
                "daily_distribution": "uniform_24h",
                "max_attempts": 3,
            },
            path,
            reuse_existing=False,
        ),
        "mrms": lambda path: download_mrms_hourly(
            boundary,
            {
                "start_utc": "2024-09-01T00:00:00Z",
                "end_utc": "2024-09-01T01:00:00Z",
                "product": "MultiSensor_QPE_01H_Pass2_00.00",
                "region": "CONUS",
                "max_attempts": 3,
            },
            path,
            reuse_existing=False,
        ),
        "soilgrids": lambda path: download_soilgrids_hydraulic(
            boundary,
            {"depth": "0-5cm", "quantile": "Q0.5", "max_attempts": 3},
            path,
            reuse_existing=False,
        ),
    }
    credentials = Path.home() / ".cdsapirc"
    if importlib.util.find_spec("cdsapi") is not None and credentials.is_file():
        cases["era5_land"] = lambda path: download_era5_land_hourly(
            boundary,
            {
                "start_utc": "2020-06-01T00:00:00Z",
                "end_utc": "2020-06-01T01:00:00Z",
            },
            path,
            reuse_existing=False,
        )

    results = {}
    for name, operation in cases.items():
        started = time.perf_counter()
        try:
            files, metadata = operation(args.output_directory / name)
            missing = [key for key, path in files.items() if not Path(path).is_file()]
            if missing:
                raise AssertionError(f"Adapter reported nonexistent files: {missing}")
            results[name] = {
                "status": "PASSED",
                "wall_seconds": time.perf_counter() - started,
                "file_count": len(files),
                "files": {key: str(value) for key, value in files.items()},
                "metadata": metadata,
            }
        except Exception as error:
            results[name] = {
                "status": "FAILED",
                "wall_seconds": time.perf_counter() - started,
                "error": f"{type(error).__name__}: {error}",
            }
    if "era5_land" not in results:
        results["era5_land"] = {
            "status": "NOT_RUN",
            "reason": "cdsapi and/or ~/.cdsapirc is unavailable on this account",
        }
    summary = args.output_directory / "summary.json"
    summary.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(summary.read_text(encoding="utf-8"))
    if any(result["status"] == "FAILED" for result in results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
