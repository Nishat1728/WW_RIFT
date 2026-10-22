"""Execute one locked HUC case through the published notebook from fresh data."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nbformat
from nbclient import NotebookClient
import pandas as pd
import yaml

from riftprep.config import resolve_simulation_ready_directory


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def duration(seconds: float) -> str:
    hours, remainder = divmod(int(round(float(seconds))), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {secs:02d}s"


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def resolve_rift_dem_path(case_root: Path) -> Path:
    """Return the DEM path from the notebook's final-input directory contract.

    New cases publish RIFT inputs under ``simulation_ready/``.  The shared
    resolver also preserves the legacy ``rift_inputs/`` directory for an
    explicitly resumed legacy case, so validation cannot silently disagree
    with the notebook/artifact contract.
    """

    return resolve_simulation_ready_directory(case_root) / "dem.asc"


def read_case_row(preflight_root: Path, case_index: int) -> dict[str, str]:
    with (preflight_root / "array_index_map.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    matches = [row for row in rows if int(row["global_index"]) == case_index]
    if len(matches) != 1:
        raise ValueError(f"Expected one preflight row for index {case_index}; got {len(matches)}")
    return matches[0]


def validate_case(case_root: Path, config: dict[str, Any], total_seconds: float) -> dict[str, Any]:
    failures: list[str] = []
    qc_root = case_root / "quality_control"
    timings = pd.read_csv(qc_root / "timings" / "wall_clock.csv")
    statuses = pd.read_csv(qc_root / "input_status_summary.csv")
    grid = json.loads((qc_root / "grid.json").read_text(encoding="utf-8"))
    qc = json.loads((qc_root / "qc_report.json").read_text(encoding="utf-8"))
    land = json.loads((qc_root / "land_cover_coverage.json").read_text(encoding="utf-8"))
    soil = json.loads((qc_root / "soil_coverage.json").read_text(encoding="utf-8"))
    rain = json.loads((qc_root / "rainfall_coverage.json").read_text(encoding="utf-8"))
    resolved = yaml.safe_load((qc_root / "resolved_case.yaml").read_text(encoding="utf-8"))
    raw_catalog_path = qc_root / "provenance" / "raw_products.json"
    raw_catalog = json.loads(raw_catalog_path.read_text(encoding="utf-8"))
    run_manifest = qc_root / "provenance" / "run_manifest.json"

    with resolve_rift_dem_path(case_root).open(encoding="utf-8") as stream:
        dem_header = dict(stream.readline().split(maxsplit=1) for _ in range(6))
    dem_cellsize = float(dem_header["cellsize"])
    cache_mode = resolved.get("cache", {}).get("mode")
    native_dem_m = int(resolved.get("dem", {}).get("options", {}).get("resolution_m", -1))

    if cache_mode != "fresh":
        failures.append(f"resolved cache mode is {cache_mode!r}, not 'fresh'")
    if config.get("cache", {}).get("mode") != "fresh":
        failures.append("submitted config did not request fresh cache mode")
    if native_dem_m != 30:
        failures.append(f"native DEM product is {native_dem_m} m, not 30 m")
    if float(grid.get("resolution", -1)) != 30.0 or dem_cellsize != 30.0:
        failures.append("final RIFT grid/DEM is not 30 m")
    if not bool(qc.get("ok")):
        failures.append("final QC report did not pass")
    if not statuses.status.eq("PASSED").all():
        failures.append("one or more input status rows did not pass")
    if land.get("status") != "PASSED" or not land.get("complete"):
        failures.append("land-cover coverage did not pass")
    if soil.get("status") != "PASSED" or not soil.get("complete"):
        failures.append("soil coverage did not pass")
    if rain.get("status") != "PASSED" or not rain.get("rift_forcing_complete"):
        failures.append("rainfall forcing did not pass")
    if not run_manifest.is_file():
        failures.append("run provenance manifest is missing")

    cover_quality = land.get("cover_class_quality", {})
    if cover_quality.get("raw_product_modified"):
        failures.append("raw NLCD cover data were modified")
    percentage_quality = land.get("percent_range_quality", {})
    if any(row.get("raw_product_modified") for row in percentage_quality.values()):
        failures.append("a raw NLCD percentage product was modified")

    raw_root = (case_root / "raw").resolve()
    raw_paths: list[Path] = []
    for artifact in raw_catalog:
        path = Path(artifact["path"]).resolve()
        raw_paths.append(path)
        if not path.is_file():
            failures.append(f"raw artifact is missing: {path}")
        if not is_relative_to(path, raw_root):
            failures.append(f"raw artifact is outside this case's raw directory: {path}")
    dem_records = [artifact for artifact in raw_catalog if artifact.get("product") == "dem"]
    if not dem_records:
        failures.append(
            "expected at least one preserved raw DEM artifact in the provenance catalog"
        )
    elif any(
        not is_relative_to(Path(record["path"]).resolve(), raw_root)
        for record in dem_records
    ):
        failures.append("a preserved raw DEM artifact is outside this case's raw directory")

    dem_download_seconds = float(
        timings.loc[timings.stage.eq("download_raw_dem"), "wall_seconds"].sum()
    )
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "cache_mode": cache_mode,
        "native_dem_product_m": native_dem_m,
        "rift_dem_cellsize_m": dem_cellsize,
        "input_statuses": dict(zip(statuses["input"], statuses["status"])),
        "land_cover_status": land.get("status"),
        "soil_status": soil.get("status"),
        "rainfall_status": rain.get("status"),
        "rainfall_zero_replacement_cells": int(rain.get("missing_active_cells", 0)),
        "manual_land_cover_values": land.get("manual_resolutions", {}),
        "manual_soil_values": soil.get("manual_resolutions", {}),
        "raw_product_count": len(raw_catalog),
        "raw_products_all_isolated": all(is_relative_to(path, raw_root) for path in raw_paths),
        "raw_product_modified": bool(
            cover_quality.get("raw_product_modified")
            or any(row.get("raw_product_modified") for row in percentage_quality.values())
        ),
        "download_raw_dem_wall_seconds": dem_download_seconds,
        "download_raw_dem_duration": duration(dem_download_seconds),
        "total_notebook_wall_seconds": total_seconds,
        "total_notebook_duration": duration(total_seconds),
        "case_bytes": directory_bytes(case_root),
        "case_root": str(case_root),
        "timing_csv": str(qc_root / "timings" / "wall_clock.csv"),
        "run_manifest": str(run_manifest),
    }


def run(repository: Path, preflight_root: Path, matrix_root: Path, case_index: int) -> int:
    repository = repository.resolve()
    preflight_root = preflight_root.resolve()
    matrix_root = matrix_root.resolve()
    row = read_case_row(preflight_root, case_index)
    config_path = (preflight_root / row["initial_config"]).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    case_name = str(config["case_name"])
    expected_parent = (matrix_root / "cases").resolve()
    configured_parent = Path(config["output_root"]).resolve()
    case_root = configured_parent / case_name
    if configured_parent != expected_parent:
        raise ValueError(
            f"Config output root {configured_parent} does not match {expected_parent}"
        )
    if config.get("cache", {}).get("mode") != "fresh":
        raise ValueError("Locked initial config must use cache.mode=fresh")
    if case_root.exists():
        raise FileExistsError(f"Fresh case root already exists: {case_root}")

    results_root = matrix_root / "benchmark_results"
    executed_root = matrix_root / "executed_notebooks"
    results_root.mkdir(parents=True, exist_ok=True)
    executed_root.mkdir(parents=True, exist_ok=True)
    result_path = results_root / f"{case_name}.json"
    csv_path = results_root / f"{case_name}.csv"
    notebook_output = executed_root / f"{case_name}.ipynb"
    if result_path.exists() or csv_path.exists() or notebook_output.exists():
        raise FileExistsError(f"Fresh result artifact already exists for {case_name}")

    record: dict[str, Any] = {
        "case": case_name,
        "case_index": case_index,
        "huc_level": int(row["level"]),
        "huc_code": row["huc"],
        "run_kind": "full_notebook",
        "status": "failed",
        "cache_mode_requested": "fresh",
        "started_utc": utc_now(),
        "config_path": str(config_path),
    }
    notebook = None
    started = time.perf_counter()
    try:
        os.environ["RIFTPREP_CASE_CONFIG"] = str(config_path)
        notebook = nbformat.read(repository / "RIFT_Input_Builder.ipynb", as_version=4)
        NotebookClient(
            notebook,
            timeout=43200,
            kernel_name="python3",
            allow_errors=False,
            resources={"metadata": {"path": str(repository)}},
        ).execute()
        total_seconds = time.perf_counter() - started
        record.update(validate_case(case_root, config, total_seconds))
        record["completed_utc"] = utc_now()
    except BaseException as error:
        record.update(
            {
                "completed_utc": utc_now(),
                "total_notebook_wall_seconds": time.perf_counter() - started,
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        if notebook is not None:
            nbformat.write(notebook, notebook_output)
        os.environ.pop("RIFTPREP_CASE_CONFIG", None)

    result_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(
        [
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in record.items()
                if key != "traceback"
            }
        ]
    ).to_csv(csv_path, index=False)
    print(json.dumps(record, indent=2))
    return 0 if record.get("status") == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--preflight-root", required=True)
    parser.add_argument("--matrix-root", required=True)
    parser.add_argument("--case-index", required=True, type=int)
    args = parser.parse_args()
    return run(
        Path(args.repository),
        Path(args.preflight_root),
        Path(args.matrix_root),
        args.case_index,
    )


if __name__ == "__main__":
    raise SystemExit(main())
