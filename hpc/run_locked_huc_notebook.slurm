#!/bin/bash
# Resource requests and array bounds are supplied by submit_locked_huc_matrix.py.
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1

set -Eeuo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY}"
MATRIX_ROOT="${RIFTPREP_MATRIX_ROOT:?Set RIFTPREP_MATRIX_ROOT}"
PREFLIGHT_ROOT="${RIFTPREP_PREFLIGHT_ROOT:?Set RIFTPREP_PREFLIGHT_ROOT}"
PYTHON="${RIFTPREP_PYTHON:?Set RIFTPREP_PYTHON}"
INDEX_LIST="${RIFTPREP_INDEX_LIST:?Set RIFTPREP_INDEX_LIST}"

IFS=',' read -r -a GLOBAL_INDICES <<< "$INDEX_LIST"
GLOBAL_INDEX="${GLOBAL_INDICES[$SLURM_ARRAY_TASK_ID]}"

cd "$REPOSITORY"
export PATH="$(dirname "$PYTHON"):$PATH"
export PYTHONPATH="$REPOSITORY${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"
export RIFTPREP_WARP_THREADS="$SLURM_CPUS_PER_TASK"

# Login-node /tmp can be a small shared filesystem.  Keep future RIFT
# provider staging and Python temporary files on the dedicated shared
# RIFT filesystem instead.  Each array task gets an isolated directory so
# concurrent jobs cannot collide.  Existing running jobs are unaffected.
RIFTPREP_SHARED_TMP_ROOT="${RIFTPREP_SHARED_TMP_ROOT:-/shared/pqz25001/nqw25003/work/RIFT_notebook/tmp}"
RIFTPREP_SHARED_TMP="$RIFTPREP_SHARED_TMP_ROOT/${SLURM_JOB_ID}_${SLURM_ARRAY_TASK_ID:-0}"
mkdir -p "$RIFTPREP_SHARED_TMP"
chmod 700 "$RIFTPREP_SHARED_TMP"
export TMPDIR="$RIFTPREP_SHARED_TMP"
export TEMP="$RIFTPREP_SHARED_TMP"
export TMP="$RIFTPREP_SHARED_TMP"
export RIFTPREP_HDF5_STAGING_DIR="${RIFTPREP_HDF5_STAGING_DIR:-$RIFTPREP_SHARED_TMP}"
trap 'rm -rf "$RIFTPREP_SHARED_TMP"' EXIT

echo "Global matrix index: $GLOBAL_INDEX"
echo "Array task: $SLURM_ARRAY_TASK_ID"
echo "Fresh provider downloads: required"
echo "HDF5 staging directory: $RIFTPREP_HDF5_STAGING_DIR"
echo "Slurm time limit: $(squeue -h -j "$SLURM_JOB_ID" -o %l)"

"$PYTHON" hpc/run_locked_huc_notebook.py \
  --repository "$REPOSITORY" \
  --preflight-root "$PREFLIGHT_ROOT" \
  --matrix-root "$MATRIX_ROOT" \
  --case-index "$GLOBAL_INDEX"
