import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
import yaml
import pytest
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


@pytest.mark.parametrize("two_stage", [False, True])
def test_complete_local_raw_first_workflow(tmp_path: Path, two_stage):
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
    if two_stage:
        config["spinup"] = {
            "enabled": True, "start_utc": "2023-02-10T00:00:00Z",
            "end_utc": "2023-06-10T00:00:00Z", "provider": "manual_constant",
            "stations": [{"id": "inlet", "constant_m3_s": 0.2,
                          "support": {"x": 1_000_100, "y": 1_000_150, "crs": "EPSG:5070"}}],
        }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    builder = RIFTInputBuilder(config_path)
    builder.load_domain()
    for name in builder.selected_products:
        builder.download(name)
    downloaded = builder.finish_raw_downloads()
    assert set(downloaded.catalog.products) == {
        "dem", "initial_state", "hydrography", "land_cover", "soil", "rainfall"
    } | ({"spinup_baseflow"} if two_stage else set())
    assert downloaded.summary_path.is_file()
    assert not (downloaded.paths.simulation_ready / "dem.asc").exists()
    assert (downloaded.paths.raw / "dem" / dem.name).is_file()
    assert (downloaded.paths.raw / "rainfall" / rain_path.name).is_file()
    result = build_rift_inputs(downloaded)
    assert result.ok
    assert (result.case_root / "processed" / "rainfall_selected_event.nc").is_file()
    assert (
        result.case_root / "processed" / "dem" / "dem_aligned_to_rift_grid.asc"
    ).is_file()
    assert (result.case_root / "simulation_ready" / "dem.asc").is_file()
    assert (result.case_root / "simulation_ready" / "rainfall_ats.h5").is_file()
    assert not (result.case_root / "rift_inputs").exists()
    assert (result.case_root / "rift_case.cfg").is_file()
    config_text = (result.case_root / "rift_case.cfg").read_text(encoding="utf-8")
    assert "simulation_ready/dem.asc" in config_text
    assert "simulation_ready/rainfall_ats.h5" in config_text
    if two_stage:
        spinup_text = result.spinup_config_path.read_text()
        assert "modules.rainfall = 0" in spinup_text
        assert "rainfall_ats.h5" not in spinup_text
        assert "modules.inflows = 1" in spinup_text
        assert "output.interval_s = 86400" in spinup_text
        assert "output.hotstart = 1" in spinup_text
        assert "output.hotstart = 1" in config_text
        assert "simulation_ready/spinup_water_depth.asc" in config_text
        assert not (result.case_root / "simulation_ready/spinup_water_depth.asc").exists()
        baseflow = json.loads((result.case_root / "quality_control/spinup_baseflow.json").read_text())
        assert baseflow["stations"][0]["method"] == "explicit_user_constant"
        assert "simulate-stages" in (result.case_root / "RUN.md").read_text()
    raw_catalog = result.raw_catalog_path.read_text(encoding="utf-8")
    assert '"product": "dem"' in raw_catalog
    assert '"product": "rainfall"' in raw_catalog
    assert '"sha256"' in raw_catalog
    raw_root = (result.case_root / "raw").resolve()
    for artifact in json.loads(raw_catalog):
        assert Path(artifact["path"]).resolve().is_relative_to(raw_root)
        assert artifact["schema"] == "riftprep-raw-artifact-v2"
        assert artifact["provider_metadata"]
        assert artifact["query_parameters"]
        assert artifact["acquisition_started_utc"]
        assert artifact["acquisition_completed_utc"]
        assert artifact["spatial_reference"]["status"] in {
            "known", "not_applicable", "unavailable"
        }
        assert len(artifact["sha256"]) == 64

    acquisition_events = json.loads(
        downloaded.acquisition_events_path.read_text(encoding="utf-8")
    )
    assert len(acquisition_events) == 7 + int(two_stage)  # Boundary + enabled inputs.
    assert {event["status"] for event in acquisition_events} == {"succeeded"}
    assert all(event["query_parameters"] for event in acquisition_events)
    assert all(event["acquisition_started_utc"] for event in acquisition_events)
    assert all(event["acquisition_completed_utc"] for event in acquisition_events)

    dem_lineage = json.loads(
        (result.case_root / "quality_control" / "dem_lineage.json").read_text(
            encoding="utf-8"
        )
    )
    assert dem_lineage["provider"] == "local"
    assert dem_lineage["source_raster_is_raw"] is True
    assert Path(
        dem_lineage["derived_intermediates"]["aligned_to_rift_grid"]
    ).is_relative_to((result.case_root / "processed").resolve())
    assert Path(dem_lineage["simulation_ready_dem"]).is_relative_to(
        (result.case_root / "simulation_ready").resolve()
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["directory_contract"]["raw"]["path"] == "raw"
    assert manifest["directory_contract"]["processed"]["path"] == "processed"
    assert (
        manifest["directory_contract"]["simulation_ready"]["path"]
        == "simulation_ready"
    )

    run_manifest = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
    assert run_manifest["schema"] == "riftprep-run-provenance-v1"
    assert run_manifest["run"]["status"] == "complete"
    assert run_manifest["run"]["cache_mode"] == "fresh"
    assert run_manifest["failure"] is None
    assert len(run_manifest["artifacts"]["raw"]) == len(json.loads(raw_catalog))
    assert all(
        len(item["sha256"]) == 64
        for area in ("processed", "simulation_ready", "root_outputs")
        for item in run_manifest["artifacts"][area]
    )
    assert run_manifest["quality_control"]["final_qc"]["ok"] is True
    assert run_manifest["acquisition"]["events_for_run"]
    assert any(
        stage["stage"] == "process_dem" and stage["status"] == "ok"
        for stage in run_manifest["stages"]
    )
    assert run_manifest["environment"]["python"]["version"]
    assert "riftprep" in run_manifest["environment"]["package_versions"]
    history = result.run_manifest_path.parent / "run_manifests"
    assert len(list(history.glob("*.json"))) == 1
