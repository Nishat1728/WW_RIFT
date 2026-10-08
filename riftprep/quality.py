from __future__ import annotations

"""Quality-control and provenance helpers for RIFT input preparation."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import CRS


class QualityControlError(RuntimeError):
    """Raised when a critical input-quality gate fails."""


def setup_workflow_logging(
    log_directory: str | Path,
    *,
    name: str = "riftprep",
    level: str = "INFO",
) -> tuple[logging.Logger, Path]:
    """Configure notebook and file logging, using Watershed Workflow if present."""
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    logfile = directory / "preprocessing.log"
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    try:
        from watershed_workflow.io import setupLogging as ww_setup_logging

        verbosity = 2 if numeric_level <= logging.DEBUG else 1
        # Watershed Workflow 1.x rejects strings here but accepts a Path-like
        # filename; newer releases may accept either.  Keep the Path object.
        ww_setup_logging(verbosity, logfile=logfile)
    except Exception:
        root = logging.getLogger()
        root.setLevel(numeric_level)
        resolved = logfile.resolve()
        has_file = any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == resolved
            for handler in root.handlers
        )
        if not has_file:
            handler = logging.FileHandler(logfile, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            root.addHandler(handler)
    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)
    # Keep the scientific log focused on workflow decisions and QC evidence.
    for noisy_name in ("fontTools", "matplotlib", "PIL", "pyogrio"):
        logging.getLogger(noisy_name).setLevel(logging.WARNING)
    logger.info("Logging initialized: %s", logfile)
    return logger, logfile


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (tuple, set)):
        return list(value)
    return value


@dataclass
class QCRecord:
    timestamp_utc: str
    module: str
    check: str
    status: str
    severity: str
    value: Any = None
    criterion: str = ""
    source: str = ""
    message: str = ""


class QCRecorder:
    """Accumulate, display, save, and enforce quality-control checks."""

    columns = [
        "timestamp_utc", "module", "check", "status", "severity", "value",
        "criterion", "source", "message",
    ]

    def __init__(self, output_directory: str | Path, logger: logging.Logger | None = None):
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("riftprep.qc")
        self.records: list[QCRecord] = []

    def check(
        self,
        module: str,
        check: str,
        condition: bool,
        *,
        value: Any = None,
        criterion: str = "",
        severity: str = "critical",
        source: str | Path = "",
        message: str = "",
    ) -> bool:
        severity = severity.lower()
        if severity not in {"critical", "warning", "information"}:
            raise ValueError(f"Unknown QC severity: {severity}")
        status = "PASS" if bool(condition) else ("FAIL" if severity == "critical" else "WARNING")
        self.records.append(
            QCRecord(
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                module=str(module), check=str(check), status=status,
                severity=severity.upper(), value=_json_value(value),
                criterion=str(criterion), source=str(source), message=str(message),
            )
        )
        log_method = self.logger.info if status == "PASS" else (
            self.logger.error if status == "FAIL" else self.logger.warning
        )
        log_method("QC %s | %s | %s | value=%s | %s", status, module, check, value, message)
        self.save()
        return bool(condition)

    def dataframe(self, module: str | None = None) -> pd.DataFrame:
        frame = pd.DataFrame([asdict(record) for record in self.records], columns=self.columns)
        if module is not None and not frame.empty:
            frame = frame.loc[frame["module"] == module]
        return frame.reset_index(drop=True)

    def save(self) -> tuple[Path, Path]:
        frame = self.dataframe()
        csv_path = self.output_directory / "qc_report.csv"
        json_path = self.output_directory / "qc_report.json"
        frame.to_csv(csv_path, index=False)
        payload = {
            "status": "FAIL" if (not frame.empty and (frame.status == "FAIL").any()) else "PASS",
            "records": frame.to_dict(orient="records"),
        }
        json_path.write_text(json.dumps(payload, indent=2, default=_json_value), encoding="utf-8")
        return csv_path, json_path

    def gate(self, module: str | None = None) -> None:
        frame = self.dataframe(module)
        failed = frame.loc[frame.status == "FAIL"] if not frame.empty else frame
        if not failed.empty:
            details = "; ".join(f"{row.module}: {row.check}" for row in failed.itertuples())
            raise QualityControlError(f"Critical quality-control checks failed: {details}")

    def style(self, module: str | None = None):
        frame = self.dataframe(module)
        if frame.empty:
            return frame
        colors = {
            "PASS": "background-color:#d9f0e3",
            "WARNING": "background-color:#fff1c7",
            "FAIL": "background-color:#f8d7da",
        }
        styler = frame.style
        color_status = lambda value: colors.get(value, "")
        if hasattr(styler, "map"):
            return styler.map(color_status, subset=["status"])
        return styler.applymap(color_status, subset=["status"])


def stage_vector_dataset(source: str | Path, destination_directory: str | Path) -> Path:
    """Copy a vector dataset, including every ESRI Shapefile sidecar."""
    import shutil

    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".shp":
        members = sorted(source.parent.glob(source.stem + ".*"))
        required = {".shp", ".shx", ".dbf"}
        available = {member.suffix.lower() for member in members}
        missing = required - available
        if missing:
            raise QualityControlError(f"Shapefile is missing required sidecars: {sorted(missing)}")
        for member in members:
            shutil.copy2(member, destination / member.name)
        return destination / source.name
    target = destination / source.name
    shutil.copy2(source, target)
    return target


def load_user_domain(
    path: str | Path,
    *,
    layer: str | None = None,
    crs_override: str | int | None = None,
    canonical_path: str | Path | None = None,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Read, validate, repair, dissolve, and optionally save a user domain."""
    source = Path(path)
    data = gpd.read_file(source, layer=layer)
    if data.empty:
        raise QualityControlError(f"User domain contains no features: {source}")
    source_feature_count = len(data)
    if data.crs is None:
        if crs_override is None:
            raise QualityControlError(
                "The user-supplied domain has no CRS. Supply SHAPEFILE_CRS_OVERRIDE "
                "as an EPSG code or WKT string; coordinates will not be guessed."
            )
        data = data.set_crs(CRS.from_user_input(crs_override), allow_override=True)
    elif crs_override is not None and CRS.from_user_input(data.crs) != CRS.from_user_input(crs_override):
        raise QualityControlError(
            f"Source declares {data.crs}, but SHAPEFILE_CRS_OVERRIDE={crs_override}. "
            "Remove the override or correct the source projection."
        )
    data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
    if data.empty:
        raise QualityControlError("Every user-domain geometry is null or empty.")
    invalid_before = int((~data.geometry.is_valid).sum())
    if invalid_before:
        try:
            data.geometry = data.geometry.make_valid()
        except AttributeError:
            data.geometry = data.geometry.buffer(0)
    data = data.explode(index_parts=False, ignore_index=True)
    data = data.loc[data.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if data.empty or (~data.geometry.is_valid).any():
        raise QualityControlError("User domain could not be repaired to valid polygon geometry.")
    union = data.geometry.union_all() if hasattr(data.geometry, "union_all") else data.geometry.unary_union
    dissolved = gpd.GeoDataFrame({"domain_id": [source.stem]}, geometry=[union], crs=data.crs)
    metadata = {
        "source": str(source),
        "source_format": source.suffix.lower(),
        "source_feature_count": source_feature_count,
        "invalid_geometries_repaired": invalid_before,
        "source_crs": str(dissolved.crs),
        "source_crs_wkt": CRS.from_user_input(dissolved.crs).to_wkt(),
        "bounds_wgs84": [float(value) for value in dissolved.to_crs(4326).total_bounds],
    }
    if canonical_path is not None:
        canonical_path = Path(canonical_path)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        dissolved.to_file(canonical_path, driver="GPKG")
        metadata["canonical_path"] = str(canonical_path)
    return dissolved, metadata


def file_inventory(paths: Iterable[str | Path]) -> pd.DataFrame:
    rows = []
    for item in paths:
        path = Path(item)
        rows.append(
            {
                "path": str(path),
                "exists": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else None,
                "sha256": sha256_file(path) if path.is_file() else None,
            }
        )
    return pd.DataFrame(rows)
