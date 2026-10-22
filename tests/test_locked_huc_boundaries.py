from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon, mapping

from hpc.validate_locked_huc_boundaries import (
    PROJECT_CRS,
    WBD_FEATURE_SERVER,
    fetch_boundary,
    locked_selection_digest,
    request_fingerprint,
    selected_cases,
    sha256_file,
    validate_boundary,
    validate_selection_bundle,
)


def test_locked_repository_selection_bundle_is_complete_and_immutable() -> None:
    selection = Path("validation/huc_matrix_selection_v2/selection_manifest.json")
    manifest, cases, catalog, all_codes = validate_selection_bundle(selection)
    assert manifest["fixed_seed"] == 20261014
    assert len(cases) == len(catalog) == 22
    assert {level: sum(row["level"] == level for row in cases) for level in (2, 4, 8, 12)} == {
        2: 2,
        4: 4,
        8: 6,
        12: 10,
    }
    assert all(all_codes[level] for level in (2, 4, 8, 12))
    assert locked_selection_digest(cases) == locked_selection_digest(selected_cases(manifest))


def test_request_fingerprint_is_order_independent() -> None:
    url = "https://example.test/query"
    assert request_fingerprint(url, {"where": "1=1", "f": "geojson"}) == request_fingerprint(
        url, {"f": "geojson", "where": "1=1"}
    )


def test_boundary_cache_reuse_requires_matching_fingerprint_and_checksum(
    tmp_path, monkeypatch
) -> None:
    code = "010100010101"
    level = 12
    raw_path = (
        tmp_path / "raw" / "huc_boundaries" / "huc12" / code / "raw_boundary.geojson"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    url = f"{WBD_FEATURE_SERVER}/{level // 2}/query"
    params = {
        "where": f"huc{level}='{code}'",
        "outFields": "*",
        "returnGeometry": "true",
        "returnZ": "false",
        "returnM": "false",
        "outSR": "4326",
        "orderByFields": f"huc{level} ASC,objectid ASC",
        "resultRecordCount": "2",
        "f": "geojson",
    }
    provenance_path = raw_path.with_name("provenance.json")
    provenance_path.write_text(json.dumps({
        "request_fingerprint_sha256": request_fingerprint(url, params),
        "response_sha256": sha256_file(raw_path),
        "attempts": [{"attempt": 1}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        "hpc.validate_locked_huc_boundaries.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    result = fetch_boundary({"level": level, "huc": code}, tmp_path, "reuse")
    assert result["reused"] is True
    assert result["raw_path"] == raw_path


def test_boundary_validation_checks_identity_crs_area_and_checksums(tmp_path) -> None:
    code = "010100010101"
    polygon = Polygon([
        (-72.8, 41.4),
        (-72.7, 41.4),
        (-72.7, 41.5),
        (-72.8, 41.5),
        (-72.8, 41.4),
    ])
    projected_area = (
        gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs(PROJECT_CRS).area.iloc[0]
        / 1_000_000.0
    )
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [{
            "type": "Feature",
            "properties": {
                "objectid": 1,
                "huc12": code,
                "tnmid": "{LOCKED-TNMID}",
                "areasqkm": projected_area,
                "states": "CT",
                "name": "Fixture",
            },
            "geometry": mapping(polygon),
        }],
    }
    raw_path = tmp_path / "raw" / "huc_boundaries" / "huc12" / code / "raw_boundary.geojson"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps(payload), encoding="utf-8")
    provenance_path = raw_path.with_name("provenance.json")
    provenance_path.write_text(
        json.dumps({"response_sha256": sha256_file(raw_path)}), encoding="utf-8"
    )
    case = {
        "level": 12,
        "huc": code,
        "name": "Fixture",
        "states": ["CT"],
        "object_id": 1,
        "tnmid": "{LOCKED-TNMID}",
    }
    fetched = {
        "case": case,
        "raw_path": raw_path,
        "provenance_path": provenance_path,
        "reused": False,
        "attempts": [],
    }
    catalog = {
        "level": 12,
        "huc": code,
        "object_id": 1,
        "tnmid": "{LOCKED-TNMID}",
        "area_sqkm": projected_area,
    }
    all_codes = {
        2: {code[:2]},
        4: {code[:4]},
        8: {code[:8]},
        12: {code},
    }
    result = validate_boundary(fetched, tmp_path, catalog, all_codes)
    assert result["status"] == "PASSED"
    assert all(result["checks"].values())
    assert result["geometry"]["source_crs"] == "EPSG:4326"
    assert result["geometry"]["project_crs"] == "EPSG:5070"
    assert result["artifacts"]["raw_product_modified"] is False
    assert result["artifacts"]["processed_boundary_sha256"]
