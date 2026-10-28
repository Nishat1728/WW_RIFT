import json
from types import SimpleNamespace

import pytest
import requests

from riftprep.service_health import ProviderServiceError, check_wms_service


VALID = b'<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer><CRS>EPSG:4326</CRS></Layer></Capability></WMS_Capabilities>'


def response(status=200, body=VALID):
    return SimpleNamespace(status_code=status, content=body, headers={"content-type": "text/xml"})


def test_wms_outage_retries_same_source_and_preserves_exact_evidence(monkeypatch, tmp_path):
    responses = iter([response(503, b'<html><body>Service Unavailable</body></html>'), response()])
    calls, sleeps = [], []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)
    monkeypatch.setattr("riftprep.service_health.requests.get", get)
    monkeypatch.setattr("riftprep.service_health.time.sleep", sleeps.append)
    result = check_wms_service("https://provider.test/wms", tmp_path, max_attempts=2, timeout=7)
    assert result["status"] == "available"
    assert [item["http_status"] for item in result["attempts"]] == [503, 200]
    assert calls[0][0] == calls[1][0] == "https://provider.test/wms"
    assert calls[0][1]["timeout"] == 7
    assert calls[0][1]["headers"]["Cache-Control"] == "no-cache"
    assert sleeps == [3]
    assert (tmp_path / result["attempts"][0]["body"]).read_bytes() == b'<html><body>Service Unavailable</body></html>'
    assert result["substituted_product"] is False


@pytest.mark.parametrize("status,body", [(503, b'<html/>'), (200, b'<html/>'), (200, b'not xml')])
def test_bad_metadata_is_provider_error_not_invalid_crs(monkeypatch, tmp_path, status, body):
    monkeypatch.setattr("riftprep.service_health.requests.get", lambda *a, **k: response(status, body))
    with pytest.raises(ProviderServiceError, match="not evidence of an invalid user CRS"):
        check_wms_service("https://provider.test/wms", tmp_path, max_attempts=1)
    report = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert report["status"] == "unavailable"
    assert report["attempts"][0]["http_status"] == status
    assert report["cache_reuse"] is False


def test_missing_endpoint_is_not_retried(monkeypatch, tmp_path):
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return response(404, b'Not Found')
    monkeypatch.setattr("riftprep.service_health.requests.get", get)
    with pytest.raises(ProviderServiceError, match="404"):
        check_wms_service("https://provider.test/wms", tmp_path, max_attempts=5)
    assert len(calls) == 1


def test_timeout_is_bounded_and_reported(monkeypatch, tmp_path):
    def get(*args, **kwargs):
        raise requests.Timeout("test timeout")
    monkeypatch.setattr("riftprep.service_health.requests.get", get)
    with pytest.raises(ProviderServiceError, match="Timeout"):
        check_wms_service("https://provider.test/wms", tmp_path, max_attempts=1)
