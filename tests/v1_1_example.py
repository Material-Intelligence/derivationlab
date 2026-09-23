"""The worked Record v1.1 log, built event by event from the contract.

The repository's other example is a Record v1 produced by a deterministic
runtime. This module builds the v1.1 counterpart in code, because no v1.1 run
could be published as it stands: the v1.1 vocabulary includes
``source_evidence_registered``, which puts third-party source text *inside* the
hash chain, where it can never be removed again. So the one record that shows
what v1.1 added has to be written rather than captured.

Writing it has a second use. The builder is a statement, in executable form, of
what each v1.1 event requires of the events around it — a Writer output that is
normalised before it is sealed, an instrument failure that a human clears before
the check may be retried, a revision the runtime declines and then one it
applies, an explicit completion of an unchanged checked route. If the verifier
and this file ever disagree, one of them is wrong and the suite says so.

What it covers: 21 of the 22 event types Record v1.1 defines — all 16 from v1
and 5 of the 6 v1.1-only types. The missing one is
``source_evidence_registered``, for the reason above; the macro-expansion
normaliser it gates is therefore also unreachable from here.

The output is deterministic: same ids, same timestamps, same hashes on every
run — with one input from outside the file. Its ``run_created`` pins the
contract and both 1.1 schemas by path and real SHA-256, because all three ship
here and a reader is told those digests are checkable. So editing
``docs/RECORD_SPEC.md`` or either schema changes this record.
``examples/runs/worked_v1_1/events.jsonl`` is this function's output,
serialised, and ``tests/test_examples.py`` pins the two together byte for byte —
which is what turns "the record has to be regenerated" into a failing test
rather than something to remember.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from derivation_agent_record.model import (
    canonical_json,
    compute_event_sha256,
    sha256_text,
    transcript_sha256,
)

EVENT_V1_1 = "derivation-agent-event-v1.1"
RUN_ID = "run-v1-1-worked-example"
BACKEND = {"name": "synthetic-record-builder", "version": "1"}

_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = "docs/RECORD_SPEC.md"
_EVENT_SCHEMA_PATH = "src/derivation_agent_record/schemas/event-v1.1.schema.json"
_CANONICAL_SCHEMA_PATH = "src/derivation_agent_record/schemas/canonical-v1.1.schema.json"

SYSTEM = {"kind": "system", "id": "record-runtime"}
MODEL = {"kind": "model", "id": "writer"}
CHECKER = {"kind": "checker", "id": "checker"}
JUDGE = {"kind": "judge", "id": "judge"}
HUMAN = {"kind": "human", "id": "operator"}

#: The step as it is finally sealed. ``derivation`` contains a LaTeX control
#: word, which is what makes the normalisation below non-trivial.
CONTENT_1: dict[str, str] = {
    "claim": "The derivative of x^3 + sin(x) is 3x^2 + cos(x).",
    "why": "The task asks for the derivative of the given elementary function.",
    "source": "Task statement: differentiate f(x) = x^3 + sin(x).",
    "derivation": (
        "Apply the power rule to x^3 and the derivative of sine: "
        "\\frac{d}{dx} x^3 = 3x^2, \\frac{d}{dx} sin(x) = cos(x)."
    ),
    "scope": "Real x. Elementary calculus only; no distributional derivatives.",
}

#: The same step as the Writer actually emitted it: transport replaced the
#: backslash of the first control word with a backspace character (U+0008).
#: This is the damage ``writer_output_normalized`` exists to repair, and the
#: repair is recorded as an offset-addressed replacement so that a reader can
#: re-apply it and get the sealed text back.
RAW_1: dict[str, str] = dict(CONTENT_1)
RAW_1["derivation"] = CONTENT_1["derivation"].replace("\\frac", "\x08frac", 1)

#: The revision: the same result, with its domain of validity stated.
CONTENT_2: dict[str, str] = dict(CONTENT_1)
CONTENT_2["claim"] = "The derivative of x^3 + sin(x) is 3x^2 + cos(x), for every real x."
CONTENT_2["scope"] = "Real x, stated in the claim. Elementary calculus only; no distributional derivatives."

_REVISION_REASON = "State the domain of validity in the claim itself."


def _digest(label: str) -> str:
    """A real digest of a real string, so no fixture carries a magic constant."""

    return sha256_text(label)


def _pin(path: str) -> dict[str, str]:
    """Pin one shipped file the way ``run_created`` does: path and real SHA-256.

    This record is written here rather than captured elsewhere, so all three of
    its pins name files that ship in this repository — and a reader who runs
    ``shasum -a 256`` on the cited path has to get the cited digest back. That
    is the whole point of the field, and it is checked by
    ``tools/verify_record_pins.py``.

    It also means this record depends on the bytes of three files: edit
    ``docs/RECORD_SPEC.md`` or either 1.1 schema and the record has to be
    regenerated. ``tests/test_examples.py`` is where that is found out.
    """

    return {"path": path, "sha256": hashlib.sha256((_ROOT / path).read_bytes()).hexdigest()}


def _commit(label: str) -> str:
    """A 40-hex value shaped like a git commit, derived rather than invented."""

    return sha256_text(label)[:40]


def _content_sha(content: dict[str, str]) -> str:
    return sha256_text(canonical_json(content))


def _control(decision: str, **extra: str) -> str:
    """The Writer's control channel: one canonical JSON object, one decision."""

    value: dict[str, Any] = {"decision": decision, "alternatives": []}
    value.update(extra)
    return canonical_json(value)


