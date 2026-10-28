# Notebook structure review

The general notebook retains the staged source-manager approach of Watershed Workflow's
[Coweeta example](https://github.com/environmental-modeling-workflows/watershed-workflow/blob/master/examples/Coweeta/coweeta_ats.ipynb):
choose parameters and managers, obtain basin/river geometry, obtain surface and subsurface
properties and forcing, then write model inputs. RIFT-specific grid, boundary and restart
contracts remain in the backend. No application framework or hidden source substitution is added.

Every cell in the 54-cell general notebook was reviewed. The paired headings and code cells cover:

| Cells (zero-based) | Review/result |
|---|---|
| 0–4 | Introduction, imports, source inventory. Locate the repository before importing its package; retain executable-source/roadmap separation. |
| 5–8 | Domain, grid, cache, DEM. Retain independent source/model resolution and explicit fresh/reuse controls. |
| 9–14 | Initial depth, hydrography, bathymetry. Retain confirmed-empty rivers and explicit derived bathymetry. |
| 15–18 | Land/soil controls. Correct legacy NLCD label, preserve raw flags and user-only null decisions. |
| 19–26 | Rainfall, optional boundaries, output controls, spin-up. Correct duration-control enablement; retain UTC alignment, four calendar months and explicit gauge selection. |
| 27–30 | Manager setup/domain. Block stale settings after user edits; display resolved source/configuration. |
| 31–46 | Each raw manager. Show acquisition progress, preserve failure evidence, display accepted products only after QC. |
| 47–50 | Complete raw catalog and build. Require current settings, all enabled products and processing QC; retain diagnostic maps and explicit corrections. |
| 51–53 | Timings, provenance, final plots and output contract. Retain separate native/RIFT products and both CFGs; disclose any example reuse. |

NLCD service metadata is checked using an uncached, timed HTTP request before third-party CRS
parsing. HTTP errors, HTML error pages and missing WMS metadata produce provider errors, with
bounded retries and exact response/checksum diagnostics. An actual source or coordinate-system
choice is never changed to make a failed request pass.

WBD retries alternate equivalent exact-match query forms after gateway errors.
HUC codes must be numeric, no wildcard is introduced, requests bypass HTTP caches, and
every returned feature must identify the requested HUC. The provider and watershed stay the same.

The terrain comparison uses a shared elevation colour scale, excluding artificial walls and
including actual outlet cuts, so unchanged elevations look the same across all panels.

Offline tests exercise real widget callbacks, configuration contracts, source/processing
fixtures, outage responses, stale settings, raw preservation and restart handling. A separate
live HUC12 notebook run validates the full input-preparation path. These checks do not establish
that every external provider is always available or that a basin model is scientifically calibrated.
