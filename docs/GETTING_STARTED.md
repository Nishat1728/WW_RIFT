# Beginner guide

RIFT Input Builder is a general, raw-first workflow for creating simulation-ready RIFT inputs.
It follows the source-manager pattern used by Watershed Workflow: the user chooses the study area
and each source in a short notebook front end, while tested Python modules handle downloading,
conversion, provenance, quality control, and RIFT file writing behind the notebook.

It is not tied to one named basin or HUC level. You can use a USGS HUC, a local polygon, or a
coordinate rectangle. The notebook **prepares inputs**; running the RIFT simulation is a separate
step that requires an installed RIFT solver.

**First time here?** Follow the beginner walkthrough below, or open the prefilled
[HUC12 demo notebook](../examples/HUC12_Demo.ipynb). Download the complete repository, not just the
notebook: its Python package, source catalog, and environment files are required.

## What the notebook guarantees

1. Every provider response or byte-preserved local source is saved under `raw/` before riftprep
   converts, mosaics, clips, interpolates, or aligns it to the RIFT grid.
2. Every raw artifact is checksummed and recorded with provider metadata, CRS status, acquisition
   timestamps, and byte size.
3. A product is displayed as accepted only after its quality gate passes.
4. The final plots show the actual processed products RIFT receives.
5. `fresh` mode deletes only the selected, package-marked case and downloads everything again.
6. `reuse` mode is available for ordinary iteration, but it is never forced on the user.
7. Every workflow attempt has a versioned, secret-safe provenance manifest, including controlled
   failures and cache-resumed attempts.

For DEMs, select the 10 m or 30 m USGS 3DEP service product, public Copernicus GLO-30, or a local
raster. The RIFT grid is a separate choice: it may be 10 m, 30 m, or another positive resolution.
For a 30 m RIFT simulation, choose a 30 m grid; the selected source DEM is then projected and
aligned to that exact grid.

```text
source choice -> immutable raw response -> processed source grid/mosaic -> RIFT-grid alignment -> simulation_ready/dem.asc
```

For 3DEP, Watershed Workflow uses the USGS WMS through py3dep. The WMS prepares the requested
10 m or 30 m EPSG:5070 grid and py3dep masks it to the request geometry before returning it.
RIFT Input Builder immediately preserves that returned dataset as
`raw/dem/3dep_<resolution>m_provider_response*.nc`; it does not claim that this snapshot is an
original upstream 3DEP COG tile. GeoTIFF conversion and any mosaic live under `processed/dem/`,
and `quality_control/dem_lineage.json` identifies every DEM stage. Existing validated cases that
already contain the old `3dep_<resolution>m_native.*` cache names are reused without migration or
rewriting; new acquisitions never create those legacy names.
See [`docs/DEM_PRESERVATION.md`](DEM_PRESERVATION.md) for the exact path and lineage contract
for each executable DEM source.
See [`docs/RUN_PROVENANCE_MANIFEST.md`](RUN_PROVENANCE_MANIFEST.md) for the versioned
run-level manifest and its machine-readable JSON schema.

For the two-stage controls, read [Spin-up and transient](SPINUP_AND_TRANSIENT.md) first.
The HUC12 demo includes one explicitly placed upstream gauge and prepares both configurations;
its gauge-transfer assumptions are documented in `examples/README.md`.

## Beginner walkthrough

### 1. What you need before starting

| Item | What it means |
|---|---|
| Complete repository | All files obtained with **Code → Download ZIP** or `git clone`. Extract a ZIP before starting. |
| Conda | A Python environment manager. Install Miniforge or another compatible Conda distribution before using the commands below. |
| JupyterLab | The notebook application; the supplied environment installs it. |
| Internet access | Needed for public provider downloads and any source-specific authentication. |
| Study area | A HUC code, a polygon file, or a rectangle. Preserve leading zeros in HUC codes. |
| Event times | Rainfall start and end, including a timezone. Use `Z` for UTC. |
| Storage and memory | Needed for source files, intermediate products, and final grids. Start with a small domain and short event. |

You do not need a GPU for input preparation. A large HUC2 case can require substantial memory and
hundreds of gigabytes of storage; the cost depends on domain extent, resolution, forcing source,
event length, and intermediate files. Use a compute node for large downloads and processing.
Users do not need access to the developers' Storrs account or directories.

