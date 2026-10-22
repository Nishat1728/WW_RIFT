# RIFT case directory contract

Every new case has three non-overlapping data areas. A file's directory identifies who owns it
and whether RIFT consumes it directly.

| Area | Owner and allowed contents | Direct RIFT input? |
|---|---|---|
| `raw/` | Immutable provider responses, provider-native downloads, byte-preserved local source copies, source manifests, and the case-local provider cache | No |
| `processed/` | Reproducible selections, unit conversions, reprojections, mosaics, and other intermediate transformations | No |
| `simulation_ready/` | Quality-controlled ESRI ASCII grids, HDF5 forcing, hydrograph tables, surge grids, and drainage tables referenced by `rift_case.cfg` | Yes |

Quality-control reports, figures, timings, and provenance remain outside those three data areas in
`quality_control/` and `figures/`. `rift_case.cfg`, `RUN.md`, and `case_manifest.json` remain at the
case root.

Each attempt writes its latest state to `quality_control/provenance/run_manifest.json` and its
run-ID history record under `quality_control/provenance/run_manifests/`. These records inventory
the three data areas without changing ownership or moving artifacts.

## Writers and readers

- Source managers write downloaded or user-supplied source products only under `raw/<input>/`.
- Processing functions may read `raw/`, write reproducible intermediates under `processed/`, and
  publish final model files under the resolved simulation-ready directory.
- DEM service responses and public source tiles are immutable `raw/dem/` artifacts. Format
  conversions, source-tile mosaics, RIFT-grid alignment, and bathymetry conditioning are derived
  DEM products and therefore live under `processed/dem/`. Only the final validated `dem.asc`
  belongs in the simulation-ready directory.
- `rift_case.cfg` uses case-relative paths into the resolved simulation-ready directory.
- Validators and Storrs result readers call `resolve_simulation_ready_directory(case_root)` instead
  of assuming a directory name.
- `quality_control/directory_contract.json` records the resolved paths and compatibility mode.

## Existing cases

Cases created by riftprep 0.3 and earlier may contain `rift_inputs/`. If `simulation_ready/` is
absent and `rift_inputs/` exists, that existing directory remains the simulation-ready owner. The
workflow does not rename, copy, delete, or migrate it. The `ProjectPaths.inputs` property remains a
compatibility alias for third-party code; new code should use `ProjectPaths.simulation_ready`.

If both names exist, `simulation_ready/` is canonical. Operators must audit such a case manually;
the workflow never merges the two directories.

Legacy 3DEP cases can also contain `raw/dem/3dep_<resolution>m_native.nc` and `.tif`. Those files
are retained and reused in place when the case already uses the legacy simulation-ready layout;
they are not renamed, reconstructed, or silently promoted to the new provenance semantics. New
acquisitions use `3dep_<resolution>m_provider_response*.nc` for raw manager responses and never
create a raw file whose name claims a derived conversion or mosaic is native.
