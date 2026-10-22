import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from riftprep.config import GridSpec, ProjectPaths, SimulationSpec
from riftprep.artifacts import ProvenanceLedger
from riftprep.managers import fetch_usgs_waterdata, RawProduct, RawCatalog
from riftprep.notebook_frontend import case_payload_from_widget_values
from riftprep.spec import case_from_mapping, ProductSpec
from riftprep.spinup import (
    SpinupSpec, calendar_shift, daily_quantile, prepare_depth_handoff,
    prepare_baseflow, run_stages,
)
from riftprep.writers import build_rift_config, write_esri_ascii


def test_calendar_months_and_lookback_without_future_data():
    assert calendar_shift("2024-06-30T00:00:00Z", months=-4).startswith("2024-02-29")
    spin = SpinupSpec(enabled=True, start_utc="2023-02-10T00:00:00Z", end_utc="2023-06-10T00:00:00Z")
    assert spin.duration_s == 120 * 86400
    request = spin.download_options()
    assert request["start_utc"].startswith("2003-02-10")
    assert request["end_utc"].startswith("2023-02-09")
    assert request["statistic_id"] == "00003"


def test_daily_quantile_preserves_zero_rejects_flags_and_never_fills():
    points = [{"time": f"2020-01-0{i+1}", "value": value}
              for i, value in enumerate([0, 10, 20, 30, -999999, None])]
    value, evidence = daily_quantile(points, "2020-01-01", "2020-01-06", 0, 0.5)
    assert value == 0
    assert evidence["zero_flow_days"] == 1
    assert evidence["missing_days"] == 2
    assert evidence["rejected_observations"] == 2
    value, _ = daily_quantile(points, "2020-01-01", "2020-01-06", 0.5, 0.5)
    assert value == 15
    with pytest.raises(ValueError, match="Insufficient"):
        daily_quantile(points, "2020-01-01", "2020-01-06", 0, 0.9)
    with pytest.raises(ValueError, match="Conflicting"):
        daily_quantile(points + [{"time": "2020-01-01", "value": 2}],
                       "2020-01-01", "2020-01-06", 0, 0.5)


def test_frontend_two_phases_defaults_manual_dates_and_hotstart_choices():
    values = {"spinup_enabled": True, "spinup_stations": '[{"site":"01184000","id":"inlet"}]'}
    spec = case_from_mapping(case_payload_from_widget_values(values))
    assert spec.spinup.quantile == 0
    assert spec.spinup.history_years == 20
    assert spec.spinup.start_utc.startswith("2023-02-10")
    assert spec.spinup.output_interval_s == 86400
    assert spec.spinup.save_hotstart and spec.simulation.save_hotstart
    assert spec.simulation.output_interval_s == 3600
    values.update(transient_match_rainfall=False, transient_start="2024-06-30T00:00:00Z",
                  transient_end="2024-07-02T00:00:00Z", transient_output_hours=2,
                  spinup_save_hotstart=False, transient_save_hotstart=False)
    spec = case_from_mapping(case_payload_from_widget_values(values))
    assert spec.spinup.start_utc.startswith("2024-02-29")
    assert spec.simulation.end_time_s == 172800
    assert spec.rainfall.options["start_utc"] == "2024-06-30T00:00:00Z"
    assert spec.simulation.output_interval_s == 7200
    assert not spec.spinup.save_hotstart and not spec.simulation.save_hotstart
    with pytest.raises(ValueError, match="explicit spin-up"):
        case_from_mapping(case_payload_from_widget_values({"spinup_enabled": True}))
    for interval in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            SimulationSpec(output_interval_s=interval)


def test_daily_adapter_uses_daily_time_statistic_and_preserves_response(tmp_path, monkeypatch):
    import riftprep.managers as managers
    calls = []
    observation = {"features": [{"properties": {"time": "2023-01-01", "value": 1}}]}
    class Reply:
        def __init__(self, url, payload):
            self.url = url
            self.content = json.dumps(payload).encode()
        def raise_for_status(self):
            pass
        def json(self):
            return json.loads(self.content)
    class Session:
        def get(self, url, params, timeout):
            calls.append((url, params))
            return Reply(url, {} if "monitoring-locations" in url else observation)
    monkeypatch.setattr(managers, "_waterdata_session", Session)
    paths = ProjectPaths.create(tmp_path)
    product = ProductSpec(provider="usgs_waterdata", options={
        "collection": "daily", "stations": [{"site": "01184000"}],
        "start_utc": "2003-01-01T00:00:00Z", "end_utc": "2022-12-31T23:59:59Z",
    })
    raw = fetch_usgs_waterdata("spinup_baseflow", product, paths,
                              ProvenanceLedger(paths.provenance), plot=False)
    params = calls[1][1]
    assert "datetime" not in params
    assert params["time"] == "2003-01-01/2022-12-31"
    assert params["statistic_id"] == "00003"
    assert params["parameter_code"] == "00060"
    assert json.loads(raw.files["observations_daily_01184000_00060_001"].read_text()) == observation


