from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from derivation_app.intake_session import (
    AnswerMode,
    AnswerStrategy,
    ConvergenceState,
    ConversationEvent,
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
    ThreadGeneration,
    ThreadGenerationStatus,
)


def specification(
    *,
    version: int = 1,
    status: ProblemSpecificationStatus = ProblemSpecificationStatus.DRAFT,
) -> ProblemSpecification:
    return ProblemSpecification(
        version=version,
        supersedes_version=None if version == 1 else version - 1,
        status=status,
        sections=tuple(
            ProblemSection(name=name, content=f"Resolved {name}")
            for name in (
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
        ),
        critical_message_refs=("message_1",),
    )


def question(
    decision_id: str,
    *,
    semantic_key: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> IntakeQuestion:
    return IntakeQuestion(
        question_id=f"question_{decision_id}",
        decision_id=decision_id,
        semantic_key=semantic_key or decision_id,
        title="Choose a convention",
        prompt="Which Fourier-sign convention should be used?",
        why_needed="The sign changes the final expression.",
        why_it_matters="The final expression flips sign with the other choice.",
        answer_mode=AnswerMode.SINGLE_CHOICE,
        options=(
            QuestionOption("minus", "exp(-iwt)", "Uses the minus-sign convention."),
            QuestionOption("plus", "exp(+iwt)", "Uses the plus-sign convention."),
        ),
        recommended_option_ids=("minus",),
        recommendation_reason="It matches the supplied starting equations.",
        allow_custom=True,
        depends_on=depends_on,
    )


def open_decision(item: IntakeQuestion) -> DecisionEntry:
    return DecisionEntry(
        decision_id=item.decision_id,
        semantic_key=item.semantic_key,
        status=DecisionStatus.OPEN,
        question=item,
    )


def resolved_decision(item: IntakeQuestion) -> DecisionEntry:
    return DecisionEntry(
        decision_id=item.decision_id,
        semantic_key=item.semantic_key,
        status=DecisionStatus.RESOLVED,
        question=item,
        answer=DecisionAnswer(
            selected_option_ids=("minus",),
            source_message_refs=("message_2",),
        ),
    )


class IntakeSessionDomainTests(unittest.TestCase):
    def test_draft_specification_can_be_incomplete(self) -> None:
        draft = ProblemSpecification(
            version=1,
            status=ProblemSpecificationStatus.DRAFT,
            sections=(
                ProblemSection(name="scientific_target", content="Derive response."),
            ),
        )
        self.assertEqual(draft.sections[0].name, "scientific_target")

    def test_candidate_specification_must_be_scientifically_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "ready problem specification"):
            ProblemSpecification(
                version=1,
                status=ProblemSpecificationStatus.CANDIDATE_READY,
                sections=(
                    ProblemSection(
                        name="scientific_target", content="Derive response."
                    ),
                ),
            )

    def test_frontier_can_hold_multiple_independent_choice_questions(self) -> None:
        first = question("convention")
        second = question("scope", semantic_key="scope_regime")
        session = IntakeSession(
            session_id="intake_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(specification(),),
            decisions=(open_decision(first), open_decision(second)),
            frontier=(first, second),
            thread_generations=(),
        )
        self.assertEqual(
            tuple(item.decision_id for item in session.frontier),
            ("convention", "scope"),
        )
        self.assertTrue(all(item.allow_custom for item in session.frontier))

    def test_frontier_rejects_unresolved_dependencies(self) -> None:
        upstream = question("upstream")
        downstream = question("downstream", depends_on=("upstream",))
        with self.assertRaisesRegex(ValueError, "dependencies"):
            IntakeSession(
                session_id="intake_1",
                revision=0,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(specification(),),
                decisions=(open_decision(upstream), open_decision(downstream)),
                frontier=(downstream,),
                thread_generations=(),
            )

    def test_resolved_decision_cannot_be_reasked(self) -> None:
        item = question("convention")
        with self.assertRaisesRegex(ValueError, "open decisions"):
            IntakeSession(
                session_id="intake_1",
                revision=1,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(specification(),),
                decisions=(resolved_decision(item),),
                frontier=(item,),
                thread_generations=(),
            )

    def test_reopened_decision_preserves_superseded_answer_and_reason(self) -> None:
        item = question("convention")
        prior = replace(
            resolved_decision(item),
            status=DecisionStatus.SUPERSEDED,
        )
        reopened = DecisionEntry(
            decision_id=item.decision_id,
            semantic_key=item.semantic_key,
            status=DecisionStatus.OPEN,
            question=item,
            revision=2,
            supersedes_revision=1,
            reopen_reason="A supplied source uses the opposite Fourier sign.",
        )
        session = IntakeSession(
            session_id="intake_1",
            revision=2,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(specification(),),
            decisions=(prior, reopened),
            frontier=(item,),
            thread_generations=(),
        )
        self.assertEqual(session.decisions[0].answer.selected_option_ids, ("minus",))
        self.assertIn("opposite Fourier sign", session.decisions[1].reopen_reason)

    def test_choice_question_requires_custom_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "custom input"):
            IntakeQuestion(
                question_id="q1",
                decision_id="d1",
                semantic_key="scope",
                title="Scope",
                prompt="Choose scope",
                why_needed="It changes the answer.",
                why_it_matters="It selects a different target quantity.",
                answer_mode=AnswerMode.SINGLE_CHOICE,
                options=(QuestionOption("a", "A", "Use A."),),
                allow_custom=False,
            )

    def test_confirmed_session_requires_confirmed_spec_and_no_blockers(self) -> None:
        session = IntakeSession(
            session_id="intake_1",
            revision=2,
            status=IntakeSessionStatus.CONFIRMED,
            problem_specifications=(
                replace(specification(), status=ProblemSpecificationStatus.SUPERSEDED),
                specification(version=2, status=ProblemSpecificationStatus.CONFIRMED),
            ),
            decisions=(),
            frontier=(),
            thread_generations=(
                ThreadGeneration(
                    generation=1,
                    status=ThreadGenerationStatus.CLOSED,
                    app_server_thread_id="thread_1",
                    prompt_version="intake-v2",
                ),
            ),
        )
        self.assertEqual(session.problem_specifications[-1].version, 2)


class SQLiteIntakeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")
        self.initial = IntakeSession(
            session_id="intake_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(specification(),),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        self.store.create(
            self.initial,
            events=(ConversationEvent("event_1", "user_message", {"text": "derive"}),),
        )

    def test_read_closes_database_connection(self) -> None:
        connection = self.store._connect()

        with patch.object(self.store, "_connect", return_value=connection):
            self.store.get("intake_1")

        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
            connection.execute("SELECT 1")

    def test_round_trip_and_archive_survive_store_reopen(self) -> None:
        reopened = SQLiteIntakeStore(self.store.path)
        self.assertEqual(reopened.get("intake_1"), self.initial)
        self.assertEqual(reopened.events("intake_1")[0].payload["text"], "derive")
        self.assertEqual(reopened.get("intake_1").model, "gpt-5.4")
        self.assertEqual(reopened.get("intake_1").effort, "low")
        self.assertEqual(reopened.get("intake_1").service_tier, "standard")

    def test_explicit_model_effort_and_service_tier_round_trip(self) -> None:
        session = replace(
            self.initial,
            model="gpt-5.6-sol",
            effort="high",
            service_tier="fast",
        )
        store = SQLiteIntakeStore(Path(self.temp.name) / "explicit.sqlite")
        store.create(
            session,
            events=(ConversationEvent("event_1", "user_message", {"text": "derive"}),),
        )
        loaded = SQLiteIntakeStore(store.path).get(session.session_id)
        self.assertEqual(loaded.model, "gpt-5.6-sol")
        self.assertEqual(loaded.effort, "high")
        self.assertEqual(loaded.service_tier, "fast")

    def test_commit_is_atomic_and_idempotent(self) -> None:
        advanced = self.initial.next_revision(
            thread_generations=(
                ThreadGeneration(
                    generation=1,
                    status=ThreadGenerationStatus.ACTIVE,
                    app_server_thread_id="thread_1",
                    prompt_version="intake-v2",
                ),
            )
        )
        first = self.store.commit(
            advanced,
            base_revision=0,
            idempotency_key="round-1",
            command_hash="sha256:one",
            events=(
                ConversationEvent("event_2", "thread_started", {"thread": "thread_1"}),
            ),
        )
        replay = self.store.commit(
            advanced,
            base_revision=0,
            idempotency_key="round-1",
            command_hash="sha256:one",
        )
        self.assertEqual(replay, first)
        self.assertEqual(len(self.store.events("intake_1")), 2)

    def test_idempotency_key_rejects_different_command(self) -> None:
        advanced = self.initial.next_revision()
        self.store.commit(
            advanced,
            base_revision=0,
            idempotency_key="round-1",
            command_hash="sha256:one",
        )
        with self.assertRaises(IntakeIdempotencyConflict):
            self.store.commit(
                advanced,
                base_revision=0,
                idempotency_key="round-1",
                command_hash="sha256:two",
            )

    def test_stale_revision_does_not_overwrite_new_state(self) -> None:
        advanced = self.initial.next_revision()
        self.store.commit(
            advanced,
            base_revision=0,
            idempotency_key="round-1",
            command_hash="sha256:one",
        )
        with self.assertRaises(IntakeStaleRevision):
            self.store.commit(
                advanced,
                base_revision=0,
                idempotency_key="round-2",
                command_hash="sha256:two",
            )
        self.assertEqual(self.store.get("intake_1").revision, 1)


