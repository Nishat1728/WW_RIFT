"""Uniform, user-facing quality checks for saved native input products."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr

from .quality import QualityControlError


QC_PASSED = "PASSED"
QC_NEEDS_USER_INPUT = "NEEDS USER INPUT"
QC_FAILED = "FAILED"


@dataclass
class InputQCResult:
    """Serializable result of one input's quality gate."""

    input_name: str
    provider: str
    status: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    accepted_paths: list[str] = field(default_factory=list)
    diagnostic_paths: list[str] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    checked_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def passed(self) -> bool:
        return self.status == QC_PASSED

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def _check(rows: list[dict[str, Any]], name: str, condition: bool, value: Any) -> None:
    rows.append({
        "check": name,
        "status": "PASS" if bool(condition) else "FAIL",
        "value": value,
    })


def _raster_checks(path: Path, prefix: str, rows: list[dict[str, Any]]) -> None:
    with rasterio.open(path) as source:
        _check(rows, f"{prefix}: readable raster", True, str(path))
        _check(rows, f"{prefix}: CRS declared", source.crs is not None, str(source.crs))
        _check(
            rows,
            f"{prefix}: positive dimensions",
            source.width > 0 and source.height > 0,
            [source.height, source.width],
        )
        sample = source.read(
            1,
            masked=True,
            out_shape=(min(source.height, 512), min(source.width, 512)),
        )
        valid = int(np.count_nonzero(~np.ma.getmaskarray(sample)))
        _check(rows, f"{prefix}: contains valid cells", valid > 0, valid)


def _vector_checks(
    path: Path,
    prefix: str,
    rows: list[dict[str, Any]],
    *,
    allow_confirmed_empty: bool = False,
) -> None:
    data = gpd.read_file(path)
    _check(rows, f"{prefix}: readable vector", True, str(path))
    _check(rows, f"{prefix}: CRS declared", data.crs is not None, str(data.crs))
    usable = int(np.count_nonzero(data.geometry.notna() & ~data.geometry.is_empty))
    if prefix == "hydrography" and usable == 0 and allow_confirmed_empty:
        _check(
            rows,
            "hydrography: provider-confirmed empty domain",
            True,
            {"features": 0, "accepted": True},
        )
    else:
        _check(rows, f"{prefix}: usable geometries", usable > 0, usable)
    if prefix == "hydrography":
        allowed = data.geometry.geom_type.isin(["LineString", "MultiLineString"])
        _check(rows, "hydrography: line geometries", bool(allowed.all()), data.geom_type.value_counts().to_dict())
    elif prefix == "soil":
        allowed = data.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        _check(rows, "soil: polygon geometries", bool(allowed.all()), data.geom_type.value_counts().to_dict())


