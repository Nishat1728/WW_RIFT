"""Case lifecycle, immutable raw-product inventory, and wall-clock timings."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ProjectPaths


CASE_MARKER = ".riftprep-case"


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


class ProvenanceLedger:
    def __init__(self, directory: str | Path):
        self.directory = _resolved(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        json_path = self.directory / "raw_products.json"
        self.records: list[RawArtifact] = (
            [RawArtifact(**item) for item in json.loads(json_path.read_text(encoding="utf-8"))]
            if json_path.is_file()
            else []
        )

    def register(
        self,
        product: str,
        provider: str,
        path: str | Path,
        options: dict[str, Any] | None = None,
        source_url: str | None = None,
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
            source_url=source_url,
            selected_options=options or {},
            created_utc=datetime.now(timezone.utc).isoformat(),
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
                row["selected_options"] = json.dumps(row["selected_options"], sort_keys=True)
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