LEGACY_SNAPSHOT_JSON = """{
  "session_id": "intake_legacy",
  "revision": 4,
  "status": "candidate_ready",
  "problem_specifications": [
    {
      "version": 1,
      "status": "candidate_ready",
      "sections": [
        {"name": "purpose", "content": "Legacy purpose", "not_applicable_reason": null},
        {"name": "scientific_target", "content": "Legacy target", "not_applicable_reason": null},
        {"name": "givens_and_starting_point", "content": "Legacy givens", "not_applicable_reason": null},
        {"name": "notation_and_conventions", "content": "Legacy notation", "not_applicable_reason": null},
        {"name": "assumptions_and_regime", "content": "Legacy regime", "not_applicable_reason": null},
        {"name": "scope_and_non_goals", "content": "Legacy scope", "not_applicable_reason": null},
        {"name": "required_output", "content": "Legacy output", "not_applicable_reason": null},
        {"name": "validation_criteria", "content": "Legacy checks", "not_applicable_reason": null},
        {"name": "agent_discretion", "content": "Legacy discretion", "not_applicable_reason": null}
      ],
      "critical_message_refs": ["message_1"],
      "supersedes_version": null
    }
  ],
  "decisions": [
    {
      "decision_id": "convention",
      "semantic_key": "convention",
      "status": "resolved",
      "question": {
        "question_id": "question_convention",
        "decision_id": "convention",
        "semantic_key": "convention",
        "title": "Choose a convention",
        "prompt": "Which Fourier-sign convention should be used?",
        "why_needed": "The sign changes the final expression.",
        "answer_mode": "single_choice",
        "options": [
          {"option_id": "minus", "label": "exp(-iwt)", "impact": "Minus-sign convention."},
          {"option_id": "plus", "label": "exp(+iwt)", "impact": "Plus-sign convention."}
        ],
        "recommended_option_ids": ["minus"],
        "recommendation_reason": "It matches the supplied starting equations.",
        "allow_custom": true,
        "depends_on": [],
        "blocking": false
      },
      "revision": 1,
      "supersedes_revision": null,
      "answer": {
        "selected_option_ids": ["minus"],
        "custom_text": null,
        "source_message_refs": ["message_2"]
      },
      "reopen_reason": null,
      "source_message_refs": ["message_2"]
    }
  ],
  "frontier": [],
  "thread_generations": [
    {
      "generation": 1,
      "status": "active",
      "app_server_thread_id": "thread_legacy",
      "prompt_version": "intake-grill-v2",
      "replaced_generation": null,
      "replacement_reason": null
    }
  ]
}"""


