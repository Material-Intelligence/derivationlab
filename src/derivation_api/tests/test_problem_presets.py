"""Direct-start transport remains explicit about unchecked unlimited runs."""

from fastapi.testclient import TestClient

from .conftest import run_request

# A textbook problem typed directly by the user, not confirmed through Intake.
DIRECT_TEXTBOOK_PROBLEM = {
    "problem_id": "harmonic-oscillator-ground-state",
    "version": 1,
    "supersedes_version": None,
    "objective": "Derive the ground-state energy of the one-dimensional quantum harmonic oscillator.",
    "givens": ["H = p^2/(2m) + m omega^2 x^2/2 with [x, p] = i hbar."],
    "assumptions": ["The oscillator is one-dimensional and non-relativistic."],
    "scope": "The ground state only.",
    "deliverable": "A closed-form ground-state energy with a symbol table.",
    "allowed_tools": ["scientific_compute"],
    "allowed_references": [],
    "success_criteria": ["The energy is expressed in terms of hbar and omega."],
    "source_pack": None,
    "origin": "direct_spec",
    "confirmed_by_user": False,
}


def test_problem_presets_offer_no_source_backed_presets(client: TestClient) -> None:
    response = client.get("/api/problem-presets")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["presets"] == []
    assert catalog["method_references"] == []
    assert catalog["method_source_pack"]["pack_id"] == "pack_empty"


def test_unlimited_unchecked_run_supports_human_branch(client: TestClient) -> None:
    request = run_request()
    request["problem"] = dict(DIRECT_TEXTBOOK_PROBLEM)
    request["config"].update(record_version="1.1", max_model_calls=None, checker_enabled=False)
    request["runtime"]["capability_profile"] = "source_reading_v1"
    response = client.post("/api/runs", json=request, headers={"Idempotency-Key": "direct-unlimited"})
    assert response.status_code == 201
    run = response.json()
    assert run["budget"]["maxSteps"] is None
    assert run["steps"][0]["checks"]["physics"] == "not_requested"
    branch = client.post(
        f"/api/runs/{run['id']}/branches",
        json={
            "from_step_revision_id": run["steps"][0]["revisionId"],
            "kind": "human_direction",
            "instruction": "Check the limiting case.",
        },
        headers={"Idempotency-Key": "direct-unlimited-branch"},
    )
    assert branch.status_code == 201
    assert branch.json()["phase"] == "review_ready"
