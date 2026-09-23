from __future__ import annotations

import unittest
from dataclasses import replace

from derivation_app.app_server_intake_session import (
    INTAKE_DECISION_CLASS_RULES,
    INTAKE_REHYDRATE_OUTPUT_SCHEMA,
    INTAKE_SESSION_DEVELOPER_INSTRUCTIONS,
    INTAKE_SESSION_OUTPUT_SCHEMA,
    INTAKE_SESSION_PROMPT_VERSION,
    MAX_ROUND_QUESTIONS,
    SPEC_AUDIT_DEVELOPER_INSTRUCTIONS,
    SPEC_AUDIT_OUTPUT_SCHEMA,
    AppServerIntakeSessionAdvisor,
    AppServerProblemSpecificationAuditor,
    IntakeRoundMode,
    intake_session_output_schema,
)
from derivation_app.intake_session import (
    DecisionClass,
    IntakeSession,
    IntakeSessionStatus,
    intake_session_fingerprint,
)
from derivation_runtime.app_server_structured_turn import StructuredTurnResult


def payload(*, ready: bool = False) -> dict[str, object]:
    sections = [
        {
            "name": name,
            "content": f"Resolved {name}",
            "not_applicable_reason": None,
        }
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
    ]
    return {
        "public_summary": "The target is clear; the sign convention remains.",
        "specification_sections": sections,
        "critical_message_refs": ["event_user_1"],
        "questions": (
            []
            if ready
            else [
                {
                    "question_id": "question_sign",
                    "decision_id": "decision_sign",
                    "semantic_key": "fourier_sign",
                    "title": "Fourier sign",
                    "prompt": "Which Fourier convention should be used?",
                    "why_needed": "It changes the final sign.",
                    "why_it_matters": "The final expression flips sign.",
                    "decision_class": "problem",
                    "answer_mode": "single_choice",
                    "options": [
                        {
                            "option_id": "minus",
                            "label": "exp(-iwt)",
                            "impact": "Matches the supplied equations.",
                        },
                        {
                            "option_id": "plus",
                            "label": "exp(+iwt)",
                            "impact": "Requires translating the supplied equations.",
                        },
                    ],
                    "recommended_option_ids": ["minus"],
                    "recommendation_reason": "It matches the starting point.",
                    "allow_custom": True,
                    "depends_on": [],
                    "blocking": True,
                    "grounded_in": None,
                }
            ]
        ),
        "declared_defaults": (
            [
                {
                    "default_id": "unit-system",
                    "decision_class": "convention",
                    "title": "Unit system",
                    "statement": "Work in SI units throughout.",
                    "rationale": "Interchangeable with Gaussian units.",
                    "alternatives": ["Gaussian units"],
                }
            ]
            if ready
            else []
        ),
        "refinement_ladder": (
            [
                {
                    "rung": 0,
                    "name": "Textbook-simplest baseline",
                    "relaxes": "Nothing; this is the comparable baseline.",
                    "default_ids": ["unit-system"],
                    "decision_ids": [],
                    "parallel_branch": False,
                },
                {
                    "rung": 1,
                    "name": "Required deliverable",
                    "relaxes": "Replaces the ideal line shape with a broadening.",
                    "default_ids": [],
                    "decision_ids": ["decision_sign"],
                    "parallel_branch": False,
                },
            ]
            if ready
            else []
        ),
        "reopen_requests": [],
        "candidate_ready": ready,
    }


def audit_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "passed": True,
        "public_summary": "The specification is executable.",
        "blocking_questions": [],
        "declared_defaults": [],
        "residual_risks": [],
    }
    base.update(overrides)
    return base


def audit_question(**overrides: object) -> dict[str, object]:
    question = dict(payload()["questions"][0])  # type: ignore[index]
    question["question_id"] = "question_audit"
    question["decision_id"] = "decision_audit"
    question["semantic_key"] = "audit_scope"
    question["grounded_in"] = "absorption coefficient versus photon energy"
    question.update(overrides)
    return question


class Runner:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def run_round(self, **kwargs: object) -> StructuredTurnResult:
        self.calls.append(kwargs)
        return StructuredTurnResult(
            payload=self.response,
            thread_id="thread-intake",
            turn_id="turn-1",
            created_thread=kwargs["resume_thread_id"] is None,
        )


