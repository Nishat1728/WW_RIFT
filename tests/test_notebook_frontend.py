import ast
import json
from pathlib import Path

import pytest

from riftprep.notebook_frontend import (
    build_case_from_widget_values,
    case_payload_from_widget_values,
)


def test_published_notebook_cells_are_parseable_and_user_facing():
    root = Path(__file__).resolve().parents[1]
    notebook = json.loads((root / "RIFT_Input_Builder.ipynb").read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    assert all(cell.get("id") for cell in notebook["cells"])
    joined = "\n".join("".join(cell["source"]) for cell in notebook["cells"])
    assert joined.count("✏️ USER EDIT") >= 9
    assert "import json" in joined
    assert "import ipywidgets as widgets" in joined
    assert joined.count("widgets.Dropdown(") >= 15
    assert "SUPPORTED_CATALOG_KEYS" in joined
    assert "every row below has an executable backend" in joined
    assert "NEEDS USER INPUT" in joined
    assert "initial_state" in joined
    assert "land_cover_percentage_over_100_before_correction_map.png" in joined
    assert "land_cover_percentage_after_correction_map.png" in joined
    for provider in (
        "copernicus_glo30", "soilgrids", "daymet", "mrms", "era5_land"
    ):
        assert provider in joined
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        assert len(source.splitlines()) <= 150
        if source.lstrip().startswith("%"):
            # The import cell begins with valid IPython magics.
            continue
        ast.parse(source, filename=f"notebook-cell-{index}")


def test_frontend_generator_and_backend_cover_the_display_contract():
    root = Path(__file__).resolve().parents[1]
    generator = (root / "tools" / "build_general_notebook.py").read_text(encoding="utf-8")
    managers = (root / "riftprep" / "managers.py").read_text(encoding="utf-8")
    frontend_backend = (root / "riftprep" / "notebook_frontend.py").read_text(
        encoding="utf-8"
    )
    workflow = (root / "riftprep" / "workflow.py").read_text(encoding="utf-8")

    assert "SUPPORTED_CATALOG_KEYS" in generator
    assert "CASE_CONFIG_OVERRIDE_WIDGET" in generator
    assert "build_case_from_widget_values" in generator
    assert "snapshot_widget_values" in generator
    assert "residual_porosity_tolerance" in frontend_backend
    assert "RIFTPREP_CASE_CONFIG" in frontend_backend
    assert "raw_initial_state_selected_product.png" in managers
    assert '"processed_inflows"' in workflow
    assert '"processed_coastal_stage"' in workflow


def test_frontend_backend_builds_default_and_alternate_source_payloads(tmp_path):
    payload = case_payload_from_widget_values({})
    assert payload["domain"]["huc_code"] == "010802050801"
    assert payload["dem"]["options"]["resolution_m"] == 30
    assert payload["rainfall"]["provider"] == "watershed_workflow_aorc"

    values = {
        "case_name": "frontend_test",
        "output_root": str(tmp_path / "cases"),
        "dem_source": "copernicus_glo30",
        "soil_source": "soilgrids",
        "rain_source": "daymet",
        "daymet_region": "hi",
    }
    spec, config_path, override_used = build_case_from_widget_values(
        values, environment={}, config_directory=tmp_path / "configs"
    )
    assert not override_used
    assert config_path.is_file()
    assert spec.dem.provider == "copernicus_glo30"
    assert spec.soil.provider == "soilgrids"
    assert spec.rainfall.provider == "daymet"
    assert spec.rainfall.options["region"] == "hi"


def test_frontend_backend_rejects_invalid_json_and_missing_local_paths():
    with pytest.raises(ValueError, match="JSON object"):
        case_payload_from_widget_values({"land_fill": "[]"})
    with pytest.raises(ValueError, match="Local DEM path"):
        case_payload_from_widget_values({"dem_source": "local"})
