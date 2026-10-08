"""Fresh real-provider validation for optional RIFT boundary/drainage forcings."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riftprep.artifacts import ProvenanceLedger, prepare_case_directory
from riftprep.config import GridSpec
from riftprep.managers import RawCatalog, fetch_usgs_waterdata
from riftprep.spec import ProductSpec
from riftprep.workflow import (
    _find_nwis_series,
    _prepare_coastal,
    _prepare_drainage,
    _prepare_inflows,
    _source_location,
)


START = "2023-06-10T00:00:00Z"
END = "2023-06-10T06:00:00Z"


def grid_at(series) -> GridSpec:
    longitude, latitude = _source_location(series)
    zone = int((longitude + 180) // 6) + 1
    crs = f"EPSG:{32600 + zone if latitude >= 0 else 32700 + zone}"
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
        longitude, latitude
    )
    return GridSpec(x - 500, y - 500, x + 500, y + 500, 100, crs, 10, 10)


def station_product(site: str, parameter: str, **station_options) -> ProductSpec:
    station = {"site": site, "parameter_code": parameter, **station_options}
    return ProductSpec(provider="usgs_waterdata", options={
        "start_utc": START,
        "end_utc": END,
        "collection": "continuous",
        "stations": [station],
    })


def validate_download(paths, ledger, name, product):
    raw = fetch_usgs_waterdata(
        name, product, paths, ledger, plot=True, dpi=120, reuse_existing=False
    )
    assert raw.files["download_manifest"].is_file()
    assert any(key.startswith("observations_") for key in raw.files)
    assert raw.files["plot"].is_file()
    return raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    summary = {"cache_mode": "fresh", "start_utc": START, "end_utc": END}

    tick = time.perf_counter()
    inflow_paths = prepare_case_directory(output_root / "usgs_inflow", "fresh")
    inflow_ledger = ProvenanceLedger(inflow_paths.provenance)
    inflow_product = station_product("01172010", "00060", id="river_boundary")
    inflow_raw = validate_download(
        inflow_paths, inflow_ledger, "inflows", inflow_product
    )
    inflow_catalog = RawCatalog(None, {"inflows": inflow_raw})
    inflow_series = _find_nwis_series(
        inflow_catalog, "inflows", "01172010", "00060"
    )
    inflow_table, _ = _prepare_inflows(
        inflow_product, inflow_catalog, grid_at(inflow_series), inflow_paths
    )
    assert inflow_table.is_file()
    summary["inflow"] = {
        "wall_seconds": time.perf_counter() - tick,
        "raw_artifacts": len(inflow_ledger.records),
        "provider": inflow_raw.provider,
        "rift_table": str(inflow_table),
        "time_series_id": inflow_series.get("time_series_id"),
    }

    tick = time.perf_counter()
    coastal_paths = prepare_case_directory(output_root / "usgs_coastal_stage", "fresh")
    coastal_ledger = ProvenanceLedger(coastal_paths.provenance)
    coastal_product = station_product(
        "01194796", "00065", datum_offset_m=0.0
    )
    coastal_raw = validate_download(
        coastal_paths, coastal_ledger, "coastal", coastal_product
    )
    coastal_catalog = RawCatalog(None, {"coastal": coastal_raw})
    coastal_series = _find_nwis_series(
        coastal_catalog, "coastal", "01194796", "00065"
    )
    coastal_grid = grid_at(coastal_series)
    mask = np.ones(coastal_grid.node_shape, dtype=bool)
    dem = np.zeros(coastal_grid.node_shape, dtype=float)
    sampled, stage_path = _prepare_coastal(
        coastal_product, coastal_catalog, coastal_grid, coastal_paths, dem, mask
    )
    assert stage_path.is_file() and len(sampled) == 7
    summary["coastal_stage"] = {
        "wall_seconds": time.perf_counter() - tick,
        "raw_artifacts": len(coastal_ledger.records),
        "provider": coastal_raw.provider,
        "rift_stage": str(stage_path),
        "time_series_id": coastal_series.get("time_series_id"),
    }

    tick = time.perf_counter()
    impervious = np.zeros(coastal_grid.node_shape, dtype=float)
    impervious[2:8, 2:8] = 90.0
    drainage_product = ProductSpec(
        provider="synthetic_nlcd", options={"impervious_threshold_pct": 80}
    )
    drainage_map, controls, map_path, controls_path = _prepare_drainage(
        drainage_product, RawCatalog(None, {}), coastal_grid, mask,
        impervious, coastal_paths,
    )
    assert drainage_map.sum() == 36
    assert map_path.is_file() and controls_path.is_file() and len(controls) == 2
    summary["synthetic_drainage"] = {
        "wall_seconds": time.perf_counter() - tick,
        "active_inlet_nodes": int(drainage_map.sum()),
        "rift_map": str(map_path),
        "rift_controls": str(controls_path),
    }

    summary_path = output_root / "optional_forcing_validation.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
