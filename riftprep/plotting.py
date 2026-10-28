from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.ticker import MaxNLocator

from .config import GridSpec


JOURNAL_STYLE = {
    "figure.dpi": 140,
    "savefig.dpi": 600,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
    "savefig.facecolor": "white",
    "savefig.edgecolor": "white",
    "savefig.transparent": False,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "DejaVu Sans"],
    "font.size": 8,
    "axes.titlesize": 9,
    "axes.labelsize": 8,
    "axes.linewidth": 0.7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "legend.fontsize": 7,
    "legend.frameon": False,
    "legend.handlelength": 2.0,
    "lines.linewidth": 1.25,
    "image.interpolation": "nearest",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    "axes.formatter.useoffset": False,
    "axes.unicode_minus": True,
}


def set_journal_style() -> None:
    mpl.rcParams.update(JOURNAL_STYLE)


def save_figure(fig, stem: str | Path, formats: Sequence[str] = ("png", "pdf", "svg"), dpi: int = 600) -> list[Path]:
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for suffix in formats:
        target = stem.with_suffix(f".{suffix}")
        fig.savefig(target, dpi=dpi if suffix == "png" else None, bbox_inches="tight", facecolor="white")
        paths.append(target)
    return paths


def _extent(grid: GridSpec):
    return (grid.xmin, grid.xmax, grid.ymin, grid.ymax)


def add_map_furniture(ax, grid: GridSpec) -> None:
    """Add a metric scale bar and north arrow to a projected spatial panel."""
    xmin, xmax = sorted(float(value) for value in ax.get_xlim())
    ymin, ymax = sorted(float(value) for value in ax.get_ylim())
    width = xmax - xmin
    candidates = np.asarray([100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000], dtype=float)
    target = width * 0.20
    scale_m = float(candidates[np.argmin(np.abs(candidates - target))])
    x0 = xmin + 0.07 * width
    yspan = ymax - ymin
    y0 = ymin + 0.055 * yspan
    ax.plot([x0, x0 + scale_m], [y0, y0], color="black", linewidth=2.0, solid_capstyle="butt", zorder=20)
    ax.plot([x0, x0], [y0 - 0.008 * yspan, y0 + 0.008 * yspan], color="black", linewidth=1.0, zorder=20)
    ax.plot([x0 + scale_m, x0 + scale_m], [y0 - 0.008 * yspan, y0 + 0.008 * yspan], color="black", linewidth=1.0, zorder=20)
    label = f"{scale_m / 1000:g} km" if scale_m >= 1000 else f"{scale_m:g} m"
    ax.text(x0 + scale_m / 2, y0 + 0.018 * yspan, label, ha="center", va="bottom", fontsize=6.5,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.78, "pad": 1.0}, zorder=21)
    ax.annotate("N", xy=(0.92, 0.92), xytext=(0.92, 0.77), xycoords="axes fraction",
                textcoords="axes fraction", ha="center", va="center", fontsize=7, fontweight="bold",
                arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1.0}, zorder=21)


def _map(ax, data, grid, title, label, cmap="viridis", **kwargs):
    image = ax.imshow(data, extent=_extent(grid), origin="upper", cmap=cmap, **kwargs)
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    ax.set_aspect("equal")
    cb = ax.figure.colorbar(image, ax=ax, fraction=0.046, pad=0.025)
    cb.set_label(label)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=3))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=3))
    ax.ticklabel_format(style="sci", scilimits=(0, 0), useOffset=False)
    add_map_furniture(ax, grid)
    return image


def plot_dem_domain(original_dem, final_dem, node_mask, grid: GridSpec, wall_elevation_m=9999.0):
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.8), constrained_layout=True)
    interior = np.where(node_mask, original_dem, np.nan)
    display = np.where(np.isclose(final_dem, wall_elevation_m), np.nan, final_dem)
    # Equal elevations must have the same colour in every comparison panel.
    # Include real outlet cuts, exclude artificial walls, and avoid a combined
    # full-grid allocation on regional domains.
    vmin = min(float(np.nanmin(interior)), float(np.nanmin(display)))
    vmax = max(float(np.nanmax(interior)), float(np.nanmax(display)))
    _map(axes[0], original_dem, grid, "a  Terrain before wall/outlets", "Elevation (m)", vmin=vmin, vmax=vmax, cmap="terrain")
    _map(axes[1], interior, grid, "b  Active domain", "Elevation (m)", vmin=vmin, vmax=vmax, cmap="terrain")
    _map(axes[2], display, grid, "c  Final terrain", "Elevation (m)", vmin=vmin, vmax=vmax, cmap="terrain")
    axes[2].contour(
        np.isclose(final_dem, wall_elevation_m), levels=[0.5], extent=_extent(grid),
        origin="upper", colors="#d62728", linewidths=0.65,
    )
    return fig


