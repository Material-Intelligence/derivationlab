from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from derivation_api.application import ApiSettings, create_app
from derivation_api.fake_service import FIXTURE_ROUND_FAILURE, FakeDerivationService
from derivation_api.middleware import RequestBodyLimitMiddleware
from derivation_api.models import CreateBranchRequest, CreateRunRequest

from .conftest import run_request


def intake_session_payload(message: str, **overrides: str) -> dict[str, str]:
    payload = {"initial_message": message, "model": "gpt-5.4", "effort": "low"}
    payload.update(overrides)
    return payload


def test_frozen_config_maps_to_exact_record_v1_shapes() -> None:
    request = CreateRunRequest.model_validate(run_request())
    assert request.config.record_configuration() == {
        "granularity": "one_claim",
        "max_active_branches": 2,
        "max_model_calls": 12,
        "models": {
            "writer": {"provider": "openai", "model": "test-writer", "effort": "medium"},
            "checker": {"provider": "openai", "model": "test-checker", "effort": "low"},
            "judge": {"provider": "openai", "model": "test-judge", "effort": "high"},
        },
        "backend": {"name": "codex-app-server", "version": "0.147.0"},
    }
    assert request.config.record_input_policy() == {
        "reference_allowed": False,
        "allowed_paths": [],
    }
    assert "auth_mode" not in request.config.record_configuration()
    assert request.runtime.auth_mode == "chatgpt"


def test_desktop_identity_account_and_quit_contracts_are_typed(client: TestClient) -> None:
    build = client.get("/api/build-info")
    site_mode = client.get("/api/site/mode")
    readiness = client.get("/api/desktop/quit-readiness")
    account = client.get("/api/account")

    assert build.status_code == site_mode.status_code == readiness.status_code == account.status_code == 200
    assert site_mode.json() == {"mode": "desktop", "channel": "development"}
    assert build.json() == {
        "schema_version": "derivationlab-build-info-v1",
        "version": "dev",
        "build_number": "0",
        "release_id": "development",
        "commit": "0" * 40,
        "openapi_sha256": "0" * 64,
        "product_mode": "development",
    }
    assert readiness.json() == {"safe_to_quit": True, "active_run_count": 0}
    assert account.json() == {
        "status": "signed_in",
        "credential_store": "file",
        "import_available": False,
        "diagnostic": "ready",
    }


def test_account_rate_limits_are_private_no_store_and_duration_classified(
    client: TestClient,
) -> None:
    response = client.get("/api/account/rate-limits")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["status"] == "available"
    assert payload["plan_type"] == "plus"
    assert [(item["kind"], item["remaining_percent"]) for item in payload["windows"]] == [
        ("five_hour", 82),
        ("weekly", 69),
    ]
    assert "email" not in payload
    assert "account_id" not in payload


def test_account_import_requires_confirmation_and_never_accepts_a_path(client: TestClient) -> None:
    missing_idempotency = client.post(
        "/api/account/import-existing",
        json={"confirm_import": True},
    )
    missing_confirmation = client.post(
        "/api/account/import-existing",
        json={},
        headers={"Idempotency-Key": "import-missing-confirmation"},
    )
    injected_path = client.post(
        "/api/account/import-existing",
        json={"confirm_import": True, "path": "/tmp/auth.json"},
        headers={"Idempotency-Key": "import-path"},
    )
    already_signed_in = client.post(
        "/api/account/import-existing",
        json={"confirm_import": True},
        headers={"Idempotency-Key": "import-signed-in"},
    )

    assert missing_idempotency.status_code == 422
    assert missing_confirmation.status_code == injected_path.status_code == 422
    assert already_signed_in.status_code == 409
    assert already_signed_in.json()["error"]["code"] == "account_already_signed_in"


