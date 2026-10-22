import hashlib
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import xarray as xr
from rasterio.transform import from_origin
from shapely.geometry import box

from riftprep.artifacts import ProvenanceLedger, prepare_case_directory
from riftprep.config import DomainSpec, ProjectPaths
from riftprep.managers import acquire_raw_product
from riftprep.spec import CachePolicy, CaseSpec, PlotPolicy, ProductSpec


@contextmanager
def _stage(_name):
    yield


def _boundary() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["test"]},
        geometry=[box(-72.80, 41.70, -72.75, 41.75)],
        crs="EPSG:4326",
    )


def _dataset() -> xr.Dataset:
    return xr.Dataset(
        {
            "dem": (
                ("y", "x"),
                np.arange(16, dtype=np.float32).reshape(4, 4),
            )
        },
        coords={
            "x": 1_000_000.0 + np.arange(4) * 30.0,
            "y": 1_000_090.0 - np.arange(4) * 30.0,
        },
        attrs={"crs": "EPSG:5070"},
    )


def _spec(tmp_path: Path, *, cache_mode: str = "reuse") -> CaseSpec:
    return CaseSpec(
        case_name="dem_preservation",
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.80, 41.70, -72.75, 41.75)
        ),
        dem=ProductSpec(
            provider="watershed_workflow_3dep",
            options={"resolution_m": 30, "tile_degrees": 1.0},
        ),
        rainfall=ProductSpec(enabled=False, provider="none"),
        cache=CachePolicy(mode=cache_mode),
        plots=PlotPolicy(raw_products=False, processed_products=False),
    )


def test_3dep_fresh_mode_saves_provider_response_before_derivation(
    monkeypatch, tmp_path: Path
):
    class FakeManager:
        def __init__(self, resolution):
            assert resolution == 30

        def getDataset(self, geometry, variables):
            return _dataset()

    monkeypatch.setattr(
        "riftprep.managers._ww_sources",
        lambda: (FakeManager, None, None, None, lambda _path: None),
    )
    spec = _spec(tmp_path, cache_mode="fresh")
    paths = prepare_case_directory(spec.case_root, "fresh")
    ledger = ProvenanceLedger(paths.provenance)

    result = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )

    assert result.files["provider_response"].is_relative_to(paths.raw)
    assert result.files["raster"].is_relative_to(paths.processed)
    assert ledger.events[-1].cache_mode == "fresh"
    assert ledger.events[-1].status == "succeeded"
    assert ledger.events[-1].artifact_paths == (
        str(result.files["provider_response"].resolve()),
    )


def test_3dep_fresh_response_is_raw_and_cache_reuse_is_immutable(
    monkeypatch, tmp_path: Path
):
    calls = []

    class FakeManager:
        def __init__(self, resolution):
            assert resolution == 30

        def getDataset(self, geometry, variables):
            calls.append((tuple(geometry.total_bounds), tuple(variables)))
            return _dataset()

    monkeypatch.setattr(
        "riftprep.managers._ww_sources",
        lambda: (FakeManager, None, None, None, lambda _path: None),
    )
    spec = _spec(tmp_path)
    paths = prepare_case_directory(spec.case_root, "reuse")
    ledger = ProvenanceLedger(paths.provenance)

    first = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )
    raw_response = first.files["provider_response"]
    derived_raster = first.files["raster"]
    raw_bytes = raw_response.read_bytes()
    raw_mtime = raw_response.stat().st_mtime_ns

    assert raw_response == paths.raw / "dem" / "3dep_30m_provider_response.nc"
    assert derived_raster.is_relative_to(paths.processed / "dem")
    assert not list((paths.raw / "dem").rglob("*.tif"))
    assert "native" not in raw_response.name
    assert "native" not in derived_raster.name
    assert len(calls) == 1

    record = next(item for item in ledger.records if item.path == str(raw_response.resolve()))
    assert record.schema == "riftprep-raw-artifact-v2"
    assert record.bytes == len(raw_bytes)
    assert record.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert record.source_url.endswith("/WMSServer")
    assert record.acquisition_started_utc
    assert record.acquisition_completed_utc
    assert record.spatial_reference["status"] == "known"
    assert record.provider_metadata["raw_product_modified"] is False
    assert "py3dep" in record.provider_metadata["provider_processing_disclosure"]

    second = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )

    assert second.files["provider_response"] == raw_response
    assert raw_response.read_bytes() == raw_bytes
    assert raw_response.stat().st_mtime_ns == raw_mtime
    assert len(calls) == 1
    assert [event.status for event in ledger.events] == ["succeeded", "reused"]
    reused_record = next(
        item for item in ledger.records if item.path == str(raw_response.resolve())
    )
    assert reused_record.acquisition_started_utc == record.acquisition_started_utc
    assert reused_record.acquisition_completed_utc == record.acquisition_completed_utc


