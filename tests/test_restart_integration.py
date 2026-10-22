from pathlib import Path
import shutil
import subprocess
import pytest

from tools.prepare_rift_uconn_restart import prepare, transform_sources


def test_unknown_solver_source_is_rejected_without_mutating_or_partial_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    before = {}
    for name in ("simulator.h", "simulator.cu", "main.cc"):
        before[name] = "// unknown newer solver source\n"
        (source / name).write_text(before[name])
    destination = tmp_path / "new_copy"
    with pytest.raises(ValueError, match="Unsupported RIFT-UConn"):
        prepare(source, destination)
    assert not destination.exists()
    assert all((source / name).read_text() == text for name, text in before.items())
    with pytest.raises(ValueError, match="new directory"):
        prepare(source, source)


def test_complete_state_asset_contains_physical_states_and_forcing_contract():
    root = Path(__file__).resolve().parents[1]
    implementation = (root / "integrations/rift_uconn/complete_restart.inc").read_text()
    for name in ("water_surface", "momentum_x", "momentum_y", "cumulative_infiltration",
                 "soil_saturation", "moisture_deficit", "urban_storage", "WWRIFT01"):
        assert name in implementation
    for physical in ("dev_F", "dev_soil_saturation", "dev_dtheta", "dev_urban_storage", "dev_hu", "dev_hv"):
        assert f"clear({physical})" not in implementation
    patcher = (root / "tools/prepare_rift_uconn_restart.py").read_text()
    assert "forcing_time_origin_s" in patcher
    assert "final_hotstart.bin" in patcher
    assert "restart-capabilities" in patcher


def test_actual_cpp_checkpoint_reader_writer_roundtrip_with_host_memory_stubs(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("A C++17 compiler is required for native serialization validation.")
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "native_restart_test"
    subprocess.run([compiler, "-std=c++17", "-O0", "-I", str(root / "integrations/rift_uconn"),
                    str(root / "tests/native_restart_roundtrip.cc"), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    result = subprocess.run([str(executable), str(tmp_path)], check=True, capture_output=True, text=True)
    assert "NATIVE_COMPLETE_STATE_ROUNDTRIP_PASSED" in result.stdout
