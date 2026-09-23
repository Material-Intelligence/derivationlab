"""formula-v2 through the service: no compiler validator, frozen policy."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from derivation_app.factory import create_fake_service, create_http_app
from derivation_app.tests.test_report_bundle import _run_request


def test_formula_v2_runs_without_compiler_and_freezes_policy(tmp_path):
    arguments = {
        "run_root": tmp_path / "active",
        "storage_root": tmp_path,
        "run_id_factory": lambda: "run-formula-v2",
    }
    service = create_fake_service(**arguments)
    service.formula_validation_policy = "formula-v2"
    with TestClient(create_http_app(service)) as client:
        command = _run_request()
        command["config"]["record_version"] = "1.1"
        assert client.post("/api/runs", json=command).status_code == 201
        for _ in range(300):
            view = client.get("/api/runs/run-formula-v2").json()
            if view["phase"] not in {"submitted", "autonomous_exploration", "running"}:
                break
            time.sleep(0.01)
        assert view["phase"] != "paused"
        context = service._runs["run-formula-v2"]
        assert service._formula_validator(context) is None
        audits = context.control.formula_audits("run-formula-v2")
        assert audits and {a["policy"] for a in audits.values()} == {"formula-v2"}
        assert {a["disposition"] for a in audits.values()} <= {
            "accepted",
            "rejected",
            "accepted_with_format_issues",
        }
    manifest = json.loads(
        (tmp_path / "active/run-formula-v2/manifest.json").read_text()
    )
    assert manifest["run_config"]["formula_validation_policy"] == "formula-v2"
