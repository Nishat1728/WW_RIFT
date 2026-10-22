# Spin-up and transient simulations

The general notebook has separate graphical controls for **two configurations**. It prepares
inputs; it does not automatically run the GPU solver. Existing YAMLs without a `spinup` block
remain event-only. The prefilled HUC12 demo is event-only because it does not select inlet gauges.

## 1. Select the event window

Leave **Match rainfall window** checked to use the selected rainfall start/end automatically.
Uncheck it to enter the event start and end explicitly. Those dates then replace acquisition
windows for all enabled transient rainfall, inflow and coastal products, keeping their clocks
aligned. Times must include a timezone; `Z` means UTC. End must follow start.

Select the event output interval in hours, its case-relative output folder and **Save transient
hotstarts** (default ON). The same cadence controls the solver's depth, discharge, water-surface
and binary-hotstart snapshots; these are not separately scheduled outputs.

## 2. Select spin-up time and output

Keep **Prepare spin-up + transient CFGs** enabled and select the baseflow boundary stations.
The automatic window is **four calendar months immediately before the event**. Month-end dates
are clamped to the target month's last day, so four months is not always 120 days. For example,
an event starting 2024-06-30 gets a spin-up starting 2024-02-29.

Uncheck the automatic window to enter dates manually. Spin-up end must equal event start; gaps
and overlaps are rejected. Spin-up solver time begins at zero. Transient solver time continues
at the spin-up end; event forcings are evaluated at `solver_time - forcing.time_origin_s`.
UTC windows and both clocks are recorded in the YAML and `simulation_plan.json`.

Spin-up saves **once every 24 hours** by default (`86400` seconds). Change **Save every (hours)**
to another positive interval if needed. Hotstart defaults ON and can be disabled separately
from the transient setting. These switches control **periodic** checkpoints. One exact-end spin-up
checkpoint is mandatory for continuation even when periodic hotstarts are disabled. These controls
govern **saved simulation output**, not daily rainfall. Checkpoints can be large; assess disk needs
and use a suitable periodic cadence for regional grids.
Spin-up rainfall and surge are disabled regardless of the event's selected sources.
The chosen soil/infiltration and drainage physics remain enabled in spin-up where selected.
Spin-up baseflow does not silently enable or replace event inflows. Enable and configure the
transient inflow manager separately if river inflow should continue during the event.

## 3. Derive baseflow from observations, or provide a constant

### Historical USGS daily mean discharge

