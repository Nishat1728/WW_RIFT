# RIFT-UConn complete-restart integration

WW_RIFT's two-stage runner requires this integration. The original RIFT-UConn hotstart in the
checked `c111780` source does not serialize dynamic soil saturation or urban retained storage,
and does not provide a shared event-forcing time-origin control. Merely pointing an event CFG
at that old binary is not sufficient for complete continuation.

## Build a separate solver copy

Keep the complete WW_RIFT repository together. From its root:

```bash
python tools/prepare_rift_uconn_restart.py \
  --source /path/to/RIFT-UConn \
  --destination /path/to/new/RIFT-UConn-complete-restart

cmake -S /path/to/new/RIFT-UConn-complete-restart \
  -B /path/to/new/RIFT-UConn-complete-restart/build \
  -DCMAKE_CUDA_ARCHITECTURES=80 \
  -DCUDA_SAMPLES_INC=/path/to/cuda-samples/Common
cmake --build /path/to/new/RIFT-UConn-complete-restart/build --parallel 4
/path/to/new/RIFT-UConn-complete-restart/build/rift --restart-capabilities
```

Choose the CUDA architecture/toolchain for your GPU. Linux, CUDA, CMake, MPI, HDF5 C and CUDA
sample helper headers are required, as for RIFT-UConn itself. The preparation tool creates a
**new directory**, copies source/build definitions (not data/results), records before/after
SHA-256 hashes, and never edits the original checkout. It fails if source anchors are unfamiliar.
The known source layout is `c111780`; adapt/review/test the integration if your version differs.

The capability output must name `rift-uconn-complete-restart-v1`, with `forcing_time_origin`
and `exact_final_checkpoint` both true. The notebook can prepare inputs without this binary;
`riftprep simulate-stages` refuses to launch spin-up with an unpatched binary.

## Checkpoint contents and compatibility

Version 1 begins with `WWRIFT01`, format version, physics/state flags, time/timestep and exact
grid metadata. Named double arrays include:

- Water surface and both momentum components.
- Cumulative infiltration when infiltration is enabled.
- Soil saturation and moisture deficit when antecedent/dynamic soil is enabled.
- Urban retained storage, cumulative outflow and capture/release diagnostics when enabled.
- Enabled depth/discharge/peak/arrival diagnostics.
- Wet/active block state and mass-reference bookkeeping.

Static terrain, roughness, conductivity and drainage parameters remain case inputs. Scratch
buffers are rebuilt and the event's own forcings are loaded. A checkpoint is not a portable
archive of every model parameter, source dataset or arbitrary future physics module.

The native binary contract is Linux little-endian IEEE-754 float64, int32 and bool8. A JSON
sidecar declares its named fields and time. Writer publication is atomic; readers reject wrong
version, grid/flags, field names/counts, truncation and non-finite array values. The Python
handoff additionally streams bounded-memory validation and SHA-256 checks before/after copying.
Legacy binaries are deliberately rejected, not silently interpreted as complete state.

## Exact end and forcing clocks

`output.final_hotstart=1` writes `final_hotstart.bin` at the exact simulation end, independent
of `output.hotstart` (periodic saving). Adaptive steps are clipped to phase ends and scheduled
outputs. Thus turning off periodic hotstarts does not disable the required handoff checkpoint.

The event retains `t`/`dt` and starts at the checkpoint's exact solver time. All forcing readers
evaluate `t - forcing.time_origin_s`, including initialization: ATS HDF5 and raster rainfall,
uniform rainfall, inflow hydrographs, coastal grids and external drainage. Raw and processed
forcing files retain their original/event-relative timestamps.

Generated events set `restart.reset_diagnostics=1`: event peak/arrival/mass and urban-outflow
tallies start afresh. Physical hydraulic/infiltration/soil/urban retained-water states do not.
For a like-for-like uninterrupted-restart comparison, omit that setting to preserve diagnostics.
`restart.audit_loaded_state=1` optionally writes `restart_loaded.bin` immediately after reading;
it is for small validation tests, not enabled by default on large domains.

## Validation

Offline tests cover CFGs, forcing offsets, the versioned parser, exact-end/grid checks, byte-
preserving handoff and fail-closed capability gates. A native C++ test executes the actual reader
and writer using host-memory CUDA API stubs, including physical-state preservation and diagnostic
reset. That test and a successful CUDA compilation do **not** establish GPU numerical equivalence;
run the following opt-in GPU test before production use:

```bash
python -m hpc.validate_complete_restart /absolute/path/to/patched/rift /new/test/directory
```

It uses a synthetic 30 m, 20×20-cell domain, nonzero soil and urban storage, changing rain and
inflows, and compares an 80-second uninterrupted run with a restart at 40 seconds. It checks
bitwise named-state loading, final-state numerical equivalence, and rejection of a truncated
binary. This is a restart regression test, not a four-month basin simulation or calibration.

The [2026-10-22 Storrs GPU receipt](../../examples/complete_restart_validation.json) records a
passing run with zero maximum difference in all 17 checked fields. It does not replace testing
your own solver version, domain or scientific configuration.
