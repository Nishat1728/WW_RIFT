#!/bin/bash
# Submit with RIFTPREP_REPOSITORY and RIFTPREP_PYTHON set for your installation.
#SBATCH --job-name=rift-demo-release
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:45:00

set -euo pipefail
cd "${RIFTPREP_REPOSITORY:?Set the candidate repository path}"
RIFTPREP_PYTHON="${RIFTPREP_PYTHON:?Set the environment Python executable}"
export PATH="$(dirname "$RIFTPREP_PYTHON"):$PATH"
export PYTHONPATH="$PWD"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export OMP_NUM_THREADS="$SLURM_CPUS_PER_TASK"
export RIFTPREP_HDF5_STAGING_DIR="${SLURM_TMPDIR:-/tmp}"

"$RIFTPREP_PYTHON" -m compileall -q riftprep tools hpc tests
"$RIFTPREP_PYTHON" -m pytest -q
before="$(sha256sum RIFT_Input_Builder.ipynb examples/HUC12_Demo.ipynb)"
"$RIFTPREP_PYTHON" tools/build_general_notebook.py
after="$(sha256sum RIFT_Input_Builder.ipynb examples/HUC12_Demo.ipynb)"
test "$before" = "$after"
"$RIFTPREP_PYTHON" -m riftprep.cli validate-config examples/demo_huc12_180101080101.yaml
"$RIFTPREP_PYTHON" -m hpc.validate_demo_notebook
echo "DEMO_RELEASE_CHECKS_PASSED"
