from pathlib import Path

import nbformat
import hashlib
import json
import pytest

from hpc.validate_demo_notebook import portable_notebook, preserved_nlcd_inputs


def test_example_export_preserves_real_values_images_and_code_but_not_host_paths():
    root = Path("/shared/example_user/RIFT_notebook/release_candidates/demo")
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(
        'print("actual result")', execution_count=1, outputs=[
            nbformat.v4.new_output("stream", name="stdout", text=f"{root}/demo_cases/actual.csv\nvalue=12.5\n"),
            nbformat.v4.new_output("display_data", data={"image/png": "aW1hZ2UtYnl0ZXM=", "text/plain": "actual plot"}),
            nbformat.v4.new_output("display_data", data={
                "application/vnd.jupyter.widget-view+json": {"model_id": "host-only-widget"},
                "text/plain": "Selected controls",
            }),
        ])])
    result = portable_notebook(notebook, root, {"huc_code": "010802050502"})
    exported = result.cells[1]
    assert exported.source == notebook.cells[0].source
    assert exported.execution_count == 1
    assert "value=12.5" in exported.outputs[0].text
    assert "/path/to/WW_RIFT/demo_cases/actual.csv" in exported.outputs[0].text
    assert exported.outputs[1].data["image/png"] == notebook.cells[0].outputs[1].data["image/png"]
    assert "application/vnd.jupyter.widget-view+json" not in exported.outputs[2].data
    assert "example_user" not in nbformat.writes(result)
    assert "example_user" in notebook.cells[0].outputs[0].text  # original run is untouched


def test_nlcd_exception_requires_original_checksums_and_copies_only_requested_products(tmp_path):
    source = tmp_path / "source_case"
    raw = source / "raw/land_cover"
    raw.mkdir(parents=True)
    records = []
    for variable in ("cover", "impervious", "canopy"):
        path = raw / f"nlcd_{variable}_2021_native.tif"
        path.write_bytes(variable.encode())
        records.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    catalog = source / "quality_control/provenance/raw_products.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(records))
    repository = tmp_path / "candidate"
    receipt = preserved_nlcd_inputs(source, repository)
    assert receipt["reused"] and not receipt["raw_product_modified"]
    assert len(receipt["artifacts"]) == 3
    for row in records:
        path = Path(row["path"])
        assert (repository / "demo_inputs/preserved_nlcd" / path.name).read_bytes() == path.read_bytes()
    Path(records[0]["path"]).write_bytes(b"modified")
    with pytest.raises(ValueError, match="Original NLCD checksum mismatch"):
        preserved_nlcd_inputs(source, repository)
