from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from derivation_app.factory import create_fake_service, create_http_app
from derivation_app.tests.test_report_bundle import _run_request
from derivation_runtime.types import FormulaValidationResult


def test_format_pause_visible_in_api_and_preserved_after_restart(tmp_path):
    arguments = {
        "run_root": tmp_path / "active",
        "storage_root": tmp_path,
        "run_id_factory": lambda: "run-formula-service",
    }
    service = create_fake_service(**arguments)
    service.formula_validation_policy = "formula-v1"
    service._formula_validator = lambda _: (
        lambda content: FormulaValidationResult(
            issues=(
                {
                    "code": "unsupported_command",
                    "severity": "error",
                    "field": "derivation",
                    "formula_index": 1,
                    "message": "Unknown source macro: q",
                },
            )
        )
    )
    with TestClient(create_http_app(service)) as client:
        command = _run_request()
        command["config"]["record_version"] = "1.1"
        assert client.post("/api/runs", json=command).status_code == 201
        for _ in range(100):
            view = client.get("/api/runs/run-formula-service").json()
            if view["phase"] == "paused":
                break
            time.sleep(0.01)
        assert view["phase"] == "paused"
        assert "derivation formula 1" in view["errorMessage"]
        assert not view["steps"]
    manifest = json.loads(
        (tmp_path / "active/run-formula-service/manifest.json").read_text()
    )
    assert manifest["run_config"]["formula_validation_policy"] == "formula-v1"
    recovered = create_fake_service(**arguments)
    # A newly installed default must not erase the frozen policy or retry stop.
    with TestClient(create_http_app(recovered)) as client:
        view = client.get("/api/runs/run-formula-service").json()
        assert view["phase"] == "paused"
        assert "Unknown source macro" in view["errorMessage"]
        assert not view["steps"]
