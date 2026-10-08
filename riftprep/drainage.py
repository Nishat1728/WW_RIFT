from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.transform import from_origin

from .config import GridSpec


RIFT_DRAINAGE_COLUMNS = [
    "type", "name", "control", "capture_capacity_m_s", "storage_capacity_m",
    "base_release_m_s", "recovery_time_s", "inlet_area_m2", "inlet_cd",
    "outlet_area_m2", "outlet_cd", "control_elevation_m", "weir_length_m",
    "weir_coefficient", "tailwater_m", "pump_capacity_m_s",
]


def rasterize_drainage_features(
    features: gpd.GeoDataFrame,
    grid: GridSpec,
    active_mask: np.ndarray,
    type_field: str = "type",
    priority: Sequence[int] = (5, 2, 1, 3, 4),
) -> tuple[np.ndarray, pd.DataFrame]:
    """Rasterize drainage assets onto the DEM-node grid with explicit priority."""
    data = features.to_crs(grid.crs).copy()
    if type_field not in data:
        raise KeyError(f"Missing drainage type field: {type_field}")
    transform = from_origin(
        grid.xmin - grid.resolution / 2, grid.ymax + grid.resolution / 2,
        grid.resolution, grid.resolution,
    )
    result = np.zeros(grid.node_shape, dtype=np.int32)
    audit = []
    for type_id in reversed(tuple(priority)):
        subset = data[data[type_field].astype(int) == int(type_id)]
        if subset.empty:
            continue
        layer = rasterize(
            [(geom, int(type_id)) for geom in subset.geometry if geom is not None],
            out_shape=grid.node_shape, transform=transform, fill=0, all_touched=True,
        )
        count_before = int(np.count_nonzero(result))
        result[layer > 0] = layer[layer > 0]
        audit.append({"type": int(type_id), "features": len(subset), "cells": int(np.sum(result == type_id)), "previous_cells": count_before})
    result[~active_mask] = 0
    return result, pd.DataFrame(audit)


def build_drainage_parameter_table(rows: Sequence[Mapping]) -> pd.DataFrame:
    table = pd.DataFrame(rows)
    missing = [c for c in RIFT_DRAINAGE_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"Drainage parameter fields are missing: {missing}")
    table = table[RIFT_DRAINAGE_COLUMNS].copy()
    allowed = {"linear", "orifice", "weir", "pump", "hybrid"}
    invalid = set(table["control"].astype(str)) - allowed
    if invalid:
        raise ValueError(f"Unknown drainage controls: {sorted(invalid)}")
    numeric = [c for c in RIFT_DRAINAGE_COLUMNS if c not in ("name", "control")]
    table[numeric] = table[numeric].apply(pd.to_numeric)
    if (table[[c for c in numeric if c != "type"]] < 0).any().any():
        raise ValueError("Drainage parameters cannot be negative.")
    return table


def write_external_drainage_series(path: str | Path, time_s, rate, units: str = "m/s") -> Path:
    factors = {"m/s": 1.0, "mm/hr": 1e-3 / 3600, "in/hr": 0.0254 / 3600}
    if units not in factors:
        raise ValueError(f"Unsupported drainage units: {units}")
    frame = pd.DataFrame({"time_s": np.asarray(time_s, float), "rate_m_s": np.asarray(rate, float) * factors[units]})
    if not np.all(np.diff(frame.time_s) > 0) or (frame.rate_m_s < 0).any():
        raise ValueError("Drainage times must increase strictly and rates must be nonnegative.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, header=False)
    return path
