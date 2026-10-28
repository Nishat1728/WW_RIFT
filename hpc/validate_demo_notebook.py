"""Run the HUC12 demo and validate the two-stage input contract.

Run on a compute node from the repository: python -m hpc.validate_demo_notebook
No account-specific paths or cached products are required by default. Explicit
NLCD-only local reuse is available for an authorized service-outage test.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import configparser
import argparse
import shutil
from copy import deepcopy
from pathlib import Path

import h5py
import nbformat
import numpy as np
import yaml
from nbclient import NotebookClient

from hpc.run_locked_huc_notebook import validate_case
from riftprep.spec import load_case


def preserved_nlcd_inputs(source_case: Path, repository: Path) -> dict:
    """Explicit test-only local input; verify against its original raw catalog."""
    catalog_path = source_case / "quality_control/provenance/raw_products.json"
    catalog = json.loads(catalog_path.read_text())
    staging = repository / "demo_inputs" / "preserved_nlcd"
    staging.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for variable in ("cover", "impervious", "canopy"):
        source = source_case / "raw/land_cover" / f"nlcd_{variable}_2021_native.tif"
        record = next((row for row in catalog if Path(row["path"]).resolve() == source.resolve()), None)
        if record is None:
            raise ValueError(f"No original raw provenance for {source}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise ValueError(f"Original NLCD checksum mismatch: {source}")
        target = staging / source.name
        shutil.copy2(source, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Preserved NLCD copy checksum mismatch: {target}")
        artifacts.append({"variable": variable, "file": target.name, "sha256": digest})
    receipt = {"reused": True, "product": "Legacy NLCD 2021", "source_case": source_case.name,
               "source_catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
               "raw_product_modified": False, "artifacts": artifacts,
               "reason": "Explicit user-approved NLCD-only exception during provider outage"}
    (staging / "reuse_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def portable_notebook(notebook, repository, summary):
    """Preserve real plots/results, replacing only machine-specific display paths."""
    result = deepcopy(notebook)
    replacements = [
        (str(repository), "/path/to/WW_RIFT"),
        (repository.as_posix(), "/path/to/WW_RIFT"),
        (str(repository.parent.parent), "/path/to/rift_workspace"),
        (repository.parent.parent.as_posix(), "/path/to/rift_workspace"),
    ]
    def clean(value):
        if isinstance(value, str):
            for before, after in replacements:
                value = value.replace(before, after)
            return value
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: item if key.startswith("image/") else clean(item)
                    for key, item in value.items() if key != "application/vnd.jupyter.widget-view+json"}
        return value
    for cell in result.cells:
        if cell.cell_type == "code":
            cell.outputs = [nbformat.from_dict(clean(dict(output))) for output in cell.outputs]
    result.metadata.pop("widgets", None)
    result.cells.insert(0, nbformat.v4.new_markdown_cell(
        "# Executed HUC12 example\n\n"
        f"This is the successful input-preparation run for **{summary['huc_code']}**. "
        + ("**NLCD-only exception:** verified previously downloaded NLCD rasters were used as "
           "explicit local inputs during the service outage. All other provider inputs were "
           "downloaded fresh. This is not an entirely fresh benchmark. "
           if summary.get("nlcd_reuse", {}).get("reused") else "All provider inputs were downloaded fresh. ")
        + "Plots, timings and numerical results are from the real run. Machine-specific output paths "
        "are replaced with portable placeholders, and live-widget view references are omitted. "
        "Use `HUC12_Demo.ipynb` for the clean rerunnable notebook.\n\n"
        "**The notebook prepared both configurations; it did not run the full four-month GPU "
        "spin-up or transient simulation.**"))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nlcd-source-case", type=Path, help=(
        "Explicit, user-approved NLCD-only reuse: case containing original 2021 raw rasters "
        "and their checksum catalog. All other inputs remain fresh. No automatic fallback."
    ))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    settings = json.loads((repository / "examples/huc12_demo_settings.json").read_text())
    requested = load_case(repository / "examples" / settings["configuration"])
    reports = repository / "demo_cases" / "validation"
    reports.mkdir(parents=True, exist_ok=True)
    os.environ.pop("RIFTPREP_CASE_CONFIG", None)
    os.environ["PYTHONPATH"] = str(repository)
    notebook = nbformat.read(repository / "examples" / "HUC12_Demo.ipynb", as_version=4)
    started = time.perf_counter()
    reuse = {"reused": False}
    if args.nlcd_source_case:
        reuse = preserved_nlcd_inputs(args.nlcd_source_case.resolve(), repository)
        index = next(i for i, cell in enumerate(notebook.cells)
                     if cell.cell_type == "code" and "widget_values = snapshot_widget_values" in cell.source)
        notebook.cells[index:index] = [nbformat.v4.new_markdown_cell(
            "### Explicit NLCD-only reuse for this validation run\n\n"
            "The selected legacy NLCD service is unavailable. The user approved reusing its "
            "previously downloaded 2021 rasters, verified against their original SHA-256 catalog. "
            "They are copied unchanged, and active-cell coverage is checked during processing. "
            "Every other input is downloaded fresh. The clean demo keeps the online source as default."
        ), nbformat.v4.new_code_cell(
            'LAND_SOURCE_WIDGET.value = "local"\n'
            'LAND_COVER_PATH_WIDGET.value = "demo_inputs/preserved_nlcd/nlcd_cover_2021_native.tif"\n'
            'LAND_IMPERVIOUS_PATH_WIDGET.value = "demo_inputs/preserved_nlcd/nlcd_impervious_2021_native.tif"\n'
            'LAND_CANOPY_PATH_WIDGET.value = "demo_inputs/preserved_nlcd/nlcd_canopy_2021_native.tif"\n'
            'display(JSON(json.loads(Path("demo_inputs/preserved_nlcd/reuse_receipt.json").read_text())))'
        )]
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
    case_root = repository / "demo_cases" / settings["case_name"]
    config = yaml.safe_load((case_root / "quality_control" / "resolved_case.yaml").read_text())
    summary = validate_case(case_root, config, elapsed)
    checks = summary["failures"]
    simulation = config["simulation"]
    duration = simulation["end_time_s"] - simulation["start_time_s"]
    if duration != 172800:
        checks.append("simulation duration does not match the 48-hour rainfall period")
    resolved = load_case(case_root / "quality_control/resolved_case.yaml")
    soil_coverage = json.loads((case_root / "quality_control/soil_coverage.json").read_text())
    expected_fill = settings.get("manual_soil_fill_values", {})
    if resolved.soil.options.get("manual_fill_values", {}) != expected_fill:
        checks.append("soil constants differ from the explicit demo assumptions")
    for variable, value in expected_fill.items():
        before = soil_coverage["coverage_before_manual_resolution"][variable]
        applied = soil_coverage["manual_resolutions"].get(variable, {})
        if (applied.get("value") != value
                or applied.get("filled_active_nodes") != before["missing_active_nodes"]):
            checks.append(f"soil {variable} replacement did not match only the original missing cells")
    if soil_coverage.get("residual_porosity_check", {}).get("values_modified"):
        checks.append("demo authorization covers missing soil cells only, not nonmissing pair corrections")
    if resolved.domain.huc_code != requested.domain.huc_code:
        checks.append("executed HUC12 differs from the published configuration")
    if not resolved.spinup.enabled or not resolved.inflows.enabled:
        checks.append("the demo did not exercise historical spin-up and observed event inflows")
    event_cfg, spinup_cfg = configparser.ConfigParser(), configparser.ConfigParser()
    event_cfg.read(case_root / "rift_case.cfg")
    spinup_cfg.read(case_root / "rift_spinup.cfg")
    plan = json.loads((case_root / "simulation_plan.json").read_text())
    spinup_report = json.loads((case_root / "quality_control/spinup_baseflow.json").read_text())
    offset = resolved.spinup.duration_s
    if (event_cfg.getfloat("simulation", "simulation.start_time_s") != offset
            or event_cfg.getfloat("simulation", "simulation.end_time_s") != offset+duration
            or event_cfg.getfloat("forcing", "forcing.time_origin_s") != offset
            or not event_cfg.get("restart", "restart.hotstart").endswith("spinup_hotstart.bin")):
        checks.append("transient CFG does not implement complete hotstart continuation on the correct clock")
    if (spinup_cfg.getboolean("modules", "modules.rainfall")
            or not spinup_cfg.getboolean("modules", "modules.inflows")
            or spinup_cfg.getfloat("outputs", "output.interval_s") != 86400
            or not spinup_cfg.getboolean("outputs", "output.final_hotstart")):
        checks.append("spin-up CFG is not rain-free/daily-output/exact-end-hotstart")
    if (spinup_report.get("status") != "PASSED" or spinup_report.get("raw_product_modified") is not False
            or spinup_report.get("requested_history_years") != 20
            or len(spinup_report.get("stations", [])) != len(settings["inflow_stations"])):
        checks.append("historical baseflow verification failed")
    if plan.get("schema") != "riftprep-two-stage-v2":
        checks.append("the example used an obsolete simulation plan")
    for phase in (event_cfg, spinup_cfg):
        if not phase.getboolean("outputs", "output.hotstart"):
            checks.append("default periodic hotstart saving was not enabled")
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
        "huc_code": settings["huc_code"], "huc_name": settings["huc_name"],
        "simulation_duration_seconds": duration,
        "rainfall_records": int(times.size), "raw_checksums_verified": len(catalog),
        "spinup_enabled": True, "spinup_duration_seconds": offset,
        "spinup_history_years": 20, "baseflow_stations": spinup_report["stations"],
        "event_inflows_enabled": True, "simulation_plan_schema": plan["schema"],
        "two_cfgs_verified": True, "full_gpu_simulation_executed": False,
        "nlcd_reuse": reuse, "all_provider_inputs_fresh": not reuse["reused"],
        "soil_coverage_before_manual_resolution": soil_coverage["coverage_before_manual_resolution"],
        "soil_residual_porosity_check": soil_coverage.get("residual_porosity_check", {}),
        "source_code_sha256": hashlib.sha256("\n\n".join(
            cell.source for cell in notebook.cells if cell.cell_type == "code"
        ).encode()).hexdigest(),
    })
    (reports / "demo_validation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    if checks:
        raise RuntimeError("Demo validation failed: " + "; ".join(checks))
    nbformat.write(portable_notebook(notebook, repository, summary), reports / "HUC12_Demo_Executed.ipynb")
    print("TWO_STAGE_HUC12_EXAMPLE_PASSED; all_provider_inputs_fresh="
          + str(not reuse["reused"]), flush=True)


if __name__ == "__main__":
    main()
