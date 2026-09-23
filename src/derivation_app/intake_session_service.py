"""Application state machine for persistent scientific Intake sessions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from .app_server_intake_session import (
    DEFAULT_MAX_AUDIT_REJECTIONS,
    DEFAULT_MAX_FRONTIER_ROUNDS,
    INTAKE_SESSION_PROMPT_VERSION,
    AppServerIntakeSessionAdvisor,
    DecisionReopenRequest,
    IntakeInvariant,
    IntakeRepairLog,
    IntakeRoundMode,
    IntakeRoundProposal,
    IntakeRoundRepair,
    SpecificationAudit,
    ThreadReplacementReceipt,
    deduplicate_intake_questions,
)
from .intake_handoff import IntakeHandoffBundle, build_intake_handoff
from .intake_session import (
    ConvergenceState,
    ConversationEvent,
    DecisionAnswer,
    DecisionEntry,
    DecisionStatus,
    DeclaredDefault,
    IntakeCommandRejected,
    IntakeQuestion,
    IntakeSession,
    IntakeSessionStatus,
    IntakeStaleRevision,
    LadderRung,
    PendingSubmission,
    PendingSubmissionKind,
    ProblemSpecification,
    ProblemSpecificationStatus,
    SQLiteIntakeStore,
    ThreadGeneration,
    ThreadGenerationStatus,
    intake_question_payload,
)
from .model_catalog import (
    DEFAULT_PRODUCT_EFFORT,
    DEFAULT_PRODUCT_MODEL,
    DEFAULT_PRODUCT_SERVICE_TIER,
)

CONVERGENCE_MAX_FRONTIER_ROUNDS = "max_frontier_rounds"
CONVERGENCE_MAX_AUDIT_REJECTIONS = "max_audit_rejections"

_RESUMABLE_STATUSES = frozenset(
    {
        IntakeSessionStatus.ACTIVE,
        IntakeSessionStatus.CONVERGENCE_REQUIRED,
        IntakeSessionStatus.CANDIDATE_READY,
    }
)


class IntakeIdFactory(Protocol):
    def __call__(self) -> str: ...


class ProblemSpecificationAuditor(Protocol):
    async def audit(
        self,
        specification: ProblemSpecification,
        *,
        initial_problem: str,
        decision_log: Sequence[Mapping[str, Any]],
        mode: IntakeRoundMode,
        session_id: str,
    ) -> SpecificationAudit: ...


@dataclass(frozen=True)
class _RoundOutcome:
    """One advisor round plus the audit verdict and the budget it consumed."""

    proposal: IntakeRoundProposal
    events: tuple[ConversationEvent, ...]
    audit_rejections: int
    convergence_reason: str | None


def default_intake_id() -> str:
    return f"intake_{uuid.uuid4().hex}"


def _answer_payload(answer: DecisionAnswer) -> dict[str, Any]:
    return {
        "selected_option_ids": list(answer.selected_option_ids),
        "custom_text": answer.custom_text,
        "strategy": None if answer.strategy is None else answer.strategy.value,
    }


def _round_count(events: Sequence[ConversationEvent]) -> int:
    """Count the frontier rounds the user has already answered."""

    return sum(1 for event in events if event.kind == "user_answers")


def _initial_problem(events: Sequence[ConversationEvent]) -> str:
    for event in events:
        if event.kind == "user_message":
            text = event.payload.get("text")
            if isinstance(text, str):
                return text
    return ""


def _merge_defaults(
    existing: tuple[DeclaredDefault, ...],
    incoming: tuple[DeclaredDefault, ...],
) -> tuple[DeclaredDefault, ...]:
    """Union declared defaults by default_id, keeping the newest wording."""

    merged: dict[str, DeclaredDefault] = {item.default_id: item for item in existing}
    order = [item.default_id for item in existing]
    for item in incoming:
        if item.default_id not in merged:
            order.append(item.default_id)
        merged[item.default_id] = item
    return tuple(merged[default_id] for default_id in order)


def _event(kind: str, payload: Mapping[str, Any]) -> ConversationEvent:
    return ConversationEvent(
        event_id=f"event_{uuid.uuid4().hex}",
        kind=kind,
        payload={
            **dict(payload),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )


_MAX_FAILURE_REASON = 500


def _failure_reason(error: BaseException) -> str:
    """One line saying what killed the round, kept short enough to display."""

    message = str(error).strip() or error.__class__.__name__
    reason = f"{error.__class__.__name__}: {message}"
    if len(reason) <= _MAX_FAILURE_REASON:
        return reason
    return reason[: _MAX_FAILURE_REASON - 1] + "…"


def _command_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _declared_default_payload(item: DeclaredDefault) -> dict[str, Any]:
    return {
        "default_id": item.default_id,
        "decision_class": item.decision_class.value,
        "title": item.title,
        "statement": item.statement,
        "rationale": item.rationale,
        "alternatives": list(item.alternatives),
    }


def _repair_payload(item: IntakeRoundRepair) -> dict[str, Any]:
    """Mirror ``IntakeRoundRepair`` field for field, like its sibling payloads."""

    return {
        "code": item.code.value,
        "detail": item.detail,
        "subject_id": item.subject_id,
    }


def _ladder_rung_payload(item: LadderRung) -> dict[str, Any]:
    return {
        "rung": item.rung,
        "name": item.name,
        "relaxes": item.relaxes,
        "default_ids": list(item.default_ids),
        "decision_ids": list(item.decision_ids),
        "parallel_branch": item.parallel_branch,
    }


def _decision_log_summary(session: IntakeSession) -> tuple[dict[str, Any], ...]:
    """Show the auditor what is already settled so it cannot reopen it."""

    latest = _latest_decisions(session)
    return tuple(
        {
            "decision_id": entry.decision_id,
            "title": entry.question.title,
            "prompt": entry.question.prompt,
            "why_it_matters": entry.question.why_it_matters,
            "status": entry.status.value,
            "answer": None if entry.answer is None else _answer_payload(entry.answer),
        }
        for _, entry in sorted(latest.items())
    )


def _keyed_as_recorded(
    question: IntakeQuestion,
    current: DecisionEntry,
    *,
    repairs: IntakeRepairLog,
) -> IntakeQuestion:
    """Keep the semantic key the decision was recorded under.

    The key is the decision's identity across its revisions and has to stay
    unique among the active decisions, so a round may reword a question it did
    not open but may not re-key the decision underneath it.
    """

    if question.semantic_key == current.semantic_key:
        return question
    repairs.record(
        IntakeInvariant.DECISION_KEY_PRESERVED,
        f"decision {current.decision_id} keeps semantic key "
        f"{current.semantic_key}; the round re-keyed it to {question.semantic_key}",
        subject_id=current.decision_id,
    )
    return replace(question, semantic_key=current.semantic_key)


def _with_provenance(
    request: DecisionReopenRequest,
    *,
    fallback: Sequence[str],
    repairs: IntakeRepairLog,
) -> DecisionReopenRequest:
    """Give a reopen request the round's provenance when it brought none.

    Refusing a reopen for missing source messages would leave the old answer
    standing under a question the model no longer agrees with — deciding for
    the user to protect an audit field. The round's own critical message refs
    are the honest fallback, and the repair says the model supplied none.
    """

    if request.source_message_refs:
        return request
    repairs.record(
        IntakeInvariant.REOPEN_PROVENANCE_BACKFILLED,
        f"reopen of decision {request.decision_id} named no source messages; "
        "using this round's critical message references instead",
        subject_id=request.decision_id,
    )
    return replace(request, source_message_refs=tuple(fallback))


def _latest_decisions(session: IntakeSession) -> dict[str, DecisionEntry]:
    latest: dict[str, DecisionEntry] = {}
    for entry in session.decisions:
        latest[entry.decision_id] = entry
    return latest


class PersistentIntakeSessionService:
    """Coordinates user commands, one model round, and one atomic store commit."""

    def __init__(
        self,
        *,
        store: SQLiteIntakeStore,
        advisor: AppServerIntakeSessionAdvisor,
        auditor: ProblemSpecificationAuditor,
        id_factory: IntakeIdFactory = default_intake_id,
        max_frontier_rounds: int = DEFAULT_MAX_FRONTIER_ROUNDS,
        max_audit_rejections: int = DEFAULT_MAX_AUDIT_REJECTIONS,
    ) -> None:
        if max_frontier_rounds < 1 or max_audit_rejections < 1:
            raise ValueError("Intake convergence budgets must be positive")
        self.store = store
        self.advisor = advisor
        self.auditor = auditor
        self.id_factory = id_factory
        self.max_frontier_rounds = max_frontier_rounds
        self.max_audit_rejections = max_audit_rejections

    async def start(
        self,
        initial_message: str,
        *,
        idempotency_key: str,
        model: str = DEFAULT_PRODUCT_MODEL,
        effort: str = DEFAULT_PRODUCT_EFFORT,
        service_tier: str = DEFAULT_PRODUCT_SERVICE_TIER,
    ) -> IntakeSession:
        message = initial_message.strip()
        if not message:
            raise ValueError("initial Intake message must be non-empty")
        model = model.strip()
        effort = effort.strip()
        service_tier = service_tier.strip()
        if not model or not effort or not service_tier:
            raise ValueError("model, effort, and service tier are required")
        if service_tier not in {"standard", "fast"}:
            raise ValueError("service_tier must be standard or fast")
        digest = _command_hash(
            {
                "initial_message": message,
                "model": model,
                "effort": effort,
                "service_tier": service_tier,
            }
        )
        session = self.store.replay_create(
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if session is not None and session.revision > 0:
            return session
        if session is None:
            session = IntakeSession(
                session_id=self.id_factory(),
                revision=0,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(),
                decisions=(),
                frontier=(),
                thread_generations=(),
                model=model,
                effort=effort,
                service_tier=service_tier,
            )
            user_event = _event("user_message", {"text": message})
            self.store.create(
                session,
                events=(user_event,),
                idempotency_key=idempotency_key,
                command_hash=digest,
            )
        else:
            events = self.store.events(session.session_id)
            user_event = next(event for event in events if event.kind == "user_message")
        proposal = await self.advisor.advance(
            session,
            user_submission={"message": message},
            visible_event_ids=(user_event.event_id,),
            resume_thread_id=None,
            mode=IntakeRoundMode.ADVANCE,
            round_number=0,
            max_frontier_rounds=self.max_frontier_rounds,
            audit_rejections=0,
        )
        outcome = await self._audit_candidate(
            proposal,
            session=session,
            initial_problem=message,
            audit_rejections=0,
            mode=IntakeRoundMode.ADVANCE,
        )
        outcome = self._repair_message_refs(
            outcome, (user_event.event_id,), session_id=session.session_id
        )
        advanced, model_events = self._apply_round(session, outcome, round_number=0)
        return self.store.commit(
            advanced,
            base_revision=0,
            idempotency_key=idempotency_key,
            command_hash=digest,
            events=(*model_events, *outcome.events),
        )

    async def submit_round(
        self,
        session_id: str,
        *,
        base_revision: int,
        answers: Mapping[str, DecisionAnswer],
        user_message: str | None = None,
        idempotency_key: str,
    ) -> IntakeSession:
        correction = None if user_message is None else user_message.strip()
        if not answers and not correction:
            raise ValueError("an Intake round requires answers or a correction message")
        digest = _command_hash(
            {
                "base_revision": base_revision,
                "user_message": correction,
                "answers": {
                    key: {
                        **_answer_payload(value),
                        "source_message_refs": list(value.source_message_refs),
                    }
                    for key, value in sorted(answers.items())
                },
            }
        )
        replay = self.store.replay(
            session_id,
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if replay is not None:
            return replay
        session = self.store.get(session_id)
        if session.revision != base_revision:
            raise IntakeStaleRevision(
                f"expected revision {session.revision}, received {base_revision}"
            )
        if session.status is IntakeSessionStatus.CONVERGENCE_REQUIRED:
            raise ValueError(
                "a convergence-required IntakeSession is finalized by the user, "
                "not advanced by another round"
            )
        if session.status not in {
            IntakeSessionStatus.ACTIVE,
            IntakeSessionStatus.CANDIDATE_READY,
        }:
            raise ValueError("only resumable Intake sessions accept round submissions")
        if session.status is IntakeSessionStatus.CANDIDATE_READY and answers:
            raise ValueError(
                "candidate-ready Intake sessions accept correction text only"
            )
        required_answers = frozenset(
            question.decision_id for question in session.frontier if question.blocking
        )
        self._require_answers_present(required_answers, answers)
        # Everything past this point can spend a minute inside the model and
        # then fail, so the answers go into the journal before the round that
        # would otherwise take them down with it.
        self._hold_submission(
            session_id,
            kind=PendingSubmissionKind.ROUND,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            answers=answers,
            user_message=correction,
        )
        try:
            session, replacement_events = await self._upgrade_legacy_thread(session)
            updated, answer_event = self._apply_answers(
                session,
                answers,
                user_message=correction,
                required=required_answers,
            )
            events = self.store.events(session_id)
            round_number = _round_count(events) + 1
            visible_ids = tuple(item.event_id for item in (*events, answer_event))
            proposal = await self.advisor.advance(
                updated,
                user_submission={
                    "message": correction,
                    "answers": {
                        key: _answer_payload(value) for key, value in answers.items()
                    },
                },
                visible_event_ids=visible_ids,
                resume_thread_id=self._active_thread_id(updated),
                mode=IntakeRoundMode.ADVANCE,
                round_number=round_number,
                max_frontier_rounds=self.max_frontier_rounds,
                audit_rejections=session.convergence.audit_rejections,
            )
            outcome = await self._audit_candidate(
                proposal,
                session=updated,
                initial_problem=_initial_problem(events),
                audit_rejections=session.convergence.audit_rejections,
                mode=IntakeRoundMode.ADVANCE,
            )
            outcome = self._repair_message_refs(
                outcome, visible_ids, session_id=session_id
            )
            advanced, model_events = self._apply_round(
                updated,
                outcome,
                round_number=round_number,
            )
            committed = self.store.commit(
                advanced,
                base_revision=base_revision,
                idempotency_key=idempotency_key,
                command_hash=digest,
                events=(
                    *replacement_events,
                    answer_event,
                    *model_events,
                    *outcome.events,
                ),
            )
        except Exception as exc:
            self.store.fail_submission(
                session_id,
                idempotency_key=idempotency_key,
                failure_reason=_failure_reason(exc),
            )
            raise
        self.store.release_submission(session_id, idempotency_key=idempotency_key)
        return committed

    async def finalize(
        self,
        session_id: str,
        *,
        base_revision: int,
        answers: Mapping[str, DecisionAnswer],
        idempotency_key: str,
    ) -> IntakeSession:
        """Start with what is known once the server has stopped asking.

        The user answers whatever problem-class questions are still pending,
        and one finalize round turns every remaining ambiguity into a declared
        default. Neither the advisor nor the audit can send the user back to
        answering from here.
        """

        digest = _command_hash(
            {
                "base_revision": base_revision,
                "action": "finalize",
                "answers": {
                    key: _answer_payload(value)
                    for key, value in sorted(answers.items())
                },
            }
        )
        replay = self.store.replay(
            session_id,
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if replay is not None:
            return replay
        session = self.store.get(session_id)
        if session.revision != base_revision:
            raise IntakeStaleRevision(
                f"expected revision {session.revision}, received {base_revision}"
            )
        if session.status is not IntakeSessionStatus.CONVERGENCE_REQUIRED:
            raise IntakeCommandRejected(
                "intake_not_convergence_required",
                "only a convergence-required IntakeSession can be finalized",
            )
        required_answers = frozenset(
            question.decision_id for question in session.pending_problem_questions
        )
        self._require_answers_present(required_answers, answers)
        # A finalize round is the same shape as an advance round: one long model
        # call between the user's answers and the only commit that keeps them.
        self._hold_submission(
            session_id,
            kind=PendingSubmissionKind.FINALIZE,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            answers=answers,
            user_message=None,
        )
        try:
            session, replacement_events = await self._upgrade_legacy_thread(session)
            updated, answer_event = self._apply_answers(
                session,
                answers,
                user_message=None,
                required=required_answers,
                event_kind="finalize_answers",
            )
            events = self.store.events(session_id)
            visible_ids = tuple(item.event_id for item in (*events, answer_event))
            proposal = await self.advisor.advance(
                updated,
                user_submission={
                    "action": "finalize",
                    "answers": {
                        key: _answer_payload(value) for key, value in answers.items()
                    },
                },
                visible_event_ids=visible_ids,
                resume_thread_id=self._active_thread_id(updated),
                mode=IntakeRoundMode.FINALIZE,
                round_number=_round_count(events),
                max_frontier_rounds=self.max_frontier_rounds,
                audit_rejections=session.convergence.audit_rejections,
            )
            outcome = await self._audit_candidate(
                proposal,
                session=updated,
                initial_problem=_initial_problem(events),
                audit_rejections=session.convergence.audit_rejections,
                mode=IntakeRoundMode.FINALIZE,
            )
            if not outcome.proposal.candidate_ready:
                raise ValueError(
                    "a finalize round must leave the IntakeSession candidate-ready"
                )
            outcome = self._repair_message_refs(
                outcome, visible_ids, session_id=session_id
            )
            convergence = ConvergenceState(
                rounds=session.convergence.rounds,
                audit_rejections=outcome.audit_rejections,
                reason=session.convergence.reason,
                finalized_by_user=True,
            )
            advanced, model_events = self._apply_proposal(
                updated,
                # A finalize round is not supposed to carry questions at all,
                # but it goes through the same repair pass so that whatever it
                # does carry is graded, and so that its repair trail reaches the
                # event chain the same way an advance round's does.
                self._repair_proposal(updated, outcome.proposal),
                status=IntakeSessionStatus.CANDIDATE_READY,
                pending=(),
                convergence=convergence,
            )
            finalize_event = _event(
                "finalize_round",
                {
                    "rounds": convergence.rounds,
                    "audit_rejections": convergence.audit_rejections,
                    "reason": convergence.reason,
                    "finalized_by_user": True,
                    "declared_default_ids": [
                        item.default_id
                        for item in advanced.problem_specifications[
                            -1
                        ].declared_defaults
                    ],
                },
            )
            committed = self.store.commit(
                advanced,
                base_revision=base_revision,
                idempotency_key=idempotency_key,
                command_hash=digest,
                events=(
                    *replacement_events,
                    answer_event,
                    *model_events,
                    *outcome.events,
                    finalize_event,
                ),
            )
        except Exception as exc:
            self.store.fail_submission(
                session_id,
                idempotency_key=idempotency_key,
                failure_reason=_failure_reason(exc),
            )
            raise
        self.store.release_submission(session_id, idempotency_key=idempotency_key)
        return committed

    def confirm(
        self,
        session_id: str,
        *,
        base_revision: int,
        idempotency_key: str,
    ) -> IntakeSession:
        digest = _command_hash({"base_revision": base_revision, "action": "confirm"})
        replay = self.store.replay(
            session_id,
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if replay is not None:
            return replay
        session = self.store.get(session_id)
        if session.revision != base_revision:
            raise IntakeStaleRevision(
                f"expected revision {session.revision}, received {base_revision}"
            )
        if session.status is not IntakeSessionStatus.CANDIDATE_READY:
            raise ValueError("only a candidate-ready IntakeSession can be confirmed")
        specs = list(session.problem_specifications)
        candidate = specs[-1]
        if not candidate.refinement_ladder:
            raise ValueError(
                "this candidate specification predates the refinement ladder; "
                "submit one correction round to regenerate it before confirming"
            )
        specs[-1] = replace(candidate, status=ProblemSpecificationStatus.SUPERSEDED)
        specs.append(
            replace(
                candidate,
                version=candidate.version + 1,
                supersedes_version=candidate.version,
                status=ProblemSpecificationStatus.CONFIRMED,
            )
        )
        generations = tuple(
            replace(item, status=ThreadGenerationStatus.CLOSED)
            if item.status is ThreadGenerationStatus.ACTIVE
            else item
            for item in session.thread_generations
        )
        confirmed = session.next_revision(
            status=IntakeSessionStatus.CONFIRMED,
            problem_specifications=tuple(specs),
            frontier=(),
            thread_generations=generations,
        )
        return self.store.commit(
            confirmed,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            command_hash=digest,
            events=(
                _event(
                    "session_confirmed",
                    {"problem_specification_version": specs[-1].version},
                ),
            ),
        )

    def handoff(self, session_id: str, *, run_id: str) -> IntakeHandoffBundle:
        session = self.store.get(session_id)
        return build_intake_handoff(
            session,
            self.store.events(session_id),
            run_id=run_id,
        )

    def get(self, session_id: str) -> IntakeSession:
        """Return one authoritative IntakeSession snapshot."""

        return self.store.get(session_id)

    def pending_submission(
        self,
        session_id: str,
        *,
        session: IntakeSession | None = None,
    ) -> PendingSubmission | None:
        """Return the submission a failed round left behind, if it still fits.

        A submission is held against the revision it was typed at. Once the
        session has moved past that revision the answers have either landed or
        been superseded, so the journal row is spent and is not offered back.

        ``session`` is the snapshot the caller already holds. Listing every
        active session otherwise re-reads each one from the store just to read
        the revision back off it.
        """

        held = self.store.pending_submission(session_id)
        if held is None:
            return None
        current = session if session is not None else self.store.get(session_id)
        if held.base_revision != current.revision:
            return None
        return held

    def list_active(self) -> tuple[IntakeSession, ...]:
        """Return resumable sessions without exposing store details to HTTP."""

        return self.store.list_active()

    def cancel(
        self,
        session_id: str,
        *,
        base_revision: int,
        idempotency_key: str,
    ) -> IntakeSession:
        digest = _command_hash({"base_revision": base_revision, "action": "cancel"})
        replay = self.store.replay(
            session_id,
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if replay is not None:
            return replay
        session = self.store.get(session_id)
        if session.revision != base_revision:
            raise IntakeStaleRevision(
                f"expected revision {session.revision}, received {base_revision}"
            )
        if session.status not in _RESUMABLE_STATUSES:
            raise ValueError("only a resumable IntakeSession can be cancelled")
        generations = tuple(
            replace(item, status=ThreadGenerationStatus.CLOSED)
            if item.status is ThreadGenerationStatus.ACTIVE
            else item
            for item in session.thread_generations
        )
        cancelled = session.next_revision(
            status=IntakeSessionStatus.CANCELLED,
            frontier=(),
            pending_problem_questions=(),
            thread_generations=generations,
        )
        return self.store.commit(
            cancelled,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            command_hash=digest,
            events=(_event("session_cancelled", {}),),
        )

    async def replace_thread(
        self,
        session_id: str,
        *,
        base_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> IntakeSession:
        replacement_reason = reason.strip()
        if not replacement_reason:
            raise ValueError("thread replacement reason must be non-empty")
        digest = _command_hash(
            {
                "base_revision": base_revision,
                "action": "replace_thread",
                "reason": replacement_reason,
            }
        )
        replay = self.store.replay(
            session_id,
            idempotency_key=idempotency_key,
            command_hash=digest,
        )
        if replay is not None:
            return replay
        session = self.store.get(session_id)
        if session.revision != base_revision:
            raise IntakeStaleRevision(
                f"expected revision {session.revision}, received {base_revision}"
            )
        if session.status not in _RESUMABLE_STATUSES:
            raise ValueError("terminal IntakeSession threads cannot be replaced")
        active = next(
            (
                item
                for item in reversed(session.thread_generations)
                if item.status is ThreadGenerationStatus.ACTIVE
            ),
            None,
        )
        if active is None:
            raise ValueError("IntakeSession has no active thread to replace")
        visible_ids = tuple(item.event_id for item in self.store.events(session_id))
        receipt: ThreadReplacementReceipt = await self.advisor.rehydrate(
            session,
            visible_event_ids=visible_ids,
        )
        generations = list(session.thread_generations)
        generations[-1] = replace(
            active,
            status=ThreadGenerationStatus.SUPERSEDED,
            replacement_reason=replacement_reason,
        )
        generations.append(
            ThreadGeneration(
                generation=active.generation + 1,
                status=ThreadGenerationStatus.ACTIVE,
                app_server_thread_id=receipt.thread_id,
                prompt_version=INTAKE_SESSION_PROMPT_VERSION,
                replaced_generation=active.generation,
                replacement_reason=replacement_reason,
            )
        )
        replaced = session.next_revision(thread_generations=tuple(generations))
        return self.store.commit(
            replaced,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            command_hash=digest,
            events=(
                _event(
                    "thread_replaced",
                    {
                        "old_thread_id": active.app_server_thread_id,
                        "new_thread_id": receipt.thread_id,
                        "turn_id": receipt.turn_id,
                        "reason": replacement_reason,
                        "canonical_state_sha256": receipt.canonical_state_sha256,
                    },
                ),
            ),
        )

    async def _audit_candidate(
        self,
        proposal: IntakeRoundProposal,
        *,
        session: IntakeSession,
        initial_problem: str,
        audit_rejections: int,
        mode: IntakeRoundMode,
    ) -> _RoundOutcome:
        """Audit a candidate specification and spend the rejection budget.

        The first rejection returns its blockers to the frontier as before. The
        rejection that exhausts the budget does not: an auditor that can always
        find one more ambiguity would otherwise never let the session finish, so
        the session stops and the user decides whether to start.
        """

        if not proposal.candidate_ready:
            return _RoundOutcome(proposal, (), audit_rejections, None)
        audit = await self.auditor.audit(
            proposal.problem_specification,
            initial_problem=initial_problem,
            decision_log=_decision_log_summary(session),
            mode=mode,
            session_id=session.session_id,
        )
        merged = replace(
            proposal,
            problem_specification=replace(
                proposal.problem_specification,
                declared_defaults=_merge_defaults(
                    proposal.problem_specification.declared_defaults,
                    audit.declared_defaults,
                ),
            ),
            # The audit repaired its own payload the way the round path does;
            # both trails belong to the same round.
            repairs=(*proposal.repairs, *audit.repairs),
        )
        event = _event(
            "specification_audit",
            {
                "passed": audit.passed,
                "summary": audit.public_summary,
                "thread_id": audit.thread_id,
                "turn_id": audit.turn_id,
                "mode": mode.value,
                "question_ids": [
                    question.question_id for question in audit.blocking_questions
                ],
                "declared_default_ids": [
                    item.default_id for item in audit.declared_defaults
                ],
                "residual_risks": list(audit.residual_risks),
            },
        )
        if audit.passed:
            return _RoundOutcome(merged, (event,), audit_rejections, None)
        rejections = audit_rejections + 1
        blocked = replace(
            merged,
            public_summary=(
                merged.public_summary + " Audit found blocking ambiguities."
            ),
            problem_specification=replace(
                merged.problem_specification,
                status=ProblemSpecificationStatus.DRAFT,
            ),
            questions=(*merged.questions, *audit.blocking_questions),
            candidate_ready=False,
        )
        reason = (
            CONVERGENCE_MAX_AUDIT_REJECTIONS
            if rejections >= self.max_audit_rejections
            else None
        )
        return _RoundOutcome(blocked, (event,), rejections, reason)

    def _apply_round(
        self,
        session: IntakeSession,
        outcome: _RoundOutcome,
        *,
        round_number: int,
    ) -> tuple[IntakeSession, tuple[ConversationEvent, ...]]:
        """Place this round's questions on the frontier, or stop asking."""

        # Repair before the frontier is read off the round: a question this
        # round should never have re-asked must be gone from every list that
        # quotes it, not only from the decision log.
        proposal = self._repair_proposal(session, outcome.proposal)
        reason = outcome.convergence_reason
        if not proposal.candidate_ready and round_number >= self.max_frontier_rounds:
            reason = reason or CONVERGENCE_MAX_FRONTIER_ROUNDS
        if reason is not None:
            status = IntakeSessionStatus.CONVERGENCE_REQUIRED
            pending = proposal.questions
        elif proposal.candidate_ready:
            status = IntakeSessionStatus.CANDIDATE_READY
            pending = ()
        else:
            status = IntakeSessionStatus.ACTIVE
            pending = ()
        convergence = ConvergenceState(
            rounds=round_number,
            audit_rejections=outcome.audit_rejections,
            reason=reason,
            finalized_by_user=session.convergence.finalized_by_user,
        )
        advanced, events = self._apply_proposal(
            session,
            proposal,
            status=status,
            pending=pending,
            convergence=convergence,
        )
        if reason is None:
            return advanced, events
        return advanced, (
            *events,
            _event(
                "convergence_required",
                {
                    "reason": reason,
                    "rounds": convergence.rounds,
                    "audit_rejections": convergence.audit_rejections,
                    "pending_question_ids": [
                        question.question_id for question in pending
                    ],
                },
            ),
        )

    @staticmethod
    def _active_thread_id(session: IntakeSession) -> str | None:
        active = next(
            (
                item
                for item in reversed(session.thread_generations)
                if item.status is ThreadGenerationStatus.ACTIVE
            ),
            None,
        )
        return None if active is None else active.app_server_thread_id

    async def _upgrade_legacy_thread(
        self,
        session: IntakeSession,
    ) -> tuple[IntakeSession, tuple[ConversationEvent, ...]]:
        active = next(
            (
                item
                for item in reversed(session.thread_generations)
                if item.status is ThreadGenerationStatus.ACTIVE
            ),
            None,
        )
        if active is None or active.prompt_version == INTAKE_SESSION_PROMPT_VERSION:
            return session, ()

        receipt = await self.advisor.rehydrate(
            session,
            visible_event_ids=tuple(
                item.event_id for item in self.store.events(session.session_id)
            ),
        )
        generations = list(session.thread_generations)
        generations[-1] = replace(
            active,
            status=ThreadGenerationStatus.SUPERSEDED,
            replacement_reason="prompt_version_upgrade",
        )
        generations.append(
            ThreadGeneration(
                generation=active.generation + 1,
                status=ThreadGenerationStatus.ACTIVE,
                app_server_thread_id=receipt.thread_id,
                prompt_version=INTAKE_SESSION_PROMPT_VERSION,
                replaced_generation=active.generation,
                replacement_reason="prompt_version_upgrade",
            )
        )
        upgraded = replace(session, thread_generations=tuple(generations))
        return (
            upgraded,
            (
                _event(
                    "thread_replaced",
                    {
                        "old_thread_id": active.app_server_thread_id,
                        "new_thread_id": receipt.thread_id,
                        "turn_id": receipt.turn_id,
                        "reason": "prompt_version_upgrade",
                        "canonical_state_sha256": receipt.canonical_state_sha256,
                    },
                ),
            ),
        )

    @staticmethod
    def _repair_message_refs(
        outcome: _RoundOutcome,
        visible_event_ids: Sequence[str],
        *,
        session_id: str,
    ) -> _RoundOutcome:
        """Drop the Conversation Archive events this round invented.

        The host tells the model exactly which events it may cite; a reference
        to anything else names something the user cannot open, so it is not
        provenance at all. This used to raise — after ``advance`` had returned,
        so past both the repair pass and the one corrective turn — and took the
        user's whole answered round with it over a citation they could never
        have seen. Nothing here needs the user's judgement and nothing here is
        scientific content, so every case is repaired: the unknown references
        are dropped and recorded, and a reopen request left without any
        provenance is backfilled by ``_repair_proposal`` as usual.
        """

        visible = set(visible_event_ids)
        proposal = outcome.proposal
        repairs = IntakeRepairLog(session_id=session_id)

        def keep(
            refs: Sequence[str], where: str, subject: str | None
        ) -> tuple[str, ...]:
            kept = tuple(ref for ref in refs if ref in visible)
            dropped = tuple(ref for ref in refs if ref not in visible)
            if dropped:
                repairs.record(
                    IntakeInvariant.UNKNOWN_MESSAGE_REF_DROPPED,
                    f"{where} cited Conversation Archive events this session "
                    "never showed the model: " + ", ".join(sorted(set(dropped))),
                    subject_id=subject,
                )
            return kept

        specification = replace(
            proposal.problem_specification,
            critical_message_refs=keep(
                proposal.problem_specification.critical_message_refs,
                "the problem specification",
                None,
            ),
        )
        reopens = tuple(
            replace(
                request,
                source_message_refs=keep(
                    request.source_message_refs,
                    f"the reopen of decision {request.decision_id}",
                    request.decision_id,
                ),
            )
            for request in proposal.reopen_requests
        )
        entries = repairs.entries()
        if not entries:
            return outcome
        return replace(
            outcome,
            proposal=replace(
                proposal,
                problem_specification=specification,
                reopen_requests=reopens,
                repairs=(*proposal.repairs, *entries),
            ),
        )

    def _hold_submission(
        self,
        session_id: str,
        *,
        kind: PendingSubmissionKind,
        base_revision: int,
        idempotency_key: str,
        answers: Mapping[str, DecisionAnswer],
        user_message: str | None,
    ) -> None:
        self.store.hold_submission(
            PendingSubmission(
                session_id=session_id,
                kind=kind,
                base_revision=base_revision,
                idempotency_key=idempotency_key,
                submitted_at=datetime.now(UTC).isoformat(),
                answers=dict(answers),
                user_message=user_message,
            )
        )

    def _apply_answers(
        self,
        session: IntakeSession,
        answers: Mapping[str, DecisionAnswer],
        *,
        user_message: str | None,
        required: frozenset[str],
        event_kind: str = "user_answers",
    ) -> tuple[IntakeSession, ConversationEvent]:
        latest = _latest_decisions(session)
        self._require_answers_present(required, answers)
        unknown = set(answers) - set(latest)
        if unknown:
            raise ValueError(
                "answers reference unknown decisions: " + ", ".join(unknown)
            )
        answer_event = _event(
            event_kind,
            {
                "base_revision": session.revision,
                "message": user_message,
                "answers": {
                    decision_id: _answer_payload(answer)
                    for decision_id, answer in sorted(answers.items())
                },
            },
        )
        decisions = list(session.decisions)
        for decision_id, answer in answers.items():
            current = latest[decision_id]
            if current.status is not DecisionStatus.OPEN:
                raise ValueError(f"decision {decision_id} is not open")
            index = max(
                position
                for position, item in enumerate(decisions)
                if item.decision_id == decision_id
            )
            decisions[index] = replace(current, status=DecisionStatus.SUPERSEDED)
            decisions.append(
                DecisionEntry(
                    decision_id=current.decision_id,
                    semantic_key=current.semantic_key,
                    status=DecisionStatus.RESOLVED,
                    question=current.question,
                    revision=current.revision + 1,
                    supersedes_revision=current.revision,
                    answer=replace(
                        answer,
                        source_message_refs=(answer_event.event_id,),
                    ),
                    source_message_refs=(answer_event.event_id,),
                )
            )
        remaining = tuple(
            question
            for question in session.frontier
            if question.decision_id not in answers
        )
        still_pending = tuple(
            question
            for question in session.pending_problem_questions
            if question.decision_id not in answers
        )
        return (
            replace(
                session,
                decisions=tuple(decisions),
                frontier=remaining,
                pending_problem_questions=still_pending,
            ),
            answer_event,
        )

    @staticmethod
    def _require_answers_present(
        required: frozenset[str],
        answers: Mapping[str, DecisionAnswer],
    ) -> None:
        if required.issubset(answers):
            return
        missing = sorted(required - set(answers))
        raise IntakeCommandRejected(
            "intake_answers_incomplete",
            "missing blocking Intake answers: " + ", ".join(missing),
        )

    @staticmethod
    def _repair_proposal(
        session: IntakeSession,
        proposal: IntakeRoundProposal,
    ) -> IntakeRoundProposal:
        """Grade this round against the decision log and repair what it can.

        The parse path sees the payload and the bare list of decision ids; it
        cannot tell an open decision from a resolved one, nor a reworded
        question from a repeated one. Those collisions used to raise here,
        after the model call was paid for and after the user had answered the
        whole frontier — the same late, expensive failure the parse path stopped
        producing. They get the same treatment, under the same rule: a repair
        either drops something the user could never have seen or hands the
        decision back to them, and never settles one on their behalf.
        """

        latest = _latest_decisions(session)
        repairs = IntakeRepairLog(session_id=session.session_id)
        # The parse path deduped the advisor's questions; the auditor's were
        # merged in after it, so the round can still name one decision twice.
        incoming = deduplicate_intake_questions(proposal.questions, repairs=repairs)
        reopens: list[DecisionReopenRequest] = []
        for request in proposal.reopen_requests:
            current = latest.get(request.decision_id)
            if current is not None and current.status is DecisionStatus.OPEN:
                # The decision is already waiting on the user. Reopening it
                # would supersede a revision nobody answered; the replacement
                # question still reaches them through the question pass below.
                repairs.record(
                    IntakeInvariant.REDUNDANT_REOPEN_DROPPED,
                    f"decision {request.decision_id} is already open, so there "
                    "is nothing to reopen",
                    subject_id=request.decision_id,
                )
                continue
            reopens.append(
                _with_provenance(
                    request,
                    fallback=proposal.problem_specification.critical_message_refs,
                    repairs=repairs,
                )
            )
        reopen_ids = {item.decision_id for item in reopens}
        questions: list[IntakeQuestion] = []
        for question in incoming:
            current = latest.get(question.decision_id)
            if current is None:
                questions.append(question)
                continue
            question = _keyed_as_recorded(question, current, repairs=repairs)
            if question.decision_id in reopen_ids:
                # The model filed the reopen itself; that path already asks the
                # user again and supersedes their old answer.
                questions.append(question)
                continue
            if current.status is DecisionStatus.RESOLVED:
                if current.question == question:
                    # The user answered exactly this. Re-asking carries no new
                    # information, and nothing the user has not already seen is
                    # lost by dropping it.
                    repairs.record(
                        IntakeInvariant.REPEATED_DECISION_DROPPED,
                        f"decision {question.decision_id} was already answered "
                        "and re-asked unchanged",
                        subject_id=question.decision_id,
                    )
                    continue
                # The wording changed, so the model is asking something the
                # user has not answered. Keeping the old answer under the new
                # question would put words in their mouth and dropping the
                # question would hide the change, so the decision is reopened
                # and they answer it again.
                repairs.record(
                    IntakeInvariant.RESOLVED_DECISION_REOPENED,
                    f"decision {question.decision_id} was re-asked with a "
                    "different question and no reopen request; reopened so the "
                    "answer is the user's again",
                    subject_id=question.decision_id,
                )
                reopens.append(
                    DecisionReopenRequest(
                        decision_id=question.decision_id,
                        reason=(
                            "This was asked again with different wording "
                            "instead of being formally reopened, so the earlier "
                            "answer no longer settles it."
                        ),
                        source_message_refs=(
                            proposal.problem_specification.critical_message_refs
                        ),
                    )
                )
                questions.append(question)
                continue
            if current.status is DecisionStatus.OPEN and current.question != question:
                # Still open, still unanswered: the user only ever sees the
                # newest wording, so taking it costs them nothing.
                repairs.record(
                    IntakeInvariant.OPEN_QUESTION_REWORDED,
                    f"decision {question.decision_id} is still open and was "
                    "reworded; the new wording replaces the old one",
                    subject_id=question.decision_id,
                )
            # Any other status is a decision log shape the host does not model;
            # the apply path refuses it rather than guessing what it means.
            questions.append(question)
        return replace(
            proposal,
            questions=tuple(questions),
            reopen_requests=tuple(reopens),
            repairs=(*proposal.repairs, *repairs.entries()),
        )

    def _apply_proposal(
        self,
        session: IntakeSession,
        proposal: IntakeRoundProposal,
        *,
        status: IntakeSessionStatus,
        pending: tuple[IntakeQuestion, ...],
        convergence: ConvergenceState,
    ) -> tuple[IntakeSession, tuple[ConversationEvent, ...]]:
        """Fold one repaired round into the session and the event chain.

        Callers pass a proposal that has already been through
        ``_repair_proposal``; the raises left here are backstops for a payload
        that reached this point without it.
        """

        latest = _latest_decisions(session)
        decisions = list(session.decisions)
        questions = {item.decision_id: item for item in proposal.questions}
        reopen_ids = {item.decision_id for item in proposal.reopen_requests}
        if not reopen_ids.issubset(questions):
            raise ValueError("every reopen request requires a replacement question")
        for request in proposal.reopen_requests:
            current = latest.get(request.decision_id)
            if current is None or current.status is not DecisionStatus.RESOLVED:
                raise ValueError("only a resolved decision can be reopened")
            index = max(
                position
                for position, item in enumerate(decisions)
                if item.decision_id == request.decision_id
            )
            decisions[index] = replace(current, status=DecisionStatus.SUPERSEDED)
            replacement = questions[request.decision_id]
            decisions.append(
                DecisionEntry(
                    decision_id=current.decision_id,
                    semantic_key=current.semantic_key,
                    status=DecisionStatus.OPEN,
                    question=replacement,
                    revision=current.revision + 1,
                    supersedes_revision=current.revision,
                    reopen_reason=request.reason,
                    source_message_refs=request.source_message_refs,
                )
            )
        latest = {entry.decision_id: entry for entry in decisions}
        for question in proposal.questions:
            if question.decision_id in reopen_ids:
                continue
            current = latest.get(question.decision_id)
            if current is None:
                decisions.append(
                    DecisionEntry(
                        decision_id=question.decision_id,
                        semantic_key=question.semantic_key,
                        status=DecisionStatus.OPEN,
                        question=question,
                        source_message_refs=(
                            proposal.problem_specification.critical_message_refs
                        ),
                    )
                )
            elif current.status is DecisionStatus.OPEN:
                if current.question != question:
                    # Reworded while still unanswered: the newest wording is the
                    # only one the user will ever be asked, so it replaces the
                    # old one in place instead of opening a second revision.
                    index = max(
                        position
                        for position, item in enumerate(decisions)
                        if item.decision_id == question.decision_id
                    )
                    decisions[index] = replace(current, question=question)
            else:
                raise ValueError(
                    "model repeated or changed an existing decision without reopen"
                )
        specs = list(session.problem_specifications)
        specification = proposal.problem_specification
        if specs:
            # Declared defaults accumulate: a default the user has already been
            # shown stays visible even if this round's wording drops it.
            specification = replace(
                specification,
                declared_defaults=_merge_defaults(
                    specs[-1].declared_defaults,
                    specification.declared_defaults,
                ),
            )
            specs[-1] = replace(specs[-1], status=ProblemSpecificationStatus.SUPERSEDED)
        specs.append(specification)
        generations = list(session.thread_generations)
        active_thread = next(
            (
                item
                for item in reversed(generations)
                if item.status is ThreadGenerationStatus.ACTIVE
            ),
            None,
        )
        if proposal.created_thread:
            if active_thread is not None:
                raise ValueError("healthy Intake cannot silently create a new thread")
            generations.append(
                ThreadGeneration(
                    generation=len(generations) + 1,
                    status=ThreadGenerationStatus.ACTIVE,
                    app_server_thread_id=proposal.thread_id,
                    prompt_version=INTAKE_SESSION_PROMPT_VERSION,
                )
            )
            thread_event_kind = "thread_started"
        else:
            if (
                active_thread is None
                or active_thread.app_server_thread_id != proposal.thread_id
            ):
                raise ValueError("Intake round resumed a different thread")
            thread_event_kind = "thread_resumed"
        pending_ids = {question.decision_id for question in pending}
        advanced = session.next_revision(
            status=status,
            problem_specifications=tuple(specs),
            decisions=tuple(decisions),
            frontier=tuple(
                question
                for question in proposal.questions
                if question.decision_id not in pending_ids
            ),
            pending_problem_questions=pending,
            thread_generations=tuple(generations),
            convergence=convergence,
        )
        round_payload: dict[str, Any] = {
            "summary": proposal.public_summary,
            "questions": [intake_question_payload(item) for item in proposal.questions],
            "declared_defaults": [
                _declared_default_payload(item)
                for item in specification.declared_defaults
            ],
            "refinement_ladder": [
                _ladder_rung_payload(item) for item in specification.refinement_ladder
            ],
            "candidate_ready": proposal.candidate_ready,
        }
        if proposal.repairs:
            # Absent rather than empty when nothing was repaired: this payload
            # is hashed verbatim into the handoff's conversation archive, so a
            # clean round has to serialise exactly as it did before repairs
            # existed, and the key's presence is itself the signal.
            round_payload["repairs"] = [
                _repair_payload(item) for item in proposal.repairs
            ]
        return (
            advanced,
            (
                _event(
                    thread_event_kind,
                    {
                        "thread_id": proposal.thread_id,
                        "turn_id": proposal.turn_id,
                    },
                ),
                _event("assistant_round", round_payload),
            ),
        )