def test_checked_history_to_constant_si_hydrograph_and_raw_unchanged(tmp_path):
    paths = ProjectPaths.create(tmp_path)
    site = "01184000"
    spin = SpinupSpec(enabled=True, start_utc="2021-01-05T00:00:00Z", end_utc="2021-05-05T00:00:00Z",
                      history_years=1, minimum_coverage=0.01, quantile=0.5,
                      stations=[{"site": site, "id": "inlet", "support": {"x": 30, "y": 30, "crs": "EPSG:5070"}}])
    location = paths.raw / "location.json"
    location.write_text('{"geometry":{"coordinates":[-72,42]}}')
    page = paths.raw / "daily.json"
    features = [{"properties": {"monitoring_location_id": "USGS-"+site, "parameter_code": "00060",
                                "statistic_id": "00003", "time_series_id": "daily",
                                "time": f"2021-01-0{i+1}", "value": value,
                                "unit_of_measure": "ft^3/s", "approval_status": "Approved"}}
                for i, value in enumerate([0, 10, 20, 30])]
    page.write_text(json.dumps({"features": features}))
    before = page.read_bytes()
    product = RawProduct("spinup_baseflow", "usgs_waterdata",
                         {"location_"+site: location, "observations_001": page}, {})
    raw = RawCatalog(boundary=None, products={"spinup_baseflow": product})
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    table, plotted = prepare_baseflow(SimpleNamespace(spinup=spin), raw, grid, paths)
    assert page.read_bytes() == before
    assert np.allclose(plotted["inlet"][1], 15 * 0.028316846592)
    report = json.loads((paths.quality_control / "spinup_baseflow.json").read_text())
    assert report["status"] == "PASSED"
    assert report["raw_product_modified"] is False
    assert table.is_file()


def test_handoff_uses_latest_snapshot_and_rejects_stale_or_outside_paths(tmp_path):
    paths = ProjectPaths.create(tmp_path)
    output = paths.results / "spinup"
    output.mkdir()
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    (paths.quality_control / "grid.json").write_text(json.dumps(asdict(grid)))
    for i, depth in [(0, 0), (7, 2)]:
        write_esri_ascii(output / f"water_depth_{i:04d}.asc", np.full((2, 2), depth), grid)
    (output / "snapshot_index.csv").write_text(
        "snapshot_index,simulation_time_s,water_depth_file\n"
        "0,0,water_depth_0000.asc\n7,86399,water_depth_0007.asc\n")
    plan = {"spinup_output_directory": "results/spinup", "spinup_duration_s": 86400,
            "maximum_snapshot_age_s": 86400, "handoff_depth": "simulation_ready/spinup_water_depth.asc"}
    target = prepare_depth_handoff(tmp_path, plan)
    assert np.allclose(np.loadtxt(target, skiprows=6), 2)
    assert json.loads((paths.quality_control / "spinup_handoff.json").read_text())["snapshot_age_s"] == 1
    with pytest.raises(ValueError, match="allowed"):
        prepare_depth_handoff(tmp_path, {**plan, "spinup_duration_s": 300000})
    with pytest.raises(ValueError, match="within"):
        prepare_depth_handoff(tmp_path, {**plan, "handoff_depth": "../outside.asc"})


def test_stages_do_not_use_stale_output_or_advance_after_failed_solver(tmp_path, monkeypatch):
    import riftprep.spinup as spinup
    paths = ProjectPaths.create(tmp_path)
    build_rift_config(tmp_path / "rift_case.cfg", SimulationSpec(), {}, {})
    (tmp_path / "simulation_plan.json").write_text(json.dumps({
        "spinup_output_directory": "results/spinup", "spinup_duration_s": 86400,
        "spinup_config": "rift_spinup.cfg", "transient_config": "rift_case.cfg",
    }))
    (paths.results / "spinup").mkdir()
    (paths.results / "spinup/old.txt").write_text("old")
    with pytest.raises(FileExistsError):
        run_stages(tmp_path, "/fake/rift")
    (paths.results / "spinup/old.txt").unlink()
    calls = []
    def failed(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(spinup.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="did not confirm"):
        run_stages(tmp_path, "/fake/rift")
    assert len(calls) == 1


def test_stage_runner_order_and_successful_depth_handoff(tmp_path, monkeypatch):
    import riftprep.spinup as spinup
    paths = ProjectPaths.create(tmp_path)
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    (paths.quality_control / "grid.json").write_text(json.dumps(asdict(grid)))
    build_rift_config(tmp_path / "rift_case.cfg", SimulationSpec(end_time_s=3600), {}, {})
    plan = {"spinup_output_directory": "results/spinup", "spinup_duration_s": 86400,
            "spinup_config": "rift_spinup.cfg", "transient_config": "rift_case.cfg",
            "transient_duration_s": 3600, "maximum_snapshot_age_s": 86400,
            "handoff_depth": "simulation_ready/spinup_water_depth.asc"}
    (tmp_path / "simulation_plan.json").write_text(json.dumps(plan))
    calls = []
    def solver(command, **kwargs):
        calls.append(command[-1])
        if command[-1] == "rift_spinup.cfg":
            output = paths.results / "spinup"
            write_esri_ascii(output / "water_depth_0100.asc", np.ones((2, 2)), grid)
            (output / "snapshot_index.csv").write_text(
                "snapshot_index,simulation_time_s,water_depth_file\n100,86400,water_depth_0100.asc\n")
            kwargs["stdout"].write("[summary] wall_s=1 simulated_s=86400 steps=10 outputs=2\n")
        else:
            assert (paths.simulation_ready / "spinup_water_depth.asc").is_file()
            kwargs["stdout"].write("[summary] wall_s=1 simulated_s=3600 steps=10 outputs=2\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(spinup.subprocess, "run", solver)
    target = run_stages(tmp_path, "/fake/rift")
    assert target.is_file()
    assert calls == ["rift_spinup.cfg", "rift_case.cfg"]
