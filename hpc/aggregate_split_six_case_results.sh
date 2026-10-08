#!/bin/bash
# Aggregate a six-case matrix when the very large HUC2 case uses home storage.

set -euo pipefail

REPOSITORY="${RIFTPREP_REPOSITORY:?Set RIFTPREP_REPOSITORY}"
SCRATCH_ROOT="${RIFTPREP_OUTPUT_ROOT:?Set RIFTPREP_OUTPUT_ROOT}"
HUC2_ROOT="${RIFTPREP_HUC2_OUTPUT_ROOT:?Set RIFTPREP_HUC2_OUTPUT_ROOT}"
PYTHON="${RIFTPREP_PYTHON:-python}"

mkdir -p "$SCRATCH_ROOT/benchmark_results"
cp "$HUC2_ROOT/benchmark_results/huc2_new_england.json" \
   "$SCRATCH_ROOT/benchmark_results/huc2_new_england.json"
cp "$HUC2_ROOT/benchmark_results/huc2_new_england.csv" \
   "$SCRATCH_ROOT/benchmark_results/huc2_new_england.csv"

cd "$REPOSITORY"
export PYTHONPATH="$REPOSITORY${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON" hpc/run_six_case_notebook.py \
  --repository "$REPOSITORY" --output-root "$SCRATCH_ROOT" --case aggregate
