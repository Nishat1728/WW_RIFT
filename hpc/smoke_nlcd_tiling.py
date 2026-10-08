"""Small real-provider smoke test for tiled NLCD acquisition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import rasterio
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riftprep.artifacts import ProvenanceLedger
from riftprep.config import ProjectPaths
from riftprep.managers import fetch_land_cover
from riftprep.spec import ProductSpec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()

    paths = ProjectPaths.create(args.output_root)
    boundary = gpd.GeoDataFrame(
        {"name": ["storrs_smoke"]},
        geometry=[box(-72.31, 41.77, -72.21, 41.85)],
        crs="EPSG:4326",
    )
    product = ProductSpec(
        provider="watershed_workflow_nlcd",
        options={
            "year": 2021,
            "location": "L48",
            "variables": ["cover", "impervious", "canopy"],
            "tile_degrees": 0.05,
            "download_workers": 2,
            "max_attempts": 4,
        },
    )
    result = fetch_land_cover(
        product,
        boundary,
        paths,
        ProvenanceLedger(paths.provenance),
        plot=True,
        dpi=100,
        reuse_existing=False,
    )
    with rasterio.open(result.files["cover"]) as source:
        raster = {
            "width": source.width,
            "height": source.height,
            "crs": str(source.crs),
            "x_resolution": source.res[0],
            "y_resolution": source.res[1],
        }
    print(json.dumps({
        "metadata": result.metadata,
        "artifact_count": len(result.files),
        "canonical_cover": raster,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
