"""Append-only Record V1 writer with checksum and transition validation."""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from derivation_agent_record import (
    EVENT_SCHEMA_VERSION,
    ReplayEngine,
    canonical_json,
    compute_event_sha256,
    load_events,
    replay_events,
    sha256_text,
    transcript_sha256,
)
from derivation_agent_record.model import EVENT_SCHEMA_VERSION_1_1

from .types import (
    CheckOutput,
    JudgeOutput,
    ModelRole,
    RunConfig,
    RuntimeInvariantError,
    StepContent,
    WriterControl,
)

Actor = Mapping[str, str]
Clock = Callable[[], str]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _open_replay(events: Sequence[Mapping[str, Any]]) -> ReplayEngine:
    """Replay a valid prefix without applying Record V1 end-of-log checks.

    Record V1 correctly rejects an in-flight call as a *complete* record.  A
    live runtime nevertheless needs to validate and reconcile the temporary
    prefix between model_call_started and its terminal event.  This helper uses
    the same envelope and transition handlers, omitting only finalization.
    """

    engine = ReplayEngine()
    for expected_seq, raw_event in enumerate(events, start=1):
        event = copy.deepcopy(dict(raw_event))
        engine._validate_envelope(event, expected_seq)  # type: ignore[attr-defined]
        handler = getattr(engine, f"_on_{event['type']}")
        handler(event)
        engine.events.append(event)
        engine.previous_event_sha256 = event["event_sha256"]
    return engine


