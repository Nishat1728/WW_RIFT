from __future__ import annotations

import json

import pytest
import requests

from riftprep.managers import _requests_post_timeout


ENDPOINT = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"
QUERY = (
    "SELECT @aoiGeom = GEOMETRY::STGeomFromText("
    "'POLYGON ((-66.228 45.271, -66.228 45.295, -66.238 45.295, "
    "-66.238 45.271, -66.228 45.271))', 4326);"
)


def _response(body: bytes, content_type: str = "application/json") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = ENDPOINT
    response.reason = "OK"
    response.headers["Content-Type"] = content_type
    response._content = body
    return response


def test_empty_body_is_preserved_before_decode(monkeypatch, tmp_path):
    seen = {}

    def fake_post(*args, **kwargs):
        seen.update(kwargs)
        return _response(b"")

    monkeypatch.setattr(requests, "post", fake_post)
    with _requests_post_timeout(17, tmp_path):
        response = requests.post(
            ENDPOINT, data={"FORMAT": "JSON", "QUERY": QUERY}
        )
        with pytest.raises(requests.exceptions.JSONDecodeError):
            response.json()

    metadata_path = next(tmp_path.glob("*_metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert seen["timeout"] == 17
    assert metadata["anomaly"] == "invalid_json"
    assert metadata["http_status"] == 200
    assert metadata["response_bytes"] == 0
    assert metadata["raw_body_saved_before_json_decode"] is True
    assert metadata["geometry_wkt_epsg4326"].startswith("POLYGON")
    assert next(tmp_path.glob("*_body.bin")).read_bytes() == b""


def test_malformed_json_is_preserved_with_decode_error_metadata(monkeypatch, tmp_path):
    body = b'{"Table": '
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: _response(body))

    with _requests_post_timeout(17, tmp_path):
        response = requests.post(
            ENDPOINT, data={"FORMAT": "JSON", "QUERY": QUERY}
        )
        with pytest.raises(requests.exceptions.JSONDecodeError):
            response.json()

    metadata = json.loads(
        next(tmp_path.glob("*_metadata.json")).read_text(encoding="utf-8")
    )
    assert metadata["anomaly"] == "invalid_json"
    assert metadata["http_status"] == 200
    assert metadata["content_type"] == "application/json"
    assert metadata["response_bytes"] == len(body)
    assert metadata["error_type"] == "JSONDecodeError"
    assert metadata["raw_body_saved_before_json_decode"] is True
    assert next(tmp_path.glob("*_body.bin")).read_bytes() == body


def test_non_json_provider_response_is_preserved_with_content_type(monkeypatch, tmp_path):
    body = b"<html><body>Service temporarily unavailable</body></html>"
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: _response(body, "text/html; charset=utf-8"),
    )

    with _requests_post_timeout(17, tmp_path):
        response = requests.post(
            ENDPOINT, data={"FORMAT": "JSON", "QUERY": QUERY}
        )
        with pytest.raises(requests.exceptions.JSONDecodeError):
            response.json()

    metadata = json.loads(
        next(tmp_path.glob("*_metadata.json")).read_text(encoding="utf-8")
    )
    assert metadata["anomaly"] == "invalid_json"
    assert metadata["http_status"] == 200
    assert metadata["reason"] == "OK"
    assert metadata["content_type"] == "text/html; charset=utf-8"
    assert metadata["response_bytes"] == len(body)
    assert metadata["body_preview_utf8"] == body.decode("utf-8")
    assert metadata["raw_body_saved_before_json_decode"] is True
    assert next(tmp_path.glob("*_body.bin")).read_bytes() == body


def test_missing_table_response_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: _response(b"{}"))
    with _requests_post_timeout(17, tmp_path):
        response = requests.post(
            ENDPOINT, data={"FORMAT": "JSON", "QUERY": QUERY}
        )
        assert response.json() == {}

    metadata = json.loads(
        next(tmp_path.glob("*_metadata.json")).read_text(encoding="utf-8")
    )
    assert metadata["anomaly"] == "missing_table"
    assert metadata["response_bytes"] == 2
    assert metadata["body_preview_utf8"] == "{}"
    assert next(tmp_path.glob("*_body.bin")).read_bytes() == b"{}"


def test_valid_table_response_leaves_no_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(
        requests, "post", lambda *args, **kwargs: _response(b'{"Table": []}')
    )
    with _requests_post_timeout(17, tmp_path):
        response = requests.post(
            ENDPOINT, data={"FORMAT": "JSON", "QUERY": QUERY}
        )
        assert response.json() == {"Table": []}

    assert not list(tmp_path.iterdir())
