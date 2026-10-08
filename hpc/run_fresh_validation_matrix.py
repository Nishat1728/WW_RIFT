"""Real-provider, no-cache integration matrix for Storrs HPC.

All cases use the same source suite and event window.  The domain and RIFT grid
resolution vary; selected native product resolutions do not.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path

import pandas as pd
import yaml

from riftprep.workflow import run_case


COMMON = {
    "dem": {
        "enabled": True, "provider": "watershed_workflow_3dep",
        "options": {"resolution_m": 30},
    },
    "hydrography": {"enabled": True, "provider": "usgs_3dhp", "options": {}},
    "land_cover": {
        "enabled": True, "provider": "watershed_workflow_nlcd",
        "options": {"year": 2021, "location": "L48", "variables": ["cover", "impervious", "canopy"]},
    },
    "soil": {
        "enabled": True, "provider": "watershed_workflow_ssurgo",
        "options": {"initial_saturation": 0.5},
    },
    "rainfall": {
        "enabled": True, "provider": "watershed_workflow_aorc",
        "options": {
            "start_utc": "2023-06-10T00:00:00Z",
            "end_utc": "2023-06-10T06:00:00Z",
            "variables": ["APCP_surface"],
        },
    },
    "inflows": {"enabled": False, "provider": "none"},
    "coastal": {"enabled": False, "provider": "none"},
    "drainage": {"enabled": False, "provider": "none"},
    "river_bathymetry": {"apply_bathymetry": False},
    "cache": {"mode": "fresh"},
    "plots": {"raw_products": True, "processed_products": True, "formats": ["png"], "dpi": 140},
    "simulation": {"start_time_s": 0, "end_time_s": 21600, "output_interval_s": 3600},
}


CASES = {
    "rectangle": {
        "case_name": "fresh_rectangle",
        "domain": {
            "mode": "rectangle", "bounds": [-72.31, 41.77, -72.23, 41.83],
            "bounds_crs": "EPSG:4326", "target_crs": "auto_utm", "cell_size_m": 100,
            "download_buffer_cells": 2, "boundary_rule": "cell_center",
            "outside_dem_mode": "real_terrain",
        },
    },
    "huc12": {
        "case_name": "fresh_huc12",
        "domain": {
            "mode": "huc", "huc_code": "010802050801", "target_crs": "auto_utm",
            "cell_size_m": 250, "download_buffer_cells": 2,
            "boundary_rule": "cell_center", "outside_dem_mode": "wall", "wall_elevation_m": 9999,
        },
        "outlets": {
            "source": "auto_hydrography_lowest", "method": "open_corridor",
            "elevation_mode": "terrain_matched", "default_width_m": 500,
        },
    },
    "huc10": {
        "case_name": "fresh_huc10",
        "domain": {
            "mode": "huc", "huc_code": "0108020508", "target_crs": "auto_utm",
            "cell_size_m": 500, "download_buffer_cells": 2,
            "boundary_rule": "cell_center", "outside_dem_mode": "wall", "wall_elevation_m": 9999,
        },
        "outlets": {
            "source": "auto_hydrography_lowest", "method": "open_corridor",
            "elevation_mode": "terrain_matched", "default_width_m": 1000,
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    config_dir = output_root / "matrix_configs"
    config_dir.mkdir(exist_ok=True)
    selected = CASES if args.case == "all" else {args.case: CASES[args.case]}
    rows = []
    for key, case in selected.items():
        payload = {**COMMON, **case, "output_root": str(output_root)}
        config_path = config_dir / f"{key}.yaml"
        config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        started = time.perf_counter()
        row = {"case": key, "case_name": payload["case_name"], "status": "failed"}
        try:
            result = run_case(config_path)
            total = time.perf_counter() - started
            stage = pd.read_csv(result.timing_csv_path)
            row.update({
                "status": "passed",
                "total_wall_seconds": total,
                "measured_stage_seconds": float(stage.wall_seconds.sum()),
                "raw_artifacts": len(json.loads(result.raw_catalog_path.read_text(encoding="utf-8"))),
                "qc_report": str(result.qc_json_path),
                "timings": str(result.timing_csv_path),
            })
        except Exception as error:
            row.update({
                "total_wall_seconds": time.perf_counter() - started,
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            })
        rows.append(row)
        pd.DataFrame(rows).to_csv(output_root / "fresh_validation_summary.csv", index=False)
        (output_root / "fresh_validation_summary.json").write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )
    print(pd.DataFrame(rows).to_string(index=False))
    return 0 if all(row["status"] == "passed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
