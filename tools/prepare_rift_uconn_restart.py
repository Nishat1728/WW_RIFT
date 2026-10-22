"""Create a NEW, patched RIFT-UConn source copy; never edit the supplied checkout.

The patch anchors target the RIFT-UConn c111780 source layout and fail closed on
unrecognized source changes. Build the destination normally using CMake/CUDA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def _replace(text, old, new, label):
    if text.count(old) != 1:
        raise ValueError(f"Unsupported RIFT-UConn source: expected one {label} anchor; found {text.count(old)}.")
    return text.replace(old, new, 1)


def transform_sources(header: str, simulator: str, main: str) -> tuple[str, str, str]:
    simulator = _replace(simulator, '#include <math.h>', '#include <math.h>\n#include <cmath>\n#include <algorithm>', "math includes")
    header = _replace(header, '#include <vector>', '#include <vector>\n#include <cstdint>\n#include <utility>', "includes")
    header = _replace(header, '    void WriteHotstart(void);', '''    // WW_RIFT complete restart v1.
    double forcing_time_origin_s = 0.0;
    bool save_final_hotstart = false;
    bool restart_reset_diagnostics = false;
    bool restart_audit_loaded_state = false;
    std::string restart_write_path;
    std::vector<std::pair<std::string, double*>> RestartFields();
    std::uint32_t RestartFlags() const;
    void WriteHotstart(void);''', "restart declarations")
    anchor = '\tif (h_print || q_print || ws_print || check_volume || save_hotstart) {'
    simulator = _replace(simulator, anchor, '''    forcing_time_origin_s = cfg.keyExists("forcing.time_origin_s") ?
        cfg.getValueOfKey<double>("forcing.time_origin_s") : 0.0;
    save_final_hotstart = cfg.keyExists("output.final_hotstart") && cfg.getValueOfKey<bool>("output.final_hotstart");
    restart_reset_diagnostics = cfg.keyExists("restart.reset_diagnostics") && cfg.getValueOfKey<bool>("restart.reset_diagnostics");
    restart_audit_loaded_state = cfg.keyExists("restart.audit_loaded_state") && cfg.getValueOfKey<bool>("restart.audit_loaded_state");
    if (!std::isfinite(forcing_time_origin_s)) throw std::runtime_error("Invalid forcing.time_origin_s");
    dt_print = tf-t0; t_print = tf; count_print = 0;
''' + anchor, "restart settings")
    # Offset the evaluation clock, NOT the raw/processed forcing files. This
    # covers HDF5/grid/uniform rain, hydrographs, coastal and external drainage.
    for name in ("hyetograph_series", "drain_series", "surge_series"):
        simulator = _replace(simulator, f"{name}->update(t0);", f"{name}->update(t0 - forcing_time_origin_s);", f"{name} init")
        simulator = _replace(simulator, f"{name}->update(t);", f"{name}->update(t - forcing_time_origin_s);", f"{name} update")
    for name in ("item.series", "hyetograph", "drain"):
        simulator = _replace(simulator, f"{name}.InterpolateRate(t);", f"{name}.InterpolateRate(t - forcing_time_origin_s);", f"{name} clock")
    simulator = _replace(simulator, '\tt += dt;', '''    // Land on phase boundaries and scheduled outputs exactly, including the
    // same checkpoint boundary in an uninterrupted equivalence test.
    if (dt_print > 0 && t_print > t + 1.e-9) dt = std::min(dt, t_print - t);
    dt = std::min(dt, tf - t);
    if (!(dt > 0) || !std::isfinite(dt)) throw std::runtime_error("Invalid remaining timestep");
\tt += dt;''', "exact end step")
    # Keep the existing initial epsilon-time output; thereafter use exact
    # scheduled times. The first t_print == t0 advances to t0+interval.
    simulator = _replace(simulator, 'if (t > t_print) {', 'if (t >= t_print - 1.e-9) {', "snapshot time")
    simulator = _replace(simulator, '\tdouble elapsed_total = EndTimer();', '''    if (save_final_hotstart) {
        if (std::abs(t-tf) > 1.e-6) throw std::runtime_error("Refusing final checkpoint before requested end time");
        restart_write_path = output_file + "/final_hotstart.bin";
        WriteHotstart();
        restart_write_path.clear();
    }
\tdouble elapsed_total = EndTimer();''', "final checkpoint")
    simulator = _replace(simulator, '        ReadHotstart(read_hotstart);', '''        ReadHotstart(read_hotstart);
        if (restart_audit_loaded_state) {
            restart_write_path = output_file + "/restart_loaded.bin";
            WriteHotstart(); restart_write_path.clear();
        }''', "restart audit")
    marker = '// Simulator::WriteHotstart'
    if simulator.count(marker) != 1 or not simulator.rstrip().endswith('}'):
        raise ValueError("Unsupported hotstart serialization layout")
    simulator = simulator[:simulator.index(marker)] + '#include "complete_restart.inc"\n'
    main = _replace(main, '    if (argc != 2) {', '''    if (argc == 2 && std::string(argv[1]) == "--restart-capabilities") {
        std::cout << "{\\"schema\\":\\"rift-uconn-complete-restart-v1\\",\\"forcing_time_origin\\":true,\\"exact_final_checkpoint\\":true}" << std::endl;
        return 0;
    }
    if (argc != 2) {''', "capability probe")
    return header, simulator, main


def prepare(source: Path, destination: Path, assets: Path | None = None) -> Path:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or destination == source or destination.is_relative_to(source):
        raise ValueError("Destination must be a new directory outside the source checkout.")
    files = {name: (source / name).read_text() for name in ("simulator.h", "simulator.cu", "main.cc")}
    transformed = transform_sources(files["simulator.h"], files["simulator.cu"], files["main.cc"])
    assets = assets or Path(__file__).resolve().parents[1] / "integrations/rift_uconn"
    implementation = assets / "complete_restart.inc"
    if not implementation.is_file():
        raise FileNotFoundError(implementation)
    destination.mkdir(parents=True)
    for item in source.iterdir():
        if item.is_file() and (item.suffix in {".cu", ".cuh", ".cc", ".h", ".hpp", ".cpp"}
                               or item.name == "CMakeLists.txt" or item.name.startswith("LICENSE")):
            shutil.copy2(item, destination / item.name)
    for name, text in zip(("simulator.h", "simulator.cu", "main.cc"), transformed):
        (destination / name).write_text(text)
    shutil.copy2(implementation, destination / implementation.name)
    receipt = {"schema": "riftprep-solver-integration-v1", "restart_contract": "rift-uconn-complete-restart-v1",
               "original_sha256": {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in files},
               "patched_sha256": {name: hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in files}}
    (destination / "restart_integration.json").write_text(json.dumps(receipt, indent=2))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.source, args.destination))