def check_raw_product(
    name: str,
    product,
    options: dict[str, Any],
    report_path: str | Path,
) -> InputQCResult:
    """Check a saved native product before the notebook displays it as accepted.

    These are structural/native checks. Spatial coverage on the final RIFT grid is
    checked during processing, when the active-cell masks exist.
    """
    rows: list[dict[str, Any]] = []
    accepted: list[str] = []
    try:
        scientific_files = {
            key: Path(path) for key, path in product.files.items() if key != "plot"
        }
        derived_without_file = name == "drainage" and product.provider == "synthetic_nlcd"
        _check(
            rows, "saved native artifacts or declared derivation",
            bool(scientific_files) or derived_without_file,
            len(scientific_files),
        )
        for key, path in scientific_files.items():
            exists = path.is_file() and path.stat().st_size > 0
            _check(rows, f"{key}: saved and nonempty", exists, str(path))
            if exists:
                accepted.append(str(path))

        if name == "dem":
            _raster_checks(scientific_files["raster"], "DEM", rows)
        elif name == "land_cover":
            for key in ("cover", "impervious", "canopy"):
                _check(rows, f"{key}: required layer present", key in scientific_files, key)
                if key in scientific_files:
                    _raster_checks(scientific_files[key], key, rows)
        elif name == "hydrography":
            provider_empty = (
                product.provider == "usgs_3dhp"
                and bool(product.metadata.get("empty_domain_confirmed"))
                and int(product.metadata.get("features", -1)) == 0
                and int(product.metadata.get("tile_count", 0)) > 0
            )
            _vector_checks(
                scientific_files["vector"], name, rows,
                allow_confirmed_empty=provider_empty,
            )
        elif name == "soil":
            if product.provider == "soilgrids":
                for field_name in ("permeability", "porosity", "residual", "alpha"):
                    _check(
                        rows,
                        f"soil raster: {field_name} present",
                        field_name in scientific_files,
                        field_name,
                    )
                    if field_name in scientific_files:
                        _raster_checks(scientific_files[field_name], field_name, rows)
            else:
                _vector_checks(scientific_files["vector"], name, rows)
                soil = gpd.read_file(scientific_files["vector"], rows=1)
                for field_name in (
                    "permeability [m^2]", "porosity [-]",
                    "residual saturation [-]", "van Genuchten alpha [Pa^-1]",
                ):
                    _check(
                        rows,
                        f"soil field: {field_name}",
                        field_name in soil.columns,
                        field_name,
                    )
        elif name == "rainfall":
            variable = str(options.get("precipitation_variable", "APCP_surface"))
            with xr.open_dataset(scientific_files["netcdf"]) as data:
                _check(rows, "rainfall variable present", variable in data, variable)
                _check(rows, "rainfall time coordinate present", "time" in data.coords, "time")
                if variable in data:
                    dims = list(data[variable].dims)
                    _check(rows, "rainfall has spatial dimensions", len(dims) >= 3, dims)
                    sample = np.asarray(data[variable].isel(time=0))
                    _check(
                        rows, "rainfall contains finite values",
                        bool(np.isfinite(sample).any()), int(np.isfinite(sample).sum()),
                    )
                    finite = sample[np.isfinite(sample)]
                    _check(
                        rows, "rainfall is nonnegative",
                        bool(finite.size and np.all(finite >= 0)),
                        float(finite.min()) if finite.size else None,
                    )
                if "time" in data.coords:
                    times = pd.to_datetime(data.time.values, errors="coerce")
                    monotonic = len(times) > 0 and not pd.isna(times).any()
                    if len(times) > 1:
                        monotonic = monotonic and bool(np.all(np.diff(times) > np.timedelta64(0, "ns")))
                    _check(rows, "rainfall time is valid and increasing", monotonic, len(times))
        elif name in {"inflows", "coastal", "spinup_baseflow"}:
            _check(
                rows, f"{name}: stations explicitly selected",
                bool(options.get("stations")), len(options.get("stations", [])),
            )
        elif name == "drainage":
            if "vector" in scientific_files:
                _vector_checks(scientific_files["vector"], "drainage", rows)
            elif "raster" in scientific_files:
                _raster_checks(scientific_files["raster"], "drainage", rows)
            else:
                _check(rows, "drainage derivation definition saved", True, product.provider)
        elif name == "initial_state":
            if "raster" in scientific_files:
                _raster_checks(scientific_files["raster"], "initial water depth", rows)
            else:
                payload = json.loads(scientific_files["definition"].read_text(encoding="utf-8"))
                depth = float(payload["depth_m"])
                _check(
                    rows, "initial water depth is nonnegative",
                    np.isfinite(depth) and depth >= 0, depth,
                )
    except Exception as error:
        _check(rows, "native product can be inspected", False, f"{type(error).__name__}: {error}")

    failed = [item for item in rows if item["status"] == "FAIL"]
    result = InputQCResult(
        input_name=name,
        provider=product.provider,
        status=QC_FAILED if failed else QC_PASSED,
        checks=rows,
        accepted_paths=accepted if not failed else [],
        message=(
            f"{len(failed)} native-product check(s) failed."
            if failed else "Native product passed structural quality checks."
        ),
    )
    result.write(report_path)
    if failed:
        names = "; ".join(item["check"] for item in failed)
        raise QualityControlError(
            f"{name} failed native-product quality control: {names}. "
            f"Inspect {report_path}."
        )
    return result


def qc_results_table(results: dict[str, InputQCResult]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "input": name,
            "provider": result.provider,
            "status": result.status,
            "checks": len(result.checks),
            "message": result.message,
        }
        for name, result in results.items()
    ])