def test_device_login_start_poll_cancel_uses_public_login_id(client: TestClient) -> None:
    started = client.post("/api/account/device-login/start")
    assert started.status_code == 200
    payload = started.json()
    assert payload["login_id"].startswith("login-")
    assert payload["verification_url"].startswith("https://auth.openai.com/")
    assert payload["user_code"] == "FAKE-CODE"

    pending = client.get(f"/api/account/device-login/{payload['login_id']}")
    canceled = client.post(f"/api/account/device-login/{payload['login_id']}/cancel")
    after_cancel = client.get(f"/api/account/device-login/{payload['login_id']}")

    assert pending.json() == {"status": "pending", "diagnostic": None}
    assert canceled.json() == {"status": "canceled"}
    assert after_cancel.json() == {"status": "canceled", "diagnostic": None}


def test_submit_autonomously_completes_then_get_returns_same_canonical_snapshot(client: TestClient) -> None:
    response = client.post("/api/runs", json=run_request(), headers={"Idempotency-Key": "submit-1"})
    assert response.status_code == 201
    assert response.headers["Idempotency-Key"] == "submit-1"
    payload = response.json()
    assert payload["id"] == "run-0001"
    assert payload["phase"] == "review_ready"
    assert payload["status"] == "review_ready"
    assert payload["rootStepId"] == "step-run-0001-001"
    assert payload["canonical_event_id"] == 4
    assert payload["steps"][0]["revisionId"] == "revision-run-0001-001"
    assert payload["steps"][0]["status"] == "sealed"
    assert payload["steps"][0]["content"]["scope"].startswith("Transport")
    assert len(payload["steps"][0]["output_sha256"]) == 64
    assert payload["steps"][0]["provenance"]["threadId"] == "fake-thread-branch-run-0001-001"
    assert payload["routes"][0]["branch_id"] == "branch-run-0001-001"
    assert payload["routes"][0]["status_history"][0]["seq"] == 1
    assert payload["config"]["max_active_branches"] == 2
    assert payload["runtime"]["retries"] == 0
    assert payload["read_only"] is False
    assert payload["commands"] == {
        "can_pause": False,
        "can_resume": False,
        "can_interrupt": False,
        "branchable_step_revision_ids": ["revision-run-0001-001"],
    }

    fetched = client.get("/api/runs/run-0001")
    assert fetched.status_code == 200
    assert fetched.json() == payload


def test_run_catalog_lists_active_runs_newest_first(client: TestClient) -> None:
    first = client.post("/api/runs", json=run_request(question="First question"))
    second = client.post("/api/runs", json=run_request(question="Second question"))
    assert first.status_code == second.status_code == 201

    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "run-0002",
            "question": "Second question",
            "status": "review_ready",
            "phase": "review_ready",
            "step_count": 1,
            "route_count": 1,
            "read_only": False,
            "created_at": "2026-08-29T16:00:00Z",
            "updated_at": "2026-08-29T16:00:04Z",
        },
        {
            "id": "run-0001",
            "question": "First question",
            "status": "review_ready",
            "phase": "review_ready",
            "step_count": 1,
            "route_count": 1,
            "read_only": False,
            "created_at": "2026-08-29T16:00:00Z",
            "updated_at": "2026-08-29T16:00:04Z",
        },
    ]


def test_report_export_requires_confirmed_route_and_rejects_client_paths(client: TestClient) -> None:
    created = client.post("/api/runs", json=run_request())
    assert created.status_code == 201
    run_id = created.json()["id"]
    route_id = created.json()["routes"][0]["id"]

    unconfirmed = client.post(
        f"/api/runs/{run_id}/reports",
        json={"selected_route_id": route_id, "confirm_selected_route": False},
    )
    arbitrary_path = client.post(
        f"/api/runs/{run_id}/reports",
        json={
            "selected_route_id": route_id,
            "confirm_selected_route": True,
            "output_path": "/tmp/escape",
        },
    )
    unavailable = client.post(
        f"/api/runs/{run_id}/reports",
        json={"selected_route_id": route_id, "confirm_selected_route": True},
    )

    assert unconfirmed.status_code == arbitrary_path.status_code == 422
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "report_export_unavailable"


