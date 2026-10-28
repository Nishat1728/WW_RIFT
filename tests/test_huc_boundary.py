import pytest
import requests

import riftprep.providers as providers


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _feature():
    return {
        "type": "Feature",
        "properties": {"huc4": "0108"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-73.0, 41.0], [-72.0, 41.0], [-72.0, 42.0],
                [-73.0, 42.0], [-73.0, 41.0],
            ]],
        },
    }


def test_get_huc_boundary_retries_empty_feature_response(monkeypatch):
    payloads = iter([
        {"type": "FeatureCollection", "features": []},
        {"type": "FeatureCollection", "features": [_feature()]},
    ])
    calls = []

    def fake_get(url, params, timeout, headers):
        calls.append((url, params, timeout))
        return _Response(next(payloads))

    monkeypatch.setattr(providers.requests, "get", fake_get)
    boundary = providers.get_huc_boundary(
        "0108", 4, max_attempts=2, retry_delay_seconds=0
    )

    assert len(calls) == 2
    assert len(boundary) == 1
    assert boundary.crs.to_epsg() == 4326


def test_get_huc_boundary_reports_exhausted_empty_retries(monkeypatch):
    monkeypatch.setattr(
        providers.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {"type": "FeatureCollection", "features": []}
        ),
    )

    with pytest.raises(LookupError, match="after 3 attempts"):
        providers.get_huc_boundary(
            "0108", 4, max_attempts=3, retry_delay_seconds=0
        )


def test_wbd_gateway_failure_uses_equivalent_fresh_exact_query(monkeypatch):
    calls = []
    def get(url, *, params, timeout, headers):
        calls.append(dict(params))
        assert headers["Cache-Control"] == "no-cache"
        if len(calls) == 1:
            raise requests.HTTPError("502 Bad Gateway")
        return _Response({"type": "FeatureCollection", "features": [_feature()]})
    monkeypatch.setattr(providers.requests, "get", get)
    assert len(providers.get_huc_boundary("0108", max_attempts=2, retry_delay_seconds=0)) == 1
    assert calls[0]["where"] == "huc4='0108'"
    assert calls[1]["where"] == "huc4 LIKE '0108'"


def test_wbd_wrong_watershed_is_rejected(monkeypatch):
    feature = _feature()
    feature["properties"]["huc4"] = "9999"
    monkeypatch.setattr(providers.requests, "get", lambda *a, **k: _Response(
        {"type": "FeatureCollection", "features": [feature]}))
    with pytest.raises(LookupError, match="identity mismatch"):
        providers.get_huc_boundary("0108", max_attempts=1)


def test_wbd_requires_numeric_code_before_any_query():
    with pytest.raises(ValueError, match="digits only"):
        providers.get_huc_boundary("01%8")
