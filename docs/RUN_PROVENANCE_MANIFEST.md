# Run provenance manifest

Every workflow attempt writes a stable `riftprep-run-provenance-v1` JSON document to:

- `quality_control/provenance/run_manifest.json` — latest state; and
- `quality_control/provenance/run_manifests/<run-id>.json` — that attempt's history record.

The same run ID advances from `running` to `raw_complete` and then to `complete`, or to `failed`
when a controlled workflow exception occurs. A later `cache.mode: reuse` attempt receives a new run
ID and links `resumed_from_run_id` to the previous manifest when that information exists. A reused
legacy case without a prior run manifest leaves the link null and lists
`run.resumed_from_run_id` in `missing_metadata`; the workflow never fabricates it.

The manifest contains the sanitized resolved configuration; Python, package, platform, and Git
version information when available; the directory contract; acquisition-event reference and
events belonging to the attempt; raw, processed, simulation-ready, and root-output artifact
inventories; wall-clock stage outcomes; explicit automatic and user-selected fallbacks; warnings;
quality-control references; and a redacted terminal failure record. Every derived/final artifact
record includes its absolute and case-relative path, byte count, and SHA-256 checksum. Raw entries
reuse the immutable raw-product ledger, including its provider metadata and original acquisition
timestamps.

Credentials are sanitized recursively using the same rules as the raw-product ledger. The
manifest records a limited environment/version snapshot and never dumps general environment
variables. The normative machine-readable structure is
[`run_provenance_manifest.schema.json`](run_provenance_manifest.schema.json).

The pre-existing `case_manifest.json` remains available with its `riftprep-case-v2` fields for
backward-compatible result readers. It now links to the additive run provenance manifest.
