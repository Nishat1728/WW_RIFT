"""Opt-in NETWORK check: fresh 20-year USGS history, quantile, raw preservation.

Run on a compute node with ``python -m hpc.validate_spinup_history <new-directory>``.
This prepares forcing; it does not run four months of the GPU solver.
"""
import json
import sys
from types import SimpleNamespace
from pathlib import Path

from pyproj import Transformer

from riftprep.artifacts import ProvenanceLedger, sha256
from riftprep.config import GridSpec, ProjectPaths
from riftprep.input_qc import check_raw_product
from riftprep.managers import RawCatalog, fetch_usgs_waterdata
from riftprep.spec import ProductSpec
from riftprep.spinup import SpinupSpec, prepare_baseflow


def main(directory):
    directory = Path(directory)
    if directory.exists():
        raise FileExistsError("Choose a NEW directory for this fresh-only check.")
    paths = ProjectPaths.create(directory)
    spinup = SpinupSpec(enabled=True, start_utc="2023-02-10T00:00:00Z",
                        end_utc="2023-06-10T00:00:00Z", stations=[{"site": "01184000", "id": "test_gauge"}])
    spinup.validate()
    selection = ProductSpec(provider="usgs_waterdata", options=spinup.download_options())
    raw = fetch_usgs_waterdata("spinup_baseflow", selection, paths,
                              ProvenanceLedger(paths.provenance), plot=False, reuse_existing=False)
    check_raw_product("spinup_baseflow", raw, selection.options, paths.quality_control / "native.json")
    hashes = {str(path): sha256(path) for path in raw.files.values()}
    location = json.loads(raw.files["location_01184000"].read_text())
    lon, lat = location["geometry"]["coordinates"][:2]
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform(lon, lat)
    grid = GridSpec(x-150, y-150, x+150, y+150, 30, "EPSG:5070", 10, 10)
    prepare_baseflow(SimpleNamespace(spinup=spinup), RawCatalog(None, {"spinup_baseflow": raw}), grid, paths)
    assert hashes == {str(path): sha256(path) for path in raw.files.values()}
    print((paths.quality_control / "spinup_baseflow.json").read_text())
    print("LIVE_20_YEAR_BASEFLOW_PASSED")


if __name__ == "__main__":
    main(sys.argv[1])
