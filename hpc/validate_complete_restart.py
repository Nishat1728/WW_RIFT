"""Opt-in tiny GPU test: complete state round-trip + split/uninterrupted equivalence.

All data are synthetic fixtures. No provider downloads or four-month simulation.
"""
from __future__ import annotations
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

from riftprep.config import GridSpec, SimulationSpec
from riftprep.restart import inspect_checkpoint
from riftprep.writers import build_rift_config, write_esri_ascii, write_ats_rainfall, write_hydrograph_bundle


def main(executable, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    executable = str(Path(executable).resolve())
    grid = GridSpec(0, 0, 600, 600, 30, "EPSG:5070", 20, 20)
    def raster(name, values):
        return write_esri_ascii(root / name, np.broadcast_to(values, grid.node_shape), grid).name
    inputs = {"domain.dem": raster("dem.asc", np.linspace(2, 1, 21)[None, :]),
              "domain.coordinates": "projected_m", "domain.square_cells": 1,
              "initial.water_depth": raster("depth.asc", 0.1),
              "surface.manning_n": raster("manning.asc", 0.035),
              "soil.saturated_conductivity": raster("ksat.asc", 1e-6), "soil.suction_head_m": 0.1,
              "soil.moisture_deficit": 0.3,
              "soil.initial_saturation": raster("saturation.asc", 0.5),
              "soil.root_depth_m": 0.5, "soil.drainage_time_s": 172800,
              "soil.residual_saturation": 0.05,
              "drainage.infrastructure_map": raster("drainage.asc", 1)}
    # Linear storage capture, deliberately nonzero to exercise urban state.
    (root / "controls.csv").write_text(
        "type,name,capture_capacity_m_s,storage_capacity_m,release_rate_m_s,recovery_time_s\n"
        "1,test_drain,0.00001,0.05,0.000001,3600\n")
    inputs["drainage.parameter_table"] = "controls.csv"
    modules = {"infiltration": True, "dynamic_soil": True, "urban_drainage": True,
               "inflows": True, "rainfall": True}
    reference_table = write_hydrograph_bundle(root / "ref_hydrographs", [{
        "id": "river", "time_s": [0, 40, 80], "discharge_m3_s": [0.01, 0.02, 0.01],
        "xmin_m": 75, "ymin_m": 285,
    }])
    event_table = write_hydrograph_bundle(root / "event_hydrographs", [{
        "id": "river", "time_s": [0, 40], "discharge_m3_s": [0.02, 0.01],
        "xmin_m": 75, "ymin_m": 285,
    }])
    # Spin-up fixture has the same (varying) boundary as the reference before
    # the split. It tests serialization, not the historical quantile estimator.
    spin_table = write_hydrograph_bundle(root / "spin_hydrographs", [{
        "id": "river", "time_s": [0, 40], "discharge_m3_s": [0.01, 0.02],
        "xmin_m": 75, "ymin_m": 285,
    }])
    write_ats_rainfall(root / "reference.h5", [0, 40, 80], np.stack([
        np.zeros(grid.cell_shape), np.full(grid.cell_shape, 3e-6), np.full(grid.cell_shape, 3e-6)]))
    write_ats_rainfall(root / "event.h5", [0, 40], np.full((2, *grid.cell_shape), 3e-6))
    inputs.update({"forcing.rainfall_hdf5": "reference.h5", "forcing.rainfall_relative_time": 1,
                   "forcing.rainfall_interpolation": "step", "inflows.table": reference_table.relative_to(root).as_posix()})
    simulation = SimulationSpec(end_time_s=80, output_interval_s=40, output_directory="reference")
    def run(name, simulation, configuration, enabled):
        output = root / simulation.output_directory
        output.mkdir()
        cfg = build_rift_config(root / (name+".cfg"), simulation, configuration, enabled,
                                output_options={"output.final_hotstart": 1})
        with (root / (name+".log")).open("w") as log:
            subprocess.run([executable, cfg.name], cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
        return inspect_checkpoint(output / "final_hotstart.bin", expected_time_s=simulation.end_time_s, include_arrays=True)
    full = run("reference", simulation, inputs, modules)
    spin_inputs = {key: value for key, value in inputs.items() if not key.startswith("forcing.")}
    spin_inputs["inflows.table"] = spin_table.relative_to(root).as_posix()
    spin = run("spinup", replace(simulation, end_time_s=40, output_directory="spinup"), spin_inputs,
               {**modules, "rainfall": False})
    event_inputs = {key: value for key, value in inputs.items() if key != "initial.water_depth"}
    event_inputs.update({"forcing.rainfall_hdf5": "event.h5", "forcing.time_origin_s": 40,
                         "inflows.table": event_table.relative_to(root).as_posix(),
                         "restart.hotstart": "spinup/final_hotstart.bin", "restart.audit_loaded_state": 1})
    split = run("event", replace(simulation, start_time_s=40, output_directory="event"), event_inputs, modules)
    loaded = inspect_checkpoint(root / "event/restart_loaded.bin", expected_time_s=40, include_arrays=True)
    assert loaded["field_sha256"] == spin["field_sha256"], "A stored evolving state changed while loading"
    assert loaded["sha256"] == spin["sha256"], "Block/scalar state changed during round-trip"
    assert np.any(spin["arrays"]["soil_saturation"] > 0)
    assert np.any(spin["arrays"]["urban_storage"] > 0), "Fixture must exercise retained urban water"
    differences = {}
    for name in full["fields"]:
        left, right = full["arrays"][name], split["arrays"][name]
        differences[name] = float(np.max(np.abs(left-right)))
        if not np.allclose(left, right, rtol=1e-9, atol=1e-11):
            raise AssertionError(f"Restart equivalence failed for {name}: {differences[name]}")
    # A truncated file must fail before being called a successful restart.
    bad = root / "truncated.bin"
    bad.write_bytes((root / "spinup/final_hotstart.bin").read_bytes()[:64])
    failure_cfg = root / "reject.cfg"
    build_rift_config(failure_cfg, replace(simulation, start_time_s=40, output_directory="reject"),
                      {**event_inputs, "restart.hotstart": "truncated.bin"}, modules)
    (root / "reject").mkdir()
    rejected = subprocess.run([executable, failure_cfg.name], cwd=root, capture_output=True, text=True)
    assert rejected.returncode != 0 and "[fatal]" in rejected.stderr
    report = {"schema": "riftprep-complete-restart-gpu-validation-v1", "status": "PASSED",
              "fixture": "synthetic 20x20 flow cells, 30m, 80s split at 40s",
              "roundtrip_bitwise_all_named_states": True, "rainfall_and_inflow_clock_alignment": True,
              "exact_phase_end": True, "truncated_checkpoint_rejected": True,
              "nonzero_soil_and_urban_state_exercised": True,
              "max_absolute_difference_by_field": differences,
              "full_four_month_simulation_executed": False}
    (root / "validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
