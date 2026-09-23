"""Everything shipped under ``examples/`` must hold up to its own claims.

A record that does not replay, or a viewer that does not match the record it
claims to show, would undermine the one thing this snapshot is for. These tests
run against whatever is in ``examples/runs/``, so adding a run means it gets
checked, and forgetting to check it is not possible.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import EXAMPLE_RUNS_DIR, read_events
from v1_1_example import worked_v1_1_events

from derivation_agent_record import load_events, render_html, replay_events

REQUIRED_FILES = ("events.jsonl", "manifest.json", "viewer.html")

#: Record v1.1 can carry registered source text inside the hash chain. Because
#: the text is hashed, it cannot be removed from a record after the fact
#: without destroying it — so a record carrying third-party text can never be
#: published, and none may appear here by accident.
FORBIDDEN_EVENT_TYPES = frozenset({"source_evidence_registered"})


def test_every_example_is_complete(example_run: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (example_run / name).is_file()]
    assert not missing, f"{example_run.name} is missing {missing}"


def test_every_example_replays(example_run: Path) -> None:
    result = replay_events(load_events(example_run / "events.jsonl"))
    assert result.canonical["event_log"]["event_count"] > 0
    assert result.canonical["schema_version"].startswith("derivation-agent-canonical-v1")


def test_every_example_carries_no_registered_source_text(example_run: Path) -> None:
    types = {event["type"] for event in read_events(example_run / "events.jsonl")}
    assert not (types & FORBIDDEN_EVENT_TYPES), (
        f"{example_run.name} carries {sorted(types & FORBIDDEN_EVENT_TYPES)}; "
        "records with registered source text must not be published"
    )


def test_the_committed_viewer_is_the_one_the_renderer_produces(example_run: Path) -> None:
    """The shipped HTML must be reproducible from the shipped events, exactly.

    If this fails, the viewer was hand-edited or the renderer changed, and a
    reader who re-renders would get something other than what they were shown.
    """

    events = load_events(example_run / "events.jsonl")
    expected = render_html(replay_events(events).canonical, events)
    assert (example_run / "viewer.html").read_text(encoding="utf-8") == expected


def test_every_manifest_is_a_json_object(example_run: Path) -> None:
    manifest = json.loads((example_run / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict) and manifest


def test_the_manifest_backend_matches_the_record(example_run: Path) -> None:
    """The sidecar and the hash-chained record must agree about what ran.

    The manifest is not covered by the chain, so it is the one file in a run
    directory that could quietly disagree with the record. The backend name is
    the field a caption is written from, so it is the field to pin.
    """

    manifest = json.loads((example_run / "manifest.json").read_text(encoding="utf-8"))
    recorded = replay_events(load_events(example_run / "events.jsonl")).canonical
    assert manifest["api_config"]["backend"] == recorded["run"]["configuration"]["backend"]


def test_examples_are_documented(example_run: Path) -> None:
    """Every run has a caption, and the caption names the run's own directory.

    A record without a plainly written account of what it is and is not invites
    exactly the over-reading this snapshot is trying to avoid.
    """

    readme = (example_run.parent / "README.md").read_text(encoding="utf-8")
    assert example_run.name in readme, f"{example_run.name} has no entry in examples/runs/README.md"


def test_both_record_generations_ship() -> None:
    """A verifier that reads two contract versions must ship one record of each.

    Half of the replay engine is Record v1.1 only. If every shipped record were
    a v1, that half would be exercised by nothing a reader can run.
    """

    versions = {
        read_events(run / "events.jsonl")[0]["schema_version"]
        for run in EXAMPLE_RUNS_DIR.iterdir()
        if (run / "events.jsonl").is_file()
    }

    assert {"derivation-agent-event-v1", "derivation-agent-event-v1.1"} <= versions


def test_the_shipped_v1_1_record_is_exactly_what_the_builder_produces() -> None:
    """The synthetic record and the code that describes it cannot drift apart.

    ``examples/runs/worked_v1_1/events.jsonl`` was written by
    ``tests/v1_1_example.py``. If the builder changes, the committed file has to
    be regenerated, and this is where a reader finds that out.
    """

    shipped = read_events(EXAMPLE_RUNS_DIR / "worked_v1_1" / "events.jsonl")

    assert shipped == worked_v1_1_events()
