"""Execute the published notebook for one fresh 30 m validation case."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import nbformat
from nbclient import NotebookClient
import pandas as pd
import rasterio
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hpc.run_six_case_30m import CASE_KEYS, make_case


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {secs:02d}s"


def _validate(case_key: str, output_root: Path, total_seconds: float) -> dict:
    case_root = output_root / f"fresh30_{case_key}"
    timings = pd.read_csv(case_root / "quality_control/timings/wall_clock.csv")
    status = pd.read_csv(case_root / "quality_control/input_status_summary.csv")
    grid = json.loads(
        (case_root / "quality_control/grid.json").read_text(encoding="utf-8")
    )
    qc = json.loads(
        (case_root / "quality_control/qc_report.json").read_text(encoding="utf-8")
    )
    land = json.loads(
        (case_root / "quality_control/land_cover_coverage.json").read_text(
            encoding="utf-8"
        )
    )
    soil = json.loads(
        (case_root / "quality_control/soil_coverage.json").read_text(encoding="utf-8")
    )
    rain = json.loads(
        (case_root / "quality_control/rainfall_coverage.json").read_text(
            encoding="utf-8"
        )
    )
    resolved = yaml.safe_load(
        (case_root / "quality_control/resolved_case.yaml").read_text(encoding="utf-8")
    )
    dem_download = float(
        timings.loc[timings.stage.eq("download_raw_dem"), "wall_seconds"].sum()
    )
    with (case_root / "rift_inputs/dem.asc").open(encoding="utf-8") as stream:
        header = dict(next(stream).split(maxsplit=1) for _ in range(6))
    failures = []
    if resolved["cache"]["mode"] != "fresh":
        failures.append("cache mode is not fresh")
    if float(grid["resolution"]) != 30.0 or float(header["cellsize"]) != 30.0:
        failures.append("RIFT DEM/grid cell size is not 30 m")
    if not qc.get("ok"):
        failures.append("final RIFT QC failed")
    if not status.status.eq("PASSED").all():
        failures.append("one or more input statuses did not pass")
    if not land.get("complete") or land.get("status") != "PASSED":
        failures.append("land-cover coverage did not pass")
    cover_class_quality = land.get("cover_class_quality", {})
    if cover_class_quality.get("raw_product_modified"):
        failures.append("the raw NLCD cover product was modified")
    percentage_quality = land.get("percent_range_quality", {})
    over_100_corrections = {
        name: int(details.get("over_100_corrected_active_nodes", 0))
        for name, details in percentage_quality.items()
    }
    if any(details.get("raw_product_modified") for details in percentage_quality.values()):
        failures.append("a raw land-cover product was modified")
    correction_maps = land.get("percentage_correction_policy", {}).get(
        "before_after_maps", {}
    )
    for stage in ("before", "after"):
        map_path = correction_maps.get(stage)
        if not map_path or not Path(map_path).is_file():
            failures.append(f"land-cover percentage {stage} correction map is missing")
    if not soil.get("complete") or soil.get("status") != "PASSED":
        failures.append("soil coverage did not pass")
    if rain.get("status") != "PASSED" or not rain.get("rift_forcing_complete"):
        failures.append("rainfall forcing did not pass")
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "cache_mode": resolved["cache"]["mode"],
        "native_dem_product_m": int(resolved["dem"]["options"]["resolution_m"]),
        "rift_dem_cellsize_m": float(header["cellsize"]),
        "land_cover_status": land.get("status"),
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
        "land_cover_over_100_corrections": over_100_corrections,
        "land_cover_percentage_correction_maps": correction_maps,
        "soil_status": soil.get("status"),
        "rainfall_status": rain.get("status"),
        "rainfall_zero_replacement_cells": rain.get("missing_active_cells", 0),
        "manual_land_cover_values": land.get("manual_resolutions", {}),
        "manual_soil_values": soil.get("manual_resolutions", {}),
        "download_raw_dem_wall_seconds": dem_download,
        "download_raw_dem_duration": _duration(dem_download),
        "total_notebook_wall_seconds": total_seconds,
        "total_notebook_duration": _duration(total_seconds),
        "case_root": str(case_root),
    }


def run_one(case_key: str, output_root: Path, repository: Path) -> int:
    results = output_root / "benchmark_results"
    configs = output_root / "matrix_configs"
    executed = output_root / "executed_notebooks"
    for directory in (results, configs, executed):
        directory.mkdir(parents=True, exist_ok=True)
    record = {
        "case": case_key,
        "status": "failed",
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    notebook_output = executed / f"{case_key}.ipynb"
    tick = time.perf_counter()
    notebook = None
    try:
        payload = make_case(case_key, output_root)
        payload.setdefault("initial_state", {
            "enabled": True, "provider": "dry_zero", "options": {},
        })
        record["manual_soil_options"] = payload["soil"]["options"].get(
            "manual_fill_values", {}
        )
        config_path = configs / f"{case_key}.yaml"
        config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        os.environ["RIFTPREP_CASE_CONFIG"] = str(config_path)
        notebook = nbformat.read(repository / "RIFT_Input_Builder.ipynb", as_version=4)
        NotebookClient(
            notebook,
            timeout=43200,
            kernel_name="python3",
            allow_errors=False,
            resources={"metadata": {"path": str(repository)}},
        ).execute()
        total = time.perf_counter() - tick
        record.update(_validate(case_key, output_root, total))
        record["completed_utc"] = datetime.now(timezone.utc).isoformat()
    except BaseException as error:
        record.update({
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "total_notebook_wall_seconds": time.perf_counter() - tick,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        })
    finally:
        if notebook is not None:
            nbformat.write(notebook, notebook_output)
        os.environ.pop("RIFTPREP_CASE_CONFIG", None)
    result_path = results / f"{case_key}.json"
    result_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    pd.DataFrame([{k: json.dumps(v) if isinstance(v, (dict, list)) else v
                   for k, v in record.items() if k != "traceback"}]).to_csv(
        results / f"{case_key}.csv", index=False
    )
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "passed" else 1


def aggregate(output_root: Path) -> int:
    records = []
    for case_key in CASE_KEYS:
        path = output_root / "benchmark_results" / f"{case_key}.json"
        records.append(
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file() else {"case": case_key, "status": "missing"}
        )
    (output_root / "six_case_30m_summary.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8"
    )
    pd.DataFrame([
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v
         for k, v in row.items() if k != "traceback"}
        for row in records
    ]).to_csv(output_root / "six_case_30m_summary.csv", index=False)
    return 0 if all(row.get("status") == "passed" for row in records) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--repository", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--case", choices=[*CASE_KEYS, "aggregate"], required=True)
    args = parser.parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    repository = Path(args.repository).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if args.case == "aggregate":
        return aggregate(output_root)
    return run_one(args.case, output_root, repository)


if __name__ == "__main__":
    raise SystemExit(main())
