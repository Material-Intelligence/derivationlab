"""What a model round is allowed to get wrong without costing the user a round.

The cases here are the ones observed in production: the model settles a
problem-class decision in an advance round, keeps asking in a finalize round, or
names an id nobody declared. Each used to raise after the model call had already
been paid for, and the bad output stayed in the thread so every retry failed
identically.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from derivation_app.app_server_intake_session import (
    INTAKE_INVARIANT_GRADES,
    INTAKE_SESSION_OUTPUT_SCHEMA,
    MAX_ROUND_QUESTIONS,
    AppServerIntakeSessionAdvisor,
    IntakeContractViolation,
    IntakeInvariant,
    IntakeInvariantGrade,
    IntakeRoundMode,
    intake_session_output_schema,
    parse_intake_round_proposal,
)
from derivation_app.intake_session import (
    AnswerMode,
    DecisionAnswer,
    DecisionClass,
    DecisionEntry,
    DecisionStatus,
    IntakeQuestion,
    IntakeSession,
)
from derivation_runtime.app_server_structured_turn import StructuredTurnResult

from .test_app_server_intake_session import ACTIVE_SESSION, payload


def problem_default(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "default_id": "default_benchmark_silicon",
        "decision_class": "problem",
        "title": "Benchmark material",
        "statement": "Benchmark against silicon.",
        "rationale": "The supplied measurements are for silicon.",
        "alternatives": ["Benchmark against germanium"],
    }
    entry.update(overrides)
    return entry


def resolved_session(*decision_ids: str) -> IntakeSession:
    question = payload()["questions"][0]  # type: ignore[index]
    decisions = tuple(
        DecisionEntry(
            decision_id=decision_id,
            semantic_key=f"key_{decision_id}",
            status=DecisionStatus.RESOLVED,
            answer=DecisionAnswer(custom_text="exp(-iwt)"),
            question=IntakeQuestion(
                question_id=f"question_{decision_id}",
                decision_id=decision_id,
                semantic_key=f"key_{decision_id}",
                title=str(question["title"]),
                prompt=str(question["prompt"]),
                why_needed=str(question["why_needed"]),
                why_it_matters=str(question["why_it_matters"]),
                answer_mode=AnswerMode.TEXT,
                allow_custom=True,
            ),
        )
        for decision_id in decision_ids
    )
    return replace(ACTIVE_SESSION, decisions=decisions)


class SequencedRunner:
    """Returns one payload per call, so a corrective turn can differ."""

    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def run_round(self, **kwargs: object) -> StructuredTurnResult:
        self.calls.append(kwargs)
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return StructuredTurnResult(
            payload=response,
            thread_id="thread-intake",
            turn_id=f"turn-{len(self.calls)}",
            created_thread=kwargs["resume_thread_id"] is None,
        )


def parse(
    body: dict[str, object],
    *,
    mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
    known_decision_ids: tuple[str, ...] | None = None,
):  # type: ignore[no-untyped-def]
    return parse_intake_round_proposal(
        body,
        specification_version=1,
        supersedes_version=None,
        result=StructuredTurnResult(
            payload=body,
            thread_id="thread-intake",
            turn_id="turn-1",
            created_thread=True,
        ),
        mode=mode,
        session_id="intake_session_1",
        known_decision_ids=known_decision_ids,
    )


def codes(proposal) -> list[str]:  # type: ignore[no-untyped-def]
    return [repair.code.value for repair in proposal.repairs]


class SchemaEncodesWhatItCanTests(unittest.TestCase):
    def test_every_invariant_carries_a_grade(self) -> None:
        self.assertEqual(set(INTAKE_INVARIANT_GRADES), set(IntakeInvariant))
        self.assertEqual(
            set(INTAKE_INVARIANT_GRADES.values()), set(IntakeInvariantGrade)
        )

    def test_advance_schema_cannot_express_a_problem_class_default(self) -> None:
        advance = intake_session_output_schema(mode=IntakeRoundMode.ADVANCE)
        defaults = advance["properties"]["declared_defaults"]["items"]

        self.assertEqual(
            defaults["properties"]["decision_class"]["enum"],
            [DecisionClass.CONVENTION.value, DecisionClass.APPROXIMATION_LEVEL.value],
        )
        self.assertEqual(
            advance["properties"]["questions"]["maxItems"], MAX_ROUND_QUESTIONS
        )
        self.assertEqual(advance["properties"]["candidate_ready"], {"type": "boolean"})

    def test_finalize_schema_admits_defaults_of_every_class(self) -> None:
        finalize = intake_session_output_schema(mode=IntakeRoundMode.FINALIZE)
        defaults = finalize["properties"]["declared_defaults"]["items"]

        self.assertEqual(
            defaults["properties"]["decision_class"]["enum"],
            [item.value for item in DecisionClass],
        )
        self.assertEqual(
            finalize["properties"]["candidate_ready"],
            {"type": "boolean", "enum": [True]},
        )

    def test_no_round_schema_carries_a_degenerate_array_bound(self) -> None:
        """A ``maxItems: 0`` would refuse questions in the format.

        It is left out on purpose: no provider is known to have accepted one
        from us, and the round that would carry it is finalize — the last and
        most expensive round to lose. The repair pass enforces it instead.
        """

        for mode in IntakeRoundMode:
            schema = intake_session_output_schema(mode=mode)
            self.assertEqual(
                schema["properties"]["questions"]["maxItems"], MAX_ROUND_QUESTIONS
            )

    def test_reopen_ids_narrow_to_the_decision_log_only_when_it_has_entries(
        self,
    ) -> None:
        empty = intake_session_output_schema()["properties"]["reopen_requests"]
        known = intake_session_output_schema(known_decision_ids=("a", "b", "a"))[
            "properties"
        ]["reopen_requests"]

        self.assertEqual(
            empty["items"]["properties"]["decision_id"], {"type": "string"}
        )
        self.assertEqual(
            known["items"]["properties"]["decision_id"],
            {"type": "string", "enum": ["a", "b"]},
        )

    def test_module_constant_is_the_advance_shape(self) -> None:
        self.assertEqual(
            INTAKE_SESSION_OUTPUT_SCHEMA,
            intake_session_output_schema(mode=IntakeRoundMode.ADVANCE),
        )

    def test_the_two_modes_differ_only_where_intended(self) -> None:
        advance = intake_session_output_schema(mode=IntakeRoundMode.ADVANCE)
        finalize = intake_session_output_schema(mode=IntakeRoundMode.FINALIZE)
        differing = {
            key
            for key in advance["properties"]
            if advance["properties"][key] != finalize["properties"][key]
        }

        self.assertEqual(differing, {"declared_defaults", "candidate_ready"})


class ProblemDefaultBecomesAQuestionTests(unittest.TestCase):
    def test_the_models_answer_survives_as_the_recommended_option(self) -> None:
        body = payload()
        body["declared_defaults"] = [problem_default()]

        proposal = parse(body)

        question = proposal.questions[-1]
        self.assertEqual(
            question.decision_id, "decision_default_default_benchmark_silicon"
        )
        self.assertEqual(
            question.question_id, "question_default_default_benchmark_silicon"
        )
        self.assertEqual(
            question.semantic_key, "declared_default:default_benchmark_silicon"
        )
        self.assertEqual(question.decision_class, DecisionClass.PROBLEM)
        self.assertTrue(question.blocking)
        self.assertTrue(question.allow_custom)
        self.assertEqual(question.options[0].label, "Benchmark against silicon.")
        self.assertEqual(question.recommended_option_ids, ("option_recommended",))
        self.assertIn(
            "“The supplied measurements are for silicon.”",
            question.recommendation_reason or "",
        )
        self.assertEqual(question.options[1].label, "Benchmark against germanium")
        self.assertEqual(proposal.problem_specification.declared_defaults, ())
        self.assertEqual(codes(proposal), ["problem_default_became_question"])

    def test_the_host_says_why_it_is_asking_instead_of_quoting_the_rationale(
        self,
    ) -> None:
        """``rationale`` argues for *not* asking; ``why_needed`` says why we do.

        The web form renders ``why_needed`` as the paragraph explaining why the
        user is being asked, so reusing the advisor's argument for skipping the
        question there tells the user the opposite of the truth.
        """

        body = payload()
        body["declared_defaults"] = [problem_default()]

        question = parse(body).questions[-1]

        self.assertNotIn(
            "The supplied measurements are for silicon.", question.why_needed
        )
        self.assertIn("yours to settle", question.why_needed)
        self.assertIn("instead of asking", question.why_needed)
        # One sentence on what the answer changes, naming this decision rather
        # than asserting physics the host does not know.
        self.assertIn("Benchmark material", question.why_it_matters)
        self.assertIn("whichever option you pick", question.why_it_matters)

    def test_the_recommended_option_and_its_alternatives_read_differently(
        self,
    ) -> None:
        body = payload()
        body["declared_defaults"] = [
            problem_default(
                alternatives=["Benchmark against germanium", "Benchmark against GaAs"]
            )
        ]

        question = parse(body).questions[-1]

        recommended, *alternatives = question.options
        self.assertNotIn(recommended.impact, {item.impact for item in alternatives})
        self.assertIn("proposed default", recommended.impact)
        for alternative in alternatives:
            self.assertIn("displaced", alternative.impact)

    def test_every_word_the_advisor_wrote_is_quoted_where_the_host_repeats_it(
        self,
    ) -> None:
        body = payload()
        body["declared_defaults"] = [problem_default()]

        question = parse(body).questions[-1]

        self.assertIn("“Benchmark against silicon.”", question.prompt)
        self.assertIn("“Benchmark material”", question.why_it_matters)

    def test_the_generated_ids_are_stable_across_retries(self) -> None:
        body = payload()
        body["declared_defaults"] = [problem_default()]

        first = parse(body)
        second = parse(body)

        self.assertEqual(
            [item.decision_id for item in first.questions],
            [item.decision_id for item in second.questions],
        )

    def test_a_ready_round_stops_being_ready(self) -> None:
        body = payload(ready=True)
        body["declared_defaults"] = [
            *body["declared_defaults"],  # type: ignore[misc]
            problem_default(),
        ]

        proposal = parse(body)

        self.assertFalse(proposal.candidate_ready)
        self.assertEqual(proposal.problem_specification.status.value, "draft")

    def test_the_models_own_question_wins_a_collision(self) -> None:
        body = payload()
        model_question = dict(body["questions"][0])  # type: ignore[index]
        model_question["decision_id"] = "decision_default_default_benchmark_silicon"
        model_question["semantic_key"] = "declared_default:default_benchmark_silicon"
        body["questions"] = [model_question]
        body["declared_defaults"] = [problem_default()]

        proposal = parse(body)

        self.assertEqual(len(proposal.questions), 1)
        self.assertEqual(proposal.questions[0].title, "Fourier sign")
        self.assertEqual(codes(proposal), ["problem_default_dropped"])

    def test_a_finalize_round_still_declares_the_default(self) -> None:
        body = payload(ready=True)
        body["declared_defaults"] = [problem_default()]
        body["refinement_ladder"][0]["default_ids"] = []  # type: ignore[index]

        proposal = parse(body, mode=IntakeRoundMode.FINALIZE)

        self.assertTrue(proposal.candidate_ready)
        self.assertEqual(
            proposal.problem_specification.declared_defaults[0].decision_class,
            DecisionClass.PROBLEM,
        )
        self.assertEqual(codes(proposal), [])


class FinalizeQuestionBecomesADefaultTests(unittest.TestCase):
    def test_the_recommended_option_becomes_the_statement(self) -> None:
        body = payload(ready=True)
        body["questions"] = payload()["questions"]

        proposal = parse(body, mode=IntakeRoundMode.FINALIZE)

        self.assertEqual(proposal.questions, ())
        converted = proposal.problem_specification.declared_defaults[-1]
        self.assertEqual(converted.default_id, "default_question_decision_sign")
        self.assertEqual(converted.decision_class, DecisionClass.PROBLEM)
        self.assertEqual(converted.statement, "exp(-iwt)")
        self.assertEqual(converted.alternatives, ("exp(+iwt)",))
        self.assertIn("Which Fourier convention", converted.rationale)
        self.assertEqual(codes(proposal), ["finalize_question_became_default"])

    def test_an_unrecommended_question_falls_back_to_its_first_option(self) -> None:
        body = payload(ready=True)
        question = dict(payload()["questions"][0])  # type: ignore[index]
        question["recommended_option_ids"] = []
        question["recommendation_reason"] = None
        body["questions"] = [question]

        proposal = parse(body, mode=IntakeRoundMode.FINALIZE)

        converted = proposal.problem_specification.declared_defaults[-1]
        self.assertEqual(converted.statement, "exp(-iwt)")
        # Taken by position, so the rationale may not read as an argument the
        # advisor made for this option.
        self.assertIn("first one it listed", converted.rationale)
        self.assertIn(
            "rather than on the advisor's recommendation", converted.rationale
        )

    def test_a_recommended_question_does_not_claim_it_was_picked_by_position(
        self,
    ) -> None:
        body = payload(ready=True)
        body["questions"] = [payload()["questions"][0]]  # type: ignore[index]

        proposal = parse(body, mode=IntakeRoundMode.FINALIZE)

        converted = proposal.problem_specification.declared_defaults[-1]
        self.assertNotIn("first one it listed", converted.rationale)


class BookkeepingRepairTests(unittest.TestCase):
    def test_a_repeated_default_keeps_the_last_wording(self) -> None:
        body = payload(ready=True)
        body["declared_defaults"] = [
            {
                "default_id": "unit-system",
                "decision_class": "convention",
                "title": "Unit system",
                "statement": "Work in SI units throughout.",
                "rationale": "Interchangeable with Gaussian units.",
                "alternatives": [],
            },
            {
                "default_id": "unit-system",
                "decision_class": "convention",
                "title": "Unit system",
                "statement": "Work in Gaussian units throughout.",
                "rationale": "The supplied equations are Gaussian.",
                "alternatives": [],
            },
        ]

        proposal = parse(body)

        defaults = proposal.problem_specification.declared_defaults
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].statement, "Work in Gaussian units throughout.")
        self.assertEqual(codes(proposal), ["duplicate_default_dropped"])

    def test_a_repeated_question_keeps_the_first(self) -> None:
        body = payload()
        first = dict(body["questions"][0])  # type: ignore[index]
        second = dict(first)
        second["title"] = "Fourier sign, again"
        body["questions"] = [first, second]

        proposal = parse(body)

        self.assertEqual(len(proposal.questions), 1)
        self.assertEqual(proposal.questions[0].title, "Fourier sign")
        self.assertEqual(codes(proposal), ["duplicate_question_dropped"])

    def test_a_repeated_semantic_key_keeps_the_first(self) -> None:
        body = payload()
        first = dict(body["questions"][0])  # type: ignore[index]
        second = dict(first)
        second["question_id"] = "question_sign_again"
        second["decision_id"] = "decision_sign_again"
        body["questions"] = [first, second]

        proposal = parse(body)

        self.assertEqual(len(proposal.questions), 1)
        self.assertEqual(codes(proposal), ["duplicate_question_dropped"])

    def test_a_reopen_request_for_an_unknown_decision_is_dropped(self) -> None:
        body = payload()
        body["reopen_requests"] = [
            {
                "decision_id": "decision_never_asked",
                "reason": "The user changed the target.",
                "source_message_refs": ["event_user_2"],
            }
        ]

        proposal = parse(body, known_decision_ids=("decision_sign",))

        self.assertEqual(proposal.reopen_requests, ())
        self.assertEqual(codes(proposal), ["unknown_reopen_dropped"])

    def test_a_reopen_request_without_a_replacement_question_is_dropped(self) -> None:
        body = payload()
        body["questions"] = []
        body["reopen_requests"] = [
            {
                "decision_id": "decision_sign",
                "reason": "The user changed the target.",
                "source_message_refs": ["event_user_2"],
            }
        ]

        proposal = parse(body, known_decision_ids=("decision_sign",))

        self.assertEqual(proposal.reopen_requests, ())
        self.assertEqual(codes(proposal), ["unanswerable_reopen_dropped"])

    def test_a_repeated_reopen_request_keeps_the_first(self) -> None:
        body = payload()
        request = {
            "decision_id": "decision_sign",
            "reason": "The user changed the target.",
            "source_message_refs": ["event_user_2"],
        }
        body["reopen_requests"] = [request, {**request, "reason": "Again."}]

        proposal = parse(body, known_decision_ids=("decision_sign",))

        self.assertEqual(len(proposal.reopen_requests), 1)
        self.assertEqual(
            proposal.reopen_requests[0].reason, "The user changed the target."
        )
        self.assertEqual(codes(proposal), ["duplicate_reopen_dropped"])

    def test_an_unknown_reopen_survives_when_the_caller_knows_no_decisions(
        self,
    ) -> None:
        """``known_decision_ids=None`` means unknown, not empty."""

        body = payload()
        body["reopen_requests"] = [
            {
                "decision_id": "decision_sign",
                "reason": "The user changed the target.",
                "source_message_refs": ["event_user_2"],
            }
        ]

        proposal = parse(body)

        self.assertEqual(proposal.reopen_requests[0].decision_id, "decision_sign")
        self.assertEqual(codes(proposal), [])

    def test_ready_alongside_a_blocking_question_becomes_draft(self) -> None:
        body = payload(ready=True)
        body["questions"] = payload()["questions"]

        proposal = parse(body)

        self.assertFalse(proposal.candidate_ready)
        self.assertEqual(codes(proposal), ["candidate_ready_downgraded"])

    def test_ready_without_a_ladder_becomes_draft_in_an_advance_round(self) -> None:
        body = payload(ready=True)
        body["refinement_ladder"] = []

        proposal = parse(body)

        self.assertFalse(proposal.candidate_ready)
        self.assertEqual(codes(proposal), ["candidate_ready_downgraded"])

    def test_ready_without_a_ladder_still_fails_a_finalize_round(self) -> None:
        body = payload(ready=True)
        body["refinement_ladder"] = []

        with self.assertRaises(IntakeContractViolation) as caught:
            parse(body, mode=IntakeRoundMode.FINALIZE)

        self.assertEqual(
            [item.invariant for item in caught.exception.violations],
            [IntakeInvariant.FINALIZE_WITHOUT_LADDER],
        )

    def test_a_dangling_ladder_default_is_dropped_and_recorded(self) -> None:
        body = payload(ready=True)
        body["refinement_ladder"][0]["default_ids"] = [  # type: ignore[index]
            "unit-system",
            "approx_isotropic_scalar",
        ]

        proposal = parse(body)

        self.assertEqual(
            proposal.problem_specification.refinement_ladder[0].default_ids,
            ("unit-system",),
        )
        self.assertEqual(codes(proposal), ["undeclared_ladder_default_dropped"])
        self.assertEqual(proposal.repairs[0].subject_id, "approx_isotropic_scalar")

    def test_a_repeated_message_reference_is_dropped(self) -> None:
        body = payload()
        body["critical_message_refs"] = ["event_user_1", "event_user_1"]

        proposal = parse(body)

        self.assertEqual(
            proposal.problem_specification.critical_message_refs, ("event_user_1",)
        )
        self.assertEqual(codes(proposal), ["text_entry_dropped"])


class HardInvariantsStayHardTests(unittest.TestCase):
    def test_a_convention_asked_as_a_question_is_refused(self) -> None:
        """Moving a decision between the user and the agent is never repaired.

        The repair for the opposite direction hands a decision *back* to the
        user; doing it this way round would take one away from them.
        """

        body = payload()
        body["questions"][0]["decision_class"] = "convention"  # type: ignore[index]

        with self.assertRaises(IntakeContractViolation) as caught:
            parse(body)

        self.assertEqual(
            [item.invariant for item in caught.exception.violations],
            [IntakeInvariant.QUESTION_NOT_PROBLEM_CLASS],
        )

    def test_a_missing_field_is_refused(self) -> None:
        body = payload()
        del body["questions"][0]["why_it_matters"]  # type: ignore[index]

        with self.assertRaises(IntakeContractViolation) as caught:
            parse(body)

        self.assertEqual(
            caught.exception.violations[0].invariant, IntakeInvariant.PAYLOAD_SHAPE
        )

    def test_a_ready_specification_missing_sections_is_refused(self) -> None:
        body = payload(ready=True)
        body["specification_sections"] = [
            {
                "name": "scientific_target",
                "content": "Derive the response.",
                "not_applicable_reason": None,
            }
        ]

        with self.assertRaisesRegex(IntakeContractViolation, "missing required"):
            parse(body)

    def test_violations_serialise_for_the_corrective_turn(self) -> None:
        body = payload()
        body["questions"][0]["decision_class"] = "convention"  # type: ignore[index]

        with self.assertRaises(IntakeContractViolation) as caught:
            parse(body)

        self.assertEqual(
            caught.exception.as_payload()[0]["code"], "question_not_problem_class"
        )


class SelfCorrectionTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_correction_is_spent_and_the_clean_payload_is_accepted(
        self,
    ) -> None:
        broken = payload()
        broken["questions"][0]["decision_class"] = "convention"  # type: ignore[index]
        runner = SequencedRunner(broken, payload())
        advisor = AppServerIntakeSessionAdvisor(runner)

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(advisor.correction_attempts, 1)
        self.assertEqual(runner.calls[1]["resume_thread_id"], "thread-intake")
        self.assertEqual(
            runner.calls[1]["output_schema"], runner.calls[0]["output_schema"]
        )
        self.assertIn("question_not_problem_class", str(runner.calls[1]["prompt"]))
        self.assertEqual(proposal.questions[0].decision_id, "decision_sign")

    async def test_the_accepted_turn_is_the_one_the_proposal_reports(self) -> None:
        broken = payload()
        broken["questions"][0]["decision_class"] = "convention"  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(SequencedRunner(broken, payload()))

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(proposal.turn_id, "turn-2")
        self.assertEqual(proposal.thread_id, "thread-intake")
        # The first turn created the thread; the caller still has to record it.
        self.assertTrue(proposal.created_thread)

    async def test_a_resumed_round_does_not_claim_to_have_created_a_thread(
        self,
    ) -> None:
        broken = payload()
        broken["questions"][0]["decision_class"] = "convention"  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(SequencedRunner(broken, payload()))

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id="thread-intake",
        )

        self.assertFalse(proposal.created_thread)

    async def test_a_second_violation_raises_without_a_third_turn(self) -> None:
        broken = payload()
        broken["questions"][0]["decision_class"] = "convention"  # type: ignore[index]
        runner = SequencedRunner(broken)
        advisor = AppServerIntakeSessionAdvisor(runner)

        with self.assertRaises(IntakeContractViolation):
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "derive"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(advisor.correction_attempts, 1)

    async def test_a_repaired_round_never_spends_a_correction(self) -> None:
        body = payload()
        body["declared_defaults"] = [problem_default()]
        runner = SequencedRunner(body)
        advisor = AppServerIntakeSessionAdvisor(runner)

        await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(advisor.correction_attempts, 0)

    async def test_a_host_bug_is_not_answered_by_asking_the_model_again(self) -> None:
        class BrokenRunner(SequencedRunner):
            async def run_round(self, **kwargs: object) -> StructuredTurnResult:
                self.calls.append(kwargs)
                raise TypeError("unsupported operand type in the host")

        runner = BrokenRunner(payload())
        advisor = AppServerIntakeSessionAdvisor(runner)

        with self.assertRaises(TypeError):
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "derive"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(advisor.correction_attempts, 0)

    async def test_rehydration_never_enters_the_corrective_turn(self) -> None:
        runner = SequencedRunner({"canonical_state_sha256": "not-the-hash"})
        advisor = AppServerIntakeSessionAdvisor(runner)

        with self.assertRaises(ValueError):
            await advisor.rehydrate(ACTIVE_SESSION, visible_event_ids=("event_user_1",))

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(advisor.correction_attempts, 0)

    async def test_the_round_schema_follows_the_sessions_decision_log(self) -> None:
        runner = SequencedRunner(payload())
        advisor = AppServerIntakeSessionAdvisor(runner)

        await advisor.advance(
            resolved_session("decision_sign"),
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        schema = runner.calls[0]["output_schema"]
        assert isinstance(schema, dict)
        self.assertEqual(
            schema["properties"]["reopen_requests"]["items"]["properties"][
                "decision_id"
            ],
            {"type": "string", "enum": ["decision_sign"]},
        )

    async def test_the_finalize_round_is_sent_the_finalize_schema(self) -> None:
        runner = SequencedRunner(payload(ready=True))
        advisor = AppServerIntakeSessionAdvisor(runner)

        await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "start"},
            visible_event_ids=("event_user_1",),
            resume_thread_id="thread-intake",
            mode=IntakeRoundMode.FINALIZE,
        )

        schema = runner.calls[0]["output_schema"]
        assert isinstance(schema, dict)
        self.assertEqual(
            schema["properties"]["candidate_ready"],
            {"type": "boolean", "enum": [True]},
        )
        # The one finalize constraint the schema cannot carry has to reach the
        # model through the prompt instead.
        self.assertIn("may ask no questions", str(runner.calls[0]["prompt"]))

    async def test_every_repair_is_logged_with_the_session_id(self) -> None:
        body = payload()
        body["declared_defaults"] = [problem_default()]
        advisor = AppServerIntakeSessionAdvisor(SequencedRunner(body))

        with self.assertLogs(
            "derivation_app.app_server_intake_session", level="WARNING"
        ) as logs:
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "derive"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

        output = "\n".join(logs.output)
        self.assertIn("problem_default_became_question", output)
        self.assertIn(ACTIVE_SESSION.session_id, output)


if __name__ == "__main__":
    unittest.main()
