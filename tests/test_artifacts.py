from pathlib import Path

import pytest

from riftprep.artifacts import CASE_MARKER, prepare_case_directory
from riftprep.config import DomainSpec
from riftprep.spec import CaseSpec, ProductSpec


def test_fresh_mode_refuses_unmarked_directory(tmp_path: Path):
    case = tmp_path / "unmarked"
    case.mkdir()
    (case / "user_file.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmarked"):
        prepare_case_directory(case, "fresh")
    assert (case / "user_file.txt").read_text(encoding="utf-8") == "keep me"


def test_fresh_mode_replaces_only_marked_case(tmp_path: Path):
    case = tmp_path / "case"
    paths = prepare_case_directory(case, "reuse")
    old = paths.raw / "old.bin"
    old.write_bytes(b"old")
    paths2 = prepare_case_directory(case, "fresh")
    assert not old.exists()
    assert (paths2.root / CASE_MARKER).is_file()
    assert paths2.provider_cache.is_dir()


def test_rift_3dep_source_is_restricted_to_10_or_30_m(tmp_path: Path):
    with pytest.raises(ValueError, match="must be 10 or 30"):
        CaseSpec(
            case_name="invalid_60m_source",
            output_root=str(tmp_path),
            domain=DomainSpec(mode="rectangle", bounds=(-72.3, 41.7, -72.2, 41.8)),
            dem=ProductSpec(
                provider="watershed_workflow_3dep", options={"resolution_m": 60}
            ),
            rainfall=ProductSpec(enabled=False, provider="none"),
        )

    valid = CaseSpec(
        case_name="arbitrary_rift_grid",
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.3, 41.7, -72.2, 41.8),
            cell_size_m=17.5,
        ),
        dem=ProductSpec(
            provider="watershed_workflow_3dep", options={"resolution_m": 10}
        ),
        rainfall=ProductSpec(enabled=False, provider="none"),
    )
    assert valid.domain.cell_size_m == 17.5
