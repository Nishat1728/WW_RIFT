"""Provider-neutral transformations from saved raw products to RIFT inputs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

import geopandas as gpd
import h5py
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject

from .config import GridSpec
from .plotting import add_map_furniture
from .spatial import align_raster
from .writers import write_esri_ascii


WATER_INTRINSIC_TO_KSAT = 1000.0 * 9.80665 / 1.0e-3
NLCD_MANNING = {
    11: 0.030, 12: 0.040,
    21: 0.040, 22: 0.060, 23: 0.100, 24: 0.150,
    31: 0.035, 41: 0.150, 42: 0.180, 43: 0.160, 52: 0.120,
    71: 0.035, 72: 0.040, 73: 0.040, 74: 0.040,
    81: 0.040, 82: 0.045, 90: 0.100, 95: 0.080,
}

# GDAL/rasterio bilinear reprojection can put a mathematically exact percentage
# endpoint a few floating-point ULPs outside [0, 100].  This tolerance separates
# numerical endpoint noise in the QC report; all processed values above 100 are
# reported and capped to 100 under the explicit notebook policy.
LAND_PERCENT_ROUNDOFF_TOLERANCE = 1.0e-9
NLCD_CANOPY_SEMANTIC_NODATA = (254.0, 255.0)


def _validate_manual_land_value(variable: str, value: float) -> float:
    value = float(value)
    if not np.isfinite(value):
        raise ValueError(f"Manual {variable} constant must be finite.")
    if variable == "cover":
        if not value.is_integer() or int(value) not in NLCD_MANNING:
            raise ValueError(
                "Manual cover constant must be an integer NLCD class supported by "
                f"the Manning lookup: {sorted(NLCD_MANNING)}"
            )
    elif variable in {"impervious", "canopy"} and not 0 <= value <= 100:
        raise ValueError(f"Manual {variable} constant must be between 0 and 100 percent.")
    return value


def _flag_invalid_land_cover_classes(
    values: np.ndarray,
    node_mask: np.ndarray,
    integer_tolerance: float = 1.0e-6,
) -> dict:
    """Mark unsupported cover classes as NoData in the processing copy."""
    finite = np.isfinite(values)
    rounded = np.rint(values)
    integer_valued = finite & (np.abs(values - rounded) <= integer_tolerance)
    supported = integer_valued & np.isin(
        rounded, np.asarray(sorted(NLCD_MANNING), dtype=float)
    )
    invalid = finite & ~supported
    invalid_active = invalid & node_mask
    unsupported_integer = integer_valued & ~supported
    non_integer = finite & ~integer_valued

    def counts(mask: np.ndarray) -> dict[str, int]:
        selected = values[mask]
        if not selected.size:
            return {}
        unique, occurrences = np.unique(selected, return_counts=True)
        result: dict[str, int] = {}
        for value, count in zip(unique, occurrences):
            key = str(int(value)) if float(value).is_integer() else f"{float(value):.12g}"
            result[key] = int(count)
        return result

    report = {
        "allowed_classes": sorted(NLCD_MANNING),
        "integer_tolerance": integer_tolerance,
        "policy": "mark_invalid_in_processing_copy_then_require_user_resolution",
        "raw_product_modified": False,
        "invalid_active_nodes": int(np.count_nonzero(invalid_active)),
        "invalid_total_cells": int(np.count_nonzero(invalid)),
        "unsupported_integer_active_nodes": int(
            np.count_nonzero(unsupported_integer & node_mask)
        ),
        "non_integer_active_nodes": int(np.count_nonzero(non_integer & node_mask)),
        "unsupported_active_class_counts": counts(unsupported_integer & node_mask),
        "non_integer_active_value_counts": counts(non_integer & node_mask),
        "processed_values_marked_nodata": bool(np.any(invalid)),
    }
    values[invalid] = np.nan
    return report


def _normalize_land_percentage_roundoff(
    values: np.ndarray,
    node_mask: np.ndarray,
    tolerance: float = LAND_PERCENT_ROUNDOFF_TOLERANCE,
) -> dict:
    """Normalize percentage values while preserving the downloaded raw product.

    Values above 100 percent are capped to 100 in the processing copy and fully
    reported.  Small negative floating-point noise is normalized to zero, while
    materially negative values remain a quality-control gap that needs an
    explicit user resolution.
    """
    finite = np.isfinite(values)
    near_zero = finite & (values < 0.0) & (values >= -tolerance)
    near_hundred = finite & (values > 100.0) & (values <= 100.0 + tolerance)
    above_hundred = finite & (values > 100.0)
    material_above_hundred = above_hundred & ~near_hundred
    normalized = near_zero | near_hundred
    normalized_active = normalized & node_mask
    above_hundred_active = above_hundred & node_mask
    material_above_hundred_active = material_above_hundred & node_mask
    above_hundred_active_values = values[above_hundred_active]
    above_hundred_values = values[above_hundred]

    values[near_zero] = 0.0
    values[above_hundred] = 100.0

    invalid = np.isfinite(values) & (values < 0.0)
    invalid_active = invalid & node_mask
    invalid_active_values = values[invalid_active]
    result = {
        "allowed_range_percent": [0.0, 100.0],
        "roundoff_tolerance": tolerance,
        "roundoff_normalized_active_nodes": int(np.count_nonzero(normalized_active)),
        "roundoff_normalized_total_cells": int(np.count_nonzero(normalized)),
        "over_100_policy": "cap_processed_value_to_100",
        "over_100_corrected_active_nodes": int(
            np.count_nonzero(above_hundred_active)
        ),
        "over_100_corrected_total_cells": int(np.count_nonzero(above_hundred)),
        "material_over_100_corrected_active_nodes": int(
            np.count_nonzero(material_above_hundred_active)
        ),
        "material_over_100_corrected_total_cells": int(
            np.count_nonzero(material_above_hundred)
        ),
        "raw_product_modified": False,
        "processed_values_modified": bool(np.any(above_hundred)),
        "invalid_active_nodes": int(np.count_nonzero(invalid_active)),
        "invalid_total_cells": int(np.count_nonzero(invalid)),
    }
    if above_hundred_active_values.size:
        result["over_100_original_active_min"] = float(
            np.min(above_hundred_active_values)
        )
        result["over_100_original_active_max"] = float(
            np.max(above_hundred_active_values)
        )
    if above_hundred_values.size:
        result["over_100_original_total_min"] = float(
            np.min(above_hundred_values)
        )
        result["over_100_original_total_max"] = float(
            np.max(above_hundred_values)
        )
    if invalid_active_values.size:
        result["invalid_active_min"] = float(np.min(invalid_active_values))
        result["invalid_active_max"] = float(np.max(invalid_active_values))

    # Negative values still follow the user-controlled NoData policy.  The saved
    # raw product is untouched in every case.
    values[invalid] = np.nan
    return result


def _count_raster_codes(path: str | Path, codes: tuple[float, ...]) -> dict[str, int]:
    """Count exact product flag codes without changing the saved raw raster."""
    counts = {str(int(code) if float(code).is_integer() else code): 0 for code in codes}
    with rasterio.open(path) as source:
        for _, window in source.block_windows(1):
            values = source.read(1, window=window)
            for code in codes:
                key = str(int(code) if float(code).is_integer() else code)
                counts[key] += int(np.count_nonzero(values == code))
    return counts


def _active_coverage_stats(values: np.ndarray, node_mask: np.ndarray, grid: GridSpec) -> dict:
    """Summarize active-node NoData without materializing a domain-sized index array."""
    active_nodes = int(np.count_nonzero(node_mask))
    missing_nodes = 0
    row_min = values.shape[0]
    row_max = -1
    col_min = values.shape[1]
    col_max = -1
    for row0 in range(0, values.shape[0], 2048):
        row1 = min(row0 + 2048, values.shape[0])
        missing = node_mask[row0:row1] & ~np.isfinite(values[row0:row1])
        count = int(np.count_nonzero(missing))
        if not count:
            continue
        missing_nodes += count
        rows, cols = np.where(missing)
        row_min = min(row_min, row0 + int(rows.min()))
        row_max = max(row_max, row0 + int(rows.max()))
        col_min = min(col_min, int(cols.min()))
        col_max = max(col_max, int(cols.max()))
    result = {
        "active_nodes": active_nodes,
        "covered_active_nodes": active_nodes - missing_nodes,
        "missing_active_nodes": missing_nodes,
        "missing_percent": 100.0 * missing_nodes / active_nodes if active_nodes else 0.0,
        "complete": missing_nodes == 0,
    }
    if missing_nodes:
        result["missing_bounds_projected"] = [
            grid.xmin + col_min * grid.resolution,
            grid.ymax - row_max * grid.resolution,
            grid.xmin + col_max * grid.resolution,
            grid.ymax - row_min * grid.resolution,
        ]
    return result


def _downsample_any(mask: np.ndarray, max_pixels: int = 1200) -> np.ndarray:
    """Aggregate a large boolean grid with logical-any for a legible QC map."""
    rows, cols = mask.shape
    row_factor = max(1, int(np.ceil(rows / max_pixels)))
    col_factor = max(1, int(np.ceil(cols / max_pixels)))
    out_rows = int(np.ceil(rows / row_factor))
    out_cols = int(np.ceil(cols / col_factor))
    result = np.zeros((out_rows, out_cols), dtype=bool)
    padded_cols = out_cols * col_factor
    for out_row in range(out_rows):
        row0 = out_row * row_factor
        row1 = min(row0 + row_factor, rows)
        block = np.zeros((row1 - row0, padded_cols), dtype=bool)
        block[:, :cols] = mask[row0:row1]
        result[out_row] = block.reshape(
            row1 - row0, out_cols, col_factor
        ).any(axis=(0, 2))
    return result


def _downsample_active_mean(
    values: np.ndarray,
    active_mask: np.ndarray,
    max_pixels: int = 1200,
) -> np.ndarray:
    """Downsample active continuous values without averaging outside the domain."""
    rows, cols = values.shape
    row_factor = max(1, int(np.ceil(rows / max_pixels)))
    col_factor = max(1, int(np.ceil(cols / max_pixels)))
    out_rows = int(np.ceil(rows / row_factor))
    out_cols = int(np.ceil(cols / col_factor))
    result = np.full((out_rows, out_cols), np.nan, dtype=np.float32)
    padded_cols = out_cols * col_factor
    for out_row in range(out_rows):
        row0 = out_row * row_factor
        row1 = min(row0 + row_factor, rows)
        valid = active_mask[row0:row1] & np.isfinite(values[row0:row1])
        value_block = np.zeros((row1 - row0, padded_cols), dtype=np.float64)
        valid_block = np.zeros((row1 - row0, padded_cols), dtype=np.uint8)
        value_block[:, :cols] = np.where(valid, values[row0:row1], 0.0)
        valid_block[:, :cols] = valid
        sums = value_block.reshape(
            row1 - row0, out_cols, col_factor
        ).sum(axis=(0, 2))
        counts = valid_block.reshape(
            row1 - row0, out_cols, col_factor
        ).sum(axis=(0, 2))
        populated = counts > 0
        result[out_row, populated] = (sums[populated] / counts[populated]).astype(
            np.float32
        )
    return result


def _write_land_percentage_correction_maps(
    arrays: dict[str, np.ndarray],
    over_100_masks: dict[str, np.ndarray],
    node_mask: np.ndarray,
    grid: GridSpec,
    before_destination: str | Path,
    after_destination: str | Path,
) -> tuple[Path, Path]:
    """Map above-100 cells before correction and the capped processed products."""
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    active_small = _downsample_any(node_mask)
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    selected = [(name, arrays[name]) for name in ("impervious", "canopy")]

    before_fig, before_axes = plt.subplots(
        1, 2, figsize=(9.4, 4.6), constrained_layout=True
    )
    before_cmap = ListedColormap(["white", "#bdbdbd", "black"])
    for ax, (name, _) in zip(before_axes, selected):
        flagged = node_mask & over_100_masks[name]
        flagged_small = _downsample_any(flagged)
        shown = np.zeros(active_small.shape, dtype=np.uint8)
        shown[active_small] = 1
        shown[flagged_small] = 2
        count = int(np.count_nonzero(flagged))
        ax.imshow(
            shown, extent=extent, origin="upper", cmap=before_cmap,
            vmin=-0.5, vmax=2.5, interpolation="nearest",
        )
        ax.set(
            title=f"{name}: {count:,} active cells above 100%",
            xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal",
        )
    before_fig.legend(
        handles=[
            Patch(facecolor="#bdbdbd", label="Active study area"),
            Patch(facecolor="black", label="Original processed value above 100%"),
        ],
        loc="outside lower center", ncol=2,
    )
    before_fig.suptitle("Land-cover percentage cells flagged before correction")
    before_destination = Path(before_destination)
    before_destination.parent.mkdir(parents=True, exist_ok=True)
    before_fig.savefig(before_destination, dpi=180, bbox_inches="tight")
    plt.close(before_fig)

    after_fig, after_axes = plt.subplots(
        1, 2, figsize=(10.8, 4.8), constrained_layout=True
    )
    highlight_cmap = ListedColormap([(0, 0, 0, 0), (0.85, 0.1, 0.1, 0.55)])
    for ax, (name, values) in zip(after_axes, selected):
        reduced = _downsample_active_mean(values, node_mask)
        shown = ax.imshow(
            reduced, extent=extent, origin="upper", cmap="viridis",
            vmin=0.0, vmax=100.0, interpolation="nearest",
        )
        flagged_small = _downsample_any(node_mask & over_100_masks[name])
        ax.imshow(
            flagged_small.astype(np.uint8), extent=extent, origin="upper",
            cmap=highlight_cmap, vmin=0, vmax=1, interpolation="nearest",
        )
        count = int(np.count_nonzero(node_mask & over_100_masks[name]))
        ax.set(
            title=f"{name}: {count:,} cells capped to 100%",
            xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal",
        )
        after_fig.colorbar(shown, ax=ax, shrink=0.76, label="Corrected percent")
    after_fig.legend(
        handles=[Patch(facecolor=(0.85, 0.1, 0.1, 0.55), label="Capped to 100%")],
        loc="outside lower center", ncol=1,
    )
    after_fig.suptitle("QC-corrected land-cover percentage products")
    after_destination = Path(after_destination)
    after_destination.parent.mkdir(parents=True, exist_ok=True)
    after_fig.savefig(after_destination, dpi=180, bbox_inches="tight")
    plt.close(after_fig)
    return before_destination, after_destination


def _write_land_coverage_map(
    arrays: dict[str, np.ndarray],
    node_mask: np.ndarray,
    grid: GridSpec,
    destination: str | Path,
) -> Path:
    """Plot the whole study area in gray and pre-resolution null cells in black."""
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    active_small = _downsample_any(node_mask)
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.6), constrained_layout=True)
    cmap = ListedColormap(["white", "#bdbdbd", "black"])
    for ax, (name, values) in zip(axes, arrays.items()):
        missing_small = _downsample_any(node_mask & ~np.isfinite(values))
        shown = np.zeros(active_small.shape, dtype=np.uint8)
        shown[active_small] = 1
        shown[missing_small] = 2
        ax.imshow(
            shown, extent=extent, origin="upper", cmap=cmap,
            vmin=-0.5, vmax=2.5, interpolation="nearest",
        )
        missing_count = int(np.count_nonzero(node_mask & ~np.isfinite(values)))
        ax.set(
            title=f"{name}: {missing_count:,} null active nodes",
            xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal",
        )
    fig.legend(
        handles=[
            Patch(facecolor="#bdbdbd", label="Active study area with data"),
            Patch(facecolor="black", label="Null active cells (before user action)"),
        ],
        loc="outside lower center", ncol=2,
    )
    fig.suptitle("Land-cover coverage relative to the complete study area")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return destination


def _write_soil_coverage_map(
    arrays: dict[str, np.ndarray],
    node_mask: np.ndarray,
    grid: GridSpec,
    destination: str | Path,
) -> Path:
    """Plot source-derived soil coverage; active null cells are black."""
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    active_small = _downsample_any(node_mask)
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    fig, axes = plt.subplots(2, 2, figsize=(10.2, 8.2), constrained_layout=True)
    cmap = ListedColormap(["white", "#bdbdbd", "black"])
    for ax, (name, values) in zip(axes.flat, arrays.items()):
        missing_small = _downsample_any(node_mask & ~np.isfinite(values))
        shown = np.zeros(active_small.shape, dtype=np.uint8)
        shown[active_small] = 1
        shown[missing_small] = 2
        ax.imshow(
            shown, extent=extent, origin="upper", cmap=cmap,
            vmin=-0.5, vmax=2.5, interpolation="nearest",
        )
        missing_count = int(np.count_nonzero(node_mask & ~np.isfinite(values)))
        ax.set(
            title=f"{name}: {missing_count:,} null active nodes",
            xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal",
        )
    fig.legend(
        handles=[
            Patch(facecolor="#bdbdbd", label="Active study area with data"),
            Patch(facecolor="black", label="Null active cells (before user action)"),
        ],
        loc="outside lower center", ncol=2,
    )
    fig.suptitle("Soil-property coverage relative to the complete study area")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return destination


def _write_rainfall_coverage_map(
    cell_mask: np.ndarray,
    missing_mask: np.ndarray,
    grid: GridSpec,
    destination: str | Path,
    missing_count: int,
) -> Path:
    """Show the whole active study area in gray and uncovered rainfall cells in black."""
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    active_small = _downsample_any(cell_mask)
    missing_small = _downsample_any(missing_mask)
    shown = np.zeros(active_small.shape, dtype=np.uint8)
    shown[active_small] = 1
    shown[missing_small] = 2
    fig, ax = plt.subplots(figsize=(7.2, 6.2), constrained_layout=True)
    ax.imshow(
        shown,
        extent=(grid.xmin, grid.xmax, grid.ymin, grid.ymax),
        origin="upper",
        cmap=ListedColormap(["white", "#bdbdbd", "black"]),
        vmin=-0.5,
        vmax=2.5,
        interpolation="nearest",
    )
    ax.set(
        title=f"Rainfall coverage: {missing_count:,} null active cells",
        xlabel="Easting (m)",
        ylabel="Northing (m)",
        aspect="equal",
    )
    add_map_furniture(ax, grid)
    fig.legend(
        handles=[
            Patch(facecolor="#bdbdbd", label="Active study area with data"),
            Patch(facecolor="black", label="Null active cells"),
        ],
        loc="outside lower center",
        ncol=2,
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return destination


def prepare_nlcd(
    cover_path: str | Path,
    impervious_path: str | Path,
    canopy_path: str | Path,
    grid: GridSpec,
    node_mask: np.ndarray,
    output_directory: str | Path,
    year: int | str = "selected",
    coverage_report_path: str | Path | None = None,
    manual_fill_values: dict[str, float] | None = None,
):
    output_directory = Path(output_directory)
    cover = align_raster(cover_path, grid, "node", "nearest")
    impervious = align_raster(impervious_path, grid, "node", "bilinear")
    canopy_flag_counts = _count_raster_codes(
        canopy_path, NLCD_CANOPY_SEMANTIC_NODATA
    )
    canopy = align_raster(
        canopy_path,
        grid,
        "node",
        "bilinear",
        source_invalid_values=NLCD_CANOPY_SEMANTIC_NODATA,
    )
    arrays = {"cover": cover, "impervious": impervious, "canopy": canopy}
    cover_class_quality = _flag_invalid_land_cover_classes(cover, node_mask)
    over_100_masks = {
        name: np.isfinite(values) & (values > 100.0)
        for name, values in {"impervious": impervious, "canopy": canopy}.items()
    }
    percent_range_quality = {
        "impervious": _normalize_land_percentage_roundoff(impervious, node_mask),
        "canopy": _normalize_land_percentage_roundoff(canopy, node_mask),
    }
    before = {
        name: _active_coverage_stats(values, node_mask, grid)
        for name, values in arrays.items()
    }
    coverage_map_path = None
    if coverage_report_path is not None:
        coverage_map_path = Path(coverage_report_path).with_name(
            "land_cover_coverage_map.png"
        )
        _write_land_coverage_map(arrays, node_mask, grid, coverage_map_path)
    manual_fill_values = dict(manual_fill_values or {})
    unknown = set(manual_fill_values) - set(arrays)
    if unknown:
        raise ValueError(f"Unknown manual land-cover fill variables: {sorted(unknown)}")
    manual_resolutions = {}
    for name, value in manual_fill_values.items():
        value = _validate_manual_land_value(name, value)
        missing = node_mask & ~np.isfinite(arrays[name])
        count = int(np.count_nonzero(missing))
        if count:
            arrays[name][missing] = value
        manual_resolutions[name] = {
            "mode": "user_constant",
            "value": value,
            "filled_active_nodes": count,
        }
    percentage_correction_maps = {"before": None, "after": None}
    if coverage_report_path is not None:
        before_correction_path = Path(coverage_report_path).with_name(
            "land_cover_percentage_over_100_before_correction_map.png"
        )
        after_correction_path = Path(coverage_report_path).with_name(
            "land_cover_percentage_after_correction_map.png"
        )
        _write_land_percentage_correction_maps(
            arrays,
            over_100_masks,
            node_mask,
            grid,
            before_correction_path,
            after_correction_path,
        )
        percentage_correction_maps = {
            "before": str(before_correction_path),
            "after": str(after_correction_path),
        }
    coverage = {
        "schema": "riftprep-land-cover-coverage-v2",
        "grid_crs": grid.crs,
        "automatic_fill_performed": False,
        "automatic_numeric_normalization_performed": any(
            item["roundoff_normalized_total_cells"] > 0
            or item["over_100_corrected_total_cells"] > 0
            for item in percent_range_quality.values()
        ),
        "automatic_percentage_capping_performed": any(
            item["over_100_corrected_total_cells"] > 0
            for item in percent_range_quality.values()
        ),
        "percentage_correction_policy": {
            "rule": "finite processed impervious/canopy values above 100 are capped to 100",
            "raw_products_modified": False,
            "before_after_maps": percentage_correction_maps,
        },
        "source_semantic_nodata": {
            "canopy": {
                "codes": canopy_flag_counts,
                "meaning": {
                    "254": "NLCD non-processing area",
                    "255": "NLCD background",
                },
                "raw_product_modified": False,
                "masked_before_reprojection": True,
            }
        },
        "cover_class_quality": cover_class_quality,
        "percent_range_quality": percent_range_quality,
        "coverage_map": str(coverage_map_path) if coverage_map_path else None,
        "coverage_before_manual_resolution": before,
        "manual_resolutions": manual_resolutions,
        "variables": {
            "cover": _active_coverage_stats(cover, node_mask, grid),
            "impervious": _active_coverage_stats(impervious, node_mask, grid),
            "canopy": _active_coverage_stats(canopy, node_mask, grid),
        },
    }
    coverage["complete"] = all(
        item["complete"] for item in coverage["variables"].values()
    )
    coverage["status"] = "PASSED" if coverage["complete"] else "NEEDS USER INPUT"
    if coverage_report_path is not None:
        report_path = Path(coverage_report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    if not coverage["complete"]:
        missing = ", ".join(
            f"{name}={item['missing_active_nodes']}"
            for name, item in coverage["variables"].items()
            if not item["complete"]
        )
        raise ValueError(
            "Selected land-cover products do not cover every active RIFT node "
            f"({missing}). No automatic fill was performed. Inspect "
            "land_cover_coverage.json, then either provide explicit "
            "land_cover.options.manual_fill_values or provide user-modified rasters "
            "with land_cover.provider='local'."
        )
    active_cover = cover[node_mask]
    rounded_cover = np.rint(active_cover)
    if np.any(np.abs(active_cover - rounded_cover) > 1e-6):
        if coverage_report_path is not None:
            coverage.update(status="FAILED", failure="active cover is not integer-valued")
            Path(coverage_report_path).write_text(json.dumps(coverage, indent=2), encoding="utf-8")
        raise ValueError("Land-cover class must be integer-valued at every active node.")
    unsupported = sorted(set(rounded_cover.astype(int)) - set(NLCD_MANNING))
    if unsupported:
        if coverage_report_path is not None:
            coverage.update(status="FAILED", failure=f"unsupported NLCD classes: {unsupported}")
            Path(coverage_report_path).write_text(json.dumps(coverage, indent=2), encoding="utf-8")
        raise ValueError(
            "Land-cover class resolution produced active classes without a Manning "
            f"mapping: {unsupported}. This indicates an internal validation error."
        )
    for name, values in {"impervious": impervious, "canopy": canopy}.items():
        active = values[node_mask]
        if np.any((active < 0) | (active > 100)):
            if coverage_report_path is not None:
                coverage.update(status="FAILED", failure=f"{name} outside 0-100 percent")
                Path(coverage_report_path).write_text(json.dumps(coverage, indent=2), encoding="utf-8")
            raise ValueError(f"{name} must be between 0 and 100 percent at active nodes.")
    cover = np.where(np.isfinite(cover), np.rint(cover), 0).astype(np.int16)
    impervious = np.clip(np.nan_to_num(impervious, nan=0.0), 0, 100)
    canopy = np.clip(np.nan_to_num(canopy, nan=0.0), 0, 100)
    manning = np.full(grid.node_shape, 0.05, dtype=float)
    for class_id, value in NLCD_MANNING.items():
        manning[cover == class_id] = value
    manning[~node_mask] = 0.05
    paths = {
        "cover": write_esri_ascii(output_directory / f"nlcd_{year}_class.asc", cover, grid),
        "impervious": write_esri_ascii(output_directory / f"nlcd_{year}_impervious_pct.asc", impervious, grid),
        "canopy": write_esri_ascii(output_directory / f"nlcd_{year}_canopy_pct.asc", canopy, grid),
        "manning": write_esri_ascii(output_directory / "manning_n_from_land_cover.asc", manning, grid),
    }
    return cover, impervious, canopy, manning, paths


def plot_nlcd(cover, impervious, canopy, manning, grid: GridSpec, node_mask=None):
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 6.0), constrained_layout=True)
    items = [
        (cover, "a  Selected land-cover class", "Class", "tab20"),
        (impervious, "b  Impervious surface", "%", "magma"),
        (canopy, "c  Tree canopy", "%", "Greens"),
        (manning, "d  Derived Manning roughness", "Manning n", "viridis"),
    ]
    for ax, (data, title, label, cmap) in zip(axes.flat, items):
        shown = np.where(node_mask, data, np.nan) if node_mask is not None else data
        image = ax.imshow(shown, extent=extent, origin="upper", cmap=cmap)
        ax.set(title=title, xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal")
        add_map_furniture(ax, grid)
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.025, label=label)
    return fig


def _node_transform(grid: GridSpec):
    return from_origin(
        grid.xmin - grid.resolution / 2, grid.ymax + grid.resolution / 2,
        grid.resolution, grid.resolution,
    )


def _rasterize_property(data: gpd.GeoDataFrame, field: str, grid: GridSpec) -> np.ndarray:
    if field not in data:
        raise KeyError(f"Selected soil product is missing {field!r}.")
    valid = data[data.geometry.notna() & pd.to_numeric(data[field], errors="coerce").notna()].copy()
    if valid.empty:
        # Keep the native product unchanged and let ``prepare_ssurgo`` apply an
        # explicit user constant (or emit its normal NEEDS USER INPUT report).
        # Raising here prevented the documented manual-fill path from handling
        # a provider field that is present but entirely null.
        return np.full(grid.node_shape, np.nan, dtype="float64")
    return rasterize(
        [(geometry, float(value)) for geometry, value in zip(valid.geometry, valid[field])],
        out_shape=grid.node_shape, transform=_node_transform(grid), fill=np.nan,
        all_touched=True, dtype="float64",
    )


def _validate_manual_soil_value(variable: str, value: float) -> float:
    value = float(value)
    if not np.isfinite(value):
        raise ValueError(f"Manual soil {variable} constant must be finite.")
    if variable in {"permeability", "alpha"} and value <= 0:
        raise ValueError(f"Manual soil {variable} constant must be positive.")
    if variable == "porosity" and not 0 < value <= 1:
        raise ValueError("Manual soil porosity must be greater than 0 and at most 1.")
    if variable == "residual" and not 0 <= value < 1:
        raise ValueError("Manual residual saturation must be at least 0 and less than 1.")
    return value


def prepare_ssurgo(
    soil_path: str | Path | Mapping[str, str | Path],
    grid: GridSpec,
    node_mask: np.ndarray,
    output_directory: str | Path,
    initial_saturation_value: float = 0.50,
    coverage_report_path: str | Path | None = None,
    manual_fill_values: dict[str, float] | None = None,
    residual_porosity_tolerance: float = 0.0,
):
    if not 0 <= initial_saturation_value <= 1:
        raise ValueError("initial_saturation must be between 0 and 1.")
    residual_porosity_tolerance = float(residual_porosity_tolerance)
    if not 0 <= residual_porosity_tolerance < 1:
        raise ValueError(
            "residual_porosity_tolerance must be at least 0 and less than 1."
        )
    fields = {
        "permeability": "permeability [m^2]", "porosity": "porosity [-]",
        "residual": "residual saturation [-]", "alpha": "van Genuchten alpha [Pa^-1]",
    }
    if isinstance(soil_path, Mapping):
        missing_rasters = set(fields) - set(soil_path)
        if missing_rasters:
            raise KeyError(
                f"Selected gridded soil product is missing {sorted(missing_rasters)}."
            )
        arrays = {
            key: align_raster(soil_path[key], grid, "node", "bilinear")
            for key in fields
        }
    else:
        soil = gpd.read_file(soil_path).to_crs(grid.crs)
        arrays = {
            key: _rasterize_property(soil, field, grid) for key, field in fields.items()
        }
    before = {
        name: _active_coverage_stats(values, node_mask, grid)
        for name, values in arrays.items()
    }
    coverage_map_path = None
    if coverage_report_path is not None:
        coverage_map_path = Path(coverage_report_path).with_name("soil_coverage_map.png")
        _write_soil_coverage_map(arrays, node_mask, grid, coverage_map_path)

    manual_fill_values = dict(manual_fill_values or {})
    unknown = set(manual_fill_values) - set(arrays)
    if unknown:
        raise ValueError(f"Unknown manual soil fill variables: {sorted(unknown)}")
    manual_resolutions = {}
    for name, value in manual_fill_values.items():
        value = _validate_manual_soil_value(name, value)
        missing = node_mask & ~np.isfinite(arrays[name])
        count = int(np.count_nonzero(missing))
        if count:
            arrays[name][missing] = value
        manual_resolutions[name] = {
            "mode": "user_constant", "value": value, "filled_active_nodes": count,
        }

    after = {
        name: _active_coverage_stats(values, node_mask, grid)
        for name, values in arrays.items()
    }
    complete = all(item["complete"] for item in after.values())
    coverage = {
        "schema": "riftprep-soil-coverage-v1",
        "status": "PASSED" if complete else "NEEDS USER INPUT",
        "grid_crs": grid.crs,
        "automatic_fill_performed": False,
        "coverage_map": str(coverage_map_path) if coverage_map_path else None,
        "coverage_before_manual_resolution": before,
        "manual_resolutions": manual_resolutions,
        "variables": after,
        "complete": complete,
    }
    if coverage_report_path is not None:
        report_path = Path(coverage_report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    if not complete:
        missing = ", ".join(
            f"{name}={item['missing_active_nodes']}"
            for name, item in after.items() if not item["complete"]
        )
        raise ValueError(
            "Selected soil product does not cover every active RIFT node "
            f"({missing}). No automatic fill was performed. Inspect soil_coverage.json "
            "and soil_coverage_map.png, then provide explicit "
            "soil.options.manual_fill_values or a corrected local soil product."
        )

    invalid_ranges = {
        "permeability": np.any(arrays["permeability"][node_mask] <= 0),
        "porosity": np.any(
            (arrays["porosity"][node_mask] <= 0)
            | (arrays["porosity"][node_mask] > 1)
        ),
        "residual": np.any(
            (arrays["residual"][node_mask] < 0)
            | (arrays["residual"][node_mask] >= 1)
        ),
        "alpha": np.any(arrays["alpha"][node_mask] <= 0),
    }
    failed_ranges = [name for name, failed in invalid_ranges.items() if failed]
    if failed_ranges:
        if coverage_report_path is not None:
            coverage.update(
                status="FAILED",
                failure=f"physical range checks failed: {failed_ranges}",
            )
            Path(coverage_report_path).write_text(
                json.dumps(coverage, indent=2), encoding="utf-8"
            )
        raise ValueError(
            "Soil properties violate physical range checks at active nodes: "
            f"{failed_ranges}. Provide a corrected local product or reviewed constants."
        )

    # Values outside the active study area cannot affect the simulation, but all
    # exported grids remain numeric. This is not an active-cell correction.
    for name, values in arrays.items():
        outside = ~node_mask & ~np.isfinite(values)
        if np.any(outside):
            values[outside] = float(np.nanmedian(values[node_mask]))
    permeability = arrays["permeability"]
    porosity = arrays["porosity"]
    residual = arrays["residual"]
    alpha = arrays["alpha"]
    relationship_gap = residual - porosity
    relationship_violation = node_mask & (relationship_gap >= 0.0)
    at_tolerance = np.isclose(
        relationship_gap,
        residual_porosity_tolerance,
        rtol=0.0,
        atol=1.0e-12,
    )
    material_violation = relationship_violation & (
        (relationship_gap > residual_porosity_tolerance) | at_tolerance
    )
    accepted_by_tolerance = relationship_violation & ~material_violation
    violating_values = relationship_gap[relationship_violation]
    accepted_values = relationship_gap[accepted_by_tolerance]
    material_values = relationship_gap[material_violation]
    coverage["residual_porosity_check"] = {
        "criterion": "residual saturation must be less than porosity",
        "accepted_gap_threshold": residual_porosity_tolerance,
        "accepted_rule": "residual_minus_porosity < accepted_gap_threshold",
        "values_modified": False,
        "violating_active_nodes": int(np.count_nonzero(relationship_violation)),
        "accepted_by_tolerance_nodes": int(np.count_nonzero(accepted_by_tolerance)),
        "materially_invalid_nodes": int(np.count_nonzero(material_violation)),
        "minimum_gap": float(np.min(violating_values)) if violating_values.size else None,
        "maximum_gap": float(np.max(violating_values)) if violating_values.size else None,
        "maximum_accepted_gap": float(np.max(accepted_values)) if accepted_values.size else None,
        "minimum_material_gap": float(np.min(material_values)) if material_values.size else None,
    }
    if accepted_values.size:
        coverage.setdefault("warnings", []).append(
            f"Accepted {accepted_values.size} active residual/porosity inconsistencies "
            f"smaller than the configured tolerance {residual_porosity_tolerance}."
        )
    material_correction_count = 0
    if material_values.size and {"porosity", "residual"}.issubset(manual_fill_values):
        # A user-approved pair of constants may resolve only the materially
        # invalid processed cells.  Valid provider values and the raw SSURGO
        # product remain unchanged; the correction is explicitly counted.
        manual_porosity = _validate_manual_soil_value(
            "porosity", manual_fill_values["porosity"]
        )
        manual_residual = _validate_manual_soil_value(
            "residual", manual_fill_values["residual"]
        )
        material_correction_count = int(np.count_nonzero(material_violation))
        arrays["porosity"][material_violation] = manual_porosity
        arrays["residual"][material_violation] = manual_residual
        manual_resolutions["porosity"]["material_violation_nodes"] = material_correction_count
        manual_resolutions["residual"]["material_violation_nodes"] = material_correction_count
        relationship_gap = arrays["residual"] - arrays["porosity"]
        relationship_violation = node_mask & (relationship_gap >= 0.0)
        at_tolerance = np.isclose(
            relationship_gap,
            residual_porosity_tolerance,
            rtol=0.0,
            atol=1.0e-12,
        )
        material_violation = relationship_violation & (
            (relationship_gap > residual_porosity_tolerance) | at_tolerance
        )
        coverage["residual_porosity_check"].update(
            values_modified=True,
            material_violation_nodes_corrected=material_correction_count,
            remaining_materially_invalid_nodes=int(np.count_nonzero(material_violation)),
            correction_mode="user_constants_processed_copy_only",
        )
    if material_values.size and material_violation.any():
        coverage.update(
            status="FAILED",
            failure=(
                "residual saturation is not less than porosity and the gap is at "
                f"least {residual_porosity_tolerance}; provide explicit manual "
                "porosity and residual constants or a corrected local product"
            ),
        )
    if coverage_report_path is not None:
        Path(coverage_report_path).write_text(
            json.dumps(coverage, indent=2), encoding="utf-8"
        )
    if material_violation.any():
        raise ValueError(
            "Soil residual saturation must be less than porosity unless the gap is "
            "smaller than soil.options.residual_porosity_tolerance. "
            f"Found {int(np.count_nonzero(material_violation))} material active-node violations."
        )
    ksat = np.clip(permeability * WATER_INTRINSIC_TO_KSAT, 1e-9, 1e-3)
    suction = np.clip(1.0 / np.maximum(alpha * 1000.0 * 9.80665, 1e-12), 0.01, 2.0)
    initial_saturation = np.full(grid.node_shape, initial_saturation_value, dtype=float)
    output_directory = Path(output_directory)
    paths = {
        "ksat": write_esri_ascii(output_directory / "soil_ksat_m_s.asc", ksat, grid),
        "porosity": write_esri_ascii(output_directory / "soil_porosity.asc", porosity, grid),
        "suction": write_esri_ascii(output_directory / "soil_suction_head_m.asc", suction, grid),
        "initial_saturation": write_esri_ascii(output_directory / "initial_saturation.asc", initial_saturation, grid),
    }
    scalars = {
        "soil.suction_head_m": float(np.nanmedian(suction[node_mask])),
        "soil.moisture_deficit": float(np.nanmedian((porosity * (1 - initial_saturation))[node_mask])),
        "soil.residual_saturation": float(np.nanmedian(residual[node_mask])),
    }
    return ksat, porosity, suction, initial_saturation, paths, scalars


def plot_soils(ksat, porosity, suction, initial_saturation, grid: GridSpec, node_mask=None):
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 6.0), constrained_layout=True)
    items = [
        (np.log10(ksat), "a  Saturated conductivity", "log10 Ksat (m s⁻¹)", "viridis"),
        (porosity, "b  Porosity", "Porosity (-)", "YlGnBu"),
        (suction, "c  Wetting-front suction", "Suction head (m)", "cividis"),
        (initial_saturation, "d  Initial relative saturation", "Saturation (-)", "Blues"),
    ]
    for ax, (data, title, label, cmap) in zip(axes.flat, items):
        shown = np.where(node_mask, data, np.nan) if node_mask is not None else data
        image = ax.imshow(shown, extent=extent, origin="upper", cmap=cmap)
        ax.set(title=title, xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal")
        add_map_furniture(ax, grid)
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.025, label=label)
    return fig


def _source_grid(event: xr.Dataset):
    if "x" in event.coords and "y" in event.coords:
        return str(event.attrs.get("source_crs", "EPSG:5070")), "x", "y"
    if "longitude" in event.coords and "latitude" in event.coords:
        return "EPSG:4326", "longitude", "latitude"
    if "lon" in event.coords and "lat" in event.coords:
        return "EPSG:4326", "lon", "lat"
    raise ValueError("Rainfall must have x/y, longitude/latitude, or lon/lat coordinates.")


def _rainfall_progress(message: str) -> None:
    """Emit a checkpoint that remains visible in notebooks and batch logs."""
    print(f"[riftprep rainfall] {message}", flush=True)


def _rainfall_staging_root() -> Path:
    """Choose local scratch when available, with an explicit user override."""
    configured = os.environ.get("RIFTPREP_HDF5_STAGING_DIR")
    slurm_tmp = os.environ.get("SLURM_TMPDIR")
    root = Path(configured or slurm_tmp or tempfile.gettempdir()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_staged_rainfall_hdf5(
    path: Path,
    times_s: np.ndarray,
    expected_hours: int,
    cell_shape: tuple[int, int],
) -> None:
    """Reopen and fully read a staged forcing before publishing it."""
    group_name = "precipitation rain [m s^-1]"
    with h5py.File(path, "r") as source:
        if "time [s]" not in source or group_name not in source:
            raise ValueError("Staged rainfall HDF5 is missing required datasets.")
        np.testing.assert_allclose(np.asarray(source["time [s]"], dtype=float), times_s)
        group = source[group_name]
        expected_keys = [str(index) for index in range(expected_hours + 1)]
        if sorted(group.keys(), key=int) != expected_keys:
            raise ValueError("Staged rainfall HDF5 has incomplete time records.")
        for key in expected_keys:
            values = np.asarray(group[key], dtype=float)
            if values.shape != cell_shape:
                raise ValueError(
                    f"Staged rainfall record {key} has shape {values.shape}; "
                    f"expected {cell_shape}."
                )
            if not np.isfinite(values).all() or np.any(values < 0):
                raise ValueError(
                    f"Staged rainfall record {key} contains invalid rates."
                )


def _publish_staged_file(staged_path: Path, final_path: Path) -> None:
    """Copy to a neighboring temporary file, then atomically expose the result."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{final_path.name}.", suffix=".tmp",
        dir=final_path.parent, delete=False,
    ) as stream:
        temporary_path = Path(stream.name)
    copied = 0
    report_interval = 512 * 1024 * 1024
    next_report = report_interval
    try:
        with staged_path.open("rb") as source, temporary_path.open("wb") as target:
            while True:
                chunk = source.read(64 * 1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                copied += len(chunk)
                if copied >= next_report:
                    _rainfall_progress(
                        f"copied {copied / (1024 ** 3):.1f} GiB to the case filesystem"
                    )
                    next_report += report_interval
            target.flush()
            os.fsync(target.fileno())
        expected_size = staged_path.stat().st_size
        if copied != expected_size or temporary_path.stat().st_size != expected_size:
            raise OSError(
                f"Rainfall HDF5 copy is incomplete: copied {copied} of "
                f"{expected_size} bytes."
            )
        os.replace(temporary_path, final_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare_aorc_event(
    source_path: str | Path,
    event_subset_path: str | Path,
    hdf5_path: str | Path,
    grid: GridSpec,
    cell_mask: np.ndarray,
    start_utc: str,
    end_utc: str,
    coverage_report_path: str | Path | None = None,
    uncovered_policy: str = "zero",
    precipitation_variable: str = "APCP_surface",
):
    if uncovered_policy not in {"zero", "error"}:
        raise ValueError("rainfall uncovered_policy must be 'zero' or 'error'.")
    start = pd.Timestamp(start_utc).tz_convert(None)
    end = pd.Timestamp(end_utc).tz_convert(None)
    expected_hours = int((end - start) / pd.Timedelta(hours=1))
    source = event = None
    _rainfall_progress(f"loading {expected_hours} selected hourly record(s) into memory")
    try:
        try:
            source = xr.open_dataset(source_path, chunks={"time": 1})
        except (ImportError, ValueError):
            source = xr.open_dataset(source_path)
        if precipitation_variable not in source:
            raise KeyError(
                f"Rainfall source is missing selected variable {precipitation_variable!r}."
            )
        event = source[[precipitation_variable]].sel(
            time=slice(start + pd.Timedelta(hours=1), end)
        )
        if event.sizes.get("time", 0) != expected_hours:
            raise ValueError(
                f"Rainfall has {event.sizes.get('time', 0)} records; "
                f"expected {expected_hours}."
            )
        event.attrs.update(
            event_start_utc=str(start), event_end_utc=str(end),
            interval_semantics="hourly accumulation ending at timestamp",
        )
        event.load()
        event_subset_path = Path(event_subset_path)
        event_subset_path.parent.mkdir(parents=True, exist_ok=True)
        event.to_netcdf(
            event_subset_path,
            encoding={
                precipitation_variable: {
                    "zlib": True, "complevel": 4, "shuffle": True,
                }
            },
        )
        source_crs, x_name, y_name = _source_grid(event)
        src_x = np.asarray(event[x_name], dtype=float).copy()
        src_y = np.asarray(event[y_name], dtype=float).copy()
        event_values = np.asarray(event[precipitation_variable], dtype=float).copy()
        event_times = np.asarray(event.time.values).copy()
    finally:
        if event is not None:
            event.close()
        if source is not None:
            source.close()
    _rainfall_progress(
        "saved the unchanged event subset and closed all source NetCDF handles"
    )
    dx, dy = abs(float(np.median(np.diff(src_x)))), abs(float(np.median(np.diff(src_y))))
    src_transform = from_origin(src_x.min() - dx / 2, src_y.max() + dy / 2, dx, dy)
    dst_transform = from_origin(grid.xmin, grid.ymax, grid.resolution, grid.resolution)
    times_s = np.arange(expected_hours + 1, dtype=float) * 3600.0
    mean_rate, max_rate = [], []
    accumulation_mm = np.zeros(grid.cell_shape, dtype=float)
    replacement_union = np.zeros(grid.cell_shape, dtype=bool)
    incomplete_records: list[dict[str, object]] = []
    hdf5_path = Path(hdf5_path)
    hdf5_path.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(tempfile.mkdtemp(
        prefix="riftprep-rainfall-", dir=_rainfall_staging_root()
    ))
    staged_hdf5 = staging_directory / hdf5_path.name
    _rainfall_progress(f"writing staged RIFT HDF5 at {staged_hdf5}")
    try:
        with h5py.File(staged_hdf5, "w") as h5:
            h5.create_dataset("time [s]", data=times_s)
            group = h5.create_group("precipitation rain [m s^-1]")
            for index in range(expected_hours):
                _rainfall_progress(
                    f"mapping rainfall record {index + 1}/{expected_hours}"
                )
                source_mm = event_values[index]
                if np.any(source_mm[np.isfinite(source_mm)] < 0):
                    if coverage_report_path is not None:
                        report_path = Path(coverage_report_path)
                        report_path.parent.mkdir(parents=True, exist_ok=True)
                        report_path.write_text(json.dumps({
                            "schema": "riftprep-rainfall-coverage-v2",
                            "status": "FAILED",
                            "failure": f"negative precipitation in record {index}",
                            "raw_native_modified": False,
                            "saved_event_subset_modified": False,
                        }, indent=2), encoding="utf-8")
                    raise ValueError(
                        f"Rainfall record {index} contains negative precipitation. "
                        "No automatic clipping was performed."
                    )
                if src_y[0] < src_y[-1]:
                    source_mm = source_mm[::-1, :]
                if src_x[0] > src_x[-1]:
                    source_mm = source_mm[:, ::-1]
                mapped_mm = np.full(grid.cell_shape, np.nan, dtype=np.float64)
                reproject(
                    source=source_mm, destination=mapped_mm,
                    src_transform=src_transform, src_crs=source_crs, src_nodata=np.nan,
                    dst_transform=dst_transform, dst_crs=grid.crs, dst_nodata=np.nan,
                    resampling=Resampling.average,
                )
                missing_mask = cell_mask & ~np.isfinite(mapped_mm)
                missing = int(np.count_nonzero(missing_mask))
                if missing:
                    replacement_union |= missing_mask
                    incomplete_records.append({
                        "record_index": index,
                        "time_utc": str(pd.Timestamp(event_times[index], tz="UTC")),
                        "missing_active_cells": missing,
                    })
                    if uncovered_policy == "error":
                        if coverage_report_path is not None:
                            report_path = Path(coverage_report_path)
                            map_path = report_path.with_name("rainfall_coverage_map.png")
                            _write_rainfall_coverage_map(
                                cell_mask, replacement_union, grid, map_path,
                                int(np.count_nonzero(replacement_union)),
                            )
                            report_path.parent.mkdir(parents=True, exist_ok=True)
                            report_path.write_text(json.dumps({
                                "schema": "riftprep-rainfall-coverage-v2",
                                "status": "NEEDS USER INPUT",
                                "uncovered_policy": uncovered_policy,
                                "raw_native_modified": False,
                                "saved_event_subset_modified": False,
                                "coverage_map": str(map_path),
                                "incomplete_records": incomplete_records,
                            }, indent=2), encoding="utf-8")
                        diagnostic = (
                            f" Inspect {coverage_report_path}."
                            if coverage_report_path else ""
                        )
                        raise ValueError(
                            f"Rainfall does not cover {missing} active RIFT cells."
                            f"{diagnostic} No automatic fill was performed; provide "
                            "complete user-modified rainfall forcing or select "
                            "uncovered_policy='zero'."
                        )
                    # Keep the saved native/event NetCDF unchanged. Only the numeric
                    # RIFT field is completed: outside source coverage means no applied
                    # precipitation, so those cells receive exactly zero.
                    mapped_mm[missing_mask] = 0.0
                mapped_mm = np.where(cell_mask, mapped_mm, 0.0)
                rate = mapped_mm * 1e-3 / 3600.0
                group.create_dataset(
                    str(index), data=rate, compression="gzip", shuffle=True
                )
                active = mapped_mm[cell_mask]
                mean_rate.append(float(active.mean()))
                max_rate.append(float(active.max()))
                accumulation_mm += mapped_mm
            group.create_dataset(
                str(expected_hours), shape=grid.cell_shape, dtype="float64",
                compression="gzip", shuffle=True, fillvalue=0.0,
            )
        _rainfall_progress("reopening and validating the complete staged HDF5")
        _validate_staged_rainfall_hdf5(
            staged_hdf5, times_s, expected_hours, grid.cell_shape
        )
        _rainfall_progress("publishing the validated HDF5 to the case directory")
        _publish_staged_file(staged_hdf5, hdf5_path)
        _rainfall_progress(f"published {hdf5_path}")
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)
    if coverage_report_path is not None:
        report_path = Path(coverage_report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        map_path = report_path.with_name("rainfall_coverage_map.png")
        replacement_mask_path = report_path.with_name("rainfall_zero_replacement_mask.tif")
        union_count = int(np.count_nonzero(replacement_union))
        _write_rainfall_coverage_map(
            cell_mask, replacement_union, grid, map_path, union_count
        )
        mask_values = np.full(grid.cell_shape, 255, dtype=np.uint8)
        mask_values[cell_mask] = 0
        mask_values[replacement_union] = 1
        with rasterio.open(
            replacement_mask_path,
            "w",
            driver="GTiff",
            height=grid.ny,
            width=grid.nx,
            count=1,
            dtype="uint8",
            crs=grid.crs,
            transform=dst_transform,
            nodata=255,
            compress="deflate",
        ) as target:
            target.write(mask_values, 1)
            target.set_band_description(
                1, "1=source-null replaced by zero in RIFT forcing; 0=source value retained"
            )
        active_count = int(np.count_nonzero(cell_mask))
        first = incomplete_records[0] if incomplete_records else None
        report_path.write_text(json.dumps({
            "schema": "riftprep-rainfall-coverage-v2",
            "status": "PASSED",
            "grid_crs": grid.crs,
            "uncovered_policy": uncovered_policy,
            "source_coverage_complete": union_count == 0,
            "rift_forcing_complete": True,
            "automatic_zero_replacement_performed": union_count > 0,
            "automatic_fill_performed": union_count > 0,
            "raw_native_modified": False,
            "raw_event_modified": False,
            "saved_event_subset_modified": False,
            "active_cells": active_count,
            "covered_active_cells": active_count - union_count,
            "missing_active_cells": union_count,
            "missing_percent": 100.0 * union_count / active_count if active_count else 0.0,
            "first_incomplete_record_index": first["record_index"] if first else None,
            "first_incomplete_time_utc": first["time_utc"] if first else None,
            "incomplete_records": incomplete_records,
            "coverage_map": str(map_path),
            "zero_replacement_mask": str(replacement_mask_path),
            "zero_replacement_mask_values": {
                "0": "original source value retained",
                "1": "source-null active cell replaced by zero",
                "255": "outside active study area",
            },
            "rift_hdf5_uncovered_value_mm_h": 0.0,
            "complete": True,
        }, indent=2), encoding="utf-8")
    summary = pd.DataFrame({
        "time_utc": pd.date_range(start, periods=expected_hours, freq="1h", tz="UTC"),
        "mean_mm_h": mean_rate, "maximum_mm_h": max_rate,
    })
    summary.to_csv(hdf5_path.with_name("rainfall_event_summary.csv"), index=False)
    return summary, accumulation_mm, times_s


def plot_aorc(summary: pd.DataFrame, accumulation_mm: np.ndarray, grid: GridSpec):
    extent = (grid.xmin, grid.xmax, grid.ymin, grid.ymax)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), constrained_layout=True)
    axes[0].plot(summary.time_utc, summary.mean_mm_h, label="Active-domain mean", color="#0072B2")
    axes[0].plot(summary.time_utc, summary.maximum_mm_h, label="Active-domain maximum", color="#D55E00")
    axes[0].set(title="a  Hourly precipitation", ylabel="Rate (mm h⁻¹)")
    axes[0].legend()
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
    axes[0].xaxis.set_major_locator(locator)
    axes[0].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    image = axes[1].imshow(accumulation_mm, extent=extent, origin="upper", cmap="Blues")
    axes[1].set(title="b  Event accumulation", xlabel="Easting (m)", ylabel="Northing (m)", aspect="equal")
    add_map_furniture(axes[1], grid)
    fig.colorbar(image, ax=axes[1], label="Accumulation (mm)")
    return fig


