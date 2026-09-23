from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from derivation_api.service import DerivationServiceError

from derivation_app.app_server_intake_session import (
    INTAKE_SESSION_PROMPT_VERSION,
    DecisionReopenRequest,
    IntakeRoundMode,
    IntakeRoundProposal,
    SpecificationAudit,
    ThreadReplacementReceipt,
)
from derivation_app.intake_session import (
    AnswerMode,
    AnswerStrategy,
    DecisionAnswer,
    DecisionClass,
    DecisionEntry,
    DecisionStatus,
    DeclaredDefault,
    IntakeCommandRejected,
    IntakeIdempotencyConflict,
    IntakeQuestion,
    IntakeSession,
    IntakeSessionStatus,
    IntakeStaleRevision,
    LadderRung,
    PendingSubmission,
    PendingSubmissionKind,
    ProblemSection,
    ProblemSpecification,
    ProblemSpecificationStatus,
    QuestionOption,
    SQLiteIntakeStore,
    ThreadGenerationStatus,
    intake_session_fingerprint,
)
from derivation_app.intake_session_service import PersistentIntakeSessionService
from derivation_app.service import RuntimeDerivationService

LADDER = (
    LadderRung(
        rung=0,
        name="Textbook-simplest baseline",
        relaxes="Nothing; this is the comparable baseline.",
        default_ids=("unit-system",),
    ),
    LadderRung(
        rung=1,
        name="Required deliverable",
        relaxes="Replaces the ideal line shape with a finite broadening.",
    ),
)

DEFAULTS = (
    DeclaredDefault(
        default_id="unit-system",
        decision_class=DecisionClass.CONVENTION,
        title="Unit system",
        statement="Work in SI units throughout.",
        rationale="Interchangeable with Gaussian units.",
        alternatives=("Gaussian units",),
    ),
)


def specification(
    version: int,
    *,
    ready: bool,
    critical_message_ref: str,
) -> ProblemSpecification:
    names = (
        "purpose",
        "scientific_target",
        "givens_and_starting_point",
        "notation_and_conventions",
        "assumptions_and_regime",
        "scope_and_non_goals",
        "required_output",
        "validation_criteria",
        "agent_discretion",
    )
    return ProblemSpecification(
        version=version,
        supersedes_version=None if version == 1 else version - 1,
        status=(
            ProblemSpecificationStatus.CANDIDATE_READY
            if ready
            else ProblemSpecificationStatus.DRAFT
        ),
        sections=tuple(
            ProblemSection(name=name, content=f"Resolved {name}") for name in names
        ),
        critical_message_refs=(critical_message_ref,),
        declared_defaults=DEFAULTS if ready else (),
        refinement_ladder=LADDER if ready else (),
    )


def question(decision_id: str) -> IntakeQuestion:
    return IntakeQuestion(
        question_id=f"question-{decision_id}",
        decision_id=decision_id,
        semantic_key=decision_id,
        title="Convention",
        prompt="Which convention?",
        why_needed="It changes the sign.",
        why_it_matters="The final expression flips sign with the other choice.",
        answer_mode=AnswerMode.SINGLE_CHOICE,
        options=(
            QuestionOption("minus", "Minus", "Use exp(-iwt)."),
            QuestionOption("plus", "Plus", "Use exp(+iwt)."),
        ),
        recommended_option_ids=("minus",),
        recommendation_reason="It matches the given equations.",
        allow_custom=True,
    )


class Advisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.reopen_on_correction = False
        self.always_asks = False
        # What the model round raises instead of answering: a contract
        # violation, a provider error, or a timeout all arrive here.
        self.fails_with: Exception | None = None

    async def advance(self, session, **kwargs):
        self.calls.append({"session": session, **kwargs})
        if self.fails_with is not None:
            raise self.fails_with
        call = len(self.calls)
        version = len(session.problem_specifications) + 1
        critical_ref = kwargs["visible_event_ids"][-1]
        thread_id = kwargs.get("resume_thread_id") or "thread-intake"
        if kwargs.get("mode") is IntakeRoundMode.FINALIZE:
            return IntakeRoundProposal(
                public_summary="Finalized with the declared defaults.",
                problem_specification=specification(
                    version,
                    ready=True,
                    critical_message_ref=critical_ref,
                ),
                questions=(),
                reopen_requests=(),
                candidate_ready=True,
                thread_id=thread_id,
                turn_id=f"turn-{call}",
                created_thread=False,
            )
        if self.always_asks:
            return IntakeRoundProposal(
                public_summary="One more independent choice remains.",
                problem_specification=specification(
                    version,
                    ready=False,
                    critical_message_ref=critical_ref,
                ),
                questions=(question(f"round_{call}"),),
                reopen_requests=(),
                candidate_ready=False,
                thread_id=thread_id,
                turn_id=f"turn-{call}",
                created_thread=call == 1,
            )
        if call == 1:
            return IntakeRoundProposal(
                public_summary="Two independent choices remain.",
                problem_specification=specification(
                    1,
                    ready=False,
                    critical_message_ref=critical_ref,
                ),
                questions=(question("sign"), question("scope")),
                reopen_requests=(),
                candidate_ready=False,
                thread_id=thread_id,
                turn_id="turn-1",
                created_thread=True,
            )
        if self.reopen_on_correction and kwargs["user_submission"]["message"]:
            return IntakeRoundProposal(
                public_summary="The correction reopens the scope decision.",
                problem_specification=specification(
                    version,
                    ready=False,
                    critical_message_ref=critical_ref,
                ),
                questions=(question("scope"),),
                reopen_requests=(
                    DecisionReopenRequest(
                        decision_id="scope",
                        reason="The user changed the requested output scope.",
                        source_message_refs=(critical_ref,),
                    ),
                ),
                candidate_ready=False,
                thread_id=thread_id,
                turn_id=f"turn-{call}",
                created_thread=False,
            )
        return IntakeRoundProposal(
            public_summary="The problem is ready.",
            problem_specification=specification(
                version,
                ready=True,
                critical_message_ref=critical_ref,
            ),
            questions=(),
            reopen_requests=(),
            candidate_ready=True,
            thread_id=thread_id,
            turn_id="turn-2",
            created_thread=False,
        )

    async def rehydrate(self, session, **kwargs):
        self.calls.append({"session": session, "mode": "rehydrate", **kwargs})
        return ThreadReplacementReceipt(
            thread_id="thread-intake-replacement",
            turn_id="turn-rehydrate",
            canonical_state_sha256=intake_session_fingerprint(session),
        )


class Auditor:
    def __init__(self) -> None:
        self.calls = []
        self.block = False
        self.declared_defaults: tuple[DeclaredDefault, ...] = ()
        self.residual_risks: tuple[str, ...] = ()

    async def audit(self, specification, **kwargs):
        self.calls.append({"specification": specification, **kwargs})
        blockers = (
            (replace(question(f"audit_scope_{len(self.calls)}"), grounded_in="target"),)
            if self.block
            else ()
        )
        return SpecificationAudit(
            passed=not self.block,
            public_summary=(
                "A scope ambiguity remains." if self.block else "Specification passes."
            ),
            blocking_questions=blockers,
            thread_id="thread-audit",
            turn_id="turn-audit",
            declared_defaults=self.declared_defaults,
            residual_risks=self.residual_risks,
        )


class IntakeSessionDomainTests(unittest.TestCase):
    def test_frontier_rejects_unresolved_dependencies(self) -> None:
        sign = question("sign")
        dependent_scope = replace(question("scope"), depends_on=("sign",))
        with self.assertRaisesRegex(
            ValueError, "frontier dependencies must already be resolved"
        ):
            IntakeSession(
                session_id="intake_dependency",
                revision=1,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(),
                decisions=(
                    DecisionEntry(
                        decision_id="sign",
                        semantic_key="sign",
                        status=DecisionStatus.OPEN,
                        question=sign,
                    ),
                    DecisionEntry(
                        decision_id="scope",
                        semantic_key="scope",
                        status=DecisionStatus.OPEN,
                        question=dependent_scope,
                    ),
                ),
                frontier=(sign, dependent_scope),
                thread_generations=(),
            )

    def test_resolved_decision_cannot_reappear_without_reopen(self) -> None:
        sign = question("sign")
        with self.assertRaisesRegex(
            ValueError, "frontier questions must point to open decisions"
        ):
            IntakeSession(
                session_id="intake_repeat",
                revision=2,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(),
                decisions=(
                    DecisionEntry(
                        decision_id="sign",
                        semantic_key="sign",
                        status=DecisionStatus.RESOLVED,
                        question=sign,
                        answer=DecisionAnswer(selected_option_ids=("minus",)),
                    ),
                ),
                frontier=(sign,),
                thread_generations=(),
            )

    def test_choice_question_requires_custom_input_and_public_reason(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "choice questions must allow custom input"
        ):
            replace(question("sign"), allow_custom=False)
        with self.assertRaisesRegex(
            ValueError, "recommended options require a public reason"
        ):
            replace(question("sign"), recommendation_reason=None)


class PersistentIntakeSessionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")
        self.advisor = Advisor()
        self.auditor = Auditor()
        self.service = PersistentIntakeSessionService(
            store=self.store,
            advisor=self.advisor,
            auditor=self.auditor,
            id_factory=lambda: "intake_session_1",
        )

    async def _cleanup(self) -> None:
        self.temp.cleanup()

    async def test_start_persists_full_frontier_thread_and_archive(self) -> None:
        session = await self.service.start(
            "Derive the linear response.",
            idempotency_key="start-1",
        )

        self.assertEqual(session.revision, 1)
        self.assertEqual(len(session.frontier), 2)
        self.assertEqual(
            session.thread_generations[0].app_server_thread_id, "thread-intake"
        )
        self.assertEqual(
            tuple(event.kind for event in self.store.events(session.session_id)),
            ("user_message", "thread_started", "assistant_round"),
        )
        assistant_round = self.store.events(session.session_id)[-1]
        self.assertEqual(
            assistant_round.payload["questions"][0]["options"][0],
            {
                "option_id": "minus",
                "label": "Minus",
                "impact": "Use exp(-iwt).",
            },
        )
        self.assertEqual(session.model, "gpt-5.6-sol")
        self.assertEqual(session.effort, "high")
        self.assertEqual(session.service_tier, "fast")

    async def test_start_stores_requested_model_effort_in_session_and_hash(
        self,
    ) -> None:
        session = await self.service.start(
            "Derive the linear response.",
            idempotency_key="start-codex",
            model="gpt-5-codex",
            effort="high",
        )

        self.assertEqual(session.model, "gpt-5-codex")
        self.assertEqual(session.effort, "high")
        self.assertEqual(self.advisor.calls[0]["session"].model, "gpt-5-codex")
        self.assertEqual(self.advisor.calls[0]["session"].effort, "high")
        replay = await self.service.start(
            "Derive the linear response.",
            idempotency_key="start-codex",
            model="gpt-5-codex",
            effort="high",
        )
        self.assertEqual(replay, session)
        with self.assertRaises(IntakeIdempotencyConflict):
            await self.service.start(
                "Derive the linear response.",
                idempotency_key="start-codex",
                model="gpt-5.4",
                effort="high",
            )

    async def test_start_idempotency_replays_without_another_model_round(self) -> None:
        first = await self.service.start("Derive.", idempotency_key="start-1")
        replay = await self.service.start("Derive.", idempotency_key="start-1")

        self.assertEqual(replay, first)
        self.assertEqual(len(self.advisor.calls), 1)
        with self.assertRaises(IntakeIdempotencyConflict):
            await self.service.start(
                "Derive something else.", idempotency_key="start-1"
            )

    async def test_round_resolves_frontier_and_resumes_same_thread(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        answer = DecisionAnswer(
            selected_option_ids=("minus",),
            source_message_refs=("event-user-answer",),
        )

        ready = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )

        self.assertEqual(ready.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertEqual(ready.revision, 2)
        self.assertEqual(len(ready.frontier), 0)
        self.assertEqual(self.advisor.calls[1]["resume_thread_id"], "thread-intake")
        self.assertEqual(
            tuple(item.version for item in ready.problem_specifications), (1, 2)
        )
        self.assertEqual(
            ready.problem_specifications[0].status,
            ProblemSpecificationStatus.SUPERSEDED,
        )
        self.assertEqual(len(self.auditor.calls), 1)
        self.assertEqual(
            self.store.events(ready.session_id)[-1].kind, "specification_audit"
        )

        resolved = next(
            item
            for item in ready.decisions
            if item.decision_id == "sign" and item.status is DecisionStatus.RESOLVED
        )
        self.assertIsNotNone(resolved.answer)
        assert resolved.answer is not None
        self.assertEqual(len(resolved.answer.source_message_refs), 1)
        source_event = next(
            item
            for item in self.store.events(ready.session_id)
            if item.event_id == resolved.answer.source_message_refs[0]
        )
        self.assertEqual(source_event.kind, "user_answers")
        self.assertEqual(
            source_event.payload["answers"]["sign"],
            {
                "selected_option_ids": ["minus"],
                "custom_text": None,
                "strategy": None,
            },
        )

    async def test_candidate_correction_resumes_the_same_thread(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        answer = DecisionAnswer(selected_option_ids=("minus",))
        candidate = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )

        corrected = await self.service.submit_round(
            candidate.session_id,
            base_revision=candidate.revision,
            answers={},
            user_message="Require the static limit as an additional final check.",
            idempotency_key="correction-1",
        )

        self.assertEqual(corrected.revision, candidate.revision + 1)
        self.assertEqual(self.advisor.calls[-1]["resume_thread_id"], "thread-intake")
        self.assertEqual(
            self.advisor.calls[-1]["user_submission"]["message"],
            "Require the static limit as an additional final check.",
        )

    async def test_candidate_correction_versions_spec_decision_and_verbatim_event(
        self,
    ) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        answer = DecisionAnswer(selected_option_ids=("minus",))
        candidate = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )
        self.advisor.reopen_on_correction = True

        corrected = await self.service.submit_round(
            candidate.session_id,
            base_revision=candidate.revision,
            answers={},
            user_message="Narrow the deliverable to the static limit.",
            idempotency_key="correction-1",
        )

        self.assertEqual(
            tuple(item.version for item in corrected.problem_specifications),
            (1, 2, 3),
        )
        self.assertEqual(
            tuple(item.status.value for item in corrected.problem_specifications),
            ("superseded", "superseded", "draft"),
        )
        scope_history = tuple(
            item for item in corrected.decisions if item.decision_id == "scope"
        )
        self.assertEqual(
            tuple((item.revision, item.status.value) for item in scope_history),
            ((1, "superseded"), (2, "superseded"), (3, "open")),
        )
        self.assertEqual(
            scope_history[-1].reopen_reason,
            "The user changed the requested output scope.",
        )
        correction_event = next(
            event
            for event in self.store.events(corrected.session_id)
            if event.payload.get("message")
            == "Narrow the deliverable to the static limit."
        )
        self.assertIn(
            correction_event.event_id,
            corrected.problem_specifications[-1].critical_message_refs,
        )
        self.assertEqual(
            scope_history[-1].source_message_refs,
            (correction_event.event_id,),
        )
        self.assertEqual(
            scope_history[-1].question.prompt,
            "Which convention?",
        )

    async def test_failed_audit_returns_only_real_blocker_to_frontier(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        self.auditor.block = True
        answer = DecisionAnswer(
            selected_option_ids=("minus",),
            source_message_refs=("event-user-answer",),
        )

        session = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )

        self.assertEqual(session.status, IntakeSessionStatus.ACTIVE)
        self.assertEqual(
            tuple(item.decision_id for item in session.frontier), ("audit_scope_1",)
        )
        self.assertEqual(session.convergence.audit_rejections, 1)
        self.assertIsNone(session.convergence.reason)
        self.assertEqual(
            session.problem_specifications[-1].status,
            ProblemSpecificationStatus.DRAFT,
        )

    async def test_idempotent_replay_does_not_allocate_another_model_call(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        answer = DecisionAnswer(
            selected_option_ids=("minus",),
            source_message_refs=("event-user-answer",),
        )
        kwargs = {
            "session_id": started.session_id,
            "base_revision": started.revision,
            "answers": {"sign": answer, "scope": answer},
            "idempotency_key": "round-1",
        }
        first = await self.service.submit_round(**kwargs)
        replay = await self.service.submit_round(**kwargs)

        self.assertEqual(replay, first)
        self.assertEqual(len(self.advisor.calls), 2)

    async def test_thread_replacement_preserves_state_and_records_lineage(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        replaced = await self.service.replace_thread(
            started.session_id,
            base_revision=started.revision,
            reason="App Server reported the original thread missing.",
            idempotency_key="replace-1",
        )

        self.assertEqual(replaced.revision, 2)
        self.assertEqual(len(replaced.thread_generations), 2)
        self.assertEqual(replaced.thread_generations[0].status.value, "superseded")
        self.assertEqual(
            replaced.thread_generations[1].app_server_thread_id,
            "thread-intake-replacement",
        )
        self.assertEqual(
            self.store.events(replaced.session_id)[-1].kind, "thread_replaced"
        )

    async def test_legacy_thread_is_upgraded_before_the_next_round(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        legacy_generation = replace(
            started.thread_generations[0],
            prompt_version="intake-grill-v2",
        )
        legacy = self.store.commit(
            started.next_revision(thread_generations=(legacy_generation,)),
            base_revision=started.revision,
            idempotency_key="mark-legacy",
            command_hash="sha256:mark-legacy",
        )
        answer = DecisionAnswer(selected_option_ids=("minus",))

        advanced = await self.service.submit_round(
            legacy.session_id,
            base_revision=legacy.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )

        self.assertEqual(len(advanced.thread_generations), 2)
        self.assertEqual(
            advanced.thread_generations[0].status,
            ThreadGenerationStatus.SUPERSEDED,
        )
        self.assertEqual(
            advanced.thread_generations[1].prompt_version,
            INTAKE_SESSION_PROMPT_VERSION,
        )
        self.assertEqual(
            advanced.thread_generations[1].app_server_thread_id,
            "thread-intake-replacement",
        )
        self.assertIn(
            "thread_replaced",
            tuple(item.kind for item in self.store.events(advanced.session_id)),
        )

    async def test_confirm_freezes_new_spec_version_and_closes_thread(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        answer = DecisionAnswer(
            selected_option_ids=("minus",),
            source_message_refs=("event-user-answer",),
        )
        candidate = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={"sign": answer, "scope": answer},
            idempotency_key="round-1",
        )

        confirmed = self.service.confirm(
            candidate.session_id,
            base_revision=candidate.revision,
            idempotency_key="confirm-1",
        )

        self.assertEqual(confirmed.status, IntakeSessionStatus.CONFIRMED)
        self.assertEqual(
            confirmed.problem_specifications[-1].status,
            ProblemSpecificationStatus.CONFIRMED,
        )
        self.assertEqual(confirmed.thread_generations[-1].status.value, "closed")

    async def test_cancel_closes_thread_and_is_idempotent(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        cancelled = self.service.cancel(
            started.session_id,
            base_revision=started.revision,
            idempotency_key="cancel-1",
        )
        replay = self.service.cancel(
            started.session_id,
            base_revision=started.revision,
            idempotency_key="cancel-1",
        )

        self.assertEqual(cancelled, replay)
        self.assertEqual(cancelled, self.service.get(started.session_id))
        self.assertEqual(cancelled.status, IntakeSessionStatus.CANCELLED)
        self.assertEqual(cancelled.frontier, ())
        self.assertEqual(
            cancelled.thread_generations[-1].status,
            ThreadGenerationStatus.CLOSED,
        )
        self.assertEqual(self.service.list_active(), ())


class IntakeConvergenceGateTests(unittest.IsolatedAsyncioTestCase):
    """The server, not the model, decides when the grill stops asking."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")
        self.advisor = Advisor()
        self.auditor = Auditor()
        self.service = PersistentIntakeSessionService(
            store=self.store,
            advisor=self.advisor,
            auditor=self.auditor,
            id_factory=lambda: "intake_session_1",
        )

    async def _cleanup(self) -> None:
        self.temp.cleanup()

    async def _answer(self, session, key: str, **kwargs):
        answers = {
            item.decision_id: DecisionAnswer(selected_option_ids=("minus",))
            for item in session.frontier
        }
        return await self.service.submit_round(
            session.session_id,
            base_revision=session.revision,
            answers=answers,
            idempotency_key=key,
            **kwargs,
        )

    async def test_third_round_stops_asking_and_parks_the_questions(self) -> None:
        self.advisor.always_asks = True
        session = await self.service.start("Derive.", idempotency_key="start-1")

        self.assertEqual(session.status, IntakeSessionStatus.ACTIVE)
        for index in range(1, 3):
            session = await self._answer(session, f"round-{index}")
            self.assertEqual(session.status, IntakeSessionStatus.ACTIVE)
            self.assertEqual(session.convergence.rounds, index)
            self.assertEqual(len(session.frontier), 1)

        stalled = await self._answer(session, "round-3")

        self.assertEqual(stalled.status, IntakeSessionStatus.CONVERGENCE_REQUIRED)
        self.assertEqual(stalled.frontier, ())
        self.assertEqual(len(stalled.pending_problem_questions), 1)
        self.assertEqual(stalled.convergence.rounds, 3)
        self.assertEqual(stalled.convergence.reason, "max_frontier_rounds")
        self.assertFalse(stalled.convergence.finalized_by_user)
        self.assertEqual(
            self.store.events(stalled.session_id)[-1].kind, "convergence_required"
        )
        with self.assertRaisesRegex(ValueError, "finalized by the user"):
            await self.service.submit_round(
                stalled.session_id,
                base_revision=stalled.revision,
                answers={},
                user_message="Please keep going.",
                idempotency_key="round-4",
            )

    async def test_second_audit_rejection_parks_instead_of_reasking(self) -> None:
        self.auditor.block = True
        started = await self.service.start("Derive.", idempotency_key="start-1")
        first = await self._answer(started, "round-1")

        self.assertEqual(first.status, IntakeSessionStatus.ACTIVE)
        self.assertEqual(first.convergence.audit_rejections, 1)
        self.assertEqual(
            tuple(item.decision_id for item in first.frontier), ("audit_scope_1",)
        )

        self.auditor.declared_defaults = DEFAULTS
        self.auditor.residual_risks = ("The tensor response is out of scope.",)
        stalled = await self._answer(first, "round-2")

        self.assertEqual(stalled.status, IntakeSessionStatus.CONVERGENCE_REQUIRED)
        self.assertEqual(stalled.convergence.audit_rejections, 2)
        self.assertEqual(stalled.convergence.reason, "max_audit_rejections")
        self.assertEqual(stalled.frontier, ())
        self.assertEqual(
            tuple(item.decision_id for item in stalled.pending_problem_questions),
            ("audit_scope_2",),
        )
        self.assertEqual(
            tuple(
                item.default_id
                for item in stalled.problem_specifications[-1].declared_defaults
            ),
            ("unit-system",),
        )

    async def test_finalize_requires_pending_answers_and_reaches_candidate_ready(
        self,
    ) -> None:
        self.advisor.always_asks = True
        session = await self.service.start("Derive.", idempotency_key="start-1")
        for index in range(1, 4):
            session = await self._answer(session, f"round-{index}")
        self.assertEqual(session.status, IntakeSessionStatus.CONVERGENCE_REQUIRED)
        pending = session.pending_problem_questions[0]
        calls_before_invalid_finalize = len(self.advisor.calls)

        with self.assertRaises(IntakeCommandRejected) as missing:
            await self.service.finalize(
                session.session_id,
                base_revision=session.revision,
                answers={},
                idempotency_key="finalize-missing",
            )
        self.assertEqual(missing.exception.code, "intake_answers_incomplete")
        self.assertEqual(len(self.advisor.calls), calls_before_invalid_finalize)

        finalized = await self.service.finalize(
            session.session_id,
            base_revision=session.revision,
            answers={
                pending.decision_id: DecisionAnswer(
                    strategy=AnswerStrategy.SIMPLEST_FIRST
                )
            },
            idempotency_key="finalize-1",
        )

        self.assertEqual(finalized.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertEqual(finalized.pending_problem_questions, ())
        self.assertTrue(finalized.convergence.finalized_by_user)
        self.assertEqual(finalized.convergence.reason, "max_frontier_rounds")
        self.assertEqual(
            self.advisor.calls[-1]["mode"],
            IntakeRoundMode.FINALIZE,
        )
        self.assertEqual(self.auditor.calls[-1]["mode"], IntakeRoundMode.FINALIZE)
        kinds = tuple(event.kind for event in self.store.events(finalized.session_id))
        self.assertIn("finalize_answers", kinds)
        self.assertEqual(kinds[-1], "finalize_round")

        resolved = next(
            item
            for item in finalized.decisions
            if item.decision_id == pending.decision_id
            and item.status is DecisionStatus.RESOLVED
        )
        assert resolved.answer is not None
        self.assertEqual(resolved.answer.strategy, AnswerStrategy.SIMPLEST_FIRST)

        replay = await self.service.finalize(
            session.session_id,
            base_revision=session.revision,
            answers={
                pending.decision_id: DecisionAnswer(
                    strategy=AnswerStrategy.SIMPLEST_FIRST
                )
            },
            idempotency_key="finalize-1",
        )
        self.assertEqual(replay, finalized)

    async def test_finalize_is_refused_outside_convergence_required(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        with self.assertRaises(IntakeCommandRejected) as refused:
            await self.service.finalize(
                started.session_id,
                base_revision=started.revision,
                answers={},
                idempotency_key="finalize-early",
            )
        self.assertEqual(
            refused.exception.code,
            "intake_not_convergence_required",
        )

    async def test_both_routes_strategy_reaches_the_decision_log(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        ready = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers={
                "sign": DecisionAnswer(strategy=AnswerStrategy.BOTH_ROUTES),
                "scope": DecisionAnswer(selected_option_ids=("minus",)),
            },
            idempotency_key="round-1",
        )

        resolved = next(
            item
            for item in ready.decisions
            if item.decision_id == "sign" and item.status is DecisionStatus.RESOLVED
        )
        assert resolved.answer is not None
        self.assertEqual(resolved.answer.strategy, AnswerStrategy.BOTH_ROUTES)
        self.assertEqual(resolved.answer.selected_option_ids, ("minus", "plus"))
        summary = self.auditor.calls[-1]["decision_log"]
        entry = next(item for item in summary if item["decision_id"] == "sign")
        self.assertEqual(entry["answer"]["strategy"], "both_routes")

    async def test_confirm_refuses_a_specification_without_a_ladder(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        candidate = await self._answer(started, "round-1")
        specs = list(candidate.problem_specifications)
        specs[-1] = replace(specs[-1], refinement_ladder=())
        self.store.commit(
            candidate.next_revision(problem_specifications=tuple(specs)),
            base_revision=candidate.revision,
            idempotency_key="strip-ladder",
            command_hash="sha256:strip",
        )

        with self.assertRaisesRegex(ValueError, "predates the refinement ladder"):
            self.service.confirm(
                candidate.session_id,
                base_revision=candidate.revision + 1,
                idempotency_key="confirm-1",
            )


class IntakePendingSubmissionTests(unittest.IsolatedAsyncioTestCase):
    """A round that dies inside the model must not take the answers with it."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")
        self.advisor = Advisor()
        self.auditor = Auditor()
        self.service = PersistentIntakeSessionService(
            store=self.store,
            advisor=self.advisor,
            auditor=self.auditor,
            id_factory=lambda: "intake_session_1",
        )
        self.answers = {
            "sign": DecisionAnswer(
                selected_option_ids=("minus",),
                custom_text="Use the retarded response.",
                source_message_refs=("event-user-answer",),
            ),
            "scope": DecisionAnswer(
                selected_option_ids=("plus",),
                strategy=AnswerStrategy.SIMPLEST_FIRST,
            ),
        }

    async def _cleanup(self) -> None:
        self.temp.cleanup()

    async def test_failed_round_keeps_the_answers_and_the_revision(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        self.advisor.fails_with = ValueError(
            "only a finalize round may declare a problem-class default"
        )

        with self.assertRaises(ValueError):
            await self.service.submit_round(
                started.session_id,
                base_revision=started.revision,
                answers=self.answers,
                user_message="Keep the temperature fixed.",
                idempotency_key="round-1",
            )

        self.assertEqual(self.service.get(started.session_id).revision, started.revision)
        held = self.service.pending_submission(started.session_id)
        assert held is not None
        self.assertEqual(held.kind, PendingSubmissionKind.ROUND)
        self.assertEqual(held.base_revision, started.revision)
        self.assertEqual(held.user_message, "Keep the temperature fixed.")
        self.assertEqual(held.answers, self.answers)
        self.assertEqual(
            held.failure_reason,
            "ValueError: only a finalize round may declare a problem-class default",
        )
        # The event chain is untouched: no answer event, no model events.
        self.assertEqual(
            tuple(event.kind for event in self.store.events(started.session_id)),
            ("user_message", "thread_started", "assistant_round"),
        )

    async def test_a_successful_round_clears_the_journal(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        ready = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=self.answers,
            idempotency_key="round-1",
        )

        self.assertEqual(ready.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertIsNone(self.store.pending_submission(started.session_id))
        self.assertIsNone(self.service.pending_submission(started.session_id))

    async def test_resubmitting_the_kept_answers_runs_a_normal_round(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        self.advisor.fails_with = ValueError("the model round failed")
        with self.assertRaises(ValueError):
            await self.service.submit_round(
                started.session_id,
                base_revision=started.revision,
                answers=self.answers,
                idempotency_key="round-1",
            )
        held = self.service.pending_submission(started.session_id)
        assert held is not None

        self.advisor.fails_with = None
        ready = await self.service.submit_round(
            started.session_id,
            base_revision=held.base_revision,
            answers=dict(held.answers),
            idempotency_key="round-1-retry",
        )

        self.assertEqual(ready.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertEqual(ready.revision, started.revision + 1)
        self.assertIsNone(self.service.pending_submission(started.session_id))
        resolved = {item.decision_id: item for item in ready.decisions}
        self.assertEqual(
            resolved["sign"].answer.custom_text, "Use the retarded response."
        )

    async def test_replaying_a_committed_round_writes_no_new_journal_row(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        ready = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=self.answers,
            idempotency_key="round-1",
        )
        calls = len(self.advisor.calls)

        replay = await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=self.answers,
            idempotency_key="round-1",
        )

        self.assertEqual(replay, ready)
        self.assertEqual(len(self.advisor.calls), calls)
        self.assertIsNone(self.store.pending_submission(started.session_id))

    async def test_a_submission_held_against_a_spent_revision_is_not_offered(
        self,
    ) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")
        self.advisor.fails_with = ValueError("the model round failed")
        with self.assertRaises(ValueError):
            await self.service.submit_round(
                started.session_id,
                base_revision=started.revision,
                answers=self.answers,
                idempotency_key="round-1",
            )
        self.advisor.fails_with = None
        await self.service.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=self.answers,
            idempotency_key="round-2",
        )

        self.assertIsNone(self.service.pending_submission(started.session_id))

    async def test_a_failed_finalize_keeps_the_pending_answers(self) -> None:
        self.advisor.always_asks = True
        session = await self.service.start("Derive.", idempotency_key="start-1")
        for index in range(1, 4):
            session = await self.service.submit_round(
                session.session_id,
                base_revision=session.revision,
                answers={
                    item.decision_id: DecisionAnswer(selected_option_ids=("minus",))
                    for item in session.frontier
                },
                idempotency_key=f"round-{index}",
            )
        self.assertEqual(session.status, IntakeSessionStatus.CONVERGENCE_REQUIRED)
        pending = session.pending_problem_questions[0]
        answers = {
            pending.decision_id: DecisionAnswer(
                selected_option_ids=("minus",),
                custom_text="Treat the drive as monochromatic.",
            )
        }
        self.advisor.fails_with = ValueError("the finalize round failed")

        with self.assertRaises(ValueError):
            await self.service.finalize(
                session.session_id,
                base_revision=session.revision,
                answers=answers,
                idempotency_key="finalize-1",
            )

        self.assertEqual(self.service.get(session.session_id).revision, session.revision)
        held = self.service.pending_submission(session.session_id)
        assert held is not None
        self.assertEqual(held.kind, PendingSubmissionKind.FINALIZE)
        self.assertEqual(held.answers, answers)
        self.assertIsNone(held.user_message)
        self.assertEqual(held.failure_reason, "ValueError: the finalize round failed")

        self.advisor.fails_with = None
        finalized = await self.service.finalize(
            session.session_id,
            base_revision=session.revision,
            answers=dict(held.answers),
            idempotency_key="finalize-2",
        )

        self.assertEqual(finalized.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertIsNone(self.service.pending_submission(session.session_id))

    async def test_a_caller_holding_the_session_is_not_made_to_reload_it(self) -> None:
        """Listing active sessions used to re-read each one from the store.

        Every list entry already carries its snapshot; the journal gate only
        needs the revision off it.
        """

        started = await self.service.start("Derive.", idempotency_key="start-1")
        self.store.hold_submission(
            PendingSubmission(
                session_id=started.session_id,
                kind=PendingSubmissionKind.ROUND,
                base_revision=started.revision,
                idempotency_key="round-held",
                submitted_at="2026-09-18T09:00:00+00:00",
                answers=dict(self.answers),
                user_message=None,
            )
        )
        reads = 0
        original = self.store.get

        def counted(session_id: str):
            nonlocal reads
            reads += 1
            return original(session_id)

        self.store.get = counted  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(self.store, "get", original))

        held = self.service.pending_submission(started.session_id, session=started)

        assert held is not None
        self.assertEqual(held.idempotency_key, "round-held")
        self.assertEqual(reads, 0)
        # And a snapshot the session has moved past still hides the row.
        self.assertIsNone(
            self.service.pending_submission(
                started.session_id,
                session=replace(started, revision=started.revision + 1),
            )
        )

    async def test_a_refused_command_never_reaches_the_journal(self) -> None:
        started = await self.service.start("Derive.", idempotency_key="start-1")

        with self.assertRaises(IntakeStaleRevision):
            await self.service.submit_round(
                started.session_id,
                base_revision=started.revision + 5,
                answers=self.answers,
                idempotency_key="round-stale",
            )

        self.assertIsNone(self.store.pending_submission(started.session_id))


class IntakeHttpErrorMappingTests(unittest.TestCase):
    def test_expected_command_code_survives_the_http_service_boundary(self) -> None:
        with self.assertRaises(DerivationServiceError) as raised:
            RuntimeDerivationService._raise_intake_error(
                IntakeCommandRejected(
                    "intake_answers_incomplete",
                    "Missing answer.",
                )
            )

        self.assertEqual(raised.exception.code, "intake_answers_incomplete")


if __name__ == "__main__":
    unittest.main()
