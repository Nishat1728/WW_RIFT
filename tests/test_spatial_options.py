import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box

from riftprep.config import GridSpec, OutletSpec, RiverBathymetrySpec
from riftprep.spatial import (
    apply_closed_huc_wall,
    burn_river_bathymetry,
    carve_outlets,
    create_domain_masks,
)


def test_boundary_rules_are_honored():
    grid = GridSpec(0, 0, 100, 100, 25, "EPSG:3857", 4, 4)
    boundary = gpd.GeoDataFrame(geometry=[box(12, 12, 88, 88)], crs=grid.crs)
    center_node, _ = create_domain_masks(boundary, grid, "cell_center")
    touched_node, _ = create_domain_masks(boundary, grid, "all_touched")
    interior_node, _ = create_domain_masks(boundary, grid, "interior")
    assert touched_node.sum() >= center_node.sum() >= interior_node.sum() > 0


def test_bathymetry_profiles_and_monotonicity():
    grid = GridSpec(0, 0, 100, 100, 10, "EPSG:3857", 10, 10)
    dem = np.tile(np.linspace(100, 110, 11)[:, None], (1, 11))
    active = np.ones(grid.node_shape, dtype=bool)
    rivers = gpd.GeoDataFrame(geometry=[LineString([(50, 100), (50, 0)])], crs=grid.crs)
    flat, flat_depth = burn_river_bathymetry(
        dem, rivers, active, grid,
        RiverBathymetrySpec(apply_bathymetry=True, depth_m=2, width_m=40, cross_section="flat"),
    )
    shaped, shaped_depth = burn_river_bathymetry(
        dem, rivers, active, grid,
        RiverBathymetrySpec(
            apply_bathymetry=True, depth_m=2, width_m=40, cross_section="v",
            enforce_downstream_monotonicity=True, downstream_direction="south",
        ),
    )
    assert np.max(flat_depth) == 2
    assert 0 < shaped_depth[:, 4].mean() < shaped_depth[:, 5].mean()
    channel_bed = shaped[:, 5]
    assert np.all(np.diff(channel_bed) <= 1e-12)
    assert np.all(flat <= dem)


def test_outlet_carving_uses_only_corridor_cells_and_terrain_matches():
    grid = GridSpec(0, 0, 100, 100, 10, "EPSG:3857", 10, 10)
    dem = np.tile(np.linspace(100, 110, 11)[:, None], (1, 11))
    active = np.zeros(grid.node_shape, dtype=bool)
    active[1:-1, 1:-1] = True
    walled = apply_closed_huc_wall(dem, active)
    spec = OutletSpec(
        source="coordinates",
        coordinates=({"x": 20.0, "y": 50.0, "width_m": 10.0},),
        crs=grid.crs,
        method="open_corridor",
        elevation_mode="terrain_matched",
    )

    carved_dem, _, carved = carve_outlets(walled, dem, active, grid, spec)

    assert carved.any()
    assert np.all(carved_dem[carved] < 9999.0)
    assert np.all(carved_dem[~carved] == walled[~carved])
    assert np.all(carved_dem[carved] >= np.nanmin(dem[active]))


def test_outlet_coordinate_rows_can_override_cut_elevation():
    grid = GridSpec(0, 0, 100, 100, 10, "EPSG:3857", 10, 10)
    dem = np.full(grid.node_shape, 100.0)
    active = np.zeros(grid.node_shape, dtype=bool)
    active[1:-1, 1:-1] = True
    walled = apply_closed_huc_wall(dem, active)
    spec = OutletSpec(
        source="coordinates",
        coordinates=({"x": 20.0, "y": 50.0, "cut_elevation_m": 7.5},),
        crs=grid.crs,
        method="open_corridor",
        elevation_mode="constant",
        cut_elevation_m=0.0,
    )

    carved_dem, _, carved = carve_outlets(walled, dem, active, grid, spec)

    assert carved.any()
    assert np.allclose(carved_dem[carved], 7.5)
