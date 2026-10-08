import json

import h5py
import numpy as np
import pandas as pd
import xarray as xr

from riftprep.config import GridSpec
from riftprep.processing import prepare_aorc_event


def test_uncovered_rainfall_is_zero_only_in_rift_field(
    tmp_path, monkeypatch, capsys
):
    source_path = tmp_path / "raw.nc"
    original = np.full((1, 2, 2), 2.0, dtype=np.float32)
    xr.Dataset(
        {"APCP_surface": (("time", "y", "x"), original)},
        coords={
            "time": pd.to_datetime(["2023-06-10T01:00:00"]),
            "x": [15.0, 45.0],
            "y": [45.0, 15.0],
        },
    ).to_netcdf(source_path)
    grid = GridSpec(
        xmin=0.0, ymin=0.0, xmax=90.0, ymax=60.0,
        resolution=30.0, crs="EPSG:5070", nx=3, ny=2,
    )
    report_path = tmp_path / "qc" / "rainfall_coverage.json"
    hdf5_path = tmp_path / "rainfall.h5"
    staging_root = tmp_path / "node-local"
    staging_root.mkdir()
    monkeypatch.setenv("RIFTPREP_HDF5_STAGING_DIR", str(staging_root))
    prepare_aorc_event(
        source_path,
        tmp_path / "event.nc",
        hdf5_path,
        grid,
        np.ones(grid.cell_shape, dtype=bool),
        "2023-06-10T00:00:00Z",
        "2023-06-10T01:00:00Z",
        coverage_report_path=report_path,
        uncovered_policy="zero",
    )
    progress = capsys.readouterr().out
    assert "closed all source NetCDF handles" in progress
    assert "reopening and validating the complete staged HDF5" in progress
    assert f"published {hdf5_path}" in progress
    assert list(staging_root.iterdir()) == []
    assert list(hdf5_path.parent.glob(f".{hdf5_path.name}.*.tmp")) == []
    with xr.open_dataset(source_path) as unchanged:
        np.testing.assert_array_equal(unchanged.APCP_surface.values, original)
    with h5py.File(hdf5_path) as forcing:
        rate = forcing["precipitation rain [m s^-1]"]["0"][:]
    assert np.all(rate[:, :2] > 0)
    assert np.all(rate[:, 2] == 0)
    report = json.loads(report_path.read_text())
    assert report["uncovered_policy"] == "zero"
    assert report["raw_event_modified"] is False
    assert report["missing_active_cells"] == 2
    assert report["status"] == "PASSED"
    assert report["automatic_zero_replacement_performed"] is True
    assert report_path.with_name("rainfall_coverage_map.png").is_file()
    assert report_path.with_name("rainfall_zero_replacement_mask.tif").is_file()