def plot_river_bathymetry(original_dem, burned_dem, burn_depth, grid: GridSpec):
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.8), constrained_layout=True)
    vmin, vmax = np.nanpercentile(original_dem, [1, 99])
    _map(axes[0], original_dem, grid, "a  Terrain before burning", "Elevation (m)", cmap="terrain", vmin=vmin, vmax=vmax)
    _map(axes[1], burn_depth, grid, "b  Applied channel depth", "Depth (m)", cmap="Blues")
    _map(axes[2], burned_dem, grid, "c  Terrain after burning", "Elevation (m)", cmap="terrain", vmin=vmin, vmax=vmax)
    return fig


def plot_outlet_diagnostics(dem, carved_mask, outlets, grid: GridSpec, wall_elevation_m=9999.0):
    fig, ax = plt.subplots(figsize=(4.8, 4.0), constrained_layout=True)
    visible = np.ma.masked_where(np.isclose(dem, wall_elevation_m), dem)
    _map(ax, visible, grid, "Carved outlets and corridors", "Elevation (m)", cmap="terrain")
    ax.contour(
        np.isclose(dem, wall_elevation_m), [0.5], extent=_extent(grid), origin="upper",
        colors="#3a3a3a", linewidths=0.6,
    )
    ax.contour(
        carved_mask, [0.5], extent=_extent(grid), origin="upper",
        colors="#0072B2", linewidths=1.3,
    )
    outlets.plot(ax=ax, color="#D55E00", marker="x", markersize=28, zorder=5)
    return fig


def plot_drainage(drainage_map, parameter_table, grid: GridSpec):
    ids = sorted(int(x) for x in np.unique(drainage_map) if x != 0)
    colors = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9"]
    cmap = ListedColormap(["#ffffff"] + colors[: len(ids)])
    remapped = np.zeros_like(drainage_map, int)
    for index, type_id in enumerate(ids, 1):
        remapped[drainage_map == type_id] = index
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), constrained_layout=True)
    axes[0].imshow(remapped, extent=_extent(grid), origin="upper", cmap=cmap, norm=BoundaryNorm(np.arange(-0.5, len(ids) + 1.5), cmap.N))
    axes[0].set(title="a  Infrastructure classes", xlabel="Easting (m)", ylabel="Northing (m)")
    add_map_furniture(axes[0], grid)
    counts = pd.Series(drainage_map.ravel()).value_counts().drop(index=0, errors="ignore").reindex(ids, fill_value=0)
    axes[1].bar([str(i) for i in ids], counts.values * grid.cell_area_m2 / 1e6, color=colors[: len(ids)])
    axes[1].set(title="b  Mapped infrastructure area", xlabel="Infrastructure type", ylabel="Area (km²)")
    return fig


def plot_rainfall(time_s, rainfall_m_s, grid: GridSpec | None = None):
    rain = np.asarray(rainfall_m_s)
    mean = rain.reshape(rain.shape[0], -1).mean(axis=1) * 3.6e6
    maximum = rain.reshape(rain.shape[0], -1).max(axis=1) * 3.6e6
    fig, ax = plt.subplots(figsize=(4.8, 3.0), constrained_layout=True)
    ax.plot(np.asarray(time_s) / 3600, mean, label="Domain mean", color="#0072B2")
    ax.plot(np.asarray(time_s) / 3600, maximum, label="Domain maximum", color="#D55E00")
    ax.set(xlabel="Time (h)", ylabel="Rainfall rate (mm h$^{-1}$)", title="Rainfall forcing")
    ax.legend()
    return fig


def plot_hydrographs(hydrographs: Mapping[str, tuple[np.ndarray, np.ndarray]]):
    fig, ax = plt.subplots(figsize=(4.8, 3.0), constrained_layout=True)
    for name, (time_s, discharge) in hydrographs.items():
        ax.plot(np.asarray(time_s) / 3600, discharge, label=name)
    ax.set(xlabel="Time (h)", ylabel="Discharge (m³ s$^{-1}$)", title="Inflow hydrographs")
    if len(hydrographs) <= 12:
        ax.legend(ncol=2)
    return fig


def plot_input_atlas(layers: Mapping[str, tuple[np.ndarray, str, str]], grid: GridSpec, columns: int = 3):
    count = len(layers)
    rows = int(np.ceil(count / columns))
    fig, axes = plt.subplots(rows, columns, figsize=(4.5 * columns, 4.0 * rows), constrained_layout=True, squeeze=False)
    for ax, (name, (data, units, cmap)) in zip(axes.flat, layers.items()):
        _map(ax, data, grid, name, units, cmap=cmap)
    for ax in axes.flat[count:]:
        ax.remove()
    return fig
