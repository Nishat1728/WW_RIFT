import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_reviewed_source_catalog_is_complete_and_honest():
    catalog_path = ROOT / "docs" / "rift_input_source_catalog.csv"
    with catalog_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) >= 80
    identities = [(row["input_category"], row["provider_key"]) for row in rows]
    assert len(identities) == len(set(identities))

    categories = {row["input_category"] for row in rows}
    assert {
        "Study-area boundary",
        "Terrain DEM and coastal topobathymetry",
        "Hydrography, channels, and outlets",
        "Land cover, imperviousness, canopy, and Manning roughness",
        "Soil and infiltration properties",
        "Rainfall and meteorological forcing",
        "River inflow hydrographs",
        "Coastal water level and surge",
        "River/channel bathymetry and hydraulic geometry",
        "Urban drainage and hydraulic controls",
        "Initial water depth and restart state",
    }.issubset(categories)

    lookup = {(row["input_category"], row["provider_key"]): row for row in rows}
    assert lookup[("Rainfall and meteorological forcing", "aorc")]["status"].startswith(
        "working"
    )
    for category, provider in (
        ("Terrain DEM and coastal topobathymetry", "copernicus_glo30"),
        ("Soil and infiltration properties", "soilgrids"),
        ("Rainfall and meteorological forcing", "daymet"),
        ("Rainfall and meteorological forcing", "mrms"),
        ("Rainfall and meteorological forcing", "era5_land"),
    ):
        assert lookup[(category, provider)]["status"].startswith("working")
    assert lookup[("Terrain DEM and coastal topobathymetry", "usgs_3dep_30m")][
        "status"
    ].startswith("working")
