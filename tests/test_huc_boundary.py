import pytest

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

    def fake_get(url, params, timeout):
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