The workflow has been exercised on Linux HPC. `environment.yml` is the portable installation
entry point; macOS and Windows installations may need platform-specific dependency troubleshooting.
The locked environment is **Linux-64 only**. Running RIFT-UConn itself requires the separate
Linux/CUDA/GPU setup described in each generated `RUN.md`.

### 2. Get the repository and install the environment

On GitHub, click **Code → Download ZIP**, extract it, and open a terminal in the extracted folder.
Alternatively, with Git installed:

```bash
git clone https://github.com/Nishat1728/WW_RIFT.git
cd WW_RIFT
```

Run these commands **from the repository folder**. Environment creation can take several minutes:

```bash
conda env create -f environment.yml
conda activate riftprep
jupyter lab RIFT_Input_Builder.ipynb
```

On Windows, use a terminal in which `conda` is available. On a later day, you only need to return
to the repository folder, activate `riftprep`, and launch JupyterLab; do not recreate the environment.
If Jupyter asks for a kernel, select the Python environment containing `riftprep`.

**GitHub is a preview, not a running notebook service.** GitHub shows saved text, code, and images.
Interactive dropdowns and downloads work after opening the notebook in a live Jupyter session.
A cloud notebook would need its own installation, credentials, memory, and storage.

Advanced users with a suitable pip environment can include the source-specific extras:

```bash
pip install -e ".[notebook,watershed-workflow,daymet,mrms,era5]"
```

### 3. Learn the few Jupyter controls you need

- A **cell** is one block of text or code. Select a code cell and press **Shift+Enter** to run it.
- The **kernel** is the Python process executing cells. A busy indicator means it is still working.
- Run the setup/selection cells to display the controls, then change the controls themselves.
  Ordinary use does not require editing Python. Advanced fields use JSON objects/lists or YAML.
- Do not click **Run All** in the general notebook before selecting your settings: it would use
  the defaults. The prefilled demo is designed to run with its defaults.
- Rerunning a widget-creation cell resets its controls. Changing a widget after building the
  managers does not update the already-built case; rebuild the managers and downstream cells.
- Save the notebook, but also keep the generated YAML: it records the selections independently
  of the live widgets. A kernel restart clears Python state; saved outputs are not a running case.

### 4. Choose your case and inputs

1. Choose a unique **Case name** and **Output root**. The case lives at
   `<output_root>/<case_name>/`. Use a new name to keep two experiments separately.
2. Select a **USGS HUC**, **local polygon**, or **coordinate rectangle**. HUC12 is a manageable
   first test; a HUC2 covers a much larger region. A Shapefile needs its companion files, including
   `.shx`, `.dbf`, and `.prj`; a GeoPackage is a convenient single-file alternative.
3. Set the **RIFT target CRS** and **RIFT cell size**. CRS means coordinate reference system.
   `EPSG:4326` uses longitude/latitude in degrees; `EPSG:5070` is a projected U.S. CRS in metres.
   A rectangle in geographic coordinates uses xmin/xmax = longitude and ymin/ymax = latitude.
4. Choose the DEM source independently of the RIFT grid. For a 30 m U.S. case, select
   **USGS 3DEP service product — 30 m** and a **30 m RIFT cell size**.
5. Choose initial water depth, hydrography, land cover, soil, and rainfall. Unused source-specific
   fields are ignored. Check that each source covers your location and dates; a global DEM does
   not make U.S.-specific NLCD or SSURGO available worldwide.
6. Enter rainfall start/end with a timezone, for example `2023-06-10T00:00:00Z` and
   `2023-06-12T00:00:00Z`. The visible duration becomes **48 hours / 172800 seconds** automatically.
7. Enable bathymetry, inflows, coastal stage, or drainage only when needed. Supply the station
   identifiers, local files, or explicit controls required by those modules.
8. Review the wall and outlet settings. **No outlet** in a walled domain leaves it closed;
   automatic or manually located outlets are modeling assumptions worth inspecting on the maps.

The output grid does not add information absent from a source. Mapping 1 km rainfall or 250 m
soil data onto 30 m cells does not turn them into observations at 30 m detail. For local elevation
and coastal-stage data, verify units and compatible vertical references before combining them.

