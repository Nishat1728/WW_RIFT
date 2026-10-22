from __future__ import annotations

from hpc.select_conus_huc_validation_matrix import (
    FIXED_SEED,
    HU_TYPE_LEVELS,
    LEVELS,
    SAMPLE_COUNTS,
    eligibility_reasons,
    query_fields,
    select_hucs,
    validate_selection,
    write_jsonl_gzip,
)


def _metadata(*names: str) -> dict:
    return {
        "fields": [{"name": name} for name in names],
    }


def _eligible(level: int, code: str) -> dict:
    return {
        "level": level,
        "huc": code,
        "name": code,
        "states_raw": "CT",
        "states": ["CT"],
        "hu_type": "S" if level in HU_TYPE_LEVELS else None,
        "area_sqkm": 1.0,
        "to_huc": None,
        "tnmid": code,
        "load_date_epoch_ms": 0,
        "object_id": int(code),
        "nominal_30m_cells": 1112,
    }


def test_query_fields_uses_only_authoritative_optional_fields() -> None:
    common = ("objectid", "tnmid", "loaddate", "areasqkm", "states", "name")
    huc4_fields, huc4_unavailable = query_fields(4, _metadata(*common, "huc4"))
    assert huc4_fields == ["huc4", "tnmid", "loaddate", "areasqkm", "states", "name"]
    assert huc4_unavailable == ["hutype", "tohuc"]

    huc2_fields, huc2_unavailable = query_fields(2, _metadata(*common, "huc2"))
    assert huc2_fields == ["huc2", "tnmid", "loaddate", "areasqkm", "states", "name"]
    assert huc2_unavailable == ["hutype", "tohuc"]

    huc12_fields, huc12_unavailable = query_fields(
        12, _metadata(*common, "huc12", "hutype", "tohuc")
    )
    assert huc12_fields[-2:] == ["hutype", "tohuc"]
    assert huc12_unavailable == []


def test_hu_type_rule_applies_only_where_wbd_publishes_it() -> None:
    all_codes = {2: {"01"}, 4: {"0101"}, 8: {"01010001"}, 12: set()}
    huc4 = _eligible(4, "0101")
    huc4["hu_type"] = None
    assert eligibility_reasons(huc4, all_codes) == []

    huc12 = _eligible(12, "010100010101")
    huc12["hu_type"] = "F"
    assert eligibility_reasons(huc12, all_codes) == ["excluded_hu_type"]


def test_fixed_seed_selection_is_order_independent_and_nonoverlapping() -> None:
    eligible = []
    eligible.extend(_eligible(2, f"{10 + index:02d}") for index in range(4))
    eligible.extend(_eligible(4, f"2{index:03d}") for index in range(8))
    eligible.extend(_eligible(8, f"30{index:06d}") for index in range(12))
    eligible.extend(_eligible(12, f"40{index:010d}") for index in range(16))

    selected = select_hucs(eligible, FIXED_SEED)
    reversed_selected = select_hucs(list(reversed(eligible)), FIXED_SEED)
    assert selected == reversed_selected
    assert {level: len(selected[level]) for level in LEVELS} == SAMPLE_COUNTS
    checks = validate_selection(selected, eligible, FIXED_SEED)
    assert all(checks.values())


def test_selection_contains_no_replacement_at_any_level() -> None:
    eligible = []
    eligible.extend(_eligible(2, f"{50 + index:02d}") for index in range(2))
    eligible.extend(_eligible(4, f"6{index:03d}") for index in range(4))
    eligible.extend(_eligible(8, f"70{index:06d}") for index in range(6))
    eligible.extend(_eligible(12, f"80{index:010d}") for index in range(10))
    selected = select_hucs(eligible, FIXED_SEED)
    codes = [row["huc"] for level in LEVELS for row in selected[level]]
    assert len(codes) == len(set(codes)) == sum(SAMPLE_COUNTS.values())


def test_catalog_metadata_uses_portable_relative_path(tmp_path) -> None:
    metadata = write_jsonl_gzip(tmp_path / "catalog.jsonl.gz", [{"huc": "0101"}])
    assert metadata["path"] == "catalog.jsonl.gz"
    assert not metadata["path"].startswith(("/", "\\"))