def prepare_synthetic_drainage(
    impervious,
    node_mask,
    grid: GridSpec,
    output_directory: str | Path,
    impervious_threshold_pct: float = 80.0,
):
    if not 0 <= impervious_threshold_pct <= 100:
        raise ValueError("impervious_threshold_pct must be between 0 and 100.")
    infrastructure = np.where(
        (impervious >= impervious_threshold_pct) & node_mask, 1, 0
    ).astype(np.int32)
    rows = [
        [0, "none", "linear", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [1, "synthetic high-impervious inlet", "orifice", 2.5e-5, 0.04, 0, 0,
         0.15, 0.67, 0.10, 0.65, 0.01, 0, 0, 0.005, 0],
    ]
    from .drainage import RIFT_DRAINAGE_COLUMNS
    table = pd.DataFrame(rows, columns=RIFT_DRAINAGE_COLUMNS)
    output_directory = Path(output_directory)
    map_path = write_esri_ascii(output_directory / "synthetic_urban_drain_type.asc", infrastructure, grid)
    table_path = output_directory / "synthetic_urban_hydraulic_controls.csv"
    table.to_csv(table_path, index=False)
    return infrastructure, table, map_path, table_path


def prepare_surge_grids(
    stage: pd.DataFrame,
    dem_unwalled: np.ndarray,
    node_mask: np.ndarray,
    outlet_xy: tuple[float, float],
    grid: GridSpec,
    output_directory: str | Path,
    interval_hours: int = 1,
    coastal_radius_m: float = 3000.0,
):
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    sampled = stage.iloc[::interval_hours].copy()
    if sampled.time_s.iloc[-1] != stage.time_s.iloc[-1]:
        sampled = pd.concat([sampled, stage.iloc[[-1]]], ignore_index=True)
    xs = grid.xmin + np.arange(grid.nx + 1) * grid.resolution
    ys = grid.ymax - np.arange(grid.ny + 1) * grid.resolution
    xx, yy = np.meshgrid(xs, ys)
    coastal = node_mask & (((xx - outlet_xy[0]) ** 2 + (yy - outlet_xy[1]) ** 2) <= coastal_radius_m**2)
    if not coastal.any():
        raise ValueError("Coastal support/radius selects no active RIFT nodes.")
    maximum = np.zeros(grid.node_shape, dtype=float)
    for index, row in sampled.reset_index(drop=True).iterrows():
        depth = np.where(coastal, np.maximum(float(row.stage_m_navd88) - dem_unwalled, 0.0), 0.0)
        write_esri_ascii(output_directory / f"surge-{index:03d}.txt", depth, grid)
        maximum = np.maximum(maximum, depth)
    return sampled, maximum, coastal
