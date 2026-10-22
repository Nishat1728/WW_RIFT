"""Aggregate and assess all locked fresh HUC notebook results."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


EXPECTED_COUNTS = {2: 2, 4: 4, 8: 6, 12: 10}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def aggregate(matrix_root: Path, preflight_root: Path) -> int:
    matrix_root = matrix_root.resolve()
    preflight_root = preflight_root.resolve()
    with (preflight_root / "array_index_map.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        expected_rows = list(csv.DictReader(stream))
    expected_count = sum(EXPECTED_COUNTS.values())
    if len(expected_rows) != expected_count:
        raise ValueError(
            f"Expected {expected_count} preflight rows; got {len(expected_rows)}"
        )

    records: list[dict[str, Any]] = []
    for row in sorted(expected_rows, key=lambda value: int(value["global_index"])):
        case_name = row["case_name"]
        path = matrix_root / "benchmark_results" / f"{case_name}.json"
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            record = {
                "case": case_name,
                "case_index": int(row["global_index"]),
                "huc_level": int(row["level"]),
                "huc_code": row["huc"],
                "status": "missing",
                "failures": ["benchmark result JSON is missing"],
            }
        records.append(record)

    status_counts = Counter(str(row.get("status")) for row in records)
    passed_counts = Counter(
        int(row["huc_level"]) for row in records if row.get("status") == "passed"
    )
    all_passed = (
        status_counts.get("passed", 0) == expected_count
        and all(passed_counts[level] == count for level, count in EXPECTED_COUNTS.items())
        and all(row.get("cache_mode") == "fresh" for row in records)
        and all(float(row.get("rift_dem_cellsize_m", -1)) == 30.0 for row in records)
        and all(int(row.get("native_dem_product_m", -1)) == 30 for row in records)
        and all(bool(row.get("raw_products_all_isolated")) for row in records)
    )
    summary = {
        "schema": "riftprep-locked-22-result-summary-v1",
        "created_utc": utc_now(),
        "status": "PASSED" if all_passed else "FAILED",
        "case_count": expected_count,
        "expected_counts": {str(key): value for key, value in EXPECTED_COUNTS.items()},
        "status_counts": dict(status_counts),
        "passed_counts": {str(key): passed_counts[key] for key in EXPECTED_COUNTS},
        "all_cache_modes_fresh": all(row.get("cache_mode") == "fresh" for row in records),
        "all_native_dems_30m": all(
            int(row.get("native_dem_product_m", -1)) == 30 for row in records
        ),
        "all_rift_dems_30m": all(
            float(row.get("rift_dem_cellsize_m", -1)) == 30.0 for row in records
        ),
        "all_raw_products_isolated": all(
            bool(row.get("raw_products_all_isolated")) for row in records
        ),
        "records": records,
    }
    summary_json = matrix_root / "locked_22_case_30m_summary.json"
    summary_csv = matrix_root / "locked_22_case_30m_summary.csv"
    summary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(
        [
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
                if key != "traceback"
            }
            for row in records
        ]
    ).to_csv(summary_csv, index=False)
    print(json.dumps(summary, indent=2))
    return 0 if all_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix-root", required=True)
    parser.add_argument("--preflight-root", required=True)
    args = parser.parse_args()
    return aggregate(Path(args.matrix_root), Path(args.preflight_root))


if __name__ == "__main__":
    raise SystemExit(main())