### 5. Download, process, and review

Run these sections in order after setting the controls:

| Notebook section | What happens |
|---|---|
| Build and log the source managers | Validates your choices, saves `generated_configs/<case_name>.yaml`, initializes the case, and displays sources and duration. |
| Domain manager | Loads the study-area polygon, creates masks/grid information, and plots the boundary. |
| DEM through optional forcing managers | Each enabled manager acquires its product, saves source data, checks it, and displays accepted products and metadata. |
| Confirm the complete raw catalog | Checks that all enabled input downloads exist; reports source artifacts and download timings. |
| Build and validate RIFT inputs | Uses saved data to align grids, apply disclosed processing rules, and write the RIFT input files and configuration. |
| Inspect timings, provenance, and quality control | Shows final input statuses, final-product maps, diagnostic maps, manual resolutions, and timings. |

The short download cells call the implementation in `riftprep/`. Source and final-product plots
are distinct. Optional bathymetry, outlets, masking, and reprojection can explain differences
between the source DEM and the DEM RIFT receives. Disabling plots does not disable data saving.

Review `input_status_summary.csv`, coverage reports/maps, and `qc_report.json` before simulation.
`PASSED` means the documented checks passed; it does not calibrate a flood model or prove the
scientific suitability of its assumptions. Manual constants and tolerance warnings remain part
of the result and should be disclosed in any scientific use.

### 6. Try the prefilled HUC12 demo

```bash
conda activate riftprep
jupyter lab examples/HUC12_Demo.ipynb
```

The demo uses HUC **010801040205, Lake Morey–Connecticut River**, a **30 m 3DEP DEM**, a
**30 m RIFT grid**, public 3DHP/legacy NLCD/SSURGO/AORC sources, and a **48-hour event**
from July 10 to July 12, 2023 UTC. Its case name is `demo_huc12_010801040205`, and its
output root is `demo_cases/` in the repository.
The notebook finds the repository root even when Jupyter starts its kernel in `examples/`.

All default settings are ready: run from top to bottom, or use **Run All** for this demo only.
Downloads are fresh by default. The demo's visible soil constants are explicit user-approved
assumptions for missing cells; no land-cover constants are preselected. Inspect the original
gap map and replacement counts. The demo enables an observed upstream river inflow
and a four-month rain-free spin-up using the preceding 20 years of daily discharge. It writes both
CFGs with complete hotstart continuation. The notebook does not run the GPU solver. Gauge-to-inlet
transfer and terrain-matched outlet choices are illustrative; read [example assumptions](../examples/README.md).

The same case is available as [YAML](../examples/demo_huc12_010801040205.yaml):

```bash
riftprep validate-config examples/demo_huc12_010801040205.yaml
riftprep run examples/demo_huc12_010801040205.yaml
```

The repository includes the example instructions and configuration; users download their own
source data. Runtime depends on network and provider response times. The demo uses public services,
so an outage can stop it even when installation and configuration are correct.

See the portable [validation summary](../examples/demo_validation.json) for the actual tested
case, acquisition policy, timings, QC results and raw checksums. During an NLCD outage, the user
may explicitly approve verified earlier NLCD rasters for a test while other inputs are downloaded
fresh. Such a test is labelled **fresh except for NLCD**, never entirely fresh. The clean notebook
retains the online-source default. Measurements are specific to a run, not runtime promises.

![Demo: terrain before wall/outlets, active study area, and final RIFT terrain](../examples/demo_dem.png)

[View the demo rainfall plot](../examples/demo_rainfall.png). These small preview images are included
for GitHub readers; the full source datasets and case outputs are downloaded/generated locally.

### 7. Understand the saved files and run RIFT separately

- `raw/`: preserved acquired data. Do not edit these files to repair a model input.
- `processed/`: intermediate transformations that can be recreated from the saved sources.
- `simulation_ready/`: final input files, including `dem.asc` and the selected forcing files.
- `figures/` and `quality_control/`: maps, checks, replacement counts, and timing reports.
- `rift_case.cfg`: the generated solver configuration; `RUN.md`: commands and compute requirements.
- `quality_control/provenance/`: provider details, configuration/code information, and SHA-256
  checksums (file fingerprints used to detect changes).

