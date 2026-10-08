#!/bin/bash
# Run syntax, unit, notebook-reproducibility, and wheel checks on Storrs.
# Required: RIFTPREP_REPOSITORY and RIFTPREP_PYTHON.
# Optional: RIFTPREP_EXTRA_PYTHONPATH for isolated adapter dependencies.
#SBATCH --job-name=riftprep-release
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --time=00:30:00

set -euo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY}"
PYTHON="${RIFTPREP_PYTHON:?Set RIFTPREP_PYTHON}"
EXTRA_PYTHONPATH="${RIFTPREP_EXTRA_PYTHONPATH:-}"

cd "$REPOSITORY"
export PYTHONDONTWRITEBYTECODE=1
export MPLBACKEND=Agg
export PYTHONPATH="$REPOSITORY${EXTRA_PYTHONPATH:+:$EXTRA_PYTHONPATH}"

before_notebook="$(sha256sum RIFT_Input_Builder.ipynb | cut -d' ' -f1)"
"$PYTHON" -m compileall -q riftprep tools hpc tests
"$PYTHON" -m pytest -q
"$PYTHON" tools/build_general_notebook.py
after_notebook="$(sha256sum RIFT_Input_Builder.ipynb | cut -d' ' -f1)"
test "$before_notebook" = "$after_notebook"

wheel_dir="${SLURM_TMPDIR:-/tmp}/riftprep-wheel"
mkdir -p "$wheel_dir"
"$PYTHON" -m pip wheel . --no-deps --wheel-dir "$wheel_dir"
test -n "$(find "$wheel_dir" -maxdepth 1 -name 'riftprep-*.whl' -print -quit)"

echo "RELEASE_CHECKS_PASSED"
