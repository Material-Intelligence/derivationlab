"""Immutable three-layer Intake handoff for one confirmed derivation Run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .intake_session import (
    ConversationEvent,
    IntakeSession,
    IntakeSessionStatus,
    intake_session_payload,
    problem_specification_payload,
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _event_payload(event: ConversationEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "payload": dict(event.payload),
    }


@dataclass(frozen=True)
class IntakeHandoffBundle:
    session_id: str
    run_id: str
    files: Mapping[str, bytes]
    manifest: Mapping[str, Any]


def build_intake_handoff(
    session: IntakeSession,
    events: tuple[ConversationEvent, ...],
    *,
    run_id: str,
) -> IntakeHandoffBundle:
    """Build deterministic exports without introducing a second writable truth."""

    if session.status is not IntakeSessionStatus.CONFIRMED:
        raise ValueError("only a confirmed IntakeSession can produce a handoff")
    if not run_id.strip():
        raise ValueError("run_id must be non-empty")
    if not events or events[-1].kind != "session_confirmed":
        raise ValueError(
            "confirmed Intake handoff requires a terminal confirmation event"
        )
    confirmation_events = tuple(
        event for event in events if event.kind == "session_confirmed"
    )
    if len(confirmation_events) != 1:
        raise ValueError(
            "confirmed Intake handoff requires exactly one confirmation event"
        )
    confirmation_event = confirmation_events[0]
    if confirmation_event.event_id != events[-1].event_id:
        raise ValueError(
            "the confirmation event must terminate the Conversation Archive"
        )
    confirmed_version = session.problem_specifications[-1].version
    if (
        confirmation_event.payload.get("problem_specification_version")
        != confirmed_version
    ):
        raise ValueError("confirmation event does not bind the confirmed specification")
    specification = problem_specification_payload(session.problem_specifications[-1])
    decision_log = [item for item in intake_session_payload(session)["decisions"]]
    conversation_lines = b"".join(
        _json_bytes(_event_payload(event)) for event in events
    )
    content_files = {
        "problem_specification.json": _json_bytes(specification),
        "decision_log.json": _json_bytes(decision_log),
        "conversation.jsonl": conversation_lines,
    }
    hashes = {name: _sha256(content) for name, content in content_files.items()}
    specification_record = session.problem_specifications[-1]
    manifest = {
        # v3 only adds keys to v2: every field a v2 reader knows keeps its
        # name, position and meaning.
        "schema_version": "intake-handoff-v3",
        "intake_session_id": session.session_id,
        "intake_revision": session.revision,
        "run_id": run_id.strip(),
        "problem_specification_version": confirmed_version,
        "problem_specification": {
            "version": confirmed_version,
            "sha256": hashes["problem_specification.json"],
            "declared_default_ids": [
                item.default_id for item in specification_record.declared_defaults
            ],
            "refinement_ladder_rungs": len(specification_record.refinement_ladder),
        },
        "convergence": {
            "rounds": session.convergence.rounds,
            "audit_rejections": session.convergence.audit_rejections,
            "reason": session.convergence.reason,
            "finalized_by_user": session.convergence.finalized_by_user,
        },
        "decision_log": {
            "revision": session.revision,
            "latest_decision_revisions": {
                decision_id: revision
                for decision_id, revision in sorted(
                    {
                        item.decision_id: item.revision for item in session.decisions
                    }.items()
                )
            },
            "sha256": hashes["decision_log.json"],
        },
        "conversation_archive": {
            "sha256": hashes["conversation.jsonl"],
            "terminal_event": {
                "event_id": events[-1].event_id,
                "kind": events[-1].kind,
                "sha256": _sha256(_json_bytes(_event_payload(events[-1]))),
            },
            "confirmation_event_id": confirmation_event.event_id,
        },
        "thread_generations": [
            {
                "generation": item.generation,
                "thread_id": item.app_server_thread_id,
                "status": item.status.value,
                "replaced_generation": item.replaced_generation,
                "replacement_reason": item.replacement_reason,
            }
            for item in session.thread_generations
        ],
        "files": hashes,
    }
    files = {
        **content_files,
        "handoff_manifest.json": _json_bytes(manifest),
    }
    return IntakeHandoffBundle(
        session_id=session.session_id,
        run_id=run_id.strip(),
        files=files,
        manifest=manifest,
    )
