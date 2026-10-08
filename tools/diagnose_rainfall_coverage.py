#!/usr/bin/env python3
"""Create rainfall coverage diagnostics from already-saved raw inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd

from riftprep.config import GridSpec
from riftprep.processing import prepare_aorc_event
from riftprep.spatial import create_domain_masks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Saved raw rainfall NetCDF")
    parser.add_argument("--boundary", required=True, help="Saved study-area vector")
    parser.add_argument("--grid-json", required=True, help="Saved RIFT grid.json")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--boundary-rule", default="cell_center")
    args = parser.parse_args()

    grid = GridSpec(**json.loads(Path(args.grid_json).read_text(encoding="utf-8")))
    boundary = gpd.read_file(args.boundary).to_crs(grid.crs)
    _, cell_mask = create_domain_masks(boundary, grid, args.boundary_rule)
    output = Path(args.output_directory)
    output.mkdir(parents=True, exist_ok=True)
    try:
        prepare_aorc_event(
            args.source,
            output / "rainfall_diagnostic_event.nc",
            output / "rainfall_diagnostic.h5",
            grid,
            cell_mask,
            args.start_utc,
            args.end_utc,
            coverage_report_path=output / "rainfall_coverage.json",
            uncovered_policy="error",
        )
    except ValueError as error:
        if (output / "rainfall_coverage.json").is_file():
            print(error)
            return 0
        raise
    print("Rainfall covers every active RIFT cell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
