# Locked HUC boundary-validation gate

The text below describes the original v1 selection. The later v2 selection contains 22 cases
at HUC2/HUC4/HUC8/HUC12. Selection snapshots are included in the repository; generated raw boundary
downloads, processed maps, and gate reports are not. Run the gate to create those artifacts.

This gate acquires and validates boundaries for the 28 cases already recorded in
`validation/huc_matrix_selection_v1/selection_manifest.json`. It does not resample the matrix,
construct RIFT cases, download DEM/soil/forcing data, or run the RIFT Input Builder workflow.

## Source and raw preservation

The authoritative source is the USGS Watershed Boundary Dataset FeatureServer:

`https://hydro.nationalmap.gov/arcgis/rest/services/wbd/FeatureServer`

For every locked HUC, `hpc/validate_locked_huc_boundaries.py` queries the layer implied by the
recorded HUC level, requests the exact recorded code, and asks the service for GeoJSON in
EPSG:4326. The unmodified HTTP body is saved before JSON or geometry parsing. Every request has a
canonical SHA-256 fingerprint, and every response records its byte count, SHA-256 checksum, HTTP
status, selected response headers, time, and bounded attempt history. `reuse` mode accepts a raw
response only when both its query fingerprint and checksum match the saved provenance.

Concurrency is explicitly bounded by `--max-workers` and hard-limited to eight. The validation run
uses four workers by default. Provider failures, empty responses, or invalid products are recorded
against the original selected HUC; the script never chooses a replacement.

## Per-case quality gate

Each of the 28 cases must pass all of these checks:

1. Returned HUC attribute exactly matches the requested code and recorded HUC level.
2. Exactly one authoritative feature is returned, with object ID and TNMID matching both the
   locked selection and its checksummed source catalog.
3. Geometry is present and nonempty.
4. Original validity is reported. Invalid geometry is repaired deterministically with
   `make_valid`; non-polygon fragments are discarded and the case fails if no valid polygonal
   geometry remains.
5. Final geometry type is Polygon or MultiPolygon.
6. Source CRS is explicitly assigned from the recorded `outSR=4326` request, and transformation to
   the project CRS EPSG:5070 succeeds.
7. Source and projected bounds contain only finite numbers.
8. WGS84 geodesic area and EPSG:5070 projected area are finite and positive.
9. Projected area agrees with the locked WBD `areasqkm` attribute within two percent.
10. Every applicable HUC parent prefix exists in the checksummed WBD attribute snapshot.
11. The final polygon covers its representative interior point, and a 5070-to-4326 round trip
    preserves that containment with Hausdorff distance no greater than `1e-7` degrees.
12. Raw-response checksum matches provenance, and the EPSG:5070 GeoPackage is nonempty and
    checksummed.

## Artifacts

The output root contains:

- `raw/source_layer_metadata/`: unchanged layer-metadata responses and provenance.
- `raw/huc_boundaries/huc<level>/<code>/`: raw GeoJSON, all HTTP attempt bodies, and provenance.
- `processed/huc_boundaries/huc<level>/<code>/boundary_epsg5070.gpkg`: validated project-CRS
  boundary.
- `quality_control/cases/`: complete per-case validation reports.
- `quality_control/boundary_validation_matrix.json`: machine-readable 28-case matrix.
- `quality_control/boundary_validation_matrix.csv`: compact review table.
- `provenance/locked_selection_manifest.json`: byte-identical copy of the locked manifest.
- `provenance/source_metadata.json` and `provenance/artifact_manifest.json`: source and output
  checksums.

The matrix explicitly records `dem_downloaded=false`, `soil_downloaded=false`,
`forcing_downloaded=false`, `rift_workflow_launched=false`, and an empty workflow-stage list.
