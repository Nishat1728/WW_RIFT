import json

import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer

from riftprep.config import GridSpec, ProjectPaths
from riftprep.managers import (
    RawCatalog,
    RawProduct,
    _load_completed_waterdata_download,
)
from riftprep.spec import ProductSpec
from riftprep.workflow import (
    _find_nwis_series,
    _prepare_coastal,
    _prepare_drainage,
    _prepare_inflows,
)


def _modern_product(tmp_path, name, site, parameter, unit, values):
    raw_dir = tmp_path / "raw" / name
    raw_dir.mkdir(parents=True)
    location = raw_dir / f"waterdata_location_{site}.json"
    location.write_text(json.dumps({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-72.6093, 42.19147]},
        "properties": {"monitoring_location_id": f"USGS-{site}"},
    }), encoding="utf-8")
    observations = raw_dir / f"waterdata_continuous_{site}_{parameter}_page_001.json"
    start = pd.Timestamp("2023-06-10T00:00:00Z")
    observations.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-72.6093, 42.19147]},
                "properties": {
                    "monitoring_location_id": f"USGS-{site}",
                    "parameter_code": parameter,
                    "time_series_id": f"USGS-{site}-{parameter}-test",
                    "time": (start + pd.Timedelta(hours=index)).isoformat().replace("+00:00", "Z"),
                    "value": str(value),
                    "unit_of_measure": unit,
                },
            }
            for index, value in enumerate(values)
        ],
    }), encoding="utf-8")
    return RawProduct(name, "usgs_waterdata", {
        f"location_{site}": location,
        f"observations_continuous_{site}_{parameter}_001": observations,
    })


def _grid_at_station():
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform(
        -72.6093, 42.19147
    )
    return GridSpec(x - 100, y - 100, x + 100, y + 100, 100, "EPSG:32618", 2, 2)


def test_modern_waterdata_inflow_is_converted_and_located(tmp_path):
    paths = ProjectPaths.create(tmp_path / "case")
    site = "01172010"
    raw = RawCatalog(None, {
        "inflows": _modern_product(tmp_path, "inflows", site, "00060", "ft^3/s", range(7))
    })
    product = ProductSpec(provider="usgs_waterdata", options={
        "start_utc": "2023-06-10T00:00:00Z",
        "end_utc": "2023-06-10T06:00:00Z",
        "stations": [{"id": "river", "site": site, "parameter_code": "00060"}],
    })

    table, plotted = _prepare_inflows(product, raw, _grid_at_station(), paths)

    assert table.is_file()
    hydrograph = np.loadtxt(paths.inputs / "hydrographs" / "river_si_m3s.csv", delimiter=",")
    assert hydrograph.shape == (7, 2)
    assert hydrograph[-1, 1] == pytest.approx(6 * 0.028316846592)
    assert plotted["river"][0][-1] == 21600


def test_modern_waterdata_stage_and_synthetic_drainage(tmp_path):
    paths = ProjectPaths.create(tmp_path / "case")
    site = "01194796"
    raw = RawCatalog(None, {
        "coastal": _modern_product(tmp_path, "coastal", site, "00065", "ft", [5] * 7)
    })
    product = ProductSpec(provider="usgs_waterdata", options={
        "start_utc": "2023-06-10T00:00:00Z",
        "end_utc": "2023-06-10T06:00:00Z",
        "stations": [{"site": site, "parameter_code": "00065"}],
        "coastal_radius_m": 500,
    })
    grid = _grid_at_station()
    node_mask = np.ones(grid.node_shape, dtype=bool)
    dem = np.zeros(grid.node_shape)

    sampled, stage_path = _prepare_coastal(product, raw, grid, paths, dem, node_mask)

    assert stage_path.is_file()
    assert sampled.stage_m_navd88.iloc[0] == pytest.approx(1.524)
    assert len(list((paths.inputs / "surge").glob("surge-*.txt"))) == 7

    impervious = np.array([[90, 10, 90], [10, 90, 10], [90, 10, 90]], dtype=float)
    drainage = ProductSpec(provider="synthetic_nlcd", options={
        "impervious_threshold_pct": 80,
    })
    drainage_map, table, map_path, table_path = _prepare_drainage(
        drainage, RawCatalog(None, {}), grid, node_mask, impervious, paths
    )
    assert drainage_map.sum() == 5
    assert set(table["type"]) == {0, 1}
    assert map_path.is_file() and table_path.is_file()


def test_modern_waterdata_requires_explicit_series_when_ambiguous(tmp_path):
    product = _modern_product(tmp_path, "inflows", "01172010", "00060", "ft^3/s", [1, 2])
    path = product.files["observations_continuous_01172010_00060_001"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["features"][0])
    duplicate["properties"] = dict(duplicate["properties"], time_series_id="second-series")
    payload["features"].append(duplicate)
    path.write_text(json.dumps(payload), encoding="utf-8")
    raw = RawCatalog(None, {"inflows": product})

    with pytest.raises(ValueError, match="multiple time_series_id"):
        _find_nwis_series(raw, "inflows", "01172010", "00060")
    selected = _find_nwis_series(
        raw, "inflows", "01172010", "00060", "second-series"
    )
    assert selected["time_series_id"] == "second-series"


def test_waterdata_reuse_requires_complete_matching_request(tmp_path):
    request = [{
        "site": "USGS-01172010",
        "parameter_code": "00060",
        "collection": "continuous",
    }]
    location = tmp_path / "waterdata_location_01172010.json"
    observations = tmp_path / "waterdata_continuous_01172010_00060_page_001.json"
    location.write_text("{}", encoding="utf-8")
    observations.write_text('{"features": []}', encoding="utf-8")
    manifest = tmp_path / "waterdata_download_complete.json"
    manifest.write_text(json.dumps({
        "requests": request,
        "start_utc": "2023-06-10T00:00:00Z",
        "end_utc": "2023-06-10T06:00:00Z",
        "files": [location.name, observations.name],
    }), encoding="utf-8")

    reused = _load_completed_waterdata_download(
        manifest, tmp_path, request,
        "2023-06-10T00:00:00Z", "2023-06-10T06:00:00Z",
    )
    assert reused["location_01172010"] == location
    assert reused["observations_continuous_01172010_00060_001"] == observations
    assert _load_completed_waterdata_download(
        manifest, tmp_path, request,
        "2023-06-11T00:00:00Z", "2023-06-11T06:00:00Z",
    ) is None
