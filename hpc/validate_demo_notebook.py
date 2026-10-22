"""Run the public demo notebook and verify its fresh 48-hour, 30 m output contract.

Run on a compute node from the repository: python -m hpc.validate_demo_notebook
No account-specific paths or cached benchmark products are required.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import h5py
import nbformat
import numpy as np
import yaml
from nbclient import NotebookClient

from hpc.run_locked_huc_notebook import validate_case


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    reports = repository / "demo_cases" / "validation"
    reports.mkdir(parents=True, exist_ok=True)
    os.environ.pop("RIFTPREP_CASE_CONFIG", None)
    os.environ["PYTHONPATH"] = str(repository)
    notebook = nbformat.read(repository / "examples" / "HUC12_Demo.ipynb", as_version=4)
    started = time.perf_counter()
    try:
        NotebookClient(
            notebook, timeout=3600, kernel_name="python3",
            resources={"metadata": {"path": str(repository / "examples")}},
            on_cell_start=lambda cell, cell_index: print(
                f"DEMO_CELL {cell_index + 1}/{len(notebook.cells)}", flush=True
            ),
        ).execute()
    finally:
        nbformat.write(notebook, reports / "HUC12_Demo_executed.ipynb")
    elapsed = time.perf_counter() - started
    case_root = repository / "demo_cases" / "demo_huc12_180101080101"
    config = yaml.safe_load((case_root / "quality_control" / "resolved_case.yaml").read_text())
    summary = validate_case(case_root, config, elapsed)
    checks = summary["failures"]
    simulation = config["simulation"]
    if simulation["end_time_s"] - simulation["start_time_s"] != 172800:
        checks.append("simulation duration does not match the 48-hour rainfall period")
    if "simulation.end_time_s = 172800" not in (case_root / "rift_case.cfg").read_text():
        checks.append("written RIFT configuration does not contain the 48-hour end time")
    with h5py.File(case_root / "simulation_ready" / "rainfall_ats.h5", "r") as forcing:
        times = np.asarray(forcing["time [s]"])
    if times.size < 48 or not np.all(np.diff(times) > 0):
        checks.append("rainfall time series does not provide ordered records for 48 hours")
    catalog = json.loads((case_root / "quality_control/provenance/raw_products.json").read_text())
    for artifact in catalog:
        digest = hashlib.sha256()
        with Path(artifact["path"]).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != artifact["sha256"]:
            checks.append(f"raw checksum changed: {Path(artifact['path']).name}")
    summary.update({
        "status": "passed" if not checks else "failed",
        "huc_code": "180101080101", "simulation_duration_seconds": 172800,
        "rainfall_records": int(times.size), "raw_checksums_verified": len(catalog),
    })
    (reports / "demo_validation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    if checks:
        raise RuntimeError("Demo validation failed: " + "; ".join(checks))


if __name__ == "__main__":
    main()