def test_create_run_idempotency_replays_response_and_rejects_changed_command(client: TestClient) -> None:
    first = client.post("/api/runs", json=run_request(), headers={"Idempotency-Key": "same-key"})
    replay = client.post("/api/runs", json=run_request(), headers={"Idempotency-Key": "same-key"})
    conflict = client.post(
        "/api/runs",
        json=run_request(question="A different question"),
        headers={"Idempotency-Key": "same-key"},
    )
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"


def test_invalid_idempotency_key_is_structured_validation_error(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json=run_request(),
        headers={"Idempotency-Key": "contains spaces"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_missing_explicit_config_and_misplaced_auth_mode_fail_closed(client: TestClient) -> None:
    missing = client.post("/api/runs", json={"question": "No hidden defaults."})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_failed"

    misplaced = run_request()
    misplaced["config"]["auth_mode"] = "chatgpt"  # type: ignore[index]
    response = client.post("/api/runs", json=misplaced)
    assert response.status_code == 422
    detail_locations = [item["location"] for item in response.json()["error"]["details"]]
    assert ["body", "config", "auth_mode"] in detail_locations


def test_reference_path_requires_explicit_permission(client: TestClient) -> None:
    request = run_request()
    request["config"]["allowed_paths"] = ["reference/oldrepo/input.txt"]  # type: ignore[index]
    response = client.post("/api/runs", json=request)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_v1_retry_policy_is_explicitly_zero_or_one(client: TestClient) -> None:
    request = run_request()
    request["runtime"]["retries"] = 2  # type: ignore[index]
    response = client.post("/api/runs", json=request)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_v1_runtime_profile_rejects_unimplemented_options(client: TestClient) -> None:
    unsupported_values = [
        ("auth_mode", "api_key"),
        ("concurrency", 2),
        ("max_run_seconds", 60),
    ]
    for field, value in unsupported_values:
        request = run_request()
        request["runtime"][field] = value  # type: ignore[index]
        response = client.post("/api/runs", json=request)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"
        assert ["body", "runtime", field] in [
            detail["location"] for detail in response.json()["error"]["details"]
        ]


def test_problem_tools_must_match_the_runtime_capability(client: TestClient) -> None:
    request = run_request()
    request["problem"]["allowed_tools"] = ["sympy_calculate"]  # type: ignore[index]
    response = client.post("/api/runs", json=request)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_problem_tools_cannot_repeat_the_same_authority(client: TestClient) -> None:
    request = run_request()
    request["problem"]["allowed_tools"] = [  # type: ignore[index]
        "scientific_compute",
        "scientific_compute",
    ]
    response = client.post("/api/runs", json=request)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_intake_session_lifecycle_is_persistent_and_does_not_create_a_run(
    client: TestClient,
) -> None:
    before = client.get("/api/runs")
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive the weak-coupling response in 2D."),
        headers={"Idempotency-Key": "intake-create-1"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["model"] == "gpt-5.4"
    assert created.json()["effort"] == "low"
    session_id = created.json()["session_id"]
    resumed = client.get(f"/api/intake/sessions/{session_id}")
    active = client.get("/api/intake/sessions", params={"status": "active"})
    questions = created.json()["frontier"]
    round_response = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created.json()["revision"],
            "answers": {
                questions[0]["decision_id"]: {
                    "selected_option_ids": [questions[0]["recommended_option_ids"][0]],
                    "custom_text": None,
                },
                questions[1]["decision_id"]: {
                    "selected_option_ids": [questions[1]["recommended_option_ids"][0]],
                    "custom_text": "Also give a directly computable expression.",
                },
            },
        },
        headers={"Idempotency-Key": "intake-round-1"},
    )
    confirmed = client.post(
        f"/api/intake/sessions/{session_id}/confirm",
        json={"base_revision": round_response.json()["revision"]},
        headers={"Idempotency-Key": "intake-confirm-1"},
    )
    after = client.get("/api/runs")

    assert created.status_code == 201
    assert created.headers["Idempotency-Key"] == "intake-create-1"
    assert resumed.status_code == active.status_code == 200
    assert resumed.json() == created.json()
    assert [item["session_id"] for item in active.json()] == [session_id]
    assert len(questions) == 2
    assert round_response.status_code == 200
    assert round_response.json()["status"] == "candidate_ready"
    assert round_response.json()["frontier"] == []
    assert round_response.json()["frozen_problem"] is None
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["thread_generations"][-1]["status"] == "closed"
    assert confirmed.json()["frozen_problem"]["problem_id"] == session_id
    assert "Also give a directly computable expression." in "\n".join(
        confirmed.json()["frozen_problem"]["accepted_decisions"]
    )
    assert (
        client.get(f"/api/intake/sessions/{session_id}").json()["frozen_problem"]
        == confirmed.json()["frozen_problem"]
    )
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json() == []


def test_intake_convergence_required_finalizes_into_candidate_ready(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive the absorption coefficient."),
        headers={"Idempotency-Key": "intake-create-ladder"},
    ).json()
    session_id = created["session_id"]
    questions = created["frontier"]
    assert all(item["decision_class"] == "problem" for item in questions)
    assert all(item["why_it_matters"] for item in questions)

    stalled = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created["revision"],
            "answers": {
                questions[0]["decision_id"]: {
                    "selected_option_ids": [questions[0]["recommended_option_ids"][0]],
                },
                questions[1]["decision_id"]: {"strategy": "simplest_first"},
            },
        },
        headers={"Idempotency-Key": "intake-round-ladder"},
    )

    assert stalled.status_code == 200
    body = stalled.json()
    assert body["status"] == "convergence_required"
    assert body["frontier"] == []
    assert [item["decision_id"] for item in body["pending_problem_questions"]] == ["regime"]
    assert body["convergence"] == {
        "rounds": 1,
        "audit_rejections": 2,
        "reason": "max_audit_rejections",
        "finalized_by_user": False,
    }
    specification = body["problem_specifications"][-1]
    assert [item["default_id"] for item in specification["declared_defaults"]] == [
        "unit-system",
        "line-shape",
    ]
    assert [item["rung"] for item in specification["refinement_ladder"]] == [0, 1]

    stale = client.post(
        f"/api/intake/sessions/{session_id}/finalize",
        json={"base_revision": body["revision"], "answers": {}},
        headers={"Idempotency-Key": "intake-finalize-incomplete"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "intake_answers_incomplete"

    finalized = client.post(
        f"/api/intake/sessions/{session_id}/finalize",
        json={
            "base_revision": body["revision"],
            "answers": {"regime": {"selected_option_ids": ["direct-only"]}},
        },
        headers={"Idempotency-Key": "intake-finalize-ladder"},
    )
    replay = client.post(
        f"/api/intake/sessions/{session_id}/finalize",
        json={
            "base_revision": body["revision"],
            "answers": {"regime": {"selected_option_ids": ["direct-only"]}},
        },
        headers={"Idempotency-Key": "intake-finalize-ladder"},
    )

    assert finalized.status_code == 200
    assert finalized.headers["Idempotency-Key"] == "intake-finalize-ladder"
    assert finalized.json() == replay.json()
    assert finalized.json()["status"] == "candidate_ready"
    assert finalized.json()["pending_problem_questions"] == []
    assert finalized.json()["convergence"]["finalized_by_user"] is True

    confirmed = client.post(
        f"/api/intake/sessions/{session_id}/confirm",
        json={"base_revision": finalized.json()["revision"]},
        headers={"Idempotency-Key": "intake-confirm-ladder"},
    )

    assert confirmed.status_code == 200
    frozen = confirmed.json()["frozen_problem"]
    assert [item["rung"] for item in frozen["refinement_ladder"]] == [0, 1]
    assert [item["default_id"] for item in frozen["declared_defaults"]] == [
        "unit-system",
        "line-shape",
    ]


def test_a_failed_intake_round_keeps_the_answers_on_the_session(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response."),
        headers={"Idempotency-Key": "intake-create-held"},
    ).json()
    session_id = created["session_id"]
    questions = created["frontier"]
    assert created["pending_submission"] is None

    failed = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created["revision"],
            "answers": {
                questions[0]["decision_id"]: {
                    "selected_option_ids": [questions[0]["recommended_option_ids"][0]],
                },
                questions[1]["decision_id"]: {"custom_text": FIXTURE_ROUND_FAILURE},
            },
        },
        headers={"Idempotency-Key": "intake-round-held"},
    )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "intake_command_failed"

    reloaded = client.get(f"/api/intake/sessions/{session_id}").json()
    assert reloaded["revision"] == created["revision"]
    held = reloaded["pending_submission"]
    assert held["kind"] == "round"
    assert held["base_revision"] == created["revision"]
    assert held["failure_reason"] == "ValueError: the fixture round was asked to fail."
    assert held["answers"][questions[1]["decision_id"]]["custom_text"] == FIXTURE_ROUND_FAILURE
    assert held["answers"][questions[0]["decision_id"]]["selected_option_ids"] == [
        questions[0]["recommended_option_ids"][0]
    ]

    retried = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created["revision"],
            "answers": {
                questions[0]["decision_id"]: {
                    "selected_option_ids": [questions[0]["recommended_option_ids"][0]],
                },
                questions[1]["decision_id"]: {
                    "selected_option_ids": [questions[1]["recommended_option_ids"][0]],
                },
            },
        },
        headers={"Idempotency-Key": "intake-round-held-retry"},
    )

    assert retried.status_code == 200
    assert retried.json()["pending_submission"] is None
    assert client.get(f"/api/intake/sessions/{session_id}").json()["pending_submission"] is None