The notebook does not install or execute the RIFT solver. After checking the case, install the
compatible RIFT-UConn solver separately, adjust the executable location in the generated run
instructions, and run the configuration on suitable hardware. Check simulation mass balance and
boundary behavior before interpreting flood results.

### Simulation duration and time conventions

With rainfall enabled, the graphical notebook sets simulation start to `0` and simulation end to
the exact elapsed seconds between the selected rainfall start and end. The duration preview updates
when either timestamp changes. Equal/reversed dates, invalid dates, and dates without a timezone
are rejected before the builder initializes or deletes a case. Timezone offsets are converted to
UTC when calculating elapsed duration. Provider-specific temporal availability still applies.

With **Match rainfall window** checked and rainfall disabled, **Duration without rain (h)** controls
the event length (default 24 hours). Uncheck **Match rainfall window** to use the explicit event
start/end instead, with or without rain. Enabled rainfall/inflow/coastal download windows then
follow that selected event window. The spin-up window and its baseflow history are separate.
See [the two-stage guide](SPINUP_AND_TRANSIENT.md) for the controls and defaults.

YAML/CLI cases infer the same duration when rainfall is enabled and `simulation.end_time_s` is
omitted. An explicitly supplied `end_time_s` remains an advanced override. With an advanced YAML
or `RIFTPREP_CASE_CONFIG` override, the **saved configuration and manager summary are authoritative**;
the graphical preview describes widget values only. Keep independently selected inflow/coastal
windows aligned with the simulation event.

### If something stops or looks wrong

For two-stage runs, the duration controls above describe **event elapsed time**. The generated
transient CFG continues the spin-up solver clock and applies a forcing-origin offset. It loads
a complete hotstart, not a depth raster. Build the supplied solver integration before simulation;
see [the two-stage guide](SPINUP_AND_TRANSIENT.md).

| Symptom | What to do |
|---|---|
| `No module named riftprep` or another import | Activate the installed environment and choose its kernel; check that you installed the complete repository. |
| No interactive controls on GitHub | Open the notebook in local/remote JupyterLab. The GitHub viewer cannot execute it. |
| Controls show plain text in Jupyter | Check the active environment includes `ipywidgets` and JupyterLab; restart Jupyter after installation. |
| `NEEDS USER INPUT` with black cells | Inspect the full-area diagnostic map, supply justified constants or a corrected local product, then follow the rebuild instructions below. |
| Provider timeout, unavailable service, unreadable download | Read the failing manager/log and preserved diagnostics. Retry after the service recovers; do not hide an acquisition failure with a constant. |
| Out-of-memory, disk-full, or very long processing | Reduce the domain/event for a test, or use an adequately resourced compute node and storage. |
| Fresh mode refuses a directory | Choose a new case name. It deliberately refuses to delete an unmarked existing directory; do not add a marker just to bypass the check. |

**Fresh mode deletes that existing marked case when the managers are rebuilt**, including raw
downloads, processing products, and final files. Use **Reuse saved case data** to retain downloads
while resolving QC issues. Reuse reprocesses the saved data; it is not a guarantee that a failed
notebook resumes at precisely its last executed cell.

All large implementation code lives in `riftprep/`; the notebook intentionally contains only a
graphical user interface and short orchestration cells. The **Advanced YAML override** field can
load a complete saved case instead of the widget values, and the `RIFTPREP_CASE_CONFIG`
environment variable provides the same path for noninteractive HPC execution.

Every source visible in a selection dropdown has an executable backend adapter. Each local-source
section includes the corresponding path fields; required paths are checked when the manager
configuration is built. Disabled optional inputs are not downloaded or processed.

## Inputs exposed in the front end

| Input | Working choices in this release | What RIFT receives |
|---|---|---|
| Study area | USGS HUC, local polygon, coordinate rectangle | node and cell masks |
| Terrain | USGS 3DEP 10 m/30 m, Copernicus GLO-30, local raster | projected DEM at selected RIFT resolution |
| Initial water depth | dry zero, user constant, local GeoTIFF | nonnegative depth grid |
| Hydrography | USGS 3DHP, local vector | outlet and optional bathymetry support |
| River bathymetry | explicit constant/attribute/order rules | channel-burned DEM |
| Land cover | NLCD, local rasters | class, imperviousness, canopy, Manning n |
| Soil | SSURGO, SoilGrids 2.0, local polygon layer | conductivity, porosity, suction, saturation |
| Rainfall | AORC, Daymet, MRMS, ERA5-Land, local gridded NetCDF | RIFT rainfall HDF5 |
| River inflow | explicit USGS station/time-series selection | SI hydrograph table |
| Coastal stage | explicit USGS station/time-series selection | boundary stage grids |
| Drainage | NLCD-derived, local vector/raster | drainage class grid and controls |

