import geopandas as gpd
import pytest

from riftprep.input_qc import check_raw_product
from riftprep.managers import RawProduct
from riftprep.quality import QualityControlError
from riftprep.workflow import _resolve_outlet_spec
from riftprep.config import OutletSpec


def _empty_hydrography(tmp_path, *, confirmed):
    path = tmp_path / "empty_flowlines.gpkg"
    gpd.GeoDataFrame(
        {"geometry": gpd.GeoSeries([], crs="EPSG:4326")},
        geometry="geometry",
        crs="EPSG:4326",
    ).to_file(path, driver="GPKG")
    return RawProduct(
        "hydrography",
        "usgs_3dhp",
        {"vector": path},
        {"features": 0, "tile_count": 1, "empty_domain_confirmed": confirmed},
    )


def test_provider_confirmed_empty_hydrography_passes_qc(tmp_path):
    product = _empty_hydrography(tmp_path, confirmed=True)
    result = check_raw_product(
        "hydrography", product, {}, tmp_path / "hydrography.json"
    )
    assert result.passed
    assert any(
        row["check"] == "hydrography: provider-confirmed empty domain"
        for row in result.checks
    )


def test_unconfirmed_empty_hydrography_still_fails_qc(tmp_path):
    product = _empty_hydrography(tmp_path, confirmed=False)
    with pytest.raises(QualityControlError, match="usable geometries"):
        check_raw_product(
            "hydrography", product, {}, tmp_path / "hydrography.json"
        )


def test_empty_hydrography_skips_automatic_outlet(tmp_path):
    outlet = OutletSpec(source="auto_hydrography_lowest")
    paths = type("Paths", (), {
        "quality_control": tmp_path,
    })()
    assert _resolve_outlet_spec(outlet, None, gpd.GeoDataFrame(
        {"geometry": gpd.GeoSeries([], crs="EPSG:5070")},
        geometry="geometry",
        crs="EPSG:5070",
    ), None, None, paths) is None
    payload = (tmp_path / "derived_outlet.json").read_text(encoding="utf-8")
    assert "NO_HYDROGRAPHY" in payload
