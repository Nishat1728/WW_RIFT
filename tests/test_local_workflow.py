from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
import yaml
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from riftprep.workflow import RIFTInputBuilder, build_rift_inputs


def _raster(path: Path, values: np.ndarray, dtype: str = "float32") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype=dtype, crs="EPSG:5070", transform=from_origin(999950, 1000350, 100, 100),
        nodata=-9999,
    ) as target:
        target.write(values.astype(dtype), 1)
    return path


def test_complete_local_raw_first_workflow(tmp_path: Path):
    source = tmp_path / "source"
    shape = (4, 5)
    dem = _raster(source / "dem.tif", 50 + np.arange(20).reshape(shape) / 10)
    cover = _raster(source / "cover.tif", np.full(shape, 41), "int16")
    impervious = _raster(source / "impervious.tif", np.full(shape, 15))
    canopy = _raster(source / "canopy.tif", np.full(shape, 60))

    soil = gpd.GeoDataFrame({
        "permeability [m^2]": [1e-12], "porosity [-]": [0.42],
        "residual saturation [-]": [0.08], "van Genuchten alpha [Pa^-1]": [2e-4],
    }, geometry=[box(1_000_000, 1_000_000, 1_000_400, 1_000_300)], crs="EPSG:5070")
    soil_path = source / "soil.gpkg"
    soil.to_file(soil_path, driver="GPKG")
    rivers = gpd.GeoDataFrame(
        geometry=[LineString([(1_000_000, 1_000_150), (1_000_400, 1_000_150)])],
        crs="EPSG:5070",
    )
    river_path = source / "rivers.gpkg"
    rivers.to_file(river_path, driver="GPKG")

    times = pd.date_range("2023-06-10T01:00:00", periods=3, freq="1h")
    rain = xr.Dataset({
        "APCP_surface": (("time", "y", "x"), np.ones((3, 4, 5), dtype=np.float32)),
    }, coords={
        "time": times,
        "x": 1_000_000 + np.arange(5) * 100,
        "y": 1_000_300 - np.arange(4) * 100,
    })
    rain_path = source / "rain.nc"
    rain.to_netcdf(rain_path)

    config = {
        "case_name": "local_complete",
        "output_root": str(tmp_path / "cases"),
        "domain": {
            "mode": "rectangle", "bounds": [1_000_000, 1_000_000, 1_000_400, 1_000_300],
            "bounds_crs": "EPSG:5070", "target_crs": "EPSG:5070", "cell_size_m": 100,
            "download_buffer_cells": 0, "outside_dem_mode": "real_terrain",
        },
        "dem": {"provider": "local", "options": {"path": str(dem)}},
        "hydrography": {"provider": "local", "options": {"path": str(river_path)}},
        "land_cover": {"provider": "local", "options": {"rasters": {
            "cover": str(cover), "impervious": str(impervious), "canopy": str(canopy),
        }}},
        "soil": {"provider": "local", "options": {"path": str(soil_path)}},
        "rainfall": {"provider": "local", "options": {
            "path": str(rain_path), "start_utc": "2023-06-10T00:00:00Z",
            "end_utc": "2023-06-10T03:00:00Z",
        }},
        "inflows": {"enabled": False, "provider": "none"},
        "coastal": {"enabled": False, "provider": "none"},
        "drainage": {"enabled": False, "provider": "none"},
        "cache": {"mode": "fresh"},
        "plots": {"raw_products": False, "processed_products": False},
        "simulation": {"end_time_s": 10800},
    }
    config_path = tmp_path / "case.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    builder = RIFTInputBuilder(config_path)
    builder.load_domain()
    for name in builder.selected_products:
        builder.download(name)
    downloaded = builder.finish_raw_downloads()
    assert set(downloaded.catalog.products) == {
        "dem", "initial_state", "hydrography", "land_cover", "soil", "rainfall"
    }
    assert downloaded.summary_path.is_file()
    assert not (downloaded.paths.inputs / "dem.asc").exists()
    result = build_rift_inputs(downloaded)
    assert result.ok
    assert (result.case_root / "rift_inputs" / "dem.asc").is_file()
    assert (result.case_root / "rift_inputs" / "rainfall_ats.h5").is_file()
    assert (result.case_root / "rift_case.cfg").is_file()
    raw_catalog = result.raw_catalog_path.read_text(encoding="utf-8")
    assert '"product": "dem"' in raw_catalog
    assert '"product": "rainfall"' in raw_catalog
    assert '"sha256"' in raw_catalog
