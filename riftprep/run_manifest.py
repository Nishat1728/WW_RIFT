"""Versioned, secret-safe provenance manifests for complete workflow attempts."""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Mapping

from .artifacts import redact_sensitive, sha256
from .config import ProjectPaths
from .spec import CaseSpec


RUN_MANIFEST_SCHEMA = "riftprep-run-provenance-v1"
RUN_MANIFEST_STATUSES = {"running", "raw_complete", "complete", "failed"}
_VERSIONED_PACKAGES = (
    "riftprep", "numpy", "pandas", "xarray", "rasterio", "geopandas",
    "pyproj", "shapely", "h5py", "watershed-workflow",
)
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|token|password|passwd|secret|authorization|cookie|credential)($|[_-])",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _relative(paths: ProjectPaths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _file_record(paths: ProjectPaths, path: Path) -> dict[str, Any]:
    record = {
        "path": str(path.resolve()),
        "case_relative_path": _relative(paths, path),
    }
    try:
        record.update(bytes=path.stat().st_size, sha256=sha256(path))
    except OSError as error:
        record.update(
            bytes=None,
            sha256=None,
            inventory_error=f"{type(error).__name__}: {error}",
            missing_metadata=["bytes", "sha256"],
        )
    return record


def _tree_inventory(paths: ProjectPaths, directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    return [
        _file_record(paths, path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def _package_versions() -> tuple[dict[str, str | None], list[str]]:
    versions: dict[str, str | None] = {}
    missing = []
    for name in _VERSIONED_PACKAGES:
        try:
            versions[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            versions[name] = None
            missing.append(f"environment.package_versions.{name}")
    return versions, missing


def _git_version() -> tuple[dict[str, Any], list[str]]:
    root = Path(__file__).resolve().parents[1]
    result: dict[str, Any] = {
        "repository_root": str(root),
        "commit": None,
        "dirty": None,
    }
    missing = []
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout
        result.update(commit=commit, dirty=bool(status.strip()))
    except (OSError, subprocess.SubprocessError):
        missing.extend(("code.git.commit", "code.git.dirty"))
    return result, missing


def _environment_snapshot() -> tuple[dict[str, Any], list[str]]:
    versions, missing = _package_versions()
    git, git_missing = _git_version()
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "package_versions": versions,
        "code": {"git": git},
    }, missing + git_missing


def _known_secret_values(value: Any, *, key: str | None = None) -> set[str]:
    if key is not None and _SENSITIVE_CONFIG_KEY.search(key):
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return set()
        text = str(value)
        return {text} if text else set()
    if isinstance(value, Mapping):
        secrets: set[str] = set()
        for item_key, item_value in value.items():
            secrets.update(_known_secret_values(item_value, key=str(item_key)))
        return secrets
    if isinstance(value, (list, tuple, set)):
        secrets = set()
        for item in value:
            secrets.update(_known_secret_values(item))
        return secrets
    return set()


def _redacted_failure_message(error: BaseException, spec: CaseSpec) -> str:
    message = str(redact_sensitive(str(error)))
    for secret in sorted(_known_secret_values(spec.as_dict()), key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return message


def _run_events(paths: ProjectPaths, started_utc: str) -> list[dict[str, Any]]:
    events = _read_json(paths.provenance / "acquisition_events.json", [])
    return [
        redact_sensitive(event)
        for event in events
        if str(event.get("acquisition_started_utc", "")) >= started_utc
    ]


def _stage_outcomes(paths: ProjectPaths, started_utc: str) -> list[dict[str, Any]]:
    rows = _read_json(paths.timings / "wall_clock.json", [])
    return [
        row for row in rows if str(row.get("started_utc", "")) >= started_utc
    ]


def _quality_control_references(paths: ProjectPaths) -> dict[str, dict[str, Any]]:
    references = {}
    candidates = {
        "final_qc": paths.quality_control / "qc_report.json",
        "input_status": paths.quality_control / "input_status_summary.json",
        "raw_download": paths.quality_control / "raw_download_complete.json",
        "dem_lineage": paths.quality_control / "dem_lineage.json",
        "land_cover_coverage": paths.quality_control / "land_cover_coverage.json",
        "soil_coverage": paths.quality_control / "soil_coverage.json",
        "rainfall_coverage": paths.quality_control / "rainfall_coverage.json",
        "initial_state_coverage": paths.quality_control / "initial_state_coverage.json",
        "spinup_baseflow": paths.quality_control / "spinup_baseflow.json",
        "directory_contract": paths.quality_control / "directory_contract.json",
    }
    for name, path in candidates.items():
        if not path.is_file():
            continue
        record = _file_record(paths, path)
        payload = _read_json(path, {})
        if isinstance(payload, Mapping):
            for key in ("schema", "status", "ok", "complete"):
                if key in payload:
                    record[key] = payload[key]
        references[name] = record
    input_qc = paths.quality_control / "input_qc"
    if input_qc.is_dir():
        for path in sorted(input_qc.glob("*.json")):
            references[f"input_qc.{path.stem}"] = _file_record(paths, path)
    return references


def _fallbacks(paths: ProjectPaths, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fallbacks: list[dict[str, Any]] = []
    if paths.uses_legacy_input_directory:
        fallbacks.append({
            "type": "legacy_simulation_ready_directory",
            "value": paths.simulation_ready.name,
            "automatic": True,
        })
    reused = sorted({
        str(event.get("product")) for event in events if event.get("status") == "reused"
    })
    if reused:
        fallbacks.append({
            "type": "cache_reuse",
            "products": reused,
            "automatic": False,
        })
    for filename, input_name in (
        ("land_cover_coverage.json", "land_cover"),
        ("soil_coverage.json", "soil"),
    ):
        payload = _read_json(paths.quality_control / filename, {})
        resolutions = payload.get("manual_resolutions", {}) if isinstance(payload, dict) else {}
        if resolutions:
            fallbacks.append({
                "type": "user_supplied_missing_value_resolution",
                "input": input_name,
                "details": resolutions,
                "automatic": False,
            })
    rainfall = _read_json(paths.quality_control / "rainfall_coverage.json", {})
    if isinstance(rainfall, dict) and rainfall.get("automatic_zero_replacement_performed"):
        fallbacks.append({
            "type": "rainfall_missing_to_zero",
            "input": "rainfall",
            "cells": int(rainfall.get("missing_active_cells", 0)),
            "automatic": True,
        })
    return redact_sensitive(fallbacks)


def _warnings(paths: ProjectPaths) -> list[str]:
    warnings: list[str] = []
    final_qc = _read_json(paths.quality_control / "qc_report.json", {})
    if isinstance(final_qc, dict):
        warnings.extend(str(item) for item in final_qc.get("warnings", []))
    for path in sorted((paths.quality_control / "input_qc").glob("*.json")):
        payload = _read_json(path, {})
        if not isinstance(payload, dict):
            continue
        if payload.get("status") not in {None, "PASSED"}:
            warnings.append(
                f"{payload.get('input_name', path.stem)}: {payload.get('message', payload.get('status'))}"
            )
    return list(dict.fromkeys(warnings))


def validate_run_manifest(payload: Mapping[str, Any]) -> None:
    """Validate the stable required surface without adding a JSON-schema dependency."""
    required = {
        "schema", "run", "configuration", "environment", "directory_contract",
        "acquisition", "artifacts", "stages", "fallbacks", "warnings",
        "quality_control", "failure", "missing_metadata",
    }
    missing = required - set(payload)
    if missing:
        raise ValueError(f"Run manifest is missing fields: {sorted(missing)}")
    if payload["schema"] != RUN_MANIFEST_SCHEMA:
        raise ValueError(f"Unsupported run manifest schema: {payload['schema']!r}")
    run = payload["run"]
    if run.get("status") not in RUN_MANIFEST_STATUSES:
        raise ValueError(f"Invalid run status: {run.get('status')!r}")
    if not run.get("id") or not run.get("started_utc") or not run.get("case_name"):
        raise ValueError("Run identity, start time, and case name are required.")
    for area in ("raw", "processed", "simulation_ready"):
        if area not in payload["artifacts"] or not isinstance(payload["artifacts"][area], list):
            raise ValueError(f"Artifact inventory {area!r} must be a list.")
    if run["status"] == "failed" and not payload["failure"]:
        raise ValueError("Failed runs require a failure record.")
    if run["status"] != "failed" and payload["failure"] is not None:
        raise ValueError("Only failed runs may contain a failure record.")


@dataclass
class RunManifestWriter:
    spec: CaseSpec
    paths: ProjectPaths
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_utc: str = field(default_factory=_utc_now)
    preexisting_case: bool = False
    resumed_from_run_id: str | None = None
    _environment: dict[str, Any] = field(init=False, repr=False)
    _missing_metadata: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.history_directory.mkdir(parents=True, exist_ok=True)
        self._environment, self._missing_metadata = _environment_snapshot()
        if self.spec.cache.mode == "reuse" and self.resumed_from_run_id is None:
            previous = _read_json(self.latest_path, {})
            previous_id = previous.get("run", {}).get("id") if isinstance(previous, dict) else None
            if previous_id and previous_id != self.run_id:
                self.resumed_from_run_id = str(previous_id)
            elif self.preexisting_case:
                self._missing_metadata.append("run.resumed_from_run_id")
        self.write(status="running", phase="initialization")

    @property
    def latest_path(self) -> Path:
        return self.paths.provenance / "run_manifest.json"

    @property
    def history_directory(self) -> Path:
        return self.paths.provenance / "run_manifests"

    @property
    def history_path(self) -> Path:
        return self.history_directory / f"{self.run_id}.json"

    @classmethod
    def reopen(
        cls, spec: CaseSpec, paths: ProjectPaths, run_id: str, started_utc: str
    ) -> "RunManifestWriter":
        instance = cls.__new__(cls)
        instance.spec = spec
        instance.paths = paths
        instance.run_id = run_id
        instance.started_utc = started_utc
        previous = _read_json(paths.provenance / "run_manifests" / f"{run_id}.json", {})
        instance.preexisting_case = bool(previous.get("run", {}).get("resumed", False))
        instance.resumed_from_run_id = previous.get("run", {}).get("resumed_from_run_id")
        instance._environment = previous.get("environment") or _environment_snapshot()[0]
        instance._missing_metadata = list(previous.get("missing_metadata", []))
        return instance

    def _raw_artifacts(self) -> list[dict[str, Any]]:
        records = _read_json(self.paths.provenance / "raw_products.json", [])
        return redact_sensitive(records if isinstance(records, list) else [])

    def _root_outputs(self) -> list[dict[str, Any]]:
        names = ("rift_case.cfg", "rift_spinup.cfg", "simulation_plan.json", "RUN.md", "case_manifest.json")
        return [
            _file_record(self.paths, self.paths.root / name)
            for name in names if (self.paths.root / name).is_file()
        ]

    def payload(
        self,
        *,
        status: str,
        phase: str,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        if status not in RUN_MANIFEST_STATUSES:
            raise ValueError(status)
        terminal = status in {"complete", "failed"}
        events = _run_events(self.paths, self.started_utc)
        acquisition_path = self.paths.provenance / "acquisition_events.json"
        acquisition_reference = (
            _file_record(self.paths, acquisition_path)
            if acquisition_path.is_file() else None
        )
        include_derived = status in {"complete", "failed"}
        artifacts = {
            "raw": self._raw_artifacts(),
            "processed": (
                _tree_inventory(self.paths, self.paths.processed)
                if include_derived else []
            ),
            "simulation_ready": (
                _tree_inventory(self.paths, self.paths.simulation_ready)
                if include_derived else []
            ),
            "root_outputs": self._root_outputs() if include_derived else [],
        }
        payload = {
            "schema": RUN_MANIFEST_SCHEMA,
            "run": {
                "id": self.run_id,
                "case_name": self.spec.case_name,
                "status": status,
                "phase": phase,
                "started_utc": self.started_utc,
                "completed_utc": _utc_now() if terminal else None,
                "cache_mode": self.spec.cache.mode,
                "resumed": bool(
                    self.spec.cache.mode == "reuse" and self.preexisting_case
                ),
                "resumed_from_run_id": self.resumed_from_run_id,
            },
            "configuration": {
                "schema": "riftprep-case-configuration-v1",
                "sanitized": redact_sensitive(self.spec.as_dict()),
                "resolved_case": _relative(
                    self.paths, self.paths.quality_control / "resolved_case.yaml"
                ),
            },
            "environment": self._environment,
            "directory_contract": self.paths.directory_contract(),
            "acquisition": {
                "events_reference": acquisition_reference,
                "events_for_run": events,
            },
            "artifacts": artifacts,
            "stages": _stage_outcomes(self.paths, self.started_utc),
            "fallbacks": _fallbacks(self.paths, events),
            "warnings": _warnings(self.paths),
            "quality_control": _quality_control_references(self.paths),
            "failure": (
                {
                    "type": type(error).__name__,
                    "message": _redacted_failure_message(error, self.spec),
                    "phase": phase,
                }
                if error is not None else None
            ),
            "missing_metadata": sorted(set(self._missing_metadata)),
        }
        return redact_sensitive(payload)

    def write(
        self,
        *,
        status: str,
        phase: str,
        error: BaseException | None = None,
    ) -> Path:
        payload = self.payload(status=status, phase=phase, error=error)
        validate_run_manifest(payload)
        text = json.dumps(payload, indent=2) + "\n"
        for destination in (self.history_path, self.latest_path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(text, encoding="utf-8", newline="\n")
            temporary.replace(destination)
        return self.history_path

    def raw_complete(self) -> Path:
        return self.write(status="raw_complete", phase="raw_download_complete")

    def complete(self) -> Path:
        return self.write(status="complete", phase="workflow_complete")

    def fail(self, error: BaseException, phase: str | None = None) -> Path:
        outcomes = _stage_outcomes(self.paths, self.started_utc)
        failed_stage = next(
            (row.get("stage") for row in reversed(outcomes) if row.get("status") == "failed"),
            None,
        )
        return self.write(
            status="failed",
            phase=phase or str(failed_stage or "workflow"),
            error=error,
        )