The default method downloads daily mean discharge (`00060`, statistic `00003`) using the
[modern USGS Water Data API](https://api.waterdata.usgs.gov/docs/ogcapi). Each response page and
station metadata are saved unchanged under `raw/spinup_baseflow/`. These are independent of
the event's instantaneous/continuous inflow records.

Choose explicit station numbers and injection locations. Optionally reuse the transient station
list, but remember that a continuous and a daily `time_series_id` may differ: omit that override
unless you know the correct daily-series identifier. If multiple daily series are returned,
the notebook stops and asks you to select one; it does not blend them.

The default history is the **20-calendar-year period before spin-up**, ending on the day before
spin-up starts. Entire daily dates are used; no partial spin-up day or event observations enter
the estimate. Change history years if scientifically justified. The daily dates follow the
provider's daily-value convention, not an invented subdaily UTC hydrograph.

The notebook then:

1. Selects only the requested station, discharge variable and daily-mean statistic.
2. Excludes null, non-finite and negative/flagged values. Genuine zero discharge is retained.
3. Uses one value per date, rejecting conflicting duplicates. It never fills missing days.
4. Reports valid/expected days, missing/invalid/zero counts, first/last dates and approval statuses.
5. Requires the user-selected record coverage (90% default). A short record is not called a
   complete 20 years. If the gate fails, inspect the gauge, choose another, revise the visible
   coverage requirement knowingly, or provide your own justified constant.
6. Computes `numpy.quantile(valid_daily_means, q, method="linear")` and converts the result to m³/s.
7. Writes a constant two-endpoint hydrograph from zero to the entire spin-up duration.
8. Checks injection support against the active domain and plots the actual processed hydrograph.

`q` is the **non-exceedance probability**, not a flow-duration exceedance label:

| Selection | Meaning |
|---|---|
| 0 (default) | Lowest valid daily mean in the historical window |
| 0.05 | Fifth percentile: 5% of sampled daily means are at/below this flow |
| 0.5 | Median daily flow |
| 1 | Maximum daily mean, normally not a low-flow spin-up choice |

The observed minimum **does not guarantee water is always present**. A zero minimum produces
a zero inflow, not an invented positive minimum. Regulated, intermittent or changing rivers
need scientific review. A flow quantile is **not** groundwater baseflow separation, an ungauged
river prediction or a calibrated boundary condition. Four months also does not prove equilibration.

Example station JSON (replace both the gauge and support for your domain):

```json
[{"site":"01184000","id":"upstream_inlet",
  "support":{"x":-72.2,"y":41.7,"crs":"EPSG:4326"},"support_width_m":30}]
```

Without `support`, the gauge location is used and checked. Never apply every gauge's full flow
to nested downstream catchments: that can double-count river inflow. Choose appropriate inlet
catchments and avoid injecting downstream total flow into an upstream model. There is no hidden
area scaling or automatic gauge-to-river assignment. A support width distributes the source;
it does not multiply discharge per covered cell.

### Explicit user constant

Choose **User constant**, then provide nonnegative `constant_m3_s` and support for every inlet:

```json
[{"id":"upstream_inlet","constant_m3_s":0.2,
  "support":{"x":-72.2,"y":41.7,"crs":"EPSG:4326"}}]
```

The constant is a user decision, not an inferred 20-year statistic. Its definition is saved in
raw provenance and disclosed in `quality_control/spinup_baseflow.json`. No historic download is
needed in this mode. Both methods preserve raw inputs and remain compatible with fresh/reuse mode.

## 4. What is generated

| Path | Purpose |
|---|---|
| `rift_spinup.cfg` | Rain-free baseflow simulation; default four months/daily output |
| `rift_case.cfg` | Transient simulation with selected event forcing |
| `simulation_plan.json` | Dates, durations, output settings and runtime handoff dependency |
| `simulation_ready/spinup_hydrographs/` | Checked constant inflow CSVs and support table |
| `quality_control/spinup_baseflow.json` | Method, daily coverage, quantile, flows, stations and units |
| `figures/processed_spinup_baseflow.png` | The constant hydrographs actually supplied to spin-up |
| `RUN.md` | Case-specific solver instructions |

The transient CFG references `simulation_ready/spinup_hotstart.bin`. This is deliberately a
**future runtime product**, absent during preparation. It is created only after spin-up runs.
The input build passing does not mean that this simulation-dependent checkpoint already exists.

## 5. Run the solver in order

After checking the maps and QC reports, build RIFT-UConn with the
[complete-restart integration](../integrations/rift_uconn/README.md). From the generated case
directory, in an appropriately configured GPU job:

```bash
riftprep simulate-stages . --executable /absolute/path/to/rift
```

The command first checks the solver's complete-restart capabilities, then runs spin-up and checks
its exit code **and reported duration**. It requires `final_hotstart.bin` at the exact phase end,
checks its version, states, grid, finiteness and checksums, copies it unchanged to the transient
restart path, then runs the event. It refuses nonempty result directories. Logs are saved as
`spinup_solver.log` and `transient_solver.log`. Raw and processed forcing files are not rewritten.

### Complete-state continuation and clocks

The handoff uses `restart.hotstart`, not `initial.water_depth`. It preserves water-surface state,
both momentum components, cumulative infiltration, dynamic soil saturation/moisture deficit,
urban retained storage, and wet/active blocks. The versioned format also saves enabled diagnostic
fields and mass-reference scalars. Static terrain and parameters are loaded from the same inputs;
scratch arrays and the new phase's forcing buffers are rebuilt rather than treated as old forcing.

The integration lands exactly on the phase end and writes a final checkpoint independently of
the periodic save cadence. There is no stale-snapshot allowance, clock reset, state interpolation
or silent depth-only fallback. `quality_control/spinup_handoff.json` records state names and hashes.
The transient resets peak/arrival/mass diagnostic tallies for event reporting, while retaining
physical momentum, infiltration history, soil saturation and stored urban water.

For a 120-day spin-up followed by a 48-hour event, the transient CFG runs from solver time
`10368000` to `10540800` seconds and sets `forcing.time_origin_s=10368000`. Its rainfall,
hydrographs, coastal grids and external drainage still use event elapsed time `0..172800`.
The offset is applied during every forcing evaluation, including initialization.

Old unversioned solver checkpoints do not contain all supported states and are rejected. Rebuild
the configuration and regenerate spin-up with the integrated solver; do not rename an old binary.
The integration is source-layout guarded and tested for the documented RIFT-UConn version, not
a claim of compatibility with every future solver or newly added physics module.

## YAML equivalent

```yaml
simulation:
  start_utc: '2023-06-10T00:00:00Z'
  end_utc: '2023-06-12T00:00:00Z'
  start_time_s: 0
  end_time_s: 172800
  output_interval_s: 3600
  output_directory: results/transient
  save_hotstart: true
spinup:
  enabled: true
  start_utc: '2023-02-10T00:00:00Z'
  end_utc: '2023-06-10T00:00:00Z'
  provider: usgs_waterdata
  history_years: 20
  quantile: 0.0
  minimum_coverage: 0.9
  stations:
    - {site: '01184000', id: upstream_inlet}
  output_interval_s: 86400
  output_directory: results/spinup
  save_hotstart: true
```

This is an illustrative fragment, not a complete runnable basin definition. Supply the domain,
other sources and scientifically appropriate inlet placement. Enabled event forcing windows must
match the transient window. A spin-up with no gauge/constant is rejected, not silently made dry.
