"""Resume the partial HUC2 notebook with verified raw caches in reuse mode."""

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
import pandas as pd
import yaml
from nbclient import NotebookClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hpc.run_six_case_30m import make_case
from riftprep.config import resolve_simulation_ready_directory


CASE_KEY = "huc2_new_england"


def _validate(case_root: Path) -> dict:
    failures = []
    resolved = yaml.safe_load(
        (case_root / "quality_control/resolved_case.yaml").read_text(encoding="utf-8")
    )
    status = pd.read_csv(case_root / "quality_control/input_status_summary.csv")
    qc = json.loads(
        (case_root / "quality_control/qc_report.json").read_text(encoding="utf-8")
    )
    grid = json.loads(
        (case_root / "quality_control/grid.json").read_text(encoding="utf-8")
    )
    land = json.loads(
        (case_root / "quality_control/land_cover_coverage.json").read_text(
            encoding="utf-8"
        )
    )
    soil = json.loads(
        (case_root / "quality_control/soil_coverage.json").read_text(encoding="utf-8")
    )
    rainfall = json.loads(
        (case_root / "quality_control/rainfall_coverage.json").read_text(
            encoding="utf-8"
        )
    )
    simulation_ready = resolve_simulation_ready_directory(case_root)
    with (simulation_ready / "dem.asc").open(encoding="utf-8") as stream:
        dem_header = dict(next(stream).split(maxsplit=1) for _ in range(6))

    if resolved["cache"]["mode"] != "reuse":
        failures.append("resume did not use cache mode reuse")
    if float(grid["resolution"]) != 30.0 or float(dem_header["cellsize"]) != 30.0:
        failures.append("final RIFT grid or DEM is not 30 m")
    if not status.status.eq("PASSED").all():
        failures.append("one or more input statuses did not pass")
    if not qc.get("ok"):
        failures.append("final RIFT QC did not pass")
    if land.get("status") != "PASSED" or not land.get("complete"):
        failures.append("land-cover processing did not pass")
    if soil.get("status") != "PASSED" or not soil.get("complete"):
        failures.append("soil processing did not pass")
    if rainfall.get("status") != "PASSED" or not rainfall.get(
        "rift_forcing_complete"
    ):
        failures.append("rainfall processing did not pass")

    soil_raw = case_root / "raw/soil/ssurgo_native.gpkg"
    rainfall_h5 = simulation_ready / "rainfall_ats.h5"
    if not soil_raw.is_file() or soil_raw.stat().st_size == 0:
        failures.append("final raw SSURGO product is missing or empty")
    if not rainfall_h5.is_file() or rainfall_h5.stat().st_size == 0:
        failures.append("RIFT rainfall HDF5 is missing or empty")
    return {
        "validation_status": "PASSED" if not failures else "FAILED",
        "validation_failures": failures,
        "cache_mode": resolved["cache"]["mode"],
        "rift_dem_cellsize_m": float(dem_header["cellsize"]),
        "input_statuses": dict(zip(status.input, status.status)),
        "land_cover_status": land.get("status"),
        "soil_status": soil.get("status"),
        "rainfall_status": rainfall.get("status"),
        "rainfall_zero_replacement_cells": rainfall.get(
            "missing_active_cells", 0
        ),
        "manual_land_cover_values": land.get("manual_resolutions", {}),
        "manual_soil_values": soil.get("manual_resolutions", {}),
        "raw_soil_path": str(soil_raw),
        "raw_soil_bytes": soil_raw.stat().st_size if soil_raw.is_file() else 0,
        "rainfall_hdf5_path": str(rainfall_h5),
        "rainfall_hdf5_bytes": (
            rainfall_h5.stat().st_size if rainfall_h5.is_file() else 0
        ),
    }


def run(output_root: Path, repository: Path, preflight_path: Path) -> int:
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight.get("status") != "PASSED":
        raise RuntimeError(f"HUC2 resume preflight did not pass: {preflight_path}")
    case_root = output_root / "fresh30_huc2_new_england"
    if Path(preflight["case_root"]).resolve() != case_root.resolve():
        raise RuntimeError("Preflight case root does not match the requested output root.")

    results = output_root / "benchmark_results"
    configs = output_root / "matrix_configs"
    executed = output_root / "executed_notebooks"
    for directory in (results, configs, executed):
        directory.mkdir(parents=True, exist_ok=True)

    record = {
        "case": CASE_KEY,
        "run_kind": "resume_from_failed_soil_stage",
        "status": "failed",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "preflight_path": str(preflight_path),
        "preflight_status": preflight["status"],
        "cache_mode": "reuse",
        "existing_raw_data_preserved": True,
    }
    notebook = None
    tick = time.perf_counter()
    try:
        payload = make_case(CASE_KEY, output_root)
        payload["cache"] = {"mode": "reuse"}
        config_path = configs / "huc2_new_england_resume.yaml"
        config_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        record["config_path"] = str(config_path)
        record["explicit_manual_soil_values"] = payload["soil"]["options"].get(
            "manual_fill_values", {}
        )
        os.environ["RIFTPREP_CASE_CONFIG"] = str(config_path)
        notebook = nbformat.read(repository / "RIFT_Input_Builder.ipynb", as_version=4)
        NotebookClient(
            notebook,
            timeout=43200,
            kernel_name="python3",
            allow_errors=False,
            resources={"metadata": {"path": str(repository)}},
        ).execute()
        record.update(_validate(case_root))
        record["status"] = (
            "passed" if record["validation_status"] == "PASSED" else "failed"
        )
    except BaseException as error:
        record.update(
            {
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        record["completed_utc"] = datetime.now(timezone.utc).isoformat()
        record["resume_wall_seconds"] = time.perf_counter() - tick
        if notebook is not None:
            nbformat.write(notebook, executed / "huc2_new_england_resume.ipynb")
        os.environ.pop("RIFTPREP_CASE_CONFIG", None)

    result_path = results / "huc2_new_england_resume.json"
    result_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    pd.DataFrame(
        [
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in record.items()
                if key != "traceback"
            }
        ]
    ).to_csv(results / "huc2_new_england_resume.csv", index=False)
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    args = parser.parse_args()
    return run(
        args.output_root.expanduser().resolve(),
        args.repository.expanduser().resolve(),
        args.preflight.expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
