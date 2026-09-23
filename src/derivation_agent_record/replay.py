"""Checksum-gated deterministic replay for Derivation Agent Record V1."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .macro_expansion import (
    DefinitionTable,
    MacroCheckError,
    verify_macro_expansion,
)
from .model import (
    ACTOR_KINDS,
    BRANCH_STATUSES,
    BRANCH_TRANSITIONS,
    CALL_TERMINAL_STATES,
    CANONICAL_SCHEMA_VERSION,
    CANONICAL_SCHEMA_VERSION_1_1,
    CHECK_VERDICTS,
    EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION_1_1,
    EVENT_TYPES,
    HARD_DEFECT_EVIDENCE_KINDS,
    HUMAN_ACTIONS,
    JUDGEMENT_VERDICTS,
    ContractError,
    ancestor_evidence_text,
    canonical_json,
    clone,
    compute_event_sha256,
    require,
    require_commit,
    require_exact_keys,
    require_id,
    require_sha,
    require_text,
    sha256_text,
    transcript_sha256,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _restorable_control(character: str) -> bool:
    """U+0000-U+0008, U+000B, U+000E-U+001F: JSON transport corruption."""
    code = ord(character)
    return code <= 0x08 or code == 0x0B or 0x0E <= code <= 0x1F


@dataclass(frozen=True)
class ReplayResult:
    """One replay: the canonical state, and the events it was replayed from.

    The two travel together because every consumer needs both and a consumer
    holding halves of two different replays is the one failure a reader cannot
    see — see ``render_html``.
    """

    canonical: dict[str, Any]
    events: list[dict[str, Any]]

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.canonical, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        return canonical_json(self.canonical) + "\n"

    def render(self) -> str:
        """This record as one self-contained read-only HTML page."""

        # Imported here, not at module scope: the renderer reads a result, so
        # importing it at the top would make the two modules import each other.
        from .render import render_html

        return render_html(self)


class ReplayEngine:
    """Replay one complete append-only record into canonical state."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.run: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.event_ids: set[str] = set()
        self.previous_event_sha256: str | None = None
        self.human_actions: dict[str, dict[str, Any]] = {}
        self.branches: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.checks: dict[str, dict[str, Any]] = {}
        #: check_id -> the ``seq`` of the event that completed it. Ordering is a
        #: property of the log, never of an event's name, so it is read from the
        #: envelope and kept here rather than parsed back out of an event_id.
        #: Engine state, not canonical state: the canonical record already
        #: carries ``completed_event_id`` and the event log carries the order.
        self.check_completion_seq: dict[str, int] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.judgements: dict[str, dict[str, Any]] = {}
        self.selections: dict[str, dict[str, Any]] = {}
        self.schema_version: str | None = None
        self.source_evidence: dict[str, dict[str, Any]] = {}

    @property
    def is_v1_1(self) -> bool:
        return self.schema_version == EVENT_SCHEMA_VERSION_1_1

    @staticmethod
    def _writer_control(call: Mapping[str, Any]) -> dict[str, Any]:
        raw = [chunk["text"] for chunk in call["chunks"] if chunk["channel"] == "raw"]
        require(len(raw) == 1, "model control requires exactly one raw chunk")
        try:
            value = json.loads(raw[0])
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid model control JSON") from exc
        require(isinstance(value, dict), "model control must be an object")
        require(canonical_json(value) == raw[0], "model control must be canonical")
        require(set(value) in ({"decision", "alternatives"}, {"decision", "alternatives", "revise_step_revision_id", "reason"}), "model control fields drift")
        require(value["decision"] in {"continue", "fork", "complete", "revise", "blocked"}, "unsupported Writer decision")
        alternatives = value["alternatives"]
        require(isinstance(alternatives, list) and all(isinstance(s, str) and s.strip() for s in alternatives), "invalid Writer alternatives")
        require(bool(alternatives) == (value["decision"] == "fork"), "fork alternatives mismatch")
        if value["decision"] == "revise":
            require_id(value.get("revise_step_revision_id"), "revision target")
            require_text(value.get("reason"), "revision reason")
        else:
            require(value.get("revise_step_revision_id") is None, "non-revision control cannot name revision target")
        if value["decision"] == "blocked":
            require_text(value.get("reason"), "blocked reason")
        return value

    @staticmethod
    def _writer_output_sha256(call: Mapping[str, Any]) -> str | None:
        """Scientific content hash of a Writer call.

        A recorded host normalization replaces the raw output hash; a call
        without one keeps its immutable raw hash exactly as before.
        """
        normalized = call.get("normalized_output")
        return call["output_sha256"] if normalized is None else normalized["output_sha256"]

    def _on_writer_output_normalized(self, event: Mapping[str, Any]) -> None:
        """Deterministic host format normalization of one finished Writer output.

        The raw call stays immutable. Replay re-applies the recorded edit list to
        the raw five fields and requires the recorded content and hash exactly;
        every edit must be one of the allowed structural kinds.
        """
        require(self.is_v1_1 and event["actor"]["kind"] == "system", "writer output normalization requires 1.1 system actor")
        p = event["payload"]
        require_exact_keys(p, {"model_call_id", "raw_output_sha256", "content", "output_sha256", "policy", "normalizer_version", "replacements"}, "writer_output_normalized.payload")
        call_id = p["model_call_id"]
        require(call_id in self.calls, "normalization targets unknown model call")
        call = self.calls[call_id]
        require(call["role"] == "writer" and call["state"] == "finished", "normalization needs finished Writer call")
        require("normalized_output" not in call, "Writer output already normalized")
        require("runtime_disposition" not in call, "Writer output already consumed")
        require(not any(s["origin"]["model_call_id"] == call_id for s in self.steps.values()), "writer output already materialized")
        require_sha(p["raw_output_sha256"], "raw_output_sha256")
        require(p["raw_output_sha256"] == call["output_sha256"], "normalization raw hash differs from Writer call output")
        require_text(p["policy"], "normalization policy")
        require_text(p["normalizer_version"], "normalizer_version")
        fields = ("claim", "why", "source", "derivation", "scope")
        try:
            raw = json.loads(call["output_text"])
        except (TypeError, ValueError) as exc:
            raise ContractError("normalized Writer output is not JSON") from exc
        require(isinstance(raw, dict) and set(raw) == set(fields) and all(isinstance(raw[f], str) for f in fields), "normalized Writer output is not five text fields")
        require(canonical_json(raw) == call["output_text"], "normalized Writer output is not canonical JSON")
        content = p["content"]
        require(isinstance(content, dict), "normalized content must be an object")
        require_exact_keys(content, set(fields), "normalized content")
        for key in fields:
            require_text(content[key], f"normalized content.{key}")
        replacements = p["replacements"]
        require(isinstance(replacements, list) and bool(replacements), "normalization needs a non-empty replacement list")
        text = dict(raw)
        macro_definitions: DefinitionTable | None = None
        for index, item in enumerate(replacements):
            context = f"normalization replacement {index}"
            require(isinstance(item, dict), f"{context} must be an object")
            require_exact_keys(item, {"field", "start", "end", "original", "replacement", "kind", "source_id", "source_line"}, context)
            require(item["field"] in fields, f"{context}: unknown field")
            require(not (item["kind"] == "macro_expansion" and item["field"] == "source"), f"{context}: quotation field is never macro-expanded")
            start, end = item["start"], item["end"]
            require(type(start) is int and type(end) is int, f"{context}: offsets must be integers")
            value = text[item["field"]]
            require(0 <= start <= end <= len(value), f"{context}: offsets outside field")
            original, replacement = item["original"], item["replacement"]
            require(isinstance(original, str) and isinstance(replacement, str) and original != replacement, f"{context}: invalid texts")
            require(value[start:end] == original, f"{context}: original does not match text")
            following = value[end:end + 2]
            kind = item["kind"]
            if kind == "macro_expansion":
                require(re.match(r"\\[A-Za-z]+", original) is not None, f"{context}: expansion must replace a control word")
                require(item["source_id"] in self.source_evidence, f"{context}: expansion source is not registered")
                line = item["source_line"]
                source_lines = self.source_evidence[item["source_id"]]["text"].count("\n") + 1
                require(type(line) is int and 1 <= line <= source_lines, f"{context}: invalid source line")
                # The only replacement whose text is not fixed by its own kind:
                # derive it from the cited definition instead of trusting it.
                if macro_definitions is None:
                    macro_definitions = DefinitionTable({sid: entry["text"] for sid, entry in self.source_evidence.items()})
                try:
                    verify_macro_expansion(field_text=value, start=start, end=end, original=original, replacement=replacement, source_id=item["source_id"], source_line=line, table=macro_definitions)
                except MacroCheckError as exc:
                    raise ContractError(f"{context}: {exc}") from exc
            else:
                require(item["source_id"] is None and item["source_line"] is None, f"{context}: control repair cannot cite a source")
                if kind == "control_char_backslash":
                    require(len(original) == 1 and _restorable_control(original) and replacement == "\\", f"{context}: invalid control restoration")
                    require(following[:1].isascii() and following[:1].isalpha(), f"{context}: restored backslash must start a control word")
                elif kind == "control_char_removed":
                    require(len(original) == 1 and _restorable_control(original) and replacement == "", f"{context}: invalid control removal")
                    require(following[:1] == "\\" and following[1:2].isascii() and following[1:2].isalpha(), f"{context}: removed control must precede a control word")
                elif kind == "del_removed":
                    require(original == "\x7f" and replacement == "" and following[:1] == "\\", f"{context}: invalid DEL removal")
                elif kind == "ansi_escape_removed":
                    require(_ANSI_ESCAPE.fullmatch(original) is not None and replacement == "", f"{context}: invalid ANSI escape removal")
                else:
                    require(False, f"{context}: unsupported replacement kind {kind!r}")
            text[item["field"]] = value[:start] + replacement + value[end:]
        require(text == content, "replacements do not reproduce normalized content")
        require_sha(p["output_sha256"], "normalized output_sha256")
        output_sha = sha256_text(canonical_json(content))
        require(p["output_sha256"] == output_sha, "normalized content hash mismatch")
        require(output_sha != call["output_sha256"], "normalization must change the Writer output")
        call["normalized_output"] = {
            "event_id": event["event_id"],
            "raw_output_sha256": p["raw_output_sha256"],
            "output_sha256": output_sha,
            "policy": p["policy"],
            "normalizer_version": p["normalizer_version"],
            "replacement_count": len(replacements),
        }

    def _on_model_revision_applied(self, event: Mapping[str, Any]) -> None:
        """An atomic, model-sourced replacement; no synthetic human action."""
        require(self.is_v1_1 and event["actor"]["kind"] == "model", "model revision requires Record 1.1 and model actor")
        p = event["payload"]
        require_exact_keys(p, {"parent_branch_id", "branch_id", "target_step_revision_id", "step_revision_id", "model_call_id", "reason", "content"}, "model_revision_applied.payload")
        parent_id, branch_id = p["parent_branch_id"], require_id(p["branch_id"], "branch_id")
        step_id = require_id(p["step_revision_id"], "step_revision_id")
        require(parent_id in self.branches and branch_id not in self.branches, "revision branch identity mismatch")
        parent = self.branches[parent_id]
        require(parent["status"] == "active", "model revision requires active parent")
        old_id = p["target_step_revision_id"]
        require(old_id in parent["step_revision_ids"] and step_id not in self.steps, "revision target is not on current route or new ID reused")
        require(p["model_call_id"] in self.calls, "revision needs known writer call")
        call = self.calls[p["model_call_id"]]
        require(call["role"] == "writer" and call["state"] == "finished", "revision needs finished Writer call")
        require(call["target"] == {"branch_id": parent_id, "step_slot": len(parent["step_revision_ids"]) + 1}, "revision call must target parent next slot")
        require(not any(s["origin"]["model_call_id"] == p["model_call_id"] for s in self.steps.values()), "writer output already materialized")
        control = self._writer_control(call)
        require(control.get("decision") == "revise" and control.get("revise_step_revision_id") == old_id, "revision differs from model control")
        require_text(p["reason"], "revision reason")
        require(control.get("reason") == p["reason"], "revision reason differs from Writer")
        content = p["content"]
        require(isinstance(content, dict), "replacement content must be an object")
        require_exact_keys(content, {"claim", "why", "source", "derivation", "scope"}, "replacement content")
        for key, value in content.items():
            require_text(value, f"replacement {key}")
        output_sha = sha256_text(canonical_json(content))
        require(self._writer_output_sha256(call) == output_sha, "replacement differs from immutable Writer output")
        old = self.steps[old_id]
        prefix = parent["step_revision_ids"][:parent["step_revision_ids"].index(old_id)]
        parent["status"] = "parked"
        parent["status_history"].append({"seq": event["seq"], "from": "active", "to": "parked", "reason": "model_superseded", "event_id": event["event_id"]})
        self.branches[branch_id] = {
            "branch_id": branch_id, "parent_branch_id": parent_id,
            "fork_mode": "replace", "anchor_step_revision_id": old_id,
            "inherited_step_revision_ids": list(prefix), "step_revision_ids": [*prefix, step_id],
            "hypothesis": {"text": parent["hypothesis"]["text"], "source": "model", "source_event_id": event["event_id"]},
            "status": "active", "created_reason": "model_revision", "human_action_id": None,
            "created_event_id": event["event_id"], "created_at": event["recorded_at"],
            "status_history": [{"seq": event["seq"], "from": None, "to": "active", "reason": "model_revision", "event_id": event["event_id"]}],
        }
        self.steps[step_id] = {
            "step_revision_id": step_id, "branch_id": branch_id, "step_slot": old["step_slot"],
            "revision": old["revision"] + 1, "replaces_step_revision_id": old_id,
            "content": clone(content), "output_sha256": output_sha,
            "origin": {"kind": "model", "model_call_id": p["model_call_id"], "human_action_id": None},
            "sealed_event_id": event["event_id"], "sealed_at": event["recorded_at"],
        }

    def _on_model_revision_deferred(self, event: Mapping[str, Any]) -> None:
        require(self.is_v1_1 and event["actor"]["kind"] == "system", "revision defer requires 1.1 system actor")
        p = event["payload"]
        require_exact_keys(p, {"model_call_id", "target_step_revision_id", "reason"}, "model_revision_deferred.payload")
        require(p["model_call_id"] in self.calls, "deferred revision requires known call")
        call = self.calls[p["model_call_id"]]
        require(call["role"] == "writer" and call["state"] == "finished", "deferred revision needs finished Writer")
        require("runtime_disposition" not in call, "Writer disposition already recorded")
        control = self._writer_control(call)
        require(control.get("decision") == "revise" and control.get("revise_step_revision_id") == p["target_step_revision_id"], "deferred revision target mismatch")
        branch = self.branches[call["target"]["branch_id"]]
        require(p["target_step_revision_id"] in branch["step_revision_ids"], "deferred revision not on current route")
        require_text(p["reason"], "deferred revision reason")
        call["runtime_disposition"] = {"kind": "revision_deferred", "target_step_revision_id": p["target_step_revision_id"], "reason": p["reason"], "event_id": event["event_id"]}

    def _on_source_evidence_registered(self, event: Mapping[str, Any]) -> None:
        require(self.is_v1_1 and event["actor"]["kind"] == "system", "source registration requires 1.1 system actor")
        p = event["payload"]
        require_exact_keys(p, {"source_id", "kind", "text", "sha256"}, "source_evidence_registered.payload")
        source_id = require_id(p["source_id"], "source_id")
        require(source_id not in self.source_evidence, "source evidence already registered")
        require(p["kind"] == "literature_quote", "unsupported source evidence kind")
        require_text(p["text"], "source evidence text")
        require_sha(p["sha256"], "source evidence sha256")
        require(sha256_text(p["text"]) == p["sha256"], "source evidence hash mismatch")
        require(not self.calls, "method source evidence must be frozen before model calls")
        self.source_evidence[source_id] = clone(p)

    def _on_check_retry_authorized(self, event: Mapping[str, Any]) -> None:
        require(self.is_v1_1 and event["actor"]["kind"] == "system", "check retry requires 1.1 system actor")
        p = event["payload"]
        require_exact_keys(p, {"check_id", "human_action_id"}, "check_retry_authorized.payload")
        require(p["check_id"] in self.checks and p["human_action_id"] in self.human_actions, "check retry needs existing check and action")
        check, action = self.checks[p["check_id"]], self.human_actions[p["human_action_id"]]
        require(check["verdict"] == "instrument_failure" and check["required_for_candidate"], "only an active instrument failure can be retried")
        require(action["action"] == "resume_branch", "check retry needs explicit resume action")
        branch = self.branches[action["target"]["branch_id"]]
        require(check["target_step_revision_id"] in branch["step_revision_ids"], "check retry action targets another route")
        require(action["seq"] > self.check_completion_seq[p["check_id"]], "check retry authorization predates failure")
        check["required_for_candidate"] = False
        check["retry_authorized_event_id"] = event["event_id"]

    def _on_writer_route_completion(self, event: Mapping[str, Any]) -> None:
        require(self.is_v1_1 and event["actor"]["kind"] == "model", "route completion requires 1.1 model actor")
        p = event["payload"]
        require_exact_keys(p, {"branch_id", "model_call_id", "tip_step_revision_id"}, "writer_route_completion.payload")
        require(p["branch_id"] in self.branches and p["model_call_id"] in self.calls, "route completion target unknown")
        branch, call = self.branches[p["branch_id"]], self.calls[p["model_call_id"]]
        require(branch["status"] == "active" and branch["step_revision_ids"] and branch["step_revision_ids"][-1] == p["tip_step_revision_id"], "completion tip mismatch")
        require(call["role"] == "writer" and call["state"] == "finished" and call["target"] == {"branch_id": p["branch_id"], "step_slot": len(branch["step_revision_ids"]) + 1}, "completion Writer call mismatch")
        require(self._writer_control(call).get("decision") == "complete", "completion lacks explicit model decision")
        require(self._writer_output_sha256(call) == self.steps[p["tip_step_revision_id"]]["output_sha256"], "completion changed scientific content")
        require("runtime_disposition" not in call, "completion call already consumed")
        required = [self.checks[cid] for cid in self._required_checks_for_transcript(branch["step_revision_ids"])]
        require(all(c["state"] == "completed" and c["verdict"] != "instrument_failure" for c in required), "completion cannot skip pending or failed instruments")
        branch["status"] = "completed"
        branch["status_history"].append({"seq": event["seq"], "from": "active", "to": "completed", "reason": "writer_complete", "event_id": event["event_id"]})
        call["runtime_disposition"] = {"kind": "route_completion", "target_step_revision_id": p["tip_step_revision_id"], "reason": "Explicit completion of the unchanged checked route.", "event_id": event["event_id"]}

    def replay(self, raw_events: Sequence[Mapping[str, Any]]) -> ReplayResult:
        require(bool(raw_events), "event log is empty")
        for expected_seq, raw_event in enumerate(raw_events, start=1):
            # A shallow copy, for the same reason ``compute_event_sha256`` takes
            # one: nothing below writes into an event, and every handler that
            # keeps part of a payload ``clone()``s what it keeps, so the caller's
            # mapping and everything under it come back untouched.
            # ``tests/test_record_contract.py`` pins that at this entry point.
            event = dict(raw_event)
            self._validate_envelope(event, expected_seq)
            handler = getattr(self, f"_on_{event['type']}")
            handler(event)
            self.events.append(event)
            self.previous_event_sha256 = event["event_sha256"]
        self._final_checks()
        return ReplayResult(self._canonical_state(), list(self.events))

    def _validate_envelope(self, event: MutableMapping[str, Any], expected_seq: int) -> None:
        require_exact_keys(
            event,
            {
                "schema_version",
                "run_id",
                "seq",
                "event_id",
                "recorded_at",
                "type",
                "actor",
                "prev_event_sha256",
                "event_sha256",
                "payload",
            },
            f"event {expected_seq}",
        )
        require(event["schema_version"] in {EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION_1_1}, f"event {expected_seq}: wrong schema_version")
        if self.schema_version is None:
            self.schema_version = event["schema_version"]
        require(event["schema_version"] == self.schema_version, "record version cannot change within a run")
        require(event["seq"] == expected_seq, f"event sequence must be contiguous; expected {expected_seq}")
        event_id = require_id(event["event_id"], f"event {expected_seq}.event_id")
        require(event_id not in self.event_ids, f"duplicate event_id {event_id}")
        self.event_ids.add(event_id)
        require_text(event["recorded_at"], f"event {expected_seq}.recorded_at")
        types = EVENT_TYPES | ({"model_revision_applied", "model_revision_deferred", "source_evidence_registered", "check_retry_authorized", "writer_route_completion", "writer_output_normalized"} if self.is_v1_1 else set())
        require(event["type"] in types, f"event {expected_seq}: unsupported event type {event['type']!r}")
        require(isinstance(event["payload"], dict), f"event {expected_seq}.payload must be an object")
        actor = event["actor"]
        require(isinstance(actor, dict), f"event {expected_seq}.actor must be an object")
        require_exact_keys(actor, {"kind", "id"}, f"event {expected_seq}.actor")
        require(actor["kind"] in ACTOR_KINDS, f"event {expected_seq}: invalid actor kind")
        require_id(actor["id"], f"event {expected_seq}.actor.id")
        run_id = require_id(event["run_id"], f"event {expected_seq}.run_id")
        if self.run_id is None:
            require(event["type"] == "run_created", "first event must be run_created")
            require(event["prev_event_sha256"] is None, "first event prev_event_sha256 must be null")
            self.run_id = run_id
        else:
            require(run_id == self.run_id, f"event {expected_seq}: run_id changed")
            require_sha(event["prev_event_sha256"], f"event {expected_seq}.prev_event_sha256")
            require(
                event["prev_event_sha256"] == self.previous_event_sha256,
                f"event {expected_seq}: prev_event_sha256 mismatch",
            )
        require_sha(event["event_sha256"], f"event {expected_seq}.event_sha256")
        actual_hash = compute_event_sha256(event)
        require(event["event_sha256"] == actual_hash, f"event {expected_seq}: event_sha256 mismatch")

    @property
    def configuration(self) -> Mapping[str, Any]:
        require(self.run is not None, "run has not been created")
        return self.run["configuration"]

    def _active_branch_count(self) -> int:
        return sum(branch["status"] == "active" for branch in self.branches.values())

    def _on_run_created(self, event: Mapping[str, Any]) -> None:
        require(self.run is None, "run_created may occur only once")
        require(event["actor"]["kind"] == "system", "run_created actor must be system")
        payload = event["payload"]
        require_exact_keys(
            payload,
            {
                "record_spec",
                "event_schema",
                "canonical_schema",
                "code_commit",
                "task",
                "pack",
                "configuration",
                "input_policy",
            },
            "run_created.payload",
        )
        for name in ("record_spec", "event_schema", "canonical_schema"):
            item = payload[name]
            require(isinstance(item, dict), f"{name} must be an object")
            require_exact_keys(item, {"path", "sha256"}, name)
            require_text(item["path"], f"{name}.path")
            require_sha(item["sha256"], f"{name}.sha256")
        require_commit(payload["code_commit"], "code_commit")
        for name in ("task", "pack"):
            item = payload[name]
            require(isinstance(item, dict), f"{name} must be an object")
            require_exact_keys(item, {"id", "sha256"}, name)
            require_id(item["id"], f"{name}.id")
            require_sha(item["sha256"], f"{name}.sha256")
        config = payload["configuration"]
        require(isinstance(config, dict), "configuration must be an object")
        require_exact_keys(
            config,
            {"granularity", "max_active_branches", "max_model_calls", "models", "backend"} | ({"checker_enabled", "max_local_repairs"} if self.is_v1_1 else set()),
            "configuration",
        )
        require(config["granularity"] in {"one_claim", "one_task"}, "unsupported per-run granularity")
        limit = config["max_active_branches"]
        require(limit is None or (isinstance(limit, int) and not isinstance(limit, bool) and limit > 0), "max_active_branches must be null or a positive integer")
        require((self.is_v1_1 and config["max_model_calls"] is None) or (type(config["max_model_calls"]) is int and config["max_model_calls"] > 0), "max_model_calls must be positive or v1.1 unlimited")
        if self.is_v1_1:
            require(type(config["checker_enabled"]) is bool, "checker_enabled must be boolean")
            require(type(config["max_local_repairs"]) is int and config["max_local_repairs"] >= 0, "max_local_repairs must be non-negative integer")
        models = config["models"]
        require(isinstance(models, dict), "models must be an object")
        require_exact_keys(models, {"writer", "checker", "judge"}, "models")
        for role in ("writer", "checker", "judge"):
            item = models[role]
            require(isinstance(item, dict), f"models.{role} must be an object")
            require_exact_keys(item, {"provider", "model", "effort"}, f"models.{role}")
            for field in ("provider", "model", "effort"):
                require_text(item[field], f"models.{role}.{field}")
        backend = config["backend"]
        require(isinstance(backend, dict), "backend must be an object")
        require_exact_keys(backend, {"name", "version"}, "backend")
        require_text(backend["name"], "backend.name")
        require_text(backend["version"], "backend.version")
        policy = payload["input_policy"]
        require(isinstance(policy, dict), "input_policy must be an object")
        require_exact_keys(policy, {"reference_allowed", "allowed_paths"}, "input_policy")
        require(isinstance(policy["reference_allowed"], bool), "reference_allowed must be boolean")
        require(isinstance(policy["allowed_paths"], list) and all(isinstance(x, str) for x in policy["allowed_paths"]), "allowed_paths must be a string list")
        for allowed_path in policy["allowed_paths"]:
            require_text(allowed_path, "allowed_paths item")
            parsed = PurePosixPath(allowed_path)
            require(not parsed.is_absolute() and ".." not in parsed.parts, "allowed_paths must contain safe repository-relative paths")
            if not policy["reference_allowed"]:
                require(not parsed.parts or parsed.parts[0] != "reference", "reference path listed while reference_allowed is false")
        self.run = clone(payload)
        self.run.update({"run_id": event["run_id"], "created_event_id": event["event_id"], "created_at": event["recorded_at"]})

    def _on_human_action_recorded(self, event: Mapping[str, Any]) -> None:
        require(event["actor"]["kind"] == "human", "human_action_recorded actor must be human")
        payload = event["payload"]
        require_exact_keys(payload, {"action_id", "action", "target", "reason", "content", "content_sha256"}, "human_action_recorded.payload")
        action_id = require_id(payload["action_id"], "action_id")
        require(action_id not in self.human_actions, f"duplicate action_id {action_id}")
        action_name = payload["action"]
        require(action_name in HUMAN_ACTIONS, f"unsupported human action {action_name!r}")
        require(isinstance(payload["target"], dict), "human action target must be an object")
        expected_target_keys = {
            "pause_branch": {"branch_id"},
            "resume_branch": {"branch_id"},
            "kill_branch": {"branch_id"},
            "revise_step": {"step_revision_id"},
            "set_direction": {"branch_id"},
            "set_hypothesis": {"branch_id"},
            "abort_model_call": {"model_call_id"},
            "select_candidate": {"candidate_id"},
        }[action_name]
        require_exact_keys(payload["target"], expected_target_keys, f"human action {action_name} target")
        require_text(payload["reason"], "human action reason")
        content = payload["content"]
        if content is None:
            require(payload["content_sha256"] is None, "null content requires null content_sha256")
        else:
            require(isinstance(content, str), "human action content must be text or null")
            require_sha(payload["content_sha256"], "human action content_sha256")
            require(payload["content_sha256"] == sha256_text(content), "human action content hash mismatch")
        if action_name in {"revise_step", "set_direction", "set_hypothesis"}:
            require(content is not None, f"human action {action_name} requires content")
        target = payload["target"]
        if "branch_id" in target:
            require(target["branch_id"] in self.branches, "human action targets unknown branch")
        if "step_revision_id" in target:
            require(target["step_revision_id"] in self.steps, "human action targets unknown step revision")
        if "model_call_id" in target:
            require(target["model_call_id"] in self.calls, "human action targets unknown model call")
        if "candidate_id" in target:
            require(target["candidate_id"] in self.candidates, "human action targets unknown candidate")
        self.human_actions[action_id] = {
            **clone(payload),
            "actor_id": event["actor"]["id"],
            "event_id": event["event_id"],
            "seq": event["seq"],
            "recorded_at": event["recorded_at"],
        }

    def _on_branch_created(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(
            payload,
            {
                "branch_id",
                "parent_branch_id",
                "fork_mode",
                "anchor_step_revision_id",
                "inherited_step_revision_ids",
                "hypothesis",
                "initial_status",
                "created_reason",
                "human_action_id",
            },
            "branch_created.payload",
        )
        branch_id = require_id(payload["branch_id"], "branch_id")
        require(branch_id not in self.branches, f"duplicate branch_id {branch_id}")
        status = payload["initial_status"]
        require(status in {"active", "paused"}, "new branch must start active or paused")
        inherited = payload["inherited_step_revision_ids"]
        require(isinstance(inherited, list) and all(isinstance(x, str) for x in inherited), "inherited_step_revision_ids must be a string list")
        hypothesis = payload["hypothesis"]
        require(isinstance(hypothesis, dict), "hypothesis must be an object")
        require_exact_keys(hypothesis, {"text", "source", "source_event_id"}, "hypothesis")
        require_text(hypothesis["text"], "hypothesis.text")
        require(hypothesis["source"] in {"model", "human", "inherited"}, "invalid hypothesis source")
        if hypothesis["source_event_id"] is not None:
            require_id(hypothesis["source_event_id"], "hypothesis.source_event_id")

        parent_id = payload["parent_branch_id"]
        fork_mode = payload["fork_mode"]
        anchor_id = payload["anchor_step_revision_id"]
        reason = payload["created_reason"]
        human_action_id = payload["human_action_id"]
        if parent_id is None:
            require(event["actor"]["kind"] == "system", "root branch actor must be system")
            require(fork_mode == "root", "root branch must use fork_mode=root")
            require(anchor_id is None and inherited == [], "root branch cannot inherit steps")
            require(reason == "root", "root branch must use created_reason=root")
            require(human_action_id is None, "root branch cannot reference a human action")
        else:
            require(parent_id in self.branches, f"unknown parent branch {parent_id}")
            require(fork_mode in {"after", "replace"}, "child branch fork_mode must be after or replace")
            require(anchor_id in self.steps, "child branch needs a known anchor step")
            parent_steps = self.branches[parent_id]["step_revision_ids"]
            require(anchor_id in parent_steps, "anchor step is not in parent transcript")
            anchor_index = parent_steps.index(anchor_id)
            expected_prefix = parent_steps[: anchor_index + (1 if fork_mode == "after" else 0)]
            require(inherited == expected_prefix, f"branch {branch_id}: inherited transcript is not the exact parent prefix")
            human_reason_to_action = {
                "human_direction": "set_direction",
                "human_hypothesis": "set_hypothesis",
                "human_revision": "revise_step",
            }
            if reason in human_reason_to_action:
                require(event["actor"]["kind"] == "human", f"{reason} branch must be created by human")
                require(human_action_id in self.human_actions, f"{reason} branch needs a prior human action")
                action = self.human_actions[human_action_id]
                require(action["action"] == human_reason_to_action[reason], f"{reason} references wrong action")
                require(hypothesis["source"] == "human", f"{reason} branch hypothesis must be human-sourced")
                require(hypothesis["source_event_id"] == action["event_id"], f"{reason} hypothesis must cite its human action event")
                if reason == "human_revision":
                    require(fork_mode == "replace", "human revision must fork with mode=replace")
                    require(action["target"].get("step_revision_id") == anchor_id, "revision action must target the replaced step")
                else:
                    require(action["target"].get("branch_id") == parent_id, f"{reason} action must target the parent branch")
                    require(action["content"] == hypothesis["text"], f"{reason} hypothesis must equal the human action content")
            else:
                require(reason in {"model_alternative", "instrument_retry"}, f"invalid child created_reason {reason!r}")
                require(human_action_id is None, "non-human branch cannot reference a human action")
                if reason == "model_alternative":
                    require(event["actor"]["kind"] == "model", "model_alternative branch must be created by model")
                    require(hypothesis["source"] == "model", "model_alternative hypothesis must be model-sourced")
                else:
                    require(event["actor"]["kind"] in {"system", "model"}, "instrument_retry branch must be created by system or model")
                    require(hypothesis["source"] in {"model", "inherited"}, "instrument_retry hypothesis must be model or inherited")
        self.branches[branch_id] = {
            "branch_id": branch_id,
            "parent_branch_id": parent_id,
            "fork_mode": fork_mode,
            "anchor_step_revision_id": anchor_id,
            "inherited_step_revision_ids": list(inherited),
            "step_revision_ids": list(inherited),
            "hypothesis": clone(hypothesis),
            "status": status,
            "created_reason": reason,
            "human_action_id": human_action_id,
            "created_event_id": event["event_id"],
            "created_at": event["recorded_at"],
            "status_history": [{"seq": event["seq"], "from": None, "to": status, "reason": reason, "event_id": event["event_id"]}],
        }
        limit = self.configuration["max_active_branches"]
        if limit is not None:
            require(self._active_branch_count() <= limit, "max_active_branches exceeded")

    def _on_branch_status_changed(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(
            payload,
            {"branch_id", "from_status", "to_status", "reason_code", "human_action_id", "check_id", "model_call_id"},
            "branch_status_changed.payload",
        )
        branch_id = payload["branch_id"]
        require(branch_id in self.branches, f"unknown branch {branch_id}")
        branch = self.branches[branch_id]
        require(payload["from_status"] == branch["status"], f"branch {branch_id}: stale from_status")
        to_status = payload["to_status"]
        require(to_status in BRANCH_STATUSES, "invalid branch status")
        require(to_status in BRANCH_TRANSITIONS[branch["status"]], f"illegal branch transition {branch['status']} -> {to_status}")
        reason = payload["reason_code"]
        action_id = payload["human_action_id"]
        check_id = payload["check_id"]
        call_id = payload["model_call_id"]
        human_reasons = {
            "human_pause": "pause_branch",
            "human_resume": "resume_branch",
            "human_kill": "kill_branch",
        }
        if reason in human_reasons:
            require(event["actor"]["kind"] == "human", f"{reason} must be human")
            require(action_id in self.human_actions, f"{reason} needs a human action")
            require(self.human_actions[action_id]["action"] == human_reasons[reason], f"{reason} references wrong action")
            require(self.human_actions[action_id]["target"].get("branch_id") == branch_id, f"{reason} action targets another branch")
            require(check_id is None and call_id is None, f"{reason} cannot cite check or model call")
        elif reason == "writer_complete":
            require(event["actor"]["kind"] == "model" and to_status == "completed", "writer_complete must be model -> completed")
            require(action_id is None and check_id is None and call_id is None, "writer_complete cannot cite action, check, or call")
        elif reason == "runtime_failure" and self.is_v1_1:
            require(event["actor"]["kind"] == "system" and to_status == "paused", "runtime_failure must be system -> paused")
            require(action_id is None and call_id in self.calls, "runtime failure needs failed call")
            call = self.calls[call_id]
            require(call["state"] in {"failed", "aborted"}, "runtime pause requires terminal failed call")
            if call["role"] == "writer":
                require(call["target"]["branch_id"] == branch_id and check_id is None, "writer failure branch mismatch")
            else:
                require(call["role"] == "checker" and check_id in self.checks, "checker failure needs check")
                require(call["target"]["check_id"] == check_id and self.checks[check_id]["target_step_revision_id"] in branch["step_revision_ids"], "checker failure route mismatch")
        elif reason == "writer_blocked" and self.is_v1_1:
            require(event["actor"]["kind"] == "model" and to_status == "parked", "writer_blocked must be model -> parked")
            require(action_id is None and check_id is None and call_id in self.calls, "writer_blocked needs only a writer call")
            call = self.calls[call_id]
            require(call["role"] == "writer" and call["state"] == "finished" and call["target"]["branch_id"] == branch_id, "writer_blocked call mismatch")
            require(self._writer_control(call)["decision"] == "blocked", "writer_blocked needs explicit model decision")
        elif reason == "hard_defect":
            require(event["actor"]["kind"] in {"checker", "system"}, "hard_defect transition actor must be checker or system")
            require(action_id is None and call_id is None, "hard_defect transition cannot cite human action or model call")
            require(check_id in self.checks, "hard_defect transition needs check_id")
            check = self.checks[check_id]
            require(check.get("verdict") == "hard_defect", "hard_defect transition references non-hard check")
            require(check["target_step_revision_id"] in branch["step_revision_ids"], "hard_defect check does not target this branch transcript")
            require(to_status == "killed", "hard_defect must kill branch")
        elif reason == "instrument_failure":
            require(event["actor"]["kind"] == "system", "instrument_failure branch transition must be recorded by system")
            require(action_id is None and check_id is None, "instrument_failure transition cannot cite human action or check")
            require(call_id in self.calls, "instrument_failure transition needs model_call_id")
            call = self.calls[call_id]
            require(call["role"] == "writer" and call["target"].get("branch_id") == branch_id, "instrument_failure call must be this branch writer")
            require(call["state"] in {"failed", "aborted"}, "instrument_failure needs failed or aborted call")
            require(call["body_chars"] == 0, "instrument_failure parking is reserved for zero-body calls")
            require(to_status == "parked", "instrument_failure must park branch")
        elif reason == "manual_reopen":
            require(event["actor"]["kind"] == "human" and action_id in self.human_actions, "manual_reopen needs human action")
            action = self.human_actions[action_id]
            require(action["action"] == "resume_branch" and action["target"].get("branch_id") == branch_id, "manual_reopen needs matching resume action")
            require(check_id is None and call_id is None, "manual_reopen cannot cite check or model call")
            require(to_status == "active", "manual_reopen must activate branch")
        else:
            require(False, f"unsupported branch status reason {reason!r}")
        branch["status"] = to_status
        branch["status_history"].append(
            {"seq": event["seq"], "from": payload["from_status"], "to": to_status, "reason": reason, "event_id": event["event_id"]}
        )
        limit = self.configuration["max_active_branches"]
        if limit is not None:
            require(self._active_branch_count() <= limit, "max_active_branches exceeded")

    def _on_model_call_started(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"model_call_id", "role", "provider", "model", "effort", "target", "prompt_sha256"}, "model_call_started.payload")
        call_id = require_id(payload["model_call_id"], "model_call_id")
        require(call_id not in self.calls, f"duplicate model_call_id {call_id}")
        role = payload["role"]
        require(role in {"writer", "checker", "judge"}, "invalid model-call role")
        expected_actor = {"writer": "model", "checker": "checker", "judge": "judge"}[role]
        require(event["actor"]["kind"] == expected_actor, f"{role} call actor kind mismatch")
        for field in ("provider", "model", "effort"):
            require_text(payload[field], f"model_call.{field}")
            require(payload[field] == self.configuration["models"][role][field], f"{role} call {field} differs from run configuration")
        require_sha(payload["prompt_sha256"], "prompt_sha256")
        target = payload["target"]
        require(isinstance(target, dict), "model call target must be an object")
        if role == "writer":
            require_exact_keys(target, {"branch_id", "step_slot"}, "writer target")
            require(target["branch_id"] in self.branches, "writer call targets unknown branch")
            branch = self.branches[target["branch_id"]]
            require(branch["status"] == "active", "writer call needs active branch")
            require(isinstance(target["step_slot"], int) and target["step_slot"] > 0, "writer target step_slot must be positive")
            require(target["step_slot"] == len(branch["step_revision_ids"]) + 1, "writer call targets a non-current step slot")
        elif role == "checker":
            require_exact_keys(target, {"check_id"}, "checker target")
            require(target["check_id"] in self.checks, "checker call targets unknown check")
            require(self.checks[target["check_id"]]["state"] == "requested", "checker call needs requested check")
        else:
            require_exact_keys(target, {"judgement_id"}, "judge target")
            require(target["judgement_id"] in self.judgements, "judge call targets unknown judgement")
            require(self.judgements[target["judgement_id"]]["state"] == "requested", "judge call needs requested judgement")
        for existing in self.calls.values():
            require(not (existing["state"] == "started" and existing["role"] == role and existing["target"] == target), "duplicate in-flight model call target")
        require(self.configuration["max_model_calls"] is None or len(self.calls) < self.configuration["max_model_calls"], "max_model_calls exceeded")
        self.calls[call_id] = {
            **clone(payload),
            "state": "started",
            "chunks": [],
            "output_text": None,
            "output_sha256": None,
            "body_chars": 0,
            "failure": None,
            "started_event_id": event["event_id"],
            "started_at": event["recorded_at"],
        }

    def _call_for_event(self, payload: Mapping[str, Any], context: str, event: Mapping[str, Any]) -> dict[str, Any]:
        call_id = payload.get("model_call_id")
        require(call_id in self.calls, f"{context}: unknown model_call_id")
        call = self.calls[call_id]
        require(call["state"] == "started", f"{context}: call is already terminal")
        expected_actor = {"writer": "model", "checker": "checker", "judge": "judge"}[call["role"]]
        require(event["actor"]["kind"] == expected_actor, f"{context}: actor kind does not match call role")
        return call

    def _on_model_call_chunk(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"model_call_id", "channel", "index", "text", "text_sha256"}, "model_call_chunk.payload")
        call = self._call_for_event(payload, "model_call_chunk", event)
        require(payload["channel"] in {"analysis", "body", "raw"}, "invalid chunk channel")
        require(payload["index"] == len(call["chunks"]), "model call chunk index is not contiguous")
        require(isinstance(payload["text"], str), "chunk text must be a string")
        require_sha(payload["text_sha256"], "chunk text_sha256")
        require(payload["text_sha256"] == sha256_text(payload["text"]), "chunk hash mismatch")
        call["chunks"].append(clone(payload))

    def _on_model_call_finished(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"model_call_id", "output_text", "output_sha256", "body_chars", "finish_reason", "usage"}, "model_call_finished.payload")
        call = self._call_for_event(payload, "model_call_finished", event)
        require(isinstance(payload["output_text"], str), "output_text must be a string")
        require_sha(payload["output_sha256"], "output_sha256")
        require(payload["output_sha256"] == sha256_text(payload["output_text"]), "model-call output hash mismatch")
        require(payload["body_chars"] == len(payload["output_text"]), "model-call body_chars mismatch")
        require_text(payload["finish_reason"], "finish_reason")
        require(isinstance(payload["usage"], dict), "usage must be an object")
        body_chunks = "".join(item["text"] for item in call["chunks"] if item["channel"] == "body")
        if body_chunks:
            require(body_chunks == payload["output_text"], "body chunks do not reconstruct finished output")
        call.update(
            {
                "state": "finished",
                "output_text": payload["output_text"],
                "output_sha256": payload["output_sha256"],
                "body_chars": payload["body_chars"],
                "finish_reason": payload["finish_reason"],
                "usage": clone(payload["usage"]),
                "terminal_event_id": event["event_id"],
                "terminal_at": event["recorded_at"],
            }
        )

    def _on_model_call_failed(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"model_call_id", "failure_kind", "message", "partial_output_text", "partial_output_sha256", "body_chars", "retryable"}, "model_call_failed.payload")
        call = self._call_for_event(payload, "model_call_failed", event)
        require_text(payload["failure_kind"], "failure_kind")
        require_text(payload["message"], "failure message")
        require(isinstance(payload["partial_output_text"], str), "partial_output_text must be a string")
        require_sha(payload["partial_output_sha256"], "partial_output_sha256")
        require(payload["partial_output_sha256"] == sha256_text(payload["partial_output_text"]), "partial output hash mismatch")
        require(payload["body_chars"] == len(payload["partial_output_text"]), "failed-call body_chars mismatch")
        require(isinstance(payload["retryable"], bool), "retryable must be boolean")
        call.update(
            {
                "state": "failed",
                "output_text": payload["partial_output_text"],
                "output_sha256": payload["partial_output_sha256"],
                "body_chars": payload["body_chars"],
                "failure": {"kind": payload["failure_kind"], "message": payload["message"], "retryable": payload["retryable"]},
                "terminal_event_id": event["event_id"],
                "terminal_at": event["recorded_at"],
            }
        )

    def _on_model_call_aborted(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"model_call_id", "human_action_id", "partial_output_text", "partial_output_sha256", "body_chars"}, "model_call_aborted.payload")
        call = self._call_for_event(payload, "model_call_aborted", event)
        action_id = payload["human_action_id"]
        require(action_id in self.human_actions, "aborted call needs prior human action")
        action = self.human_actions[action_id]
        require(action["action"] == "abort_model_call", "aborted call references wrong human action")
        require(action["target"].get("model_call_id") == payload["model_call_id"], "abort action targets another call")
        require(isinstance(payload["partial_output_text"], str), "partial_output_text must be a string")
        require_sha(payload["partial_output_sha256"], "partial_output_sha256")
        require(payload["partial_output_sha256"] == sha256_text(payload["partial_output_text"]), "partial output hash mismatch")
        require(payload["body_chars"] == len(payload["partial_output_text"]), "aborted-call body_chars mismatch")
        call.update(
            {
                "state": "aborted",
                "output_text": payload["partial_output_text"],
                "output_sha256": payload["partial_output_sha256"],
                "body_chars": payload["body_chars"],
                "failure": {"kind": "human_abort", "message": action["reason"], "retryable": True},
                "terminal_event_id": event["event_id"],
                "terminal_at": event["recorded_at"],
            }
        )

    def _on_step_revision_sealed(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(
            payload,
            {"step_revision_id", "branch_id", "step_slot", "revision", "replaces_step_revision_id", "content", "output_sha256", "origin"},
            "step_revision_sealed.payload",
        )
        step_id = require_id(payload["step_revision_id"], "step_revision_id")
        require(step_id not in self.steps, f"duplicate step_revision_id {step_id}")
        branch_id = payload["branch_id"]
        require(branch_id in self.branches, "step targets unknown branch")
        branch = self.branches[branch_id]
        require(branch["status"] == "active", "step may be sealed only on active branch")
        expected_slot = len(branch["step_revision_ids"]) + 1
        require(payload["step_slot"] == expected_slot, f"step_slot must be {expected_slot}")
        require(isinstance(payload["revision"], int) and payload["revision"] > 0, "revision must be positive")
        content = payload["content"]
        require(isinstance(content, dict), "step content must be an object")
        require_exact_keys(content, {"claim", "why", "source", "derivation", "scope"}, "step content")
        for field in ("claim", "why", "source", "derivation", "scope"):
            require_text(content[field], f"step content.{field}")
        require_sha(payload["output_sha256"], "step output_sha256")
        require(payload["output_sha256"] == sha256_text(canonical_json(content)), "step output hash mismatch")
        replaces = payload["replaces_step_revision_id"]
        if replaces is None:
            require(payload["revision"] == 1, "new step slot must start at revision 1")
        else:
            require(replaces in self.steps, "replacement targets unknown step revision")
            old = self.steps[replaces]
            require(branch["fork_mode"] == "replace" and branch["anchor_step_revision_id"] == replaces, "replacement step must be first step on replace branch")
            require(branch["step_revision_ids"] == branch["inherited_step_revision_ids"], "replacement must be the first new step on replace branch")
            require(payload["step_slot"] == old["step_slot"], "replacement must keep the old step slot")
            require(payload["revision"] == old["revision"] + 1, "replacement revision must increment by one")
        origin = payload["origin"]
        require(isinstance(origin, dict), "step origin must be an object")
        require_exact_keys(origin, {"kind", "model_call_id", "human_action_id"}, "step origin")
        if origin["kind"] == "model":
            require(event["actor"]["kind"] == "model", "model step must be sealed by model")
            call_id = origin["model_call_id"]
            require(call_id in self.calls, "model step needs known model call")
            call = self.calls[call_id]
            require(call["role"] == "writer" and call["state"] == "finished", "model step needs finished writer call")
            require(call["target"]["branch_id"] == branch_id and call["target"]["step_slot"] == payload["step_slot"], "writer call target mismatch")
            require(self._writer_output_sha256(call) == payload["output_sha256"], "writer call output hash differs from sealed step")
            require(origin["human_action_id"] is None, "model step cannot cite human action")
        elif origin["kind"] == "human":
            require(event["actor"]["kind"] == "human", "human step must be sealed by human")
            action_id = origin["human_action_id"]
            require(action_id in self.human_actions, "human step needs known human action")
            action = self.human_actions[action_id]
            require(action["action"] == "revise_step", "human step needs revise_step action")
            require(replaces is not None and action["target"].get("step_revision_id") == replaces, "human revision target mismatch")
            require(action["content_sha256"] == payload["output_sha256"], "human revision content hash mismatch")
            require(origin["model_call_id"] is None, "human step cannot cite model call")
        else:
            require(False, "invalid step origin kind")
        self.steps[step_id] = {
            **clone(payload),
            "sealed_event_id": event["event_id"],
            "sealed_at": event["recorded_at"],
        }
        branch["step_revision_ids"].append(step_id)

    def _on_check_requested(self, event: Mapping[str, Any]) -> None:
        require(event["actor"]["kind"] == "system", "check_requested actor must be system")
        payload = event["payload"]
        require_exact_keys(payload, {"check_id", "target_step_revision_id", "target_output_sha256", "required_for_candidate", "reason"}, "check_requested.payload")
        check_id = require_id(payload["check_id"], "check_id")
        require(check_id not in self.checks, f"duplicate check_id {check_id}")
        step_id = payload["target_step_revision_id"]
        require(step_id in self.steps, "check targets unknown step revision")
        require(payload["target_output_sha256"] == self.steps[step_id]["output_sha256"], "check target hash mismatch")
        require(isinstance(payload["required_for_candidate"], bool), "required_for_candidate must be boolean")
        require_text(payload["reason"], "check reason")
        if payload["required_for_candidate"]:
            for candidate in self.candidates.values():
                require(step_id not in candidate["transcript_step_revision_ids"], "cannot add required check after candidate snapshot")
        self.checks[check_id] = {
            **clone(payload),
            "state": "requested",
            "verdict": None,
            "checker_call_id": None,
            "completion_reason": None,
            "evidence": [],
            "requested_event_id": event["event_id"],
            "requested_at": event["recorded_at"],
        }

    def _validate_hard_defect_evidence(self, evidence: Any, check: Mapping[str, Any]) -> None:
        require(isinstance(evidence, list) and bool(evidence), "hard_defect requires quoted evidence")
        target_step = self.steps[check["target_step_revision_id"]]
        target_branch_id = target_step["branch_id"]
        target_transcript = set(self.branches[target_branch_id]["step_revision_ids"][: self.branches[target_branch_id]["step_revision_ids"].index(check["target_step_revision_id"]) + 1])
        target_lineage = set(self._branch_lineage(target_branch_id))
        for index, item in enumerate(evidence):
            require(isinstance(item, dict), f"hard-defect evidence {index} must be an object")
            require_exact_keys(item, {"kind", "source_id", "quote"}, f"hard-defect evidence {index}")
            kinds = HARD_DEFECT_EVIDENCE_KINDS | ({"literature_quote"} if self.is_v1_1 else set())
            require(item["kind"] in kinds, "invalid hard-defect evidence kind")
            require_text(item["source_id"], "hard-defect evidence source_id")
            quote = require_text(item["quote"], "hard-defect evidence quote")
            if item["kind"] == "literature_quote":
                require(item["source_id"] in self.source_evidence, "literature evidence is not in frozen sources")
                require(quote in self.source_evidence[item["source_id"]]["text"], "literature quote is not in registered source")
            elif item["kind"] in {"ancestor_quote", "scope_quote"}:
                source_id = item["source_id"]
                require(source_id in self.steps, "hard-defect step evidence source is unknown")
                require(source_id in target_transcript, "hard-defect step evidence is outside the checked transcript")
                source = self.steps[source_id]
                if item["kind"] == "scope_quote":
                    require(quote in source["content"]["scope"], "scope evidence quote is not in source")
                else:
                    require(quote in ancestor_evidence_text(source["content"], record_version="1.1" if self.is_v1_1 else "1.0"), "ancestor evidence quote is not in source")
            elif item["kind"] == "hypothesis_quote":
                source_id = item["source_id"]
                require(source_id in self.branches, "hypothesis evidence source is unknown")
                require(source_id in target_lineage, "hypothesis evidence branch is outside the checked lineage")
                require(quote in self.branches[source_id]["hypothesis"]["text"], "hypothesis evidence quote is not in source")
            else:
                require(item["source_id"] == self.run["task"]["id"], "task evidence source_id must equal the run task id")

    def _on_check_completed(self, event: Mapping[str, Any]) -> None:
        require(event["actor"]["kind"] == "checker", "check_completed actor must be checker")
        payload = event["payload"]
        require_exact_keys(payload, {"check_id", "target_step_revision_id", "target_output_sha256", "checker_call_id", "verdict", "reason", "evidence"}, "check_completed.payload")
        check_id = payload["check_id"]
        require(check_id in self.checks, "check_completed targets unknown check")
        check = self.checks[check_id]
        require(check["state"] == "requested", "check was already completed")
        require(payload["target_step_revision_id"] == check["target_step_revision_id"], "check completion step mismatch")
        require(payload["target_output_sha256"] == check["target_output_sha256"], "check completion hash mismatch")
        verdict = payload["verdict"]
        require(verdict in CHECK_VERDICTS, "invalid check verdict")
        require_text(payload["reason"], "check completion reason")
        call_id = payload["checker_call_id"]
        require(call_id in self.calls, "check completion needs checker call")
        call = self.calls[call_id]
        require(call["role"] == "checker" and call["target"]["check_id"] == check_id, "checker call target mismatch")
        if verdict == "instrument_failure":
            require(call["state"] in {"failed", "aborted"}, "checker instrument failure needs failed/aborted call")
            require(payload["evidence"] == [], "checker instrument failure cannot carry hard-defect evidence")
        else:
            require(call["state"] == "finished", "check verdict needs finished checker call")
            require(isinstance(payload["evidence"], list), "check evidence must be a list")
        if verdict == "hard_defect" or (self.is_v1_1 and payload["evidence"]):
            self._validate_hard_defect_evidence(payload["evidence"], check)
        check.update(
            {
                "state": "completed",
                "verdict": verdict,
                "checker_call_id": call_id,
                "completion_reason": payload["reason"],
                "evidence": clone(payload["evidence"]),
                "completed_event_id": event["event_id"],
                "completed_at": event["recorded_at"],
            }
        )
        self.check_completion_seq[check_id] = event["seq"]

    def _required_checks_for_transcript(self, step_ids: Sequence[str]) -> list[str]:
        step_set = set(step_ids)
        return sorted(
            check_id
            for check_id, check in self.checks.items()
            if check["required_for_candidate"] and check["target_step_revision_id"] in step_set
        )

    def _candidate_status(self, candidate: Mapping[str, Any]) -> str:
        checks = [self.checks[check_id] for check_id in candidate["required_check_ids"]]
        if any(check["state"] != "completed" for check in checks):
            return "provisional"
        if not self.is_v1_1 and any(check["verdict"] == "hard_defect" for check in checks):
            return "rejected"
        if any(check["verdict"] == "instrument_failure" for check in checks):
            return "blocked"
        if self.is_v1_1 and any(check["verdict"] in {"hard_defect", "objection"} for check in checks):
            return "conditional"
        return "eligible"

    def _on_candidate_declared(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(
            payload,
            {"candidate_id", "branch_id", "tip_step_revision_id", "transcript_step_revision_ids", "transcript_sha256", "required_check_ids", "declared_by", "reason"} | ({"unresolved_check_ids"} if self.is_v1_1 else set()),
            "candidate_declared.payload",
        )
        candidate_id = require_id(payload["candidate_id"], "candidate_id")
        require(candidate_id not in self.candidates, f"duplicate candidate_id {candidate_id}")
        branch_id = payload["branch_id"]
        require(branch_id in self.branches, "candidate targets unknown branch")
        branch = self.branches[branch_id]
        require(branch["status"] == "completed", "candidate requires completed branch")
        step_ids = payload["transcript_step_revision_ids"]
        require(isinstance(step_ids, list) and bool(step_ids) and all(isinstance(x, str) for x in step_ids), "candidate transcript must be a non-empty id list")
        require(step_ids == branch["step_revision_ids"], "candidate must freeze exact current branch transcript")
        require(payload["tip_step_revision_id"] == step_ids[-1], "candidate tip_step_revision_id mismatch")
        require_sha(payload["transcript_sha256"], "candidate transcript_sha256")
        require(payload["transcript_sha256"] == transcript_sha256(step_ids, self.steps), "candidate transcript hash mismatch")
        required_ids = payload["required_check_ids"]
        require(isinstance(required_ids, list) and all(isinstance(x, str) for x in required_ids), "required_check_ids must be a string list")
        require(sorted(required_ids) == self._required_checks_for_transcript(step_ids), "candidate omitted or added required check")
        if self.is_v1_1:
            unresolved = sorted(cid for cid in required_ids if self.checks[cid]["verdict"] in {"objection", "hard_defect"})
            require(payload["unresolved_check_ids"] == unresolved, "candidate omitted or altered unresolved checks")
        require(payload["declared_by"] in {"writer", "human"}, "invalid candidate declarer")
        expected_actor = "model" if payload["declared_by"] == "writer" else "human"
        require(event["actor"]["kind"] == expected_actor, "candidate declarer/actor mismatch")
        require_text(payload["reason"], "candidate reason")
        self.candidates[candidate_id] = {
            **clone(payload),
            "declared_event_id": event["event_id"],
            "declared_at": event["recorded_at"],
            "declared_seq": event["seq"],
        }

    def _on_judgement_requested(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"judgement_id", "candidate_id", "candidate_transcript_sha256", "requested_by", "reason"}, "judgement_requested.payload")
        judgement_id = require_id(payload["judgement_id"], "judgement_id")
        require(judgement_id not in self.judgements, f"duplicate judgement_id {judgement_id}")
        candidate_id = payload["candidate_id"]
        require(candidate_id in self.candidates, "judgement targets unknown candidate")
        candidate = self.candidates[candidate_id]
        require(payload["candidate_transcript_sha256"] == candidate["transcript_sha256"], "judgement candidate hash mismatch")
        require(self._candidate_status(candidate) in ({"eligible", "conditional"} if self.is_v1_1 else {"eligible"}), "judgement may be requested only for eligible or explicitly conditional candidate")
        require(payload["requested_by"] in {"system", "human"}, "invalid judgement requester")
        require(event["actor"]["kind"] == payload["requested_by"], "judgement requester/actor mismatch")
        require_text(payload["reason"], "judgement request reason")
        self.judgements[judgement_id] = {
            **clone(payload),
            "state": "requested",
            "verdict": None,
            "judge_call_id": None,
            "completion_reason": None,
            "score": None,
            "requested_event_id": event["event_id"],
            "requested_at": event["recorded_at"],
        }

    def _on_judgement_completed(self, event: Mapping[str, Any]) -> None:
        require(event["actor"]["kind"] == "judge", "judgement_completed actor must be judge")
        payload = event["payload"]
        require_exact_keys(payload, {"judgement_id", "candidate_id", "candidate_transcript_sha256", "judge_call_id", "verdict", "reason", "score"}, "judgement_completed.payload")
        judgement_id = payload["judgement_id"]
        require(judgement_id in self.judgements, "judgement_completed targets unknown judgement")
        judgement = self.judgements[judgement_id]
        require(judgement["state"] == "requested", "judgement was already completed")
        require(payload["candidate_id"] == judgement["candidate_id"], "judgement candidate mismatch")
        require(payload["candidate_transcript_sha256"] == judgement["candidate_transcript_sha256"], "judgement candidate hash mismatch")
        candidate = self.candidates[judgement["candidate_id"]]
        require(self._candidate_status(candidate) in ({"eligible", "conditional"} if self.is_v1_1 else {"eligible"}), "judgement completion requires eligible or explicitly conditional candidate")
        verdict = payload["verdict"]
        require(verdict in JUDGEMENT_VERDICTS, "invalid judgement verdict")
        require_text(payload["reason"], "judgement reason")
        score = payload["score"]
        require(score is None or (isinstance(score, (int, float)) and not isinstance(score, bool)), "judgement score must be numeric or null")
        call_id = payload["judge_call_id"]
        require(call_id in self.calls, "judgement completion needs judge call")
        call = self.calls[call_id]
        require(call["role"] == "judge" and call["target"]["judgement_id"] == judgement_id, "judge call target mismatch")
        if verdict == "instrument_failure":
            require(call["state"] in {"failed", "aborted"}, "judge instrument failure needs failed/aborted call")
        else:
            require(call["state"] == "finished", "judgement verdict needs finished judge call")
        judgement.update(
            {
                "state": "completed",
                "verdict": verdict,
                "judge_call_id": call_id,
                "completion_reason": payload["reason"],
                "score": score,
                "completed_event_id": event["event_id"],
                "completed_at": event["recorded_at"],
            }
        )

    def _on_selection_recorded(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        require_exact_keys(payload, {"selection_id", "candidate_id", "candidate_transcript_sha256", "judgement_id", "reason", "human_action_id"}, "selection_recorded.payload")
        selection_id = require_id(payload["selection_id"], "selection_id")
        require(selection_id not in self.selections, f"duplicate selection_id {selection_id}")
        require(not self.selections, "a record permits one selected submission per run")
        candidate_id = payload["candidate_id"]
        require(candidate_id in self.candidates, "selection targets unknown candidate")
        candidate = self.candidates[candidate_id]
        require(payload["candidate_transcript_sha256"] == candidate["transcript_sha256"], "selection candidate hash mismatch")
        require(self._candidate_status(candidate) == "eligible", "only eligible candidate may be selected")
        judgement_id = payload["judgement_id"]
        require(judgement_id in self.judgements, "selection needs known judgement")
        judgement = self.judgements[judgement_id]
        require(judgement["candidate_id"] == candidate_id, "selection judgement targets another candidate")
        require(judgement["candidate_transcript_sha256"] == candidate["transcript_sha256"], "selection judgement hash mismatch")
        require(judgement["state"] == "completed" and judgement["verdict"] == "pass", "selection requires completed pass judgement")
        require_text(payload["reason"], "selection reason")
        action_id = payload["human_action_id"]
        if event["actor"]["kind"] == "human":
            require(action_id in self.human_actions, "human selection needs select_candidate action")
            action = self.human_actions[action_id]
            require(action["action"] == "select_candidate", "selection references wrong human action")
            require(action["target"].get("candidate_id") == candidate_id, "selection action targets another candidate")
        else:
            require(event["actor"]["kind"] == "system", "selection actor must be human or system")
            require(action_id is None, "system selection cannot reference human action")
        self.selections[selection_id] = {
            **clone(payload),
            "actor": clone(event["actor"]),
            "event_id": event["event_id"],
            "recorded_at": event["recorded_at"],
            "seq": event["seq"],
        }

    def _final_checks(self) -> None:
        require(self.run is not None, "missing run_created")
        for call_id, call in self.calls.items():
            require(call["state"] in CALL_TERMINAL_STATES, f"model call {call_id} is still in flight at end of record")
        for selection in self.selections.values():
            candidate = self.candidates[selection["candidate_id"]]
            require(self._candidate_status(candidate) == "eligible", "selected candidate became ineligible")
            judgement = self.judgements[selection["judgement_id"]]
            require(judgement["state"] == "completed" and judgement["verdict"] == "pass", "selected judgement is not pass")

    def _branch_lineage(self, branch_id: str) -> list[str]:
        lineage: list[str] = []
        current: str | None = branch_id
        seen: set[str] = set()
        while current is not None:
            require(current not in seen, "branch lineage cycle")
            seen.add(current)
            lineage.append(current)
            current = self.branches[current]["parent_branch_id"]
        return list(reversed(lineage))

    def _branch_provenance(self, branch_id: str) -> dict[str, Any]:
        branch = self.branches[branch_id]
        lineage = self._branch_lineage(branch_id)
        steering: set[str] = set()
        edits: set[str] = set()
        for lineage_id in lineage:
            item = self.branches[lineage_id]
            if item["created_reason"] in {"human_direction", "human_hypothesis"} and item["human_action_id"]:
                steering.add(item["human_action_id"])
            if item["created_reason"] == "human_revision" and item["human_action_id"]:
                edits.add(item["human_action_id"])
        for step_id in branch["step_revision_ids"]:
            step = self.steps[step_id]
            if step["origin"]["kind"] == "human":
                edits.add(step["origin"]["human_action_id"])
        operational: set[str] = set()
        lineage_set = set(lineage)
        for action_id, action in self.human_actions.items():
            if action["target"].get("branch_id") in lineage_set and action["action"] in {"pause_branch", "resume_branch", "kill_branch"}:
                operational.add(action_id)
        if edits:
            content_class = "human_edited"
        elif steering:
            content_class = "human_steered"
        else:
            content_class = "model_only"
        return {
            "human_touched": bool(edits or steering),
            "content_class": content_class,
            "steering_action_ids": sorted(steering),
            "edit_action_ids": sorted(edits),
            "operational_action_ids": sorted(operational),
        }

    def _canonical_state(self) -> dict[str, Any]:
        # Replay is over, so lineage and provenance are now fixed for the run.
        # Each is asked for up to three times per branch below — once for the
        # branch, again for every candidate on it, again for a selection of that
        # candidate — and each answer walks the lineage and every human action.
        # Compute each one once.
        lineage_of = {branch_id: self._branch_lineage(branch_id) for branch_id in self.branches}
        provenance_of = {branch_id: self._branch_provenance(branch_id) for branch_id in self.branches}

        branches: list[dict[str, Any]] = []
        for branch_id in sorted(self.branches):
            branch = clone(self.branches[branch_id])
            branch["lineage_branch_ids"] = list(lineage_of[branch_id])
            branch["provenance"] = clone(provenance_of[branch_id])
            branches.append(branch)

        steps = [clone(self.steps[key]) for key in sorted(self.steps)]
        calls = [clone(self.calls[key]) for key in sorted(self.calls)]
        checks = [clone(self.checks[key]) for key in sorted(self.checks)]

        candidates: list[dict[str, Any]] = []
        for candidate_id in sorted(self.candidates):
            item = clone(self.candidates[candidate_id])
            item["status"] = self._candidate_status(item)
            item["provenance"] = clone(provenance_of[item["branch_id"]])
            candidates.append(item)

        judgements = [clone(self.judgements[key]) for key in sorted(self.judgements)]
        human_actions = [clone(self.human_actions[key]) for key in sorted(self.human_actions)]

        selections: list[dict[str, Any]] = []
        for selection_id in sorted(self.selections):
            item = clone(self.selections[selection_id])
            candidate = self.candidates[item["candidate_id"]]
            candidate_provenance = clone(provenance_of[candidate["branch_id"]])
            item["provenance"] = {
                **candidate_provenance,
                "selection_human_action_id": item["human_action_id"],
                "human_touched": candidate_provenance["human_touched"] or item["actor"]["kind"] == "human",
            }
            selections.append(item)

        candidate_status_counts = {status: 0 for status in ("provisional", "blocked", "rejected", "eligible")}
        if self.is_v1_1:
            candidate_status_counts["conditional"] = 0
        for candidate in candidates:
            candidate_status_counts[candidate["status"]] += 1

        return {
            "schema_version": CANONICAL_SCHEMA_VERSION_1_1 if self.is_v1_1 else CANONICAL_SCHEMA_VERSION,
            "run": clone(self.run),
            "branches": branches,
            "step_revisions": steps,
            "model_calls": calls,
            "checks": checks,
            "candidates": candidates,
            "judgements": judgements,
            "human_actions": human_actions,
            "selections": selections,
            **({"source_evidence": [clone(self.source_evidence[key]) for key in sorted(self.source_evidence)]} if self.is_v1_1 else {}),
            "summary": {
                "branch_count": len(branches),
                "step_revision_count": len(steps),
                "model_call_count": len(calls),
                "check_count": len(checks),
                "candidate_count": len(candidates),
                "judgement_count": len(judgements),
                "selection_count": len(selections),
                "candidate_status_counts": candidate_status_counts,
                "selected_candidate_id": selections[0]["candidate_id"] if selections else None,
            },
            "event_log": {
                "event_count": len(self.events),
                "head_event_sha256": self.events[-1]["event_sha256"],
            },
        }


def replay_events(events: Sequence[Mapping[str, Any]]) -> ReplayResult:
    return ReplayEngine().replay(events)
