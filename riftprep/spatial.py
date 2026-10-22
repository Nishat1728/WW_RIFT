from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject
from scipy.ndimage import distance_transform_edt
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from .config import DomainSpec, GridSpec, OutletSpec, RiverBathymetrySpec


def _auto_utm(boundary: gpd.GeoDataFrame) -> str:
    ll = boundary.to_crs(4326)
    center = unary_union(ll.geometry).centroid
    zone = int((center.x + 180) // 6) + 1
    epsg = 32600 + zone if center.y >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def build_grid(spec: DomainSpec, boundary: gpd.GeoDataFrame | None = None) -> GridSpec:
    if spec.mode in ("huc", "shapefile"):
        if boundary is None or boundary.empty:
            raise ValueError("A boundary is required in HUC or shapefile mode.")
        target_crs = _auto_utm(boundary) if spec.target_crs == "auto_utm" else spec.target_crs
        geom = boundary.to_crs(target_crs)
        xmin, ymin, xmax, ymax = geom.total_bounds
    else:
        bounds_ll = gpd.GeoDataFrame(geometry=[box(*spec.bounds)], crs=spec.bounds_crs)
        target_crs = _auto_utm(bounds_ll) if spec.target_crs == "auto_utm" else spec.target_crs
        bounds = bounds_ll.to_crs(target_crs).geometry
        xmin, ymin, xmax, ymax = bounds.total_bounds
    pad = spec.download_buffer_cells * spec.cell_size_m
    xmin, ymin, xmax, ymax = xmin - pad, ymin - pad, xmax + pad, ymax + pad
    r = spec.cell_size_m
    xmin, ymin = math.floor(xmin / r) * r, math.floor(ymin / r) * r
    xmax, ymax = math.ceil(xmax / r) * r, math.ceil(ymax / r) * r
    nx, ny = int(round((xmax - xmin) / r)), int(round((ymax - ymin) / r))
    return GridSpec(xmin, ymin, xmax, ymax, r, str(CRS.from_user_input(target_crs)), nx, ny)


def _mask_for_points(geometry, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    try:
        from shapely import contains_xy

        xx, yy = np.meshgrid(x, y)
        return contains_xy(geometry.buffer(1e-8), xx, yy)
    except ImportError:
        prepared = geometry.buffer(1e-8)
        return np.array([[prepared.covers(Point(xi, yi)) for xi in x] for yi in y], dtype=bool)


def create_domain_masks(
    boundary: gpd.GeoDataFrame,
    grid: GridSpec,
    boundary_rule: str = "cell_center",
) -> tuple[np.ndarray, np.ndarray]:
    """Create node/cell masks using the explicitly selected boundary rule."""
    geom = unary_union(boundary.to_crs(grid.crs).geometry)
    node_x = grid.xmin + np.arange(grid.nx + 1) * grid.resolution
    node_y = grid.ymax - np.arange(grid.ny + 1) * grid.resolution
    cell_x = grid.xmin + (np.arange(grid.nx) + 0.5) * grid.resolution
    cell_y = grid.ymax - (np.arange(grid.ny) + 0.5) * grid.resolution
    if boundary_rule == "cell_center":
        return _mask_for_points(geom, node_x, node_y), _mask_for_points(geom, cell_x, cell_y)
    if boundary_rule == "all_touched":
        node = rasterize(
            [(geom, 1)], out_shape=grid.node_shape, transform=_grid_transform(grid, "node"),
            fill=0, all_touched=True,
        ).astype(bool)
        cell = rasterize(
            [(geom, 1)], out_shape=grid.cell_shape, transform=_grid_transform(grid, "cell"),
            fill=0, all_touched=True,
        ).astype(bool)
        return node, cell
    if boundary_rule == "interior":
        # Require each square support to remain inside the polygon, not only its center.
        inset = geom.buffer(-grid.resolution * math.sqrt(2.0) / 2.0)
        if inset.is_empty:
            raise ValueError("The domain is too narrow for boundary_rule='interior'.")
        return _mask_for_points(inset, node_x, node_y), _mask_for_points(inset, cell_x, cell_y)
    raise ValueError("boundary_rule must be cell_center, all_touched, or interior.")


def read_raster(path: str | Path) -> tuple[np.ndarray, dict]:
    with rasterio.open(path) as src:
        return src.read(1), src.profile.copy()


def _grid_transform(grid: GridSpec, location: str):
    if location == "node":
        return from_origin(
            grid.xmin - grid.resolution / 2,
            grid.ymax + grid.resolution / 2,
            grid.resolution,
            grid.resolution,
        )
    return from_origin(grid.xmin, grid.ymax, grid.resolution, grid.resolution)


def align_raster(
    source: str | Path,
    grid: GridSpec,
    location: str = "node",
    resampling: str = "bilinear",
    dst_nodata: float = np.nan,
    num_threads: int | None = None,
    source_invalid_values: Iterable[float] | None = None,
) -> np.ndarray:
    """Reproject a raster onto the exact RIFT node or flow-cell grid.

    ``source_invalid_values`` masks product-specific flag codes in an in-memory
    processing copy.  The source raster on disk is never modified.
    """
    shape = grid.node_shape if location == "node" else grid.cell_shape
    destination = np.full(shape, dst_nodata, dtype=np.float64)
    method = getattr(Resampling, resampling)
    if num_threads is None:
        configured = os.environ.get("RIFTPREP_WARP_THREADS")
        if configured is None:
            configured = os.environ.get("SLURM_CPUS_PER_TASK")
        num_threads = int(configured) if configured else min(os.cpu_count() or 1, 8)
    num_threads = max(1, min(int(num_threads), 32))
    with rasterio.open(source) as src:
        warp_source = rasterio.band(src, 1)
        src_nodata = src.nodata
        invalid_values = tuple(float(value) for value in (source_invalid_values or ()))
        if invalid_values:
            source_values = src.read(1).astype(np.float64, copy=False)
            invalid = ~np.isfinite(source_values)
            if src.nodata is not None:
                invalid |= np.isclose(source_values, src.nodata, rtol=0.0, atol=0.0)
            for value in invalid_values:
                invalid |= np.isclose(source_values, value, rtol=0.0, atol=0.0)
            source_values[invalid] = np.nan
            warp_source = source_values
            src_nodata = np.nan
        reproject(
            source=warp_source,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src_nodata,
            dst_transform=_grid_transform(grid, location),
            dst_crs=grid.crs,
            dst_nodata=dst_nodata,
            resampling=method,
            num_threads=num_threads,
        )
    return destination


def apply_closed_huc_wall(dem: np.ndarray, node_mask: np.ndarray, wall_elevation_m: float = 9999.0) -> np.ndarray:
    if dem.shape != node_mask.shape:
        raise ValueError("DEM and node mask shapes differ.")
    interior = dem[node_mask]
    if interior.size == 0 or not np.isfinite(interior).all():
        raise ValueError("The active-domain DEM contains missing or non-finite elevations.")
    if wall_elevation_m <= float(np.max(interior)):
        raise ValueError("Wall elevation must exceed the maximum interior DEM elevation.")
    result = dem.astype(float, copy=True)
    result[~node_mask] = wall_elevation_m
    return result


def derive_river_terminus(
    rivers: gpd.GeoDataFrame,
    target_crs: str,
    downstream_direction: str = "south",
) -> Point:
    """Return an extreme river vertex for an explicitly known downstream direction.

    This is intended for a named mainstem whose regional direction is known. It avoids
    confusing an upstream watershed-boundary crossing with a downstream river mouth.
    """
    if rivers.empty:
        raise ValueError("Cannot derive a terminus from an empty river layer.")
    data = rivers.to_crs(target_crs)
    coordinates: list[tuple[float, float]] = []

    def collect(geometry) -> None:
        if geometry is None or geometry.is_empty:
            return
        if geometry.geom_type in {"LineString", "LinearRing"}:
            coordinates.extend((float(x), float(y)) for x, y, *_ in geometry.coords)
        elif hasattr(geometry, "geoms"):
            for part in geometry.geoms:
                collect(part)

    for geometry in data.geometry:
        collect(geometry)
    if not coordinates:
        raise ValueError("River layer contains no line vertices.")
    selectors = {
        "south": lambda xy: xy[1],
        "north": lambda xy: -xy[1],
        "west": lambda xy: xy[0],
        "east": lambda xy: -xy[0],
    }
    if downstream_direction not in selectors:
        raise ValueError("downstream_direction must be north, south, east, or west.")
    return Point(min(coordinates, key=selectors[downstream_direction]))


def _outlet_gdf(spec: OutletSpec, target_crs: str) -> gpd.GeoDataFrame:
    if spec.source == "coordinates":
        if not spec.coordinates or not spec.crs:
            raise ValueError("Coordinate outlets require coordinates and a CRS.")
        rows = list(spec.coordinates)
        geometry = [Point(float(r["x"]), float(r["y"])) for r in rows]
        return gpd.GeoDataFrame(rows, geometry=geometry, crs=spec.crs).to_crs(target_crs)
    data = gpd.read_file(spec.source)
    if data.crs is None:
        raise ValueError("Outlet vector has no CRS.")
    return data.to_crs(target_crs)


def _nearest_valid_dem_value(
    dem: np.ndarray,
    node_mask: np.ndarray,
    grid: GridSpec,
    point: Point,
) -> float:
    """Return the nearest finite active DEM value without full-grid coordinates."""
    candidate = node_mask & np.isfinite(dem)
    if not candidate.any():
        raise ValueError("Cannot terrain-match an outlet without active DEM values.")

    nrows, ncols = dem.shape
    row0 = int(np.clip(np.rint((grid.ymax - point.y) / grid.resolution), 0, nrows - 1))
    col0 = int(np.clip(np.rint((point.x - grid.xmin) / grid.resolution), 0, ncols - 1))
    if candidate[row0, col0]:
        return float(dem[row0, col0])

    # The automatic outlet resolver normally lands on an active node.  This
    # bounded fallback handles an explicit point that falls just outside the
    # active mask without allocating arrays for the entire HUC2 grid.
    for radius in (1, 2, 4, 8, 16, 32, 64, 128):
        r0, r1 = max(0, row0 - radius), min(nrows, row0 + radius + 1)
        c0, c1 = max(0, col0 - radius), min(ncols, col0 + radius + 1)
        local = candidate[r0:r1, c0:c1]
        if not local.any():
            continue
        rows, cols = np.where(local)
        rows = rows + r0
        cols = cols + c0
        dx = grid.xmin + cols * grid.resolution - point.x
        dy = grid.ymax - rows * grid.resolution - point.y
        index = int(np.argmin(dx * dx + dy * dy))
        return float(dem[rows[index], cols[index]])

    # Rare fallback for a point far outside a highly fragmented mask.  Work in
    # row blocks so memory scales with the block, not with the whole grid.
    x = grid.xmin + np.arange(ncols, dtype=float) * grid.resolution
    best_distance = np.inf
    best_value: float | None = None
    for start in range(0, nrows, 512):
        stop = min(start + 512, nrows)
        block = candidate[start:stop]
        if not block.any():
            continue
        rows, cols = np.where(block)
        rows = rows + start
        dx = x[cols] - point.x
        dy = grid.ymax - rows * grid.resolution - point.y
        distances = dx * dx + dy * dy
        index = int(np.argmin(distances))
        if float(distances[index]) < best_distance:
            best_distance = float(distances[index])
            best_value = float(dem[rows[index], cols[index]])
    if best_value is None:
        raise ValueError("Cannot terrain-match an outlet without active DEM values.")
    return best_value


def carve_outlets(
    walled_dem: np.ndarray,
    original_dem: np.ndarray,
    node_mask: np.ndarray,
    grid: GridSpec,
    spec: OutletSpec,
    wall_elevation_m: float = 9999.0,
) -> tuple[np.ndarray, gpd.GeoDataFrame, np.ndarray]:
    """Cut one or more notches or corridors through a closed-domain wall."""
    outlets = _outlet_gdf(spec, grid.crs)
    result = walled_dem.astype(float, copy=True)
    carved = np.zeros_like(node_mask, dtype=bool)

    for _, feature in outlets.iterrows():
        point = feature.geometry.centroid
        width = float(feature.get("width_m", spec.default_width_m))
        feature_cut_elevation = feature.get("cut_elevation_m", spec.cut_elevation_m)
        try:
            feature_cut_elevation = float(feature_cut_elevation)
        except (TypeError, ValueError):
            feature_cut_elevation = spec.cut_elevation_m
        if not np.isfinite(feature_cut_elevation):
            feature_cut_elevation = spec.cut_elevation_m
        feature_cut_elevation = float(feature_cut_elevation)
        target = point
        if spec.method == "open_corridor":
            candidates = [
                Point(grid.xmin, point.y), Point(grid.xmax, point.y),
                Point(point.x, grid.ymin), Point(point.x, grid.ymax),
            ]
            edge = min(candidates, key=point.distance)
            target = LineString([point, edge]).buffer(max(width / 2, grid.resolution / 2), cap_style=2)
        else:
            # A wall notch has no domain-edge endpoint.  Keep the outlet point
            # as the reference endpoint for the optional sloped elevation mode.
            edge = point
            target = point.buffer(max(width / 2, grid.resolution / 2))
        cut = rasterize(
            [(target, 1)], out_shape=grid.node_shape,
            transform=_grid_transform(grid, "node"), fill=0, all_touched=True,
        ).astype(bool)
        cut &= np.isclose(result, wall_elevation_m)
        if not cut.any():
            raise ValueError(f"Outlet at ({point.x:.2f}, {point.y:.2f}) does not intersect the wall.")
        if spec.elevation_mode in {"zero", "constant"}:
            # Assign only the rasterized corridor cells.  ``zero`` retains the
            # historical cut_elevation_m default of 0 m while allowing an
            # explicit value when requested.
            result[cut] = feature_cut_elevation
        elif spec.elevation_mode == "terrain_matched":
            terrain = _nearest_valid_dem_value(original_dem, node_mask, grid, point)
            result[cut] = terrain
        elif spec.elevation_mode == "sloped":
            terrain = _nearest_valid_dem_value(original_dem, node_mask, grid, point)
            rows, cols = np.where(cut)
            x = grid.xmin + cols * grid.resolution
            y = grid.ymax - rows * grid.resolution
            distance = np.sqrt((x - point.x) ** 2 + (y - point.y) ** 2)
            edge_z = feature_cut_elevation
            length = max(point.distance(edge), grid.resolution)
            result[cut] = terrain + np.clip(distance / length, 0, 1) * (edge_z - terrain)
        else:
            raise ValueError(f"Unsupported outlet elevation mode: {spec.elevation_mode}")
        carved |= cut
    return result, outlets, carved


def burn_river_bathymetry(
    dem: np.ndarray,
    rivers: gpd.GeoDataFrame,
    node_mask: np.ndarray,
    grid: GridSpec,
    spec: RiverBathymetrySpec,
) -> tuple[np.ndarray, np.ndarray]:
    """Lower a DEM along river vectors and return the modified DEM and depth raster."""
    if not spec.apply_bathymetry:
        return dem.copy(), np.zeros_like(dem, dtype=float)
    river_data = rivers.to_crs(grid.crs)
    depth = np.zeros_like(dem, dtype=float)
    for _, feature in river_data.iterrows():
        if feature.geometry is None or feature.geometry.is_empty:
            continue
        width = float(feature.get(spec.width_field, spec.width_m)) if spec.width_method == "attribute" else spec.width_m
        if spec.depth_method == "attribute":
            d = float(feature[spec.depth_field])
        elif spec.depth_method == "stream_order":
            order = int(feature[spec.stream_order_field])
            d = float(spec.depth_by_order.get(order, spec.depth_m))
        else:
            d = spec.depth_m
        half_width = max(width / 2, grid.resolution / 2)
        footprint = feature.geometry.buffer(half_width, cap_style=2)
        mask = rasterize(
            [(footprint, 1)], out_shape=grid.node_shape,
            transform=_grid_transform(grid, "node"), fill=0, all_touched=True,
        ).astype(bool) & node_mask
        if spec.cross_section == "flat":
            candidate_depth = np.where(mask, d, 0.0)
        else:
            centerline = rasterize(
                [(feature.geometry, 1)], out_shape=grid.node_shape,
                transform=_grid_transform(grid, "node"), fill=0, all_touched=True,
            ).astype(bool)
            distance = distance_transform_edt(~centerline) * grid.resolution
            if spec.cross_section == "v":
                candidate_depth = d * np.clip(1.0 - distance / half_width, 0.0, 1.0)
            elif spec.cross_section == "gaussian":
                sigma = max(half_width / 3.0, grid.resolution / 2.0)
                candidate_depth = d * np.exp(-0.5 * (distance / sigma) ** 2)
                candidate_depth[~mask] = 0.0
            else:
                raise ValueError("cross_section must be flat, v, or gaussian.")
        depth = np.maximum(depth, np.where(mask, candidate_depth, 0.0))
    modified = dem - depth
    if spec.preserve_existing_lower_terrain:
        modified = np.minimum(modified, dem)
    if spec.enforce_downstream_monotonicity:
        _enforce_axis_monotonicity(
            modified,
            depth > 0,
            spec.downstream_direction,
            spec.minimum_downstream_slope * grid.resolution,
        )
        depth = np.maximum(dem - modified, 0.0)
    return modified, depth


def _enforce_axis_monotonicity(
    elevation: np.ndarray,
    channel: np.ndarray,
    direction: str,
    minimum_drop_per_cell: float,
) -> None:
    """Enforce downstream non-increase within contiguous channel runs in place."""
    if direction in {"north", "south"}:
        row_order = range(elevation.shape[0]) if direction == "south" else range(elevation.shape[0] - 1, -1, -1)
        for column in range(elevation.shape[1]):
            previous = None
            for row in row_order:
                if not channel[row, column]:
                    previous = None
                    continue
                if previous is not None:
                    elevation[row, column] = min(elevation[row, column], previous - minimum_drop_per_cell)
                previous = elevation[row, column]
    elif direction in {"east", "west"}:
        column_order = range(elevation.shape[1]) if direction == "east" else range(elevation.shape[1] - 1, -1, -1)
        for row in range(elevation.shape[0]):
            previous = None
            for column in column_order:
                if not channel[row, column]:
                    previous = None
                    continue
                if previous is not None:
                    elevation[row, column] = min(elevation[row, column], previous - minimum_drop_per_cell)
                previous = elevation[row, column]
    else:
        raise ValueError("downstream_direction must be north, south, east, or west.")