class AuditRunner:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def run_ephemeral(self, **kwargs: object) -> StructuredTurnResult:
        self.calls.append(kwargs)
        return StructuredTurnResult(
            payload=self.response,
            thread_id="thread-audit",
            turn_id="turn-audit",
            created_thread=True,
        )


class AppServerIntakeSessionAdvisorTests(unittest.IsolatedAsyncioTestCase):
    def test_model_output_schemas_use_supported_structured_output_subset(self) -> None:
        unsupported_keywords = {"minLength", "uniqueItems"}

        def walk(value: object) -> None:
            if isinstance(value, dict):
                self.assertFalse(unsupported_keywords.intersection(value))
                # An empty enum matches nothing and a zero-length array bound is
                # the degenerate case of a bound: both are legal JSON Schema and
                # neither is known to survive the provider, so no round's schema
                # may carry one.
                for keyword in ("maxItems", "minItems"):
                    if keyword in value:
                        self.assertGreater(value[keyword], 0)
                if "enum" in value:
                    self.assertTrue(value["enum"])
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        # Every shape a round is actually run with, not only the advance one:
        # the finalize schema and the schema of a session that already has a
        # decision log are built per round and never appear as a constant.
        for schema in (
            INTAKE_SESSION_OUTPUT_SCHEMA,
            intake_session_output_schema(mode=IntakeRoundMode.FINALIZE),
            intake_session_output_schema(
                mode=IntakeRoundMode.ADVANCE,
                known_decision_ids=("decision_sign", "decision_units"),
            ),
            intake_session_output_schema(
                mode=IntakeRoundMode.FINALIZE,
                known_decision_ids=("decision_sign",),
            ),
            SPEC_AUDIT_OUTPUT_SCHEMA,
            INTAKE_REHYDRATE_OUTPUT_SCHEMA,
        ):
            walk(schema)
        question_schema = INTAKE_SESSION_OUTPUT_SCHEMA["properties"]["questions"][
            "items"
        ]
        self.assertEqual(
            question_schema["properties"]["allow_custom"],
            {"type": "boolean", "enum": [True]},
        )

    def test_finalize_schema_pins_readiness_without_forbidding_questions(self) -> None:
        finalize = intake_session_output_schema(mode=IntakeRoundMode.FINALIZE)
        advance = INTAKE_SESSION_OUTPUT_SCHEMA

        self.assertEqual(
            finalize["properties"]["candidate_ready"],
            {"type": "boolean", "enum": [True]},
        )
        self.assertEqual(advance["properties"]["candidate_ready"], {"type": "boolean"})
        # The one constraint the format deliberately does not carry: a finalize
        # round's questions are repaired into declared defaults instead.
        self.assertEqual(
            finalize["properties"]["questions"]["maxItems"], MAX_ROUND_QUESTIONS
        )
        self.assertEqual(
            [
                item.value
                for item in (
                    DecisionClass.CONVENTION,
                    DecisionClass.APPROXIMATION_LEVEL,
                )
            ],
            advance["properties"]["declared_defaults"]["items"]["properties"][
                "decision_class"
            ]["enum"],
        )
        self.assertIn(
            DecisionClass.PROBLEM.value,
            finalize["properties"]["declared_defaults"]["items"]["properties"][
                "decision_class"
            ]["enum"],
        )

    def test_known_decision_ids_narrow_the_reopen_target(self) -> None:
        without = INTAKE_SESSION_OUTPUT_SCHEMA["properties"]["reopen_requests"][
            "items"
        ]["properties"]["decision_id"]
        self.assertEqual(without, {"type": "string"})
        with_log = intake_session_output_schema(
            known_decision_ids=("decision_sign", "decision_sign", "decision_units")
        )["properties"]["reopen_requests"]["items"]["properties"]["decision_id"]
        self.assertEqual(
            with_log,
            {"type": "string", "enum": ["decision_sign", "decision_units"]},
        )

    async def test_returns_entire_frontier_and_persistent_thread_receipt(self) -> None:
        runner = Runner(payload())
        advisor = AppServerIntakeSessionAdvisor(runner)
        session = IntakeSession(
            session_id="intake_session_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )

        proposal = await advisor.advance(
            session,
            user_submission={"message": "derive the response"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(proposal.thread_id, "thread-intake")
        self.assertTrue(proposal.created_thread)
        self.assertEqual(proposal.questions[0].decision_id, "decision_sign")
        self.assertTrue(proposal.questions[0].allow_custom)
        self.assertIn(
            "every currently independent", INTAKE_SESSION_DEVELOPER_INSTRUCTIONS
        )
        self.assertIsNone(runner.calls[0]["resume_thread_id"])
        self.assertEqual(runner.calls[0]["model"], "gpt-5.4")
        self.assertEqual(runner.calls[0]["effort"], "low")
        self.assertEqual(runner.calls[0]["service_tier"], "standard")

    async def test_advance_passes_session_model_effort_into_run_round(self) -> None:
        runner = Runner(payload())
        advisor = AppServerIntakeSessionAdvisor(runner)
        session = replace(
            IntakeSession(
                session_id="intake_session_1",
                revision=0,
                status=IntakeSessionStatus.ACTIVE,
                problem_specifications=(),
                decisions=(),
                frontier=(),
                thread_generations=(),
            ),
            model="gpt-5-codex",
            effort="high",
            service_tier="fast",
        )

        await advisor.advance(
            session,
            user_submission={"message": "derive the response"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(runner.calls[0]["model"], "gpt-5-codex")
        self.assertEqual(runner.calls[0]["effort"], "high")
        self.assertEqual(runner.calls[0]["service_tier"], "fast")

    async def test_next_round_passes_the_same_thread_id(self) -> None:
        runner = Runner(payload(ready=True))
        advisor = AppServerIntakeSessionAdvisor(runner)
        session = IntakeSession(
            session_id="intake_session_1",
            revision=1,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )

        proposal = await advisor.advance(
            session,
            user_submission={"answers": {"decision_sign": "minus"}},
            visible_event_ids=("event_user_1", "event_user_2"),
            resume_thread_id="thread-intake",
        )

        self.assertTrue(proposal.candidate_ready)
        self.assertEqual(runner.calls[0]["resume_thread_id"], "thread-intake")

    async def test_ready_is_rejected_when_scientific_sections_are_missing(self) -> None:
        broken = payload(ready=True)
        broken["specification_sections"] = [
            {
                "name": "scientific_target",
                "content": "Derive the response.",
                "not_applicable_reason": None,
            }
        ]
        advisor = AppServerIntakeSessionAdvisor(Runner(broken))
        session = IntakeSession(
            session_id="intake_session_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )

        with self.assertRaisesRegex(ValueError, "missing required sections"):
            await advisor.advance(
                session,
                user_submission={"message": "ready"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

    async def test_independent_specification_audit_passes_without_questions(
        self,
    ) -> None:
        runner = AuditRunner(audit_payload())
        auditor = AppServerProblemSpecificationAuditor(runner)
        proposal = payload(ready=True)
        advisor = AppServerIntakeSessionAdvisor(Runner(proposal))
        session = IntakeSession(
            session_id="intake_session_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        candidate = await advisor.advance(
            session,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        audit = await auditor.audit(
            candidate.problem_specification,
            initial_problem="Derive the absorption coefficient.",
            decision_log=(),
            mode=IntakeRoundMode.ADVANCE,
        )

        self.assertTrue(audit.passed)
        self.assertEqual(audit.thread_id, "thread-audit")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(audit.repairs, ())

    async def test_an_audit_repair_is_reported_and_logged_like_a_rounds(self) -> None:
        """The audit path repairs its payload too; those repairs used to vanish.

        They were collected into a log the method then dropped on the floor, so
        an audit-path repair reached neither the round's repair trail nor the
        operator log with a session id attached.
        """

        runner = AuditRunner(
            audit_payload(residual_risks=["The line shape is unstated.", "   "])
        )
        auditor = AppServerProblemSpecificationAuditor(runner)
        candidate = await AppServerIntakeSessionAdvisor(
            Runner(payload(ready=True))
        ).advance(
            ACTIVE_SESSION,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        with self.assertLogs(
            "derivation_app.app_server_intake_session", level="WARNING"
        ) as logs:
            audit = await auditor.audit(
                candidate.problem_specification,
                initial_problem="Derive the absorption coefficient.",
                decision_log=(),
                mode=IntakeRoundMode.ADVANCE,
                session_id="intake_session_1",
            )

        self.assertEqual(audit.residual_risks, ("The line shape is unstated.",))
        self.assertEqual(
            [item.code.value for item in audit.repairs], ["text_entry_dropped"]
        )
        self.assertIn("intake_session_1", "\n".join(logs.output))

    async def test_thread_rehydration_requires_exact_canonical_hash(self) -> None:
        session = IntakeSession(
            session_id="intake_session_1",
            revision=1,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        runner = Runner({"canonical_state_sha256": intake_session_fingerprint(session)})
        advisor = AppServerIntakeSessionAdvisor(runner)

        receipt = await advisor.rehydrate(
            session,
            visible_event_ids=("event_user_1",),
        )

        self.assertEqual(receipt.thread_id, "thread-intake")
        self.assertEqual(
            receipt.canonical_state_sha256, intake_session_fingerprint(session)
        )


ACTIVE_SESSION = IntakeSession(
    session_id="intake_session_1",
    revision=0,
    status=IntakeSessionStatus.ACTIVE,
    problem_specifications=(),
    decisions=(),
    frontier=(),
    thread_generations=(),
)


class IntakeDecisionLadderContractTests(unittest.IsolatedAsyncioTestCase):
    """Round and audit contracts that stop the grill from running forever."""

    def test_prompt_version_and_shared_blocking_definition(self) -> None:
        self.assertEqual(INTAKE_SESSION_PROMPT_VERSION, "intake-grill-v3")
        self.assertIn(
            INTAKE_DECISION_CLASS_RULES, INTAKE_SESSION_DEVELOPER_INSTRUCTIONS
        )
        self.assertIn(INTAKE_DECISION_CLASS_RULES, SPEC_AUDIT_DEVELOPER_INSTRUCTIONS)
        for counterexample in (
            "unit system",
            "line shape",
            "parabolic two-band",
            "state-dependent lifetime",
            "finite-temperature occupation",
        ):
            self.assertIn(counterexample, INTAKE_DECISION_CLASS_RULES)

    def test_round_schema_caps_questions_and_declares_defaults_and_ladder(self) -> None:
        properties = INTAKE_SESSION_OUTPUT_SCHEMA["properties"]
        self.assertEqual(properties["questions"]["maxItems"], MAX_ROUND_QUESTIONS)
        self.assertIn("declared_defaults", properties)
        self.assertIn("refinement_ladder", properties)
        question_properties = properties["questions"]["items"]["properties"]
        self.assertEqual(
            question_properties["decision_class"]["enum"],
            [DecisionClass.PROBLEM.value],
        )
        self.assertIn("why_it_matters", question_properties)

    async def test_advisor_prompt_carries_mode_round_and_budget(self) -> None:
        runner = Runner(payload())
        advisor = AppServerIntakeSessionAdvisor(runner)

        await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive the response"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
            mode=IntakeRoundMode.ADVANCE,
            round_number=2,
            max_frontier_rounds=3,
            audit_rejections=1,
        )

        prompt = runner.calls[0]["prompt"]
        assert isinstance(prompt, str)
        for fragment in (
            '"mode":"advance"',
            '"round_number":2',
            '"max_frontier_rounds":3',
            '"audit_rejections":1',
        ):
            self.assertIn(fragment, prompt.replace(", ", ",").replace(": ", ":"))

    async def test_convention_class_question_is_rejected(self) -> None:
        broken = payload()
        broken["questions"][0]["decision_class"] = "convention"  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(Runner(broken))

        with self.assertRaisesRegex(ValueError, "declared defaults"):
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "derive"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

    async def test_question_without_why_it_matters_is_rejected(self) -> None:
        broken = payload()
        del broken["questions"][0]["why_it_matters"]  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(Runner(broken))

        with self.assertRaisesRegex(ValueError, "why_it_matters"):
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "derive"},
                visible_event_ids=("event_user_1",),
                resume_thread_id=None,
            )

    async def test_advance_round_without_a_ladder_is_downgraded_to_draft(self) -> None:
        """An advance round that overclaims readiness loses the claim, not the round.

        Weakening candidate_ready is always safe: the user is asked once more.
        Finalize is the case where the same payload has to fail, and
        ``test_finalize_round_must_be_ready_and_ask_nothing`` covers it.
        """

        broken = payload(ready=True)
        broken["refinement_ladder"] = []
        advisor = AppServerIntakeSessionAdvisor(Runner(broken))

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertFalse(proposal.candidate_ready)
        self.assertIn(
            "candidate_ready_downgraded",
            [repair.code for repair in proposal.repairs],
        )

    async def test_advance_round_problem_class_default_becomes_a_question(self) -> None:
        """The advance round may not settle a problem-class decision itself.

        It used to lose the whole round for trying. Now the decision goes back to
        the user with the model's own answer as the recommendation.
        """

        broken = payload(ready=True)
        broken["declared_defaults"][0]["decision_class"] = "problem"  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(Runner(broken))

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertFalse(proposal.candidate_ready)
        question = proposal.questions[-1]
        self.assertEqual(question.decision_id, "decision_default_unit-system")
        self.assertEqual(question.options[0].label, "Work in SI units throughout.")
        self.assertIn(
            "problem_default_became_question",
            [repair.code for repair in proposal.repairs],
        )

    async def test_finalize_round_must_be_ready_and_ask_nothing(self) -> None:
        advisor = AppServerIntakeSessionAdvisor(Runner(payload()))
        with self.assertRaisesRegex(ValueError, "must return a candidate-ready"):
            await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "start"},
                visible_event_ids=("event_user_1",),
                resume_thread_id="thread-intake",
                mode=IntakeRoundMode.FINALIZE,
            )

        # A finalize round that still asks no longer loses the round: the
        # finalize contract's own semantics turn the question into a default.
        still_asking = payload(ready=True)
        still_asking["candidate_ready"] = True
        still_asking["questions"] = payload()["questions"]
        advisor = AppServerIntakeSessionAdvisor(Runner(still_asking))
        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "start"},
            visible_event_ids=("event_user_1",),
            resume_thread_id="thread-intake",
            mode=IntakeRoundMode.FINALIZE,
        )

        self.assertEqual(proposal.questions, ())
        self.assertIn(
            "default_question_decision_sign",
            [
                item.default_id
                for item in proposal.problem_specification.declared_defaults
            ],
        )

    async def test_finalize_round_may_declare_a_problem_class_default(self) -> None:
        finalized = payload(ready=True)
        finalized["declared_defaults"][0]["decision_class"] = "problem"  # type: ignore[index]
        advisor = AppServerIntakeSessionAdvisor(Runner(finalized))

        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "start"},
            visible_event_ids=("event_user_1",),
            resume_thread_id="thread-intake",
            mode=IntakeRoundMode.FINALIZE,
        )

        self.assertTrue(proposal.candidate_ready)
        self.assertEqual(
            proposal.problem_specification.declared_defaults[0].decision_class,
            DecisionClass.PROBLEM,
        )

    async def test_ready_round_inherits_defaults_before_ladder_validation(self) -> None:
        draft_payload = payload()
        draft_payload["declared_defaults"] = payload(ready=True)["declared_defaults"]
        draft = await AppServerIntakeSessionAdvisor(Runner(draft_payload)).advance(
            ACTIVE_SESSION,
            user_submission={"message": "derive"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )
        session = IntakeSession(
            session_id="intake_session_1",
            revision=1,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(draft.problem_specification,),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        ready_payload = payload(ready=True)
        ready_payload["declared_defaults"] = []

        proposal = await AppServerIntakeSessionAdvisor(Runner(ready_payload)).advance(
            session,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )

        self.assertEqual(
            [
                item.default_id
                for item in proposal.problem_specification.declared_defaults
            ],
            ["unit-system"],
        )

    async def test_ladder_default_no_round_declared_does_not_lose_the_round(
        self,
    ) -> None:
        """A dangling ladder id costs the id, not the user's answered round.

        ``declared_defaults`` is capped per round, so a long session pushes an
        older default out of the model's list while a rung still names it. The
        round arrives after a minute of model work and after the user answered
        the whole frontier, so refusing it here is the expensive failure.
        """

        dangling = payload(ready=True)
        dangling["refinement_ladder"][0]["default_ids"] = [  # type: ignore[index]
            "unit-system",
            "approx_isotropic_scalar",
        ]
        advisor = AppServerIntakeSessionAdvisor(Runner(dangling))

        with self.assertLogs(
            "derivation_app.app_server_intake_session", level="WARNING"
        ) as logs:
            proposal = await advisor.advance(
                ACTIVE_SESSION,
                user_submission={"message": "ready"},
                visible_event_ids=("event_user_1",),
                resume_thread_id="thread-intake",
            )

        ladder = proposal.problem_specification.refinement_ladder
        self.assertEqual(ladder[0].default_ids, ("unit-system",))
        self.assertEqual(
            [
                item.default_id
                for item in proposal.problem_specification.declared_defaults
            ],
            ["unit-system"],
        )
        self.assertIn("approx_isotropic_scalar", "\n".join(logs.output))
        self.assertIn(ACTIVE_SESSION.session_id, "\n".join(logs.output))

    async def test_audit_blocker_must_be_grounded_and_capped(self) -> None:
        ungrounded = audit_question(grounded_in=None)
        auditor = AppServerProblemSpecificationAuditor(
            AuditRunner(audit_payload(passed=False, blocking_questions=[ungrounded]))
        )
        specification = await self._candidate_specification()

        with self.assertRaisesRegex(ValueError, "grounded_in"):
            await auditor.audit(
                specification,
                initial_problem="Derive the absorption coefficient.",
                decision_log=(),
                mode=IntakeRoundMode.ADVANCE,
            )
        self.assertEqual(
            SPEC_AUDIT_OUTPUT_SCHEMA["properties"]["blocking_questions"]["maxItems"],
            3,
        )

    async def test_audit_carries_declared_defaults_and_residual_risks(self) -> None:
        runner = AuditRunner(
            audit_payload(
                declared_defaults=[
                    {
                        "default_id": "broadening",
                        "decision_class": "approximation_level",
                        "title": "Line shape",
                        "statement": "Start from an ideal delta-function line shape.",
                        "rationale": "Broadening is a later ladder rung.",
                        "alternatives": ["Constant broadening"],
                    }
                ],
                residual_risks=["The tensor response is not covered."],
            )
        )
        auditor = AppServerProblemSpecificationAuditor(runner)
        specification = await self._candidate_specification()

        audit = await auditor.audit(
            specification,
            initial_problem="Derive the absorption coefficient.",
            decision_log=({"decision_id": "decision_sign", "answer": "minus"},),
            mode=IntakeRoundMode.ADVANCE,
        )

        self.assertEqual(audit.declared_defaults[0].default_id, "broadening")
        self.assertEqual(audit.residual_risks, ("The tensor response is not covered.",))
        prompt = runner.calls[0]["prompt"]
        assert isinstance(prompt, str)
        self.assertIn("decision_log", prompt)
        self.assertIn("Derive the absorption coefficient.", prompt)

    async def test_finalize_audit_converts_rejection_to_residual_risk(self) -> None:
        auditor = AppServerProblemSpecificationAuditor(
            AuditRunner(
                audit_payload(passed=False, blocking_questions=[audit_question()])
            )
        )
        specification = await self._candidate_specification()

        audit = await auditor.audit(
            specification,
            initial_problem="Derive the absorption coefficient.",
            decision_log=(),
            mode=IntakeRoundMode.FINALIZE,
        )

        self.assertTrue(audit.passed)
        self.assertEqual(audit.blocking_questions, ())
        self.assertIn(
            "Finalize audit: The specification is executable.", audit.residual_risks
        )
        self.assertIn(
            "Fourier sign: The final expression flips sign.", audit.residual_risks
        )

    async def _candidate_specification(self):  # type: ignore[no-untyped-def]
        advisor = AppServerIntakeSessionAdvisor(Runner(payload(ready=True)))
        proposal = await advisor.advance(
            ACTIVE_SESSION,
            user_submission={"message": "ready"},
            visible_event_ids=("event_user_1",),
            resume_thread_id=None,
        )
        return proposal.problem_specification


if __name__ == "__main__":
    unittest.main()
