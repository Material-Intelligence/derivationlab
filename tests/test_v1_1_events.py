"""The Record v1.1 event vocabulary, exercised against the real verifier.

Record v1.1 added six event types, and five of them are reachable in a record
that may be published (the sixth, ``source_evidence_registered``, puts source
text inside the hash chain). ``tests/v1_1_example.py`` builds one record that
uses all five; this module replays it, and then breaks it one rule at a time.

The negative cases matter more than the positive one. Each of them is a record
whose hashes all agree — every mutation re-chains the whole log — so nothing but
the cross-event rules can reject it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from conftest import rechain, write_events
from v1_1_example import worked_v1_1_events

from derivation_agent_record import ContractError, load_events, replay_events

V1_1_ONLY = {
    "model_revision_applied",
    "model_revision_deferred",
    "check_retry_authorized",
    "writer_route_completion",
    "writer_output_normalized",
}


def index_of(events: list[dict[str, Any]], type_: str, occurrence: int = 0) -> int:
    matches = [index for index, event in enumerate(events) if event["type"] == type_]
    assert len(matches) > occurrence, f"no {type_} #{occurrence} in the record"
    return matches[occurrence]


def index_of_action(events: list[dict[str, Any]], action: str) -> int:
    for index, event in enumerate(events):
        if event["type"] == "human_action_recorded" and event["payload"]["action"] == action:
            return index
    raise AssertionError(f"no {action} in the record")


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_the_worked_v1_1_record_replays() -> None:
    result = replay_events(worked_v1_1_events())

    assert result.canonical["schema_version"] == "derivation-agent-canonical-v1.1"
    assert result.canonical["summary"]["selected_candidate_id"] == "cand-1"
    assert result.canonical["summary"]["candidate_status_counts"]["eligible"] == 1


def test_the_worked_record_uses_every_publishable_v1_1_event_type() -> None:
    """A vocabulary the shipped records never use is a vocabulary nothing tests."""

    types = {event["type"] for event in worked_v1_1_events()}

    assert V1_1_ONLY <= types
    assert "source_evidence_registered" not in types


def test_event_ids_are_not_required_to_encode_their_order() -> None:
    """Ordering comes from ``seq``; an id is a name, not a position.

    The worked record uses hyphenated ids (``evt-0031``), which no arithmetic
    can be done on. A verifier that read ordering out of an id would fail here
    rather than verify — which is what it used to do.
    """

    events = worked_v1_1_events()

    assert all("_" not in event["event_id"] for event in events)
    replay_events(events)


# ---------------------------------------------------------------------------
# check_retry_authorized: the rule that a human cleared the failure afterwards
# ---------------------------------------------------------------------------


def move_human_action_before_the_failure(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-order the resume so it happens before the check it is meant to clear."""

    events = copy.deepcopy(events)
    action = events.pop(index_of_action(events, "resume_branch"))
    events.insert(index_of(events, "branch_created") + 1, action)
    return rechain(events)


def test_a_retry_authorised_before_the_failure_is_rejected() -> None:
    events = move_human_action_before_the_failure(worked_v1_1_events())

    with pytest.raises(ContractError, match="predates"):
        replay_events(events)


def test_a_low_numbered_failure_event_id_cannot_fake_the_ordering() -> None:
    """The rule reads ``seq``, so renaming the failure event proves nothing.

    This is the forgery the previous implementation admitted: it derived the
    failure's position by parsing the numeric tail of ``completed_event_id``, so
    a producer that named a late event ``00000001`` could authorise a retry from
    anywhere. Here the resume sits at sequence 4, the failure it claims to
    follow is event 16, and the record must still be rejected.
    """

    events = move_human_action_before_the_failure(worked_v1_1_events())
    completion = events[index_of(events, "check_completed")]
    assert completion["payload"]["verdict"] == "instrument_failure"
    completion["event_id"] = "00000001"
    events = rechain(events)

    resume = events[index_of_action(events, "resume_branch")]
    assert resume["seq"] < completion["seq"]

    with pytest.raises(ContractError, match="predates"):
        replay_events(events)


def test_a_retry_needs_a_resume_not_just_any_human_action() -> None:
    """The authorisation has to cite the act of resuming, not an unrelated one."""

    events = copy.deepcopy(worked_v1_1_events())
    retry = events[index_of(events, "check_retry_authorized")]
    retry["payload"]["human_action_id"] = "ha-0"

    with pytest.raises(ContractError, match="explicit resume action"):
        replay_events(rechain(events))


# ---------------------------------------------------------------------------
# The other four publishable v1.1 events
# ---------------------------------------------------------------------------


def test_normalisation_must_reproduce_the_content_it_claims() -> None:
    """The replacement list is re-applied; a normaliser is not taken at its word."""

    events = copy.deepcopy(worked_v1_1_events())
    normalization = events[index_of(events, "writer_output_normalized")]
    normalization["payload"]["replacements"][0]["replacement"] = "/"

    with pytest.raises(ContractError, match="invalid control restoration"):
        replay_events(rechain(events))


def test_a_model_revision_may_not_change_the_text_the_model_produced() -> None:
    events = copy.deepcopy(worked_v1_1_events())
    revision = events[index_of(events, "model_revision_applied")]
    revision["payload"]["content"]["claim"] += " Quod erat demonstrandum."

    with pytest.raises(ContractError, match="differs from immutable Writer output"):
        replay_events(rechain(events))


def test_a_call_may_carry_only_one_disposition() -> None:
    """Deferring is a disposition; a call cannot be disposed of twice."""

    events = copy.deepcopy(worked_v1_1_events())
    position = index_of(events, "model_revision_deferred")
    again = copy.deepcopy(events[position])
    again["event_id"] = "evt-repeat"
    events.insert(position + 1, again)

    with pytest.raises(ContractError, match="disposition already recorded"):
        replay_events(rechain(events))


def test_route_completion_may_not_skip_a_pending_check() -> None:
    events = copy.deepcopy(worked_v1_1_events())
    completion = events[index_of(events, "check_completed", 2)]
    assert completion["payload"]["check_id"] == "chk-3"
    events.pop(index_of(events, "check_completed", 2))

    with pytest.raises(ContractError, match="pending or failed instruments"):
        replay_events(rechain(events))


# ---------------------------------------------------------------------------
# Through the CLI, the way a reader meets it
# ---------------------------------------------------------------------------


def test_the_cli_verifies_a_v1_1_record(cli: Any, tmp_path: Any) -> None:
    path = write_events(tmp_path / "record", worked_v1_1_events())

    result = cli("verify", path)

    assert result.returncode == 0, result.stderr
    assert "48 events" in result.stdout


def test_the_cli_rejects_a_forged_retry(cli: Any, tmp_path: Any) -> None:
    events = move_human_action_before_the_failure(worked_v1_1_events())
    path = write_events(tmp_path / "record", events)

    result = cli("verify", path)

    assert result.returncode == 2
    assert "predates" in result.stderr


def test_the_record_on_disk_is_the_record_the_builder_describes(tmp_path: Any) -> None:
    """Round-trip through JSON Lines: what is written is what is verified."""

    path = write_events(tmp_path / "record", worked_v1_1_events())

    assert load_events(path) == worked_v1_1_events()
