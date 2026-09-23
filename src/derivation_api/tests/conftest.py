from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from derivation_api.application import create_app
from derivation_api.fake_service import FakeDerivationService

VALID_RUN_REQUEST = {
    "problem": {
        "problem_id": "bounded-deterministic-test",
        "version": 1,
        "supersedes_version": None,
        "objective": "Derive a bounded deterministic test result.",
        "givens": ["The deterministic fixture is available."],
        "assumptions": [],
        "scope": "Application integration only.",
        "deliverable": "A checked symbolic derivation.",
        "allowed_tools": ["scientific_compute"],
        "allowed_references": [],
        "success_criteria": ["The terminal judge returns a verdict."],
        "source_pack": None,
        "confirmed_by_user": True,
    },
    "config": {
        "granularity": "one_claim",
        "writer": {"provider": "openai", "model": "test-writer", "effort": "medium"},
        "checker": {"provider": "openai", "model": "test-checker", "effort": "low"},
        "judge": {"provider": "openai", "model": "test-judge", "effort": "high"},
        "backend": {"name": "codex-app-server", "version": "0.147.0"},
        "max_model_calls": 12,
        "max_active_branches": 2,
        "reference_allowed": False,
        "allowed_paths": [],
    },
    "runtime": {
        "auth_mode": "chatgpt",
        "concurrency": 1,
        "retries": 0,
        "max_run_seconds": None,
    },
}


def run_request(*, question: str | None = None) -> dict[str, object]:
    request = copy.deepcopy(VALID_RUN_REQUEST)
    if question is not None:
        request["problem"]["objective"] = question  # type: ignore[index]
    return request


@pytest.fixture
def fake_service() -> FakeDerivationService:
    return FakeDerivationService()


@pytest.fixture
def client(fake_service: FakeDerivationService):
    with TestClient(create_app(fake_service)) as test_client:
        yield test_client
