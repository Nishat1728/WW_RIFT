#!/bin/bash
#SBATCH --job-name=rift-restart-gpu
#SBATCH --partition=general-gpu
#SBATCH --constraint="a100|a30|l40"
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=12G
#SBATCH --time=00:03:00
set -euo pipefail
module purge
module load gcc/15.3.0 cuda/13.0 openmpi/5.0.10 hdf5/1.14.5-RHEL9
cd "${RIFTPREP_REPOSITORY:?}"
export PYTHONPATH="$PWD"
export MPLBACKEND=Agg
"${RIFTPREP_PYTHON:?}" -m hpc.validate_complete_restart "${RIFT_EXECUTABLE:?}" "$PWD/.validation_restart/gpu_${SLURM_JOB_ID}"
echo COMPLETE_RESTART_GPU_PASSED