def test_3dep_tiled_acquisition_separates_responses_conversions_and_mosaic(
    monkeypatch, tmp_path: Path
):
    calls = []

    class FakeManager:
        def __init__(self, resolution):
            assert resolution == 30

        def getDataset(self, geometry, variables):
            calls.append(tuple(geometry.total_bounds))
            return _dataset()

    monkeypatch.setattr(
        "riftprep.managers._ww_sources",
        lambda: (FakeManager, None, None, None, lambda _path: None),
    )
    spec = CaseSpec(
        case_name="tiled_dem",
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.80, 41.70, -72.75, 41.75)
        ),
        dem=ProductSpec(
            provider="watershed_workflow_3dep",
            options={"resolution_m": 30, "tile_degrees": 0.04},
        ),
        rainfall=ProductSpec(enabled=False, provider="none"),
        cache=CachePolicy(mode="fresh"),
        plots=PlotPolicy(raw_products=False, processed_products=False),
    )
    paths = prepare_case_directory(spec.case_root, "fresh")
    ledger = ProvenanceLedger(paths.provenance)

    result = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )

    responses = sorted((paths.raw / "dem" / "provider_responses").glob("*.nc"))
    converted_tiles = sorted(
        (paths.processed / "dem" / "watershed_workflow_3dep" / "source_tiles").glob(
            "*.tif"
        )
    )
    assert len(calls) == len(responses) == len(converted_tiles) == 4
    assert not list((paths.raw / "dem").rglob("*.tif"))
    assert result.files["raster"].name == "3dep_30m_source_mosaic.tif"
    assert result.files["raster"].is_relative_to(paths.processed)
    assert all("provider_response" in path.name for path in responses)
    assert all(record.path.endswith(".nc") for record in ledger.records)
    assert all(record.schema == "riftprep-raw-artifact-v2" for record in ledger.records)


def test_validated_legacy_3dep_paths_are_reused_without_migration(
    monkeypatch, tmp_path: Path
):
    case_root = tmp_path / "legacy_case"
    (case_root / "rift_inputs").mkdir(parents=True)
    paths = ProjectPaths.create(case_root)
    raw_dir = paths.raw / "dem"
    raw_dir.mkdir(parents=True)
    legacy_nc = raw_dir / "3dep_30m_native.nc"
    _dataset().to_netcdf(legacy_nc)
    legacy_tif = raw_dir / "3dep_30m_native.tif"
    with rasterio.open(
        legacy_tif,
        "w",
        driver="GTiff",
        height=4,
        width=4,
        count=1,
        dtype="float32",
        crs="EPSG:5070",
        transform=from_origin(999_985, 1_000_105, 30, 30),
        nodata=-9999,
    ) as target:
        target.write(np.arange(16, dtype=np.float32).reshape(4, 4), 1)
    ledger = ProvenanceLedger(paths.provenance)
    spec = CaseSpec(
        case_name="legacy_case",
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.80, 41.70, -72.75, 41.75)
        ),
        dem=ProductSpec(
            provider="watershed_workflow_3dep",
            options={"resolution_m": 30, "tile_degrees": 1.0},
        ),
        rainfall=ProductSpec(enabled=False, provider="none"),
        cache=CachePolicy(mode="reuse"),
        plots=PlotPolicy(raw_products=False, processed_products=False),
    )
    monkeypatch.setattr(
        "riftprep.managers._ww_sources",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )

    product = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )

    assert paths.uses_legacy_input_directory is True
    assert product.files == {"netcdf": legacy_nc, "raster": legacy_tif}
    assert not (case_root / "simulation_ready").exists()
    assert not (paths.processed / "dem").exists()
    assert ledger.events[-1].status == "reused"
