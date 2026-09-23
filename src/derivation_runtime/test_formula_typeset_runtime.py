"""Typeset-layer turns over the scripted App Server client and the fake runtime."""

from __future__ import annotations

import unittest

from .app_server_runtime import CodexAppServerRuntime
from .fake import DeterministicFakeRuntime
from .prompts import (
    FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS,
    FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS,
    formula_repair_output_schema,
    formula_repair_user_prompt,
    formula_review_output_schema,
)
from .test_app_server_runtime import (
    ScriptedAppServerClient,
    TurnScript,
    config,
    settings,
    step_payload,
)
from .types import (
    CheckOutput,
    FormulaEquivalenceOutput,
    FormulaEquivalenceRequest,
    FormulaRepairItem,
    FormulaRepairOutput,
    FormulaRepairRequest,
    FormulaRepairStep,
    FormulaReviewItem,
    JudgeOutput,
    ModelRole,
    RuntimeInvariantError,
    RuntimeInvocationError,
    StepContent,
    StepSnapshot,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)

CONTENT = StepContent(**step_payload("one"))


def repair_request(*ids: str) -> FormulaRepairRequest:
    return FormulaRepairRequest(
        run_id="run-app-server-runtime",
        repair_id="typeset_route_br_0001_r01",
        route_id="route_br_0001",
        steps=(
            FormulaRepairStep(
                step_revision_id="step_0001",
                content=CONTENT,
                formulas=tuple(
                    FormulaRepairItem(
                        formula_id=formula_id,
                        field="derivation",
                        latex=r"x^2^3",
                        compiler_error="Double superscript",
                    )
                    for formula_id in ids
                ),
            ),
        ),
    )


def review_request(*ids: str) -> FormulaEquivalenceRequest:
    return FormulaEquivalenceRequest(
        run_id="run-app-server-runtime",
        review_id="typeset_route_br_0001_v01",
        route_id="route_br_0001",
        steps=(StepSnapshot(step_revision_id="step_0001", content=CONTENT),),
        items=tuple(
            FormulaReviewItem(
                formula_id=formula_id,
                step_revision_id="step_0001",
                field="derivation",
                original_latex=r"x^2^3",
                corrected_latex=r"{x^2}^3",
                compiler_error="Double superscript",
            )
            for formula_id in ids
        ),
    )


class FormulaTypesetTurnTests(unittest.IsolatedAsyncioTestCase):
    def runtime(
        self, scripts
    ) -> tuple[CodexAppServerRuntime, ScriptedAppServerClient]:
        client = ScriptedAppServerClient(scripts)
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=client,
            settings=settings(),  # type: ignore[arg-type]
        )
        self.addAsyncCleanup(runtime.close)
        return runtime, client

    async def test_repair_runs_on_a_fresh_tool_free_writer_thread(self) -> None:
        request = repair_request("step_0001:derivation:1")
        runtime, client = self.runtime(
            [
                TurnScript(
                    {
                        "corrections": [
                            {
                                "formula_id": "step_0001:derivation:1",
                                "latex": r"{x^2}^3",
                            }
                        ]
                    }
                )
            ]
        )

        invocation = await runtime.start_formula_repair(request)
        output = await runtime.collect_formula_repair(invocation)

        self.assertIsInstance(output, FormulaRepairOutput)
        self.assertEqual(
            output.corrections, (("step_0001:derivation:1", r"{x^2}^3"),)
        )
        self.assertEqual(output.usage.values["last"]["totalTokens"], 5)
        started = client.thread_start_calls[0]
        self.assertEqual(started["dynamicTools"], [])
        self.assertTrue(started["ephemeral"])
        self.assertEqual(
            started["developerInstructions"], FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS
        )
        # The run's Writer model, effort and service tier, as for a real step.
        self.assertEqual(started["model"], config().writer.model)
        self.assertEqual(
            started["config"]["model_reasoning_effort"], config().writer.effort
        )
        turn = client.turn_start_calls[0]
        self.assertEqual(turn["outputSchema"], formula_repair_output_schema(request))
        self.assertEqual(
            turn["input"][0]["text"], formula_repair_user_prompt(request)
        )
        self.assertEqual(turn["effort"], config().writer.effort)
        # No literature, transcript or computation tool is offered.
        self.assertNotIn("source_read", turn["input"][0]["text"])

    async def test_review_runs_on_a_fresh_checker_thread(self) -> None:
        request = review_request("step_0001:derivation:1")
        runtime, client = self.runtime(
            [
                TurnScript(
                    {
                        "reviews": [
                            {
                                "formula_id": "step_0001:derivation:1",
                                "verdict": "equivalent",
                                "reason": "only the grouping changed",
                            }
                        ]
                    }
                )
            ]
        )

        invocation = await runtime.start_formula_review(request)
        output = await runtime.collect_formula_review(invocation)

        self.assertIsInstance(output, FormulaEquivalenceOutput)
        self.assertEqual(
            output.verdicts,
            (("step_0001:derivation:1", "equivalent", "only the grouping changed"),),
        )
        started = client.thread_start_calls[0]
        self.assertEqual(started["model"], config().checker.model)
        self.assertEqual(started["dynamicTools"], [])
        self.assertEqual(
            started["developerInstructions"], FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS
        )
        self.assertEqual(
            client.turn_start_calls[0]["outputSchema"],
            formula_review_output_schema(request),
        )

    async def test_a_repair_must_answer_exactly_the_requested_ids(self) -> None:
        request = repair_request("step_0001:derivation:1", "step_0001:claim:1")
        runtime, _ = self.runtime(
            [
                TurnScript(
                    {
                        "corrections": [
                            {
                                "formula_id": "step_0001:derivation:1",
                                "latex": r"{x^2}^3",
                            }
                        ]
                    }
                )
            ]
        )

        invocation = await runtime.start_formula_repair(request)
        with self.assertRaises(RuntimeInvocationError) as raised:
            await runtime.collect_formula_repair(invocation)
        self.assertEqual(raised.exception.failure_kind, "invalid_model_output")

    async def test_a_review_verdict_outside_the_schema_is_refused(self) -> None:
        request = review_request("step_0001:derivation:1")
        runtime, _ = self.runtime(
            [
                TurnScript(
                    {
                        "reviews": [
                            {
                                "formula_id": "step_0001:derivation:1",
                                "verdict": "maybe",
                                "reason": "unsure",
                            }
                        ]
                    }
                )
            ]
        )

        invocation = await runtime.start_formula_review(request)
        with self.assertRaises(RuntimeInvocationError):
            await runtime.collect_formula_review(invocation)

    async def test_a_typeset_turn_belongs_to_its_own_run(self) -> None:
        from dataclasses import replace

        runtime, _ = self.runtime([TurnScript(None)])
        with self.assertRaises(RuntimeInvariantError):
            await runtime.start_formula_repair(
                replace(repair_request("step_0001:derivation:1"), run_id="other-run")
            )

    async def test_repair_output_restores_json_escaped_control_characters(self) -> None:
        request = repair_request("step_0001:derivation:1")
        runtime, _ = self.runtime(
            [
                TurnScript(
                    {
                        "corrections": [
                            {
                                "formula_id": "step_0001:derivation:1",
                                "latex": "x + \bfrac{1}{2}",
                            }
                        ]
                    }
                )
            ]
        )

        invocation = await runtime.start_formula_repair(request)
        output = await runtime.collect_formula_repair(invocation)

        self.assertEqual(output.corrections[0][1], r"x + \bfrac{1}{2}")


