import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
import xarray as xr
from pyproj import Transformer
from rasterio.transform import from_origin

from riftprep.config import GridSpec
from riftprep.managers import dataset_variable_to_geotiff
import riftprep.processing as processing
from riftprep.processing import prepare_nlcd
from riftprep.workflow import _query_geometry


def _raster(path: Path, values: np.ndarray, grid: GridSpec) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype="float32",
        crs=grid.crs,
        transform=from_origin(
            grid.xmin - grid.resolution / 2,
            grid.ymax + grid.resolution / 2,
            grid.resolution,
            grid.resolution,
        ),
        nodata=-9999.0,
    ) as target:
        target.write(values.astype("float32"), 1)
    return path


def test_query_geometry_contains_densified_projected_grid_edge():
    grid = GridSpec(
        1_824_270.0, 2_209_650.0, 2_325_360.0, 3_087_030.0,
        30.0, "EPSG:5070", 16_703, 29_246,
    )
    query = _query_geometry(grid).geometry.iloc[0]
    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    ys = np.linspace(grid.ymin, grid.ymax, 1001)
    xs = np.full_like(ys, grid.xmin)
    lon, lat = transformer.transform(xs, ys)
    qxmin, qymin, qxmax, qymax = query.bounds
    assert np.min(lon) >= qxmin
    assert np.max(lon) <= qxmax
    assert np.min(lat) >= qymin
    assert np.max(lat) <= qymax


