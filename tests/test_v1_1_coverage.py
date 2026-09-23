"""The Record v1.1 rules that no publishable record can reach.

Registered source evidence cannot be published (see ``tests/v1_1_coverage.py``),
and every shipped record settles on an eligible candidate. So the handler that
freezes a source, the macro expansion it gates, three of the five replacement
kinds, three of the five hard-defect evidence kinds, and three of the four
candidate statuses were reached by nothing a reader — or the suite — could run.
This module is where each of them is exercised, positively and then negatively:
every negative case is re-chained, so its hashes all agree and only a cross-event
rule can reject it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from conftest import rechain, write_events
from v1_1_coverage import (
    CONTENT,
    EVIDENCE,
    RAW,
    SOURCE_ID,
    SOURCE_TEXT,
    blocked_candidate_events,
    conditional_candidate_events,
    evidence_and_normalization_events,
    judgement_of_the_blocked_candidate,
    judgement_of_the_provisional_candidate,
    provisional_candidate_events,
    selection_of_the_conditional_candidate,
)

from derivation_agent_record import ContractError, load_events, replay_events


def index_of(events: list[dict[str, Any]], type_: str) -> int:
    return next(index for index, event in enumerate(events) if event["type"] == type_)


def payload_of(events: list[dict[str, Any]], type_: str) -> dict[str, Any]:
    return events[index_of(events, type_)]["payload"]


def refused(events: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ContractError, match=message):
        replay_events(rechain(events))


def mutated(build: Callable[[], list[dict[str, Any]]], type_: str, mutate: Callable[[dict[str, Any]], None]):
    events = copy.deepcopy(build())
    mutate(payload_of(events, type_))
    return events


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_the_evidence_record_replays() -> None:
    from derivation_agent_record.model import sha256_text

    canonical = replay_events(evidence_and_normalization_events()).canonical

    assert canonical["schema_version"] == "derivation-agent-canonical-v1.1"
    assert canonical["source_evidence"] == [
        {
            "source_id": SOURCE_ID,
            "kind": "literature_quote",
            "text": SOURCE_TEXT,
            "sha256": sha256_text(SOURCE_TEXT),
        }
    ]
    assert canonical["step_revisions"][0]["content"] == CONTENT


def test_the_normalisation_really_changed_the_scientific_text() -> None:
    """A record whose 'normalisation' was a no-op would prove nothing."""

    assert RAW != CONTENT
    normalization = payload_of(evidence_and_normalization_events(), "writer_output_normalized")

    assert {item["kind"] for item in normalization["replacements"]} == {
        "ansi_escape_removed",
        "del_removed",
        "control_char_removed",
        "control_char_backslash",
        "macro_expansion",
    }


def test_the_check_carries_every_evidence_kind_the_contract_defines() -> None:
    from derivation_agent_record.model import HARD_DEFECT_EVIDENCE_KINDS

    kinds = {item["kind"] for item in EVIDENCE}

    assert kinds == HARD_DEFECT_EVIDENCE_KINDS | {"literature_quote"}


def test_the_record_round_trips_through_json_lines(tmp_path: Any) -> None:
    path = write_events(tmp_path / "record", evidence_and_normalization_events())

    assert load_events(path) == evidence_and_normalization_events()


# ---------------------------------------------------------------------------
# source_evidence_registered
# ---------------------------------------------------------------------------


def test_registered_source_text_must_hash_to_what_the_record_says() -> None:
    refused(
        mutated(evidence_and_normalization_events, "source_evidence_registered", lambda p: p.update(sha256="0" * 64)),
        "source evidence hash mismatch",
    )


def test_only_a_literature_quote_may_be_registered() -> None:
    refused(
        mutated(
            evidence_and_normalization_events,
            "source_evidence_registered",
            lambda p: p.update(kind="private_note"),
        ),
        "unsupported source evidence kind",
    )


def test_a_source_registered_after_the_first_model_call_is_refused() -> None:
    """Evidence has to be frozen before anything could have been written to fit it."""

    events = copy.deepcopy(evidence_and_normalization_events())
    late = copy.deepcopy(events[index_of(events, "source_evidence_registered")])
    late["event_id"] = "evt-late"
    late["payload"]["source_id"] = "src-late"
    events.insert(index_of(events, "model_call_started") + 1, late)

    refused(events, "frozen before model calls")


# ---------------------------------------------------------------------------
# writer_output_normalized: one negative per replacement kind
# ---------------------------------------------------------------------------


def _replacement(index: int, **changes: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(payload: dict[str, Any]) -> None:
        payload["replacements"][index].update(changes)

    return mutate


#: ``(what the record claims, how to write it, the rule that refuses it)``.
ILLEGAL_REPLACEMENTS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "a macro expansion that is not what the cited definition produces",
        _replacement(4, replacement="\\wrongvarepsilon"),
        "expands to",
    ),
    (
        "an expansion citing a source that was never registered",
        _replacement(4, source_id="src-absent"),
        "expansion source is not registered",
    ),
    (
        "an expansion citing a line that does not carry the definition",
        _replacement(4, source_line=2),
        "does not define",
    ),
    (
        "a DEL removal whose character is not DEL",
        _replacement(2, kind="del_removed"),
        "invalid DEL removal",
    ),
    (
        "an ANSI removal whose text is not an escape sequence",
        _replacement(1, kind="ansi_escape_removed"),
        "invalid ANSI escape removal",
    ),
    (
        "a control removal that puts text in rather than taking it out",
        _replacement(2, replacement="Q"),
        "invalid control removal",
    ),
    (
        "a backslash restoration that restores something else",
        _replacement(3, replacement="/"),
        "invalid control restoration",
    ),
    (
        "a replacement kind the contract does not define",
        _replacement(0, kind="creative_rewrite"),
        "unsupported replacement kind",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [(mutate, message) for _label, mutate, message in ILLEGAL_REPLACEMENTS],
    ids=[label for label, _mutate, _message in ILLEGAL_REPLACEMENTS],
)
def test_an_illegal_replacement_is_refused(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    refused(mutated(evidence_and_normalization_events, "writer_output_normalized", mutate), message)


def test_the_quotation_field_is_never_macro_expanded() -> None:
    """``source`` holds what was quoted; a host may not rewrite it as an expansion."""

    refused(
        mutated(
            evidence_and_normalization_events,
            "writer_output_normalized",
            _replacement(2, field="source", kind="macro_expansion"),
        ),
        "quotation field is never macro-expanded",
    )


# ---------------------------------------------------------------------------
# check_completed: one negative per hard-defect evidence kind
# ---------------------------------------------------------------------------


def _evidence(index: int, **changes: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(payload: dict[str, Any]) -> None:
        payload["evidence"][index].update(changes)

    return mutate


#: One unfounded quotation per evidence kind, plus one per way of citing a
#: source that is not there: ``(id, how to write it, the rule that refuses it)``.
UNFOUNDED_EVIDENCE: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("literature_quote-absent-text", _evidence(0, quote="nowhere in the note"), "not in registered source"),
    ("literature_quote-absent-source", _evidence(0, source_id="src-absent"), "not in frozen sources"),
    ("ancestor_quote-absent-text", _evidence(1, quote="nowhere in the step"), "ancestor evidence quote is not"),
    ("ancestor_quote-absent-source", _evidence(1, source_id="sr-absent"), "step evidence source is unknown"),
    ("scope_quote-absent-text", _evidence(2, quote="nowhere in the scope"), "scope evidence quote is not in source"),
    ("hypothesis_quote-absent-text", _evidence(3, quote="not the hypothesis"), "hypothesis evidence quote is not"),
    ("hypothesis_quote-absent-source", _evidence(3, source_id="br-absent"), "hypothesis evidence source is unknown"),
    ("task_constraint_quote-other-task", _evidence(4, source_id="task-other"), "must equal the run task id"),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [(mutate, message) for _label, mutate, message in UNFOUNDED_EVIDENCE],
    ids=[label for label, _mutate, _message in UNFOUNDED_EVIDENCE],
)
def test_unfounded_evidence_is_refused(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    refused(mutated(evidence_and_normalization_events, "check_completed", mutate), message)


def test_an_evidence_kind_the_contract_does_not_define_is_refused() -> None:
    refused(
        mutated(evidence_and_normalization_events, "check_completed", _evidence(0, kind="vibes")),
        "invalid hard-defect evidence kind",
    )


# ---------------------------------------------------------------------------
# The three candidate statuses no shipped record produces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "build"),
    [
        ("conditional", conditional_candidate_events),
        ("provisional", provisional_candidate_events),
        ("blocked", blocked_candidate_events),
    ],
)
def test_a_candidate_resolves_to_the_status_its_checks_imply(status: str, build) -> None:
    canonical = replay_events(build()).canonical
    candidate = canonical["candidates"][0]

    assert candidate["status"] == status
    assert canonical["summary"]["candidate_status_counts"][status] == 1


def test_a_conditional_candidate_names_its_unresolved_checks_and_is_judged() -> None:
    """The headline Record v1.1 change, exercised rather than described.

    Under v1 a candidate holding an objection could not be judged at all. Under
    1.1 it can, provided the record says which checks are still open — so the
    reader of a judgement can see what it was made in spite of.
    """

    canonical = replay_events(conditional_candidate_events()).canonical
    candidate = canonical["candidates"][0]

    assert candidate["unresolved_check_ids"] == ["chk-1"]
    assert canonical["judgements"][0]["verdict"] == "near_pass"


def test_a_candidate_may_not_hide_an_unresolved_check() -> None:
    refused(
        mutated(conditional_candidate_events, "candidate_declared", lambda p: p.update(unresolved_check_ids=[])),
        "omitted or altered unresolved checks",
    )


def test_a_conditional_candidate_may_be_judged_but_not_selected() -> None:
    """Judging is where 1.1 widened the rule; selection is where it did not."""

    refused(selection_of_the_conditional_candidate(), "only eligible candidate may be selected")


@pytest.mark.parametrize(
    ("status", "build"),
    [
        ("provisional", judgement_of_the_provisional_candidate),
        ("blocked", judgement_of_the_blocked_candidate),
    ],
)
def test_an_unresolved_candidate_may_not_be_judged(status: str, build) -> None:
    """``conditional`` is the only status 1.1 opened to judgement.

    A pending check and a failed instrument both mean the same thing — nobody
    knows yet — and neither may be judged past.
    """

    refused(build(), "eligible or explicitly conditional candidate")
