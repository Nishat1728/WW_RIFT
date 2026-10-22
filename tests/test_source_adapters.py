import gzip
import sys
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
import xarray as xr
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import box

import riftprep.source_adapters as adapters


def _boundary():
    return gpd.GeoDataFrame(
        {"name": ["test"]}, geometry=[box(-72.8, 41.7, -72.7, 41.8)], crs="EPSG:4326"
    )


def _geotiff_bytes(values, transform=None, crs="EPSG:4326", dtype="float32"):
    values = np.asarray(values, dtype=dtype)
    transform = transform or from_origin(-73.0, 42.0, 0.25, 0.25)
    profile = {
        "driver": "GTiff", "height": values.shape[0], "width": values.shape[1],
        "count": 1, "dtype": str(values.dtype), "crs": crs,
        "transform": transform, "nodata": -9999, "compress": "deflate",
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as output:
            output.write(values, 1)
        return memory.read()


def test_copernicus_glo30_preserves_tile_and_builds_mosaic(monkeypatch, tmp_path):
    raw_bytes = _geotiff_bytes(np.arange(16, dtype=np.float32).reshape(4, 4))
    calls = []

    def fake_download(url, destination, **kwargs):
        calls.append(url)
        Path(destination).write_bytes(raw_bytes)
        return Path(destination)

    monkeypatch.setattr(adapters, "_download", fake_download)
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    files, metadata = adapters.download_copernicus_glo30(
        _boundary(), raw_dir, processed_dir=processed_dir
    )
    assert files["source_tile_0001"].read_bytes() == raw_bytes
    assert files["source_tile_0001"].is_relative_to(raw_dir)
    assert files["raster"].is_relative_to(processed_dir)
    assert files["raster"].is_file()
    assert files["raster"].name == "copernicus_glo30_source_mosaic.tif"
    assert "native" not in files["raster"].name
    assert metadata["source_tile_count"] == 1
    assert metadata["raw_source_tiles_modified"] is False
    assert metadata["mosaic_is_derived"] is True

    raw_mtime = files["source_tile_0001"].stat().st_mtime_ns
    cached_files, _ = adapters.download_copernicus_glo30(
        _boundary(), raw_dir, processed_dir=processed_dir, reuse_existing=True
    )
    assert len(calls) == 1
    assert cached_files["source_tile_0001"].read_bytes() == raw_bytes
    assert cached_files["source_tile_0001"].stat().st_mtime_ns == raw_mtime


def test_daymet_daily_total_is_explicitly_distributed_hourly(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "_earthdata_session", requests.Session)

    def fake_subset(boundary, year_start, year_end, year, region, destination, session):
        dataset = xr.Dataset(
            {"prcp": (("time", "y", "x"), np.full((1, 2, 2), 24.0, dtype=np.float32))},
            coords={
                "time": [np.datetime64("2020-01-01T12:00:00")],
                "x": xr.DataArray([0.0, 1.0], dims="x", attrs={"units": "km"}),
                "y": xr.DataArray([1.0, 0.0], dims="y", attrs={"units": "km"}),
            },
        )
        dataset.to_netcdf(destination)
        return "EPSG:5070"

    monkeypatch.setattr(adapters, "_download_daymet_opendap_subset", fake_subset)
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    files, metadata = adapters.download_daymet_hourly(
        _boundary(),
        {
            "start_utc": "2020-01-01T00:00:00Z",
            "end_utc": "2020-01-02T00:00:00Z",
            "daily_distribution": "uniform_24h",
        },
        raw_dir,
        processed_dir=processed_dir,
    )
    with xr.open_dataset(files["netcdf"]) as result:
        assert result.sizes["time"] == 24
        np.testing.assert_allclose(result["APCP_surface"], 1.0)
        assert result.x.values.tolist() == [0.0, 1000.0]
    assert metadata["native_time_step"] == "daily"
    assert metadata["region"] == "na"
    assert files["provider_netcdf_2020"].is_file()
    assert files["provider_netcdf_2020"].is_relative_to(raw_dir)
    assert files["netcdf"].is_relative_to(processed_dir)
    with xr.open_dataset(files["provider_netcdf_2020"]) as raw:
        np.testing.assert_allclose(raw["prcp"], 24.0)


def test_mrms_archive_is_retained_and_standardized(monkeypatch, tmp_path):
    keys = [
        "CONUS/MultiSensor_QPE_01H_Pass2_00.00/20200101/"
        f"MRMS_MultiSensor_QPE_01H_Pass2_00.00_20200101-{hour:02d}0000.grib2.gz"
        for hour in (1, 2)
    ]
    monkeypatch.setattr(adapters, "_s3_keys", lambda prefix, **kwargs: keys)

    def fake_download(url, destination, **kwargs):
        value = 1.0 if "-010000" in url else 2.0
        payload = _geotiff_bytes(np.full((4, 4), value, dtype=np.float32))
        with gzip.open(destination, "wb") as output:
            output.write(payload)
        return Path(destination)

    monkeypatch.setattr(adapters, "_download", fake_download)
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    files, metadata = adapters.download_mrms_hourly(
        _boundary(),
        {
            "start_utc": "2020-01-01T00:00:00Z",
            "end_utc": "2020-01-01T02:00:00Z",
            "product": "MultiSensor_QPE_01H_Pass2_00.00",
            "region": "CONUS",
        },
        raw_dir,
        processed_dir=processed_dir,
    )
    with xr.open_dataset(files["netcdf"]) as result:
        assert result.sizes["time"] == 2
        np.testing.assert_allclose(result["APCP_surface"].mean(("x", "y")), [1.0, 2.0])
    assert len(list((raw_dir / "provider_grib2_gz").glob("*.gz"))) == 2
    assert files["netcdf"].is_relative_to(processed_dir)
    assert len(list((processed_dir / "decoded_grib2").glob("*.grib2"))) == 2
    assert metadata["time_records"] == 2


def test_era5_land_splits_months_and_deaccumulates(monkeypatch, tmp_path):
    requests = []

    class FakeResult:
        def __init__(self, request):
            self.request = request

        def download(self, destination):
            year = int(self.request["year"])
            month = int(self.request["month"])
            days = [int(value) for value in self.request["day"]]
            times = pd.DatetimeIndex([
                pd.Timestamp(year=year, month=month, day=day, hour=hour)
                for day in days for hour in range(24)
            ])
            accumulation = np.array(
                [0.024 if time.hour == 0 else time.hour * 0.001 for time in times],
                dtype=np.float32,
            )[:, None, None]
            xr.Dataset(
                {"tp": (("valid_time", "latitude", "longitude"), accumulation)},
                coords={"valid_time": times, "latitude": [41.75], "longitude": [-72.75]},
            ).to_netcdf(destination)

    class FakeClient:
        def retrieve(self, name, request):
            requests.append(request)
            return FakeResult(request)

    monkeypatch.setitem(sys.modules, "cdsapi", SimpleNamespace(Client=FakeClient))
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    files, metadata = adapters.download_era5_land_hourly(
        _boundary(),
        {
            "start_utc": "2020-01-01T00:00:00Z",
            "end_utc": "2020-01-01T03:00:00Z",
        },
        raw_dir,
        processed_dir=processed_dir,
    )
    assert len(requests) == 2  # Previous day and event day cross a month boundary.
    assert "provider_netcdf_201912" in files
    assert "provider_netcdf_202001" in files
    assert files["provider_netcdf_201912"].is_relative_to(raw_dir)
    assert files["netcdf"].is_relative_to(processed_dir)
    with xr.open_dataset(files["netcdf"]) as result:
        np.testing.assert_allclose(result["APCP_surface"], 1.0, rtol=1e-5)
    assert metadata["deaccumulated"] is True


def test_soilgrids_retains_properties_and_derives_hydraulic_rasters(monkeypatch, tmp_path):
    values = {"sand": 400, "clay": 200, "bdod": 130, "wv1500": 100}

    def fake_download(url, destination, **kwargs):
        name = next(key for key in values if f"_{key}_" in Path(destination).name)
        Path(destination).write_bytes(
            _geotiff_bytes(np.full((3, 3), values[name], dtype=np.int16), dtype="int16")
        )
        return Path(destination)

    monkeypatch.setattr(adapters, "_download", fake_download)
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    files, metadata = adapters.download_soilgrids_hydraulic(
        _boundary(), {"depth": "0-5cm", "quantile": "Q0.5"}, raw_dir,
        processed_dir=processed_dir,
    )
    for key in ("provider_sand", "provider_clay", "provider_bdod", "provider_wv1500"):
        assert files[key].is_file()
        assert files[key].is_relative_to(raw_dir)
    for key in ("permeability", "porosity", "residual", "alpha"):
        assert files[key].is_relative_to(processed_dir)
        with rasterio.open(files[key]) as source:
            assert np.isfinite(source.read(1, masked=True)).all()
    with rasterio.open(files["porosity"]) as source:
        np.testing.assert_allclose(source.read(1), 1.0 - 1.3 / 2.65, rtol=1e-5)
    assert metadata["raw_native_modified"] is False