def test_prepare_nlcd_records_and_rejects_active_node_gap(tmp_path: Path):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    values = np.full(grid.node_shape, 41.0, dtype=np.float32)
    cover = values.copy()
    cover[1, 1] = -9999.0
    cover_path = _raster(tmp_path / "cover.tif", cover, grid)
    impervious_path = _raster(tmp_path / "impervious.tif", values, grid)
    canopy_path = _raster(tmp_path / "canopy.tif", values, grid)
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    with pytest.raises(ValueError, match="No automatic fill was performed"):
        prepare_nlcd(
            cover_path,
            impervious_path,
            canopy_path,
            grid,
            np.ones(grid.node_shape, dtype=bool),
            tmp_path / "rift_inputs",
            coverage_report_path=report_path,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert not report["complete"]
    assert report["status"] == "NEEDS USER INPUT"
    assert report["variables"]["cover"]["missing_active_nodes"] == 1
    assert (tmp_path / "qc" / "land_cover_coverage_map.png").is_file()
    assert not (tmp_path / "rift_inputs" / "nlcd_selected_class.asc").exists()

    prepare_nlcd(
        cover_path,
        impervious_path,
        canopy_path,
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
        manual_fill_values={"cover": 41},
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"]
    assert report["status"] == "PASSED"
    assert report["automatic_fill_performed"] is False
    assert report["manual_resolutions"]["cover"]["filled_active_nodes"] == 1


def test_prepare_nlcd_maps_unsupported_cover_classes_then_uses_user_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    cover = np.full(grid.node_shape, 41.0)
    cover[0, 0] = -1.0
    cover[0, 1] = 0.0
    cover[1, 0] = 41.5
    impervious = np.full(grid.node_shape, 25.0)
    canopy = np.full(grid.node_shape, 60.0)
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    def install_alignment():
        aligned = iter((cover, impervious, canopy))
        monkeypatch.setattr(
            processing, "align_raster", lambda *args, **kwargs: next(aligned).copy()
        )
        monkeypatch.setattr(
            processing, "_count_raster_codes", lambda *args, **kwargs: {"254": 0, "255": 0}
        )

    install_alignment()
    with pytest.raises(ValueError, match="No automatic fill was performed"):
        prepare_nlcd(
            "cover.tif",
            "impervious.tif",
            "canopy.tif",
            grid,
            np.ones(grid.node_shape, dtype=bool),
            tmp_path / "rift_inputs",
            coverage_report_path=report_path,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    quality = report["cover_class_quality"]
    assert report["status"] == "NEEDS USER INPUT"
    assert report["variables"]["cover"]["missing_active_nodes"] == 3
    assert quality["invalid_active_nodes"] == 3
    assert quality["unsupported_integer_active_nodes"] == 2
    assert quality["non_integer_active_nodes"] == 1
    assert quality["unsupported_active_class_counts"] == {"-1": 1, "0": 1}
    assert quality["non_integer_active_value_counts"] == {"41.5": 1}
    assert quality["raw_product_modified"] is False
    assert (tmp_path / "qc" / "land_cover_coverage_map.png").is_file()

    install_alignment()
    result_cover, _, _, _, _ = prepare_nlcd(
        "cover.tif",
        "impervious.tif",
        "canopy.tif",
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
        manual_fill_values={"cover": 43},
    )
    assert result_cover[0, 0] == 43
    assert result_cover[0, 1] == 43
    assert result_cover[1, 0] == 43
    assert cover[0, 0] == -1.0
    assert cover[0, 1] == 0.0
    assert cover[1, 0] == 41.5
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["manual_resolutions"]["cover"]["filled_active_nodes"] == 3


def test_unexpected_dimension_requires_explicit_user_choice(tmp_path: Path):
    dataset = xr.Dataset(
        {"cover": (("y", "x", "band"), np.ones((2, 3, 2), dtype=np.float32))},
        coords={"x": [0.0, 30.0, 60.0], "y": [30.0, 0.0], "band": [1, 2]},
    )
    output = tmp_path / "cover.tif"
    with pytest.raises(ValueError, match="No automatic correction was made"):
        dataset_variable_to_geotiff(dataset, "cover", output, "EPSG:5070")

    dataset_variable_to_geotiff(
        dataset,
        "cover",
        output,
        "EPSG:5070",
        invalid_dimension_constant=41,
    )
    with rasterio.open(output) as source:
        assert source.shape == (2, 3)
        assert np.all(source.read(1) == 41)


def test_prepare_nlcd_normalizes_only_floating_point_percentage_roundoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    cover = np.full(grid.node_shape, 41.0)
    impervious = np.full(grid.node_shape, 25.0)
    canopy = np.full(grid.node_shape, 60.0)
    impervious[0, 0] = 100.0 + 3.0e-14
    canopy[1, 1] = -3.0e-14
    aligned = iter((cover, impervious, canopy))
    monkeypatch.setattr(
        processing, "align_raster", lambda *args, **kwargs: next(aligned).copy()
    )
    monkeypatch.setattr(
        processing, "_count_raster_codes", lambda *args, **kwargs: {"254": 0, "255": 0}
    )
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    _, result_impervious, result_canopy, _, _ = prepare_nlcd(
        "cover.tif",
        "impervious.tif",
        "canopy.tif",
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
    )

    assert result_impervious[0, 0] == 100.0
    assert result_canopy[1, 1] == 0.0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["automatic_fill_performed"] is False
    assert report["automatic_numeric_normalization_performed"] is True
    assert report["percent_range_quality"]["impervious"][
        "roundoff_normalized_active_nodes"
    ] == 1
    assert report["percent_range_quality"]["canopy"][
        "roundoff_normalized_active_nodes"
    ] == 1


def test_prepare_nlcd_caps_values_above_100_and_writes_before_after_maps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    cover = np.full(grid.node_shape, 41.0)
    impervious = np.full(grid.node_shape, 25.0)
    canopy = np.full(grid.node_shape, 60.0)
    impervious[0, 0] = 100.1
    impervious[1, 1] = 127.0
    canopy[0, 1] = 105.0
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    def install_alignment():
        aligned = iter((cover, impervious, canopy))
        monkeypatch.setattr(
            processing, "align_raster", lambda *args, **kwargs: next(aligned).copy()
        )
        monkeypatch.setattr(
            processing, "_count_raster_codes", lambda *args, **kwargs: {"254": 0, "255": 0}
        )

    install_alignment()
    _, result_impervious, result_canopy, _, _ = prepare_nlcd(
        "cover.tif",
        "impervious.tif",
        "canopy.tif",
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["complete"] is True
    assert report["automatic_percentage_capping_performed"] is True
    assert report["percentage_correction_policy"]["raw_products_modified"] is False
    impervious_quality = report["percent_range_quality"]["impervious"]
    assert impervious_quality["over_100_corrected_active_nodes"] == 2
    assert impervious_quality["material_over_100_corrected_active_nodes"] == 2
    assert impervious_quality["over_100_original_active_min"] == pytest.approx(100.1)
    assert impervious_quality["over_100_original_active_max"] == 127.0
    assert impervious_quality["invalid_active_nodes"] == 0
    assert report["percent_range_quality"]["canopy"][
        "over_100_corrected_active_nodes"
    ] == 1
    assert result_impervious[0, 0] == 100.0
    assert result_impervious[1, 1] == 100.0
    assert result_canopy[0, 1] == 100.0
    assert impervious[0, 0] == 100.1
    assert impervious[1, 1] == 127.0
    assert canopy[0, 1] == 105.0
    assert (
        tmp_path
        / "qc"
        / "land_cover_percentage_over_100_before_correction_map.png"
    ).is_file()
    assert (
        tmp_path / "qc" / "land_cover_percentage_after_correction_map.png"
    ).is_file()


def test_prepare_nlcd_negative_percentage_still_requires_user_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    cover = np.full(grid.node_shape, 41.0)
    impervious = np.full(grid.node_shape, 25.0)
    canopy = np.full(grid.node_shape, 60.0)
    impervious[0, 0] = -0.1
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    def install_alignment():
        aligned = iter((cover, impervious, canopy))
        monkeypatch.setattr(
            processing, "align_raster", lambda *args, **kwargs: next(aligned).copy()
        )
        monkeypatch.setattr(
            processing, "_count_raster_codes", lambda *args, **kwargs: {"254": 0, "255": 0}
        )

    install_alignment()
    with pytest.raises(ValueError, match="No automatic fill was performed"):
        prepare_nlcd(
            "cover.tif",
            "impervious.tif",
            "canopy.tif",
            grid,
            np.ones(grid.node_shape, dtype=bool),
            tmp_path / "rift_inputs",
            coverage_report_path=report_path,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "NEEDS USER INPUT"
    assert report["variables"]["impervious"]["missing_active_nodes"] == 1
    assert report["percent_range_quality"]["impervious"]["invalid_active_nodes"] == 1

    install_alignment()
    _, result_impervious, _, _, _ = prepare_nlcd(
        "cover.tif",
        "impervious.tif",
        "canopy.tif",
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
        manual_fill_values={"impervious": 0.0},
    )
    assert result_impervious[0, 0] == 0.0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["manual_resolutions"]["impervious"]["filled_active_nodes"] == 1


def test_prepare_nlcd_masks_canopy_product_flags_without_changing_raw(
    tmp_path: Path,
):
    grid = GridSpec(0.0, 0.0, 60.0, 60.0, 30.0, "EPSG:5070", 2, 2)
    cover = np.full(grid.node_shape, 41.0, dtype=np.float32)
    impervious = np.full(grid.node_shape, 25.0, dtype=np.float32)
    canopy = np.full(grid.node_shape, 60.0, dtype=np.float32)
    canopy[0, 0] = 254.0
    canopy[2, 2] = 255.0
    cover_path = _raster(tmp_path / "cover.tif", cover, grid)
    impervious_path = _raster(tmp_path / "impervious.tif", impervious, grid)
    canopy_path = _raster(tmp_path / "canopy.tif", canopy, grid)
    report_path = tmp_path / "qc" / "land_cover_coverage.json"

    with pytest.raises(ValueError, match="No automatic fill was performed"):
        prepare_nlcd(
            cover_path,
            impervious_path,
            canopy_path,
            grid,
            np.ones(grid.node_shape, dtype=bool),
            tmp_path / "rift_inputs",
            coverage_report_path=report_path,
        )

    with rasterio.open(canopy_path) as source:
        unchanged = source.read(1)
    assert unchanged[0, 0] == 254.0
    assert unchanged[2, 2] == 255.0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["source_semantic_nodata"]["canopy"]["codes"] == {
        "254": 1,
        "255": 1,
    }
    assert report["source_semantic_nodata"]["canopy"]["raw_product_modified"] is False
    assert report["variables"]["canopy"]["missing_active_nodes"] == 2

    prepare_nlcd(
        cover_path,
        impervious_path,
        canopy_path,
        grid,
        np.ones(grid.node_shape, dtype=bool),
        tmp_path / "rift_inputs",
        coverage_report_path=report_path,
        manual_fill_values={"canopy": 50.0},
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["manual_resolutions"]["canopy"]["filled_active_nodes"] == 2
