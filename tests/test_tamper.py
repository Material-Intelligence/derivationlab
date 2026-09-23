"""What tampering with a record actually costs, demonstrated three ways.

The claim this snapshot makes is narrow and worth stating precisely, because
the overstated version of it is everywhere: a hash chain does not make a record
unforgeable. Anyone holding the file can rewrite it end to end and recompute
every digest. What the chain buys is that a *partial* edit is caught
immediately, and what catches a *total* rewrite is the second layer — the
cross-event rules the replay engine enforces, which a forger has to satisfy as
well.

Each test below is one of the three cases, run through the command a reader
would run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import read_events, rechain, run_cli, write_events

from derivation_agent_record.model import compute_event_sha256


def _tampered_copy(tmp_path: Path, source: Path, mutate, *, rehash: str) -> Path:
    """Copy a record, mutate it, and re-hash as much as ``rehash`` says.

    ``rehash="none"`` leaves every digest as it was; ``"event"`` recomputes only
    the edited event's own digest; ``"all"`` rewrites the entire chain.
    """

    events = read_events(source)
    index = mutate(events)
    if rehash == "event":
        events[index]["event_sha256"] = compute_event_sha256(
            {key: value for key, value in events[index].items() if key != "event_sha256"}
        )
    elif rehash == "all":
        rechain(events)
    elif rehash != "none":
        raise ValueError(rehash)
    return write_events(tmp_path, events)


def test_one_edited_byte_breaks_that_event(tmp_path: Path, example_run: Path) -> None:
    """The cheapest attack: change a payload and hope nobody re-hashes."""

    def mutate(events: list[dict]) -> int:
        events[0]["payload"]["task"]["id"] = "task-someone-else"
        return 0

    path = _tampered_copy(tmp_path, example_run / "events.jsonl", mutate, rehash="none")
    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "contract error" in result.stderr
    assert "event_sha256 mismatch" in result.stderr


def test_repairing_one_event_breaks_the_next_link(tmp_path: Path, example_run: Path) -> None:
    """The next attack up: fix the edited event's own digest. The event after
    it still remembers what the old one hashed to."""

    def mutate(events: list[dict]) -> int:
        events[0]["payload"]["task"]["id"] = "task-someone-else"
        return 0

    path = _tampered_copy(tmp_path, example_run / "events.jsonl", mutate, rehash="event")
    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "prev_event_sha256 mismatch" in result.stderr


def test_a_full_rewrite_is_caught_by_the_replay_rules_not_the_chain(tmp_path: Path, example_run: Path) -> None:
    """The honest case. Every digest agrees; the record still does not replay,
    because a candidate must freeze the transcript hash of the steps it names,
    and that hash is recomputed from the steps rather than trusted."""

    def mutate(events: list[dict]) -> int:
        for index, event in enumerate(events):
            if event["type"] == "candidate_declared":
                event["payload"]["transcript_sha256"] = "0" * 64
                return index
        pytest.skip(f"{example_run.name} declares no candidate")

    path = _tampered_copy(tmp_path, example_run / "events.jsonl", mutate, rehash="all")
    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "transcript hash mismatch" in result.stderr


def test_deleting_an_event_breaks_the_sequence(tmp_path: Path, example_run: Path) -> None:
    """Removing an inconvenient event from the middle is not an option either."""

    events = read_events(example_run / "events.jsonl")
    if len(events) < 3:
        pytest.skip("record is too short to delete from the middle")
    del events[len(events) // 2]
    path = write_events(tmp_path, events)

    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "contract error" in result.stderr


def test_reordering_two_events_is_rejected(tmp_path: Path, example_run: Path) -> None:
    events = read_events(example_run / "events.jsonl")
    if len(events) < 4:
        pytest.skip("record is too short to reorder")
    middle = len(events) // 2
    events[middle], events[middle + 1] = events[middle + 1], events[middle]
    path = write_events(tmp_path, events)

    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "contract error" in result.stderr


def test_a_second_run_cannot_be_spliced_in(tmp_path: Path, example_run: Path) -> None:
    """Events from another run cannot be appended to this one, even with the
    chain repaired: the run id is inside every event body."""

    events = read_events(example_run / "events.jsonl")
    foreign = json.loads(json.dumps(events[-1]))
    foreign["run_id"] = "run-somewhere-else"
    foreign["event_id"] = "evt-spliced"
    events.append(foreign)
    path = write_events(tmp_path, rechain(events))

    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "run_id changed" in result.stderr


def test_an_untouched_copy_still_verifies(tmp_path: Path, example_run: Path) -> None:
    """The control. A copy made by the same machinery, with nothing changed,
    must pass — otherwise every test above proves only that the copier is
    broken."""

    path = write_events(tmp_path, read_events(example_run / "events.jsonl"))
    result = run_cli("verify", path)
    assert result.returncode == 0, result.stderr
