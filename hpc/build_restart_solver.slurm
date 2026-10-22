#!/bin/bash
#SBATCH --job-name=rift-restart-build
#SBATCH --partition=general
#SBATCH --constraint=epyc128
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=12G
#SBATCH --time=00:20:00
set -euo pipefail
module purge
module load gcc/15.3.0 cuda/13.0 openmpi/5.0.10 hdf5/1.14.5-RHEL9 cmake/3.31.12
cd "${RIFTPREP_REPOSITORY:?}"
solver_directory="$PWD/.validation_restart/solver_${SLURM_JOB_ID}"
build_directory="$PWD/.validation_restart/build_${SLURM_JOB_ID}"
"${RIFTPREP_PYTHON:?}" tools/prepare_rift_uconn_restart.py --source "${RIFT_SOURCE:?}" --destination "$solver_directory"
cmake -S "$solver_directory" -B "$build_directory" \
  -DCMAKE_CUDA_ARCHITECTURES=80 -DCUDA_SAMPLES_INC="${CUDA_SAMPLES_INC:?}" \
  -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ -DCMAKE_CUDA_HOST_COMPILER=g++
cmake --build "$build_directory" --parallel "$SLURM_CPUS_PER_TASK"
"$build_directory/rift" --restart-capabilities
echo COMPLETE_RESTART_BUILD_PASSED