class IntakeDecisionLadderTests(unittest.TestCase):
    def test_selected_strategy_requires_a_selection_or_custom_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            DecisionAnswer(strategy=AnswerStrategy.SELECTED)

        answer = DecisionAnswer(
            custom_text="Use the supplied convention.",
            strategy=AnswerStrategy.SELECTED,
        )
        self.assertEqual(answer.custom_text, "Use the supplied convention.")

    def test_only_problem_class_decisions_can_become_questions(self) -> None:
        for decision_class in (
            DecisionClass.CONVENTION,
            DecisionClass.APPROXIMATION_LEVEL,
        ):
            with self.assertRaisesRegex(ValueError, "declared defaults"):
                replace(question("units"), decision_class=decision_class)

    def test_problem_class_questions_are_always_blocking(self) -> None:
        with self.assertRaisesRegex(ValueError, "always blocking"):
            replace(question("units"), blocking=False)

    def test_question_requires_a_why_it_matters_sentence(self) -> None:
        with self.assertRaisesRegex(ValueError, "why_it_matters"):
            replace(question("units"), why_it_matters="   ")

    def test_ladder_rungs_are_contiguous_and_reference_declared_defaults(self) -> None:
        default = DeclaredDefault(
            default_id="unit-system",
            decision_class=DecisionClass.CONVENTION,
            title="Unit system",
            statement="Work in SI units.",
            rationale="Interchangeable with Gaussian units.",
            alternatives=("Gaussian units",),
        )
        with self.assertRaisesRegex(ValueError, "contiguous and start at zero"):
            replace(
                specification(),
                declared_defaults=(default,),
                refinement_ladder=(
                    LadderRung(rung=1, name="Second", relaxes="Adds broadening."),
                ),
            )
        with self.assertRaisesRegex(ValueError, "undeclared defaults"):
            replace(
                specification(),
                refinement_ladder=(
                    LadderRung(
                        rung=0,
                        name="Baseline",
                        relaxes="Nothing; textbook-simplest baseline.",
                        default_ids=("unit-system",),
                    ),
                ),
            )
        laddered = replace(
            specification(),
            declared_defaults=(default,),
            refinement_ladder=(
                LadderRung(
                    rung=0,
                    name="Baseline",
                    relaxes="Nothing; textbook-simplest baseline.",
                    default_ids=("unit-system",),
                ),
                LadderRung(
                    rung=1,
                    name="Finite temperature",
                    relaxes="Replaces zero-temperature occupation factors.",
                    parallel_branch=True,
                ),
            ),
        )
        self.assertEqual(laddered.refinement_ladder[0].rung, 0)

    def test_both_routes_without_a_selection_means_every_offered_route(self) -> None:
        item = question("units")
        entry = DecisionEntry(
            decision_id=item.decision_id,
            semantic_key=item.semantic_key,
            status=DecisionStatus.RESOLVED,
            question=item,
            answer=DecisionAnswer(strategy=AnswerStrategy.BOTH_ROUTES),
        )
        assert entry.answer is not None
        self.assertEqual(entry.answer.selected_option_ids, ("minus", "plus"))
        self.assertEqual(entry.answer.strategy, AnswerStrategy.BOTH_ROUTES)

    def test_ladder_strategies_are_rejected_on_free_text_questions(self) -> None:
        text_question = IntakeQuestion(
            question_id="question_text",
            decision_id="text_decision",
            semantic_key="text_decision",
            title="Describe the regime",
            prompt="Which regime applies?",
            why_needed="It fixes the physics that is included.",
            why_it_matters="A different regime changes the target quantity.",
            answer_mode=AnswerMode.TEXT,
            allow_custom=True,
        )
        with self.assertRaisesRegex(ValueError, "require a choice question"):
            DecisionEntry(
                decision_id=text_question.decision_id,
                semantic_key=text_question.semantic_key,
                status=DecisionStatus.RESOLVED,
                question=text_question,
                answer=DecisionAnswer(strategy=AnswerStrategy.SIMPLEST_FIRST),
            )

    def test_convergence_required_sessions_replace_the_frontier_with_pending(
        self,
    ) -> None:
        item = question("units")
        decisions = (open_decision(item),)
        with self.assertRaisesRegex(ValueError, "must record a reason"):
            IntakeSession(
                session_id="intake_pending",
                revision=3,
                status=IntakeSessionStatus.CONVERGENCE_REQUIRED,
                problem_specifications=(specification(),),
                decisions=decisions,
                frontier=(),
                thread_generations=(),
                pending_problem_questions=(item,),
            )
        with self.assertRaisesRegex(ValueError, "replace the frontier"):
            IntakeSession(
                session_id="intake_pending",
                revision=3,
                status=IntakeSessionStatus.CONVERGENCE_REQUIRED,
                problem_specifications=(specification(),),
                decisions=decisions,
                frontier=(item,),
                thread_generations=(),
                convergence=ConvergenceState(
                    rounds=3,
                    audit_rejections=0,
                    reason="max_frontier_rounds",
                ),
            )
        stalled = IntakeSession(
            session_id="intake_pending",
            revision=3,
            status=IntakeSessionStatus.CONVERGENCE_REQUIRED,
            problem_specifications=(specification(),),
            decisions=decisions,
            frontier=(),
            thread_generations=(),
            pending_problem_questions=(item,),
            convergence=ConvergenceState(
                rounds=3,
                audit_rejections=0,
                reason="max_frontier_rounds",
            ),
        )
        self.assertEqual(stalled.pending_problem_questions, (item,))
        self.assertFalse(stalled.convergence.finalized_by_user)


class PendingSubmissionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "control.sqlite"
        self.store = SQLiteIntakeStore(self.path)
        self.store.create(
            IntakeSession(
                session_id="intake_1",
                revision=0,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(specification(),),
                decisions=(),
                frontier=(),
                thread_generations=(),
            ),
            events=(ConversationEvent("event_1", "user_message", {"text": "derive"}),),
        )

    def submission(self, **overrides: object) -> PendingSubmission:
        fields: dict[str, object] = {
            "session_id": "intake_1",
            "kind": PendingSubmissionKind.ROUND,
            "base_revision": 0,
            "idempotency_key": "key-1",
            "submitted_at": "2026-09-18T09:00:00+00:00",
            "answers": {
                "convention": DecisionAnswer(
                    selected_option_ids=("minus",),
                    custom_text="Keep the retarded Green function.",
                    source_message_refs=("event_1",),
                    strategy=AnswerStrategy.SIMPLEST_FIRST,
                )
            },
            "user_message": "Also keep the temperature fixed.",
        }
        fields.update(overrides)
        return PendingSubmission(**fields)  # type: ignore[arg-type]

    def test_journal_round_trips_every_part_of_the_answer(self) -> None:
        self.store.hold_submission(self.submission())

        held = self.store.pending_submission("intake_1")

        assert held is not None
        self.assertEqual(held, self.submission())
        self.assertEqual(
            held.answers["convention"].custom_text, "Keep the retarded Green function."
        )
        self.assertEqual(held.answers["convention"].source_message_refs, ("event_1",))
        self.assertEqual(
            held.answers["convention"].strategy, AnswerStrategy.SIMPLEST_FIRST
        )

    def test_a_newer_submission_replaces_the_one_before_it(self) -> None:
        self.store.hold_submission(self.submission())

        self.store.hold_submission(
            self.submission(
                idempotency_key="key-2",
                answers={"convention": DecisionAnswer(selected_option_ids=("plus",))},
                user_message=None,
            )
        )

        held = self.store.pending_submission("intake_1")
        assert held is not None
        self.assertEqual(held.idempotency_key, "key-2")
        self.assertEqual(held.answers["convention"].selected_option_ids, ("plus",))
        self.assertIsNone(held.user_message)

    def test_a_late_failure_cannot_stamp_a_replaced_submission(self) -> None:
        self.store.hold_submission(self.submission())
        self.store.hold_submission(self.submission(idempotency_key="key-2"))

        self.store.fail_submission(
            "intake_1", idempotency_key="key-1", failure_reason="ValueError: too late"
        )

        held = self.store.pending_submission("intake_1")
        assert held is not None
        self.assertIsNone(held.failure_reason)

    def test_release_only_drops_the_submission_it_names(self) -> None:
        self.store.hold_submission(self.submission())

        self.store.release_submission("intake_1", idempotency_key="key-2")
        self.assertIsNotNone(self.store.pending_submission("intake_1"))

        self.store.release_submission("intake_1", idempotency_key="key-1")
        self.assertIsNone(self.store.pending_submission("intake_1"))

    def commit_one_revision(self, idempotency_key: str) -> IntakeSession:
        current = self.store.get("intake_1")
        return self.store.commit(
            current.next_revision(status=IntakeSessionStatus.ACTIVE),
            base_revision=current.revision,
            idempotency_key=idempotency_key,
            command_hash=f"hash-{idempotency_key}",
        )

    def test_a_commit_clears_the_submission_its_revision_left_behind(self) -> None:
        """The 409 retry mints a new key, so release alone leaks the old row.

        Nothing serves it — the revision gate filters it out — but the user's
        answers should not sit in the file forever.
        """

        self.store.hold_submission(self.submission())
        self.store.fail_submission(
            "intake_1", idempotency_key="key-1", failure_reason="ValueError: nope"
        )

        self.commit_one_revision("retry-key")

        self.assertIsNone(self.store.pending_submission("intake_1"))

    def test_a_commit_keeps_a_submission_typed_against_its_own_result(self) -> None:
        """The guarantee the key-scoping gave: a replacement is never collateral.

        The user retyped against revision 1 while the round that produced
        revision 1 was still finishing; that commit must not take their answers.
        """

        committed = self.commit_one_revision("round-key")
        self.store.hold_submission(
            self.submission(base_revision=committed.revision, idempotency_key="key-2")
        )

        self.store.release_submission("intake_1", idempotency_key="round-key")

        held = self.store.pending_submission("intake_1")
        assert held is not None
        self.assertEqual(held.idempotency_key, "key-2")

    def test_journal_survives_reopening_the_store_file(self) -> None:
        self.store.hold_submission(self.submission())
        self.store.fail_submission(
            "intake_1",
            idempotency_key="key-1",
            failure_reason="ValueError: the model declared an undeclared default",
        )

        reopened = SQLiteIntakeStore(self.path)

        held = reopened.pending_submission("intake_1")
        assert held is not None
        self.assertEqual(
            held.failure_reason,
            "ValueError: the model declared an undeclared default",
        )
        self.assertEqual(held.kind, PendingSubmissionKind.ROUND)

    def test_a_store_file_without_the_table_migrates_without_rewriting_rows(
        self,
    ) -> None:
        older = Path(self.temp.name) / "older.sqlite"
        with closing(sqlite3.connect(older)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE intake_sessions (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE intake_events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    UNIQUE (session_id, event_id)
                );
                CREATE TABLE intake_idempotency (
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, idempotency_key)
                );
                CREATE TABLE intake_create_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    command_hash TEXT NOT NULL,
                    session_id TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO intake_sessions VALUES (?, ?, ?, ?)",
                ("intake_legacy", 4, "candidate_ready", LEGACY_SNAPSHOT_JSON),
            )
            connection.execute(
                "INSERT INTO intake_events VALUES (?, ?, ?, ?, ?)",
                ("intake_legacy", 1, "event_1", "user_message", '{"text":"derive"}'),
            )

        store = SQLiteIntakeStore(older)

        self.assertIsNone(store.pending_submission("intake_legacy"))
        self.assertEqual(store.get("intake_legacy").revision, 4)
        self.assertEqual(store.events("intake_legacy")[0].payload["text"], "derive")
        store.hold_submission(self.submission(session_id="intake_legacy"))
        held = store.pending_submission("intake_legacy")
        assert held is not None
        self.assertEqual(held.idempotency_key, "key-1")

    def test_a_held_submission_carries_answers_or_a_correction(self) -> None:
        with self.assertRaisesRegex(ValueError, "answers or a correction"):
            self.submission(answers={}, user_message=None)


class LegacyIntakeSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")

    def test_pre_ladder_snapshot_loads_with_v3_defaults(self) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO intake_sessions VALUES (?, ?, ?, ?)",
                ("intake_legacy", 4, "candidate_ready", LEGACY_SNAPSHOT_JSON),
            )

        session = self.store.get("intake_legacy")

        self.assertEqual(session.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertEqual(session.pending_problem_questions, ())
        self.assertEqual(session.convergence, ConvergenceState())
        self.assertEqual(session.model, "gpt-5.4")
        self.assertEqual(session.effort, "low")
        self.assertEqual(session.service_tier, "standard")
        specification_ = session.problem_specifications[-1]
        self.assertEqual(specification_.declared_defaults, ())
        self.assertEqual(specification_.refinement_ladder, ())
        legacy_question = session.decisions[0].question
        self.assertEqual(legacy_question.decision_class, DecisionClass.PROBLEM)
        self.assertTrue(legacy_question.blocking)
        self.assertEqual(
            legacy_question.why_it_matters,
            "The sign changes the final expression.",
        )
        self.assertIsNone(legacy_question.grounded_in)
        assert session.decisions[0].answer is not None
        self.assertIsNone(session.decisions[0].answer.strategy)

    def test_open_optional_legacy_decision_is_withdrawn_without_hiding_session(
        self,
    ) -> None:
        raw = json.loads(LEGACY_SNAPSHOT_JSON)
        raw["decisions"][0]["status"] = "open"
        raw["decisions"][0]["answer"] = None
        raw["frontier"] = [raw["decisions"][0]["question"]]
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO intake_sessions VALUES (?, ?, ?, ?)",
                ("intake_legacy", 4, "candidate_ready", json.dumps(raw)),
            )

        session = self.store.get("intake_legacy")

        self.assertEqual(session.status, IntakeSessionStatus.CANDIDATE_READY)
        self.assertEqual(session.frontier, ())
        self.assertEqual(session.decisions[0].status, DecisionStatus.SUPERSEDED)
        self.assertIsNone(session.decisions[0].answer)
        self.assertEqual(self.store.list_active(), (session,))


class IntakeCommandRejectedTests(unittest.TestCase):
    def test_carries_a_stable_api_error_code(self) -> None:
        error = IntakeCommandRejected("intake_answers_incomplete", "Missing answer.")

        self.assertEqual(error.code, "intake_answers_incomplete")
        self.assertEqual(str(error), "Missing answer.")


if __name__ == "__main__":
    unittest.main()
