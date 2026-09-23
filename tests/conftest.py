"""Shared helpers for the pytest suite.

Two things live here and nothing else:

* **Record builders.** The repository ships one example record, and it is a
  Record v1. Every other record the tests need is built here, in code, from the
  contract itself — a minimal v1 run, the same run as v1.1, and an upgrade of
  the full shipped example to v1.1. Building them beats committing more fixture
  files: the builder *is* a readable statement of what the contract requires,
  and it cannot drift away from the verifier the way a frozen blob can.

* **A CLI runner.** The public surface this snapshot promises is two commands.
  The tests exercise them the way a reader will — as a subprocess, through
  ``python -m derivation_agent_record`` — rather than by calling ``main()``
  in-process, so that argument parsing, exit codes and stderr are all covered.

Nothing here writes inside the repository. Records under test are written to
pytest's ``tmp_path``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from derivation_agent_record.model import compute_event_sha256, sha256_text

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_RUNS_DIR = REPO_ROOT / "examples" / "runs"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

EVENT_V1 = "derivation-agent-event-v1"
EVENT_V1_1 = "derivation-agent-event-v1.1"
CANONICAL_V1 = "derivation-agent-canonical-v1"
CANONICAL_V1_1 = "derivation-agent-canonical-v1.1"

#: A record contains no repository paths of its own, but it does cite the
#: contract and the two schemas by path and digest. A test record cites files
#: that are really here, at their real digests — a pin nothing resolves is how
#: the shipped v1.1 record came to carry three placeholders at real paths.
_SPEC_PATH = "docs/RECORD_SPEC.md"
_EVENT_SCHEMA_PATH = "docs/spec/derivation_agent_event_v1.schema.json"
_CANONICAL_SCHEMA_PATH = "docs/spec/derivation_agent_canonical_v1.schema.json"


def example_run_dirs() -> list[Path]:
    """Every shipped example run, sorted. Empty is a failure, not a pass."""

    if not EXAMPLE_RUNS_DIR.is_dir():
        return []
    return sorted(path for path in EXAMPLE_RUNS_DIR.iterdir() if (path / "events.jsonl").is_file())


EXAMPLE_RUNS = example_run_dirs()
EXAMPLE_RUN_IDS = [path.name for path in EXAMPLE_RUNS]


@pytest.fixture(params=EXAMPLE_RUNS, ids=EXAMPLE_RUN_IDS)
def example_run(request: pytest.FixtureRequest) -> Path:
    """One shipped example run directory, once per test."""

    return request.param


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


def rechain(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Renumber and re-hash a whole log so it is internally consistent again.

    This is what an attacker who rewrites history has to do, and it is exactly
    why the chain is not the only defence: a full rewrite produces a log whose
    hashes all agree. What stops it is the cross-event rules the replay engine
    enforces on top. ``tests/test_tamper.py`` demonstrates both halves.
    """

    previous: str | None = None
    for index, event in enumerate(events, start=1):
        event["seq"] = index
        event["prev_event_sha256"] = previous
        event.pop("event_sha256", None)
        event["event_sha256"] = compute_event_sha256(event)
        previous = event["event_sha256"]
    return events


def write_events(directory: Path, events: Sequence[dict[str, Any]], name: str = "events.jsonl") -> Path:
    """Write an event log as JSON Lines and return its path."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read an event log as plain dicts, without going through the verifier."""

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------


def _digest_of(label: str) -> str:
    """A real digest of a real string, so no test carries a magic constant."""

    return sha256_text(label)


def _commit_of(label: str) -> str:
    """A 40-hex value shaped like a git commit, derived rather than invented."""

    return sha256_text(label)[:40]


def pin_of(path: str) -> dict[str, str]:
    """One ``run_created`` pin: a shipped path and that file's real SHA-256."""

    return {"path": path, "sha256": hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()}


