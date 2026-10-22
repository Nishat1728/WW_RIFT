# Changelog

All notable changes to this project are documented here. Versions follow semantic versioning.

## Unreleased

- Added a beginner walkthrough covering installation, widgets, sources, credentials, quality
  gates, fresh/reuse behavior, output files, and the separate RIFT simulation step.
- Matched notebook simulation duration to the selected rainfall period, with a visible preview,
  timezone/date validation, and a configurable duration when rainfall is disabled. YAML cases
  also infer duration when no explicit simulation end is supplied.
- Added a reproducible HUC12 demonstration notebook and configuration using fresh 30 m inputs.
- Enlarged multi-panel terrain/input maps and shortened projected-coordinate tick labels so
  the notebook's default fonts do not crowd panel titles and colorbars.
- Added an explicit `raw/`, `processed/`, and `simulation_ready/` directory contract for new
  cases, with non-migrating compatibility for completed cases that already use `rift_inputs/`.
- Added v2 raw-artifact provenance and an acquisition-event ledger with provider metadata,
  redacted queries, timestamps, CRS status, cached-reuse history, and checksummed partial failures.
- Preserved 3DEP manager-response NetCDF snapshots and Copernicus COG tiles under `raw/dem/`,
  moved DEM conversions, mosaics, and aligned grids under `processed/dem/`, and added an explicit
  DEM lineage report without changing validated legacy caches.
- Added `riftprep-run-provenance-v1` manifests for successful, failed, and cache-resumed workflow
  attempts, with sanitized configuration, environment/code versions, checksummed artifacts,
  acquisition and stage outcomes, fallbacks, warnings, and quality-control references.
- Added a standalone SSURGO HTTP probe that preserves the raw provider body before JSON decoding.
- Added permanent SSURGO response diagnostics with request geometry, HTTP metadata, byte count,
  checksums, raw bodies, and explicit malformed-response failure messages.
- Added regression tests for invalid JSON, valid Table-free JSON, and normal Table responses.
- Added a deterministic fixed-seed CONUS HUC validation-matrix selector, authoritative USGS WBD
  attribute snapshot with stable pagination/ID checks, recorded exclusions and checksums, and a
  28-basin non-replacement manifest without launching basin workflows.
- Added a boundary-only validation gate for the locked 28-case matrix, with unchanged raw WBD
  GeoJSON, checksummed request provenance and cache reuse, deterministic validity repair,
  EPSG:5070 products, and per-case identity/topology/CRS/area/containment diagnostics.

## 0.3.0 — 2026-10-07

- Added the graphical, source-manager-style RIFT Input Builder notebook.
- Added raw-first acquisition, checksums, provenance, quality gates, diagnostic maps, and
  RIFT-ready writers.
- Added executable adapters for USGS 3DEP, Copernicus GLO-30, NLCD, SSURGO, SoilGrids, AORC,
  Daymet, MRMS, ERA5-Land, USGS hydrography/time series, and supported local products.
- Added user-controlled handling for land-cover and soil gaps and zero-only replacement of missing
  rainfall cells.
- Added fresh/reuse cache modes, six-domain HPC validation utilities, tests, and documentation.
