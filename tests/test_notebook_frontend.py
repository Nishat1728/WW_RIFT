import ast
import json
from pathlib import Path

import pytest

from riftprep.notebook_frontend import (
    build_case_from_widget_values,
    case_payload_from_widget_values,
    simulation_duration_seconds,
)
from riftprep.spec import case_from_mapping, load_case
from riftprep.writers import build_rift_config


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

    outlet_spec, _, _ = build_case_from_widget_values(
        {
            "case_name": "outlet_frontend_test",
            "output_root": str(tmp_path / "cases"),
            "outlet_source": "coordinates",
            "outlet_coordinates": (
                '[{"x": 653939.5, "y": 1584239.8, "width_m": 120, '
                '"cut_elevation_m": 0}]'
            ),
            "outlet_crs": "EPSG:5070",
            "outlet_elevation": "constant",
        },
        environment={}, config_directory=tmp_path / "configs",
    )
    assert outlet_spec.outlets is not None
    assert outlet_spec.outlets.coordinates[0]["width_m"] == 120.0
    assert outlet_spec.outlets.coordinates[0]["cut_elevation_m"] == 0.0

    vector_spec, _, _ = build_case_from_widget_values(
        {
            "case_name": "vector_outlet_frontend_test",
            "output_root": str(tmp_path / "cases"),
            "outlet_source": "vector",
            "outlet_vector_path": str(tmp_path / "outlets.gpkg"),
        },
        environment={}, config_directory=tmp_path / "configs",
    )
    assert vector_spec.outlets is not None
    assert vector_spec.outlets.source.endswith("outlets.gpkg")


def test_frontend_backend_rejects_invalid_json_and_missing_local_paths():
    with pytest.raises(ValueError, match="JSON object"):
        case_payload_from_widget_values({"land_fill": "[]"})
    with pytest.raises(ValueError, match="Local DEM path"):
        case_payload_from_widget_values({"dem_source": "local"})


@pytest.mark.parametrize("end,seconds", [
    ("2023-06-10T01:30:00Z", 5400),
    ("2023-06-12T00:00:00Z", 172800),
    ("2023-06-13T00:00:00+02:00", 252000),
])
def test_rainfall_window_reaches_saved_spec_and_rift_config(tmp_path, end, seconds):
    spec, saved_path, _ = build_case_from_widget_values(
        {"case_name": "duration", "output_root": str(tmp_path / "cases"),
         "rain_start": "2023-06-10T00:00:00Z", "rain_end": end},
        environment={}, config_directory=tmp_path / "configs",
    )
    assert spec.simulation.start_time_s == 0
    assert load_case(saved_path).simulation.end_time_s == seconds
    config_path = build_rift_config(tmp_path / "rift.cfg", spec.simulation, {}, {})
    config = config_path.read_text()
    assert f"simulation.end_time_s = {seconds:g}" in config


@pytest.mark.parametrize("start,end,message", [
    ("2023-06-10T00:00:00Z", "2023-06-10T00:00:00Z", "later"),
    ("2023-06-10T00:00:00Z", "2023-06-09T00:00:00Z", "later"),
    ("2023-06-10", "2023-06-11", "timezone"),
    ("invalid", "2023-06-11T00:00:00Z", "ISO"),
])
def test_invalid_rainfall_period_rejected_before_case_creation(tmp_path, start, end, message):
    with pytest.raises(ValueError, match=message):
        build_case_from_widget_values(
            {"rain_start": start, "rain_end": end}, environment={},
            config_directory=tmp_path / "configs",
        )
    assert not (tmp_path / "configs").exists()


def test_disabled_rainfall_uses_explicit_duration_and_ignores_unused_dates():
    payload = case_payload_from_widget_values({
        "rain_enabled": False, "rain_start": "unused", "rain_end": "unused",
        "simulation_duration_hours": 0.25,
    })
    assert payload["simulation"]["end_time_s"] == 900
    assert payload["simulation"]["output_interval_s"] == 900
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive finite"):
            simulation_duration_seconds({"rain_enabled": False,
                                         "simulation_duration_hours": invalid})


def test_yaml_infers_duration_unless_end_is_explicit():
    payload = case_payload_from_widget_values({"rain_end": "2023-06-12T00:00:00Z"})
    payload["simulation"] = {"start_time_s": 3600}
    assert case_from_mapping(payload).simulation.end_time_s == 176400
    payload["simulation"]["end_time_s"] = 7200
    assert case_from_mapping(payload).simulation.end_time_s == 7200


def test_huc12_demo_uses_public_sources_and_infers_48_hours():
    root = Path(__file__).resolve().parents[1]
    spec = load_case(root / "examples" / "demo_huc12_180101080101.yaml")
    assert spec.domain.huc_code == "180101080101"
    assert spec.domain.cell_size_m == spec.dem.options["resolution_m"] == 30
    assert spec.cache.mode == "fresh"
    assert spec.simulation.end_time_s == 172800
    assert not spec.land_cover.options["manual_fill_values"]
    assert not spec.soil.options["manual_fill_values"]
    demo = json.loads((root / "examples" / "HUC12_Demo.ipynb").read_text())
    for index, cell in enumerate(demo["cells"]):
        source = "".join(cell["source"])
        if cell["cell_type"] == "code" and not source.lstrip().startswith("%"):
            ast.parse(source, filename=f"demo-cell-{index}")