class FakeRuntimeTypesetTests(unittest.IsolatedAsyncioTestCase):
    def fake(self, **overrides) -> DeterministicFakeRuntime:
        return DeterministicFakeRuntime(
            writer_outputs={},
            check_factory=lambda _request: CheckOutput(
                verdict="ok",
                reason="fixture",
                evidence=(),
                finish_reason="stop",
                usage=Usage({}),
            ),
            judge_factory=lambda _request: JudgeOutput(
                verdict="pass",
                reason="fixture",
                score=1.0,
                finish_reason="stop",
                usage=Usage({}),
            ),
            **overrides,
        )

    async def test_a_scripted_fake_answers_both_typeset_kinds(self) -> None:
        runtime = self.fake(
            formula_repair_factory=lambda request: FormulaRepairOutput(
                corrections=tuple((item, r"{x^2}^3") for item in request.formula_ids),
                finish_reason="stop",
                usage=Usage({"total_tokens": 3}),
                raw_output="{}",
            ),
            formula_review_factory=lambda request: FormulaEquivalenceOutput(
                verdicts=tuple(
                    (item.formula_id, "equivalent", "grouping only")
                    for item in request.items
                ),
                finish_reason="stop",
                usage=Usage({"total_tokens": 3}),
                raw_output="{}",
            ),
        )
        repair = await runtime.start_formula_repair(
            repair_request("step_0001:derivation:1")
        )
        self.assertIs(repair.role, ModelRole.WRITER)
        repaired = await runtime.collect_formula_repair(repair)
        self.assertEqual(repaired.corrections[0][1], r"{x^2}^3")
        review = await runtime.start_formula_review(
            review_request("step_0001:derivation:1")
        )
        self.assertIs(review.role, ModelRole.CHECKER)
        reviewed = await runtime.collect_formula_review(review)
        self.assertEqual(reviewed.verdicts[0][1], "equivalent")
        self.assertEqual(len(runtime.formula_repair_requests), 1)

    async def test_an_unscripted_fake_refuses_typeset_calls(self) -> None:
        runtime = self.fake()
        with self.assertRaises(RuntimeInvocationError) as raised:
            await runtime.start_formula_repair(
                repair_request("step_0001:derivation:1")
            )
        self.assertEqual(raised.exception.failure_kind, "unsupported_request")
        with self.assertRaises(RuntimeInvocationError):
            await runtime.start_formula_review(
                review_request("step_0001:derivation:1")
            )

    async def test_a_writer_output_is_not_collected_as_a_repair(self) -> None:
        runtime = self.fake(
            formula_repair_factory=lambda _request: WriterOutput(
                content=CONTENT,
                control=WriterControl(
                    decision=WriterDecision.COMPLETE, alternatives=()
                ),
                finish_reason="stop",
                usage=Usage({}),
            )
        )
        invocation = await runtime.start_formula_repair(
            repair_request("step_0001:derivation:1")
        )
        with self.assertRaises(RuntimeInvariantError):
            await runtime.collect_formula_repair(invocation)


if __name__ == "__main__":  # pragma: no cover - direct execution helper
    unittest.main()