def test_intake_finalize_rejects_a_session_that_is_not_convergence_required(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response."),
        headers={"Idempotency-Key": "intake-create-not-stalled"},
    ).json()

    refused = client.post(
        f"/api/intake/sessions/{created['session_id']}/finalize",
        json={"base_revision": created["revision"], "answers": {}},
        headers={"Idempotency-Key": "intake-finalize-not-stalled"},
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "intake_not_convergence_required"


def test_intake_round_rejects_stale_revision_and_missing_answers(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response."),
        headers={"Idempotency-Key": "intake-create-2"},
    ).json()
    session_id = created["session_id"]

    stale = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created["revision"] - 1,
            "answers": {"convention": {"selected_option_ids": ["standard-minus"]}},
        },
        headers={"Idempotency-Key": "intake-round-stale"},
    )
    incomplete = client.post(
        f"/api/intake/sessions/{session_id}/rounds",
        json={
            "base_revision": created["revision"],
            "answers": {"convention": {"selected_option_ids": ["standard-minus"]}},
        },
        headers={"Idempotency-Key": "intake-round-incomplete"},
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "intake_stale_revision"
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["code"] == "intake_answers_incomplete"


def test_intake_create_requires_idempotency_and_old_turn_route_is_removed(
    client: TestClient,
) -> None:
    missing_key = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response."),
    )
    old_route = client.post("/api/intake/turn", json={})

    assert missing_key.status_code == 422
    assert old_route.status_code == 404