The full reviewed inventory—including Stage IV, GPM IMERG, other DEMs, land-cover products, and
additional soil products—is in
[`docs/RIFT_INPUT_SOURCE_CATALOG.md`](RIFT_INPUT_SOURCE_CATALOG.md). The notebook displays
the executable subset and reports how many reviewed future sources remain in the roadmap. Sources
marked `adapter needed` are never shown in executable dropdowns.

### Additional automatic sources

- **Copernicus GLO-30:** public 30 m COG tiles are saved unchanged under `raw/`; the derived
  source mosaic is saved under `processed/dem/`, then mapped to
  the selected RIFT grid. The native vertical reference is retained and reported for review.
- **Daymet:** requires a NASA Earthdata account, authorization for ORNL DAAC, and a protected
  `~/.netrc` entry. Choose continental North America, Hawaii, or Puerto Rico; the original 1 km
  DAP4 spatial/time subset is saved. Because RIFT requires hourly forcing, the interface makes the
  currently supported assumption explicit: each daily total is distributed uniformly over its
  24 UTC hours.
- **MRMS:** original compressed GRIB2 files are retained. Select gauge-adjusted Pass 1, Pass 2, or
  radar-only hourly QPE and the applicable NOAA region. Negative provider missing/no-coverage flags
  become missing values before the normal rainfall coverage policy is applied. `environment.yml`
  installs cfgrib/ecCodes so the adapter does not depend on a GDAL build with its GRIB plugin.
- **ERA5-Land:** requires `cdsapi`, an accepted CDS licence, and valid `~/.cdsapirc` credentials.
  The untouched CDS response is retained; cumulative precipitation is de-accumulated following the
  ERA5-Land convention and converted from metres to millimetres.
- **SoilGrids:** original 250 m WCS property subsets are retained. The selected depth/statistic and
  the hydraulic pedotransfer method are written to provenance before the RIFT soil rasters are
  produced.

## Domain examples

Any supported HUC level is inferred from the code length:

```yaml
domain:
  mode: huc
  huc_code: '010802050801'
  target_crs: EPSG:5070
  cell_size_m: 30
  outside_dem_mode: wall
  wall_elevation_m: 9999
```

USGS WBD boundary requests use bounded exponential-backoff retries for transient network errors
and HTTP-success responses that unexpectedly contain no polygon. A genuinely unavailable HUC still
fails with the attempted code and retry count instead of silently changing the study area.

Local polygon:

```yaml
domain:
  mode: shapefile
  vector_path: data/my_study_area.gpkg
  vector_layer: basin
  target_crs: auto_utm
  cell_size_m: 30
  outside_dem_mode: wall
```

Coordinate rectangle:

```yaml
domain:
  mode: rectangle
  bounds: [-72.31, 41.77, -72.23, 41.83]
  bounds_crs: EPSG:4326
  target_crs: auto_utm
  cell_size_m: 30
  outside_dem_mode: real_terrain
```

For a walled domain, configure an outlet explicitly. The optional
`auto_hydrography_lowest` rule chooses the lowest DEM crossing near the boundary and records the
derived location; it is never enabled invisibly. The notebook also accepts a point Shapefile or
GeoPackage with a declared CRS, or explicit coordinate JSON:

```json
[
  {"x": 653939.5, "y": 1584239.8, "width_m": 120, "cut_elevation_m": 0}
]
```

The coordinates use the CRS entered in the outlet control (for example `EPSG:5070`). Omit the
optional per-outlet fields to use the notebook defaults. **Match terrain** samples the nearest
valid active DEM value; **Constant**, **Zero**, and **Sloped** use the selected cut elevation.
Outlet carving rasterizes only the local corridor or notch rather than allocating full-domain
coordinate and distance arrays.

