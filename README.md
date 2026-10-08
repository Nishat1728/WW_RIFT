# RIFT Input Builder

RIFT Input Builder is a general, raw-first workflow for creating simulation-ready RIFT inputs.
It follows the source-manager pattern used by Watershed Workflow: the user chooses the study area
and each source in a short notebook front end, while tested Python modules handle downloading,
conversion, provenance, quality control, and RIFT file writing behind the notebook.

It is not tied to one named basin or HUC level. You can use a USGS HUC, a local polygon, or a
coordinate rectangle.

## What the notebook guarantees

1. Every selected native product is saved before it is transformed.
2. Every native artifact is checksummed and recorded in provenance.
3. A product is displayed as accepted only after its quality gate passes.
4. The final plots show the actual processed products RIFT receives.
5. `fresh` mode deletes only the selected, package-marked case and downloads everything again.
6. `reuse` mode is available for ordinary iteration, but it is never forced on the user.

For DEMs, select the native 10 m or 30 m USGS 3DEP product, public Copernicus GLO-30, or a local
raster. The RIFT grid is a separate choice: it may be 10 m, 30 m, or another positive resolution.
For a 30 m RIFT simulation, choose a 30 m grid; the selected source DEM is then projected and
aligned to that exact grid.

```text
source choice -> untouched native files -> native QC -> RIFT-grid conversion -> final QC -> RIFT
```

## Quick start

```bash
conda env create -f environment.yml
conda activate riftprep
jupyter lab RIFT_Input_Builder.ipynb
```

For a pip environment, include the source-specific extras:

```bash
pip install -e ".[notebook,watershed-workflow,daymet,mrms,era5]"
```

In the notebook:

1. Find the cells headed **✏️ USER EDIT**.
2. Use the visible dropdowns, checkboxes, sliders, and text fields—no Python editing is required.
3. Choose the case name, output folder, domain, grid resolution, cache mode, and one source in
   each input section. Fields belonging to an unselected or disabled source are ignored.
4. Run downward one cell at a time. Each manager downloads, saves, checks, and displays its own
   native product.
5. Confirm the raw catalog.
6. Run the RIFT processing cell.
7. Review the final per-input status table, final product plots, coverage maps, and timings.

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
[`docs/RIFT_INPUT_SOURCE_CATALOG.md`](docs/RIFT_INPUT_SOURCE_CATALOG.md). The notebook displays
the executable subset and reports how many reviewed future sources remain in the roadmap. Sources
marked `adapter needed` are never shown in executable dropdowns.

### Additional automatic sources

- **Copernicus GLO-30:** public 30 m COG tiles are saved unchanged, then mosaicked and mapped to
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
derived location; it is never enabled invisibly.

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

The configurable `soil.options.residual_porosity_tolerance` controls the paired consistency
check. With `0.1`, cells where `residual - porosity < 0.1` are retained unchanged and recorded as
QC warnings; gaps of `0.1` or larger still fail the run.

### Rainfall

Native rainfall and the saved event subset are never altered. If a source record contains null
active cells, the default `uncovered_policy: zero` writes exactly zero only at those cells in the
RIFT-ready HDF5. All available forcing values remain unchanged.

The workflow saves:

- `quality_control/rainfall_coverage.json`
- `quality_control/rainfall_coverage_map.png`
- `quality_control/rainfall_zero_replacement_mask.tif`

The mask uses 0 for an original retained value, 1 for a source-null cell replaced by zero, and
255 outside the active study area. Use `uncovered_policy: error` if any gap should pause instead.

## Output layout

```text
<output_root>/<case_name>/
├── raw/                         # untouched selected products and case-local caches
│   ├── provider_cache/
│   └── boundary/ dem/ initial_state/ hydrography/ land_cover/ soil/ rainfall/
├── processed/                   # explicit intermediate transformations
├── rift_inputs/                 # files RIFT receives
├── figures/                     # raw_* and processed_* figures
├── quality_control/
│   ├── input_qc/                # one native-product QC JSON per input
│   ├── input_status_summary.{json,csv}
│   ├── provenance/raw_products.{json,csv}
│   ├── timings/wall_clock.{json,csv}
│   ├── *_coverage.json and *_coverage_map.png
│   ├── resolved_case.yaml
│   └── qc_report.{json,csv}
├── case_manifest.json
├── rift_case.cfg
└── RUN.md
```

Every canonical raw artifact has a SHA-256 checksum. Processing never overwrites a raw product.

## Command-line use

The notebook and command line use the same backend:

```bash
cp examples/case_template.yaml my_case.yaml
# Edit my_case.yaml.
riftprep validate-config my_case.yaml
riftprep run my_case.yaml
```

See [`examples/case_template.yaml`](examples/case_template.yaml) for every common option.

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

- Release history: [`CHANGELOG.md`](CHANGELOG.md)
- Citation metadata: [`CITATION.cff`](CITATION.cff)
- Contribution and new-source requirements: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Private vulnerability reporting guidance: [`SECURITY.md`](SECURITY.md)
- Community expectations: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

Use GitHub issues for reproducible bugs, feature requests, and scientific-source discussions. Do
not post credentials or restricted provider data. A quality-control `PASSED` result validates the
documented data contract; users remain responsible for source licences, datum compatibility, and
model calibration.

## Scientific scope

A `PASSED` result confirms the documented data contract and quality gates. It does not calibrate
RIFT or prove that uncertain channel geometry, soil assumptions, boundary conditions, or forcing
choices are scientifically correct for a particular study. Those choices remain visible and
reviewable by the user.
