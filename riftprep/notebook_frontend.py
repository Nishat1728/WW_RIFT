"""Translate the graphical notebook controls into a validated case specification.

This module intentionally has no dependency on IPython or ipywidgets.  The notebook
collects the current ``.value`` of each control and hands the plain mapping to this
module, keeping configuration rules testable and out of the public front end.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from .spec import CaseSpec, case_from_mapping, load_case, rainfall_period_seconds, save_case


def simulation_duration_seconds(values: Mapping[str, Any]) -> float:
    """Match rainfall when enabled; otherwise use the explicit dry-run duration."""
    if bool(values.get("rain_enabled", True)):
        return rainfall_period_seconds({
            "start_utc": values.get("rain_start", "2023-06-10T00:00:00Z"),
            "end_utc": values.get("rain_end", "2023-06-11T00:00:00Z"),
        })
    try:
        hours = float(values.get("simulation_duration_hours", 24))
    except (TypeError, ValueError) as exc:
        raise ValueError("Simulation duration without rainfall must be numeric hours.") from exc
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("Simulation duration without rainfall must be positive finite hours.")
    return hours * 3600


def snapshot_widget_values(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Return values from every ``*_WIDGET`` object in a notebook namespace."""

    values: dict[str, Any] = {}
    for name, control in namespace.items():
        if name.endswith("_WIDGET") and hasattr(control, "value"):
            values[name.removesuffix("_WIDGET").lower()] = control.value
    return values


def _value(values: Mapping[str, Any], name: str, default: Any = None) -> Any:
    return values.get(name, default)


def _optional_text(values: Mapping[str, Any], name: str, default: str = "") -> str | None:
    value = _value(values, name, default)
    text = "" if value is None else str(value).strip()
    return text or None


def _required_text(
    values: Mapping[str, Any], name: str, label: str, default: str = ""
) -> str:
    text = _optional_text(values, name, default)
    if text is None:
        raise ValueError(f"{label} is required for the selected option.")
    return text


def _json_value(
    values: Mapping[str, Any], name: str, label: str, expected_type: type
) -> Any:
    value = _value(values, name, "{}" if expected_type is dict else "[]")
    if isinstance(value, expected_type):
        return value
    text = "" if value is None else str(value).strip()
    if not text:
        return expected_type()
    parsed = json.loads(text)
    if not isinstance(parsed, expected_type):
        kind = "object" if expected_type is dict else "list"
        raise ValueError(f"{label} must be a JSON {kind}.")
    return parsed


