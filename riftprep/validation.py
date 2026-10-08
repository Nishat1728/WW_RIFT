from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import h5py
import numpy as np
import pandas as pd

from .config import GridSpec
from .drainage import RIFT_DRAINAGE_COLUMNS


RIFT_INFLOW_COLUMNS = [
    "id", "hydrograph_file", "xmin_m", "ymin_m", "xmax_m", "ymax_m",
]
RIFT_DRAINAGE_CONTROLS = {"linear", "orifice", "weir", "pump", "hybrid"}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"severity": "ERROR", "message": x} for x in self.errors]
            + [{"severity": "WARNING", "message": x} for x in self.warnings]
            + [{"severity": "PASS", "message": x} for x in self.checks]
        )

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValueError("Case validation failed:\n- " + "\n- ".join(self.errors))


def validate_case(
    grid: GridSpec,
    dem: np.ndarray,
    node_mask: np.ndarray,
    wall_elevation_m: float | None = None,
    rainfall: np.ndarray | None = None,
    rainfall_time_s: np.ndarray | None = None,
    rainfall_path: str | Path | None = None,
    rainfall_group: str = "precipitation rain [m s^-1]",
    rainfall_time_dataset: str = "time [s]",
    inflow_table: str | Path | None = None,
    drainage_map: np.ndarray | None = None,
    drainage_parameters: pd.DataFrame | None = None,
    carved_outlets: np.ndarray | None = None,
    extra_rasters: Mapping[str, np.ndarray] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    if dem.shape != grid.node_shape:
        report.errors.append(f"DEM shape {dem.shape} must equal node shape {grid.node_shape}.")
    elif not np.isfinite(dem).all():
        report.errors.append("DEM contains non-finite values.")
    else:
        report.checks.append("DEM dimensions and finite values are valid.")
    if node_mask.shape != grid.node_shape or not node_mask.any():
        report.errors.append("Active-domain node mask is empty or has the wrong shape.")
    if wall_elevation_m is not None and node_mask.any():
        if np.any(np.isclose(dem[node_mask], wall_elevation_m)):
            report.errors.append("Wall elevation occurs inside the active domain.")
        outside = ~node_mask
        opened = carved_outlets if carved_outlets is not None else np.zeros_like(node_mask)
        if not np.all(np.isclose(dem[outside & ~opened], wall_elevation_m)):
            report.errors.append("Unexpected non-wall DEM values exist outside the active domain.")
        elif carved_outlets is None or not carved_outlets.any():
            report.warnings.append("Closed domain has no carved outlet.")
        else:
            report.checks.append("Closed-domain wall and outlet cells are internally consistent.")
    if rainfall is not None:
        if rainfall.ndim != 3 or rainfall.shape[1:] not in (grid.node_shape, grid.cell_shape):
            report.errors.append("Rainfall must be [time,y,x] on the node or flow grid.")
        if rainfall_time_s is None or len(rainfall_time_s) != rainfall.shape[0] or not np.all(np.diff(rainfall_time_s) > 0):
            report.errors.append("Rainfall timestamps are missing, mismatched, or not strictly increasing.")
        if np.any(rainfall < 0) or not np.isfinite(rainfall).all():
            report.errors.append("Rainfall contains negative or non-finite values.")
    if rainfall_path is not None:
        path = Path(rainfall_path)
        if not path.is_file():
            report.errors.append(f"ATS rainfall file does not exist: {path}")
        else:
            try:
                with h5py.File(path, "r") as h5:
                    if rainfall_time_dataset not in h5:
                        report.errors.append(f"ATS rainfall is missing {rainfall_time_dataset!r}.")
                    if rainfall_group not in h5:
                        report.errors.append(f"ATS rainfall is missing group {rainfall_group!r}.")
                    if rainfall_time_dataset in h5 and rainfall_group in h5:
                        times = np.asarray(h5[rainfall_time_dataset], dtype=float)
                        keys = sorted(h5[rainfall_group].keys(), key=lambda key: int(key))
                        if times.ndim != 1 or not np.isfinite(times).all() or not np.all(np.diff(times) > 0):
                            report.errors.append("ATS rainfall time [s] must be finite and strictly increasing.")
                        if len(keys) != times.size:
                            report.errors.append("ATS rainfall record count does not match time [s].")
                        bad_shapes = [key for key in keys if h5[rainfall_group][key].shape not in (grid.node_shape, grid.cell_shape, ())]
                        if bad_shapes:
                            report.errors.append(f"ATS rainfall fields have incompatible shapes: {bad_shapes[:5]}")
                        sample_keys = sorted(set(keys[index] for index in (0, len(keys)//2, len(keys)-1))) if keys else []
                        for key in sample_keys:
                            field = np.asarray(h5[rainfall_group][key], dtype=float)
                            if not np.isfinite(field).all() or np.any(field < 0):
                                report.errors.append(f"ATS rainfall sample {key} contains invalid rates.")
                        if not report.errors or not any("ATS rainfall" in item for item in report.errors):
                            report.checks.append(
                                f"ATS rainfall schema is valid ({len(keys)} records; {rainfall_group})."
                            )
            except Exception as error:
                report.errors.append(f"ATS rainfall validation failed: {type(error).__name__}: {error}")
    if inflow_table is not None:
        table_path = Path(inflow_table)
        if not table_path.is_file():
            report.errors.append(f"Inflow boundary table does not exist: {table_path}")
        else:
            try:
                table = pd.read_csv(table_path)
                if list(table.columns) != RIFT_INFLOW_COLUMNS:
                    report.errors.append(
                        f"Inflow table columns must be exactly {RIFT_INFLOW_COLUMNS}."
                    )
                elif table.empty or table["id"].astype(str).duplicated().any():
                    report.errors.append("Inflow table must contain unique, nonempty source IDs.")
                else:
                    invalid_supports = []
                    invalid_series = []
                    for _, row in table.iterrows():
                        xmin, ymin, xmax, ymax = (float(row[name]) for name in RIFT_INFLOW_COLUMNS[2:])
                        if xmax < xmin or ymax < ymin or xmin < grid.xmin or xmax > grid.xmax or ymin < grid.ymin or ymax > grid.ymax:
                            invalid_supports.append(str(row["id"]))
                        x = 0.5 * (xmin + xmax)
                        y = 0.5 * (ymin + ymax)
                        col = int(np.clip(np.floor((x - grid.xmin) / grid.resolution), 0, grid.node_shape[1] - 1))
                        rindex = int(np.clip(np.floor((grid.ymax - y) / grid.resolution), 0, grid.node_shape[0] - 1))
                        row0, row1 = max(0, rindex - 1), min(node_mask.shape[0], rindex + 2)
                        col0, col1 = max(0, col - 1), min(node_mask.shape[1], col + 2)
                        # Boundary inflows may lie exactly on the polygon edge. RIFT maps the
                        # coordinate to a computational cell, so accept a one-node tolerance
                        # when the support touches the active domain.
                        if not bool(node_mask[row0:row1, col0:col1].any()):
                            invalid_supports.append(str(row["id"]))
                        hydro_path = table_path.parent / str(row["hydrograph_file"])
                        try:
                            series = np.loadtxt(hydro_path, delimiter=",")
                            series = np.atleast_2d(series)
                            if series.shape[1] != 2 or series.shape[0] < 2 or not np.isfinite(series).all() or not np.all(np.diff(series[:, 0]) > 0) or np.any(series[:, 1] < 0):
                                invalid_series.append(str(row["id"]))
                        except Exception:
                            invalid_series.append(str(row["id"]))
                    if invalid_supports:
                        report.errors.append(f"Inflow supports are outside the active domain: {sorted(set(invalid_supports))}")
                    if invalid_series:
                        report.errors.append(f"Invalid headerless time_s,discharge_m3_s files: {sorted(set(invalid_series))}")
                    if not invalid_supports and not invalid_series:
                        report.checks.append(f"Canonical inflow table and {len(table)} hydrographs are valid.")
            except Exception as error:
                report.errors.append(f"Inflow bundle validation failed: {type(error).__name__}: {error}")
    if drainage_map is not None:
        if drainage_map.shape != grid.node_shape:
            report.errors.append("Infrastructure raster must match the DEM-node shape.")
        if np.any(drainage_map[~node_mask] != 0):
            report.errors.append("Drainage infrastructure exists outside the active domain.")
        if drainage_parameters is None:
            report.errors.append("Infrastructure map exists without a parameter table.")
        else:
            missing = set(RIFT_DRAINAGE_COLUMNS) - set(drainage_parameters.columns)
            map_ids = set(np.unique(drainage_map).astype(int)) - {0}
            table_ids = set(drainage_parameters["type"].astype(int))
            if missing:
                report.errors.append(f"Drainage parameter columns missing: {sorted(missing)}")
            if map_ids - table_ids:
                report.errors.append(f"Drainage type IDs lack parameters: {sorted(map_ids - table_ids)}")
            invalid_controls = set(drainage_parameters.get("control", pd.Series(dtype=str)).astype(str)) - RIFT_DRAINAGE_CONTROLS
            if invalid_controls:
                report.errors.append(f"Unknown RIFT-UConn drainage controls: {sorted(invalid_controls)}")
            if not missing and not (map_ids - table_ids) and not invalid_controls:
                report.checks.append("Drainage raster IDs and 16-column hydraulic controls are valid.")
    for name, array in (extra_rasters or {}).items():
        if array.shape != grid.node_shape:
            report.errors.append(f"{name} shape does not match the DEM-node grid.")
        elif not np.isfinite(array).all():
            report.errors.append(f"{name} contains non-finite values.")
    return report
