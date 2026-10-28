"""Small, uncached provider checks with preserved failure evidence."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests


class ProviderServiceError(RuntimeError):
    """A provider could not supply valid service metadata (not a user CRS error)."""


def check_wms_service(
    url: str, directory: Path, *, max_attempts: int = 5, timeout: float = 30,
) -> dict:
    """Check HTTP status and WMS structure before third-party CRS parsing.

    Do not use the provider's HTTP cache or substitute a source. Keep the exact
    response body, a checksum and bounded attempt history for diagnosis.
    """
    if max_attempts < 1 or timeout <= 0:
        raise ValueError("Service attempts and timeout must be positive.")
    directory.mkdir(parents=True, exist_ok=True)
    attempts = []
    report_path = directory / f"wms_health_{time.time_ns()}.json"
    for attempt in range(1, max_attempts + 1):
        details = {"attempt": attempt, "url": url,
                   "checked_utc": datetime.now(timezone.utc).isoformat()}
        retryable = True
        try:
            response = requests.get(
                url, params={"service": "WMS", "request": "GetCapabilities", "version": "1.3.0"},
                headers={"Cache-Control": "no-cache"}, timeout=timeout,
            )
            body_path = directory / f"{report_path.stem}_attempt_{attempt}.response"
            body_path.write_bytes(response.content)
            details.update(http_status=response.status_code,
                           content_type=response.headers.get("content-type"),
                           bytes=len(response.content), body=body_path.name,
                           sha256=hashlib.sha256(response.content).hexdigest())
            try:
                root = ElementTree.fromstring(response.content)
                root_name = root.tag.rsplit("}", 1)[-1]
                crs = sorted({node.text.strip() for node in root.iter()
                              if node.tag.rsplit("}", 1)[-1] in {"CRS", "SRS"}
                              and node.text and node.text.strip()})
                valid = (response.status_code == 200
                         and root_name in {"WMS_Capabilities", "WMT_MS_Capabilities"}
                         and bool(crs))
                details.update(xml_root=root_name, advertised_crs=crs)
            except ElementTree.ParseError:
                valid = False
                details["xml_parse_error"] = True
            retryable = (response.status_code in {200, 408, 429}
                         or response.status_code >= 500)
        except requests.RequestException as error:
            valid = False
            details["error"] = f"{type(error).__name__}: {error}"
        attempts.append(details)
        report = {"schema": "riftprep-wms-health-v1", "status": "available" if valid else "unavailable",
                  "attempts": attempts, "cache_reuse": False, "substituted_product": False}
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if valid:
            return {**report, "diagnostic_path": str(report_path)}
        status = details.get("http_status", details.get("error", "invalid WMS metadata"))
        print(f"WMS service unavailable: {status}; attempt {attempt}/{max_attempts}. "
              f"Evidence: {report_path}", flush=True)
        if not retryable or attempt == max_attempts:
            raise ProviderServiceError(
                f"The selected NLCD/WMS provider is unavailable or returned invalid service metadata "
                f"({status}). Endpoint: {url}. This response cannot establish CRS support; "
                f"it is not evidence of an invalid user CRS. Diagnostic: {report_path}. "
                "Retry later or explicitly select verified local rasters. No cached data or "
                "alternative source was selected automatically."
            )
        time.sleep(min(30.0, 3.0 * 2 ** (attempt - 1)))
    raise AssertionError("unreachable")
