from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from hpc.run_locked_huc_notebook import resolve_rift_dem_path
from riftprep.config import SIMULATION_READY_DIRECTORY


def test_locked_runner_matches_new_notebook_output_directory(tmp_path: Path):
    """The locked validator must follow the notebook's canonical final-input root."""

    case_root = tmp_path / "case"
    simulation_ready = case_root / SIMULATION_READY_DIRECTORY
    simulation_ready.mkdir(parents=True)
    dem = simulation_ready / "dem.asc"
    dem.write_text("ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 30\nNODATA_value -9999\n0\n", encoding="utf-8")

    assert resolve_rift_dem_path(case_root) == dem
    assert not (case_root / "rift_inputs").exists()


def test_locked_runner_preserves_explicit_legacy_directory(tmp_path: Path):
    """Legacy resumed cases remain valid without changing the new-case contract."""

    case_root = tmp_path / "legacy_case"
    legacy = case_root / "rift_inputs"
    legacy.mkdir(parents=True)
    dem = legacy / "dem.asc"
    dem.write_text("legacy", encoding="utf-8")

    assert resolve_rift_dem_path(case_root) == dem
