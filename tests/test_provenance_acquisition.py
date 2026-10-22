import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from riftprep.artifacts import ProvenanceLedger, prepare_case_directory
from riftprep.config import DomainSpec
from riftprep.managers import RawProduct, acquire_raw_product
from riftprep.spec import CachePolicy, CaseSpec, ProductSpec


@contextmanager
def _stage(_name):
    yield


def _spec(tmp_path: Path, options: dict, *, cache_mode: str = "reuse") -> CaseSpec:
    return CaseSpec(
        case_name="provenance_case",
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.8, 41.7, -72.7, 41.8)
        ),
        dem=ProductSpec(provider="mock_provider", options=options),
        rainfall=ProductSpec(enabled=False, provider="none"),
        cache=CachePolicy(mode=cache_mode),
    )


def _boundary() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["test"]}, geometry=[box(-72.8, 41.7, -72.7, 41.8)],
        crs="EPSG:4326",
    )


def test_success_and_cached_reuse_preserve_metadata_and_original_timestamp(
    monkeypatch, tmp_path: Path
):
    spec = _spec(
        tmp_path,
        {
            "variable": "elevation",
            "api_key": "must-not-appear",
            "endpoint": "https://provider.test/data?token=must-not-appear",
        },
    )
    paths = prepare_case_directory(spec.case_root, "reuse")
    ledger = ProvenanceLedger(paths.provenance)

    def fake_fetch(product, boundary, paths, ledger, plot, dpi, reuse_existing):
        artifact = paths.raw / "dem" / "provider.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if not artifact.exists():
            artifact.write_bytes(b"provider-native-response")
        ledger.register("dem", product.provider, artifact, product.options)
        return RawProduct(
            "dem", product.provider, {"response": artifact},
            {
                "dataset": "mock-dem-v1",
                "endpoint": "https://provider.test/data?token=must-not-appear",
            },
        )

    monkeypatch.setattr("riftprep.managers.fetch_dem", fake_fetch)
    first = acquire_raw_product(
        "dem", spec, paths, ledger, _boundary(), _stage
    )
    assert first.files["response"].is_file()
    first_record = ledger.records[0]
    first_started = first_record.acquisition_started_utc
    first_completed = first_record.acquisition_completed_utc

    acquire_raw_product("dem", spec, paths, ledger, _boundary(), _stage)

    assert [event.status for event in ledger.events] == ["succeeded", "reused"]
    record = ledger.records[0]
    assert record.schema == "riftprep-raw-artifact-v2"
    assert record.sha256 == hashlib.sha256(b"provider-native-response").hexdigest()
    assert record.provider_metadata["dataset"] == "mock-dem-v1"
    assert record.provider_metadata["endpoint"].endswith("token=%5BREDACTED%5D")
    assert record.query_parameters["selected_options"]["api_key"] == "[REDACTED]"
    assert record.query_parameters["selected_options"]["endpoint"].endswith(
        "token=%5BREDACTED%5D"
    )
    assert record.acquisition_started_utc == first_started
    assert record.acquisition_completed_utc == first_completed
    assert record.spatial_reference["status"] == "not_applicable"
    assert "must-not-appear" not in (
        paths.provenance / "raw_products.json"
    ).read_text(encoding="utf-8")
    assert "must-not-appear" not in (
        paths.provenance / "acquisition_events.json"
    ).read_text(encoding="utf-8")


def test_failed_partial_response_records_checksum_and_redacts_error(
    monkeypatch, tmp_path: Path
):
    spec = _spec(tmp_path, {"password": "hidden-password"})
    paths = prepare_case_directory(spec.case_root, "reuse")
    ledger = ProvenanceLedger(paths.provenance)

    def failing_fetch(product, boundary, paths, ledger, plot, dpi, reuse_existing):
        partial = paths.raw / "dem" / "partial-response.bin"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"partial-provider-body")
        raise RuntimeError(
            "provider failed at https://provider.test/data?token=hidden-token"
        )

    monkeypatch.setattr("riftprep.managers.fetch_dem", failing_fetch)
    with pytest.raises(RuntimeError, match="provider failed"):
        acquire_raw_product("dem", spec, paths, ledger, _boundary(), _stage)

    assert len(ledger.events) == 1
    event = ledger.events[0]
    assert event.status == "failed"
    assert event.error_type == "RuntimeError"
    assert "hidden-token" not in event.error_message
    assert "[REDACTED]" in event.error_message
    assert event.query_parameters["selected_options"]["password"] == "[REDACTED]"
    assert event.missing_metadata == ("provider_metadata",)
    assert len(event.partial_artifacts) == 1
    partial = event.partial_artifacts[0]
    assert partial["bytes"] == len(b"partial-provider-body")
    assert partial["sha256"] == hashlib.sha256(b"partial-provider-body").hexdigest()
    assert ledger.records == []


def test_v1_raw_product_ledger_loads_without_inventing_metadata(tmp_path: Path):
    provenance = tmp_path / "quality_control" / "provenance"
    provenance.mkdir(parents=True)
    artifact = tmp_path / "raw" / "dem" / "legacy.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"legacy")
    (provenance / "raw_products.json").write_text(json.dumps([{
        "product": "dem",
        "provider": "legacy",
        "path": str(artifact.resolve()),
        "bytes": artifact.stat().st_size,
        "sha256": hashlib.sha256(b"legacy").hexdigest(),
        "source_url": None,
        "selected_options": {},
        "created_utc": "2026-01-01T00:00:00+00:00",
    }]), encoding="utf-8")

    ledger = ProvenanceLedger(provenance)

    assert len(ledger.records) == 1
    record = ledger.records[0]
    assert record.schema == "riftprep-raw-artifact-v1"
    assert record.provider_metadata == {}
    assert record.query_parameters == {}
    assert record.acquisition_started_utc is None
    assert record.acquisition_completed_utc is None
    assert record.spatial_reference == {"status": "unavailable", "value": None}
    assert set(record.missing_metadata) == {
        "provider_metadata", "query_parameters", "acquisition_started_utc",
        "acquisition_completed_utc", "crs",
    }
