import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box

from riftprep.config import GridSpec, RiverBathymetrySpec
from riftprep.spatial import burn_river_bathymetry, create_domain_masks


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
