"""Every record under ``tests/records/`` is a test, discovered by being there.

This is the corpus `CONTRIBUTING.md` points at. A record someone sends because
the verifier got it wrong is worth far more as a permanent case than as a fixed
bug, so adding one is meant to cost nothing but dropping a directory in:

    tests/records/should_verify/<case>/{events.jsonl,why.md}
    tests/records/must_be_rejected/<case>/{events.jsonl,why.md}

``why.md`` is required, and it is the point of the exercise. A rejected record
with no written account of *which* rule should have caught it is a puzzle, not a
test — so the file is checked for existence here and read by humans.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO_ROOT

from derivation_agent_record import ContractError, load_events, replay_events

CORPUS = REPO_ROOT / "tests" / "records"


def cases(kind: str) -> list[Path]:
    directory = CORPUS / kind
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if (path / "events.jsonl").is_file())


SHOULD_VERIFY = cases("should_verify")
MUST_BE_REJECTED = cases("must_be_rejected")


def test_the_corpus_is_not_empty() -> None:
    """A corpus that quietly emptied itself would make every test below pass."""

    assert SHOULD_VERIFY, "tests/records/should_verify/ has no cases"
    assert MUST_BE_REJECTED, "tests/records/must_be_rejected/ has no cases"


@pytest.mark.parametrize("case", SHOULD_VERIFY, ids=[path.name for path in SHOULD_VERIFY])
def test_a_sound_record_verifies(case: Path) -> None:
    result = replay_events(load_events(case / "events.jsonl"))

    assert result.canonical["event_log"]["event_count"] > 0


@pytest.mark.parametrize("case", MUST_BE_REJECTED, ids=[path.name for path in MUST_BE_REJECTED])
def test_an_unsound_record_is_rejected(case: Path) -> None:
    with pytest.raises(ContractError):
        replay_events(load_events(case / "events.jsonl"))


@pytest.mark.parametrize(
    "case", SHOULD_VERIFY + MUST_BE_REJECTED, ids=[path.name for path in SHOULD_VERIFY + MUST_BE_REJECTED]
)
def test_every_case_says_why(case: Path) -> None:
    why = case / "why.md"

    assert why.is_file(), f"{case.name} has no why.md"
    assert why.read_text(encoding="utf-8").strip(), f"{case.name}: why.md is empty"