## Fresh downloads versus cache reuse

```yaml
cache:
  mode: fresh  # fresh | reuse
```

- `fresh` removes the existing case—including its provider and HTTP caches—and downloads every
  selected input again. It will delete only a directory containing this package's
  `.riftprep-case` safety marker.
- `reuse` keeps that case's local provider cache for faster iteration.

Each case owns its own cache under `raw/provider_cache/`. For wall-clock benchmarks, use `fresh`
and keep `quality_control/timings/wall_clock.csv`.

## If a quality gate asks for a user value

The case specification is immutable once the source managers have been built. After inspecting a
black-cell diagnostic map:

1. Enter only the scientifically approved constant in the relevant land-cover or soil widget, or
   select a corrected local source.
2. Change **Cache mode** to **Reuse saved case data** if the original raw downloads should be kept.
3. Rerun **Build and log the source managers**, the domain cell, every enabled raw-manager cell,
   **Confirm the complete raw catalog**, and **Build and validate RIFT inputs**.

Use **Fresh download** instead only when you intentionally want the marked case directory and its
case-local cache removed and every source acquired again.

## Quality-control decisions

Every input receives one of three clear states:

- `PASSED` — the saved native product and processed RIFT input passed their applicable checks.
- `NEEDS USER INPUT` — source data contain a gap or condition that requires a scientific choice.
- `FAILED` — the product is unreadable, structurally invalid, or violates a hard criterion.

### Land cover

The online dropdown uses **legacy NLCD 2021**, not the distinct Annual NLCD product series.
Before the downloader reads CRS metadata, an uncached health request checks HTTP status and WMS
structure. A `503` or an HTML error page is reported as a provider outage, with bounded retries
and untouched response/checksum evidence in `raw/land_cover/service_diagnostics/`. An error page
cannot establish CRS support and is never reported as proof that your chosen CRS is invalid.

Retry when the service recovers, or explicitly select **Local** and supply verified cover,
imperviousness and canopy rasters with adequate geographic coverage. Other inputs can still use
fresh downloads. The notebook does not silently choose cached data or switch to Annual NLCD.

NLCD class, imperviousness, and canopy are checked at every active RIFT node. If any active value
is null, `quality_control/land_cover_coverage_map.png` displays the whole study area in gray and
null cells in black. The workflow stops without guessing. The user may then provide constants
for only the null cells or supply corrected local rasters:

```yaml
land_cover:
  provider: watershed_workflow_nlcd
  options:
    manual_fill_values: {cover: 43, impervious: 0, canopy: 50}
    manual_dimension_constants: {}
```

Cover must be a valid integer NLCD class. Imperviousness and canopy supplied to RIFT are constrained
to 0–100 percent. Every finite processed imperviousness or canopy value above 100 is reported and
set to exactly 100 in the processing copy; the downloaded raw product is never changed. The workflow
writes `land_cover_percentage_over_100_before_correction_map.png`, with the affected cells in black,
and `land_cover_percentage_after_correction_map.png`, showing the QC-corrected percentage products
and highlighting the cells capped to 100. Small negative numerical endpoint noise within `1e-9`
percentage points of zero is normalized to zero. Materially negative or null values still appear on
the coverage map and require an explicit user constant or corrected local raster.
Unsupported or non-integer cover values are handled before final class validation: the workflow
counts the original values, marks only the processing copy as null, and displays those active cells
in black on `land_cover_coverage_map.png`. The user may then supply a supported integer NLCD class
through `manual_fill_values.cover` or use a corrected local raster. The raw NLCD file remains intact.
NLCD canopy codes `254` (non-processing area) and `255` (background) are preserved in the raw
download, masked only in the processing copy before reprojection, counted in the coverage report,
and handled through the same black-map and explicit-user-value policy.

### Soil

SSURGO permeability, porosity, residual saturation, and van Genuchten alpha follow the same
manual-only policy. No spatial gap is filled with a median. The workflow writes
`quality_control/soil_coverage_map.png` and pauses. After inspection, the user can provide
explicit constants for the missing properties:

```yaml
soil:
  provider: watershed_workflow_ssurgo
  options:
    manual_fill_values:
      permeability: 1.0e-12
      porosity: 0.42
      residual: 0.08
      alpha: 2.0e-4
```

