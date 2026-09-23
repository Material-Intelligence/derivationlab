"""Record v1.1 builders for the parts of the verifier no shipped record reaches.

Two blocks of the replay engine are unreachable from anything under
``examples/runs/``, for two different reasons, and both are load-bearing.

**Registered source evidence and the normalisation it gates.** A record carrying
``source_evidence_registered`` puts third-party text inside the hash chain,
where it can never be removed again, so no record here may be *published* with
it — ``tests/test_examples.py`` enforces that on ``examples/runs/``. The ban is
on publication, not on existence: a record built in memory by the suite costs
nothing in publishable surface. Without one, the whole
``_on_source_evidence_registered`` handler goes untested, and so does the
``macro_expansion`` replacement — the one normalisation kind that can put
arbitrary text where sealed scientific content was, which is exactly the shape
of the attack ``macro_expansion.py`` exists to refuse. That module was unit
tested; its use by the verifier was not.

**Candidate statuses other than ``eligible``.** Every shipped record settles on
an eligible candidate, so ``provisional``, ``blocked`` and ``conditional`` were
produced by nothing — including ``conditional``, the headline Record v1.1
change, together with the judgement-eligibility widening that goes with it.

Every builder here returns a record the verifier must accept. The tests in
``tests/test_v1_1_coverage.py`` then break each one a rule at a time.
"""

from __future__ import annotations

from typing import Any

from v1_1_example import (
    _CANONICAL_SCHEMA_PATH,
    _EVENT_SCHEMA_PATH,
    _SPEC_PATH,
    BACKEND,
    CHECKER,
    JUDGE,
    MODEL,
    SYSTEM,
    _commit,
    _content_sha,
    _digest,
    _Log,
    _pin,
)

from derivation_agent_record.model import canonical_json, transcript_sha256

TASK_ID = "task-sphere"
SOURCE_ID = "src-note"

#: The registered source: one readable macro definition on line 1, and one
#: quotable sentence on line 2. Both are inside the hash chain the moment this
#: is registered, which is why a record like this one is never published.
SOURCE_TEXT = "\\newcommand{\\eps}{\\varepsilon}\nThe permittivity of free space is a constant throughout this note.\n"

HYPOTHESIS = "Use the divergence theorem on a concentric spherical surface."

#: The Writer's output exactly as it arrived, carrying one instance of every
#: kind of damage a recorded normalisation is allowed to repair:
#:
#: * a terminal colour code that survived transport (``claim``);
#: * a DEL byte in front of a control word (``why``);
#: * a C0 control character in front of a control word (``source``);
#: * a control word whose backslash arrived as a backspace (``derivation``);
#: * a manuscript macro that the host expanded from the registered source
#:   (``derivation``) — the only replacement whose text is not fixed by its own
#:   kind, and therefore the only one replay has to re-derive.
RAW: dict[str, str] = {
    "claim": "\x1b[31mOutside the sphere the field is that of a point charge at its centre.",
    "why": "The enclosed charge is the whole charge, so \x7f\\nabla acts on a radial field.",
    "source": "The note states the field of \x01\\rho(r) inside a uniformly charged sphere.",
    "derivation": "By the divergence theorem, 4 pi r^2 E(r) = Q / \\eps_0, so E(r) = \x08frac{Q}{4 pi r^2}.",
    "scope": "Static charge, uniform density, and a strictly spherical body.",
}

#: ``(field, original, replacement, kind, source_id, source_line)``, in the order
#: the record applies them. Offsets are derived below rather than written down,
#: because each edit is addressed against the field as it stands when that edit
#: is applied — which is how replay re-applies them.
_EDITS: tuple[tuple[str, str, str, str, str | None, int | None], ...] = (
    ("claim", "\x1b[31m", "", "ansi_escape_removed", None, None),
    ("why", "\x7f", "", "del_removed", None, None),
    ("source", "\x01", "", "control_char_removed", None, None),
    ("derivation", "\x08", "\\", "control_char_backslash", None, None),
    ("derivation", "\\eps", "\\varepsilon", "macro_expansion", SOURCE_ID, 1),
)


