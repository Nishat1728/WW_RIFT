"""Submit the locked 22-case fresh notebook matrix in staged Slurm arrays."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_ORDER = (12, 8, 4, 2)
CONCURRENCY = {12: 4, 8: 2, 4: 1, 2: 1}
EXPECTED_COUNTS = {2: 2, 4: 4, 8: 6, 12: 10}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def submit(command: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True, env=env)
    job_id = result.stdout.strip().split(";", 1)[0]
    if not job_id.isdigit():
        raise RuntimeError(f"Could not parse sbatch job ID from {result.stdout!r}")
    return job_id


def run(
    repository: Path,
    matrix_root: Path,
    preflight_root: Path,
    python: Path,
) -> dict[str, Any]:
    repository = repository.resolve()
    matrix_root = matrix_root.resolve()
    preflight_root = preflight_root.resolve()
    python = python.resolve()
    report = json.loads(
        (preflight_root / "preflight_report.json").read_text(encoding="utf-8")
    )
    if report.get("status") != "PASSED" or not report.get("submission_allowed"):
        raise RuntimeError(f"Preflight does not allow submission: {report.get('blockers')}")
    if Path(report["output_isolation"]["target_output_root"]).resolve() != matrix_root:
        raise ValueError("Preflight target output root does not match the submission root")
    if matrix_root.exists():
        raise FileExistsError(f"Fresh matrix root already exists: {matrix_root}")

    with (preflight_root / "array_index_map.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != sum(EXPECTED_COUNTS.values()):
        raise ValueError(f"Unexpected preflight row count: {len(rows)}")
    counts = {
        level: sum(int(row["level"]) == level for row in rows)
        for level in EXPECTED_COUNTS
    }
    if counts != EXPECTED_COUNTS:
        raise ValueError(f"Unexpected HUC-level counts: {counts}")

    matrix_root.mkdir(parents=True)
    logs_root = matrix_root / "logs"
    logs_root.mkdir()
    wrapper = repository / "hpc" / "run_locked_huc_notebook.slurm"
    base_env = os.environ.copy()
    base_env.update(
        {
            "RIFTPREP_REPOSITORY": str(repository),
            "RIFTPREP_MATRIX_ROOT": str(matrix_root),
            "RIFTPREP_PREFLIGHT_ROOT": str(preflight_root),
            "RIFTPREP_PYTHON": str(python),
        }
    )

    jobs = []
    dependency: str | None = None
    for level in STAGE_ORDER:
        members = sorted(
            (row for row in rows if int(row["level"]) == level),
            key=lambda row: int(row["global_index"]),
        )
        indices = [int(row["global_index"]) for row in members]
        cpus = max(int(row["cpus"]) for row in members)
        memory_gb = max(int(row["memory_gb"]) for row in members)
        time_limit = max((row["time_limit"] for row in members))
        env = base_env.copy()
        env["RIFTPREP_INDEX_LIST"] = ",".join(map(str, indices))
        command = [
            "sbatch",
            "--parsable",
            f"--job-name=rift22_huc{level}",
            f"--array=0-{len(indices) - 1}%{CONCURRENCY[level]}",
            f"--cpus-per-task={cpus}",
            f"--mem={memory_gb}G",
            f"--time={time_limit}",
            f"--output={logs_root}/huc{level}_%A_%a.out",
            f"--error={logs_root}/huc{level}_%A_%a.out",
            "--export=ALL",
        ]
        if dependency:
            command.append(f"--dependency=afterany:{dependency}")
        command.append(str(wrapper))
        job_id = submit(command, env)
        jobs.append(
            {
                "stage": f"huc{level}",
                "job_id": job_id,
                "global_indices": indices,
                "array": f"0-{len(indices) - 1}%{CONCURRENCY[level]}",
                "cpus_per_task": cpus,
                "memory_gb": memory_gb,
                "time_limit": time_limit,
                "dependency": dependency,
                "cache_mode": "fresh",
            }
        )
        dependency = job_id

    summary_command = [
        "sbatch",
        "--parsable",
        "--job-name=rift22_summary",
        "--partition=general",
        "--constraint=epyc128",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=2",
        "--mem=8G",
        "--time=01:00:00",
        f"--output={logs_root}/summary_%j.out",
        f"--error={logs_root}/summary_%j.out",
        f"--dependency=afterany:{dependency}",
        "--wrap",
        (
            f"cd '{repository}' && '{python}' hpc/aggregate_locked_huc_results.py "
            f"--matrix-root '{matrix_root}' --preflight-root '{preflight_root}'"
        ),
    ]
    summary_job = submit(summary_command, base_env)
    manifest = {
        "schema": "riftprep-locked-22-submission-v1",
        "created_utc": utc_now(),
        "repository": str(repository),
        "matrix_root": str(matrix_root),
        "preflight_root": str(preflight_root),
        "python": str(python),
        "fresh_downloads_required": True,
        "stage_order": list(STAGE_ORDER),
        "jobs": jobs,
        "summary_job_id": summary_job,
    }
    (matrix_root / "submission_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--matrix-root", required=True)
    parser.add_argument("--preflight-root", required=True)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    run(
        Path(args.repository),
        Path(args.matrix_root),
        Path(args.preflight_root),
        Path(args.python),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
