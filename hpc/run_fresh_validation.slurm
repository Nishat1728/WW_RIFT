#!/bin/bash
#SBATCH --job-name=riftprep-fresh
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=06:00:00
#SBATCH --output=riftprep_fresh_%j.out

set -euo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY to the synced repository path}"
OUTPUT_ROOT="${RIFTPREP_OUTPUT_ROOT:?Set RIFTPREP_OUTPUT_ROOT to a new or marked validation directory}"
PYTHON="${RIFTPREP_PYTHON:-python}"

cd "$REPOSITORY"
export PYTHONPATH="$REPOSITORY${PYTHONPATH:+:$PYTHONPATH}"
export MPLBACKEND=Agg
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"

"$PYTHON" hpc/run_fresh_validation_matrix.py --output-root "$OUTPUT_ROOT" --case all
