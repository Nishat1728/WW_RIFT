# HUC12 input-preparation example

Open `HUC12_Demo.ipynb` in the installed environment and run from top to bottom. It uses the
same cells and backend as the general notebook, with prefilled values for **010801040205,
Lake Morey–Connecticut River**, New Hampshire/Vermont (152.52 km²).

- Terrain: nominal 30 m USGS 3DEP; final RIFT grid: 30 m, EPSG:5070.
- Event: July 10–12, 2023 UTC; AORC precipitation and observed USGS inflow.
- Spin-up: March 10–July 10, 2023; no rainfall, 20-year daily-mean discharge minimum
  (quantile 0) as a constant hydrograph. Daily outputs and hotstart saving are enabled.
- Outputs: `rift_spinup.cfg`, `rift_case.cfg`, the simulation plan and QC-approved inputs.
  The notebook does not execute either GPU simulation.

USGS **01138500, Connecticut River at Wells River**, is upstream of the model inlet, about
**18.70 km away in projected straight-line distance**. Its observations are assigned without routing or drainage-area scaling to the
nearest named mainstem entry inside a 60 m inward-buffered WBD polygon. Intervening ungauged
contributions are not invented. This is an illustrative boundary approximation, not a calibrated
flood model. The gauge minimum is an observed low flow, not guaranteed perennial baseflow.

The downstream named mainstem endpoint defines a 600 m terrain-matched outlet corridor.
Channel bathymetry is not inferred. Review the maps before simulation. Changing the HUC also
requires reviewing gauges, support coordinates and outlets.

The clean notebook requests fresh online inputs by default. During a legacy NLCD service outage,
a test may explicitly select previously downloaded cover, imperviousness and canopy rasters.
Their original SHA-256 catalog must match, and the final active-cell coverage check must pass.
Such a run is **fresh except for NLCD**, and its validation receipt and executed notebook must
say so. No cache or alternative dataset is selected automatically by the notebook.

[`demo_validation.json`](demo_validation.json) records the actual tested acquisition policy and timings.
[`HUC12_Demo_Executed.ipynb`](HUC12_Demo_Executed.ipynb) preserves real plots and QC results while
replacing host-specific paths. [`demo_timings.csv`](demo_timings.csv) lists every timed stage.
Use the clean notebook for your own run; provider datasets and private filesystem paths are
not distributed with the repository. No land-cover constants are preselected.

The user approved these illustrative soil values for missing processed cells: permeability
`1e-12 m²`, porosity `0.42`, residual `0.08`, and alpha `0.0002 Pa⁻¹`. They are visible in the
soil widget and YAML. Existing valid soil values and raw SSURGO are preserved; the coverage
report records original gaps and replacement counts. These are demo assumptions, not a soil
calibration or evidence that these are the true properties beneath river/lake cells.
