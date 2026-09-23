"""Both record generations, read by one verifier.

Records written by the earlier runtime are Record v1; the later runtime writes
v1.1. A verifier that only reads the current generation would strand every
record already written, which defeats the purpose. These tests state what each
generation requires, and prove the reader handles both — including a complete
v1.1 record built by restating the shipped v1 example in the newer vocabulary.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    CANONICAL_V1,
    CANONICAL_V1_1,
    EVENT_V1,
    EVENT_V1_1,
    minimal_events,
    read_events,
    rechain,
    upgrade_to_v1_1,
)

from derivation_agent_record import ContractError, replay_events


def _canonical(events: list[dict[str, Any]]) -> dict[str, Any]:
    return replay_events(events).canonical


# ---------------------------------------------------------------------------
# Both generations verify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [(EVENT_V1, CANONICAL_V1), (EVENT_V1_1, CANONICAL_V1_1)],
    ids=["v1", "v1_1"],
)
def test_a_minimal_record_verifies(version: str, expected: str) -> None:
    canonical = _canonical(minimal_events(version=version))
    assert canonical["schema_version"] == expected
    assert canonical["event_log"]["event_count"] == 1
    assert canonical["summary"]["branch_count"] == 0


def test_a_complete_v1_1_record_verifies(example_run: Path) -> None:
    """The shipped example, restated in v1.1, replays to the same shape.

    Every event body changes in the upgrade, so every digest and every link in
    the chain is recomputed — this is a different 51-event record that happens
    to describe the same run, not the same file with a new label.
    """

    original = read_events(example_run / "events.jsonl")
    if original[0]["schema_version"] != EVENT_V1:
        pytest.skip(f"{example_run.name} is not a v1 record")

    before = _canonical(copy.deepcopy(original))
    after = _canonical(upgrade_to_v1_1(original))

    assert after["schema_version"] == CANONICAL_V1_1
    assert after["event_log"]["event_count"] == before["event_log"]["event_count"]
    assert after["event_log"]["head_event_sha256"] != before["event_log"]["head_event_sha256"]
    for field in (
        "branch_count",
        "step_revision_count",
        "model_call_count",
        "check_count",
        "candidate_count",
        "judgement_count",
    ):
        assert after["summary"][field] == before["summary"][field], field


# ---------------------------------------------------------------------------
# What each generation requires of the other
# ---------------------------------------------------------------------------


def test_v1_1_adds_a_source_evidence_section_to_the_canonical_form() -> None:
    assert "source_evidence" not in _canonical(minimal_events(version=EVENT_V1))
    assert _canonical(minimal_events(version=EVENT_V1_1))["source_evidence"] == []


def test_v1_1_adds_a_conditional_candidate_status() -> None:
    assert "conditional" not in _canonical(minimal_events(version=EVENT_V1))["summary"]["candidate_status_counts"]
    assert "conditional" in _canonical(minimal_events(version=EVENT_V1_1))["summary"]["candidate_status_counts"]


def test_v1_1_configuration_fields_are_required_in_v1_1() -> None:
    events = minimal_events(version=EVENT_V1_1)
    del events[0]["payload"]["configuration"]["checker_enabled"]
    with pytest.raises(ContractError, match="configuration"):
        replay_events(rechain(events))


def test_v1_1_configuration_fields_are_rejected_in_v1() -> None:
    """A v1 reader does not silently accept fields it cannot interpret."""

    events = minimal_events(version=EVENT_V1)
    events[0]["payload"]["configuration"]["checker_enabled"] = True
    with pytest.raises(ContractError, match="configuration"):
        replay_events(rechain(events))


def test_only_v1_1_may_leave_the_call_budget_open() -> None:
    events = minimal_events(version=EVENT_V1)
    events[0]["payload"]["configuration"]["max_model_calls"] = None
    with pytest.raises(ContractError, match="max_model_calls"):
        replay_events(rechain(events))

    unlimited = minimal_events(version=EVENT_V1_1)
    assert unlimited[0]["payload"]["configuration"]["max_model_calls"] is None
    assert _canonical(unlimited)["run"]["configuration"]["max_model_calls"] is None


@pytest.mark.parametrize(
    "event_type",
    [
        "model_revision_applied",
        "model_revision_deferred",
        "source_evidence_registered",
        "check_retry_authorized",
        "writer_route_completion",
        "writer_output_normalized",
    ],
)
def test_v1_1_only_event_types_are_unknown_to_v1(event_type: str) -> None:
    """The six event types v1.1 introduced are rejected outright in a v1 record
    rather than being skipped as unrecognised."""

    events = minimal_events(version=EVENT_V1)
    events.append(
        {
            **copy.deepcopy(events[0]),
            "event_id": "evt-0002",
            "type": event_type,
            "payload": {},
        }
    )
    with pytest.raises(ContractError, match="unsupported event type"):
        replay_events(rechain(events))


def test_a_record_cannot_change_version_midway() -> None:
    events = minimal_events(version=EVENT_V1)
    events.append({**copy.deepcopy(events[0]), "event_id": "evt-0002", "schema_version": EVENT_V1_1})
    with pytest.raises(ContractError, match="record version cannot change"):
        replay_events(rechain(events))


def test_an_unknown_version_is_refused() -> None:
    events = minimal_events(version=EVENT_V1)
    events[0]["schema_version"] = "derivation-agent-event-v2"
    with pytest.raises(ContractError, match="schema_version"):
        replay_events(rechain(events))


def test_the_first_event_must_open_the_run() -> None:
    events = minimal_events(version=EVENT_V1)
    events[0]["type"] = "branch_created"
    with pytest.raises(ContractError, match="first event must be run_created"):
        replay_events(rechain(events))
