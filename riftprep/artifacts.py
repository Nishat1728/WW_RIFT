"""Case lifecycle, immutable raw-product inventory, and wall-clock timings."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import ProjectPaths


CASE_MARKER = ".riftprep-case"
REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|token|password|passwd|secret|authorization|cookie|credential)($|[_-])",
    re.IGNORECASE,
)


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-safe copy with credentials and URL secrets removed."""
    if key is not None and _SENSITIVE_KEY.search(str(key)):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if re.search(r"\bBearer\s+\S+", value, flags=re.IGNORECASE):
            value = re.sub(
                r"\bBearer\s+\S+", f"Bearer {REDACTED}", value,
                flags=re.IGNORECASE,
            )
        value = re.sub(
            r"(?i)([?&](?:api[_-]?key|token|password|passwd|secret|authorization|cookie|credential)=[^&\s]*)",
            lambda match: match.group(0).split("=", 1)[0] + f"={REDACTED}",
            value,
        )
        try:
            parts = urlsplit(value)
        except ValueError:
            return value
        if parts.scheme and parts.netloc:
            query = [
                (name, REDACTED if _SENSITIVE_KEY.search(name) else item_value)
                for name, item_value in parse_qsl(parts.query, keep_blank_values=True)
            ]
            netloc = parts.hostname or ""
            try:
                port = parts.port
            except ValueError:
                port = None
            if port is not None:
                netloc = f"{netloc}:{port}"
            return urlunsplit(
                (parts.scheme, netloc, parts.path, urlencode(query), parts.fragment)
            )
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def prepare_case_directory(root: str | Path, cache_mode: str) -> ProjectPaths:
    """Create/reuse a case; fresh deletion is allowed only for marked case roots."""
    root = _resolved(root)
    if root.parent == root:
        raise ValueError("Refusing to use a filesystem root as a case directory.")
    marker = root / CASE_MARKER
    if root.exists() and cache_mode == "fresh":
        if not marker.is_file():
            raise RuntimeError(
                f"Fresh mode refuses to delete unmarked directory: {root}. "
                f"Choose a new case_name or add {CASE_MARKER} after auditing it."
            )
        shutil.rmtree(root)
    paths = ProjectPaths.create(root)
    marker.write_text(
        json.dumps({
            "schema": "riftprep-case-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    (paths.quality_control / "directory_contract.json").write_text(
        json.dumps(paths.directory_contract(), indent=2), encoding="utf-8"
    )
    return paths


def sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class RawArtifact:
    product: str
    provider: str
    path: str
    bytes: int
    sha256: str
    source_url: str | None
    selected_options: dict[str, Any]
    created_utc: str
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    query_parameters: dict[str, Any] = field(default_factory=dict)
    acquisition_started_utc: str | None = None
    acquisition_completed_utc: str | None = None
    spatial_reference: dict[str, Any] = field(
        default_factory=lambda: {"status": "unavailable", "value": None}
    )
    missing_metadata: tuple[str, ...] = ()
    schema: str = "riftprep-raw-artifact-v2"


@dataclass(frozen=True)
class AcquisitionEvent:
    product: str
    provider: str
    status: str
    cache_mode: str
    acquisition_started_utc: str
    acquisition_completed_utc: str
    query_parameters: dict[str, Any]
    provider_metadata: dict[str, Any]
    artifact_paths: tuple[str, ...]
    partial_artifacts: tuple[dict[str, Any], ...]
    error_type: str | None = None
    error_message: str | None = None
    missing_metadata: tuple[str, ...] = ()
    schema: str = "riftprep-acquisition-event-v1"


class ProvenanceLedger:
    def __init__(self, directory: str | Path):
        self.directory = _resolved(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        json_path = self.directory / "raw_products.json"
        self.records: list[RawArtifact] = []
        if json_path.is_file():
            for item in json.loads(json_path.read_text(encoding="utf-8")):
                payload = dict(item)
                payload["source_url"] = redact_sensitive(payload.get("source_url"))
                payload["selected_options"] = redact_sensitive(
                    payload.get("selected_options", {})
                )
                payload["provider_metadata"] = redact_sensitive(
                    payload.get("provider_metadata", {})
                )
                payload["query_parameters"] = redact_sensitive(
                    payload.get("query_parameters", {})
                )
                payload.setdefault(
                    "spatial_reference", {"status": "unavailable", "value": None}
                )
                payload.setdefault("schema", "riftprep-raw-artifact-v1")
                payload["missing_metadata"] = tuple(payload.get(
                    "missing_metadata",
                    (
                        "provider_metadata", "query_parameters",
                        "acquisition_started_utc", "acquisition_completed_utc",
                        "crs",
                    ),
                ))
                self.records.append(RawArtifact(**payload))
        events_path = self.directory / "acquisition_events.json"
        self.events: list[AcquisitionEvent] = []
        if events_path.is_file():
            for item in json.loads(events_path.read_text(encoding="utf-8")):
                payload = dict(item)
                payload["query_parameters"] = redact_sensitive(
                    payload.get("query_parameters", {})
                )
                payload["provider_metadata"] = redact_sensitive(
                    payload.get("provider_metadata", {})
                )
                payload["error_message"] = redact_sensitive(
                    payload.get("error_message")
                )
                for name in ("artifact_paths", "partial_artifacts", "missing_metadata"):
                    payload[name] = tuple(payload.get(name, ()))
                payload.setdefault("schema", "riftprep-acquisition-event-v1")
                self.events.append(AcquisitionEvent(**payload))

    def register(
        self,
        product: str,
        provider: str,
        path: str | Path,
        options: dict[str, Any] | None = None,
        source_url: str | None = None,
        spatial_reference: dict[str, Any] | None = None,
    ) -> RawArtifact:
        path = _resolved(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Cannot register missing/empty raw artifact: {path}")
        digest = sha256(path)
        for existing in self.records:
            if (
                existing.product == product
                and existing.provider == provider
                and existing.path == str(path)
                and existing.sha256 == digest
            ):
                return existing
        self.records = [
            existing for existing in self.records
            if not (
                existing.product == product
                and existing.provider == provider
                and existing.path == str(path)
            )
        ]
        record = RawArtifact(
            product=product,
            provider=provider,
            path=str(path),
            bytes=path.stat().st_size,
            sha256=digest,
            source_url=redact_sensitive(source_url),
            selected_options=redact_sensitive(options or {}),
            created_utc=datetime.now(timezone.utc).isoformat(),
            spatial_reference=spatial_reference or {
                "status": "unavailable", "value": None
            },
            missing_metadata=tuple(
                name for name, missing in (
                    ("source_url", source_url is None),
                    ("provider_metadata", True),
                    ("query_parameters", True),
                    ("acquisition_started_utc", True),
                    ("acquisition_completed_utc", True),
                    (
                        "crs",
                        (spatial_reference or {}).get("status")
                        in {None, "unavailable"},
                    ),
                ) if missing
            ),
        )
        self.records.append(record)
        self.write()
        return record

    def register_tree(
        self,
        product: str,
        provider: str,
        directory: str | Path,
        options: dict[str, Any] | None = None,
    ) -> list[RawArtifact]:
        return [
            self.register(product, provider, path, options)
            for path in sorted(_resolved(directory).rglob("*"))
            if path.is_file() and path.name != CASE_MARKER
        ]

    def write(self) -> None:
        json_path = self.directory / "raw_products.json"
        csv_path = self.directory / "raw_products.csv"
        payload = [asdict(record) for record in self.records]
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        fields = list(asdict(self.records[0]).keys()) if self.records else list(RawArtifact.__annotations__)
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in payload:
                row = dict(row)
                for name in (
                    "selected_options", "provider_metadata", "query_parameters",
                    "spatial_reference", "missing_metadata",
                ):
                    row[name] = json.dumps(row[name], sort_keys=True)
                writer.writerow(row)

    def finalize_acquisition(
        self,
        *,
        product: str,
        provider: str,
        files: Mapping[str, str | Path],
        raw_root: str | Path,
        cache_mode: str,
        status: str,
        started_utc: str,
        completed_utc: str,
        query_parameters: Mapping[str, Any] | None,
        provider_metadata: Mapping[str, Any] | None,
        spatial_references: Mapping[str, dict[str, Any]] | None = None,
        partial_artifacts: tuple[dict[str, Any], ...] = (),
        error: BaseException | None = None,
    ) -> AcquisitionEvent:
        """Finalize one successful, cached, failed, or partial acquisition."""
        raw_root = _resolved(raw_root)
        raw_files = {
            key: _resolved(path)
            for key, path in files.items()
            if _resolved(path).is_relative_to(raw_root)
        }
        safe_query = redact_sensitive(query_parameters or {})
        safe_metadata = redact_sensitive(provider_metadata or {})
        references = dict(spatial_references or {})
        raw_paths = {str(path) for path in raw_files.values()}
        updated: list[RawArtifact] = []
        for record in self.records:
            if (
                record.product == product
                and record.provider == provider
                and record.path in raw_paths
            ):
                key = next(
                    (name for name, path in raw_files.items() if str(path) == record.path),
                    None,
                )
                reference = references.get(
                    key or "", {"status": "unavailable", "value": None}
                )
                missing = []
                if not safe_metadata:
                    missing.append("provider_metadata")
                if not safe_query:
                    missing.append("query_parameters")
                if reference.get("status") == "unavailable":
                    missing.append("crs")
                if record.source_url is None:
                    missing.append("source_url")
                artifact_started = started_utc
                artifact_completed = completed_utc
                if status == "reused":
                    artifact_started = record.acquisition_started_utc
                    artifact_completed = record.acquisition_completed_utc
                if artifact_started is None:
                    missing.append("acquisition_started_utc")
                if artifact_completed is None:
                    missing.append("acquisition_completed_utc")
                updated.append(RawArtifact(
                    **{
                        **asdict(record),
                        "provider_metadata": safe_metadata,
                        "query_parameters": safe_query,
                        "acquisition_started_utc": artifact_started,
                        "acquisition_completed_utc": artifact_completed,
                        "spatial_reference": reference,
                        "missing_metadata": tuple(missing),
                        "schema": (
                            record.schema if status == "reused"
                            else "riftprep-raw-artifact-v2"
                        ),
                    }
                ))
            else:
                updated.append(record)
        self.records = updated
        self.write()

        missing = []
        if not safe_metadata:
            missing.append("provider_metadata")
        if not safe_query:
            missing.append("query_parameters")
        event = AcquisitionEvent(
            product=product,
            provider=provider,
            status=status,
            cache_mode=cache_mode,
            acquisition_started_utc=started_utc,
            acquisition_completed_utc=completed_utc,
            query_parameters=safe_query,
            provider_metadata=safe_metadata,
            artifact_paths=tuple(sorted(raw_paths)),
            partial_artifacts=tuple(redact_sensitive(partial_artifacts)),
            error_type=type(error).__name__ if error is not None else None,
            error_message=(
                redact_sensitive(str(error)) if error is not None else None
            ),
            missing_metadata=tuple(missing),
        )
        self.events.append(event)
        self.write_events()
        return event

    def write_events(self) -> None:
        rows = [asdict(item) for item in self.events]
        (self.directory / "acquisition_events.json").write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )
        fields = list(AcquisitionEvent.__annotations__)
        with (self.directory / "acquisition_events.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for payload in rows:
                row = dict(payload)
                for name in (
                    "query_parameters", "provider_metadata", "artifact_paths",
                    "partial_artifacts", "missing_metadata",
                ):
                    row[name] = json.dumps(row[name], sort_keys=True)
                writer.writerow(row)


@dataclass(frozen=True)
class TimingRecord:
    stage: str
    started_utc: str
    wall_seconds: float
    status: str


class TimingLedger:
    def __init__(self, directory: str | Path):
        self.directory = _resolved(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        json_path = self.directory / "wall_clock.json"
        self.records: list[TimingRecord] = (
            [TimingRecord(**item) for item in json.loads(json_path.read_text(encoding="utf-8"))]
            if json_path.is_file()
            else []
        )

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = datetime.now(timezone.utc).isoformat()
        tick = time.perf_counter()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.records.append(TimingRecord(name, started, time.perf_counter() - tick, status))
            self.write()

    def write(self) -> None:
        rows = [asdict(item) for item in self.records]
        (self.directory / "wall_clock.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        with (self.directory / "wall_clock.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(TimingRecord.__annotations__))
            writer.writeheader()
            writer.writerows(rows)