def test_intake_create_requires_catalog_model_and_effort(client: TestClient) -> None:
    missing = client.post(
        "/api/intake/sessions",
        json={"initial_message": "Derive a response."},
        headers={"Idempotency-Key": "intake-missing-model"},
    )
    invalid = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response.", model="not-a-model", effort="ultra"),
        headers={"Idempotency-Key": "intake-invalid-model"},
    )
    created = client.post(
        "/api/intake/sessions",
        json=intake_session_payload("Derive a response.", model="gpt-5-codex", effort="high"),
        headers={"Idempotency-Key": "intake-valid-model"},
    )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_failed"
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "intake_model_invalid"
    assert created.status_code == 201
    assert created.json()["status"] == "active"


def test_pause_resume_and_interrupt_have_distinct_eligibility_and_idempotency() -> None:
    service = FakeDerivationService(auto_complete_on_submit=False)
    with TestClient(create_app(service)) as client:
        first = client.post("/api/runs", json=run_request()).json()
        assert first["commands"] == {
            "can_pause": True,
            "can_resume": False,
            "can_interrupt": True,
            "branchable_step_revision_ids": [],
        }
        paused = client.post(f"/api/runs/{first['id']}/pause", headers={"Idempotency-Key": "pause-1"})
        pause_replay = client.post(
            f"/api/runs/{first['id']}/pause",
            headers={"Idempotency-Key": "pause-1"},
        )
        assert paused.status_code == 200
        assert pause_replay.status_code == 200
        assert pause_replay.json() == paused.json()
        assert paused.json()["phase"] == "paused"
        assert paused.json()["pause_requested"] is True
        assert paused.json()["hard_interrupt_requested"] is False
        assert paused.json()["branches"][0]["status"] == "paused"
        assert paused.json()["commands"] == {
            "can_pause": False,
            "can_resume": True,
            "can_interrupt": False,
            "branchable_step_revision_ids": [],
        }

        duplicate_pause = client.post(f"/api/runs/{first['id']}/pause")
        assert duplicate_pause.status_code == 409
        assert duplicate_pause.json()["error"]["code"] == "run_not_pausable"
        assert duplicate_pause.json()["error"]["details"] == {"phase": "paused"}

        paused_interrupt = client.post(f"/api/runs/{first['id']}/interrupt")
        assert paused_interrupt.status_code == 409
        assert paused_interrupt.json()["error"]["code"] == "run_not_interruptible"
        assert paused_interrupt.json()["error"]["details"] == {
            "phase": "paused",
            "active_calls": 0,
        }

        resumed = client.post(
            f"/api/runs/{first['id']}/resume",
            headers={"Idempotency-Key": "resume-1"},
        )
        resume_replay = client.post(
            f"/api/runs/{first['id']}/resume",
            headers={"Idempotency-Key": "resume-1"},
        )
        assert resumed.status_code == 200
        assert resumed.headers["Idempotency-Key"] == "resume-1"
        assert resume_replay.json() == resumed.json()
        assert resumed.json()["phase"] == "autonomous_exploration"
        assert resumed.json()["status"] == "running"
        assert resumed.json()["pause_requested"] is False
        assert resumed.json()["hard_interrupt_requested"] is False
        assert resumed.json()["branches"][0]["status"] == "active"
        assert resumed.json()["commands"] == {
            "can_pause": True,
            "can_resume": False,
            "can_interrupt": True,
            "branchable_step_revision_ids": [],
        }

        duplicate_resume = client.post(f"/api/runs/{first['id']}/resume")
        assert duplicate_resume.status_code == 409
        assert duplicate_resume.json()["error"]["code"] == "run_not_resumable"
        assert duplicate_resume.json()["error"]["details"] == {"phase": "autonomous_exploration"}

        interrupted = client.post(
            f"/api/runs/{first['id']}/interrupt",
            headers={"Idempotency-Key": "interrupt-1"},
        )
        interrupt_replay = client.post(
            f"/api/runs/{first['id']}/interrupt",
            headers={"Idempotency-Key": "interrupt-1"},
        )
        assert interrupted.status_code == 200
        assert interrupt_replay.json() == interrupted.json()
        assert interrupted.json()["phase"] == "interrupted"
        assert interrupted.json()["pause_requested"] is False
        assert interrupted.json()["hard_interrupt_requested"] is True
        assert interrupted.json()["commands"] == {
            "can_pause": False,
            "can_resume": False,
            "can_interrupt": False,
            "branchable_step_revision_ids": [],
        }

        duplicate_interrupt = client.post(f"/api/runs/{first['id']}/interrupt")
        assert duplicate_interrupt.status_code == 409
        assert duplicate_interrupt.json()["error"]["code"] == "run_not_interruptible"
        assert duplicate_interrupt.json()["error"]["details"] == {
            "phase": "interrupted",
            "active_calls": 0,
        }