def minimal_events(*, version: str = EVENT_V1) -> list[dict[str, Any]]:
    """The smallest log the contract accepts: one ``run_created`` event.

    A run that was opened and never stepped is a legitimate record, and it is
    the cheapest possible statement of what an implementation must emit before
    anything else can happen. The v1.1 form differs in exactly three places —
    the schema version, two configuration fields, and an unbounded call budget
    — which is the point of parameterising it here.
    """

    if version not in {EVENT_V1, EVENT_V1_1}:
        raise ValueError(f"unsupported record version {version!r}")
    is_v1_1 = version == EVENT_V1_1

    configuration: dict[str, Any] = {
        "granularity": "one_claim",
        "max_active_branches": 2,
        "max_model_calls": None if is_v1_1 else 12,
        "models": {
            role: {"provider": "fake", "model": f"deterministic-{role}", "effort": "low"}
            for role in ("writer", "checker", "judge")
        },
        "backend": {"name": "deterministic-fake-runtime", "version": "1"},
    }
    if is_v1_1:
        configuration["checker_enabled"] = True
        configuration["max_local_repairs"] = 0

    event: dict[str, Any] = {
        "schema_version": version,
        "run_id": "run-minimal-" + ("v1-1" if is_v1_1 else "v1"),
        "seq": 1,
        "event_id": "evt-0001",
        "recorded_at": "2026-01-01T00:00:00Z",
        "type": "run_created",
        "actor": {"kind": "system", "id": "record-runtime"},
        "prev_event_sha256": None,
        "payload": {
            "record_spec": pin_of(_SPEC_PATH),
            "event_schema": pin_of(_EVENT_SCHEMA_PATH),
            "canonical_schema": pin_of(_CANONICAL_SCHEMA_PATH),
            "code_commit": _commit_of("minimal record"),
            "task": {"id": "task-minimal", "sha256": _digest_of("task")},
            "pack": {"id": "pack-minimal", "sha256": _digest_of("pack")},
            "configuration": configuration,
            "input_policy": {"reference_allowed": False, "allowed_paths": []},
        },
    }
    return rechain([event])


def upgrade_to_v1_1(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restate a complete v1 log in the v1.1 vocabulary, then re-chain it.

    The whole delta between the two generations, for a record that uses no
    v1.1-only event type, is three edits. Writing them out as code is the
    clearest description of the version bump this repository can offer, and the
    test that replays the result proves the description is accurate.
    """

    upgraded = copy.deepcopy([dict(event) for event in events])
    checks: dict[str, str | None] = {}
    for event in upgraded:
        event["schema_version"] = EVENT_V1_1
        payload = event["payload"]
        if event["type"] == "run_created":
            # 1. v1.1 runs state whether the checker ran and how many local
            #    repairs the writer may attempt before it must fork.
            payload["configuration"]["checker_enabled"] = True
            payload["configuration"]["max_local_repairs"] = 0
        elif event["type"] == "check_completed":
            checks[payload["check_id"]] = payload["verdict"]
        elif event["type"] == "candidate_declared":
            # 2. A v1.1 candidate must name the checks it is carrying unresolved
            #    rather than leaving a reader to recompute them.
            payload["unresolved_check_ids"] = sorted(
                check_id
                for check_id in payload["required_check_ids"]
                if checks.get(check_id) in {"objection", "hard_defect"}
            )
    # 3. Every event body changed, so every hash and every link changes.
    return rechain(upgraded)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_cli(*args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the module CLI as a subprocess and capture everything it says.

    ``PYTHONPATH`` points at the checkout's ``src/`` so the suite behaves identically
    whether or not the package has been installed — which is the same promise
    the README makes to a reader who has only cloned the repository.
    """

    command = [sys.executable, "-m", "derivation_agent_record", *(str(arg) for arg in args)]
    return subprocess.run(
        command,
        cwd=str(cwd or REPO_ROOT),
        env={**_clean_env(), "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


@pytest.fixture
def cli() -> Iterator[Any]:
    """The CLI runner, as a fixture, so tests read as ``cli("verify", path)``."""

    yield run_cli
