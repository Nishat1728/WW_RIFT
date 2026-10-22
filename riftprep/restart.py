"""Inspect/copy versioned complete solver checkpoints without rewriting state."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
from pathlib import Path

CONTRACT = "rift-uconn-complete-restart-v1"


def checkpoint_fields(flags: int) -> list[str]:
    fields = ["water_surface", "momentum_x", "momentum_y"]
    if flags & 1:
        fields += ["cumulative_infiltration"]
    if flags & 2:
        fields += ["soil_saturation", "moisture_deficit"]
    if flags & 8:
        fields += ["urban_storage", "urban_cumulative_outflow", "urban_peak_capture", "urban_peak_release"]
    if flags & 16:
        fields += ["water_depth"]
    if flags & 32:
        fields += ["unit_discharge"]
    if flags & 64:
        fields += ["peak_depth", "peak_discharge", "time_peak", "time_dry"]
    if flags & 128:
        fields += ["arrival_time"]
    return fields


def inspect_checkpoint(path: str | Path, *, expected_time_s=None, expected_nodes=None,
                       include_arrays=False) -> dict:
    """Validate structure, named states, exact clock and SHA-256; bounded memory.

    Contract v1 targets Linux little-endian IEEE-754 doubles/int32/bool8, matching
    the checked CUDA solver build. Legacy unversioned binaries are rejected.
    """
    import numpy as np
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        def read(size):
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("Truncated complete hotstart.")
            digest.update(data)
            return data
        def number(fmt):
            return struct.unpack("<"+fmt, read(struct.calcsize("<"+fmt)))[0]
        def string():
            size = number("I")
            if size > 4096:
                raise ValueError("Invalid checkpoint string length.")
            return read(size)
        if read(8) != b"WWRIFT01" or number("I") != 1:
            raise ValueError("Not a complete versioned hotstart; regenerate spin-up with the integrated solver.")
        flags = number("I")
        stamp, dt = number("d"), number("d")
        if flags & ~255 or not math.isfinite(stamp) or not math.isfinite(dt) or dt <= 0:
            raise ValueError("Invalid checkpoint flags/time/timestep.")
        if expected_time_s is not None and not math.isclose(stamp, expected_time_s, abs_tol=1e-6, rel_tol=0):
            raise ValueError("Checkpoint is not at the exact requested spin-up end.")
        geometry = string()
        if len(geometry) < 8:
            raise ValueError("Missing checkpoint geometry.")
        nx, ny = struct.unpack("<ii", geometry[:8])
        if nx < 2 or ny < 2 or (expected_nodes is not None and (nx, ny) != tuple(expected_nodes)):
            raise ValueError("Checkpoint grid does not match this case.")
        names = checkpoint_fields(flags)
        if number("I") != len(names):
            raise ValueError("Checkpoint field count mismatch.")
        field_hashes, arrays = {}, {}
        expected_cells = (nx+3)*(ny+3)
        if include_arrays and expected_cells > 1_000_000:
            raise ValueError("Array extraction is for small validation cases only.")
        for name in names:
            if string().decode("ascii") != name or number("Q") != expected_cells:
                raise ValueError("Checkpoint named state/dimensions mismatch.")
            remaining = expected_cells * 8
            state_hash = hashlib.sha256()
            parts = []
            while remaining:
                part = read(min(remaining, 8*1024*1024))
                if not np.isfinite(np.frombuffer(part, dtype="<f8")).all():
                    raise ValueError(f"Non-finite checkpoint field: {name}.")
                state_hash.update(part)
                if include_arrays:
                    parts.append(part)
                remaining -= len(part)
            field_hashes[name] = state_hash.hexdigest()
            if include_arrays:
                arrays[name] = np.frombuffer(b"".join(parts), dtype="<f8").copy()
        blocks = number("Q")
        if not 0 < blocks < 100_000_000:
            raise ValueError("Invalid checkpoint block count.")
        remaining = blocks * 5  # bool8 wet mask + int32 active block indices
        while remaining:
            part = read(min(remaining, 8*1024*1024))
            remaining -= len(part)
        scalars = struct.unpack("<dddd?", read(33))
        if not all(math.isfinite(v) for v in scalars[:4]) or stream.read(1):
            raise ValueError("Invalid checkpoint diagnostics/trailing bytes.")
    metadata = json.loads(Path(str(path)+".json").read_text(encoding="utf-8"))
    if (metadata.get("schema") != CONTRACT or metadata.get("fields") != names
            or metadata.get("flags") != flags or metadata.get("node_columns") != nx
            or metadata.get("node_rows") != ny or metadata.get("simulation_time_s") != stamp):
        raise ValueError("Checkpoint metadata does not match its binary state.")
    result = {"schema": CONTRACT, "simulation_time_s": stamp, "timestep_s": dt,
              "node_columns": nx, "node_rows": ny, "flags": flags,
              "fields": names, "field_sha256": field_hashes, "sha256": digest.hexdigest()}
    if include_arrays:
        result["arrays"] = arrays
    return result


def prepare_hotstart_handoff(root: Path, plan: dict) -> Path:
    from .spinup import _within
    source = _within(root, plan["spinup_output_directory"]) / "final_hotstart.bin"
    grid = json.loads((root / "quality_control/grid.json").read_text())
    expected_nodes = (grid["nx"]+1, grid["ny"]+1)
    before = inspect_checkpoint(source, expected_time_s=plan["spinup_duration_s"], expected_nodes=expected_nodes)
    target = _within(root, plan["handoff_hotstart"])
    temporary = target.with_suffix(".pending.bin")
    shutil.copyfile(source, temporary)
    temporary.replace(target)
    shutil.copyfile(str(source)+".json", str(target)+".json")
    after = inspect_checkpoint(target, expected_time_s=plan["spinup_duration_s"], expected_nodes=expected_nodes)
    if before["sha256"] != after["sha256"]:
        raise ValueError("Checkpoint changed during handoff.")
    receipt = {**after, "status": "PASSED", "source": str(source), "target": str(target),
               "binary_modified": False, "state_resampling": "none",
               "forcing_time_origin_s": plan["spinup_duration_s"],
               "diagnostic_reset": "event statistics only; physical water/soil/urban state retained"}
    (root / "quality_control/spinup_handoff.json").write_text(json.dumps(receipt, indent=2))
    return target
