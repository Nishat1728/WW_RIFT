# Fixed-seed CONUS HUC validation-matrix selection

This procedure selects study areas only. It does not download basin forcing, create grids, submit
Slurm jobs, or run RIFT Input Builder.

## Authoritative source snapshot

The source is the U.S. Geological Survey Watershed Boundary Dataset FeatureServer:

`https://hydro.nationalmap.gov/arcgis/rest/services/wbd/FeatureServer`

Layers 2, 4, 5, and 6 provide HUC4, HUC8, HUC10, and HUC12 records. The snapshot requests only
attributes; geometry is explicitly disabled. The HUC code, object ID, name, WBD states, area,
TNMID, and load date are required. `hutype` and `tohuc` are requested only where the authoritative
layer schema publishes them. Records are paged in ascending HUC-code/object-ID order. Each layer's
feature count and complete object-ID set are queried before and after pagination. The build stops
if either count or ID set changes, pagination is incomplete, page IDs are duplicated, or page IDs
do not reproduce the authoritative ID set. Canonical JSONL is written with a deterministic gzip
header and both compressed-file and uncompressed-canonical SHA-256 checksums.

### Authoritative schema limitation discovered before selection

The first source-only attempt requested `hutype` and `tohuc` uniformly at every level. The official
service returned ArcGIS code 400 (`Unable to complete operation`, extended code `-2147467259`) for
HUC4. One-element query isolation showed that the supported HUC4 fields succeed, while adding
either `hutype` or `tohuc` produces the same 400. The authoritative layer schemas show:

| Level | HU type | Downstream HUC |
|---|---|---|
| HUC4 | not published | not published |
| HUC8 | not published | not published |
| HUC10 | published | not published |
| HUC12 | published | published |

No missing attribute is invented, derived, or taken from an alternate catalog. The script validates
the live schema, queries only published optional fields, and records both queried and unavailable
fields in each layer manifest.

The project search found only Lower Connecticut regional HUC files. They are not a complete CONUS
catalog and are therefore excluded as a selection source rather than silently combined with the
national service.

## Eligibility fixed before sampling

A record is eligible only when all rules pass:

1. The HUC contains only decimal digits and its length exactly equals its level.
2. The code is unique within the authoritative level snapshot.
3. WBD `states` is nonempty and every value is one of the 48 contiguous states or DC. Records
   mentioning Alaska, Hawaii, territories, Canada (`CN`), Mexico (`MX`), or another code are
   excluded. This is the reproducible definition of “entirely CONUS” for this attribute-only
   snapshot.
4. At HUC10 and HUC12, where the authoritative schema publishes WBD HU type, it must be `S`
   (standard), `C` (closed basin), `M` (multiple outlet), or `U` (urban). Frontal, island, water,
   and indeterminate units are excluded because the current RIFT source suite requires dependable
   terrestrial 3DEP/NLCD/SSURGO/AORC coverage. The schema does not publish HU type at HUC4 or HUC8,
   so this one rule is explicitly inapplicable at those levels; all other predeclared rules remain
   unchanged.
5. `AreaSqKm` is finite and greater than zero.
6. Every available parent prefix among HUC4, HUC8, and HUC10 exists in the authoritative snapshot.
7. The polygon area does not exceed the predeclared nominal 30 m cell budget below. This is a
   computational screen, not a scientific weighting. Nominal cells are `ceil(area_km² / 0.0009)`;
   actual rectangular RIFT-grid dimensions can be larger.

| Level | Maximum nominal 30 m cells | Equivalent area ceiling |
|---|---:|---:|
| HUC4 | 50,000,000 | 45,000 km² |
| HUC8 | 15,000,000 | 13,500 km² |
| HUC10 | 5,000,000 | 4,500 km² |
| HUC12 | 2,000,000 | 1,800 km² |

Every exclusion reason and count is written to `candidate_metadata.json`.

## Fixed-seed selection

The master seed is the integer `20261011`. Selection is independent of input row order and Python
random-library versions:

1. Process HUC4, HUC8, HUC10, then HUC12.
2. For every eligible code, compute
   `SHA256("<seed>:<level>:<huc>")`.
3. Rank by the digest and then HUC code.
4. Exclude a candidate when it is an ancestor or descendant of an already selected code. This
   prevents the matrix from testing the same geographic hierarchy at multiple levels.
5. Take, without replacement, 4 HUC4, 6 HUC8, 8 HUC10, and 10 HUC12 records.

Validation checks exact counts, code lengths, decimal syntax, eligibility membership, uniqueness,
no selected ancestor/descendant overlap, score recomputation, and complete deterministic
recomputation.

## Artifact order

`hpc/select_conus_huc_validation_matrix.py build` writes these artifacts before any tests:

1. `source_layer_metadata/huc*.json`
2. `wbd_authoritative_catalog.jsonl.gz`
3. `source_metadata.json`
4. `eligible_conus_hucs.jsonl.gz`
5. `candidate_metadata.json`
6. `selection_manifest.json`

The selection manifest contains the fixed seed, algorithm, source/candidate checksums, selected
records, and validation results. The `verify` command refuses checksum, eligibility, hierarchy,
count, or reproducibility drift. No unavailable code is fabricated and no alternate source is
substituted.
