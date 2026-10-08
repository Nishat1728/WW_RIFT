from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class DomainSpec:
    mode: Literal["huc", "shapefile", "rectangle"] = "huc"
    huc_code: str | None = None
    huc_level: int | None = None
    vector_path: str | None = None
    vector_layer: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    bounds_crs: str = "EPSG:4326"
    target_crs: str = "auto_utm"
    cell_size_m: float = 30.0
    download_buffer_cells: int = 2
    boundary_rule: Literal["cell_center", "all_touched", "interior"] = "cell_center"
    outside_dem_mode: Literal["wall", "real_terrain"] = "wall"
    wall_elevation_m: float = 9999.0

    def __post_init__(self) -> None:
        if self.mode not in ("huc", "shapefile", "rectangle"):
            raise ValueError("mode must be 'huc', 'shapefile', or 'rectangle'.")
        if self.mode == "huc" and not self.huc_code:
            raise ValueError("huc_code is required for mode='huc'.")
        if self.mode == "shapefile" and not self.vector_path:
            raise ValueError("vector_path is required for mode='shapefile'.")
        if self.mode == "rectangle" and self.bounds is None:
            raise ValueError("bounds are required for mode='rectangle'.")
        if self.huc_code and self.huc_level is None:
            object.__setattr__(self, "huc_level", len(str(self.huc_code)))
        if self.huc_level not in (None, 2, 4, 6, 8, 10, 12, 14, 16):
            raise ValueError("HUC level must be an even value from 2 through 16.")
        if self.cell_size_m <= 0:
            raise ValueError("cell_size_m must be positive.")


@dataclass(frozen=True)
class OutletSpec:
    source: str = "coordinates"
    coordinates: tuple[dict[str, Any], ...] = ()
    crs: str | None = None
    method: Literal["wall_notch", "open_corridor"] = "open_corridor"
    elevation_mode: Literal["zero", "constant", "terrain_matched", "sloped"] = "zero"
    cut_elevation_m: float = 0.0
    default_width_m: float = 60.0


@dataclass(frozen=True)
class RiverBathymetrySpec:
    source: str = "usgs_3dhp"
    apply_bathymetry: bool = False
    depth_method: Literal["constant", "attribute", "stream_order"] = "constant"
    depth_m: float = 1.5
    depth_field: str | None = None
    stream_order_field: str | None = None
    depth_by_order: dict[int, float] = field(default_factory=dict)
    width_method: Literal["constant", "attribute"] = "constant"
    width_m: float = 30.0
    width_field: str | None = None
    cross_section: Literal["flat", "v", "gaussian"] = "gaussian"
    preserve_existing_lower_terrain: bool = True
    enforce_downstream_monotonicity: bool = False
    downstream_direction: Literal["north", "south", "east", "west"] = "south"
    minimum_downstream_slope: float = 0.0

    def __post_init__(self) -> None:
        if self.depth_m < 0 or self.width_m <= 0:
            raise ValueError("River depth must be nonnegative and width must be positive.")
        if self.minimum_downstream_slope < 0:
            raise ValueError("minimum_downstream_slope cannot be negative.")


@dataclass(frozen=True)
class DrainageSpec:
    urban_enabled: bool = False
    source: str = "local_vector"
    type_field: str = "type"
    priority: tuple[int, ...] = (5, 2, 1, 3, 4)
    external_enabled: bool = False
    external_source: str | None = None


@dataclass(frozen=True)
class SimulationSpec:
    start_time_s: float = 0.0
    end_time_s: float = 86400.0
    integration: Literal["rk2", "euler"] = "rk2"
    courant_limit: float = 0.25
    gpu_device: int = 0
    output_interval_s: float = 3600.0
    output_directory: str = "results/native"


@dataclass(frozen=True)
class GridSpec:
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    resolution: float
    crs: str
    nx: int
    ny: int

    @property
    def cell_shape(self) -> tuple[int, int]:
        return self.ny, self.nx

    @property
    def node_shape(self) -> tuple[int, int]:
        return self.ny + 1, self.nx + 1

    @property
    def cell_area_m2(self) -> float:
        return self.resolution**2

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    raw: Path
    provider_cache: Path
    processed: Path
    inputs: Path
    figures: Path
    quality_control: Path
    provenance: Path
    timings: Path
    results: Path

    @classmethod
    def create(cls, root: str | Path) -> "ProjectPaths":
        root = Path(root).expanduser().resolve()
        paths = cls(
            root=root,
            raw=root / "raw",
            provider_cache=root / "raw" / "provider_cache",
            processed=root / "processed",
            inputs=root / "rift_inputs",
            figures=root / "figures",
            quality_control=root / "quality_control",
            provenance=root / "quality_control" / "provenance",
            timings=root / "quality_control" / "timings",
            results=root / "results",
        )
        for path in asdict(paths).values():
            Path(path).mkdir(parents=True, exist_ok=True)
        return paths
