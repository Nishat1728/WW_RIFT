#!/bin/bash
# Submit with case-specific --mem/--time and RIFTPREP_CASE.
#SBATCH --job-name=rift30
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH --time=12:00:00

set -euo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY}"
OUTPUT_ROOT="${RIFTPREP_OUTPUT_ROOT:?Set RIFTPREP_OUTPUT_ROOT}"
CASE="${RIFTPREP_CASE:?Set RIFTPREP_CASE}"
PYTHON="${RIFTPREP_PYTHON:-python}"

cd "$REPOSITORY"
export PYTHONPATH="$REPOSITORY${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"
export RIFTPREP_WARP_THREADS="${RIFTPREP_WARP_THREADS:-16}"
export RIFTPREP_HDF5_STAGING_DIR="${RIFTPREP_HDF5_STAGING_DIR:-${SLURM_TMPDIR:-/tmp}}"

echo "RIFT case: $CASE"
echo "RIFT HDF5 staging directory: $RIFTPREP_HDF5_STAGING_DIR"
echo "Slurm time limit: $(squeue -h -j "$SLURM_JOB_ID" -o %l)"

"$PYTHON" hpc/run_six_case_notebook.py \
  --repository "$REPOSITORY" --output-root "$OUTPUT_ROOT" --case "$CASE"