Or provide a corrected local soil polygon product with the required fields.

SSURGO requests have a bounded timeout, bounded exponential-backoff retries, and spatial
subdivision for queries that are too large or return no `Table`. If the USDA service returns an
empty, invalid, non-JSON, or HTTP-error response, the workflow saves the exact request SQL,
requested EPSG:4326 geometry, HTTP status and headers, response size and checksum, and untouched
response body under `raw/soil/provider_response_diagnostics/`. The response body is saved before
JSON decoding. A persistently malformed response stops the run with the diagnostic path; it is
never reclassified as soil no-data and never triggers an automatic soil constant. A valid JSON
response without a `Table` is retried and subdivided, and only a repeatedly Table-free smallest
subquery is recorded as transparent source no-data in
`raw/soil/ssurgo_terminal_empty_queries.json`.

The configurable `soil.options.residual_porosity_tolerance` controls the paired consistency
check. With `0.1`, cells where `residual - porosity < 0.1` are retained unchanged and recorded as
QC warnings. Gaps of `0.1` or larger require an explicit valid pair of user porosity/residual
constants (recorded only in the processing copy), or a corrected local product; unresolved
material conflicts stop the run. The tolerance is a disclosed user policy, not a physical proof.

### Rainfall

Native rainfall and the saved event subset are never altered. If a source record contains null
active cells, the default `uncovered_policy: zero` writes exactly zero only at those cells in the
RIFT-ready HDF5. All available forcing values remain unchanged.

A zero substituted for missing source data is an assumption, not evidence that no rainfall
occurred. Review the replacement mask/counts, or select `error` to stop on gaps. Daymet's uniform
24-hour distribution preserves a daily total but does not reconstruct observed storm timing.

The workflow saves:

- `quality_control/rainfall_coverage.json`
- `quality_control/rainfall_coverage_map.png`
- `quality_control/rainfall_zero_replacement_mask.tif`

The mask uses 0 for an original retained value, 1 for a source-null cell replaced by zero, and
255 outside the active study area. Use `uncovered_policy: error` if any gap should pause instead.

Before the RIFT rainfall HDF5 is created, the selected event is loaded into memory and every
source NetCDF handle is closed. The HDF5 is then written and fully reopened for validation in
node-local temporary storage before it is copied and atomically published under
`simulation_ready/`.
Batch jobs use `SLURM_TMPDIR` when available and otherwise `/tmp`. Set
`RIFTPREP_HDF5_STAGING_DIR` to choose another staging filesystem. Progress checkpoints identify
each mapped record, validation, transfer, and final publication in both notebooks and job logs.

## Output layout

```text
<output_root>/<case_name>/
├── raw/                         # untouched selected products and case-local caches
│   ├── provider_cache/
│   └── boundary/ dem/ initial_state/ hydrography/ land_cover/ soil/ rainfall/
├── processed/                   # reproducible intermediate transformations
├── simulation_ready/            # quality-controlled files RIFT receives
├── figures/                     # raw_* and processed_* figures
├── quality_control/
│   ├── input_qc/                # one native-product QC JSON per input
│   ├── input_status_summary.{json,csv}
│   ├── provenance/raw_products.{json,csv}
│   ├── provenance/acquisition_events.{json,csv}
│   ├── provenance/run_manifest.json
│   ├── provenance/run_manifests/<run-id>.json
│   ├── timings/wall_clock.{json,csv}
│   ├── *_coverage.json and *_coverage_map.png
│   ├── resolved_case.yaml
│   └── qc_report.{json,csv}
├── case_manifest.json
├── rift_case.cfg
└── RUN.md
```

Every canonical raw artifact has a SHA-256 checksum. Processing never overwrites a raw product.
`quality_control/directory_contract.json` records the resolved owner of each data area. New cases
use `simulation_ready/`. Existing cases that already contain `rift_inputs/` continue to use that
directory in place when resumed; the workflow does not rename, copy, or migrate their completed
files. See [`docs/DIRECTORY_CONTRACT.md`](DIRECTORY_CONTRACT.md) for the complete writer and
reader contract.