def test_completed_run_rejects_pause_resume_and_interrupt(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()

    expected_codes = {
        "pause": "run_not_pausable",
        "resume": "run_not_resumable",
        "interrupt": "run_not_interruptible",
    }
    for command, code in expected_codes.items():
        response = client.post(f"/api/runs/{run['id']}/{command}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["details"]["phase"] == "review_ready"


def test_branch_from_sealed_review_node_preserves_parent_and_is_idempotent(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    command = {
        "from_step_revision_id": run["steps"][0]["revisionId"],
        "kind": "human_direction",
        "instruction": "Check the boundary case independently.",
    }
    first = client.post(
        f"/api/runs/{run['id']}/branches",
        json=command,
        headers={"Idempotency-Key": "branch-1"},
    )
    replay = client.post(
        f"/api/runs/{run['id']}/branches",
        json=command,
        headers={"Idempotency-Key": "branch-1"},
    )
    assert first.status_code == replay.status_code == 201
    payload = first.json()
    assert replay.json() == payload
    assert payload["phase"] == "review_ready"
    assert len(payload["branches"]) == 2
    child = payload["branches"][1]
    assert child["parent_branch_id"] == "branch-run-0001-001"
    assert child["anchor_step_revision_id"] == "revision-run-0001-001"
    assert child["kind"] == "human_direction"
    assert child["step_revision_ids"] == ["revision-run-0001-001", "revision-run-0001-002"]
    assert payload["edges"][0]["kind"] == "human_direction"


def test_human_revision_uses_replace_prefix(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    old_hash = run["steps"][0]["output_sha256"]
    replacement = {
        "claim": "The corrected human-authored claim.",
        "why": "The original claim overstated its boundary.",
        "source": "Direct human review of the sealed step.",
        "derivation": "Restrict the statement to the explicitly supported regime.",
        "scope": "Only the bounded regime specified here.",
    }
    pretty_instruction = json.dumps(replacement, ensure_ascii=False, indent=2)
    parsed = CreateBranchRequest.model_validate(
        {
            "from_step_revision_id": run["steps"][0]["revisionId"],
            "kind": "human_revision",
            "instruction": pretty_instruction,
        }
    )
    expected_canonical = json.dumps(
        replacement,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert parsed.canonical_instruction() == expected_canonical
    response = client.post(
        f"/api/runs/{run['id']}/branches",
        json={
            "from_step_revision_id": run["steps"][0]["revisionId"],
            "kind": "human_revision",
            "instruction": pretty_instruction,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    child = payload["branches"][1]
    assert child["kind"] == "human_revision"
    assert child["step_revision_ids"] == ["revision-run-0001-002"]
    assert payload["branches"][0]["step_revision_ids"] == ["revision-run-0001-001"]
    assert payload["steps"][0]["output_sha256"] == old_hash
    replacement_step = payload["steps"][1]
    assert replacement_step["content"] == replacement
    assert replacement_step["provenance"] is None
    assert replacement_step["output_sha256"] == hashlib.sha256(expected_canonical.encode()).hexdigest()


def test_human_revision_requires_exact_five_field_json(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    revision_id = run["steps"][0]["revisionId"]
    invalid_instructions = [
        "not json",
        json.dumps(
            {
                "claim": "c",
                "why": "w",
                "source": "s",
                "derivation": "d",
            }
        ),
        json.dumps(
            {
                "claim": "c",
                "why": "w",
                "source": "s",
                "derivation": "d",
                "scope": "bounded",
                "extra": "not allowed",
            }
        ),
    ]
    for instruction in invalid_instructions:
        response = client.post(
            f"/api/runs/{run['id']}/branches",
            json={
                "from_step_revision_id": revision_id,
                "kind": "human_revision",
                "instruction": instruction,
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"


def test_unsealed_pending_revision_cannot_be_branched() -> None:
    service = FakeDerivationService(auto_complete_on_submit=False)
    with TestClient(create_app(service)) as client:
        run = client.post("/api/runs", json=run_request()).json()
        assert run["rootStepId"] is None
        assert run["steps"] == []
        response = client.post(
            f"/api/runs/{run['id']}/branches",
            json={
                "from_step_revision_id": "pending-run-0001-001",
                "kind": "human_direction",
                "instruction": "This must be rejected.",
            },
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "step_not_sealed"


def test_branch_creation_never_exceeds_call_or_active_branch_caps(client: TestClient) -> None:
    request = run_request()
    request["config"]["max_model_calls"] = 1  # type: ignore[index]
    request["config"]["max_active_branches"] = 1  # type: ignore[index]
    run = client.post("/api/runs", json=request).json()
    command = {
        "from_step_revision_id": run["steps"][0]["revisionId"],
        "kind": "human_direction",
        "instruction": "Do not start a call beyond the cap.",
    }
    capped = client.post(f"/api/runs/{run['id']}/branches", json=command)
    assert capped.status_code == 201
    payload = capped.json()
    assert payload["phase"] == "review_ready_due_to_cap"
    assert payload["budget"] == {"usedSteps": 1, "maxSteps": 1}
    assert len(payload["steps"]) == 1
    assert payload["branches"][1]["status"] == "active"

    blocked = client.post(f"/api/runs/{run['id']}/branches", json=command)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "max_active_branches_reached"


def test_structured_not_found_and_request_id(client: TestClient) -> None:
    response = client.get("/api/runs/run-missing", headers={"X-Request-ID": "request-123"})
    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.json() == {
        "error": {
            "code": "run_not_found",
            "message": "Derivation run 'run-missing' does not exist.",
            "details": None,
        },
        "request_id": "request-123",
    }


def test_malformed_run_identifier_is_rejected_before_service_lookup(client: TestClient) -> None:
    response = client.get("/api/runs/%20bad%20id")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_local_origin_policy_and_cors(client: TestClient) -> None:
    rejected = client.post(
        "/api/runs",
        json=run_request(),
        headers={"Origin": "https://evil.example"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "origin_not_allowed"

    accepted = client.post(
        "/api/runs",
        json=run_request(),
        headers={"Origin": "http://localhost:5173"},
    )
    assert accepted.status_code == 201
    assert accepted.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_body_limit_returns_structured_413() -> None:
    service = FakeDerivationService()
    app = create_app(service, settings=ApiSettings(max_body_bytes=128))
    with TestClient(app) as client:
        response = client.post("/api/runs", json=run_request(question="x" * 500))
    assert response.status_code == 413
    assert response.json()["error"] == {
        "code": "request_too_large",
        "message": "Request body exceeds the configured limit.",
        "details": {"max_bytes": 128},
    }


def test_streamed_body_limit_does_not_depend_on_content_length() -> None:
    sent: list[dict[str, object]] = []
    incoming = [
        {"type": "http.request", "body": b"x" * 80, "more_body": True},
        {"type": "http.request", "body": b"x" * 80, "more_body": False},
    ]

    async def consuming_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await JSONResponse({"unexpected": True})(scope, receive, send)

    async def receive() -> dict[str, object]:
        return incoming.pop(0)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/runs",
        "raw_path": b"/api/runs",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
        "state": {"request_id": "stream-limit-test"},
    }
    middleware = RequestBodyLimitMiddleware(consuming_app, max_bytes=128)
    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = next(message for message in sent if message["type"] == "http.response.body")
    assert start["status"] == 413
    assert b"request_too_large" in body["body"]


def test_health_and_capabilities(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "service": "deterministic-fake"}
    capabilities = client.get("/api/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["event_transport"] == "sse"
    assert capabilities.json()["branch_kinds"] == ["human_direction", "human_revision"]
    assert "recovering" in capabilities.json()["phases"]
    assert "review_ready_due_to_cap" in capabilities.json()["phases"]
    defaults = capabilities.json()["create_run_defaults"]
    assert defaults["config"]["backend"] == {
        "name": "deterministic-fake-runtime",
        "version": "1",
    }
    assert defaults["config"]["writer"] == {
        "provider": "fake",
        "model": "deterministic",
        "effort": "none",
    }
    assert defaults["runtime"]["capability_profile"] == "benchmark_symbolic_v1"
    assert defaults["config"]["max_model_calls"] == 100
    assert "deterministic" in defaults["allowed_models"]
    assert "none" in defaults["allowed_efforts"]
    assert defaults["allowed_models"]
    assert defaults["allowed_efforts"]
