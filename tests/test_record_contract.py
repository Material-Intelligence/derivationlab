"""Collect the package's offline contract suite under pytest.

The suite itself lives in ``derivation_agent_record.selftest`` so that it ships
with the package and runs with nothing installed:

    python3 -m unittest derivation_agent_record.selftest -v

This module exists only so ``pytest`` finds the same tests. Importing the
``TestCase`` is the whole shim; pytest collects ``unittest.TestCase`` subclasses
natively.
"""

import copy
from pathlib import Path

import pytest
from conftest import minimal_events, write_events

from derivation_agent_record import ContractError, load_events, replay_events
from derivation_agent_record.model import compute_event_sha256
from derivation_agent_record.selftest import RecordContractTests  # noqa: F401


def test_hashing_an_event_does_not_touch_the_caller_s_event() -> None:
    """Why ``compute_event_sha256`` may take a shallow copy.

    It runs once per event on the verifier's hot path, and deep-copying every
    payload there was measurably about a third of replay time on a large
    record. The copy is shallow because nothing in the function mutates what it
    was given — it serializes and hashes. That is the property asserted here, so
    the reason the optimisation is safe is written down rather than assumed.
    """

    event = {
        "seq": 1,
        "payload": {"nested": {"list": [1, 2, 3]}, "text": "unchanged"},
        "event_sha256": "0" * 64,
    }
    before = copy.deepcopy(event)

    first = compute_event_sha256(event)

    assert event == before
    assert compute_event_sha256(event) == first


def test_replaying_a_record_does_not_touch_the_caller_s_events() -> None:
    """The same property, asserted where a caller can actually observe it.

    ``replay()`` used to deep-copy every event before handling it, one line
    above the shallow copy whose safety is argued for at length in
    ``compute_event_sha256`` — so the smaller of the two copies was the one that
    had been optimised away. Neither is needed: no handler writes into an event,
    and every handler that keeps part of a payload ``clone()``s what it keeps.
    That is what this pins, so dropping the deep copy is a checked claim rather
    than a hope.
    """

    events = minimal_events()
    before = copy.deepcopy(events)

    result = replay_events(events)

    assert events == before
    assert result.canonical["run"]["configuration"] is not events[0]["payload"]["configuration"]

    result.canonical["run"]["configuration"]["granularity"] = "one_task"
    result.canonical["run"]["input_policy"]["allowed_paths"].append("mutated")

    assert events == before


def test_a_blank_line_is_tolerated_but_does_not_become_an_event(tmp_path: Path) -> None:
    """One event per line is a rule for producers, read leniently by readers.

    A stray trailing newline is a transport artefact, not a claim about the
    record, and treating it as a missing event would reject sound records for a
    reason that has nothing to do with them. What a blank line may never do is
    consume a sequence number: RECORD_SPEC section 2 states the tolerance, and
    this is it.
    """

    events = minimal_events()
    path = write_events(tmp_path / "run", events)
    path.write_text("\n" + path.read_text(encoding="utf-8") + "   \n\n", encoding="utf-8")

    loaded = load_events(path)

    assert loaded == events
    assert replay_events(loaded).canonical["event_log"]["event_count"] == 1


def test_a_file_of_nothing_but_blank_lines_is_not_a_record(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n   \n", encoding="utf-8")

    with pytest.raises(ContractError, match="event log is empty"):
        load_events(path)
