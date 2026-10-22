"""Public API for the RIFT-UConn input-builder notebook."""

from .config import (
    DomainSpec,
    DrainageSpec,
    GridSpec,
    OutletSpec,
    ProjectPaths,
    RiverBathymetrySpec,
    SimulationSpec,
    resolve_simulation_ready_directory,
)
from .spec import (
    CachePolicy,
    CaseSpec,
    PlotPolicy,
    ProductSpec,
    case_from_mapping,
    load_case,
    save_case,
)
from .artifacts import (
    AcquisitionEvent,
    ProvenanceLedger,
    RawArtifact,
    TimingLedger,
    prepare_case_directory,
    redact_sensitive,
)
from .run_manifest import (
    RUN_MANIFEST_SCHEMA,
    RunManifestWriter,
    validate_run_manifest,
)
from .managers import RawCatalog, RawProduct, acquire_all_raw
from .workflow import (
    CaseResult,
    RIFTInputBuilder,
    RawDownloadResult,
    build_rift_inputs,
    download_raw_inputs,
    run_case,
)
from .providers import (
    PROVIDERS,
    describe_providers,
    download_url,
    download_usgs_3dep,
    get_huc_boundary,
    get_usgs_3dhp_flowlines,
    load_vector,
    query_arcgis_features,
)
from .spatial import (
    align_raster,
    apply_closed_huc_wall,
    build_grid,
    burn_river_bathymetry,
    carve_outlets,
    create_domain_masks,
    derive_river_terminus,
    read_raster,
)
from .drainage import (
    RIFT_DRAINAGE_COLUMNS,
    build_drainage_parameter_table,
    rasterize_drainage_features,
    write_external_drainage_series,
)
from .writers import (
    build_rift_config,
    read_ats_rainfall,
    write_ats_rainfall,
    write_esri_ascii,
    write_hydrograph_bundle,
    write_manifest,
    write_run_instructions,
)
from .validation import ValidationReport, validate_case
from .quality import (
    QCRecorder,
    QualityControlError,
    file_inventory,
    load_user_domain,
    setup_workflow_logging,
    sha256_file,
    stage_vector_dataset,
)
from .input_qc import (
    InputQCResult,
    QC_FAILED,
    QC_NEEDS_USER_INPUT,
    QC_PASSED,
    check_raw_product,
    qc_results_table,
)
from .audit import SOURCE_ENDPOINTS, audit_provider_catalog
from .processing import (
    NLCD_MANNING,
    plot_aorc,
    plot_nlcd,
    plot_soils,
    prepare_aorc_event,
    prepare_nlcd,
    prepare_ssurgo,
    prepare_surge_grids,
    prepare_synthetic_drainage,
)
from .plotting import (
    JOURNAL_STYLE,
    add_map_furniture,
    plot_dem_domain,
    plot_drainage,
    plot_hydrographs,
    plot_input_atlas,
    plot_outlet_diagnostics,
    plot_rainfall,
    plot_river_bathymetry,
    save_figure,
    set_journal_style,
)

__all__ = [name for name in globals() if not name.startswith("_")]
