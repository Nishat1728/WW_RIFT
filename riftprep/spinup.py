"""Rain-free, observed-low-flow spin-up and complete hotstart continuation.

Daily-mean discharge quantiles are NOT groundwater baseflow separation and do not
guarantee perennial flow. Raw observations are never clipped, filled or rewritten.
"""
from __future__ import annotations

import calendar
import json
import math
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_time(value: str | None) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Use an ISO date/time with timezone, e.g. 2023-06-10T00:00:00Z.") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Simulation times must include a timezone (Z for UTC).")
    return result.astimezone(timezone.utc)


def calendar_shift(value: str, *, months: int = 0, years: int = 0) -> str:
    """Shift calendar months/years, clamping only an unavailable month-end day."""
    stamp = utc_time(value)
    month_index = stamp.year * 12 + stamp.month - 1 + months + years * 12
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return stamp.replace(year=year, month=month,
                         day=min(stamp.day, calendar.monthrange(year, month)[1])).isoformat()


@dataclass(frozen=True)
class SpinupSpec:
    # Legacy YAML files remain transient-only; the notebook asks explicitly.
    enabled: bool = False
    start_utc: str = ""
    end_utc: str = ""
    provider: str = "usgs_waterdata"
    stations: list[dict[str, Any]] = field(default_factory=list)
    history_years: int = 20
    quantile: float = 0.0
    minimum_coverage: float = 0.90
    output_interval_s: float = 86400.0
    save_hotstart: bool = True
    output_directory: str = "results/spinup"

    @property
    def duration_s(self) -> float:
        return (utc_time(self.end_utc) - utc_time(self.start_utc)).total_seconds()

    def validate(self) -> None:
        from .config import SimulationSpec
        if self.duration_s <= 0:
            raise ValueError("Spin-up end must be after its start.")
        if self.output_interval_s > self.duration_s:
            raise ValueError("Spin-up save interval cannot exceed the whole spin-up duration.")
        if self.provider not in {"usgs_waterdata", "manual_constant"}:
            raise ValueError("Spin-up source must be usgs_waterdata or manual_constant.")
        if not self.stations:
            raise ValueError("Select explicit spin-up gauges/injection points; no ungauged flow is invented.")
        ids = [str(item.get("id", item.get("site", ""))) for item in self.stations]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in ids):
            raise ValueError("Spin-up station IDs must be unique letters, digits, underscores or hyphens.")
        if (not isinstance(self.history_years, int) or isinstance(self.history_years, bool)
                or self.history_years <= 0):
            raise ValueError("History years must be a positive integer.")
        if not math.isfinite(self.quantile) or not 0 <= self.quantile <= 1:
            raise ValueError("Baseflow quantile must be between 0 and 1 (non-exceedance probability).")
        if not math.isfinite(self.minimum_coverage) or not 0 < self.minimum_coverage <= 1:
            raise ValueError("Minimum daily-record coverage must be in (0, 1].")
        for station in self.stations:
            if self.provider == "usgs_waterdata":
                if not re.fullmatch(r"(?:USGS-)?[0-9]+", str(station.get("site", ""))):
                    raise ValueError("Each historical-flow station needs an explicit USGS site number.")
                if station.get("parameter_code", "00060") != "00060":
                    raise ValueError("Spin-up uses discharge parameter 00060, not stage or other variables.")
            else:
                flow = float(station.get("constant_m3_s", float("nan")))
                if not math.isfinite(flow) or flow < 0 or not station.get("support"):
                    raise ValueError("Manual baseflow needs nonnegative constant_m3_s and explicit support coordinates.")
        SimulationSpec(end_time_s=self.duration_s, output_interval_s=self.output_interval_s,
                       output_directory=self.output_directory)

    def download_options(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        # USGS daily dates are local calendar dates. Exclude the start day of
        # spin-up entirely, preventing partial-day/future leakage.
        history_end = utc_time(self.start_utc).date() - timedelta(days=1)
        history_start = utc_time(calendar_shift(self.start_utc, years=-self.history_years)).date()
        return {
            "collection": "daily", "statistic_id": "00003",
            "start_utc": history_start.isoformat() + "T00:00:00Z",
            "end_utc": history_end.isoformat() + "T23:59:59Z",
            "stations": [
                {**station, "collection": "daily", "statistic_id": "00003"}
                for station in self.stations
            ],
            "quantile": self.quantile, "minimum_coverage": self.minimum_coverage,
            "history_years": self.history_years,
        }


def daily_quantile(observations, start_date: str, end_date: str, quantile: float,
                   minimum_coverage: float) -> tuple[float, dict[str, Any]]:
    """Use valid unique daily means only; preserve genuine zeros, never gap-fill."""
    import numpy as np
    import pandas as pd
    if not 0 <= quantile <= 1 or not 0 < minimum_coverage <= 1:
        raise ValueError("Invalid quantile or minimum daily coverage.")
    days = pd.date_range(start_date[:10], end_date[:10], freq="D")
    by_day: dict[str, float] = {}
    rejected = outside = 0
    for item in observations:
        day = str(item.get("time", ""))[:10]
        try:
            stamp = datetime.strptime(day, "%Y-%m-%d").date()
            value = float(item.get("value"))
        except (TypeError, ValueError):
            rejected += 1
            continue
        if not (days[0].date() <= stamp <= days[-1].date()):
            outside += 1
            continue
        if not math.isfinite(value) or value < 0:
            rejected += 1
            continue
        if day in by_day and by_day[day] != value:
            raise ValueError(f"Conflicting daily discharge values on {day}; choose one time_series_id.")
        by_day[day] = value
    values = np.asarray(list(by_day.values()), dtype=float)
    coverage = len(values) / len(days)
    evidence = {
        "requested_days": len(days), "valid_days": len(values),
        "missing_days": len(days) - len(values), "coverage_fraction": coverage,
        "minimum_coverage": minimum_coverage, "rejected_observations": rejected,
        "out_of_window_observations": outside, "zero_flow_days": int((values == 0).sum()),
        "first_valid_day": min(by_day) if by_day else None,
        "last_valid_day": max(by_day) if by_day else None,
        "quantile": quantile, "quantile_method": "numpy.quantile(method='linear')",
        "gap_filling": "none",
    }
    if not values.size or coverage < minimum_coverage:
        raise ValueError(f"Insufficient daily discharge record: {len(values)}/{len(days)} days "
                         f"({coverage:.1%}); required {minimum_coverage:.1%}.")
    return float(np.quantile(values, quantile, method="linear")), evidence


def prepare_baseflow(spec, raw, grid, paths):
    """Resolve checked daily USGS means (or a declared manual constant) to SI."""
    import numpy as np
    from .workflow import _support_xy, _unit_factor
    from .writers import write_hydrograph_bundle

    options = spec.spinup.download_options()
    product = raw.require("spinup_baseflow")
    report: dict[str, Any] = {
        "status": "RUNNING", "provider": product.provider, "stations": [],
        "raw_product_modified": False, "rainfall": "disabled",
        "interpretation": "Historical daily-mean flow quantile; not separated groundwater baseflow or guaranteed perennial flow.",
        "history_start_date": options["start_utc"][:10],
        "history_end_date": options["end_utc"][:10],
        "requested_history_years": spec.spinup.history_years,
    }
    report_path = paths.quality_control / "spinup_baseflow.json"
    sources = []
    plots = {}
    try:
        for station in spec.spinup.stations:
            site = str(station.get("site", "")).removeprefix("USGS-")
            name = str(station.get("id", site))
            evidence: dict[str, Any] = {"id": name, "site": site}
            series: dict[str, Any] = {}
            if product.provider == "manual_constant":
                flow = float(station["constant_m3_s"])
                evidence.update(method="explicit_user_constant", discharge_m3_s=flow)
            else:
                groups: dict[str, list[dict[str, Any]]] = {}
                units: dict[str, set[str]] = {}
                location_path = product.files[f"location_{site}"]
                location = json.loads(location_path.read_text(encoding="utf-8"))
                for key, path in product.files.items():
                    if not key.startswith("observations_"):
                        continue
                    for feature in json.loads(path.read_text(encoding="utf-8")).get("features", []):
                        props = feature.get("properties", {})
                        if (str(props.get("monitoring_location_id")).removeprefix("USGS-") != site
                                or str(props.get("parameter_code")) != "00060"):
                            continue
                        if str(props.get("statistic_id")) != "00003":
                            raise ValueError("Provider returned a non-daily-mean statistic; refusing to mix statistics.")
                        key_id = str(props.get("time_series_id", ""))
                        groups.setdefault(key_id, []).append(props)
                        units.setdefault(key_id, set()).add(str(props.get("unit_of_measure", "unknown")))
                selected = str(station.get("time_series_id", ""))
                if not selected and len(groups) == 1:
                    selected = next(iter(groups))
                if selected not in groups:
                    raise ValueError(f"Select one daily time_series_id for {site}; available={sorted(groups)}.")
                if len(units[selected]) != 1:
                    raise ValueError(f"Mixed discharge units for station {site}.")
                unit = next(iter(units[selected]))
                value, stats = daily_quantile(
                    groups[selected], options["start_utc"], options["end_utc"],
                    spec.spinup.quantile, spec.spinup.minimum_coverage,
                )
                factor = _unit_factor(unit, station, "discharge")
                if not math.isfinite(factor) or factor <= 0:
                    raise ValueError("Discharge conversion factor must be positive finite.")
                flow = value * factor
                xy = location["geometry"]["coordinates"]
                series = {"sourceInfo": {"geoLocation": {"geogLocation": {
                    "longitude": xy[0], "latitude": xy[1],
                }}}}
                evidence.update(stats, method="observed_daily_mean_quantile",
                                time_series_id=selected, provider_unit=unit,
                                discharge_m3_s=flow,
                                approval_status_counts={str(status): sum(
                                    str(p.get("approval_status")) == str(status)
                                    for p in groups[selected]
                                ) for status in {p.get("approval_status") for p in groups[selected]}})
            x, y = _support_xy(station, series, grid)
            source = {"id": name, "time_s": [0, spec.spinup.duration_s],
                      "discharge_m3_s": [flow, flow], "xmin_m": x, "ymin_m": y}
            width = float(station.get("support_width_m", 0))
            if not math.isfinite(width) or width < 0:
                raise ValueError("Support width must be finite and nonnegative.")
            if width:
                source.update(xmin_m=x-width/2, xmax_m=x+width/2,
                              ymin_m=y-width/2, ymax_m=y+width/2)
            sources.append(source)
            plots[name] = (np.asarray(source["time_s"]), np.asarray(source["discharge_m3_s"]))
            evidence.update(support_x_m=x, support_y_m=y, support_crs=grid.crs)
            report["stations"].append(evidence)
        table = write_hydrograph_bundle(paths.simulation_ready / "spinup_hydrographs", sources)
        report.update(status="PASSED", inflows_table=str(table))
        return table, plots
    except Exception as error:
        report.update(status="FAILED", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def write_phase_configs(spec, paths, inputs, modules, baseflow_table):
    """Continue solver time; evaluate all event forcing on its own elapsed clock."""
    from .writers import build_rift_config
    spinup_inputs = {key: value for key, value in inputs.items()
                     if not key.startswith(("forcing.", "inflows.", "restart."))}
    spinup_inputs["inflows.table"] = baseflow_table.relative_to(paths.root).as_posix()
    spinup_modules = {**modules, "rainfall": False, "inflows": True, "surge": False}
    spinup_sim = replace(spec.simulation, start_time_s=0, end_time_s=spec.spinup.duration_s,
                         start_utc=spec.spinup.start_utc, end_utc=spec.spinup.end_utc,
                         output_interval_s=spec.spinup.output_interval_s,
                         output_directory=spec.spinup.output_directory,
                         save_hotstart=spec.spinup.save_hotstart)
    cfg = build_rift_config(paths.root / "rift_spinup.cfg", spinup_sim, spinup_inputs, spinup_modules,
                            output_options={"output.final_hotstart": 1})
    handoff = (paths.simulation_ready / "spinup_hotstart.bin").relative_to(paths.root).as_posix()
    event_inputs = {key: value for key, value in inputs.items() if key != "initial.water_depth"}
    event_inputs.update({"restart.hotstart": handoff, "restart.reset_diagnostics": 1,
                         "forcing.time_origin_s": spec.spinup.duration_s})
    duration = spec.simulation.end_time_s - spec.simulation.start_time_s
    event_sim = replace(spec.simulation, start_time_s=spec.spinup.duration_s,
                        end_time_s=spec.spinup.duration_s+duration)
    event = build_rift_config(paths.root / "rift_case.cfg", event_sim, event_inputs, modules,
                              output_options={"output.final_hotstart": int(spec.simulation.save_hotstart)})
    plan = {
        "schema": "riftprep-two-stage-v2", "required_solver_contract": "rift-uconn-complete-restart-v1",
        "spinup_config": cfg.name,
        "transient_config": event.name,
        "spinup_start_utc": spec.spinup.start_utc, "spinup_end_utc": spec.spinup.end_utc,
        "spinup_duration_s": spec.spinup.duration_s,
        "transient_start_utc": spec.simulation.start_utc,
        "transient_end_utc": spec.simulation.end_utc,
        "transient_duration_s": spec.simulation.end_time_s - spec.simulation.start_time_s,
        "spinup_output_directory": spec.spinup.output_directory,
        "spinup_output_interval_s": spec.spinup.output_interval_s,
        "spinup_save_hotstart": spec.spinup.save_hotstart,
        "transient_output_interval_s": spec.simulation.output_interval_s,
        "transient_save_hotstart": spec.simulation.save_hotstart,
        "handoff_hotstart": handoff,
        "handoff": "Complete versioned binary state at the exact spin-up end; no interpolation or clock reset.",
        "transient_solver_start_s": spec.spinup.duration_s,
        "transient_solver_end_s": spec.spinup.duration_s+duration,
        "forcing_time_origin_s": spec.spinup.duration_s,
        "runtime_dependency": "Requires the WW_RIFT complete-restart solver integration and its final checkpoint. No legacy/depth fallback.",
    }
    (paths.root / "simulation_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return event


def run_stages(case_root: str | Path, executable: str | Path) -> Path:
    """Run phases in order, verify complete restart support, copy state, then event.

    Never assumes output 0001 is final or silently reuses old solver output.
    All paths below are confined to the explicitly selected generated case.
    """
    root = Path(case_root).resolve()
    plan = json.loads((root / "simulation_plan.json").read_text(encoding="utf-8"))
    if plan.get("schema") != "riftprep-two-stage-v2":
        raise ValueError("Rebuild the old depth-only configuration for complete hotstart continuation.")
    executable = str(Path(executable).expanduser().resolve())
    spinup_dir = _within(root, plan["spinup_output_directory"])
    if spinup_dir.exists() and any(spinup_dir.iterdir()):
        raise FileExistsError("Spin-up output directory is not empty; choose a new case, do not reuse stale output.")
    event_dir = _config_output(root / plan["transient_config"])
    event_path = _within(root, event_dir)
    if event_path.exists() and any(event_path.iterdir()):
        raise FileExistsError("Transient output directory is not empty; choose a new case.")
    from .restart import CONTRACT, prepare_hotstart_handoff
    try:
        capability = subprocess.run([executable, "--restart-capabilities"], capture_output=True,
                                    text=True, check=True)
        support = json.loads(capability.stdout)
        if (not isinstance(support, dict) or support.get("schema") != CONTRACT or not support.get("forcing_time_origin")
                or not support.get("exact_final_checkpoint")):
            raise ValueError("incomplete capabilities")
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        raise RuntimeError("RIFT solver lacks the complete-restart integration. See integrations/rift_uconn/README.md; no partial-state fallback is allowed.") from error
    spinup_dir.mkdir(parents=True, exist_ok=True)
    log = root / "spinup_solver.log"
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run([executable, plan["spinup_config"]], cwd=root,
                                   stdout=stream, stderr=subprocess.STDOUT, check=False)
    text = log.read_text(encoding="utf-8", errors="replace")
    summaries = re.findall(r"\[summary\][^\n]*simulated_s=([0-9.eE+\-]+)", text)
    if completed.returncode or not summaries or float(summaries[-1]) < plan["spinup_duration_s"] - 1e-3:
        raise RuntimeError(f"Spin-up did not confirm completion of its requested duration. Inspect {log}.")
    hotstart = prepare_hotstart_handoff(root, plan)
    event_path.mkdir(parents=True, exist_ok=True)
    with (root / "transient_solver.log").open("w", encoding="utf-8") as stream:
        subprocess.run([executable, plan["transient_config"]], cwd=root,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)
    text = (root / "transient_solver.log").read_text(encoding="utf-8", errors="replace")
    summaries = re.findall(r"\[summary\][^\n]*simulated_s=([0-9.eE+\-]+)", text)
    if not summaries or float(summaries[-1]) < plan["transient_duration_s"] - 1e-3:
        raise RuntimeError("Transient solver did not confirm completion. Inspect transient_solver.log.")
    return hotstart


def _within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root):
        raise ValueError("Simulation paths must stay within the case directory.")
    return path


def _config_output(path: Path) -> str:
    import configparser
    config = configparser.ConfigParser()
    config.read(path)
    return config["outputs"]["output.directory"]
