from __future__ import annotations

import json
from pathlib import Path

from derivation_api.application import create_app
from derivation_api.fake_service import FakeDerivationService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_checked_openapi_contract_matches_application() -> None:
    checked = json.loads((PROJECT_ROOT / "openapi.json").read_text(encoding="utf-8"))
    assert checked == create_app(FakeDerivationService()).openapi()


def test_every_documented_error_uses_the_runtime_error_envelope() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    expected_error_statuses = {
        ("/healthz", "get"): {400, 403, 413, 500, 503},
        ("/api/capabilities", "get"): {400, 403, 413, 500, 503},
        ("/api/intake/sessions", "get"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/intake/sessions", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/intake/sessions/{session_id}", "get"): {
            400,
            403,
            404,
            409,
            413,
            422,
            500,
            503,
        },
        ("/api/intake/sessions/{session_id}/rounds", "post"): {
            400,
            403,
            404,
            409,
            413,
            422,
            500,
            503,
        },
        ("/api/intake/sessions/{session_id}/finalize", "post"): {
            400,
            403,
            404,
            409,
            413,
            422,
            500,
            503,
        },
        ("/api/intake/sessions/{session_id}/confirm", "post"): {
            400,
            403,
            404,
            409,
            413,
            422,
            500,
            503,
        },
        ("/api/intake/sessions/{session_id}/cancel", "post"): {
            400,
            403,
            404,
            409,
            413,
            422,
            500,
            503,
        },
        ("/api/runs", "get"): {400, 403, 413, 500, 503},
        ("/api/runs", "post"): {400, 403, 409, 413, 422, 500, 503},
        ("/api/runs/{run_id}", "get"): {400, 403, 404, 413, 422, 500, 503},
        ("/api/runs/{run_id}/events", "get"): {400, 403, 404, 413, 422, 500, 503},
        ("/api/runs/{run_id}/pause", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/runs/{run_id}/resume", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/runs/{run_id}/interrupt", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/runs/{run_id}/branches", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
        ("/api/runs/{run_id}/reports", "post"): {400, 403, 404, 409, 413, 422, 500, 503},
    }

    for (path, method), status_codes in expected_error_statuses.items():
        responses = schema["paths"][path][method]["responses"]
        for status_code in status_codes:
            error_schema = responses[str(status_code)]["content"]["application/json"]["schema"]
            assert error_schema == {"$ref": "#/components/schemas/ErrorEnvelope"}

    public_operations = json.dumps(schema["paths"], sort_keys=True)
    assert "HTTPValidationError" not in public_operations
    assert "ValidationError" not in public_operations
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ValidationError" not in schema["components"]["schemas"]


def test_openapi_declares_transport_headers_sse_only_and_command_eligibility() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    paths = schema["paths"]

    command_responses = {
        ("/api/runs", "post", "201"),
        ("/api/runs/{run_id}/pause", "post", "200"),
        ("/api/runs/{run_id}/resume", "post", "200"),
        ("/api/runs/{run_id}/interrupt", "post", "200"),
        ("/api/runs/{run_id}/branches", "post", "201"),
        ("/api/intake/sessions", "post", "201"),
        ("/api/intake/sessions/{session_id}/rounds", "post", "200"),
        ("/api/intake/sessions/{session_id}/finalize", "post", "200"),
        ("/api/intake/sessions/{session_id}/confirm", "post", "200"),
        ("/api/intake/sessions/{session_id}/cancel", "post", "200"),
    }
    for path, method, status in command_responses:
        header = paths[path][method]["responses"][status]["headers"]["Idempotency-Key"]
        assert header["schema"] == {"type": "string"}
        assert "echoed" in header["description"]

    events = paths["/api/runs/{run_id}/events"]["get"]
    assert set(events["responses"]["200"]["content"]) == {"text/event-stream"}
    last_event_id = next(
        parameter
        for parameter in events["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Last-Event-ID"
    )
    assert last_event_id["required"] is False
    assert "takes precedence" in last_event_id["description"]

    pause_description = paths["/api/runs/{run_id}/pause"]["post"]["description"]
    assert all(
        phase in pause_description for phase in ["submitted", "autonomous_exploration", "human_expansion"]
    )
    assert "paused" in paths["/api/runs/{run_id}/resume"]["post"]["description"]
    assert "in-flight call" in paths["/api/runs/{run_id}/interrupt"]["post"]["description"]


def test_openapi_freezes_the_intake_decision_ladder_contract() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    schemas = schema["components"]["schemas"]

    assert set(schemas["IntakeSessionView"]["properties"]["status"]["enum"]) == {
        "active",
        "convergence_required",
        "candidate_ready",
        "confirmed",
        "cancelled",
    }
    question = schemas["IntakeQuestionView"]["properties"]
    assert set(question["decision_class"]["enum"]) == {
        "problem",
        "convention",
        "approximation_level",
    }
    assert question["decision_class"]["default"] == "problem"
    assert "why_it_matters" in schemas["IntakeQuestionView"]["required"]
    assert "grounded_in" in question
    strategy = schemas["IntakeAnswerInput"]["properties"]["strategy"]
    assert set(strategy["anyOf"][0]["enum"]) == {
        "selected",
        "simplest_first",
        "both_routes",
    }

    session = schemas["IntakeSessionView"]["properties"]
    assert {"model", "effort"} <= set(session)
    assert {"model", "effort"} <= set(schemas["IntakeSessionView"]["required"])
    assert session["pending_problem_questions"]["items"] == {
        "$ref": "#/components/schemas/IntakeQuestionView"
    }
    assert session["convergence"]["$ref"] == "#/components/schemas/IntakeConvergenceView"
    assert set(schemas["IntakeConvergenceView"]["properties"]) == {
        "rounds",
        "audit_rejections",
        "reason",
        "finalized_by_user",
    }

    specification = schemas["IntakeProblemSpecificationView"]["properties"]
    assert specification["declared_defaults"]["items"] == {
        "$ref": "#/components/schemas/IntakeDeclaredDefaultView"
    }
    assert specification["refinement_ladder"]["items"] == {
        "$ref": "#/components/schemas/IntakeLadderRungView"
    }
    assert set(schemas["IntakeLadderRungView"]["properties"]) == {
        "rung",
        "name",
        "relaxes",
        "default_ids",
        "decision_ids",
        "parallel_branch",
    }
    assert set(schemas["IntakeDeclaredDefaultView"]["properties"]) == {
        "default_id",
        "decision_class",
        "title",
        "statement",
        "rationale",
        "alternatives",
    }

    finalize = schemas["FinalizeIntakeSessionRequest"]
    assert set(finalize["properties"]) == {"base_revision", "answers"}
    assert finalize["required"] == ["base_revision"]
    frozen = schemas["FrozenProblemInput"]["properties"]
    assert "refinement_ladder" in frozen
    assert "declared_defaults" in frozen


def test_openapi_exposes_only_the_implemented_v1_runtime_profile() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    runtime = schema["components"]["schemas"]["RuntimeConfig"]["properties"]

    assert runtime["auth_mode"]["const"] == "chatgpt"
    assert runtime["concurrency"]["const"] == 1
    assert runtime["max_run_seconds"]["type"] == "null"


def test_openapi_requires_backend_owned_defaults_and_run_affordances() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    schemas = schema["components"]["schemas"]

    capabilities = schemas["CapabilitiesView"]
    assert "create_run_defaults" in capabilities["required"]
    assert capabilities["properties"]["create_run_defaults"] == {
        "$ref": "#/components/schemas/CreateRunDefaultsView"
    }
    defaults = schemas["CreateRunDefaultsView"]
    assert {
        "config",
        "runtime",
        "model_options",
        "model_catalog_source",
        "model_catalog_refreshed_at",
        "allowed_models",
        "allowed_efforts",
    } <= set(defaults["required"])
    option = schemas["ModelOptionView"]
    assert {
        "model",
        "display_name",
        "is_default",
        "default_effort",
        "supported_efforts",
    } == set(option["required"])
    intake = schemas["CreateIntakeSessionRequest"]
    assert set(intake["required"]) == {"initial_message", "model", "effort"}

    run = schemas["RunView"]
    assert {"read_only", "commands", "routes"} <= set(run["required"])
    assert run["properties"]["commands"] == {"$ref": "#/components/schemas/RunCommandCapabilities"}

    commands = schemas["RunCommandCapabilities"]
    assert {
        "can_pause",
        "can_resume",
        "can_interrupt",
        "branchable_step_revision_ids",
    } <= set(commands["required"])


def test_report_export_contract_has_confirmation_but_no_client_filesystem_path() -> None:
    schema = create_app(FakeDerivationService()).openapi()
    request = schema["components"]["schemas"]["ExportReportRequest"]
    response = schema["components"]["schemas"]["ReportBundleView"]

    assert set(request["required"]) == {"selected_route_id", "confirm_selected_route"}
    assert request["properties"]["confirm_selected_route"]["const"] is True
    assert "output_path" not in json.dumps(request, sort_keys=True)
    assert "bundle_path" in response["properties"]


def test_openapi_event_ids_are_bounded_to_javascript_safe_integers() -> None:
    maximum = 9_007_199_254_740_991
    schema = create_app(FakeDerivationService()).openapi()
    events = schema["paths"]["/api/runs/{run_id}/events"]["get"]

    after = next(parameter for parameter in events["parameters"] if parameter["name"] == "after")
    integer_schema = next(item for item in after["schema"]["anyOf"] if item["type"] == "integer")
    assert integer_schema["maximum"] == maximum

    last_event_id = next(
        parameter for parameter in events["parameters"] if parameter["name"] == "Last-Event-ID"
    )
    assert last_event_id["schema"]["maxLength"] == len(str(maximum))
    assert (
        schema["components"]["schemas"]["RunView"]["properties"]["canonical_event_id"]["maximum"] == maximum
    )
    assert schema["components"]["schemas"]["RunEvent"]["properties"]["event_id"]["maximum"] == maximum
