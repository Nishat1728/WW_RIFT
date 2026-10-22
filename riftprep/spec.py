"""Validated, provider-neutral configuration for a RIFT input build."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from .config import DomainSpec, OutletSpec, RiverBathymetrySpec, SimulationSpec
from .spinup import SpinupSpec, utc_time


def rainfall_period_seconds(options: Mapping[str, Any]) -> float:
    """Return the elapsed UTC event duration, rejecting ambiguous timestamps."""
    times = []
    for key, label in (("start_utc", "Rainfall start"), ("end_utc", "Rainfall end")):
        text = str(options.get(key, "")).strip()
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{label} must be an ISO date/time with a timezone, "
                "for example 2023-06-10T00:00:00Z."
            ) from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone (use Z for UTC).")
        times.append(timestamp.astimezone(timezone.utc))
    duration = (times[1] - times[0]).total_seconds()
    if duration <= 0:
        raise ValueError("Rainfall end must be later than rainfall start.")
    return duration


@dataclass(frozen=True)
class ProductSpec:
    """One selectable source product and its provider-specific options."""

    enabled: bool = True
    provider: str = "local"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CachePolicy:
    """Control provider-cache reuse without weakening fresh-run safety."""

    mode: Literal["fresh", "reuse"] = "fresh"

    def __post_init__(self) -> None:
        if self.mode not in {"fresh", "reuse"}:
            raise ValueError("cache.mode must be 'fresh' or 'reuse'.")


@dataclass(frozen=True)
class PlotPolicy:
    raw_products: bool = True
    processed_products: bool = True
    formats: tuple[str, ...] = ("png",)
    dpi: int = 180


@dataclass(frozen=True)
class CaseSpec:
    """Everything needed to download raw data and produce a RIFT-ready case."""

    case_name: str
    output_root: str
    domain: DomainSpec
    dem: ProductSpec
    hydrography: ProductSpec = field(
        default_factory=lambda: ProductSpec(provider="usgs_3dhp")
    )
    land_cover: ProductSpec = field(
        default_factory=lambda: ProductSpec(provider="watershed_workflow_nlcd")
    )
    soil: ProductSpec = field(
        default_factory=lambda: ProductSpec(provider="watershed_workflow_ssurgo")
    )
    rainfall: ProductSpec = field(
        default_factory=lambda: ProductSpec(provider="watershed_workflow_aorc")
    )
    inflows: ProductSpec = field(
        default_factory=lambda: ProductSpec(enabled=False, provider="none")
    )
    coastal: ProductSpec = field(
        default_factory=lambda: ProductSpec(enabled=False, provider="none")
    )
    drainage: ProductSpec = field(
        default_factory=lambda: ProductSpec(enabled=False, provider="none")
    )
    initial_state: ProductSpec = field(
        default_factory=lambda: ProductSpec(provider="dry_zero")
    )
    outlets: OutletSpec | None = None
    river_bathymetry: RiverBathymetrySpec = field(default_factory=RiverBathymetrySpec)
    cache: CachePolicy = field(default_factory=CachePolicy)
    plots: PlotPolicy = field(default_factory=PlotPolicy)
    simulation: SimulationSpec = field(default_factory=SimulationSpec)
    spinup: SpinupSpec = field(default_factory=SpinupSpec)

    def __post_init__(self) -> None:
        if not self.case_name or any(c in self.case_name for c in "\\/:*?\"<>|"):
            raise ValueError("case_name must be a nonempty portable directory name.")
        if self.dem.enabled and self.dem.provider == "watershed_workflow_3dep":
            resolution = int(self.dem.options.get("resolution_m", 30))
            if resolution not in {10, 30}:
                raise ValueError("RIFT 3DEP resolution_m must be 10 or 30.")
        for name, product in (
            ("rainfall", self.rainfall), ("inflows", self.inflows), ("coastal", self.coastal)
        ):
            if product.enabled and not {"start_utc", "end_utc"}.issubset(product.options):
                raise ValueError(f"{name} needs start_utc and end_utc.")
        if self.simulation.start_utc or self.simulation.end_utc:
            start = utc_time(self.simulation.start_utc)
            end = utc_time(self.simulation.end_utc)
            if (end - start).total_seconds() != self.simulation.end_time_s - self.simulation.start_time_s:
                raise ValueError("Transient UTC window must match its elapsed simulation duration.")
            for name in ("rainfall", "inflows", "coastal"):
                product = getattr(self, name)
                if product.enabled and (
                    utc_time(product.options["start_utc"]) != start
                    or utc_time(product.options["end_utc"]) != end
                ):
                    raise ValueError(f"{name} window must equal the transient UTC window.")
        if self.spinup.enabled:
            self.spinup.validate()
            if self.simulation.start_time_s != 0:
                raise ValueError("Two-stage runs require a zero-based transient solver clock.")
            if not self.simulation.start_utc:
                raise ValueError("Spin-up requires simulation.start_utc and simulation.end_utc.")
            if utc_time(self.spinup.end_utc) != utc_time(self.simulation.start_utc):
                raise ValueError("Spin-up end must equal transient start; gaps/overlaps are not implicit.")
            if self.spinup.output_directory == self.simulation.output_directory:
                raise ValueError("Spin-up and transient output directories must differ.")

    @property
    def spinup_baseflow(self) -> ProductSpec:
        return ProductSpec(
            enabled=self.spinup.enabled, provider=self.spinup.provider,
            options=self.spinup.download_options(),
        )

    @property
    def case_root(self) -> Path:
        return Path(self.output_root).expanduser() / self.case_name

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _product(value: Mapping[str, Any] | None, default: ProductSpec) -> ProductSpec:
    return default if value is None else ProductSpec(**dict(value))


def case_from_mapping(payload: Mapping[str, Any]) -> CaseSpec:
    """Build a strongly validated spec from a YAML-compatible mapping."""
    data = dict(payload)
    case_name = str(data.pop("case_name"))
    output_root = str(data.pop("output_root"))
    domain_payload = dict(data.pop("domain"))
    if "bounds" in domain_payload and domain_payload["bounds"] is not None:
        domain_payload["bounds"] = tuple(domain_payload["bounds"])
    domain = DomainSpec(**domain_payload)
    simulation_payload = dict(data.pop("simulation", {}))
    spinup_payload = dict(data.pop("spinup", {}))
    cache = CachePolicy(**dict(data.pop("cache", {})))
    plots_payload = dict(data.pop("plots", {}))
    if "formats" in plots_payload:
        plots_payload["formats"] = tuple(plots_payload["formats"])
    plots = PlotPolicy(**plots_payload)
    outlets_payload = data.pop("outlets", None)
    if outlets_payload is not None:
        outlets_payload = dict(outlets_payload)
        if "coordinates" in outlets_payload:
            outlets_payload["coordinates"] = tuple(outlets_payload["coordinates"])
        outlets = OutletSpec(**outlets_payload)
    else:
        outlets = None
    river_payload = dict(data.pop("river_bathymetry", {}))
    if "depth_by_order" in river_payload:
        river_payload["depth_by_order"] = {
            int(key): float(value) for key, value in river_payload["depth_by_order"].items()
        }
    river_bathymetry = RiverBathymetrySpec(**river_payload)
    defaults = {
        "hydrography": ProductSpec(provider="usgs_3dhp"),
        "land_cover": ProductSpec(provider="watershed_workflow_nlcd"),
        "soil": ProductSpec(provider="watershed_workflow_ssurgo"),
        "rainfall": ProductSpec(provider="watershed_workflow_aorc"),
        "inflows": ProductSpec(enabled=False, provider="none"),
        "coastal": ProductSpec(enabled=False, provider="none"),
        "drainage": ProductSpec(enabled=False, provider="none"),
        "initial_state": ProductSpec(provider="dry_zero"),
    }
    products = {
        name: _product(data.pop(name, None), default) for name, default in defaults.items()
    }
    if products["rainfall"].enabled and "end_time_s" not in simulation_payload:
        simulation_payload["end_time_s"] = (
            float(simulation_payload.get("start_time_s", 0))
            + rainfall_period_seconds(products["rainfall"].options)
        )
    if products["rainfall"].enabled and (
        float(simulation_payload["end_time_s"]) - float(simulation_payload.get("start_time_s", 0))
        == rainfall_period_seconds(products["rainfall"].options)
    ):
        for key in ("start_utc", "end_utc"):
            simulation_payload.setdefault(key, products["rainfall"].options[key])
    simulation = SimulationSpec(**simulation_payload)
    dem = _product(data.pop("dem", None), ProductSpec(provider="watershed_workflow_3dep"))
    if data:
        raise ValueError(f"Unknown configuration keys: {sorted(data)}")
    return CaseSpec(
        case_name=case_name,
        output_root=output_root,
        domain=domain,
        simulation=simulation,
        spinup=SpinupSpec(**spinup_payload),
        cache=cache,
        plots=plots,
        outlets=outlets,
        river_bathymetry=river_bathymetry,
        dem=dem,
        **products,
    )


def load_case(path: str | Path) -> CaseSpec:
    path = Path(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"Configuration must be a mapping: {path}")
    return case_from_mapping(payload)


def save_case(spec: CaseSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec.as_dict(), sort_keys=False), encoding="utf-8")
    return path
