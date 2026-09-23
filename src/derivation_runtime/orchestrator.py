"""Autonomous derivation-tree orchestrator over a provider-neutral runtime."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from derivation_agent_record import canonical_json, sha256_text

from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS
from .control import CallBookmark, ControlStore
from .evidence import evidence_catalog, validate_check_output
from .formula_normalization import (
    NORMALIZER_VERSION,
    SourceMacroTables,
    normalize_step_fields,
)
from .formula_validation import (
    WARNING_ONLY_CODES,
    EngineWhitelist,
    formula_v2_issues,
    load_engine_whitelist,
)
from .prompts import (
    FORK_SUPPRESSED_WRITER_NOTE,
    bounded_transcript,
    checker_user_prompt,
    judge_user_prompt,
    writer_user_prompt,
)
from .query import ReplayQuery
from .record import RecordV1Writer, raw_control_event_id, writer_control_from_call
from .types import (
    CheckEvidence,
    CheckOutput,
    CheckRequest,
    FormulaValidationResult,
    JudgeOutput,
    JudgeRequest,
    ModelRole,
    ModelRuntime,
    ProviderForkError,
    ProviderLineage,
    ReconcileResult,
    ReconcileStatus,
    RunConfig,
    RunPhase,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeInvocationError,
    RuntimeSession,
    StepContent,
    StepSnapshot,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
    WriterRequest,
)

# Failures of one *model-proposed* fork attempt.  ``ProviderForkError`` is the
# runtime saying the fork alone failed.  ``RuntimeInvocationError`` is a failed
# provider call, and the only call being made here is the optional one that
# would open an alternative branch: recording it as a refused alternative loses
# nothing the record needs, while a client that is genuinely broken still ends
# the run at the next call the derivation actually depends on.  Deliberately
# absent is ``RuntimeInvariantError`` - an orchestration transition that must
# not be papered over by continuing.
_FORK_ATTEMPT_FAILURES = (ProviderForkError, RuntimeInvocationError)

# The provider message of a turn refused because its thread no longer fits the
# model's context window (Codex App Server 0.147.0, turn error).  Record V1.1
# keeps a failed call's kind and message but not the provider's structured
# error code, and crash repair must decide from the Record alone, so the
# message is the classifier.  A reworded message after an upgrade only turns
# the retry below back into the earlier pause.
CONTEXT_WINDOW_EXHAUSTED_MESSAGE = "ran out of room in the model's context window"

FORMULA_V2_POLICY = "formula-v2"
# Format rewrites of one Writer slot under formula-v2. They are a separate
# budget from scientific repairs and provider retries; once spent, the output is
# sealed with its recorded format issues instead of pausing the run.
FORMULA_V2_MAX_FORMAT_REWRITES = 2


class DerivationOrchestrator:
    """Drive all accepted branches without per-step human confirmation."""

    def __init__(
        self,
        *,
        config: RunConfig,
        task_text: str,
        runtime: ModelRuntime,
        record: RecordV1Writer,
        control: ControlStore,
        provider_handles_live: bool = True,
        formula_validator: Callable[[StepContent], FormulaValidationResult]
        | None = None,
        formula_engine_whitelist: EngineWhitelist | None = None,
    ) -> None:
        if sha256_text(task_text) != config.task.sha256:
            raise RuntimeInvariantError(
                "task text does not match the frozen RunConfig hash"
            )
        if record.config != config:
            raise RuntimeInvariantError(
                "record writer and orchestrator use different RunConfig"
            )
        self.config = config
        self.task_text = task_text
        self.runtime = runtime
        self.record = record
        self.control = control
        self._provider_handles_live = provider_handles_live
        self._formula_validator = formula_validator
        # formula-v2 only; the committed whitelist is loaded on first use.
        self._formula_engine_whitelist = formula_engine_whitelist
        self._formula_tables: tuple[Any, SourceMacroTables] | None = None

    async def submit(self, root_hypothesis: str) -> ReplayQuery:
        await self.initialize(root_hypothesis)
        return await self.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)

    async def initialize(self, root_hypothesis: str) -> str:
        if self.record.events:
            raise RuntimeInvariantError("submit requires an empty Record V1 log")
        self.record.create_run()
        if self.config.record_version == "1.1":
            source_provider = getattr(self.runtime, "evidence_sources", None)
            if source_provider is not None:
                for source in source_provider():
                    self.record.register_source_evidence(source.source_id, source.text)
        self.control.ensure_run(self.config, phase=RunPhase.SUBMITTED)
        branch_id = self._next_id("br", [])
        self.record.create_root_branch(branch_id, root_hypothesis)
        self.control.upsert_branch(
            self.config.run_id,
            branch_id,
            runtime_state="active",
            provider_session_id=None,
            provider_lineage=None,
            last_operation_id=None,
            last_step_revision_id=None,
            attempt=0,
            last_error=None,
        )
        self.control.update_phase(
            self.config.run_id,
            RunPhase.SUBMITTED,
            stop_reason=None,
        )
        self._sync_head()
        return branch_id

    async def drive(self, *, phase: RunPhase) -> ReplayQuery:
        self.control.update_phase(self.config.run_id, phase, stop_reason=None)
        while True:
            self._repair_record_prefix()
            if (
                self.control.run(self.config.run_id).stop_reason
                == "formula_validation_failed"
            ):
                return self._strict_query()
            state = self.record.snapshot()
            failed_check = next(
                (
                    item
                    for item in state["checks"]
                    if item["verdict"] == "instrument_failure"
                    and item["required_for_candidate"]
                ),
                None,
            )
            if failed_check is not None:
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.PAUSED
                    if self.config.record_version == "1.1"
                    else RunPhase.ERROR,
                    stop_reason="checker_instrument_failure",
                    last_error=failed_check["completion_reason"],
                )
                self._sync_head()
                if self.config.record_version == "1.1":
                    return self._strict_query()
                raise RuntimeInvariantError(
                    "Checker instrument failure blocks scientific advancement."
                )
            if not state["branches"]:
                message = "Record contains run_created but no root branch"
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="invalid_record_prefix",
                    last_error=message,
                )
                self._sync_head()
                raise RuntimeInvariantError(message)
            run_bookmark = self.control.run(self.config.run_id)
            if run_bookmark.pause_requested:
                self._apply_soft_pause(
                    actor_id=run_bookmark.pause_actor_id or "runtime-user",
                    reason=run_bookmark.pause_reason or "Soft pause requested.",
                )
                return self._strict_query()

            active = self._active_branches(state)
            if not self._has_call_capacity(state):
                before = self.record.head
                for branch in active:
                    current = self._branch(self.record.snapshot(), branch["branch_id"])
                    if current["status"] == "active":
                        await self._advance_branch(
                            branch["branch_id"], allow_model_calls=False
                        )
                self._repair_record_prefix()
                if self.record.head != before:
                    continue
                state = self.record.snapshot()
                unresolved = bool(self._active_branches(state)) or any(
                    item["state"] == "requested" for item in state["judgements"]
                )
                terminal_phase = (
                    RunPhase.REVIEW_READY_DUE_TO_CAP
                    if unresolved
                    else RunPhase.REVIEW_READY
                )
                self.control.update_phase(
                    self.config.run_id,
                    terminal_phase,
                    stop_reason=(
                        "max_model_calls"
                        if terminal_phase is RunPhase.REVIEW_READY_DUE_TO_CAP
                        else None
                    ),
                )
                self._sync_head()
                return self._strict_query()

            pending_judgements = sorted(
                (
                    item
                    for item in state["judgements"]
                    if item["state"] == "requested"
                    and not any(
                        call["state"] == "started"
                        and call["role"] == ModelRole.JUDGE.value
                        and call["target"] == {"judgement_id": item["judgement_id"]}
                        for call in state["model_calls"]
                    )
                ),
                key=lambda item: (item["requested_event_id"], item["judgement_id"]),
            )
            if pending_judgements:
                await self._run_existing_judgement(
                    pending_judgements[0]["judgement_id"]
                )
                continue

            if not active:
                blocked = any(
                    item["status"] == "parked"
                    and item["status_history"][-1]["reason"] == "writer_blocked"
                    for item in state["branches"]
                )
                paused = any(item["status"] == "paused" for item in state["branches"])
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.PAUSED if blocked or paused else RunPhase.REVIEW_READY,
                    stop_reason="model_blocked"
                    if blocked
                    else ("runtime_failure" if paused else None),
                )
                self._sync_head()
                return self._strict_query()

            await self._advance_branch(active[0]["branch_id"], allow_model_calls=True)

    def request_soft_pause(self, *, actor_id: str, reason: str) -> None:
        self.control.request_pause(self.config.run_id, actor_id=actor_id, reason=reason)

    async def resume(self, *, actor_id: str, reason: str) -> ReplayQuery:
        state = self.record.snapshot()
        if self.config.formula_validation_policy not in {None, FORMULA_V2_POLICY}:
            audits = self.control.formula_audits(self.config.run_id)
            for call in state["model_calls"]:
                audit = audits.get(call["model_call_id"])
                if audit and audit["disposition"] == "infrastructure_failure":
                    self._formula_gate(
                        call["model_call_id"],
                        audit["branch_id"],
                        audit["step_slot"],
                        self._parse_step_content(
                            call["output_text"], context="formula recheck"
                        ),
                        recheck_infrastructure=True,
                    )
        paused = [item for item in state["branches"] if item["status"] == "paused"]
        for branch in paused:
            action_id = self._next_id(
                "act",
                [item["action_id"] for item in self.record.snapshot()["human_actions"]],
            )
            self.record.record_human_action(
                actor_id=actor_id,
                action_id=action_id,
                action="resume_branch",
                target={"branch_id": branch["branch_id"]},
                reason=reason,
                content=None,
            )
            self.record.change_branch_status(
                branch_id=branch["branch_id"],
                from_status="paused",
                to_status="active",
                reason_code="human_resume",
                actor={"kind": "human", "id": actor_id},
                human_action_id=action_id,
            )
            if self.config.record_version == "1.1":
                for check in self.record.snapshot()["checks"]:
                    if (
                        check["target_step_revision_id"] in branch["step_revision_ids"]
                        and check["verdict"] == "instrument_failure"
                        and check["required_for_candidate"]
                    ):
                        self.record.authorize_check_retry(check["check_id"], action_id)
            self.control.upsert_branch(
                self.config.run_id,
                branch["branch_id"],
                runtime_state="active",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=None,
                attempt=0,
                last_error=None,
            )
        self.control.clear_pause(self.config.run_id)
        self._sync_head()
        return await self.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)

    async def expand_from_step(
        self,
        *,
        parent_branch_id: str,
        step_revision_id: str,
        direction: str,
        actor_id: str,
        reason: str,
    ) -> ReplayQuery:
        query = self._strict_query()
        if query.status().phase not in {
            RunPhase.REVIEW_READY,
            RunPhase.REVIEW_READY_DUE_TO_CAP,
        }:
            raise RuntimeInvariantError(
                "human expansion is allowed only from review_ready"
            )
        state = self.record.snapshot()
        branches = {item["branch_id"]: item for item in state["branches"]}
        if parent_branch_id not in branches:
            raise RuntimeInvariantError("human expansion targets an unknown branch")
        parent = branches[parent_branch_id]
        if step_revision_id not in parent["step_revision_ids"]:
            raise RuntimeInvariantError(
                "human expansion anchor is not sealed on the selected route"
            )
        if not self._has_call_capacity(state):
            raise RuntimeInvariantError(
                "human expansion cannot run under an exhausted frozen call cap; start a new Run"
            )
        if (
            self.config.max_active_branches is not None
            and len(self._active_branches(state)) >= self.config.max_active_branches
        ):
            raise RuntimeInvariantError(
                "max_active_branches leaves no room for human expansion"
            )

        action_id = self._next_id(
            "act", [item["action_id"] for item in state["human_actions"]]
        )
        action_event = self.record.record_human_action(
            actor_id=actor_id,
            action_id=action_id,
            action="set_direction",
            target={"branch_id": parent_branch_id},
            reason=reason,
            content=direction,
        )
        inherited = parent["step_revision_ids"][
            : parent["step_revision_ids"].index(step_revision_id) + 1
        ]
        child_session = await self._fork_or_rehydrate(step_revision_id, inherited)
        branch_id = self._next_id(
            "br", [item["branch_id"] for item in self.record.snapshot()["branches"]]
        )
        self.record.create_child_branch(
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            fork_mode="after",
            anchor_step_revision_id=step_revision_id,
            inherited_step_revision_ids=inherited,
            hypothesis=direction,
            hypothesis_source="human",
            hypothesis_source_event_id=action_event["event_id"],
            created_reason="human_direction",
            human_action_id=action_id,
            actor={"kind": "human", "id": actor_id},
        )
        self.control.upsert_branch(
            self.config.run_id,
            branch_id,
            runtime_state="active",
            provider_session_id=child_session.session_id,
            provider_lineage=child_session.lineage.value,
            last_operation_id=None,
            last_step_revision_id=step_revision_id,
            attempt=0,
            last_error=None,
        )
        self._sync_head()
        return await self.drive(phase=RunPhase.HUMAN_EXPANSION)

    async def revise_step(
        self,
        *,
        parent_branch_id: str,
        step_revision_id: str,
        replacement: StepContent,
        actor_id: str,
        reason: str,
    ) -> ReplayQuery:
        """Replace a sealed step on a new branch, then continue autonomously."""

        query = self._strict_query()
        if query.status().phase not in {
            RunPhase.REVIEW_READY,
            RunPhase.REVIEW_READY_DUE_TO_CAP,
        }:
            raise RuntimeInvariantError(
                "human revision is allowed only from review_ready"
            )
        state = self.record.snapshot()
        branches = {item["branch_id"]: item for item in state["branches"]}
        if parent_branch_id not in branches:
            raise RuntimeInvariantError("human revision targets an unknown branch")
        parent = branches[parent_branch_id]
        if step_revision_id not in parent["step_revision_ids"]:
            raise RuntimeInvariantError(
                "human revision anchor is not sealed on the selected route"
            )
        if not self._has_call_capacity(state):
            raise RuntimeInvariantError(
                "human revision cannot run under an exhausted frozen call cap; start a new Run"
            )
        if (
            self.config.max_active_branches is not None
            and len(self._active_branches(state)) >= self.config.max_active_branches
        ):
            raise RuntimeInvariantError(
                "max_active_branches leaves no room for human revision"
            )

        old_step = self._step(state, step_revision_id)
        action_id = self._next_id(
            "act", [item["action_id"] for item in state["human_actions"]]
        )
        action_event = self.record.record_human_action(
            actor_id=actor_id,
            action_id=action_id,
            action="revise_step",
            target={"step_revision_id": step_revision_id},
            reason=reason,
            content=canonical_json(replacement.to_record()),
        )
        anchor_index = parent["step_revision_ids"].index(step_revision_id)
        inherited = parent["step_revision_ids"][:anchor_index]
        branch_id = self._next_id(
            "br", [item["branch_id"] for item in self.record.snapshot()["branches"]]
        )
        self.record.create_child_branch(
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            fork_mode="replace",
            anchor_step_revision_id=step_revision_id,
            inherited_step_revision_ids=inherited,
            hypothesis=f"Human replacement of {step_revision_id}: {reason}",
            hypothesis_source="human",
            hypothesis_source_event_id=action_event["event_id"],
            created_reason="human_revision",
            human_action_id=action_id,
            actor={"kind": "human", "id": actor_id},
        )
        replacement_id = self._next_id(
            "step",
            [
                item["step_revision_id"]
                for item in self.record.snapshot()["step_revisions"]
            ],
        )
        self.record.seal_human_revision(
            step_revision_id=replacement_id,
            branch_id=branch_id,
            step_slot=old_step["step_slot"],
            revision=old_step["revision"] + 1,
            replaces_step_revision_id=step_revision_id,
            content=replacement,
            human_action_id=action_id,
            actor_id=actor_id,
        )
        # A replace branch intentionally starts without a native provider
        # session: rehydrating the exact new transcript avoids retaining the
        # replaced turn in provider history.
        self.control.upsert_branch(
            self.config.run_id,
            branch_id,
            runtime_state="active",
            provider_session_id=None,
            provider_lineage=None,
            last_operation_id=None,
            last_step_revision_id=replacement_id,
            attempt=0,
            last_error=None,
        )
        self._sync_head()
        return await self.drive(phase=RunPhase.HUMAN_EXPANSION)

    async def hard_interrupt(
        self, *, model_call_id: str, actor_id: str, reason: str
    ) -> None:
        state = self.record.snapshot()
        calls = {item["model_call_id"]: item for item in state["model_calls"]}
        call = calls.get(model_call_id)
        if call is None or call["state"] != "started":
            raise RuntimeInvariantError(
                "hard interrupt requires an in-flight Record ModelCall"
            )
        bookmark = self.control.call(self.config.run_id, model_call_id)
        if (
            bookmark is None
            or bookmark.provider_session_id is None
            or bookmark.provider_operation_id is None
        ):
            raise RuntimeInvariantError("hard interrupt has no live provider handle")
        invocation = self._invocation_from_bookmark(bookmark)
        action_id = self._next_id(
            "act", [item["action_id"] for item in state["human_actions"]]
        )
        self.record.record_human_action(
            actor_id=actor_id,
            action_id=action_id,
            action="abort_model_call",
            target={"model_call_id": model_call_id},
            reason=reason,
            content=None,
        )
        interrupted = await self.runtime.interrupt(invocation)
        terminal = self.record.abort_model_call(
            model_call_id=model_call_id,
            role=ModelRole(call["role"]),
            human_action_id=action_id,
            partial_output=interrupted.partial_output,
        )
        self.control.finish_call(
            self.config.run_id,
            model_call_id,
            state="aborted",
            record_terminal_seq=terminal["seq"],
            last_error=reason,
        )
        if call["role"] == ModelRole.WRITER.value and not interrupted.partial_output:
            branch_id = call["target"]["branch_id"]
            self.record.change_branch_status(
                branch_id=branch_id,
                from_status="active",
                to_status="parked",
                reason_code="instrument_failure",
                actor=RecordV1Writer.SYSTEM,
                model_call_id=model_call_id,
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="parked",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=None,
                attempt=bookmark.attempt,
                last_error=reason,
            )
        self._sync_head()

    async def reconcile_in_flight(self) -> ReplayQuery | None:
        try:
            self.control.run(self.config.run_id)
        except KeyError:
            self.control.ensure_run(self.config, phase=RunPhase.RECOVERING)
        self.control.update_phase(
            self.config.run_id, RunPhase.RECOVERING, stop_reason=None
        )
        # An abort intent advances the Record head before the provider is
        # interrupted.  Consume that prefix while the old control bookmark
        # still owns the live provider handle; rebuilding first would discard
        # the only evidence needed to perform a real interrupt.
        await self._repair_orphan_abort_actions()
        record_seq, record_sha = self.record.head
        bookmark = self.control.run(self.config.run_id)
        if (
            bookmark.record_event_seq != record_seq
            or bookmark.record_event_sha256 != record_sha
        ):
            snapshot = self.record.snapshot()
            self.control.reconcile_from_snapshot(snapshot)

        state = self.record.snapshot()
        in_flight = [
            item for item in state["model_calls"] if item["state"] == "started"
        ]
        any_running = False
        for call in in_flight:
            call_id = call["model_call_id"]
            control_call = self.control.call(self.config.run_id, call_id)
            if (
                control_call is None
                or control_call.provider_session_id is None
                or control_call.provider_operation_id is None
            ):
                outcome = ReconcileResult(
                    status=ReconcileStatus.MISSING,
                    output=None,
                    partial_output="",
                    failure_kind="unknown_provider_state",
                    message="control bookmark has no provider handle",
                    retryable=True,
                )
            else:
                outcome = await self.runtime.reconcile(
                    self._invocation_from_bookmark(control_call)
                )
            if outcome.status is ReconcileStatus.RUNNING:
                any_running = True
                continue
            if (
                self.config.record_version == "1.1"
                and outcome.status is ReconcileStatus.MISSING
            ):
                # A missing provider handle does not prove that the remote
                # invocation failed. Preserve its started record for recovery.
                any_running = True
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.RECOVERING,
                    stop_reason="unknown_provider_state",
                    last_error=outcome.message
                    or "Provider outcome is not known; no retry was started.",
                )
                continue
            await self._apply_reconcile_outcome(call, control_call, outcome)

        self._sync_head()
        if any_running:
            return None
        return await self.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)

    async def _advance_branch(
        self, branch_id: str, *, allow_model_calls: bool = True
    ) -> None:
        state = self.record.snapshot()
        branch = self._branch(state, branch_id)
        if branch["status"] != "active":
            raise RuntimeInvariantError("scheduler selected a non-active branch")
        if branch["step_revision_ids"]:
            last_step_id = branch["step_revision_ids"][-1]
            last_step = self._step(state, last_step_id)
            if (
                self.config.record_version == "1.1"
                and last_step["branch_id"] == branch_id
                and last_step["origin"]["kind"] == "model"
            ):
                call = self._call_for_step(state, last_step_id)
                if writer_control_from_call(call).decision is WriterDecision.BLOCKED:
                    self.record.change_branch_status(
                        branch_id=branch_id,
                        from_status="active",
                        to_status="parked",
                        reason_code="writer_blocked",
                        actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
                        model_call_id=call["model_call_id"],
                    )
                    self._sync_head()
                    return
            check = self._required_check_for_step(state, last_step_id)
            if check is None and self.config.checker_enabled:
                if allow_model_calls and self._has_call_capacity(state):
                    await self._run_check(branch_id, last_step_id)
                return
            if check is not None and check["state"] == "requested":
                if allow_model_calls and self._has_call_capacity(state):
                    await self._run_existing_check(branch_id, check["check_id"])
                return
            if check is not None and check["verdict"] == "instrument_failure":
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="checker_instrument_failure",
                    last_error=check["completion_reason"],
                )
                self._sync_head()
                raise RuntimeInvariantError(
                    "Checker instrument failure blocks scientific advancement."
                )
            if (
                check is not None
                and check["verdict"] == "hard_defect"
                and self.config.record_version == "1.0"
            ):
                self.record.change_branch_status(
                    branch_id=branch_id,
                    from_status="active",
                    to_status="killed",
                    reason_code="hard_defect",
                    actor=RecordV1Writer.ACTORS[ModelRole.CHECKER],
                    check_id=check["check_id"],
                )
                self.control.upsert_branch(
                    self.config.run_id,
                    branch_id,
                    runtime_state="killed",
                    provider_session_id=None,
                    provider_lineage=None,
                    last_operation_id=None,
                    last_step_revision_id=last_step_id,
                    attempt=0,
                    last_error=check["completion_reason"],
                )
                self._sync_head()
                return

            # An inherited tip is context, not a fresh instruction for the
            # child. Replaying the parent's fork directive here would create a
            # recursive duplicate branch at every descendant.
            if (
                last_step["branch_id"] == branch_id
                and last_step["origin"]["kind"] == "model"
            ):
                call = self._call_for_step(state, last_step_id)
                control = writer_control_from_call(call)
                if control.decision is WriterDecision.FORK:
                    await self._ensure_model_forks(
                        branch, last_step_id, call, control.alternatives
                    )
                    state = self.record.snapshot()
                elif control.decision is WriterDecision.COMPLETE:
                    # Give a completed but newly disputed segment one feedback
                    # turn. The Writer can repair it or finish conditionally.
                    if (
                        self.config.record_version == "1.1"
                        and check is not None
                        and check["verdict"] in {"objection", "hard_defect"}
                    ):
                        if allow_model_calls and self._has_call_capacity(state):
                            await self._run_writer(branch_id)
                        return
                    await self._complete_branch(branch_id)
                    return

        if allow_model_calls and self._has_call_capacity(self.record.snapshot()):
            await self._run_writer(branch_id)

    async def _run_writer(self, branch_id: str) -> None:
        state = self.record.snapshot()
        branch = self._branch(state, branch_id)
        step_slot = len(branch["step_revision_ids"]) + 1
        full_transcript = self._transcript(state, branch["step_revision_ids"])
        feedback = self._writer_feedback(state, branch)
        preparation = None
        preparation_provider = getattr(self.runtime, "writer_preparation", None)
        if callable(preparation_provider):
            preparation = preparation_provider(include_full=step_slot == 1)
        request = WriterRequest(
            run_id=self.config.run_id,
            branch_id=branch_id,
            step_slot=step_slot,
            task_text=self.task_text,
            hypothesis=branch["hypothesis"]["text"],
            transcript=bounded_transcript(full_transcript)
            if self.config.record_version == "1.1"
            else full_transcript,
            full_transcript=full_transcript,
            granularity=self.config.granularity,
            checker_enabled=self.config.checker_enabled,
            checker_feedback=tuple(
                {
                    **item,
                    "reason": item["reason"][:1500],
                    "evidence": [
                        {**evidence, "quote": evidence["quote"][:700]}
                        for evidence in item["evidence"][:2]
                    ],
                    "full_feedback_readable": True,
                }
                for item in feedback[-16:]
            ),
            full_checker_feedback=feedback,
            repair_attempts=max(
                (item["repair_attempts"] for item in feedback), default=0
            ),
            max_local_repairs=self.config.max_local_repairs,
            exhausted_revision_ids=self._exhausted_revision_ids(state, branch),
            record_version=self.config.record_version,
            preparation_context=preparation,
            intent_ledger_first=self.config.intent_ledger_first,
            dimension_check=self.config.dimension_check,
            fork_available=self.config.max_active_branches != 1,
            runtime_notes=self._fork_suppression_note(state, branch),
            formula_validation_policy=self.config.formula_validation_policy,
            formula_feedback=self._formula_feedback(branch_id, step_slot),
        )
        prompt = self._writer_prompt(request)
        target_calls = self._calls_on_target(
            state, ModelRole.WRITER, {"branch_id": branch_id, "step_slot": step_slot}
        )
        # A turn the provider refused for want of context room is retried on a
        # fresh thread rehydrated from the Record, as a context rotation is;
        # the refused thread would refuse it again.
        fresh_thread = (
            self.config.record_version == "1.1"
            and bool(target_calls)
            and self._context_window_exhausted(target_calls[-1])
        )
        call_id = self._next_id(
            "call", [item["model_call_id"] for item in state["model_calls"]]
        )
        start = self.record.start_model_call(
            model_call_id=call_id,
            role=ModelRole.WRITER,
            target={"branch_id": branch_id, "step_slot": step_slot},
            prompt=prompt,
        )
        attempt = self.control.next_attempt(
            self.config.run_id, "writer", "writer_step", f"{branch_id}:{step_slot}"
        )
        self.control.start_call(
            run_id=self.config.run_id,
            model_call_id=call_id,
            branch_id=branch_id,
            role="writer",
            target_kind="writer_step",
            target_id=f"{branch_id}:{step_slot}",
            attempt=attempt,
            record_start_seq=start["seq"],
        )
        self._sync_head()
        try:
            session = await self._writer_session(
                branch_id,
                request.transcript,
                rehydrate_if_missing=attempt > 1 or fresh_thread,
                rotate_context=self.config.record_version == "1.1"
                and (
                    request.transcript != full_transcript
                    or len(full_transcript) >= 8
                    or fresh_thread
                ),
            )
            invocation = await self.runtime.start_writer(request, session)
            self.control.attach_invocation(self.config.run_id, call_id, invocation)
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="running",
                provider_session_id=invocation.session.session_id,
                provider_lineage=invocation.session.lineage.value,
                last_operation_id=invocation.operation_id,
                last_step_revision_id=None,
                attempt=attempt,
                last_error=None,
            )
            output = await self.runtime.collect_writer(invocation)
        except RuntimeInvocationError as exc:
            await self._handle_call_failure(
                call_id=call_id,
                role=ModelRole.WRITER,
                branch_id=branch_id,
                target_id=f"{branch_id}:{step_slot}",
                attempt=attempt,
                error=exc,
            )
            return
        await self._finish_writer(call_id, branch_id, step_slot, invocation, output)

    def _materialize_model_revision(
        self,
        call_id: str,
        parent_branch_id: str,
        step_id: str,
        content: StepContent,
        control: WriterControl,
    ) -> str | None:
        if self.config.record_version != "1.1":
            raise RuntimeInvariantError("model revisions require Record 1.1")
        state = self.record.snapshot()
        branch_id = self._next_id(
            "br", [item["branch_id"] for item in state["branches"]]
        )
        assert (
            control.revise_step_revision_id is not None and control.reason is not None
        )
        parent = self._branch(state, parent_branch_id)
        if control.revise_step_revision_id in self._exhausted_revision_ids(
            state, parent
        ):
            self.record.defer_model_revision(
                model_call_id=call_id,
                target_step_revision_id=control.revise_step_revision_id,
                reason="Local repair limit reached on this step lineage. Preserve the unresolved issue; advance a different subgoal before reopening it, or declare blocked.",
            )
            return None
        self.record.apply_model_revision(
            parent_branch_id=parent_branch_id,
            branch_id=branch_id,
            target_step_revision_id=control.revise_step_revision_id,
            step_revision_id=step_id,
            model_call_id=call_id,
            reason=control.reason,
            content=content,
        )
        self.control.upsert_branch(
            self.config.run_id,
            parent_branch_id,
            runtime_state="parked",
            provider_session_id=None,
            provider_lineage=None,
            last_operation_id=None,
            last_step_revision_id=None,
            attempt=0,
            last_error="Superseded by immutable model revision.",
        )
        return branch_id

    def _complete_unchanged_route(
        self, call_id: str, branch_id: str, content: StepContent, control: WriterControl
    ) -> bool:
        if (
            self.config.record_version != "1.1"
            or control.decision is not WriterDecision.COMPLETE
        ):
            return False
        state = self.record.snapshot()
        branch = self._branch(state, branch_id)
        if not branch["step_revision_ids"]:
            return False
        tip = self._step(state, branch["step_revision_ids"][-1])
        if tip["content"] != content.to_record():
            return False
        self.record.complete_unchanged_route(
            branch_id, call_id, tip["step_revision_id"]
        )
        self._sync_head()
        return True

    def _exhausted_revision_ids(
        self, state: Mapping[str, Any], branch: Mapping[str, Any]
    ) -> tuple[str, ...]:
        if self.config.record_version != "1.1":
            return ()
        steps = {item["step_revision_id"]: item for item in state["step_revisions"]}
        # A new ordinary segment is explicit intervening progress. Until then,
        # changing Checker wording or verdict cannot reset a repair lineage.
        progress_seq = max(
            (
                int(steps[sid]["sealed_event_id"].split("_")[-1])
                for sid in branch["step_revision_ids"]
                if steps[sid]["replaces_step_revision_id"] is None
            ),
            default=0,
        )
        exhausted = []
        for sid in branch["step_revision_ids"]:
            current, count = sid, 0
            while steps[current]["replaces_step_revision_id"] is not None:
                if int(steps[current]["sealed_event_id"].split("_")[-1]) > progress_seq:
                    count += 1
                current = steps[current]["replaces_step_revision_id"]
            if count >= self.config.max_local_repairs:
                exhausted.append(sid)
        return tuple(exhausted)

    def _writer_feedback(
        self,
        state: Mapping[str, Any],
        branch: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        if self.config.record_version != "1.1":
            return ()
        steps = {s["step_revision_id"]: s for s in state["step_revisions"]}
        lineage: dict[str, tuple[str, int]] = {}
        for sid in branch["step_revision_ids"]:
            current, count = sid, 0
            while steps[current]["replaces_step_revision_id"] is not None:
                current = steps[current]["replaces_step_revision_id"]
                count += 1
            lineage[sid] = (current, count)
        return tuple(
            {
                "check_id": check["check_id"],
                "step_revision_id": check["target_step_revision_id"],
                "issue_anchor": lineage[check["target_step_revision_id"]][0],
                "repair_attempts": lineage[check["target_step_revision_id"]][1],
                "verdict": check["verdict"],
                "reason": check["completion_reason"],
                "evidence": check["evidence"],
                "disposition": "unresolved_after_local_repairs"
                if lineage[check["target_step_revision_id"]][1]
                >= self.config.max_local_repairs
                else "address_or_repair",
                "confirmed_refutation": False,
            }
            for check in state["checks"]
            if check["target_step_revision_id"] in lineage
            and check["state"] == "completed"
            and check["verdict"] in {"objection", "hard_defect"}
        )

    async def _finish_writer(
        self,
        call_id: str,
        branch_id: str,
        step_slot: int,
        invocation: RuntimeInvocation,
        output: WriterOutput,
    ) -> None:
        raw = canonical_json(output.control.to_record())
        body = canonical_json(output.content.to_record())
        self._ensure_writer_chunks(
            call_id,
            (("raw", raw), ("body", body)),
        )
        terminal = self.record.finish_model_call(
            model_call_id=call_id,
            role=ModelRole.WRITER,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )
        self.control.finish_call(
            self.config.run_id,
            call_id,
            state="finished",
            record_terminal_seq=terminal["seq"],
            last_error=None,
        )
        self._sync_head()
        content = output.content
        if self.config.formula_validation_policy == FORMULA_V2_POLICY:
            gated = self._formula_gate_v2(call_id, branch_id, step_slot, content)
            if gated is None:
                return
            # Everything downstream (seal, Checker, transcripts) uses the
            # recorded normalized content; the raw call stays immutable.
            content = gated
        elif not self._formula_gate(call_id, branch_id, step_slot, content):
            return
        if self._complete_unchanged_route(call_id, branch_id, content, output.control):
            return
        step_id = self._next_id(
            "step",
            [
                item["step_revision_id"]
                for item in self.record.snapshot()["step_revisions"]
            ],
        )
        if output.control.decision is WriterDecision.REVISE:
            branch_id = self._materialize_model_revision(
                call_id, branch_id, step_id, content, output.control
            )
            if branch_id is None:
                self._sync_head()
                return
        else:
            self.record.seal_model_step(
                step_revision_id=step_id,
                branch_id=branch_id,
                step_slot=step_slot,
                content=content,
                model_call_id=call_id,
            )
        if output.control.decision is not WriterDecision.REVISE:
            self.control.record_step(self.config.run_id, step_id, branch_id, invocation)
        self.control.upsert_branch(
            self.config.run_id,
            branch_id,
            runtime_state="active",
            provider_session_id=None
            if output.control.decision is WriterDecision.REVISE
            else invocation.session.session_id,
            provider_lineage=None
            if output.control.decision is WriterDecision.REVISE
            else invocation.session.lineage.value,
            last_operation_id=invocation.operation_id,
            last_step_revision_id=step_id,
            attempt=0,
            last_error=None,
        )
        self._sync_head()

    def _formula_feedback(
        self, branch_id: str, step_slot: int
    ) -> tuple[Mapping[str, Any], ...]:
        if not self.config.formula_validation_policy:
            return ()
        audits = self.control.formula_audits(self.config.run_id)
        matching = [
            audit
            for audit in audits.values()
            if audit["branch_id"] == branch_id
            and audit["step_slot"] == step_slot
            and audit["disposition"] == "rejected"
        ]
        if not matching:
            return ()
        if self.config.formula_validation_policy == FORMULA_V2_POLICY:
            issues = matching[-1]["issues"]
            # Blocking diagnostics ask for the rewrite. A possible escape
            # swallow has no deterministic repair - the command letter is
            # gone - so only the author can say what was meant; it rides along
            # as advice while the Writer is already correcting this slot, and
            # it neither spends a format rewrite nor blocks sealing.
            return tuple(
                [issue for issue in issues if issue.get("severity") == "error"]
                + [
                    {**issue, "advisory": True}
                    for issue in issues
                    if issue.get("severity") != "error"
                    and issue.get("code") in WARNING_ONLY_CODES
                ]
            )
        return tuple(matching[-1]["issues"])

    def _formula_whitelist(self) -> EngineWhitelist:
        if self._formula_engine_whitelist is None:
            try:
                self._formula_engine_whitelist = load_engine_whitelist()
            except (OSError, ValueError) as exc:
                raise RuntimeInvariantError(
                    f"formula-v2 engine whitelist is unavailable: {exc}"
                ) from exc
        return self._formula_engine_whitelist

    def _formula_source_tables(
        self, state: Mapping[str, Any]
    ) -> tuple[dict[str, str], SourceMacroTables]:
        sources = {
            item["source_id"]: item["text"] for item in state.get("source_evidence", ())
        }
        provider = getattr(self.runtime, "evidence_source_documents", None)
        documents = dict(provider()) if callable(provider) else {}
        documents = {key: value for key, value in documents.items() if key in sources}
        key = (
            tuple(
                (item["source_id"], item["sha256"])
                for item in state.get("source_evidence", ())
            ),
            tuple(sorted(documents.items())),
        )
        if self._formula_tables is None or self._formula_tables[0] != key:
            self._formula_tables = (key, SourceMacroTables(sources, documents))
        return sources, self._formula_tables[1]

    def _formula_v2_normalization(
        self, state: Mapping[str, Any], raw: Mapping[str, str]
    ) -> tuple[
        dict[str, str],
        list[dict[str, Any]],
        frozenset[tuple[str, int]],
        dict[str, Any] | None,
    ]:
        """Normalized fields, recorded edits, prose quotations, audit report.

        Record 1.0 cannot carry a normalization event, so there the raw output
        is validated unchanged.
        """
        if self.config.record_version != "1.1":
            return dict(raw), [], frozenset(), None
        sources, tables = self._formula_source_tables(state)
        result = normalize_step_fields(
            raw,
            sources=sources,
            whitelist=self._formula_whitelist(),
            tables=tables,
        )
        report = result.report()
        if any(not value.strip() for value in result.fields.values()):
            # Removing corruption must never empty a field; keep the raw text
            # and let validation request a rewrite instead. The audit then has
            # to describe what was sealed, not what was computed and dropped:
            # an audit that claimed these edits would be a record of an edit
            # the Record never carries.
            return (
                dict(raw),
                [],
                result.quotation_fragments,
                {
                    **report,
                    "normalization_applied": False,
                    "withheld_reason": "normalization_emptied_a_field",
                    "withheld_replacement_count": report["replacement_count"],
                    "withheld_replacement_kinds": report["replacement_kinds"],
                    "replacement_count": 0,
                    "replacement_kinds": {},
                },
            )
        return (
            result.fields,
            result.replacement_records(),
            result.quotation_fragments,
            {**report, "normalization_applied": True},
        )

    def _formula_v2_reject(
        self,
        state: Mapping[str, Any],
        call_id: str,
        branch_id: str,
        step_slot: int,
        audit: Mapping[str, Any],
    ) -> None:
        """Leave a rejected output unsealed and ask for a rewrite of its slot."""
        same_target = [
            call
            for call in state["model_calls"]
            if call["role"] == "writer"
            and call["target"] == {"branch_id": branch_id, "step_slot": step_slot}
        ]
        if same_target and same_target[-1]["model_call_id"] != call_id:
            return
        branch = self._branch(state, branch_id)
        if branch["status"] == "active":
            bookmark = self.control.branch(self.config.run_id, branch_id)
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=bookmark.provider_session_id if bookmark else None,
                provider_lineage=bookmark.provider_lineage if bookmark else None,
                last_operation_id=bookmark.last_operation_id if bookmark else None,
                last_step_revision_id=bookmark.last_step_revision_id
                if bookmark
                else None,
                attempt=audit["format_rewrites_used"] + 1,
                last_error="Formula format correction required",
            )
        self._sync_head()
        return

    def _formula_gate_v2(
        self,
        call_id: str,
        branch_id: str,
        step_slot: int,
        content: StepContent,
    ) -> StepContent | None:
        """formula-v2: normalize, validate statically, never pause for format.

        Returns the content to seal, or None when this output is rejected for a
        format rewrite in the same slot. The audit stores the raw output hash
        and the normalized hash; the normalization itself is written to Record
        before the step it produces, so replay verifies every edit.
        """
        policy = FORMULA_V2_POLICY
        raw = content.to_record()
        raw_hash = sha256_text(canonical_json(raw))
        audits = self.control.formula_audits(self.config.run_id)
        audit = audits.get(call_id)
        if audit is not None and (
            audit["policy"] != policy or audit["output_sha256"] != raw_hash
        ):
            raise RuntimeInvariantError("formula audit policy or output hash mismatch")
        state = self.record.snapshot()
        if audit is not None and audit["disposition"] == "rejected":
            # A rejected output is never sealed, so a restart does not need to
            # (and after a normalizer upgrade could not) reproduce its edits.
            self._formula_v2_reject(state, call_id, branch_id, step_slot, audit)
            return None
        call = next(
            item for item in state["model_calls"] if item["model_call_id"] == call_id
        )
        recorded = call.get("normalized_output")
        if audit is not None and recorded is not None:
            # Record is the source of truth once the normalization is written:
            # seal exactly the replay-verified content, whatever the current
            # normalizer would produce.
            if recorded["output_sha256"] != audit["normalized_output_sha256"]:
                raise RuntimeInvariantError(
                    "recorded Writer normalization differs from the formula audit"
                )
            (event,) = [
                item
                for item in self.record.events
                if item["type"] == "writer_output_normalized"
                and item["payload"]["model_call_id"] == call_id
            ]
            return StepContent(**event["payload"]["content"])
        fields, replacements, quotations, report = self._formula_v2_normalization(
            state, raw
        )
        normalized_hash = sha256_text(canonical_json(fields))
        whitelist = self._formula_whitelist()
        if audit is None:
            issues = formula_v2_issues(
                fields, whitelist=whitelist, quotation_fragments=quotations
            )
            rewrites_used = sum(
                item["policy"] == policy
                and item["branch_id"] == branch_id
                and item["step_slot"] == step_slot
                and item["disposition"] == "rejected"
                for item in audits.values()
            )
            disposition = (
                "accepted"
                if not any(issue["severity"] == "error" for issue in issues)
                else "rejected"
                if rewrites_used < FORMULA_V2_MAX_FORMAT_REWRITES
                else "accepted_with_format_issues"
            )
            audit = {
                "policy": policy,
                "output_sha256": raw_hash,
                "normalized_output_sha256": normalized_hash,
                "normalizer_version": NORMALIZER_VERSION,
                "normalization": report,
                "engine_whitelist": whitelist.identity(),
                "branch_id": branch_id,
                "step_slot": step_slot,
                "issues": issues,
                "infrastructure_error": None,
                "format_rewrites_used": rewrites_used,
                "disposition": disposition,
            }
            self.control.record_formula_audit(self.config.run_id, call_id, audit)
            audits[call_id] = audit
        elif audit["normalized_output_sha256"] != normalized_hash:
            raise RuntimeInvariantError(
                "formula normalization does not reproduce its recorded audit"
            )
        if audit["disposition"] == "rejected":
            self._formula_v2_reject(state, call_id, branch_id, step_slot, audit)
            return None
        if audit["disposition"] not in {"accepted", "accepted_with_format_issues"}:
            raise RuntimeInvariantError(
                f"unsupported formula-v2 disposition {audit['disposition']!r}"
            )
        if recorded is not None:
            raise RuntimeInvariantError(
                "Writer normalization was recorded without a formula audit"
            )
        if replacements:
            self.record.normalize_writer_output(
                model_call_id=call_id,
                raw_output_sha256=raw_hash,
                content=StepContent(**fields),
                policy=policy,
                normalizer_version=NORMALIZER_VERSION,
                replacements=replacements,
            )
            self._sync_head()
        return StepContent(**fields)

    def _formula_gate(
        self,
        call_id: str,
        branch_id: str,
        step_slot: int,
        content: StepContent,
        *,
        recheck_infrastructure: bool = False,
    ) -> bool:
        """Audit a finished output before any scientific state transition.

        Rejected calls remain finished raw evidence. Their durable disposition
        prevents crash repair from treating them as pending steps.
        """
        policy = self.config.formula_validation_policy
        if policy is None:
            return True
        audits = self.control.formula_audits(self.config.run_id)
        output_hash = sha256_text(canonical_json(content.to_record()))
        audit = audits.get(call_id)
        if audit is not None and (
            audit["policy"] != policy or audit["output_sha256"] != output_hash
        ):
            raise RuntimeInvariantError("formula audit policy or output hash mismatch")
        if audit is None or recheck_infrastructure:
            if self._formula_validator is None:
                result = FormulaValidationResult(
                    infrastructure_error="Formula validator is unavailable"
                )
            else:
                try:
                    result = self._formula_validator(content)
                    if not isinstance(result, FormulaValidationResult):
                        raise TypeError("formula validator returned an invalid result")
                except Exception as exc:  # noqa: BLE001 - isolate validator faults from scientific state
                    result = FormulaValidationResult(
                        infrastructure_error=f"Formula validator failed: {type(exc).__name__}: {exc}"
                    )
            disposition = (
                "infrastructure_failure"
                if result.infrastructure_error
                else "rejected"
                if any(issue.get("severity") == "error" for issue in result.issues)
                else "accepted"
            )
            audit = {
                "policy": policy,
                "output_sha256": output_hash,
                "branch_id": branch_id,
                "step_slot": step_slot,
                "issues": [dict(issue) for issue in result.issues],
                "infrastructure_error": result.infrastructure_error,
                "disposition": disposition,
            }
            self.control.record_formula_audit(
                self.config.run_id,
                call_id,
                audit,
                recheck_infrastructure=recheck_infrastructure,
            )
            audits[call_id] = audit
        if audit["disposition"] == "accepted":
            return True
        # A later call proves this disposition was already consumed. Do not
        # apply an old rejection to the newly advanced branch on restart.
        state = self.record.snapshot()
        same_target = [
            call
            for call in state["model_calls"]
            if call["role"] == "writer"
            and call["target"] == {"branch_id": branch_id, "step_slot": step_slot}
        ]
        if same_target and same_target[-1]["model_call_id"] != call_id:
            return False
        rejected_count = sum(
            item["branch_id"] == branch_id
            and item["step_slot"] == step_slot
            and item["disposition"] == "rejected"
            for item in audits.values()
        )
        pause = (
            audit["disposition"] == "infrastructure_failure"
            or rejected_count > self.config.retries
        )
        branch = self._branch(state, branch_id)
        if pause and branch["status"] == "active":
            reason = audit["infrastructure_error"] or (
                "Formula format retry budget exhausted. Resume cannot reset this frozen "
                "budget; start a new run with corrected format instructions. See format diagnostics."
            )
            # Record V1.1 only permits runtime_failure on failed/aborted calls.
            # Preserve this finished call and pause the host, not scientific
            # branch state; the durable audit restores this stop after restart.
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="paused",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=branch["step_revision_ids"][-1]
                if branch["step_revision_ids"]
                else None,
                attempt=rejected_count,
                last_error=reason,
            )
            self.control.update_phase(
                self.config.run_id,
                RunPhase.PAUSED,
                stop_reason="formula_validation_failed",
                last_error=reason,
            )
        elif not pause and branch["status"] == "active":
            bookmark = self.control.branch(self.config.run_id, branch_id)
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=bookmark.provider_session_id if bookmark else None,
                provider_lineage=bookmark.provider_lineage if bookmark else None,
                last_operation_id=bookmark.last_operation_id if bookmark else None,
                last_step_revision_id=bookmark.last_step_revision_id
                if bookmark
                else None,
                attempt=rejected_count,
                last_error="Formula format correction required",
            )
        self._sync_head()
        return False

    def _completion_intent(
        self, state: Mapping[str, Any], step_revision_id: str
    ) -> bool:
        """Whether the writer asked to finish on the step under review.

        The checker enforces complete requirement coverage only when this is
        true, so it decides what the check actually does.  It is recorded with
        the request: without that, whether a completion review happened at all
        is only recoverable from whatever the checker happens to echo in prose.
        """

        step = self._step(state, step_revision_id)
        if step["origin"]["kind"] != "model":
            return False
        return (
            writer_control_from_call(
                self._call_for_step(state, step_revision_id)
            ).decision
            is WriterDecision.COMPLETE
        )

    def _completion_requirements(self) -> tuple[str, ...]:
        """The deterministic completion gates the checker is shown.

        They are also quotable evidence, so this has to return the same tuple
        wherever the catalog is built: at request time and again when a returned
        output is validated.
        """

        provider = getattr(self.runtime, "writer_preparation", None)
        preparation = provider(include_full=False) if callable(provider) else None
        requirements = (
            tuple(preparation.get("completion_requirements", ()))
            if isinstance(preparation, Mapping)
            else COMMON_WRITER_CLOSURE_REQUIREMENTS
        )
        if any(not isinstance(item, str) or not item.strip() for item in requirements):
            raise RuntimeInvariantError(
                "Writer preparation completion requirements are invalid"
            )
        return requirements

    @staticmethod
    def _route_first_step(branch: Mapping[str, Any], step_revision_id: str) -> bool:
        """Whether the step under review opens the branch's own route.

        The route is the branch's ordered step list, so this stays correct
        without tracking history separately.  A revise starts a replacement
        branch whose list is the untouched prefix followed by the new step, so
        replacing the opening step makes the replacement the first step again
        while replacing a later one does not.  A fork inherits the prefix up to
        its anchor, so a forked branch's first step is the step the parent
        opened with, not the fork's own first new step.
        """

        route = branch["step_revision_ids"]
        return bool(route) and route[0] == step_revision_id

    def _record_completion_intent(
        self, check_id: str, step_revision_id: str, intent: bool
    ) -> None:
        """Audit the flag beside the run, never inside the record.

        The flag decides what a check enforces, so it has to be recoverable
        afterwards.  It cannot go in the check_requested payload, whose schema
        is frozen with additionalProperties false, and it must not go in the
        request reason either: that string is rendered into the final judge's
        packet, so writing it there would silently change judge-visible text
        between rounds.  A side-car beside events.jsonl keeps the audit out of
        both.
        """

        path = getattr(getattr(self.record, "log", None), "path", None)
        if path is None:
            return
        audit = Path(path).parent / "check_completion_intent.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "check_id": check_id,
                "target_step_revision_id": step_revision_id,
                "completion_intent": intent,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    async def _run_check(self, branch_id: str, step_revision_id: str) -> None:
        state = self.record.snapshot()
        step = self._step(state, step_revision_id)
        check_id = self._next_id(
            "check", [item["check_id"] for item in state["checks"]]
        )
        intent = self._completion_intent(state, step_revision_id)
        self.record.request_check(
            check_id=check_id,
            step_revision_id=step_revision_id,
            output_sha256=step["output_sha256"],
            reason="Required deterministic step check before candidate eligibility.",
        )
        self._record_completion_intent(check_id, step_revision_id, intent)
        self._sync_head()
        await self._run_existing_check(branch_id, check_id)

    async def _run_existing_check(self, branch_id: str, check_id: str) -> None:
        state = self.record.snapshot()
        check = self._check(state, check_id)
        step = self._step(state, check["target_step_revision_id"])
        branch = self._branch(state, branch_id)
        completion_requirements = self._completion_requirements()
        request = CheckRequest(
            run_id=self.config.run_id,
            check_id=check_id,
            task_text=self.task_text,
            target=self._step_snapshot(
                step,
                {item["step_revision_id"]: item for item in state["step_revisions"]},
            ),
            transcript=bounded_transcript(
                self._transcript(state, branch["step_revision_ids"])
            )
            if self.config.record_version == "1.1"
            else self._transcript(state, branch["step_revision_ids"]),
            evidence_sources=evidence_catalog(
                state,
                check["target_step_revision_id"],
                self.task_text,
                completion_requirements,
            ),
            full_transcript=self._transcript(state, branch["step_revision_ids"]),
            record_version=self.config.record_version,
            completion_requirements=completion_requirements,
            completion_intent=self._completion_intent(
                state, check["target_step_revision_id"]
            ),
            intent_ledger_first=self.config.intent_ledger_first,
            dimension_check=self.config.dimension_check,
            target_is_route_first_step=self._route_first_step(
                branch, check["target_step_revision_id"]
            ),
        )
        prompt = self._check_prompt(request)
        call_id = self._next_id(
            "call", [item["model_call_id"] for item in state["model_calls"]]
        )
        start = self.record.start_model_call(
            model_call_id=call_id,
            role=ModelRole.CHECKER,
            target={"check_id": check_id},
            prompt=prompt,
        )
        attempt = self.control.next_attempt(
            self.config.run_id, "checker", "check", check_id
        )
        self.control.start_call(
            run_id=self.config.run_id,
            model_call_id=call_id,
            branch_id=branch_id,
            role="checker",
            target_kind="check",
            target_id=check_id,
            attempt=attempt,
            record_start_seq=start["seq"],
        )
        self._sync_head()
        try:
            invocation = await self.runtime.start_checker(request)
            self.control.attach_invocation(self.config.run_id, call_id, invocation)
            output = await self.runtime.collect_checker(invocation)
        except RuntimeInvocationError as exc:
            await self._handle_call_failure(
                call_id=call_id,
                role=ModelRole.CHECKER,
                branch_id=branch_id,
                target_id=check_id,
                attempt=attempt,
                error=exc,
            )
            return
        await self._finish_check(call_id, check, invocation, output)

    async def _finish_check(
        self,
        call_id: str,
        check: Mapping[str, Any],
        invocation: RuntimeInvocation,
        output: CheckOutput,
    ) -> None:
        state = self.record.snapshot()
        try:
            # The finished call keeps the body the provider returned; the check
            # records evidence quotes as the exact source spans they copy, so a
            # quote accepted for whitespace reflow still replays.
            recorded = validate_check_output(
                output,
                evidence_catalog(
                    state,
                    check["target_step_revision_id"],
                    self.task_text,
                    self._completion_requirements(),
                ),
            )
        except RuntimeInvocationError as exc:
            bookmark = self.control.call(self.config.run_id, call_id)
            await self._handle_call_failure(
                call_id=call_id,
                role=ModelRole.CHECKER,
                branch_id=self._step(state, check["target_step_revision_id"])[
                    "branch_id"
                ],
                target_id=check["check_id"],
                attempt=bookmark.attempt if bookmark else 1,
                error=exc,
            )
            return
        body = canonical_json(
            {
                "verdict": output.verdict,
                "reason": output.reason,
                "evidence": [item.to_record() for item in output.evidence],
            }
        )
        terminal = self.record.finish_model_call(
            model_call_id=call_id,
            role=ModelRole.CHECKER,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )
        self.control.finish_call(
            self.config.run_id,
            call_id,
            state="finished",
            record_terminal_seq=terminal["seq"],
            last_error=None,
        )
        self._sync_head()
        self.record.complete_check(
            check_id=check["check_id"],
            step_revision_id=check["target_step_revision_id"],
            output_sha256=check["target_output_sha256"],
            checker_call_id=call_id,
            output=recorded,
        )
        self._sync_head()

    async def _complete_branch(self, branch_id: str) -> None:
        self.record.change_branch_status(
            branch_id=branch_id,
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        candidate_id = self._next_id(
            "cand",
            [item["candidate_id"] for item in self.record.snapshot()["candidates"]],
        )
        self.record.declare_candidate(
            candidate_id=candidate_id,
            branch_id=branch_id,
            reason="Writer declared the branch purpose reached after required checks.",
        )
        state = self.record.snapshot()
        candidate = self._candidate(state, candidate_id)
        self.control.upsert_branch(
            self.config.run_id,
            branch_id,
            runtime_state="completed",
            provider_session_id=None,
            provider_lineage=None,
            last_operation_id=None,
            last_step_revision_id=candidate["tip_step_revision_id"],
            attempt=0,
            last_error=None,
        )
        self._sync_head()

    async def _run_judgement(self, candidate_id: str) -> None:
        state = self.record.snapshot()
        candidate = self._candidate(state, candidate_id)
        judgement_id = self._next_id(
            "judge", [item["judgement_id"] for item in state["judgements"]]
        )
        self.record.request_judgement(
            judgement_id=judgement_id,
            candidate_id=candidate_id,
            candidate_sha256=candidate["transcript_sha256"],
            reason="Independent terminal judgement for an eligible candidate.",
        )
        self._sync_head()
        await self._run_existing_judgement(judgement_id)

    async def _run_existing_judgement(self, judgement_id: str) -> None:
        state = self.record.snapshot()
        judgement = self._judgement(state, judgement_id)
        if judgement["state"] != "requested":
            raise RuntimeInvariantError("judge retry requires a requested judgement")
        candidate = self._candidate(state, judgement["candidate_id"])
        request = JudgeRequest(
            run_id=self.config.run_id,
            judgement_id=judgement_id,
            candidate_id=candidate["candidate_id"],
            task_text=self.task_text,
            transcript=self._transcript(
                state, candidate["transcript_step_revision_ids"]
            ),
            unresolved_checks=tuple(
                check
                for check in state["checks"]
                if check["check_id"] in candidate.get("unresolved_check_ids", [])
            ),
        )
        prompt = self._judge_prompt(request)
        call_id = self._next_id(
            "call", [item["model_call_id"] for item in state["model_calls"]]
        )
        start = self.record.start_model_call(
            model_call_id=call_id,
            role=ModelRole.JUDGE,
            target={"judgement_id": judgement_id},
            prompt=prompt,
        )
        attempt = self.control.next_attempt(
            self.config.run_id, "judge", "judgement", judgement_id
        )
        self.control.start_call(
            run_id=self.config.run_id,
            model_call_id=call_id,
            branch_id=candidate["branch_id"],
            role="judge",
            target_kind="judgement",
            target_id=judgement_id,
            attempt=attempt,
            record_start_seq=start["seq"],
        )
        self._sync_head()
        try:
            invocation = await self.runtime.start_judge(request)
            self.control.attach_invocation(self.config.run_id, call_id, invocation)
            output = await self.runtime.collect_judge(invocation)
        except RuntimeInvocationError as exc:
            await self._handle_call_failure(
                call_id=call_id,
                role=ModelRole.JUDGE,
                branch_id=candidate["branch_id"],
                target_id=judgement_id,
                attempt=attempt,
                error=exc,
            )
            return
        await self._finish_judgement(
            call_id, judgement_id, candidate, invocation, output
        )

    async def _finish_judgement(
        self,
        call_id: str,
        judgement_id: str,
        candidate: Mapping[str, Any],
        invocation: RuntimeInvocation,
        output: JudgeOutput,
    ) -> None:
        body = canonical_json(
            {"verdict": output.verdict, "reason": output.reason, "score": output.score}
        )
        terminal = self.record.finish_model_call(
            model_call_id=call_id,
            role=ModelRole.JUDGE,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )
        self.control.finish_call(
            self.config.run_id,
            call_id,
            state="finished",
            record_terminal_seq=terminal["seq"],
            last_error=None,
        )
        self._sync_head()
        self.record.complete_judgement(
            judgement_id=judgement_id,
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["transcript_sha256"],
            judge_call_id=call_id,
            output=output,
        )
        self._sync_head()

    async def _ensure_model_forks(
        self,
        parent: Mapping[str, Any],
        anchor_step_revision_id: str,
        writer_call: Mapping[str, Any],
        alternatives: Sequence[Any],
    ) -> None:
        source_event_id = raw_control_event_id(writer_call, self.record.events)
        suppressed: list[str] = []
        for alternative in alternatives:
            state = self.record.snapshot()
            existing = any(
                branch["parent_branch_id"] == parent["branch_id"]
                and branch["anchor_step_revision_id"] == anchor_step_revision_id
                and branch["created_reason"] == "model_alternative"
                and branch["hypothesis"]["text"] == alternative.hypothesis
                for branch in state["branches"]
            )
            if existing:
                continue
            if (
                self.config.max_active_branches is not None
                and len(self._active_branches(state)) >= self.config.max_active_branches
            ):
                # The Writer named a route nothing will now explore. Dropping it
                # silently would leave the parent continuing as though the
                # alternative were covered, so the disposition is recorded on the
                # control plane and told to the Writer on its next turn.
                suppressed.append(alternative.hypothesis)
                continue
            inherited = parent["step_revision_ids"][
                : parent["step_revision_ids"].index(anchor_step_revision_id) + 1
            ]
            try:
                session = await self._fork_or_rehydrate(
                    anchor_step_revision_id, inherited
                )
            except _FORK_ATTEMPT_FAILURES as error:
                # The provider refused this one fork, or the call preparing it
                # failed.  The parent branch, its session and every other run
                # sharing the child are unharmed, so the alternative is traced
                # exactly like one the cap refused and the parent carries on.
                # Ending the run here would throw away the whole derivation over
                # a branch that was optional.
                self._record_fork_suppression(
                    parent["branch_id"],
                    anchor_step_revision_id,
                    [alternative.hypothesis],
                    reason="fork_failed",
                    error=str(error),
                )
                continue
            branch_id = self._next_id(
                "br", [item["branch_id"] for item in state["branches"]]
            )
            self.record.create_child_branch(
                branch_id=branch_id,
                parent_branch_id=parent["branch_id"],
                fork_mode="after",
                anchor_step_revision_id=anchor_step_revision_id,
                inherited_step_revision_ids=inherited,
                hypothesis=alternative.hypothesis,
                hypothesis_source="model",
                hypothesis_source_event_id=source_event_id,
                created_reason="model_alternative",
                human_action_id=None,
                actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=session.session_id,
                provider_lineage=session.lineage.value,
                last_operation_id=None,
                last_step_revision_id=anchor_step_revision_id,
                attempt=0,
                last_error=None,
            )
            self._sync_head()
        if suppressed:
            self._record_fork_suppression(
                parent["branch_id"],
                anchor_step_revision_id,
                suppressed,
                reason="max_active_branches",
            )

    def _record_fork_suppression(
        self,
        branch_id: str,
        anchor_step_revision_id: str,
        alternatives: Sequence[str],
        *,
        reason: str,
        error: str | None = None,
    ) -> None:
        """Leave a durable control-plane line for a fork that did not happen.

        Two things reach this: an alternative the active-branch cap refused, and
        an alternative the provider refused when the fork was attempted. They
        get the same trace because they have the same consequence - the parent
        continues as though a named route were covered when nothing explores it
        - and they are told apart by ``reason``.

        No new record event type is introduced for this. The Record 1.1 event
        schema file is hashed into every run's own ``run_created`` payload, so a
        new event branch would change the artifact hash that already-archived
        runs recorded, and none of the existing payloads accepts the alternatives
        this disposition has to carry. The record does hold both halves of the
        fact already - the Writer's raw fork control, and the absence of a
        ``branch_created`` anchored at that step - so the trace is split: the
        control-plane log keeps the host's own statement of it, including the
        reason and any provider error text, and ``_fork_suppression_note``
        re-derives which alternatives went missing from the record itself.
        """

        detail: dict[str, Any] = {
            "fork_suppressed": True,
            "alternatives": list(alternatives),
            "reason": reason,
            "anchor_step_revision_id": anchor_step_revision_id,
        }
        if error is not None:
            detail["error"] = error
        self.control.record_disposition(
            self.config.run_id,
            branch_id,
            kind="fork_suppressed",
            detail=canonical_json(detail),
        )

    def _fork_suppression_note(
        self, state: Mapping[str, Any], branch: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], ...]:
        """Re-derive the forks this tip did not get, from durable state only.

        *Which* alternatives are missing comes from the record alone, so a
        resumed process reaches the same prompt as an uninterrupted one: the
        Writer's fork control is immutable in the call, and a created
        alternative is a ``branch_created`` anchored at the same step, so what
        is missing is exactly what was refused. *Why* each one is missing cannot
        be read off the record, which states the refusal only by omission, so
        the reason is taken from the equally durable control-plane disposition
        and falls back to the cap when no line survives.
        """

        if not branch["step_revision_ids"]:
            return ()
        tip_id = branch["step_revision_ids"][-1]
        tip = self._step(state, tip_id)
        if tip["branch_id"] != branch["branch_id"] or tip["origin"]["kind"] != "model":
            return ()
        control = writer_control_from_call(self._call_for_step(state, tip_id))
        if control.decision is not WriterDecision.FORK:
            return ()
        created = {
            item["hypothesis"]["text"]
            for item in state["branches"]
            if item["parent_branch_id"] == branch["branch_id"]
            and item["anchor_step_revision_id"] == tip_id
            and item["created_reason"] == "model_alternative"
        }
        missing = [
            alternative.hypothesis
            for alternative in control.alternatives
            if alternative.hypothesis not in created
        ]
        if not missing:
            return ()
        notes: list[Mapping[str, Any]] = []
        explained: set[str] = set()
        for line in self.control.dispositions(
            self.config.run_id, kind="fork_suppressed"
        ):
            if line.branch_id != branch["branch_id"]:
                continue
            detail = json.loads(line.detail)
            if detail.get("anchor_step_revision_id") != tip_id:
                continue
            named = [
                item
                for item in detail.get("alternatives", ())
                if item in missing and item not in explained
            ]
            if not named:
                continue
            explained.update(named)
            note: dict[str, Any] = {
                "kind": "fork_suppressed",
                "reason": detail.get("reason", "max_active_branches"),
                "alternatives": named,
                "guidance": FORK_SUPPRESSED_WRITER_NOTE,
            }
            if detail.get("error"):
                note["error"] = detail["error"]
            notes.append(note)
        unexplained = [item for item in missing if item not in explained]
        if unexplained:
            notes.append(
                {
                    "kind": "fork_suppressed",
                    "reason": "max_active_branches",
                    "alternatives": unexplained,
                    "guidance": FORK_SUPPRESSED_WRITER_NOTE,
                }
            )
        return tuple(notes)

    async def _fork_or_rehydrate(
        self, step_revision_id: str, inherited_step_ids: Sequence[str]
    ) -> RuntimeSession:
        bookmark = self.control.step(self.config.run_id, step_revision_id)
        if (
            self._provider_handles_live
            and bookmark is not None
            and bookmark.provider_session_id is not None
            and bookmark.provider_operation_id is not None
            and bookmark.provider_lineage is not None
        ):
            parent = RuntimeSession(
                bookmark.provider_session_id,
                ProviderLineage(bookmark.provider_lineage),
            )
            return await self.runtime.fork(parent, bookmark.provider_operation_id)
        state = self.record.snapshot()
        session = await self.runtime.rehydrate(
            self._transcript(state, inherited_step_ids)
        )
        self._provider_handles_live = True
        return session

    async def _writer_session(
        self,
        branch_id: str,
        transcript: Sequence[StepSnapshot],
        *,
        rehydrate_if_missing: bool,
        rotate_context: bool = False,
    ) -> RuntimeSession | None:
        bookmark = self.control.branch(self.config.run_id, branch_id)
        # Rotate provider history before it grows indefinitely. Full immutable
        # history is in Record; the request's bounded exact context and read
        # tools recover older assumptions without carrying old provider turns.
        rotate = (
            rotate_context
            or self.config.record_version == "1.1"
            and (
                len(transcript) >= 8
                or sum(len(canonical_json(s.content.to_record())) for s in transcript)
                >= 48000
            )
        )
        if (
            self._provider_handles_live
            and not rotate
            and bookmark
            and bookmark.provider_session_id
            and bookmark.provider_lineage
        ):
            return RuntimeSession(
                bookmark.provider_session_id,
                ProviderLineage(bookmark.provider_lineage),
            )
        if transcript or rehydrate_if_missing:
            session = await self.runtime.rehydrate(transcript)
            self._provider_handles_live = True
            # The Branch keeps its identity across a rotation or a restart; only
            # the provider thread carrying it is new. The runtime binds a Branch
            # to one writer session and rejects every other one, so the move has
            # to be announced here rather than discovered when the next turn
            # starts on a session the Branch was never given.
            self.runtime.rebind_branch_writer_session(branch_id, session)
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=session.session_id,
                provider_lineage=session.lineage.value,
                last_operation_id=None,
                last_step_revision_id=(
                    transcript[-1].step_revision_id if transcript else None
                ),
                attempt=0,
                last_error=None,
            )
            return session
        return None

    async def _handle_call_failure(
        self,
        *,
        call_id: str,
        role: ModelRole,
        branch_id: str,
        target_id: str,
        attempt: int,
        error: RuntimeInvocationError,
    ) -> None:
        terminal = self.record.fail_model_call(
            model_call_id=call_id,
            role=role,
            failure_kind=error.failure_kind,
            message=str(error),
            partial_output=error.partial_output,
            retryable=error.retryable,
        )
        self.control.finish_call(
            self.config.run_id,
            call_id,
            state="failed",
            record_terminal_seq=terminal["seq"],
            last_error=str(error),
        )
        failed_state = self.record.snapshot()
        target_calls = self._calls_on_target(
            failed_state,
            role,
            next(
                item["target"]
                for item in failed_state["model_calls"]
                if item["model_call_id"] == call_id
            ),
        )
        may_retry = (
            error.retryable
            and attempt - self._context_window_excuse(target_calls)
            <= self.config.retries
            and self._has_call_capacity(failed_state)
        )
        if (
            self.config.record_version == "1.1"
            and role is ModelRole.WRITER
            and self._context_window_retry_due(target_calls)
        ):
            # Retry once on a fresh provider thread instead of pausing: the next
            # Writer turn for this slot rehydrates the Branch from the Record
            # (see _run_writer), and crash repair reaches the same decision.
            bookmark = self.control.branch(self.config.run_id, branch_id)
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=bookmark.last_step_revision_id
                if bookmark
                else None,
                attempt=attempt,
                last_error=str(error),
            )
            self._sync_head()
            return
        if (
            self.config.record_version == "1.1"
            and role is ModelRole.CHECKER
            and may_retry
        ):
            # Keep the requested check open and preserve the failed raw call.
            # The scheduler will issue a new, independent Checker invocation;
            # only exhausting the configured retries pauses the scientific run.
            self._sync_head()
            return
        if self.config.record_version == "1.1" and role in {
            ModelRole.WRITER,
            ModelRole.CHECKER,
        }:
            if role is ModelRole.CHECKER:
                check = self._check(self.record.snapshot(), target_id)
                self.record.complete_check(
                    check_id=target_id,
                    step_revision_id=check["target_step_revision_id"],
                    output_sha256=check["target_output_sha256"],
                    checker_call_id=call_id,
                    output=CheckOutput(
                        "instrument_failure", str(error), (), "failed", Usage({})
                    ),
                )
            self.record.change_branch_status(
                branch_id=branch_id,
                from_status="active",
                to_status="paused",
                reason_code="runtime_failure",
                actor=RecordV1Writer.SYSTEM,
                model_call_id=call_id,
                check_id=target_id if role is ModelRole.CHECKER else None,
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="paused",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=None,
                attempt=attempt,
                last_error=str(error),
            )
            self.control.update_phase(
                self.config.run_id,
                RunPhase.PAUSED,
                stop_reason="runtime_failure",
                last_error=str(error),
            )
            self._sync_head()
            return
        if role is ModelRole.WRITER and error.partial_output:
            self.control.update_phase(
                self.config.run_id,
                RunPhase.ERROR,
                stop_reason="partial_writer_failure_requires_review",
                last_error=str(error),
            )
            self._sync_head()
            raise error

        if may_retry:
            self._sync_head()
            return

        if role is ModelRole.WRITER:
            self.record.change_branch_status(
                branch_id=branch_id,
                from_status="active",
                to_status="parked",
                reason_code="instrument_failure",
                actor=RecordV1Writer.SYSTEM,
                model_call_id=call_id,
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="parked",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=None,
                attempt=attempt,
                last_error=str(error),
            )
        elif role is ModelRole.CHECKER:
            state = self.record.snapshot()
            check = self._check(state, target_id)
            self.record.complete_check(
                check_id=target_id,
                step_revision_id=check["target_step_revision_id"],
                output_sha256=check["target_output_sha256"],
                checker_call_id=call_id,
                output=CheckOutput(
                    verdict="instrument_failure",
                    reason=str(error),
                    evidence=(),
                    finish_reason="failed",
                    usage=Usage({}),
                ),
            )
        else:
            state = self.record.snapshot()
            judgement = self._judgement(state, target_id)
            candidate = self._candidate(state, judgement["candidate_id"])
            self.record.complete_judgement(
                judgement_id=target_id,
                candidate_id=candidate["candidate_id"],
                candidate_sha256=candidate["transcript_sha256"],
                judge_call_id=call_id,
                output=JudgeOutput(
                    verdict="instrument_failure",
                    reason=str(error),
                    score=None,
                    finish_reason="failed",
                    usage=Usage({}),
                ),
            )
        self._sync_head()

    async def _apply_reconcile_outcome(
        self,
        call: Mapping[str, Any],
        control_call: CallBookmark | None,
        outcome: ReconcileResult,
    ) -> None:
        call_id = call["model_call_id"]
        role = ModelRole(call["role"])
        if outcome.status is ReconcileStatus.COMPLETED:
            if outcome.output is None:
                raise RuntimeInvariantError("completed reconciliation omitted output")
            invocation = (
                self._invocation_from_bookmark(control_call)
                if control_call is not None
                and control_call.provider_session_id is not None
                and control_call.provider_operation_id is not None
                else None
            )
            if invocation is None:
                raise RuntimeInvariantError(
                    "completed reconciliation needs a provider handle"
                )
            if role is ModelRole.WRITER:
                output = cast(WriterOutput, outcome.output)
                await self._finish_writer(
                    call_id,
                    call["target"]["branch_id"],
                    call["target"]["step_slot"],
                    invocation,
                    output,
                )
            elif role is ModelRole.CHECKER:
                check = self._check(self.record.snapshot(), call["target"]["check_id"])
                await self._finish_check(
                    call_id, check, invocation, cast(CheckOutput, outcome.output)
                )
            else:
                judgement_id = call["target"]["judgement_id"]
                judgement = self._judgement(self.record.snapshot(), judgement_id)
                candidate = self._candidate(
                    self.record.snapshot(), judgement["candidate_id"]
                )
                await self._finish_judgement(
                    call_id,
                    judgement_id,
                    candidate,
                    invocation,
                    cast(JudgeOutput, outcome.output),
                )
            return

        failure_kind = outcome.failure_kind or "unknown_provider_state"
        failure_message = (
            outcome.message or "provider call did not reach a completed state"
        )
        partial_output = outcome.partial_output
        retryable = outcome.retryable
        if role is ModelRole.WRITER:
            durable_body = "".join(
                chunk["text"] for chunk in call["chunks"] if chunk["channel"] == "body"
            )
            if durable_body and partial_output:
                if partial_output.startswith(durable_body):
                    pass
                elif durable_body.startswith(partial_output):
                    partial_output = durable_body
                else:
                    failure_kind = "provider_partial_mismatch"
                    failure_message = "provider partial output conflicts with durable Record body chunks"
                    partial_output = durable_body
                    retryable = False
            elif durable_body:
                partial_output = durable_body
        failure = RuntimeInvocationError(
            failure_kind,
            failure_message,
            partial_output=partial_output,
            retryable=retryable,
        )
        branch_id = call["target"].get("branch_id")
        if branch_id is None and control_call is not None:
            branch_id = control_call.branch_id
        await self._handle_call_failure(
            call_id=call_id,
            role=role,
            branch_id=branch_id or "unknown",
            target_id=(
                call["target"].get("check_id")
                or call["target"].get("judgement_id")
                or f"{call['target'].get('branch_id')}:{call['target'].get('step_slot')}"
            ),
            attempt=control_call.attempt if control_call is not None else 1,
            error=failure,
        )

    def _repair_record_prefix(self) -> bool:
        """Idempotently complete durable zero-call semantics after a crash.

        Record V1 is the source of truth.  A terminal ModelCall is never
        silently rerun when its durable output is sufficient to append the
        next semantic event.  Incomplete or ambiguous evidence fails closed.
        """

        changed = False
        while True:
            state = self.record.snapshot()
            self._align_terminal_control_calls(state)
            if any(
                item["verdict"] == "instrument_failure"
                and item["required_for_candidate"]
                for item in state["checks"]
            ):
                # Bookmarks are derived operational state; scientific repair is
                # advancement too, even when it does not invoke a provider.
                self._sync_head()
                break
            repaired = (
                self._repair_one_orphan_human_action(state)
                or self._repair_one_human_revision(state)
                or self._repair_one_finished_writer(state)
                or self._repair_one_terminal_writer_failure(state)
                or self._repair_one_check_completion(state)
                or self._repair_one_judgement_completion(state)
                or self._repair_one_candidate(state)
                or self._repair_one_judgement_request(state)
            )
            if not repaired:
                break
            changed = True
        if changed:
            self._sync_head()
        return changed

    async def _repair_orphan_abort_actions(self) -> None:
        while True:
            state = self.record.snapshot()
            consumed = self._consumed_human_action_ids()
            orphan = next(
                (
                    action
                    for action in state["human_actions"]
                    if action["action"] == "abort_model_call"
                    and action["action_id"] not in consumed
                ),
                None,
            )
            if orphan is None:
                return
            call = next(
                (
                    item
                    for item in state["model_calls"]
                    if item["model_call_id"] == orphan["target"]["model_call_id"]
                ),
                None,
            )
            if call is None or call["state"] != "started":
                message = "orphan abort action does not target an in-flight call"
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="orphan_human_action",
                    last_error=message,
                )
                self._sync_head()
                raise RuntimeInvariantError(message)
            bookmark = self.control.call(self.config.run_id, call["model_call_id"])
            if (
                bookmark is None
                or bookmark.state not in {"starting", "running"}
                or bookmark.provider_session_id is None
                or bookmark.provider_operation_id is None
                or bookmark.provider_lineage is None
            ):
                self._fail_interrupt_reconciliation(
                    "orphan abort recovery has no live provider handle"
                )
            invocation = self._invocation_from_bookmark(bookmark)
            try:
                provider_state = await self.runtime.reconcile(invocation)
            except Exception as reconcile_error:  # noqa: BLE001 - provider boundary
                self._fail_interrupt_reconciliation(
                    f"provider state could not be reconciled before interrupt: {reconcile_error}"
                )
            if provider_state.status is ReconcileStatus.INTERRUPTED:
                # The previous process already completed the provider-side
                # half of the command.  Do not issue a non-idempotent second
                # interrupt; close only the missing Record half.
                provider_partial = provider_state.partial_output
            elif provider_state.status is ReconcileStatus.RUNNING:
                try:
                    interrupted = await self.runtime.interrupt(invocation)
                    provider_partial = interrupted.partial_output
                except Exception as interrupt_error:  # noqa: BLE001 - provider boundary
                    # The provider may accept the interrupt and then lose the
                    # response.  A second read is the only safe confirmation.
                    try:
                        reconciled = await self.runtime.reconcile(invocation)
                    except Exception as reconcile_error:  # noqa: BLE001 - provider boundary
                        self._fail_interrupt_reconciliation(
                            "provider interrupt failed and its state could not be "
                            f"reconciled: {interrupt_error}; {reconcile_error}"
                        )
                    if reconciled.status is not ReconcileStatus.INTERRUPTED:
                        self._fail_interrupt_reconciliation(
                            "provider interrupt could not be confirmed; reconciliation "
                            f"reported {reconciled.status.value}: {interrupt_error}"
                        )
                    provider_partial = reconciled.partial_output
            else:
                self._fail_interrupt_reconciliation(
                    "abort intent raced with a provider state that does not prove "
                    f"interruption: {provider_state.status.value}"
                )

            durable_body = "".join(
                chunk["text"] for chunk in call["chunks"] if chunk["channel"] == "body"
            )
            try:
                partial_output = self._merge_partial_output(
                    durable_body=durable_body,
                    provider_partial=provider_partial,
                )
            except RuntimeInvariantError as error:
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="interrupt_partial_mismatch",
                    last_error=str(error),
                )
                self._sync_head()
                raise
            terminal = self.record.abort_model_call(
                model_call_id=call["model_call_id"],
                role=ModelRole(call["role"]),
                human_action_id=orphan["action_id"],
                partial_output=partial_output,
            )
            self.control.finish_call(
                self.config.run_id,
                call["model_call_id"],
                state="aborted",
                record_terminal_seq=terminal["seq"],
                last_error=orphan["reason"],
            )
            if call["role"] == ModelRole.WRITER.value:
                branch_id = call["target"]["branch_id"]
                if partial_output:
                    self.control.update_phase(
                        self.config.run_id,
                        RunPhase.ERROR,
                        stop_reason="partial_writer_failure_requires_review",
                        last_error="hard interrupt preserved durable partial writer output",
                    )
                    self._sync_head()
                    raise RuntimeInvariantError(
                        "hard interrupt with partial writer output requires manual review"
                    )
                self.record.change_branch_status(
                    branch_id=branch_id,
                    from_status="active",
                    to_status="parked",
                    reason_code="instrument_failure",
                    actor=RecordV1Writer.SYSTEM,
                    model_call_id=call["model_call_id"],
                )
                self.control.upsert_branch(
                    self.config.run_id,
                    branch_id,
                    runtime_state="parked",
                    provider_session_id=None,
                    provider_lineage=None,
                    last_operation_id=None,
                    last_step_revision_id=None,
                    attempt=bookmark.attempt,
                    last_error=orphan["reason"],
                )
            self._sync_head()

    def _fail_interrupt_reconciliation(self, message: str) -> None:
        self.control.update_phase(
            self.config.run_id,
            RunPhase.ERROR,
            stop_reason="interrupt_reconciliation_failed",
            last_error=message,
        )
        self._sync_head()
        raise RuntimeInvariantError(message)

    def _ensure_writer_chunks(
        self,
        model_call_id: str,
        expected: Sequence[tuple[str, str]],
    ) -> None:
        """Append only missing chunks and reject a divergent crash replay."""

        calls = [
            call
            for call in self.record.snapshot()["model_calls"]
            if call["model_call_id"] == model_call_id
        ]
        if len(calls) != 1:
            raise RuntimeInvariantError(
                "writer completion requires exactly one started model call"
            )
        chunks = calls[0]["chunks"]
        if len(chunks) > len(expected):
            raise RuntimeInvariantError(
                "writer call has more durable chunks than the recovered output"
            )
        for index, chunk in enumerate(chunks):
            channel, text = expected[index]
            if (
                chunk["index"] != index
                or chunk["channel"] != channel
                or chunk["text"] != text
            ):
                raise RuntimeInvariantError(
                    "durable writer chunk differs from the recovered provider output"
                )
        for index in range(len(chunks), len(expected)):
            channel, text = expected[index]
            self.record.chunk_model_call(
                model_call_id=model_call_id,
                role=ModelRole.WRITER,
                channel=channel,
                index=index,
                text=text,
            )

    @staticmethod
    def _merge_partial_output(*, durable_body: str, provider_partial: str) -> str:
        if not durable_body:
            return provider_partial
        if not provider_partial:
            return durable_body
        if provider_partial.startswith(durable_body):
            return provider_partial
        if durable_body.startswith(provider_partial):
            return durable_body
        raise RuntimeInvariantError(
            "provider interrupt partial output conflicts with durable Record body chunks"
        )

    def _repair_one_orphan_human_action(self, state: Mapping[str, Any]) -> bool:
        consumed_action_ids = self._consumed_human_action_ids()
        for action in state["human_actions"]:
            if action["action_id"] in consumed_action_ids:
                continue
            if action["action"] == "abort_model_call":
                message = "orphan abort action was not reconciled before scheduling"
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="orphan_human_action",
                    last_error=message,
                )
                self._sync_head()
                raise RuntimeInvariantError(message)
            if action["action"] not in {"pause_branch", "resume_branch"}:
                message = (
                    f"orphan {action['action']} action lacks enough durable context "
                    "for deterministic recovery"
                )
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="orphan_human_action",
                    last_error=message,
                )
                self._sync_head()
                raise RuntimeInvariantError(message)
            branch_id = action["target"]["branch_id"]
            branch = self._branch(state, branch_id)
            expected_status = (
                "active" if action["action"] == "pause_branch" else "paused"
            )
            if branch["status"] != expected_status:
                raise RuntimeInvariantError(
                    f"orphan {action['action']} action targets branch in "
                    f"unexpected {branch['status']} state"
                )
            if action["action"] == "pause_branch":
                to_status = "paused"
                reason_code = "human_pause"
                runtime_state = "paused"
                self.control.request_pause(
                    self.config.run_id,
                    actor_id=action["actor_id"],
                    reason=action["reason"],
                )
            else:
                to_status = "active"
                reason_code = "human_resume"
                runtime_state = "active"
            self.record.change_branch_status(
                branch_id=branch_id,
                from_status=expected_status,
                to_status=to_status,
                reason_code=reason_code,
                actor={"kind": "human", "id": action["actor_id"]},
                human_action_id=action["action_id"],
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state=runtime_state,
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=(
                    branch["step_revision_ids"][-1]
                    if branch["step_revision_ids"]
                    else None
                ),
                attempt=0,
                last_error=None,
            )
            if action["action"] == "resume_branch":
                self.control.clear_pause(self.config.run_id)
            return True
        return False

    def _consumed_human_action_ids(self) -> set[str]:
        consumed: set[str] = set()
        for event in self.record.events:
            payload = event["payload"]
            if event["type"] in {
                "branch_created",
                "branch_status_changed",
                "model_call_aborted",
                "selection_recorded",
            }:
                action_id = payload.get("human_action_id")
                if action_id is not None:
                    consumed.add(action_id)
        return consumed

    def _align_terminal_control_calls(self, state: Mapping[str, Any]) -> None:
        for call in state["model_calls"]:
            if call["state"] not in {"finished", "failed", "aborted"}:
                continue
            bookmark = self.control.call(self.config.run_id, call["model_call_id"])
            if bookmark is None:
                raise RuntimeInvariantError(
                    f"terminal call {call['model_call_id']} has no control bookmark"
                )
            if bookmark.state in {"starting", "running"}:
                self.control.finish_call(
                    self.config.run_id,
                    call["model_call_id"],
                    state=call["state"],
                    record_terminal_seq=self._event_sequence(call["terminal_event_id"]),
                    last_error=(
                        call["failure"]["message"]
                        if call["state"] in {"failed", "aborted"}
                        else None
                    ),
                )
            elif bookmark.state != call["state"]:
                raise RuntimeInvariantError(
                    f"Record/control call-state mismatch for {call['model_call_id']}: "
                    f"{call['state']} != {bookmark.state}"
                )

    def _repair_one_human_revision(self, state: Mapping[str, Any]) -> bool:
        actions = {item["action_id"]: item for item in state["human_actions"]}
        calls = state["model_calls"]
        for branch in state["branches"]:
            if branch["created_reason"] != "human_revision":
                continue
            inherited = branch["inherited_step_revision_ids"]
            own_step_ids = branch["step_revision_ids"][len(inherited) :]
            if own_step_ids:
                first = self._step(state, own_step_ids[0])
                if (
                    first["origin"]["kind"] != "human"
                    or first["origin"]["human_action_id"] != branch["human_action_id"]
                    or first["replaces_step_revision_id"]
                    != branch["anchor_step_revision_id"]
                ):
                    raise RuntimeInvariantError(
                        "human_revision branch is missing its required human replacement"
                    )
                continue
            if branch["status"] != "active":
                raise RuntimeInvariantError(
                    "unsealed human_revision replacement is not on an active branch"
                )
            anchor = self._step(state, branch["anchor_step_revision_id"])
            target = {
                "branch_id": branch["branch_id"],
                "step_slot": anchor["step_slot"],
            }
            if any(
                call["role"] == ModelRole.WRITER.value and call["target"] == target
                for call in calls
            ):
                raise RuntimeInvariantError(
                    "ordinary writer call already occupies an unsealed human replacement slot"
                )
            action_id = branch["human_action_id"]
            action = actions.get(action_id)
            if action is None or action["action"] != "revise_step":
                raise RuntimeInvariantError(
                    "human_revision branch has no matching revise_step action"
                )
            content = self._parse_step_content(
                action["content"], context=f"human action {action_id}"
            )
            action_events = [
                event
                for event in self.record.events
                if event["event_id"] == action["event_id"]
                and event["type"] == "human_action_recorded"
            ]
            if len(action_events) != 1 or action_events[0]["actor"]["kind"] != "human":
                raise RuntimeInvariantError(
                    "human revision action has no unique human actor event"
                )
            step_id = self._next_id(
                "step",
                [item["step_revision_id"] for item in state["step_revisions"]],
            )
            self.record.seal_human_revision(
                step_revision_id=step_id,
                branch_id=branch["branch_id"],
                step_slot=anchor["step_slot"],
                revision=anchor["revision"] + 1,
                replaces_step_revision_id=anchor["step_revision_id"],
                content=content,
                human_action_id=action_id,
                actor_id=action_events[0]["actor"]["id"],
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch["branch_id"],
                runtime_state="active",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=step_id,
                attempt=0,
                last_error=None,
            )
            return True
        return False

    def _repair_one_finished_writer(self, state: Mapping[str, Any]) -> bool:
        sealed_call_ids = {
            item["origin"]["model_call_id"]
            for item in state["step_revisions"]
            if item["origin"]["kind"] == "model"
        }
        all_calls = state["model_calls"]
        for call in all_calls:
            if (
                call["role"] != ModelRole.WRITER.value
                or call["state"] != "finished"
                or call.get("runtime_disposition") is not None
                or call["model_call_id"] in sealed_call_ids
            ):
                continue
            gated: StepContent | None = None
            if self.config.formula_validation_policy == FORMULA_V2_POLICY:
                gated = self._formula_gate_v2(
                    call["model_call_id"],
                    call["target"]["branch_id"],
                    call["target"]["step_slot"],
                    self._parse_step_content(
                        call["output_text"],
                        context=f"writer call {call['model_call_id']}",
                    ),
                )
                if gated is None:
                    continue
            elif self.config.formula_validation_policy:
                content = self._parse_step_content(
                    call["output_text"], context=f"writer call {call['model_call_id']}"
                )
                if not self._formula_gate(
                    call["model_call_id"],
                    call["target"]["branch_id"],
                    call["target"]["step_slot"],
                    content,
                ):
                    continue
            same_target = [
                item
                for item in all_calls
                if item["role"] == ModelRole.WRITER.value
                and item["target"] == call["target"]
            ]
            if same_target[-1]["model_call_id"] != call["model_call_id"]:
                raise RuntimeInvariantError(
                    "finished writer output was superseded before its step was sealed"
                )
            branch = self._branch(state, call["target"]["branch_id"])
            if branch["status"] != "active":
                raise RuntimeInvariantError(
                    "unsealed finished writer targets a non-active branch"
                )
            expected_slot = len(branch["step_revision_ids"]) + 1
            if call["target"]["step_slot"] != expected_slot:
                raise RuntimeInvariantError(
                    "unsealed finished writer no longer targets the next branch slot"
                )
            decision = writer_control_from_call(call)
            content = (
                gated
                if gated is not None
                else self._parse_step_content(
                    call["output_text"],
                    context=f"writer call {call['model_call_id']}",
                )
            )
            if self._complete_unchanged_route(
                call["model_call_id"], branch["branch_id"], content, decision
            ):
                return True
            step_id = self._next_id(
                "step",
                [item["step_revision_id"] for item in state["step_revisions"]],
            )
            branch_id = branch["branch_id"]
            if decision.decision is WriterDecision.REVISE:
                branch_id = self._materialize_model_revision(
                    call["model_call_id"], branch_id, step_id, content, decision
                )
                if branch_id is None:
                    return True
            else:
                self.record.seal_model_step(
                    step_revision_id=step_id,
                    branch_id=branch_id,
                    step_slot=expected_slot,
                    content=content,
                    model_call_id=call["model_call_id"],
                )
            self.control.upsert_branch(
                self.config.run_id,
                branch_id,
                runtime_state="active",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=step_id,
                attempt=0,
                last_error=None,
            )
            return True
        return False

    def _repair_one_terminal_writer_failure(self, state: Mapping[str, Any]) -> bool:
        all_calls = state["model_calls"]
        for branch in self._active_branches(state):
            target = {
                "branch_id": branch["branch_id"],
                "step_slot": len(branch["step_revision_ids"]) + 1,
            }
            calls = [
                item
                for item in all_calls
                if item["role"] == ModelRole.WRITER.value and item["target"] == target
            ]
            if not calls:
                continue
            latest = calls[-1]
            if latest["state"] in {"started", "finished"}:
                continue
            if self.config.record_version == "1.1":
                resumed = any(
                    item["reason"] in {"human_resume", "manual_reopen"}
                    and item["seq"] > self._event_sequence(latest["terminal_event_id"])
                    for item in branch["status_history"]
                )
                if resumed or self._context_window_retry_due(calls):
                    continue
                self.record.change_branch_status(
                    branch_id=branch["branch_id"],
                    from_status="active",
                    to_status="paused",
                    reason_code="runtime_failure",
                    actor=RecordV1Writer.SYSTEM,
                    model_call_id=latest["model_call_id"],
                )
                return True
            if latest["body_chars"]:
                self.control.update_phase(
                    self.config.run_id,
                    RunPhase.ERROR,
                    stop_reason="partial_writer_failure_requires_review",
                    last_error=latest["failure"]["message"],
                )
                raise RuntimeInvariantError(
                    "partial terminal writer output requires manual review"
                )
            if self._terminal_call_may_retry(calls):
                continue
            self.record.change_branch_status(
                branch_id=branch["branch_id"],
                from_status="active",
                to_status="parked",
                reason_code="instrument_failure",
                actor=RecordV1Writer.SYSTEM,
                model_call_id=latest["model_call_id"],
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch["branch_id"],
                runtime_state="parked",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=(
                    branch["step_revision_ids"][-1]
                    if branch["step_revision_ids"]
                    else None
                ),
                attempt=len(calls),
                last_error=latest["failure"]["message"],
            )
            return True
        return False

    def _repair_one_check_completion(self, state: Mapping[str, Any]) -> bool:
        for check in state["checks"]:
            if check["state"] != "requested":
                continue
            calls = [
                item
                for item in state["model_calls"]
                if item["role"] == ModelRole.CHECKER.value
                and item["target"] == {"check_id": check["check_id"]}
            ]
            if not calls or calls[-1]["state"] == "started":
                continue
            latest = calls[-1]
            if any(item["state"] == "finished" for item in calls[:-1]):
                raise RuntimeInvariantError(
                    "checker target was retried after a durable finished output"
                )
            if latest["state"] == "finished":
                # Same recorded evidence as the live path: a reflowed quote in
                # the durable body is recorded as its exact source span.
                output = validate_check_output(
                    self._parse_check_output(latest),
                    evidence_catalog(
                        state,
                        check["target_step_revision_id"],
                        self.task_text,
                        self._completion_requirements(),
                    ),
                )
            elif self._terminal_call_may_retry(calls):
                continue
            else:
                output = CheckOutput(
                    verdict="instrument_failure",
                    reason=latest["failure"]["message"],
                    evidence=(),
                    finish_reason="failed",
                    usage=Usage({}),
                )
            self.record.complete_check(
                check_id=check["check_id"],
                step_revision_id=check["target_step_revision_id"],
                output_sha256=check["target_output_sha256"],
                checker_call_id=latest["model_call_id"],
                output=output,
            )
            return True
        return False

    def _repair_one_judgement_completion(self, state: Mapping[str, Any]) -> bool:
        for judgement in state["judgements"]:
            if judgement["state"] != "requested":
                continue
            calls = [
                item
                for item in state["model_calls"]
                if item["role"] == ModelRole.JUDGE.value
                and item["target"] == {"judgement_id": judgement["judgement_id"]}
            ]
            if not calls or calls[-1]["state"] == "started":
                continue
            latest = calls[-1]
            if any(item["state"] == "finished" for item in calls[:-1]):
                raise RuntimeInvariantError(
                    "judgement target was retried after a durable finished output"
                )
            candidate = self._candidate(state, judgement["candidate_id"])
            if latest["state"] == "finished":
                output = self._parse_judge_output(latest)
            elif self._terminal_call_may_retry(calls):
                continue
            else:
                output = JudgeOutput(
                    verdict="instrument_failure",
                    reason=latest["failure"]["message"],
                    score=None,
                    finish_reason="failed",
                    usage=Usage({}),
                )
            self.record.complete_judgement(
                judgement_id=judgement["judgement_id"],
                candidate_id=candidate["candidate_id"],
                candidate_sha256=candidate["transcript_sha256"],
                judge_call_id=latest["model_call_id"],
                output=output,
            )
            return True
        return False

    def _repair_one_candidate(self, state: Mapping[str, Any]) -> bool:
        candidates_by_branch: dict[str, list[Mapping[str, Any]]] = {}
        for candidate in state["candidates"]:
            candidates_by_branch.setdefault(candidate["branch_id"], []).append(
                candidate
            )
        for branch in state["branches"]:
            if branch["status"] != "completed":
                continue
            existing = candidates_by_branch.get(branch["branch_id"], [])
            if len(existing) > 1:
                raise RuntimeInvariantError("completed branch has multiple candidates")
            if existing:
                continue
            candidate_id = self._next_id(
                "cand", [item["candidate_id"] for item in state["candidates"]]
            )
            self.record.declare_candidate(
                candidate_id=candidate_id,
                branch_id=branch["branch_id"],
                reason="Recovered durable writer completion after a runtime restart.",
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch["branch_id"],
                runtime_state="completed",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=branch["step_revision_ids"][-1],
                attempt=0,
                last_error=None,
            )
            return True
        return False

    def _repair_one_judgement_request(self, state: Mapping[str, Any]) -> bool:
        if self.config.record_version == "1.1":
            # Final review is isolated after generation and never fed back
            # into the Writer's independent derivation loop.
            return False
        judgements_by_candidate: dict[str, list[Mapping[str, Any]]] = {}
        for judgement in state["judgements"]:
            judgements_by_candidate.setdefault(judgement["candidate_id"], []).append(
                judgement
            )
        for candidate in state["candidates"]:
            if candidate["status"] not in (
                {"eligible", "conditional"}
                if self.config.record_version == "1.1"
                else {"eligible"}
            ):
                continue
            existing = judgements_by_candidate.get(candidate["candidate_id"], [])
            if len(existing) > 1:
                raise RuntimeInvariantError("candidate has multiple judgements")
            if existing:
                continue
            judgement_id = self._next_id(
                "judge", [item["judgement_id"] for item in state["judgements"]]
            )
            self.record.request_judgement(
                judgement_id=judgement_id,
                candidate_id=candidate["candidate_id"],
                candidate_sha256=candidate["transcript_sha256"],
                reason="Independent terminal judgement for an eligible candidate.",
            )
            return True
        return False

    def _terminal_call_may_retry(self, calls: Sequence[Mapping[str, Any]]) -> bool:
        latest = calls[-1]
        return (
            latest["state"] == "failed"
            and latest["failure"]["retryable"]
            and len(calls) - self._context_window_excuse(calls) <= self.config.retries
            and self._has_call_capacity(self.record.snapshot())
        )

    @staticmethod
    def _calls_on_target(
        state: Mapping[str, Any], role: ModelRole, target: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        return [
            item
            for item in state["model_calls"]
            if item["role"] == role.value and item["target"] == target
        ]

    @staticmethod
    def _context_window_exhausted(call: Mapping[str, Any]) -> bool:
        """A zero-body call the provider refused because its context was full."""

        failure = call.get("failure")
        return (
            call["state"] == "failed"
            and isinstance(failure, Mapping)
            and failure["kind"] == "provider_failed"
            and failure["retryable"] is True
            and call["body_chars"] == 0
            and CONTEXT_WINDOW_EXHAUSTED_MESSAGE in failure["message"]
        )

    def _context_window_excuse(self, calls: Sequence[Mapping[str, Any]]) -> int:
        """Failures on one Writer or Checker target that the retry budget ignores.

        Record V1.1 retries one context-window exhausted failure per call target
        on a fresh provider thread without charging the configured retries:
        the failure says the thread was full, not that the request is at fault.
        A Checker turn always runs on a new thread; a Writer turn is moved to a
        rehydrated one by _run_writer.  Any further failure on the target, a
        second exhausted context included, follows the ordinary rules.
        """

        if (
            self.config.record_version != "1.1"
            or not calls
            or calls[-1]["role"]
            not in {ModelRole.WRITER.value, ModelRole.CHECKER.value}
        ):
            return 0
        return 1 if any(self._context_window_exhausted(item) for item in calls) else 0

    def _context_window_retry_due(self, calls: Sequence[Mapping[str, Any]]) -> bool:
        """Whether the latest call on a target is the one excused failure."""

        return (
            self._context_window_excuse(calls) == 1
            and self._context_window_exhausted(calls[-1])
            and sum(self._context_window_exhausted(item) for item in calls) == 1
            and self._has_call_capacity(self.record.snapshot())
        )

    @staticmethod
    def _event_sequence(event_id: str) -> int:
        if not event_id.startswith("evt_") or not event_id[4:].isdigit():
            raise RuntimeInvariantError(f"invalid Record event id {event_id!r}")
        return int(event_id[4:])

    @staticmethod
    def _parse_canonical_object(
        text: str, *, keys: set[str], context: str
    ) -> Mapping[str, Any]:
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeInvariantError(f"{context} is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != keys:
            raise RuntimeInvariantError(f"{context} has an invalid object shape")
        if canonical_json(value) != text:
            raise RuntimeInvariantError(f"{context} is not canonical JSON")
        return value

    @classmethod
    def _parse_step_content(cls, text: str, *, context: str) -> StepContent:
        value = cls._parse_canonical_object(
            text,
            keys={"claim", "why", "source", "derivation", "scope"},
            context=context,
        )
        try:
            return StepContent(
                claim=value["claim"],
                why=value["why"],
                source=value["source"],
                derivation=value["derivation"],
                scope=value["scope"],
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeInvariantError(f"{context} has invalid step content") from exc

    @classmethod
    def _parse_check_output(cls, call: Mapping[str, Any]) -> CheckOutput:
        context = f"checker call {call['model_call_id']}"
        value = cls._parse_canonical_object(
            call["output_text"],
            keys={"verdict", "reason", "evidence"},
            context=context,
        )
        if not isinstance(value["evidence"], list):
            raise RuntimeInvariantError(f"{context} evidence must be a list")
        evidence: list[CheckEvidence] = []
        try:
            for item in value["evidence"]:
                if not isinstance(item, dict) or set(item) != {
                    "kind",
                    "source_id",
                    "quote",
                }:
                    raise RuntimeInvariantError(f"{context} has invalid evidence shape")
                evidence.append(
                    CheckEvidence(
                        kind=item["kind"],
                        source_id=item["source_id"],
                        quote=item["quote"],
                    )
                )
            return CheckOutput(
                verdict=value["verdict"],
                reason=value["reason"],
                evidence=tuple(evidence),
                finish_reason=call["finish_reason"],
                usage=Usage(call["usage"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeInvariantError(f"{context} has invalid output") from exc

    @classmethod
    def _parse_judge_output(cls, call: Mapping[str, Any]) -> JudgeOutput:
        context = f"judge call {call['model_call_id']}"
        value = cls._parse_canonical_object(
            call["output_text"],
            keys={"verdict", "reason", "score"},
            context=context,
        )
        try:
            return JudgeOutput(
                verdict=value["verdict"],
                reason=value["reason"],
                score=value["score"],
                finish_reason=call["finish_reason"],
                usage=Usage(call["usage"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeInvariantError(f"{context} has invalid output") from exc

    def _apply_soft_pause(self, *, actor_id: str, reason: str) -> None:
        state = self.record.snapshot()
        for branch in self._active_branches(state):
            current = self.record.snapshot()
            action_id = self._next_id(
                "act", [item["action_id"] for item in current["human_actions"]]
            )
            self.record.record_human_action(
                actor_id=actor_id,
                action_id=action_id,
                action="pause_branch",
                target={"branch_id": branch["branch_id"]},
                reason=reason,
                content=None,
            )
            self.record.change_branch_status(
                branch_id=branch["branch_id"],
                from_status="active",
                to_status="paused",
                reason_code="human_pause",
                actor={"kind": "human", "id": actor_id},
                human_action_id=action_id,
            )
            self.control.upsert_branch(
                self.config.run_id,
                branch["branch_id"],
                runtime_state="paused",
                provider_session_id=None,
                provider_lineage=None,
                last_operation_id=None,
                last_step_revision_id=(
                    branch["step_revision_ids"][-1]
                    if branch["step_revision_ids"]
                    else None
                ),
                attempt=0,
                last_error=None,
            )
        self.control.update_phase(
            self.config.run_id, RunPhase.PAUSED, stop_reason="soft_pause"
        )
        self._sync_head()

    def _strict_query(self) -> ReplayQuery:
        return ReplayQuery(self.record.verify_complete())

    def _sync_head(self) -> None:
        seq, event_sha = self.record.head
        self.control.sync_record_head(self.config.run_id, seq, event_sha)

    def _has_call_capacity(self, state: Mapping[str, Any]) -> bool:
        return (
            self.config.max_model_calls is None
            or state["summary"]["model_call_count"] < self.config.max_model_calls
        )

    @staticmethod
    def _next_id(prefix: str, existing: Sequence[str]) -> str:
        occupied = set(existing)
        number = 1
        while f"{prefix}_{number:04d}" in occupied:
            number += 1
        return f"{prefix}_{number:04d}"

    @staticmethod
    def _active_branches(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return sorted(
            [item for item in state["branches"] if item["status"] == "active"],
            key=lambda branch: (
                int(branch["status_history"][0]["seq"]),
                branch["branch_id"],
            ),
        )

    @staticmethod
    def _branch(state: Mapping[str, Any], branch_id: str) -> Mapping[str, Any]:
        try:
            return next(
                item for item in state["branches"] if item["branch_id"] == branch_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError(f"unknown branch {branch_id}") from exc

    @staticmethod
    def _step(state: Mapping[str, Any], step_id: str) -> Mapping[str, Any]:
        try:
            return next(
                item
                for item in state["step_revisions"]
                if item["step_revision_id"] == step_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError(f"unknown step {step_id}") from exc

    @staticmethod
    def _check(state: Mapping[str, Any], check_id: str) -> Mapping[str, Any]:
        try:
            return next(
                item for item in state["checks"] if item["check_id"] == check_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError(f"unknown check {check_id}") from exc

    @staticmethod
    def _candidate(state: Mapping[str, Any], candidate_id: str) -> Mapping[str, Any]:
        try:
            return next(
                item
                for item in state["candidates"]
                if item["candidate_id"] == candidate_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError(f"unknown candidate {candidate_id}") from exc

    @staticmethod
    def _judgement(state: Mapping[str, Any], judgement_id: str) -> Mapping[str, Any]:
        try:
            return next(
                item
                for item in state["judgements"]
                if item["judgement_id"] == judgement_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError(f"unknown judgement {judgement_id}") from exc

    @staticmethod
    def _required_check_for_step(
        state: Mapping[str, Any], step_id: str
    ) -> Mapping[str, Any] | None:
        matches = [
            item
            for item in state["checks"]
            if item["target_step_revision_id"] == step_id
            and item["required_for_candidate"]
        ]
        if len(matches) > 1:
            raise RuntimeInvariantError("step has duplicate required checks")
        return matches[0] if matches else None

    @staticmethod
    def _call_for_step(state: Mapping[str, Any], step_id: str) -> Mapping[str, Any]:
        step = DerivationOrchestrator._step(state, step_id)
        call_id = step["origin"]["model_call_id"]
        try:
            return next(
                item
                for item in state["model_calls"]
                if item["model_call_id"] == call_id
            )
        except StopIteration as exc:
            raise RuntimeInvariantError("sealed step lost its writer call") from exc

    @staticmethod
    def _step_snapshot(
        step: Mapping[str, Any],
        steps: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> StepSnapshot:
        """Attach the route lineage a model cannot work out for itself.

        A step is sealed with an identifier it could not have cited while it
        was being written, so a ledger entry that points at "the step that
        proves this" can only name a stable route position.  The slot is that
        position, and the superseded chain is every earlier revision of the
        same slot, newest first, walked from ``replaces_step_revision_id``.
        Without ``steps`` only the step's own slot is known.
        """

        superseded: list[str] = []
        if steps is not None:
            current = step["replaces_step_revision_id"]
            while current is not None:
                superseded.append(current)
                current = steps[current]["replaces_step_revision_id"]
        return StepSnapshot(
            step_revision_id=step["step_revision_id"],
            content=StepContent(**step["content"]),
            step_slot=step.get("step_slot"),
            superseded_step_revision_ids=tuple(superseded),
        )

    def _transcript(
        self, state: Mapping[str, Any], step_ids: Sequence[str]
    ) -> tuple[StepSnapshot, ...]:
        steps = {item["step_revision_id"]: item for item in state["step_revisions"]}
        return tuple(
            self._step_snapshot(self._step(state, step_id), steps)
            for step_id in step_ids
        )

    @staticmethod
    def _writer_prompt(request: WriterRequest) -> str:
        return writer_user_prompt(request)

    @staticmethod
    def _check_prompt(request: CheckRequest) -> str:
        return checker_user_prompt(request)

    @staticmethod
    def _judge_prompt(request: JudgeRequest) -> str:
        return judge_user_prompt(request)

    @staticmethod
    def _invocation_from_bookmark(bookmark: CallBookmark) -> RuntimeInvocation:
        if (
            bookmark.provider_session_id is None
            or bookmark.provider_operation_id is None
            or bookmark.provider_lineage is None
        ):
            raise RuntimeInvariantError("provider bookmark is incomplete")
        return RuntimeInvocation(
            session=RuntimeSession(
                bookmark.provider_session_id,
                ProviderLineage(bookmark.provider_lineage),
            ),
            operation_id=bookmark.provider_operation_id,
            role=ModelRole(bookmark.role),
        )


__all__ = ["DerivationOrchestrator"]