def _validate_outlet_coordinates(rows: list[Any]) -> list[dict[str, Any]]:
    """Validate the user-facing outlet JSON before it reaches GeoPandas."""
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"Outlet coordinates row {index + 1} must be a JSON object.")
        item = dict(row)
        for key in ("x", "y"):
            if key not in item:
                raise ValueError(f"Outlet coordinates row {index + 1} requires '{key}'.")
            try:
                value = float(item[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Outlet coordinates row {index + 1} field '{key}' must be numeric."
                ) from exc
            if not math.isfinite(value):
                raise ValueError(f"Outlet coordinates row {index + 1} field '{key}' must be finite.")
            item[key] = value
        if "width_m" in item:
            try:
                width = float(item["width_m"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Outlet row {index + 1} width_m must be numeric.") from exc
            if not math.isfinite(width) or width <= 0:
                raise ValueError(f"Outlet row {index + 1} width_m must be greater than zero.")
            item["width_m"] = width
        if "cut_elevation_m" in item:
            try:
                elevation = float(item["cut_elevation_m"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Outlet row {index + 1} cut_elevation_m must be numeric."
                ) from exc
            if not math.isfinite(elevation):
                raise ValueError(
                    f"Outlet row {index + 1} cut_elevation_m must be finite."
                )
            item["cut_elevation_m"] = elevation
        validated.append(item)
    return validated


def case_payload_from_widget_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete provider-neutral configuration from plain widget values."""

    duration_seconds = simulation_duration_seconds(values)
    domain_mode = _value(values, "domain_mode", "huc")
    if domain_mode == "huc":
        domain = {
            "mode": "huc",
            "huc_code": _required_text(values, "huc_code", "HUC code", "010802050801"),
            "huc_level": _value(values, "huc_level"),
        }
    elif domain_mode == "shapefile":
        domain = {
            "mode": "shapefile",
            "vector_path": _required_text(values, "shapefile_path", "Polygon file"),
            "vector_layer": _optional_text(values, "shapefile_layer"),
        }
    elif domain_mode == "rectangle":
        domain = {
            "mode": "rectangle",
            "bounds": [
                _value(values, "rectangle_xmin", -72.30),
                _value(values, "rectangle_ymin", 41.60),
                _value(values, "rectangle_xmax", -72.15),
                _value(values, "rectangle_ymax", 41.75),
            ],
            "bounds_crs": _required_text(
                values, "rectangle_crs", "Rectangle CRS", "EPSG:4326"
            ),
        }
    else:
        raise ValueError(f"Unsupported study-area mode: {domain_mode}")
    domain.update(
        {
            "target_crs": _required_text(
                values, "target_crs", "RIFT target CRS", "EPSG:5070"
            ),
            "cell_size_m": _value(values, "rift_grid_resolution", 30.0),
            "download_buffer_cells": _value(values, "download_buffer", 2),
            "boundary_rule": _value(values, "boundary_rule", "cell_center"),
            "outside_dem_mode": _value(values, "outside_dem", "wall"),
            "wall_elevation_m": _value(values, "wall_elevation", 9999.0),
        }
    )

    dem_choice = _value(values, "dem_source", "3dep_30")
    if dem_choice in {"3dep_10", "3dep_30"}:
        dem = {
            "enabled": True,
            "provider": "watershed_workflow_3dep",
            "options": {
                "resolution_m": 10 if dem_choice == "3dep_10" else 30,
                "tile_degrees": _value(values, "dem_tile_degrees", 0.5),
                "max_attempts": _value(values, "dem_max_attempts", 8),
            },
        }
    elif dem_choice == "copernicus_glo30":
        dem = {
            "enabled": True,
            "provider": "copernicus_glo30",
            "options": {
                "resolution_m": 30,
                "max_attempts": _value(values, "dem_max_attempts", 8),
            },
        }
    elif dem_choice == "local":
        dem = {
            "enabled": True,
            "provider": "local",
            "options": {
                "path": _required_text(values, "dem_local_path", "Local DEM path")
            },
        }
    else:
        raise ValueError(f"Unsupported DEM source: {dem_choice}")

    initial_choice = _value(values, "initial_source", "dry_zero")
    initial_options: dict[str, Any] = {}
    if initial_choice == "constant":
        initial_options["depth_m"] = _value(values, "initial_depth", 0.0)
    elif initial_choice == "local":
        initial_options.update(
            {
                "path": _required_text(
                    values, "initial_local_path", "Local initial-depth path"
                ),
                "resampling": _value(values, "initial_resampling", "bilinear"),
            }
        )
    elif initial_choice != "dry_zero":
        raise ValueError(f"Unsupported initial-state source: {initial_choice}")
    initial_state = {"enabled": True, "provider": initial_choice, "options": initial_options}

    hydro_enabled = bool(_value(values, "hydro_enabled", True))
    hydro_provider = _value(values, "hydro_source", "usgs_3dhp")
    hydro_options = (
        {
            "path": _required_text(
                values, "hydro_local_path", "Local hydrography path"
            )
        }
        if hydro_provider == "local" and hydro_enabled
        else {
            "tile_degrees": _value(values, "hydro_tile_degrees", 1.0),
            "download_workers": _value(values, "hydro_workers", 4),
        }
    )
    hydrography = {
        "enabled": hydro_enabled,
        "provider": hydro_provider,
        "options": hydro_options,
    }

    bathy_enabled = bool(_value(values, "bathy_enabled", False))
    bathy_depth_method = _value(values, "bathy_depth_method", "constant")
    bathy_width_method = _value(values, "bathy_width_method", "constant")
    if bathy_enabled and not hydro_enabled:
        raise ValueError("River bathymetry requires hydrography to be enabled.")
    if bathy_enabled and bathy_depth_method == "attribute":
        _required_text(values, "bathy_depth_field", "Depth attribute")
    if bathy_enabled and bathy_depth_method == "stream_order":
        _required_text(values, "bathy_order_field", "Stream-order attribute")
    if bathy_enabled and bathy_width_method == "attribute":
        _required_text(values, "bathy_width_field", "Width attribute")
    river_bathymetry = {
        "source": hydro_provider,
        "apply_bathymetry": bathy_enabled,
        "depth_method": bathy_depth_method,
        "depth_m": _value(values, "bathy_depth", 1.5),
        "depth_field": _optional_text(values, "bathy_depth_field"),
        "stream_order_field": _optional_text(values, "bathy_order_field"),
        "depth_by_order": _json_value(
            values, "bathy_depth_by_order", "Depth by order", dict
        ),
        "width_method": bathy_width_method,
        "width_m": _value(values, "bathy_width", 30.0),
        "width_field": _optional_text(values, "bathy_width_field"),
        "cross_section": _value(values, "bathy_section", "gaussian"),
        "preserve_existing_lower_terrain": bool(
            _value(values, "bathy_preserve", True)
        ),
        "enforce_downstream_monotonicity": bool(
            _value(values, "bathy_monotonic", False)
        ),
        "downstream_direction": _value(values, "bathy_direction", "south"),
        "minimum_downstream_slope": _value(values, "bathy_min_slope", 0.0),
    }

    land_enabled = bool(_value(values, "land_enabled", True))
    land_provider = _value(values, "land_source", "watershed_workflow_nlcd")
    land_options = {
        "manual_fill_values": _json_value(
            values, "land_fill", "Land-cover constants", dict
        ),
        "manual_dimension_constants": _json_value(
            values, "land_dimension", "Malformed-dimension constants", dict
        ),
    }
    if land_provider == "local" and land_enabled:
        land_options["rasters"] = {
            "cover": _required_text(values, "land_cover_path", "Local cover raster"),
            "impervious": _required_text(
                values, "land_impervious_path", "Local impervious raster"
            ),
            "canopy": _required_text(values, "land_canopy_path", "Local canopy raster"),
        }
    else:
        land_options.update(
            {
                "year": _value(values, "land_year", 2021),
                "location": _required_text(
                    values, "land_location", "NLCD location", "L48"
                ),
                "variables": ["cover", "impervious", "canopy"],
                "tile_degrees": _value(values, "land_tile_degrees", 1.0),
                "tile_overlap_degrees": _value(values, "land_tile_overlap", 0.02),
                "download_workers": _value(values, "land_workers", 4),
                "max_attempts": _value(values, "land_attempts", 8),
            }
        )
    land_cover = {
        "enabled": land_enabled,
        "provider": land_provider,
        "options": land_options,
    }

    soil_enabled = bool(_value(values, "soil_enabled", True))
    soil_provider = _value(values, "soil_source", "watershed_workflow_ssurgo")
    soil_options = {
        "initial_saturation": _value(values, "soil_initial_saturation", 0.5),
        "residual_porosity_tolerance": _value(values, "soil_tolerance", 0.1),
        "root_depth_m": _value(values, "soil_root_depth", 0.5),
        "drainage_time_s": _value(values, "soil_drainage_time", 172800),
        "manual_fill_values": _json_value(values, "soil_fill", "Soil constants", dict),
    }
    if soil_provider == "local" and soil_enabled:
        soil_options["path"] = _required_text(
            values, "soil_local_path", "Local soil path"
        )
    elif soil_provider == "soilgrids":
        soil_options.update(
            {
                "depth": _value(values, "soilgrids_depth", "0-5cm"),
                "quantile": _value(values, "soilgrids_quantile", "Q0.5"),
                "max_attempts": _value(values, "soil_attempts", 8),
            }
        )
    else:
        soil_options.update(
            {
                "tile_degrees": _value(values, "soil_tile_degrees", 0.25),
                "download_workers": _value(values, "soil_workers", 4),
                "max_attempts": _value(values, "soil_attempts", 8),
                "request_timeout_seconds": _value(values, "soil_timeout", 120),
                "max_subdivision_depth": _value(values, "soil_subdivision", 2),
            }
        )
    soil = {"enabled": soil_enabled, "provider": soil_provider, "options": soil_options}

    rain_enabled = bool(_value(values, "rain_enabled", True))
    rain_provider = _value(values, "rain_source", "watershed_workflow_aorc")
    rain_variable = _required_text(
        values, "rain_variable", "Rainfall variable", "APCP_surface"
    )
    rain_options = {
        "start_utc": _required_text(
            values, "rain_start", "Rainfall start UTC", "2023-06-10T00:00:00Z"
        ),
        "end_utc": _required_text(
            values, "rain_end", "Rainfall end UTC", "2023-06-11T00:00:00Z"
        ),
        "variables": [rain_variable],
        "precipitation_variable": rain_variable,
        "uncovered_policy": _value(values, "rain_policy", "zero"),
        "max_attempts": _value(values, "rain_attempts", 8),
    }
    if rain_provider == "local" and rain_enabled:
        rain_options["path"] = _required_text(
            values, "rain_local_path", "Local rainfall path"
        )
    elif rain_provider == "daymet":
        rain_options.update(
            {
                "daily_distribution": _value(
                    values, "daymet_distribution", "uniform_24h"
                ),
                "region": _value(values, "daymet_region", "na"),
            }
        )
    elif rain_provider == "mrms":
        rain_options.update(
            {
                "product": _value(
                    values, "mrms_product", "MultiSensor_QPE_01H_Pass2_00.00"
                ),
                "region": _value(values, "mrms_region", "CONUS"),
            }
        )
    elif rain_provider not in {"watershed_workflow_aorc", "era5_land"}:
        raise ValueError(f"Unsupported rainfall source: {rain_provider}")
    rainfall = {
        "enabled": rain_enabled,
        "provider": rain_provider,
        "options": rain_options,
    }

    inflow_enabled = bool(_value(values, "inflow_enabled", False))
    inflow_stations = _json_value(values, "inflow_stations", "Inflow stations", list)
    if inflow_enabled and not inflow_stations:
        raise ValueError("Enabled river inflows require at least one explicit station.")
    inflows = {
        "enabled": inflow_enabled,
        "provider": _value(values, "inflow_source", "usgs_waterdata"),
        "options": {
            "start_utc": _required_text(
                values, "inflow_start", "Inflow start UTC", "2023-06-10T00:00:00Z"
            ),
            "end_utc": _required_text(
                values, "inflow_end", "Inflow end UTC", "2023-06-11T00:00:00Z"
            ),
            "stations": inflow_stations,
        },
    }

    coastal_enabled = bool(_value(values, "coastal_enabled", False))
    coastal_stations = _json_value(
        values, "coastal_stations", "Coastal stations", list
    )
    if coastal_enabled and len(coastal_stations) != 1:
        raise ValueError("Enabled coastal stage requires exactly one explicit station.")
    coastal = {
        "enabled": coastal_enabled,
        "provider": _value(values, "coastal_source", "usgs_waterdata"),
        "options": {
            "start_utc": _required_text(
                values, "coastal_start", "Coastal start UTC", "2023-06-10T00:00:00Z"
            ),
            "end_utc": _required_text(
                values, "coastal_end", "Coastal end UTC", "2023-06-11T00:00:00Z"
            ),
            "interval_hours": _value(values, "coastal_interval", 1),
            "coastal_radius_m": _value(values, "coastal_radius", 3000.0),
            "stations": coastal_stations,
        },
    }

    drainage_enabled = bool(_value(values, "drainage_enabled", False))
    drainage_provider = _value(values, "drainage_source", "synthetic_nlcd")
    drainage_options = {
        "impervious_threshold_pct": _value(values, "drainage_threshold", 80.0)
    }
    if drainage_enabled and drainage_provider in {"local_vector", "local_raster"}:
        controls = _json_value(values, "drainage_controls", "Hydraulic controls", list)
        if not controls:
            raise ValueError(
                "Enabled local drainage requires at least one hydraulic-control row."
            )
        drainage_options.update(
            {
                "path": _required_text(
                    values, "drainage_local_path", "Local drainage path"
                ),
                "controls": controls,
            }
        )
    drainage = {
        "enabled": drainage_enabled,
        "provider": drainage_provider,
        "options": drainage_options,
    }

    outlet_source = _value(values, "outlet_source", "auto_hydrography_lowest")
    if outlet_source == "none":
        outlets = None
    else:
        outlet_coordinates = (
            _json_value(values, "outlet_coordinates", "Outlet coordinates", list)
            if outlet_source == "coordinates"
            else []
        )
        if outlet_source == "coordinates":
            outlet_coordinates = _validate_outlet_coordinates(outlet_coordinates)
        if outlet_source == "coordinates" and not outlet_coordinates:
            raise ValueError("Coordinate outlet mode requires at least one coordinate row.")
        outlet_crs = _optional_text(values, "outlet_crs")
        if outlet_source == "coordinates" and outlet_crs is None:
            raise ValueError("Coordinate outlet mode requires an outlet CRS.")
        if outlet_source == "vector":
            outlet_source = _required_text(
                values, "outlet_vector_path", "Outlet vector path"
            )
        if outlet_source == "auto_hydrography_lowest" and not hydro_enabled:
            raise ValueError("Automatic hydrography outlet selection requires hydrography.")
        outlets = {
            "source": outlet_source,
            "coordinates": outlet_coordinates,
            "crs": outlet_crs,
            "method": _value(values, "outlet_method", "open_corridor"),
            "elevation_mode": _value(values, "outlet_elevation", "terrain_matched"),
            "cut_elevation_m": _value(values, "outlet_cut_elevation", 0.0),
            "default_width_m": _value(values, "outlet_width", 120.0),
        }

    return {
        "case_name": _required_text(values, "case_name", "Case name", "my_rift_case"),
        "output_root": _required_text(values, "output_root", "Output root", "rift_cases"),
        "domain": domain,
        "dem": dem,
        "initial_state": initial_state,
        "hydrography": hydrography,
        "land_cover": land_cover,
        "soil": soil,
        "rainfall": rainfall,
        "inflows": inflows,
        "coastal": coastal,
        "drainage": drainage,
        "outlets": outlets,
        "river_bathymetry": river_bathymetry,
        "cache": {"mode": _value(values, "cache_mode", "fresh")},
        "plots": {
            "raw_products": bool(_value(values, "raw_plots", True)),
            "processed_products": bool(_value(values, "processed_plots", True)),
            "formats": ["png"],
            "dpi": _value(values, "plot_dpi", 160),
        },
        "simulation": {
            "start_time_s": 0,
            "end_time_s": duration_seconds,
            "output_interval_s": min(3600, duration_seconds),
        },
    }


def build_case_from_widget_values(
    values: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    config_directory: str | Path = "generated_configs",
) -> tuple[CaseSpec, Path, bool]:
    """Load an override or save and return the case selected in the front end."""

    environment = os.environ if environment is None else environment
    override = _optional_text(values, "case_config_override") or environment.get(
        "RIFTPREP_CASE_CONFIG"
    )
    if override:
        config_path = Path(override).expanduser().resolve()
        return load_case(config_path), config_path, True

    spec = case_from_mapping(case_payload_from_widget_values(values))
    config_path = Path(config_directory) / f"{spec.case_name}.yaml"
    save_case(spec, config_path)
    return spec, config_path, False
