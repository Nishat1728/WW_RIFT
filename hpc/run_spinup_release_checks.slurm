#!/bin/bash
#SBATCH --job-name=rift-spinup-check
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:45:00
set -euo pipefail
cd "${RIFTPREP_REPOSITORY:?Set the candidate repository path}"
export PYTHONPATH="$PWD"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"
"${RIFTPREP_PYTHON:?}" -m compileall -q riftprep tools hpc tests
"$RIFTPREP_PYTHON" -m pytest -q
before="$(sha256sum RIFT_Input_Builder.ipynb examples/HUC12_Demo.ipynb)"
"$RIFTPREP_PYTHON" tools/build_general_notebook.py
test "$before" = "$(sha256sum RIFT_Input_Builder.ipynb examples/HUC12_Demo.ipynb)"
"$RIFTPREP_PYTHON" -m hpc.validate_spinup_history "$PWD/.validation_spinup/history_${SLURM_JOB_ID}"
echo SPINUP_RELEASE_CHECKS_PASSED
