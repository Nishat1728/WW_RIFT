import hashlib
import json
from pathlib import Path

import pytest

from riftprep.artifacts import ProvenanceLedger, prepare_case_directory
from riftprep.config import DomainSpec, ProjectPaths
from riftprep.run_manifest import (
    RUN_MANIFEST_SCHEMA,
    RunManifestWriter,
    validate_run_manifest,
)
from riftprep.spec import CachePolicy, CaseSpec, PlotPolicy, ProductSpec
from riftprep.workflow import RIFTInputBuilder


def _spec(tmp_path: Path, *, case_name="manifest_case", cache_mode="reuse") -> CaseSpec:
    return CaseSpec(
        case_name=case_name,
        output_root=str(tmp_path),
        domain=DomainSpec(
            mode="rectangle", bounds=(-72.8, 41.7, -72.7, 41.8)
        ),
        dem=ProductSpec(
            provider="local",
            options={
                "path": str(tmp_path / "source_dem.tif"),
                "api_key": "manifest-secret",
            },
        ),
        hydrography=ProductSpec(enabled=False, provider="none"),
        land_cover=ProductSpec(enabled=False, provider="none"),
        soil=ProductSpec(enabled=False, provider="none"),
        rainfall=ProductSpec(enabled=False, provider="none"),
        initial_state=ProductSpec(enabled=False, provider="none"),
        cache=CachePolicy(mode=cache_mode),
        plots=PlotPolicy(raw_products=False, processed_products=False),
    )


def test_run_manifest_schema_success_inventory_and_secret_redaction(tmp_path: Path):
    spec = _spec(tmp_path)
    paths = prepare_case_directory(spec.case_root, "reuse")
    (paths.quality_control / "resolved_case.yaml").write_text(
        "case_name: manifest_case\n", encoding="utf-8"
    )
    ledger = ProvenanceLedger(paths.provenance)
    raw = paths.raw / "dem" / "provider.bin"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"provider-response")
    ledger.register("dem", "mock", raw, {"token": "manifest-secret"})
    ledger.finalize_acquisition(
        product="dem",
        provider="mock",
        files={"provider_response": raw},
        raw_root=paths.raw,
        cache_mode="reuse",
        status="succeeded",
        started_utc="2026-10-11T00:00:00+00:00",
        completed_utc="2026-10-11T00:00:01+00:00",
        query_parameters={"bounds": [-72.8, 41.7, -72.7, 41.8]},
        provider_metadata={"product": "fixture"},
    )
    writer = RunManifestWriter(spec, paths, preexisting_case=False)
    writer.raw_complete()
    processed = paths.processed / "dem" / "aligned.asc"
    processed.parent.mkdir(parents=True)
    processed.write_bytes(b"processed-dem")
    final = paths.simulation_ready / "dem.asc"
    final.write_bytes(b"final-dem")
    (paths.quality_control / "qc_report.json").write_text(
        json.dumps({"ok": True, "errors": [], "warnings": ["fixture warning"]}),
        encoding="utf-8",
    )
    writer.complete()

    text = writer.latest_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    validate_run_manifest(payload)
    assert payload["schema"] == RUN_MANIFEST_SCHEMA
    assert payload["run"]["status"] == "complete"
    assert payload["run"]["id"] == writer.run_id
    assert payload["failure"] is None
    assert "manifest-secret" not in text
    assert "[REDACTED]" in text
    assert payload["warnings"] == ["fixture warning"]
    assert payload["quality_control"]["final_qc"]["ok"] is True
    assert payload["environment"]["python"]["version"]
    assert "riftprep" in payload["environment"]["package_versions"]
    assert payload["artifacts"]["processed"][0]["sha256"] == hashlib.sha256(
        b"processed-dem"
    ).hexdigest()
    assert payload["artifacts"]["simulation_ready"][0]["sha256"] == hashlib.sha256(
        b"final-dem"
    ).hexdigest()
    assert writer.history_path.read_text(encoding="utf-8") == text


def test_controlled_download_failure_writes_redacted_terminal_manifest(tmp_path: Path):
    spec = _spec(tmp_path, case_name="failed_manifest", cache_mode="fresh")
    builder = RIFTInputBuilder(spec)

    with pytest.raises(FileNotFoundError):
        builder.download("dem")

    text = builder.run_manifest.latest_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    validate_run_manifest(payload)
    assert payload["run"]["status"] == "failed"
    assert payload["run"]["phase"] == "download_raw_dem"
    assert payload["failure"]["type"] == "FileNotFoundError"
    assert "manifest-secret" not in text
    assert payload["acquisition"]["events_for_run"][-1]["status"] == "failed"


def test_failure_message_redacts_known_configuration_secret(tmp_path: Path):
    spec = _spec(tmp_path, case_name="secret_failure")
    paths = prepare_case_directory(spec.case_root, "reuse")
    writer = RunManifestWriter(spec, paths, preexisting_case=False)

    writer.fail(RuntimeError("provider rejected credential manifest-secret"))

    text = writer.latest_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "manifest-secret" not in text
    assert payload["failure"]["message"].endswith("[REDACTED]")


def test_reuse_run_links_previous_attempt_and_preserves_history(tmp_path: Path):
    spec = _spec(tmp_path)
    paths = prepare_case_directory(spec.case_root, "reuse")
    first = RunManifestWriter(spec, paths, preexisting_case=False)
    first.complete()
    second = RunManifestWriter(spec, paths, preexisting_case=True)
    second.complete()

    latest = json.loads(second.latest_path.read_text(encoding="utf-8"))
    assert first.run_id != second.run_id
    assert latest["run"]["resumed"] is True
    assert latest["run"]["resumed_from_run_id"] == first.run_id
    assert first.history_path.is_file()
    assert second.history_path.is_file()
    assert len(list(second.history_directory.glob("*.json"))) == 2


def test_legacy_reuse_does_not_invent_previous_run_identity(tmp_path: Path):
    spec = _spec(tmp_path, case_name="legacy_case")
    legacy_root = spec.case_root
    (legacy_root / "rift_inputs").mkdir(parents=True)
    paths = ProjectPaths.create(legacy_root)

    writer = RunManifestWriter(spec, paths, preexisting_case=True)
    writer.complete()
    payload = json.loads(writer.latest_path.read_text(encoding="utf-8"))

    assert payload["run"]["resumed"] is True
    assert payload["run"]["resumed_from_run_id"] is None
    assert "run.resumed_from_run_id" in payload["missing_metadata"]
    assert any(
        item["type"] == "legacy_simulation_ready_directory"
        for item in payload["fallbacks"]
    )


def test_documented_json_schema_matches_runtime_schema(tmp_path: Path):
    schema_path = Path(__file__).parents[1] / "docs" / "run_provenance_manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == RUN_MANIFEST_SCHEMA
    assert set(schema["required"]) == {
        "schema", "run", "configuration", "environment", "directory_contract",
        "acquisition", "artifacts", "stages", "fallbacks", "warnings",
        "quality_control", "failure", "missing_metadata",
    }
