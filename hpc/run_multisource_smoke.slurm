#!/bin/bash
# Fresh, small live-provider smoke test for optional DEM/soil/rainfall adapters.
# Required: RIFTPREP_REPOSITORY, RIFTPREP_PYTHON, RIFTPREP_SMOKE_OUTPUT.
# Optional: RIFTPREP_EXTRA_PYTHONPATH for isolated adapter dependencies.
#SBATCH --job-name=riftprep-sources
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --time=01:00:00

set -euo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY}"
PYTHON="${RIFTPREP_PYTHON:?Set RIFTPREP_PYTHON}"
OUTPUT="${RIFTPREP_SMOKE_OUTPUT:?Set RIFTPREP_SMOKE_OUTPUT}"
EXTRA_PYTHONPATH="${RIFTPREP_EXTRA_PYTHONPATH:-}"

cd "$REPOSITORY"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export PYTHONPATH="$REPOSITORY${EXTRA_PYTHONPATH:+:$EXTRA_PYTHONPATH}"

"$PYTHON" hpc/smoke_multisource_adapters.py "$OUTPUT"