class _Log:
    """Appends events, numbering, timestamping and chaining each one."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, type_: str, actor: dict[str, str], payload: dict[str, Any]) -> None:
        seq = len(self.events) + 1
        event: dict[str, Any] = {
            "schema_version": EVENT_V1_1,
            "run_id": RUN_ID,
            "seq": seq,
            "event_id": f"evt-{seq:04d}",
            # One event a minute from a fixed start: the record has to be
            # byte-identical on every build, so nothing may read the clock.
            "recorded_at": f"2026-02-03T09:{seq:02d}:00Z",
            "type": type_,
            "actor": actor,
            "prev_event_sha256": self.events[-1]["event_sha256"] if self.events else None,
            "payload": payload,
        }
        event["event_sha256"] = compute_event_sha256(event)
        self.events.append(event)

    def model_call(self, call_id: str, *, role: str, target: dict[str, Any], actor: dict[str, str]) -> None:
        self.add(
            "model_call_started",
            actor,
            {
                "model_call_id": call_id,
                "role": role,
                "provider": "fake",
                "model": f"deterministic-{role}",
                "effort": "low",
                "target": target,
                "prompt_sha256": _digest(f"prompt {call_id}"),
            },
        )

    def finish(self, call_id: str, text: str, actor: dict[str, str]) -> None:
        self.add(
            "model_call_finished",
            actor,
            {
                "model_call_id": call_id,
                "output_text": text,
                "output_sha256": sha256_text(text),
                "body_chars": len(text),
                "finish_reason": "stop",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )

    def writer_call(
        self, call_id: str, branch_id: str, slot: int, decision: str, content: dict[str, str], **extra: str
    ) -> None:
        """A complete Writer call: start, control chunk, body chunk, finish."""

        self.model_call(call_id, role="writer", target={"branch_id": branch_id, "step_slot": slot}, actor=MODEL)
        raw = _control(decision, **extra)
        self.add(
            "model_call_chunk",
            MODEL,
            {"model_call_id": call_id, "channel": "raw", "index": 0, "text": raw, "text_sha256": sha256_text(raw)},
        )
        body = canonical_json(content)
        self.add(
            "model_call_chunk",
            MODEL,
            {"model_call_id": call_id, "channel": "body", "index": 1, "text": body, "text_sha256": sha256_text(body)},
        )
        self.finish(call_id, body, MODEL)

    def checker_call(self, call_id: str, check_id: str, verdict: str) -> None:
        self.model_call(call_id, role="checker", target={"check_id": check_id}, actor=CHECKER)
        self.finish(call_id, canonical_json({"verdict": verdict}), CHECKER)


def worked_v1_1_events() -> list[dict[str, Any]]:
    """Build the worked Record v1.1 log. Deterministic; see the module docstring."""

    log = _Log()

    log.add(
        "run_created",
        SYSTEM,
        {
            "record_spec": _pin(_SPEC_PATH),
            "event_schema": _pin(_EVENT_SCHEMA_PATH),
            "canonical_schema": _pin(_CANONICAL_SCHEMA_PATH),
            "code_commit": _commit("worked v1.1 example"),
            "task": {"id": "task-derivative", "sha256": _digest("task")},
            "pack": {"id": "pack-calculus", "sha256": _digest("pack")},
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

    log.add(
        "branch_created",
        SYSTEM,
        {
            "branch_id": "br-1",
            "parent_branch_id": None,
            "fork_mode": "root",
            "anchor_step_revision_id": None,
            "inherited_step_revision_ids": [],
            "hypothesis": {
                "text": "Differentiate term by term with the power rule and the derivative of sine.",
                "source": "model",
                "source_event_id": None,
            },
            "initial_status": "active",
            "created_reason": "root",
            "human_action_id": None,
        },
    )

    # 1. A Writer call the operator stops by hand. The record keeps the partial
    #    output and the reason; nothing it produced reaches a step.
    log.model_call("mc-0", role="writer", target={"branch_id": "br-1", "step_slot": 1}, actor=MODEL)
    log.add(
        "human_action_recorded",
        HUMAN,
        {
            "action_id": "ha-0",
            "action": "abort_model_call",
            "target": {"model_call_id": "mc-0"},
            "reason": "The prompt named the wrong function; stop it before it writes a step.",
            "content": None,
            "content_sha256": None,
        },
    )
    partial = "The derivative of x^2"
    log.add(
        "model_call_aborted",
        MODEL,
        {
            "model_call_id": "mc-0",
            "human_action_id": "ha-0",
            "partial_output_text": partial,
            "partial_output_sha256": sha256_text(partial),
            "body_chars": len(partial),
        },
    )

    # 2. The Writer call that does produce the first step — and whose raw output
    #    arrives with a control word damaged in transport.
    log.writer_call("mc-1", "br-1", 1, "continue", RAW_1)
    damaged_at = RAW_1["derivation"].index("\x08")
    log.add(
        "writer_output_normalized",
        SYSTEM,
        {
            "model_call_id": "mc-1",
            "raw_output_sha256": _content_sha(RAW_1),
            "content": CONTENT_1,
            "output_sha256": _content_sha(CONTENT_1),
            "policy": "restore-transport-damaged-control-words",
            "normalizer_version": "1",
            "replacements": [
                {
                    "field": "derivation",
                    "start": damaged_at,
                    "end": damaged_at + 1,
                    "original": "\x08",
                    "replacement": "\\",
                    "kind": "control_char_backslash",
                    "source_id": None,
                    "source_line": None,
                }
            ],
        },
    )
    log.add(
        "step_revision_sealed",
        MODEL,
        {
            "step_revision_id": "sr-1",
            "branch_id": "br-1",
            "step_slot": 1,
            "revision": 1,
            "replaces_step_revision_id": None,
            "content": CONTENT_1,
            "output_sha256": _content_sha(CONTENT_1),
            "origin": {"kind": "model", "model_call_id": "mc-1", "human_action_id": None},
        },
    )

    # 3. The check whose instrument fails. Under v1.1 the record cannot simply
    #    forget it: a human has to resume the branch, and the retry has to cite
    #    that action and come after the failure.
    log.add(
        "check_requested",
        SYSTEM,
        {
            "check_id": "chk-1",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT_1),
            "required_for_candidate": True,
            "reason": "Differentiation check on the sealed step.",
        },
    )
    log.model_call("mc-2", role="checker", target={"check_id": "chk-1"}, actor=CHECKER)
    log.add(
        "model_call_failed",
        CHECKER,
        {
            "model_call_id": "mc-2",
            "failure_kind": "transport_timeout",
            "message": "The checker process did not answer within the deadline.",
            "partial_output_text": "",
            "partial_output_sha256": sha256_text(""),
            "body_chars": 0,
            "retryable": True,
        },
    )
    log.add(
        "check_completed",
        CHECKER,
        {
            "check_id": "chk-1",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT_1),
            "checker_call_id": "mc-2",
            "verdict": "instrument_failure",
            "reason": "The checker call timed out; no verdict was produced.",
            "evidence": [],
        },
    )
    log.add(
        "branch_status_changed",
        SYSTEM,
        {
            "branch_id": "br-1",
            "from_status": "active",
            "to_status": "paused",
            "reason_code": "runtime_failure",
            "human_action_id": None,
            "check_id": "chk-1",
            "model_call_id": "mc-2",
        },
    )
    log.add(
        "human_action_recorded",
        HUMAN,
        {
            "action_id": "ha-1",
            "action": "resume_branch",
            "target": {"branch_id": "br-1"},
            "reason": "The timeout was an infrastructure fault; the checker answers again.",
            "content": None,
            "content_sha256": None,
        },
    )
    log.add("check_retry_authorized", SYSTEM, {"check_id": "chk-1", "human_action_id": "ha-1"})
    log.add(
        "branch_status_changed",
        HUMAN,
        {
            "branch_id": "br-1",
            "from_status": "paused",
            "to_status": "active",
            "reason_code": "manual_reopen",
            "human_action_id": "ha-1",
            "check_id": None,
            "model_call_id": None,
        },
    )

    # 4. The retry, which returns a real verdict: an objection, quoting the step.
    log.add(
        "check_requested",
        SYSTEM,
        {
            "check_id": "chk-2",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT_1),
            "required_for_candidate": True,
            "reason": "Retry of the differentiation check after the instrument failure.",
        },
    )
    log.checker_call("mc-3", "chk-2", "objection")
    log.add(
        "check_completed",
        CHECKER,
        {
            "check_id": "chk-2",
            "target_step_revision_id": "sr-1",
            "target_output_sha256": _content_sha(CONTENT_1),
            "checker_call_id": "mc-3",
            "verdict": "objection",
            "reason": "The result is right but the claim does not say on what domain it holds.",
            "evidence": [{"kind": "scope_quote", "source_id": "sr-1", "quote": "Elementary calculus only"}],
        },
    )

    # 5. A revision the runtime declines to apply, and then one it applies. Both
    #    are model-authored: neither invents a human action to carry the edit.
    log.writer_call("mc-4", "br-1", 2, "revise", CONTENT_2, revise_step_revision_id="sr-1", reason=_REVISION_REASON)
    log.add(
        "model_revision_deferred",
        SYSTEM,
        {
            "model_call_id": "mc-4",
            "target_step_revision_id": "sr-1",
            "reason": "The local repair budget for this route was already spent; recorded, not applied.",
        },
    )
    log.writer_call("mc-5", "br-1", 2, "revise", CONTENT_2, revise_step_revision_id="sr-1", reason=_REVISION_REASON)
    log.add(
        "model_revision_applied",
        MODEL,
        {
            "parent_branch_id": "br-1",
            "branch_id": "br-2",
            "target_step_revision_id": "sr-1",
            "step_revision_id": "sr-2",
            "model_call_id": "mc-5",
            "reason": _REVISION_REASON,
            "content": CONTENT_2,
        },
    )

    # 6. The revised route is checked, completed explicitly, judged and selected.
    log.add(
        "check_requested",
        SYSTEM,
        {
            "check_id": "chk-3",
            "target_step_revision_id": "sr-2",
            "target_output_sha256": _content_sha(CONTENT_2),
            "required_for_candidate": True,
            "reason": "Differentiation check on the revised step.",
        },
    )
    log.checker_call("mc-6", "chk-3", "ok")
    log.add(
        "check_completed",
        CHECKER,
        {
            "check_id": "chk-3",
            "target_step_revision_id": "sr-2",
            "target_output_sha256": _content_sha(CONTENT_2),
            "checker_call_id": "mc-6",
            "verdict": "ok",
            "reason": "The derivative is correct and the domain is now stated in the claim.",
            "evidence": [],
        },
    )
    log.writer_call("mc-7", "br-2", 2, "complete", CONTENT_2)
    log.add(
        "writer_route_completion",
        MODEL,
        {"branch_id": "br-2", "model_call_id": "mc-7", "tip_step_revision_id": "sr-2"},
    )

    transcript = transcript_sha256(["sr-2"], {"sr-2": {"output_sha256": _content_sha(CONTENT_2)}})
    log.add(
        "candidate_declared",
        MODEL,
        {
            "candidate_id": "cand-1",
            "branch_id": "br-2",
            "tip_step_revision_id": "sr-2",
            "transcript_step_revision_ids": ["sr-2"],
            "transcript_sha256": transcript,
            "required_check_ids": ["chk-3"],
            "unresolved_check_ids": [],
            "declared_by": "writer",
            "reason": "The checked route states both the derivative and the domain it holds on.",
        },
    )
    log.add(
        "judgement_requested",
        SYSTEM,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": transcript,
            "requested_by": "system",
            "reason": "One eligible candidate on a completed route.",
        },
    )
    log.model_call("mc-8", role="judge", target={"judgement_id": "jdg-1"}, actor=JUDGE)
    log.finish("mc-8", canonical_json({"verdict": "pass"}), JUDGE)
    log.add(
        "judgement_completed",
        JUDGE,
        {
            "judgement_id": "jdg-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": transcript,
            "judge_call_id": "mc-8",
            "verdict": "pass",
            "reason": "The derivative is correct and the scope statement matches the claim.",
            "score": 1,
        },
    )
    log.add(
        "selection_recorded",
        SYSTEM,
        {
            "selection_id": "sel-1",
            "candidate_id": "cand-1",
            "candidate_transcript_sha256": transcript,
            "judgement_id": "jdg-1",
            "reason": "The only eligible candidate, and its judgement passed.",
            "human_action_id": None,
        },
    )
    return log.events
