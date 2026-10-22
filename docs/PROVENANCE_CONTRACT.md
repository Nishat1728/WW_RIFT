# Raw-input provenance contract

RIFT Input Builder maintains two append-compatible provenance views under
`quality_control/provenance/`.

It also maintains the versioned run-level view described in
[`RUN_PROVENANCE_MANIFEST.md`](RUN_PROVENANCE_MANIFEST.md), which links acquisition records to
processing stages, derived/final checksums, quality control, warnings, fallbacks, and terminal
status without replacing either ledger below.

## Raw artifacts

`raw_products.json` and `raw_products.csv` describe files accepted into `raw/`. Each v2 record
contains:

- product and provider identifiers;
- artifact path and byte count;
- SHA-256 checksum;
- source URL when explicitly available;
- selected source options and effective spatial query context;
- provider-returned product metadata;
- original acquisition start and completion timestamps;
- embedded CRS metadata with `known`, `not_applicable`, or `unavailable` status; and
- `missing_metadata`, which names absent values rather than inventing them.

Provider-normalized adapter outputs under `processed/` are not registered as raw artifacts. Older
v1 list records load unchanged. Unknown v1 values remain null or unavailable and are identified as
missing; they are never reconstructed from file modification times.

For DEMs, provenance distinguishes the provider response from reproducible derivatives. USGS
3DEP records identify the saved Watershed Workflow/py3dep response snapshot and disclose that the
upstream WMS selected the requested resolution/CRS and that py3dep applied its request-geometry
mask before riftprep received the dataset. Copernicus records point to the unchanged public COG
tiles. Converted GeoTIFFs, mosaics, and RIFT-aligned grids are not entered as raw artifacts.
`quality_control/dem_lineage.json` links those raw records to the derived files under `processed/`
and the final `simulation_ready/dem.asc` without relabeling any derived file as native.

## Acquisition events

`acquisition_events.json` and `acquisition_events.csv` record each manager attempt with status
`succeeded`, `reused`, or `failed`. A cached reuse adds a new event while preserving the artifact's
original acquisition timestamp. Failed attempts record error type, a redacted error message, and
the path, size, and SHA-256 checksum of each new or changed partial response retained under the
input's `raw/` directory. Partial files are evidence, not valid raw products.

## Secret handling

Before either ledger is written, nested credential-like fields are replaced by `[REDACTED]`.
Redaction covers API keys, tokens, passwords, secrets, authorization values, cookies, credentials,
bearer tokens, URL user information, and sensitive URL query parameters. The workflow records
missing metadata explicitly and does not place environment credentials into query dictionaries.