def _apply_edits() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The replacement list as the record carries it, and the text it produces."""

    text = dict(RAW)
    items: list[dict[str, Any]] = []
    for field, original, replacement, kind, source_id, line in _EDITS:
        start = text[field].index(original)
        items.append(
            {
                "field": field,
                "start": start,
                "end": start + len(original),
                "original": original,
                "replacement": replacement,
                "kind": kind,
                "source_id": source_id,
                "source_line": line,
            }
        )
        text[field] = text[field][:start] + replacement + text[field][start + len(original) :]
    return items, text


REPLACEMENTS, CONTENT = _apply_edits()

#: One item per hard-defect evidence kind the contract defines. Three of the
#: five were reached by no test before this record existed.
EVIDENCE: list[dict[str, str]] = [
    {"kind": "literature_quote", "source_id": SOURCE_ID, "quote": "permittivity of free space"},
    {"kind": "ancestor_quote", "source_id": "sr-1", "quote": "the field is that of a point charge"},
    {"kind": "scope_quote", "source_id": "sr-1", "quote": "strictly spherical body"},
    {"kind": "hypothesis_quote", "source_id": "br-1", "quote": "concentric spherical surface"},
    {"kind": "task_constraint_quote", "source_id": TASK_ID, "quote": "derive the exterior field"},
]

TRANSCRIPT_SHA256 = transcript_sha256(["sr-1"], {"sr-1": {"output_sha256": _content_sha(CONTENT)}})


# ---------------------------------------------------------------------------
# Pieces every record below shares
# ---------------------------------------------------------------------------


def _run_created(log: _Log, run_id: str) -> None:
    log.add(
        "run_created",
        SYSTEM,
        {
            "record_spec": _pin(_SPEC_PATH),
            "event_schema": _pin(_EVENT_SCHEMA_PATH),
            "canonical_schema": _pin(_CANONICAL_SCHEMA_PATH),
            "code_commit": _commit(run_id),
            "task": {"id": TASK_ID, "sha256": _digest("task")},
            "pack": {"id": "pack-electrostatics", "sha256": _digest("pack")},
            "configuration": {
                "granularity": "one_claim",
                "max_active_branches": 2,
                "max_model_calls": None,
                "checker_enabled": True,
                "max_local_repairs": 1,
                "models": {
                    role: {"provider": "fake", "model": f"deterministic-{role}", "effort": "low"}
                    for role in ("writer", "checker", "judge")
                },
                "backend": BACKEND,
            },
            "input_policy": {"reference_allowed": False, "allowed_paths": []},
        },
    )


def _root_branch(log: _Log) -> None:
    log.add(
        "branch_created",
        SYSTEM,
        {
            "branch_id": "br-1",
            "parent_branch_id": None,
            "fork_mode": "root",
            "anchor_step_revision_id": None,
            "inherited_step_revision_ids": [],
            "hypothesis": {"text": HYPOTHESIS, "source": "model", "source_event_id": None},
            "initial_status": "active",
            "created_reason": "root",
            "human_action_id": None,
        },
    )


def _seal(log: _Log, *, call_id: str) -> None:
    log.add(
        "step_revision_sealed",
        MODEL,
        {
            "step_revision_id": "sr-1",
            "branch_id": "br-1",
            "step_slot": 1,
            "revision": 1,
            "replaces_step_revision_id": None,
            "content": CONTENT,
            "output_sha256": _content_sha(CONTENT),
            "origin": {"kind": "model", "model_call_id": call_id, "human_action_id": None},
        },
    )


def _request_check(log: _Log) -> None:
    log.add(
        "check_requested",
        SYSTEM,
        {
            "check_id": "chk-1",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT),
            "required_for_candidate": True,
            "reason": "Required check on the sealed step.",
        },
    )


def _complete_check(log: _Log, *, call_id: str, verdict: str, reason: str, evidence: list[dict[str, str]]) -> None:
    log.add(
        "check_completed",
        CHECKER,
        {
            "check_id": "chk-1",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT),
            "checker_call_id": call_id,
            "verdict": verdict,
            "reason": reason,
            "evidence": evidence,
        },
    )


# ---------------------------------------------------------------------------
# Registered source evidence, every replacement kind, every evidence kind
# ---------------------------------------------------------------------------


def evidence_and_normalization_events() -> list[dict[str, Any]]:
    """Registers a source, normalises with every replacement kind, and completes
    one check carrying every hard-defect evidence kind."""

    log = _Log()
    _run_created(log, "run-v1-1-evidence")
    log.add(
        "source_evidence_registered",
        SYSTEM,
        {"source_id": SOURCE_ID, "kind": "literature_quote", "text": SOURCE_TEXT, "sha256": _digest(SOURCE_TEXT)},
    )
    _root_branch(log)

    log.writer_call("mc-1", "br-1", 1, "continue", RAW)
    log.add(
        "writer_output_normalized",
        SYSTEM,
        {
            "model_call_id": "mc-1",
            "raw_output_sha256": _content_sha(RAW),
            "content": CONTENT,
            "output_sha256": _content_sha(CONTENT),
            "policy": "restore-transport-damage-and-expand-cited-macros",
            "normalizer_version": "1",
            "replacements": REPLACEMENTS,
        },
    )
    _seal(log, call_id="mc-1")

    _request_check(log)
    log.checker_call("mc-2", "chk-1", "hard_defect")
    _complete_check(
        log,
        call_id="mc-2",
        verdict="hard_defect",
        reason="The step asserts the exterior field without establishing spherical symmetry.",
        evidence=EVIDENCE,
    )
    return log.events


# ---------------------------------------------------------------------------
# The three candidate statuses no shipped record produces
# ---------------------------------------------------------------------------


def _up_to_the_requested_check(run_id: str) -> _Log:
    """Run, branch, one sealed step, one required check — no verdict yet."""

    log = _Log()
    _run_created(log, run_id)
    _root_branch(log)
    log.writer_call("mc-1", "br-1", 1, "continue", CONTENT)
    _seal(log, call_id="mc-1")
    _request_check(log)
    return log


def _complete_route_and_declare(log: _Log, *, unresolved: list[str]) -> None:
    log.add(
        "branch_status_changed",
        MODEL,
        {
            "branch_id": "br-1",
            "from_status": "active",
            "to_status": "completed",
            "reason_code": "writer_complete",
            "human_action_id": None,
            "check_id": None,
            "model_call_id": None,
        },
    )
    log.add(
        "candidate_declared",
        MODEL,
        {
            "candidate_id": "cand-1",
            "branch_id": "br-1",
            "tip_step_revision_id": "sr-1",
            "transcript_step_revision_ids": ["sr-1"],
            "transcript_sha256": TRANSCRIPT_SHA256,
            "required_check_ids": ["chk-1"],
            "unresolved_check_ids": unresolved,
            "declared_by": "writer",
            "reason": "The route is complete; anything outstanding is named rather than hidden.",
        },
    )


def _judge(log: _Log, *, call_id: str) -> None:
    log.add(
        "judgement_requested",
        SYSTEM,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": TRANSCRIPT_SHA256,
            "requested_by": "system",
            "reason": "One candidate on a completed route.",
        },
    )
    log.model_call(call_id, role="judge", target={"judgement_id": "jdg-1"}, actor=JUDGE)
    log.finish(call_id, canonical_json({"verdict": "near_pass"}), JUDGE)
    log.add(
        "judgement_completed",
        JUDGE,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": TRANSCRIPT_SHA256,
            "judge_call_id": call_id,
            "verdict": "near_pass",
            "reason": "The derivation holds, but the open objection is not a matter of taste.",
            "score": 0.5,
        },
    )


def conditional_candidate_events() -> list[dict[str, Any]]:
    """A required check objects, so the candidate is ``conditional``: judged,
    with the unresolved check named in the record, and not selectable."""

    log = _up_to_the_requested_check("run-v1-1-conditional")
    log.checker_call("mc-2", "chk-1", "objection")
    _complete_check(
        log,
        call_id="mc-2",
        verdict="objection",
        reason="The scope does not say whether the charge density has to be static.",
        evidence=[],
    )
    _complete_route_and_declare(log, unresolved=["chk-1"])
    _judge(log, call_id="mc-3")
    return log.events


def selection_of_the_conditional_candidate() -> list[dict[str, Any]]:
    """The same record with a selection appended. It must be refused."""

    log = _Log()
    log.events = conditional_candidate_events()
    log.add(
        "selection_recorded",
        SYSTEM,
        {
            "selection_id": "sel-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": TRANSCRIPT_SHA256,
            "judgement_id": "jdg-1",
            "reason": "Ship it anyway; the objection can be answered in review.",
            "human_action_id": None,
        },
    )
    return log.events


def provisional_candidate_events() -> list[dict[str, Any]]:
    """A required check is still ``requested`` when the record ends, so the
    candidate is ``provisional``."""

    log = _up_to_the_requested_check("run-v1-1-provisional")
    _complete_route_and_declare(log, unresolved=[])
    return log.events


def judgement_of_the_provisional_candidate() -> list[dict[str, Any]]:
    """The same record with a judgement requested. It must be refused."""

    log = _Log()
    log.events = provisional_candidate_events()
    log.add(
        "judgement_requested",
        SYSTEM,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": TRANSCRIPT_SHA256,
            "requested_by": "system",
            "reason": "Judge it now and read the checker's answer later.",
        },
    )
    return log.events


def blocked_candidate_events() -> list[dict[str, Any]]:
    """A required check ended in ``instrument_failure`` that no human cleared,
    so the candidate is ``blocked``."""

    log = _up_to_the_requested_check("run-v1-1-blocked")
    log.model_call("mc-2", role="checker", target={"check_id": "chk-1"}, actor=CHECKER)
    log.add(
        "model_call_failed",
        CHECKER,
        {
            "model_call_id": "mc-2",
            "failure_kind": "transport_timeout",
            "message": "The checker process did not answer within the deadline.",
            "partial_output_text": "",
            "partial_output_sha256": _digest(""),
            "body_chars": 0,
            "retryable": True,
        },
    )
    _complete_check(
        log,
        call_id="mc-2",
        verdict="instrument_failure",
        reason="The checker call timed out; no verdict was produced.",
        evidence=[],
    )
    _complete_route_and_declare(log, unresolved=[])
    return log.events


def judgement_of_the_blocked_candidate() -> list[dict[str, Any]]:
    """The same record with a judgement requested. It must be refused."""

    log = _Log()
    log.events = blocked_candidate_events()
    log.add(
        "judgement_requested",
        SYSTEM,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": TRANSCRIPT_SHA256,
            "requested_by": "system",
            "reason": "Judge it and treat the instrument failure as noise.",
        },
    )
    return log.events
