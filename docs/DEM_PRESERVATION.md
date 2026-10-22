# DEM preservation and lineage

This contract applies to every executable DEM choice in the notebook. A new acquisition must save
the provider response or byte-preserved user source under `raw/dem/` before riftprep creates a
format conversion, mosaic, RIFT-grid reprojection, interpolation, or conditioning product.

| Selection | Immutable raw artifact | Reproducible derived artifacts | Final RIFT artifact |
|---|---|---|---|
| USGS 3DEP 10 m/30 m | `raw/dem/3dep_<resolution>m_provider_response.nc` for one request, or numbered response snapshots under `raw/dem/provider_responses/` | source-grid GeoTIFFs and any source mosaic under `processed/dem/watershed_workflow_3dep/`; aligned/conditioned grids under `processed/dem/` | `simulation_ready/dem.asc` |
| Copernicus GLO-30 | unchanged public COG tiles under `raw/dem/tiles/` | `processed/dem/copernicus_glo30/copernicus_glo30_source_mosaic.tif`, then the aligned/conditioned grids | `simulation_ready/dem.asc` |
| Local raster | byte-preserved copy under `raw/dem/` | aligned/conditioned grids under `processed/dem/` | `simulation_ready/dem.asc` |

The 3DEP raw snapshot is the scientific dataset returned to riftprep by Watershed Workflow's
`Manager3DEP`. The manager uses py3dep and the USGS 3DEP WMS. The service constructs the requested
10 m or 30 m EPSG:5070 grid, and py3dep masks the returned dataset to the request geometry before
riftprep receives it. The provenance record discloses this provider-side/adapter-side behavior; it
does not describe the saved NetCDF snapshot as an original upstream COG tile.

Every registered raw artifact has its path, provider, provider metadata, query parameters,
acquisition timestamps, embedded CRS status, byte size, and SHA-256 checksum in
`quality_control/provenance/raw_products.*`. Every attempt, including cache reuse, appears in
`acquisition_events.*`. `quality_control/dem_lineage.json` connects the raw artifact(s), the source
raster used for alignment, all saved processed stages, and the final RIFT DEM.

Existing validated cases with `rift_inputs/` and the historical
`raw/dem/3dep_<resolution>m_native.*` names remain untouched. Reuse reads those files in place and
does not rename, copy, rewrite, or reinterpret them. New acquisitions never create those names.
