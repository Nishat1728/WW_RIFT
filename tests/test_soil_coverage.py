import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from riftprep.config import GridSpec
from riftprep.processing import prepare_ssurgo


def _aligned_raster(path: Path, value: float) -> Path:
    with rasterio.open(
        path, "w", driver="GTiff", height=3, width=3, count=1,
        dtype="float32", crs="EPSG:5070",
        transform=from_origin(-15, 75, 30, 30), nodata=-9999.0,
    ) as output:
        output.write(np.full((3, 3), value, dtype=np.float32), 1)
    return path


def _soil(path: Path) -> Path:
    data = gpd.GeoDataFrame({
        "permeability [m^2]": [1e-12],
        "porosity [-]": [0.42],
        "residual saturation [-]": [0.08],
        "van Genuchten alpha [Pa^-1]": [2e-4],
    }, geometry=[box(0, 0, 30, 60)], crs="EPSG:5070")
    data.to_file(path, driver="GPKG")
    return path


def test_soil_gap_requires_explicit_user_values(tmp_path: Path):
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    report = tmp_path / "qc" / "soil_coverage.json"
    with pytest.raises(ValueError, match="No automatic fill was performed"):
        prepare_ssurgo(
            _soil(tmp_path / "soil.gpkg"), grid,
            np.ones(grid.node_shape, dtype=bool), tmp_path / "inputs",
            coverage_report_path=report,
        )
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["status"] == "NEEDS USER INPUT"
    assert evidence["automatic_fill_performed"] is False
    assert (tmp_path / "qc" / "soil_coverage_map.png").is_file()

    prepare_ssurgo(
        tmp_path / "soil.gpkg", grid,
        np.ones(grid.node_shape, dtype=bool), tmp_path / "inputs",
        coverage_report_path=report,
        manual_fill_values={
            "permeability": 1e-12,
            "porosity": 0.42,
            "residual": 0.08,
            "alpha": 2e-4,
        },
    )
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["status"] == "PASSED"
    assert evidence["complete"] is True


def test_small_residual_porosity_gap_is_recorded_and_accepted(tmp_path: Path):
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    soil = gpd.GeoDataFrame({
        "permeability [m^2]": [1e-12],
        "porosity [-]": [0.236],
        "residual saturation [-]": [0.247],
        "van Genuchten alpha [Pa^-1]": [2e-4],
    }, geometry=[box(0, 0, 60, 60)], crs="EPSG:5070")
    soil_path = tmp_path / "small_gap_soil.gpkg"
    soil.to_file(soil_path, driver="GPKG")
    report = tmp_path / "qc" / "soil_coverage.json"

    prepare_ssurgo(
        soil_path,
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "inputs",
        coverage_report_path=report,
        residual_porosity_tolerance=0.1,
    )
    evidence = json.loads(report.read_text(encoding="utf-8"))
    check = evidence["residual_porosity_check"]
    assert evidence["status"] == "PASSED"
    assert check["accepted_by_tolerance_nodes"] == 9
    assert check["materially_invalid_nodes"] == 0
    assert check["values_modified"] is False


def test_material_residual_porosity_gap_still_fails(tmp_path: Path):
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    soil = gpd.GeoDataFrame({
        "permeability [m^2]": [1e-12],
        "porosity [-]": [0.2],
        "residual saturation [-]": [0.3],
        "van Genuchten alpha [Pa^-1]": [2e-4],
    }, geometry=[box(0, 0, 60, 60)], crs="EPSG:5070")
    soil_path = tmp_path / "large_gap_soil.gpkg"
    soil.to_file(soil_path, driver="GPKG")
    report = tmp_path / "qc" / "soil_coverage.json"

    with pytest.raises(ValueError, match="material active-node violations"):
        prepare_ssurgo(
            soil_path,
            grid,
            np.ones(grid.node_shape, dtype=bool),
            tmp_path / "inputs",
            coverage_report_path=report,
            residual_porosity_tolerance=0.1,
        )
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["status"] == "FAILED"
    assert evidence["residual_porosity_check"]["materially_invalid_nodes"] == 9


def test_gridded_soil_adapter_products_flow_to_rift_outputs(tmp_path: Path):
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    sources = {
        "permeability": _aligned_raster(tmp_path / "permeability.tif", 1.0e-12),
        "porosity": _aligned_raster(tmp_path / "porosity.tif", 0.45),
        "residual": _aligned_raster(tmp_path / "residual.tif", 0.10),
        "alpha": _aligned_raster(tmp_path / "alpha.tif", 2.0e-4),
    }
    report = tmp_path / "qc" / "soil_coverage.json"
    ksat, porosity, suction, saturation, outputs, _ = prepare_ssurgo(
        sources, grid, np.ones(grid.node_shape, dtype=bool), tmp_path / "inputs",
        initial_saturation_value=0.4, coverage_report_path=report,
    )
    assert ksat.shape == grid.node_shape
    np.testing.assert_allclose(porosity, 0.45)
    np.testing.assert_allclose(saturation, 0.4)
    assert np.all(np.isfinite(suction))
    assert all(path.is_file() for path in outputs.values())
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "PASSED"