Every raw-product record includes its provider, redacted query parameters, provider metadata,
acquisition timestamps, byte count, SHA-256 checksum, and embedded CRS status. Missing metadata is
recorded explicitly in `missing_metadata`; the workflow never guesses a CRS or acquisition time.
The acquisition-event ledger distinguishes `succeeded`, `reused`, and `failed` attempts. If a
provider leaves a partial response before failing, its path, size, and SHA-256 checksum are retained
in the failed event. Credential-like fields, URL query secrets, authorization values, cookies, and
bearer tokens are redacted before JSON or CSV is written. See
[`docs/PROVENANCE_CONTRACT.md`](PROVENANCE_CONTRACT.md).

## Command-line use

The notebook and command line use the same backend:

```bash
cp examples/case_template.yaml my_case.yaml
# Edit my_case.yaml.
riftprep validate-config my_case.yaml
riftprep run my_case.yaml
```

See [`examples/case_template.yaml`](../examples/case_template.yaml) for every common option.

## Development and Storrs validation

```bash
pytest
python tools/build_general_notebook.py
python -m riftprep.cli validate-config examples/case_template.yaml
```

Unit tests use small local fixtures. The Storrs matrix executes the same notebook for six domain
styles with native 30 m 3DEP, a 30 m RIFT grid, `cache.mode: fresh`, and isolated case roots. The
runner records DEM-download time and total notebook wall time for every case. Large downloads
must run on compute nodes, not a login node.

The expanded CONUS validation matrix is selected independently of basin execution. Run
`hpc/select_conus_huc_validation_matrix.py build` to snapshot the authoritative USGS WBD
attributes and generate the fixed-seed, non-overlapping 4/6/8/10 sample at HUC4/HUC8/HUC10/HUC12.
The publishable snapshot and manifest are under `validation/huc_matrix_selection_v1/`; the exact
eligibility rules, service-schema limitation, seed, checksums, and reproduction procedure are in
[`docs/HUC_VALIDATION_MATRIX_SELECTION.md`](HUC_VALIDATION_MATRIX_SELECTION.md). Building the
selection downloads no basin geometry or forcing and launches no RIFT workflow.

Boundary-only validation preserves USGS WBD GeoJSON and request provenance and checks identity,
topology, CRS, area, containment, and checksums. The original 28-case selection is retained as
`validation/huc_matrix_selection_v1/`; the later 22-case selection (2 HUC2, 4 HUC4, 6 HUC8,
10 HUC12) is retained as `validation/huc_matrix_selection_v2/`. Generated boundary downloads,
maps, and investigation notes are excluded from the source checkout. See
[`docs/HUC_BOUNDARY_VALIDATION.md`](HUC_BOUNDARY_VALIDATION.md) for the gate; it does not
create RIFT cases or download DEM, soil, or forcing products.

GitHub Actions repeats the unit suite on Python 3.11 and 3.12, compiles every Python source, and
regenerates the notebook to detect drift between its published JSON and the generator. The
Linux-64 package locks record the exact Storrs environment used for provider and matrix testing.
To reproduce it on Linux:

```bash
conda create --name riftprep-locked --file environment-linux-64.lock
conda activate riftprep-locked
python -m pip install -r requirements-linux-64-adapters.lock
python -m pip install --no-deps -e .
```

`environment-linux-64.lock` is platform-specific. Continue using `environment.yml` for macOS or
Windows development.

## Releases, citation, support, and contributions

- Release history: [`CHANGELOG.md`](../CHANGELOG.md)
- Citation metadata: [`CITATION.cff`](../CITATION.cff)
- Contribution and new-source requirements: [`CONTRIBUTING.md`](../CONTRIBUTING.md)
- Private vulnerability reporting guidance: [`SECURITY.md`](../SECURITY.md)
- Community expectations: [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)

Use GitHub issues for reproducible bugs, feature requests, and scientific-source discussions. Do
not post credentials or restricted provider data. A quality-control `PASSED` result validates the
documented data contract; users remain responsible for source licences, datum compatibility, and
model calibration.

## Scientific scope

A `PASSED` result confirms the documented data contract and quality gates. It does not calibrate
RIFT or prove that uncertain channel geometry, soil assumptions, boundary conditions, or forcing
choices are scientifically correct for a particular study. Those choices remain visible and
reviewable by the user.
