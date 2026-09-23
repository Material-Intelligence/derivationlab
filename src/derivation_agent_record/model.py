"""Shared types, constants, and hashing helpers for Derivation Agent Record V1."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

EVENT_SCHEMA_VERSION = "derivation-agent-event-v1"
CANONICAL_SCHEMA_VERSION = "derivation-agent-canonical-v1"
EVENT_SCHEMA_VERSION_1_1 = "derivation-agent-event-v1.1"
CANONICAL_SCHEMA_VERSION_1_1 = "derivation-agent-canonical-v1.1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

EVENT_TYPES = {
    "run_created",
    "human_action_recorded",
    "branch_created",
    "branch_status_changed",
    "model_call_started",
    "model_call_chunk",
    "model_call_finished",
    "model_call_failed",
    "model_call_aborted",
    "step_revision_sealed",
    "check_requested",
    "check_completed",
    "candidate_declared",
    "judgement_requested",
    "judgement_completed",
    "selection_recorded",
}

ACTOR_KINDS = {"system", "model", "checker", "judge", "human"}
BRANCH_STATUSES = {"active", "paused", "parked", "completed", "killed"}
BRANCH_TRANSITIONS = {
    "active": {"paused", "parked", "completed", "killed"},
    "paused": {"active", "parked", "killed"},
    "parked": {"active", "paused", "killed"},
    "completed": {"active", "killed"},
    "killed": set(),
}
CALL_TERMINAL_STATES = {"finished", "failed", "aborted"}
CHECK_VERDICTS = {"ok", "objection", "hard_defect", "instrument_failure"}
JUDGEMENT_VERDICTS = {"pass", "near_pass", "fail", "instrument_failure"}
HARD_DEFECT_EVIDENCE_KINDS = {
    "ancestor_quote",
    "hypothesis_quote",
    "scope_quote",
    "task_constraint_quote",
}
HUMAN_ACTIONS = {
    "pause_branch",
    "resume_branch",
    "kill_branch",
    "revise_step",
    "set_direction",
    "set_hypothesis",
    "abort_model_call",
    "select_candidate",
}


class ContractError(ValueError):
    """Raised when a record violates the V1 contract."""


def canonical_json(value: Any) -> str:
    """Return the byte-stable UTF-8 JSON representation used by all hashes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ancestor_evidence_text(content: Mapping[str, str], *, record_version: str) -> str:
    """Use the same decoded field text that the 1.1 transcript tools expose.

    The stable labels only separate fields. Quotes, backslashes, Unicode and
    newlines inside each field remain unchanged; serialization is not evidence.
    Frozen 1.0 retains its historical canonical-JSON source representation.
    """
    if record_version == "1.0":
        return canonical_json(content)
    if record_version != "1.1":
        raise ValueError("unsupported ancestor evidence version")
    return "\n\n".join(
        f"{field}:\n{content[field]}"
        for field in ("claim", "why", "source", "derivation", "scope")
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def compute_event_sha256(event: Mapping[str, Any]) -> str:
    """Hash an event: canonical JSON of every field except ``event_sha256``.

    The shallow copy is deliberate. Nothing here mutates the event — the only
    thing done with ``body`` is serialization — and this runs once per event on
    the verifier's hot path, where deep-copying every payload was measurably a
    third of replay time on a large record. ``tests/test_record_contract.py``
    pins the fact that the caller's mapping comes back unchanged.
    """

    body = {key: value for key, value in event.items() if key != "event_sha256"}
    return sha256_json(body)


def transcript_sha256(step_revision_ids: Sequence[str], steps: Mapping[str, Mapping[str, Any]]) -> str:
    material = [
        {
            "step_revision_id": step_id,
            "output_sha256": steps[step_id]["output_sha256"],
        }
        for step_id in step_revision_ids
    ]
    return sha256_json(material)


def load_events(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ContractError(f"line {line_number}: invalid JSON: {exc}") from exc
            require(isinstance(event, dict), f"line {line_number}: event must be an object")
            events.append(event)
    require(events, "event log is empty")
    return events


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def require_exact_keys(mapping: Mapping[str, Any], expected: Iterable[str], context: str) -> None:
    expected_set = set(expected)
    actual_set = set(mapping)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    require(not missing and not extra, f"{context}: missing={missing}, extra={extra}")


def require_id(value: Any, context: str) -> str:
    require(isinstance(value, str) and bool(ID_RE.fullmatch(value)), f"{context}: invalid id {value!r}")
    return value


def require_sha(value: Any, context: str) -> str:
    require(isinstance(value, str) and bool(SHA256_RE.fullmatch(value)), f"{context}: invalid sha256")
    return value


def require_commit(value: Any, context: str) -> str:
    require(isinstance(value, str) and bool(COMMIT_RE.fullmatch(value)), f"{context}: invalid git commit")
    return value


def require_text(value: Any, context: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{context}: expected non-empty text")
    return value


def clone(value: Any) -> Any:
    return copy.deepcopy(value)
