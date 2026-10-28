# RIFT input-source catalog

Last reviewed: 2026-10-07

This catalog inventories practical public and user-supplied sources that can provide the inputs
used by the current RIFT preparation workflow. It is deliberately broader than the adapters that
are implemented today. A source must not appear as a selectable notebook option until its status
is `working`.

Status meanings:

- **working** — the repository has a download/read adapter and RIFT conversion path.
- **adapter needed** — scientifically relevant and accessible, but code and tests are still needed.
- **local/manual** — accepted when the user supplies a file; automatic discovery/download is not
  promised.
- **derived** — generated from another selected source rather than independently downloaded.
- **legacy** — still available for reproducibility, but a newer authoritative source is preferred.

“RIFT conversion” describes the transformation required after the untouched raw product is saved.
It does not imply that resampling creates new spatial or temporal information.

## 1. Study-area boundary

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `usgs_wbd` | [USGS Watershed Boundary Dataset](https://www.usgs.gov/national-hydrography/watershed-boundary-dataset) | United States; HUC2–HUC12 complete, optional HUC14/HUC16 | **working** | Select HUC, dissolve, project, and rasterize masks |
| `usgs_nldi` | [USGS NLDI basin endpoint](https://api.water.usgs.gov/docs/nldi/basin/) | U.S. upstream basin for a network feature or outlet | adapter needed | Retrieve full-resolution basin polygon and project |
| `hydrobasins` | [HydroBASINS](https://www.hydrosheds.org/products) | Global nested sub-basins | adapter needed | Select level/basin IDs, dissolve, and project |
| `hydroatlas` | [HydroATLAS basins](https://www.hydrosheds.org/hydroatlas) | Global HydroBASINS geometry plus attributes | adapter needed | Select basin, retain provenance, and project |
| `local_vector` | User SHP/GPKG/GeoJSON | Any valid polygon and CRS | **working** | Validate, dissolve, project, and rasterize masks |
| `coordinate_rectangle` | User bounds and CRS | Any rectangular study area | **working** | Create polygon, project, and rasterize masks |

## 2. Terrain DEM and coastal topobathymetry

| Provider key | Source | Coverage / native resolution | Status | RIFT conversion |
|---|---|---|---|---|
| `usgs_3dep_10m` | [USGS 3DEP 1/3 arc-second](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) | U.S.; approximately 10 m seamless bare-earth DEM | **working** | Save the returned WMS/py3dep dataset snapshot under `raw/`; convert and align under `processed/` |
| `usgs_3dep_30m` | [USGS 3DEP 1 arc-second](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) | U.S. and neighboring areas; approximately 30 m | **working** | Save the returned WMS/py3dep dataset snapshot under `raw/`; convert and align under `processed/` |
| `usgs_3dep_1m` | [USGS 3DEP 1 m](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) | Partial but expanding U.S. coverage | adapter needed | Tile/mosaic, verify coverage and vertical datum, then reproject |
| `noaa_cudem` | [NOAA coastal elevation models / CUDEM](https://www.ncei.noaa.gov/products/coastal-elevation-models) | U.S. coastal topobathymetry; about 3 m or 10 m products | adapter needed | Resolve vertical datum, mosaic, and reproject |
| `copernicus_glo30` | [Copernicus DEM GLO-30](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) | Global land; 30 m | **working** | Save public COG tiles unchanged, mosaic, record native vertical reference, and reproject |
| `copernicus_glo90` | [Copernicus DEM GLO-90](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) | Global land; 90 m | adapter needed | Download COG tiles, mosaic, vertical-datum audit, reproject |
| `nasadem` | [NASA NASADEM](https://data.nasa.gov/dataset/nasadem-merged-dem-source-global-1-arc-second-nc-v001) | Near-global land; 1 arc-second / about 30 m | adapter needed | Earthdata-authenticated download, mosaic, and reproject |
| `merit_dem` | [MERIT DEM](http://hydro.iis.u-tokyo.ac.jp/~yamadai/MERIT_DEM/) | Near-global land; 3 arc-second / about 90 m | adapter needed | Download tiles, mosaic, and reproject |
| `alos_aw3d30` | [JAXA ALOS AW3D30](https://www.eorc.jaxa.jp/ALOS/en/dataset/aw3d30/) | Global land; about 30 m | adapter needed | Registered download, mosaic, and reproject |
| `fabdem` | [FABDEM](https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn) | Near-global, forest/building-reduced DEM; about 30 m | adapter needed | Download tiles, mosaic, and reproject |
| `local_raster` | User GeoTIFF/NetCDF/ESRI ASCII | Any grid with CRS and elevation units | **working** | Validate units/datum and reproject |

## 3. Hydrography, channels, and outlets

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `usgs_3dhp` | [USGS 3D Hydrography Program](https://www.usgs.gov/3d-hydrography-program/access-3dhp-data-products) | U.S.; current web service and annual downloads | **working** | Select flowlines, clip, project, and use for outlets/bathymetry |
| `nhdplus_hr` | [USGS NHDPlus High Resolution](https://www.usgs.gov/national-hydrography/nhdplus-high-resolution) | U.S.; legacy high-resolution network/catchments | adapter needed / legacy | Read GDB/download package and map attributes |
| `nhdplus_v2` | [USGS NLDI / NHDPlusV2 fabric](https://api.water.usgs.gov/docs/nldi/) | CONUS network used by NLDI | adapter needed / legacy | Query flowlines/catchments and project |
| `hydrorivers` | [HydroRIVERS](https://www.hydrosheds.org/products) | Global vector river network | adapter needed | Select by basin/area, clip, and project |
| `openstreetmap_waterways` | [OpenStreetMap Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) | Global, community-mapped waterways | adapter needed | Query tags, filter topology/quality, and project |
| `dem_derived` | Selected DEM | Any DEM-covered domain | derived | Depression handling, flow direction/accumulation, vectorization |
| `local_vector` | User SHP/GPKG/GeoJSON | Any line network with CRS | **working** | Validate, clip, project, and optionally map attributes |

## 4. Land cover, imperviousness, canopy, and Manning roughness

| Provider key | Source | Coverage / native resolution | Status | RIFT conversion |
|---|---|---|---|---|
| `nlcd` | [Legacy NLCD 2021 / MRLC](https://www.mrlc.gov/data) | U.S.; 30 m legacy land cover, imperviousness, canopy; distinct from Annual NLCD | **working** | Save native layers, nearest-neighbor map, translate classes to Manning n |
| `landfire` | [LANDFIRE](https://landfire.gov/data-downloads) | U.S.; 30 m vegetation/fuel/canopy products | adapter needed | Select layers/year and define explicit Manning crosswalk |
| `esa_worldcover` | [ESA WorldCover](https://esa-worldcover.org/en/data-access) | Global; 10 m, 2020/2021 | adapter needed | Download COG tiles and define explicit class-to-Manning crosswalk |
| `copernicus_lcfm` | [Copernicus LCFM successor to WorldCover](https://esa-worldcover.org/en/news) | Global; operational 10 m land-cover family | adapter needed | Select release, download tiles, and crosswalk classes |
| `dynamic_world` | [Google Dynamic World](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1) | Global; near-real-time 10 m labels/probabilities | adapter needed | Authenticate/export composite and define confidence/crosswalk rules |
| `copernicus_global_landcover` | [Copernicus Global Land Cover](https://land.copernicus.eu/en/products/global-dynamic-land-cover) | Global; categorical/fractional products | adapter needed | Download and crosswalk classes/fractions |
| `modis_mcd12q1` | [NASA MODIS MCD12Q1](https://lpdaac.usgs.gov/products/mcd12q1v061/) | Global annual land cover; 500 m | adapter needed | Earthdata download, select scheme/year, and crosswalk classes |
| `noaa_ccap` | [NOAA C-CAP land cover](https://coast.noaa.gov/digitalcoast/data/ccapregional.html) | U.S. coastal regions; 30 m | adapter needed | Download regional tiles and crosswalk classes |
| `corine` | [Copernicus CORINE Land Cover](https://land.copernicus.eu/en/products/corine-land-cover) | Europe; 100 m inventory with higher-resolution change layers | adapter needed | Select vintage, download, and crosswalk classes |
| `local_rasters` | User categorical cover plus optional impervious/canopy rasters | Any valid rasters | **working** | Validate categories/ranges and map to RIFT grid |
| `constant_manning` | User constant | Any domain | derived/manual | Fill active nodes with documented Manning n |

## 5. Soil and infiltration properties

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `ssurgo` | [USDA SSURGO / Soil Data Access](https://www.nrcs.usda.gov/resources/data-and-reports/soil-survey-geographic-database-ssurgo) | Detailed U.S. soil map units, commonly 1:12,000–1:63,360 | **working** | Query polygons/attributes and derive RIFT infiltration parameters |
| `gssurgo` | [USDA gSSURGO / SSURGO Portal](https://www.nrcs.usda.gov/resources/data-and-reports/ssurgo-portal) | U.S. rasterized/packaged SSURGO | adapter needed | Download package, join properties, derive parameters |
| `statsgo2` | [USDA STATSGO2](https://www.nrcs.usda.gov/resources/data-and-reports/ssurgo/stats2go-metadata) | Generalized U.S. soils | adapter needed | Query/download and derive parameters |
| `polaris` | [POLARIS soil properties](https://scholars.duke.edu/publication/1381493) | CONUS; 30 m probabilistic properties | adapter needed | Select depth/statistic and derive hydraulic parameters |
| `soilgrids` | [ISRIC SoilGrids](https://isric.org/explore/soilgrids/) | Global; 250 m, six depth intervals | **working** | Save WCS layers and apply recorded Cosby/bulk-density/water-content pedotransfer rules |
| `glhymps` | [GLHYMPS](https://doi.org/10.1002/2014GL059856) | Global permeability and porosity polygons | adapter needed | Select layer, convert permeability/porosity, and rasterize |
| `hwsd` | [FAO HWSD v2](https://www.fao.org/land-water/resources/tools/databases/hwsd/en) | Global; about 1 km, seven depth layers | adapter needed | Join raster codes to attributes and derive hydraulic parameters |
| `openlandmap` | [OpenLandMap](https://openlandmap.org/) | Global gridded soil/environmental layers | adapter needed | Select property/depth and derive parameters |
| `local_vector_or_rasters` | User soil polygons or parameter rasters | Any valid user data | **working** | Validate units/ranges, rasterize or reproject |
| `constant_soil` | User constants | Any domain | derived/manual | Create documented constant hydraulic fields |

## 6. Rainfall and meteorological forcing

| Provider key | Source | Coverage / native resolution and time step | Status | RIFT conversion |
|---|---|---|---|---|
| `aorc` | [NOAA AORC](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/) | U.S.; about 1 km, hourly | **working** | Preserve hourly accumulation, conservative/average spatial mapping, write RIFT rate |
| `mrms` | [NOAA MRMS](https://www.nssl.noaa.gov/projects/mrms/operational/tables.php) | U.S. radar domains; about 1 km hourly QPE selected here | **working** | Save compressed GRIB2, map negative flags to missing, and subset hourly accumulation |
| `stage_iv` | [NOAA Stage IV](https://api.water.noaa.gov/about/precipitation-data-access) | CONUS; about 4 km, hourly/multi-hour accumulation | adapter needed | Read GRIB, preserve interval semantics, map accumulation |
| `hrrr` | [NOAA HRRR](https://rapidrefresh.noaa.gov/hrrr/) | CONUS; 3 km, hourly updated forecast/analysis | adapter needed | Select cycle/forecast hours, deaccumulate precipitation |
| `nldas2` | [NASA NLDAS-2](https://ldas.gsfc.nasa.gov/nldas/nldas-2-model-data) | North America; 0.125 degree, hourly | adapter needed | Authenticated subset, variable/unit conversion, map to grid |
| `era5_land` | [Copernicus ERA5-Land](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land) | Global land; about 9 km, hourly | **working with CDS credentials** | Save CDS response, deaccumulate following the official convention, convert metres to millimetres, and map to grid |
| `gpm_imerg` | [NASA GPM IMERG](https://gpm.nasa.gov/data/imerg) | Near-global; 0.1 degree/about 10 km, 30 minute | adapter needed | Earthdata subset, select Early/Late/Final, integrate rate |
| `daymet` | [ORNL Daymet](https://daymet.ornl.gov/getdata) | North America/Hawaii; 1 km, daily precipitation total | **working with Earthdata credentials** | Preserve the authenticated DAP4 spatial/time subset and require the explicit uniform-24-hour distribution rule |
| `prism` | [PRISM Climate Group](https://prism.oregonstate.edu/recent/) | CONUS; about 4 km, daily/monthly | adapter needed | Download BIL, preserve daily total, require timing rule |
| `noaa_atlas_design_storm` | [NOAA precipitation frequency estimates](https://hdsc.nws.noaa.gov/pfds/) | U.S.; point depth-duration-frequency estimates | adapter needed | Construct a documented synthetic hyetograph; not observed forcing |
| `rain_gauges` | NOAA/USGS/state/local gauge CSV | Station dependent; point time series | local/manual | QC, temporal alignment, spatialization selected by user |
| `local_grids` | User NetCDF/GRIB/GeoTIFF/HDF5 | Any supported gridded forcing | **working** | User identifies variable, units, CRS, interval semantics |

Daymet and PRISM are useful for daily/long-duration studies but are not interchangeable with
sub-hourly storm products. The notebook must require an explicit temporal-distribution method
before converting daily totals into a RIFT event forcing.

## 7. River inflow hydrographs

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `usgs_waterdata` | [Modern USGS Water Data APIs](https://api.waterdata.usgs.gov/docs/ogcapi/) | U.S. continuous observations, often 15 minute | **working** | Select station/time-series ID, convert discharge to m³/s, align time |
| `usgs_nwis_legacy` | [USGS legacy-to-modern migration guide](https://api.waterdata.usgs.gov/docs/ogcapi/migration/) | U.S. legacy instantaneous/daily services | **working** / legacy | Read legacy response and convert units/time |
| `noaa_nwps` | [NOAA National Water Prediction Service API](https://water.noaa.gov/about/api) | U.S. observations and official forecasts | adapter needed | Select forecast point/product and convert units/time |
| `national_water_model` | [NOAA National Water Model](https://water.noaa.gov/about/nwm) | U.S. modeled streamflow, forecast and retrospective products | adapter needed | Map feature ID, select product/cycle, extract and align discharge |
| `hydrologic_model_output` | HEC-HMS, SWAT, WRF-Hydro, other model output | Model dependent | local/manual | Parse explicit location/time/unit mapping |
| `local_csv` | User time/discharge table | Any boundary location | **working** through user-specified series workflow | Validate monotonic time and SI discharge |

## 8. Coastal water level and surge

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `noaa_coops` | [NOAA CO-OPS API](https://api.tidesandcurrents.noaa.gov/api/prod/) | U.S. coastal stations; commonly 6-minute water level | adapter needed | Select station/datum, convert vertical datum and units |
| `usgs_waterdata_stage` | [USGS Water Data APIs](https://api.waterdata.usgs.gov/) | Stage observations at available U.S. sites | **working** | Select time series, convert datum/units, apply at boundary support |
| `stofs` | [NOAA STOFS](https://vlab.noaa.gov/web/stofs) | Operational coastal surge/tide forecast grids | adapter needed | Select cycle, interpolate water level, resolve datum |
| `estofs_legacy` | NOAA ESTOFS archives | Legacy operational forecast grids | adapter needed / legacy | Read NetCDF and resolve datum |
| `adcirc` | [ADCIRC](https://adcirc.org/) model output | Model/domain dependent | local/manual | Read fort.63/NetCDF, interpolate, resolve datum |
| `local_stage_or_grids` | User CSV/NetCDF/GeoTIFF/ASCII | Any coastal boundary | **working** for configured local data | Validate time, units, datum, and spatial support |

## 9. River/channel bathymetry and hydraulic geometry

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `usace_ehydro` | [USACE eHydro surveys](https://water.usgs.gov/catalog/datasets/247a297a-39dc-4d1e-96bb-7bf684aefbd4/) | Surveyed U.S. navigation channels | adapter needed | Select survey vintage, resolve datum, rasterize depths |
| `noaa_cudem_bathy` | [NOAA CUDEM and coastal DEMs](https://www.ncei.noaa.gov/products/coastal-elevation-models) | U.S. coastal/inland-connected topobathymetry where available | adapter needed | Resolve datum and merge with terrain |
| `noaa_hydrographic_surveys` | [NOAA Bathymetric Data Viewer](https://www.ncei.noaa.gov/maps/bathymetry/) | U.S. coastal soundings/surveys | adapter needed | Select survey, grid soundings, resolve datum |
| `fema_nfhl_cross_sections` | [FEMA National Flood Hazard Layer](https://www.fema.gov/flood-maps/national-flood-hazard-layer) | Available mapped U.S. flood studies | adapter needed | Read cross sections/profile references; coverage varies |
| `hydraulic_geometry` | 3DHP/NHDPlus drainage attributes plus user equations | Network dependent | **derived** | Compute documented width/depth by order or drainage area |
| `constant_channel` | Hydrography plus user width/depth | Any mapped network | **working** | Burn explicit constant/profile depth into DEM |
| `local_bathymetry` | User points/cross sections/raster | Any surveyed channel | local/manual | Resolve datum, interpolate/rasterize, and merge with DEM |

## 10. Urban drainage and hydraulic controls

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `municipal_gis` | Local/municipal stormwater GIS | Local pipes, inlets, outlets, controls | local/manual | Schema mapping, rasterize types, write control table |
| `epa_swmm_inp` | [EPA SWMM input](https://www.epa.gov/water-research/storm-water-management-model-swmm) | User/municipal model network | adapter needed | Parse nodes/links/controls and map supported RIFT controls |
| `openstreetmap_drainage` | [OpenStreetMap Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) | Global but incomplete/variable drainage features | adapter needed | Query/filter features and require QA confirmation |
| `synthetic_nlcd` | Selected NLCD imperviousness | U.S. where NLCD is selected | **working** / derived | Apply explicit threshold and create synthetic drainage classes |
| `local_vector` | User SHP/GPKG/GeoJSON | Any mapped drainage network | **working** | Map fields to RIFT schema and rasterize |
| `local_raster` | User drainage-class raster | Any grid | **working** | Nearest-neighbor map and validate class/control IDs |

## 11. Initial water depth and restart state

| Provider key | Source | Coverage / native form | Status | RIFT conversion |
|---|---|---|---|---|
| `dry_zero` | Constant zero depth | Any domain | **working** / derived | Write zero initial-depth raster |
| `constant_depth` | User constant | Any domain | **working** / derived | Fill active nodes with documented nonnegative depth |
| `local_depth_raster` | User GeoTIFF | Any domain | **working** / local | Reproject, clip, and validate finite nonnegative depth |
| `rift_hotstart` | Previous compatible RIFT state | Matching RIFT grid/schema | adapter needed | Validate grid/config/version and copy restart state |
| `external_model_state` | User hydraulic-model water surface/depth | Model dependent | local/manual | Convert water surface to depth and map conservatively |

## 12. Source-selection rules for the notebook

The notebook selector is generated from this catalog and follows these rules:

1. Show `working` sources as selectable choices.
2. Show `adapter needed` sources in a separate “available for future implementation” table, not
   in the executable dropdown.
3. Always include local/user data where the backend reader is implemented.
4. Display coverage, native resolution, time step, authentication, and scientific caveats before
   download.
5. Save the untouched provider response before clipping, reprojection, temporal conversion, class
   translation, or parameter derivation.
6. Record the selected source, version/release, endpoint, options, checksums, and transformations.
7. Never silently substitute a different source when coverage is absent.
8. Require explicit decisions for vertical datum conversion, daily-to-subdaily rainfall,
   class-to-Manning mapping, pedotransfer functions, and synthetic drainage/bathymetry.

## 13. Immediate adapter priorities

After the first multi-source release, implement and test the remaining adapters in this order:

1. Rainfall: Stage IV, then GPM IMERG and NLDAS-2.
2. Terrain: NOAA CUDEM, NASADEM, and FABDEM.
3. Land cover: ESA WorldCover and LANDFIRE.
4. Soil: POLARIS and GLHYMPS.
5. Boundaries/hydrography: NLDI, HydroBASINS, and HydroRIVERS.
6. Inflow/coastal: National Water Model and NOAA CO-OPS.
7. Bathymetry/drainage: USACE eHydro and EPA SWMM.

Each adapter needs unit tests with synthetic fixtures and at least one fresh real-provider test on
Storrs before its status changes to `working`.