class EventLogWriter:
    """Single-process append writer for one Record V1 JSONL stream."""

    def __init__(
        self, path: str | Path, run_id: str, *, clock: Clock = utc_now
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.clock = clock
        self._lock = threading.RLock()
        if self.path.exists() and self.path.stat().st_size:
            self._events = load_events(self.path)
            _open_replay(self._events)
            if self._events[0]["run_id"] != run_id:
                raise RuntimeInvariantError("record run_id differs from writer run_id")
        else:
            self._events: list[dict[str, Any]] = []
        self.schema_version = (
            self._events[0]["schema_version"] if self._events else EVENT_SCHEMA_VERSION
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._events)

    @property
    def head(self) -> tuple[int, str | None]:
        if not self._events:
            return (0, None)
        return (len(self._events), self._events[-1]["event_sha256"])

    def append(
        self, event_type: str, actor: Actor, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            seq = len(self._events) + 1
            event: dict[str, Any] = {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "seq": seq,
                "event_id": f"evt_{seq:08d}",
                "recorded_at": self.clock(),
                "type": event_type,
                "actor": dict(actor),
                "prev_event_sha256": self._events[-1]["event_sha256"]
                if self._events
                else None,
                "payload": copy.deepcopy(dict(payload)),
            }
            event["event_sha256"] = compute_event_sha256(event)
            proposed = [*self._events, event]
            _open_replay(proposed)

            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = canonical_json(event) + "\n"
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(event)
            return copy.deepcopy(event)

    def snapshot(self) -> dict[str, Any]:
        if not self._events:
            raise RuntimeInvariantError("record has not been initialized")
        engine = _open_replay(self._events)
        return engine._canonical_state()  # type: ignore[attr-defined]

    def verify_complete(self) -> dict[str, Any]:
        return replay_events(self._events).canonical


class RecordV1Writer:
    """Semantic convenience layer that emits only frozen Record V1 events."""

    SYSTEM: ClassVar[dict[str, str]] = {"kind": "system", "id": "record-runtime"}
    ACTORS: ClassVar[dict[ModelRole, dict[str, str]]] = {
        ModelRole.WRITER: {"kind": "model", "id": "runtime:writer"},
        ModelRole.CHECKER: {"kind": "checker", "id": "runtime:checker"},
        ModelRole.JUDGE: {"kind": "judge", "id": "runtime:judge"},
    }

    def __init__(self, event_log: EventLogWriter, config: RunConfig) -> None:
        if event_log.run_id != config.run_id:
            raise RuntimeInvariantError("event writer and RunConfig disagree on run_id")
        self.log = event_log
        self.config = config
        version = (
            EVENT_SCHEMA_VERSION_1_1
            if config.record_version == "1.1"
            else EVENT_SCHEMA_VERSION
        )
        if event_log.events and event_log.schema_version != version:
            raise RuntimeInvariantError(
                "record version differs from frozen configuration"
            )
        event_log.schema_version = version

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.log.events

    @property
    def head(self) -> tuple[int, str | None]:
        return self.log.head

    def snapshot(self) -> dict[str, Any]:
        return self.log.snapshot()

    def verify_complete(self) -> dict[str, Any]:
        return self.log.verify_complete()

    def create_run(self) -> dict[str, Any]:
        return self.log.append(
            "run_created",
            self.SYSTEM,
            {
                "record_spec": self.config.record_spec.to_record(),
                "event_schema": self.config.event_schema.to_record(),
                "canonical_schema": self.config.canonical_schema.to_record(),
                "code_commit": self.config.code_commit,
                "task": self.config.task.to_record(),
                "pack": self.config.pack.to_record(),
                "configuration": self.config.record_configuration(),
                "input_policy": self.config.input_policy.to_record(),
            },
        )

    def create_root_branch(self, branch_id: str, hypothesis: str) -> dict[str, Any]:
        return self.log.append(
            "branch_created",
            self.SYSTEM,
            {
                "branch_id": branch_id,
                "parent_branch_id": None,
                "fork_mode": "root",
                "anchor_step_revision_id": None,
                "inherited_step_revision_ids": [],
                "hypothesis": {
                    "text": hypothesis,
                    "source": "inherited",
                    "source_event_id": None,
                },
                "initial_status": "active",
                "created_reason": "root",
                "human_action_id": None,
            },
        )

    def register_source_evidence(self, source_id: str, text: str) -> dict[str, Any]:
        return self.log.append(
            "source_evidence_registered",
            self.SYSTEM,
            {
                "source_id": source_id,
                "kind": "literature_quote",
                "text": text,
                "sha256": sha256_text(text),
            },
        )

    def authorize_check_retry(
        self, check_id: str, human_action_id: str
    ) -> dict[str, Any]:
        return self.log.append(
            "check_retry_authorized",
            self.SYSTEM,
            {
                "check_id": check_id,
                "human_action_id": human_action_id,
            },
        )

    def complete_unchanged_route(
        self, branch_id: str, model_call_id: str, tip_step_revision_id: str
    ) -> dict[str, Any]:
        return self.log.append(
            "writer_route_completion",
            self.ACTORS[ModelRole.WRITER],
            {
                "branch_id": branch_id,
                "model_call_id": model_call_id,
                "tip_step_revision_id": tip_step_revision_id,
            },
        )

    def record_human_action(
        self,
        *,
        actor_id: str,
        action_id: str,
        action: str,
        target: Mapping[str, str],
        reason: str,
        content: str | None,
    ) -> dict[str, Any]:
        return self.log.append(
            "human_action_recorded",
            {"kind": "human", "id": actor_id},
            {
                "action_id": action_id,
                "action": action,
                "target": dict(target),
                "reason": reason,
                "content": content,
                "content_sha256": sha256_text(content) if content is not None else None,
            },
        )

    def create_child_branch(
        self,
        *,
        branch_id: str,
        parent_branch_id: str,
        fork_mode: str,
        anchor_step_revision_id: str,
        inherited_step_revision_ids: Sequence[str],
        hypothesis: str,
        hypothesis_source: str,
        hypothesis_source_event_id: str,
        created_reason: str,
        human_action_id: str | None,
        actor: Actor,
    ) -> dict[str, Any]:
        return self.log.append(
            "branch_created",
            actor,
            {
                "branch_id": branch_id,
                "parent_branch_id": parent_branch_id,
                "fork_mode": fork_mode,
                "anchor_step_revision_id": anchor_step_revision_id,
                "inherited_step_revision_ids": list(inherited_step_revision_ids),
                "hypothesis": {
                    "text": hypothesis,
                    "source": hypothesis_source,
                    "source_event_id": hypothesis_source_event_id,
                },
                "initial_status": "active",
                "created_reason": created_reason,
                "human_action_id": human_action_id,
            },
        )

    def change_branch_status(
        self,
        *,
        branch_id: str,
        from_status: str,
        to_status: str,
        reason_code: str,
        actor: Actor,
        human_action_id: str | None = None,
        check_id: str | None = None,
        model_call_id: str | None = None,
    ) -> dict[str, Any]:
        return self.log.append(
            "branch_status_changed",
            actor,
            {
                "branch_id": branch_id,
                "from_status": from_status,
                "to_status": to_status,
                "reason_code": reason_code,
                "human_action_id": human_action_id,
                "check_id": check_id,
                "model_call_id": model_call_id,
            },
        )

    def start_model_call(
        self,
        *,
        model_call_id: str,
        role: ModelRole,
        target: Mapping[str, Any],
        prompt: str,
    ) -> dict[str, Any]:
        model = self.config.model_for(role)
        return self.log.append(
            "model_call_started",
            self.ACTORS[role],
            {
                "model_call_id": model_call_id,
                "role": role.value,
                "provider": model.provider,
                "model": model.model,
                "effort": model.effort,
                "target": dict(target),
                "prompt_sha256": sha256_text(prompt),
            },
        )

    def chunk_model_call(
        self,
        *,
        model_call_id: str,
        role: ModelRole,
        channel: str,
        index: int,
        text: str,
    ) -> dict[str, Any]:
        return self.log.append(
            "model_call_chunk",
            self.ACTORS[role],
            {
                "model_call_id": model_call_id,
                "channel": channel,
                "index": index,
                "text": text,
                "text_sha256": sha256_text(text),
            },
        )

    def finish_model_call(
        self,
        *,
        model_call_id: str,
        role: ModelRole,
        output_text: str,
        finish_reason: str,
        usage: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.log.append(
            "model_call_finished",
            self.ACTORS[role],
            {
                "model_call_id": model_call_id,
                "output_text": output_text,
                "output_sha256": sha256_text(output_text),
                "body_chars": len(output_text),
                "finish_reason": finish_reason,
                "usage": dict(usage),
            },
        )

    def normalize_writer_output(
        self,
        *,
        model_call_id: str,
        raw_output_sha256: str,
        content: StepContent,
        policy: str,
        normalizer_version: str,
        replacements: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Record 1.1 host format normalization of one finished Writer output."""
        if self.config.record_version != "1.1":
            raise RuntimeInvariantError(
                "writer output normalization requires Record 1.1"
            )
        body = content.to_record()
        return self.log.append(
            "writer_output_normalized",
            self.SYSTEM,
            {
                "model_call_id": model_call_id,
                "raw_output_sha256": raw_output_sha256,
                "content": body,
                "output_sha256": sha256_text(canonical_json(body)),
                "policy": policy,
                "normalizer_version": normalizer_version,
                "replacements": [dict(item) for item in replacements],
            },
        )

    def fail_model_call(
        self,
        *,
        model_call_id: str,
        role: ModelRole,
        failure_kind: str,
        message: str,
        partial_output: str,
        retryable: bool,
    ) -> dict[str, Any]:
        return self.log.append(
            "model_call_failed",
            self.ACTORS[role],
            {
                "model_call_id": model_call_id,
                "failure_kind": failure_kind,
                "message": message,
                "partial_output_text": partial_output,
                "partial_output_sha256": sha256_text(partial_output),
                "body_chars": len(partial_output),
                "retryable": retryable,
            },
        )

    def abort_model_call(
        self,
        *,
        model_call_id: str,
        role: ModelRole,
        human_action_id: str,
        partial_output: str,
    ) -> dict[str, Any]:
        return self.log.append(
            "model_call_aborted",
            self.ACTORS[role],
            {
                "model_call_id": model_call_id,
                "human_action_id": human_action_id,
                "partial_output_text": partial_output,
                "partial_output_sha256": sha256_text(partial_output),
                "body_chars": len(partial_output),
            },
        )

    def seal_model_step(
        self,
        *,
        step_revision_id: str,
        branch_id: str,
        step_slot: int,
        content: StepContent,
        model_call_id: str,
    ) -> dict[str, Any]:
        body = content.to_record()
        return self.log.append(
            "step_revision_sealed",
            self.ACTORS[ModelRole.WRITER],
            {
                "step_revision_id": step_revision_id,
                "branch_id": branch_id,
                "step_slot": step_slot,
                "revision": 1,
                "replaces_step_revision_id": None,
                "content": body,
                "output_sha256": sha256_text(canonical_json(body)),
                "origin": {
                    "kind": "model",
                    "model_call_id": model_call_id,
                    "human_action_id": None,
                },
            },
        )

    def apply_model_revision(
        self,
        *,
        parent_branch_id: str,
        branch_id: str,
        target_step_revision_id: str,
        step_revision_id: str,
        model_call_id: str,
        reason: str,
        content: StepContent,
    ) -> dict[str, Any]:
        return self.log.append(
            "model_revision_applied",
            self.ACTORS[ModelRole.WRITER],
            {
                "parent_branch_id": parent_branch_id,
                "branch_id": branch_id,
                "target_step_revision_id": target_step_revision_id,
                "step_revision_id": step_revision_id,
                "model_call_id": model_call_id,
                "reason": reason,
                "content": content.to_record(),
            },
        )

    def defer_model_revision(
        self, *, model_call_id: str, target_step_revision_id: str, reason: str
    ) -> dict[str, Any]:
        return self.log.append(
            "model_revision_deferred",
            self.SYSTEM,
            {
                "model_call_id": model_call_id,
                "target_step_revision_id": target_step_revision_id,
                "reason": reason,
            },
        )

    def seal_human_revision(
        self,
        *,
        step_revision_id: str,
        branch_id: str,
        step_slot: int,
        revision: int,
        replaces_step_revision_id: str,
        content: StepContent,
        human_action_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        body = content.to_record()
        return self.log.append(
            "step_revision_sealed",
            {"kind": "human", "id": actor_id},
            {
                "step_revision_id": step_revision_id,
                "branch_id": branch_id,
                "step_slot": step_slot,
                "revision": revision,
                "replaces_step_revision_id": replaces_step_revision_id,
                "content": body,
                "output_sha256": sha256_text(canonical_json(body)),
                "origin": {
                    "kind": "human",
                    "model_call_id": None,
                    "human_action_id": human_action_id,
                },
            },
        )

    def request_check(
        self,
        *,
        check_id: str,
        step_revision_id: str,
        output_sha256: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.log.append(
            "check_requested",
            self.SYSTEM,
            {
                "check_id": check_id,
                "target_step_revision_id": step_revision_id,
                "target_output_sha256": output_sha256,
                "required_for_candidate": True,
                "reason": reason,
            },
        )

    def complete_check(
        self,
        *,
        check_id: str,
        step_revision_id: str,
        output_sha256: str,
        checker_call_id: str,
        output: CheckOutput,
    ) -> dict[str, Any]:
        return self.log.append(
            "check_completed",
            self.ACTORS[ModelRole.CHECKER],
            {
                "check_id": check_id,
                "target_step_revision_id": step_revision_id,
                "target_output_sha256": output_sha256,
                "checker_call_id": checker_call_id,
                "verdict": output.verdict,
                "reason": output.reason,
                "evidence": [item.to_record() for item in output.evidence],
            },
        )

    def declare_candidate(
        self, *, candidate_id: str, branch_id: str, reason: str
    ) -> dict[str, Any]:
        state = self.snapshot()
        branches = {item["branch_id"]: item for item in state["branches"]}
        steps = {item["step_revision_id"]: item for item in state["step_revisions"]}
        checks = state["checks"]
        step_ids = branches[branch_id]["step_revision_ids"]
        required = sorted(
            item["check_id"]
            for item in checks
            if item["required_for_candidate"]
            and item["target_step_revision_id"] in step_ids
        )
        return self.log.append(
            "candidate_declared",
            self.ACTORS[ModelRole.WRITER],
            {
                "candidate_id": candidate_id,
                "branch_id": branch_id,
                "tip_step_revision_id": step_ids[-1],
                "transcript_step_revision_ids": list(step_ids),
                "transcript_sha256": transcript_sha256(step_ids, steps),
                "required_check_ids": required,
                **(
                    {
                        "unresolved_check_ids": sorted(
                            item["check_id"]
                            for item in checks
                            if item["check_id"] in required
                            and item["verdict"] in {"objection", "hard_defect"}
                        )
                    }
                    if self.config.record_version == "1.1"
                    else {}
                ),
                "declared_by": "writer",
                "reason": reason,
            },
        )

    def request_judgement(
        self,
        *,
        judgement_id: str,
        candidate_id: str,
        candidate_sha256: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.log.append(
            "judgement_requested",
            self.SYSTEM,
            {
                "judgement_id": judgement_id,
                "candidate_id": candidate_id,
                "candidate_transcript_sha256": candidate_sha256,
                "requested_by": "system",
                "reason": reason,
            },
        )

    def complete_judgement(
        self,
        *,
        judgement_id: str,
        candidate_id: str,
        candidate_sha256: str,
        judge_call_id: str,
        output: JudgeOutput,
    ) -> dict[str, Any]:
        return self.log.append(
            "judgement_completed",
            self.ACTORS[ModelRole.JUDGE],
            {
                "judgement_id": judgement_id,
                "candidate_id": candidate_id,
                "candidate_transcript_sha256": candidate_sha256,
                "judge_call_id": judge_call_id,
                "verdict": output.verdict,
                "reason": output.reason,
                "score": output.score,
            },
        )


def writer_control_from_call(call: Mapping[str, Any]) -> WriterControl:
    from .types import BranchAlternative, WriterDecision

    raw_chunks = [item for item in call["chunks"] if item["channel"] == "raw"]
    if len(raw_chunks) != 1:
        raise RuntimeInvariantError(
            "writer call must contain exactly one raw control chunk"
        )
    raw_text = raw_chunks[0]["text"]
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeInvariantError("writer raw control chunk is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) not in (
        {"decision", "alternatives"},
        {"decision", "alternatives", "revise_step_revision_id", "reason"},
    ):
        raise RuntimeInvariantError(
            "writer raw control chunk must use exactly decision and alternatives"
        )
    if canonical_json(value) != raw_text:
        raise RuntimeInvariantError("writer raw control chunk must be canonical JSON")
    raw_alternatives = value["alternatives"]
    if not isinstance(raw_alternatives, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_alternatives
    ):
        raise RuntimeInvariantError(
            "writer control alternatives must be a list of non-empty strings"
        )
    try:
        decision = WriterDecision(value["decision"])
        alternatives = tuple(BranchAlternative(text) for text in raw_alternatives)
        return WriterControl(
            decision=decision,
            alternatives=alternatives,
            revise_step_revision_id=value.get("revise_step_revision_id"),
            reason=value.get("reason"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeInvariantError("writer raw control chunk is invalid") from exc


def raw_control_event_id(
    call: Mapping[str, Any], events: Iterable[Mapping[str, Any]]
) -> str:
    raw_chunks = [item for item in call["chunks"] if item["channel"] == "raw"]
    if len(raw_chunks) != 1:
        raise RuntimeInvariantError(
            "writer call must contain exactly one raw control chunk"
        )
    target_index = raw_chunks[0]["index"]
    matches = [
        event["event_id"]
        for event in events
        if event["type"] == "model_call_chunk"
        and event["payload"]["model_call_id"] == call["model_call_id"]
        and event["payload"]["index"] == target_index
    ]
    if len(matches) != 1:
        raise RuntimeInvariantError("cannot bind proposal to its raw control event")
    return matches[0]


__all__ = [
    "EventLogWriter",
    "RecordV1Writer",
    "raw_control_event_id",
    "utc_now",
    "writer_control_from_call",
]
