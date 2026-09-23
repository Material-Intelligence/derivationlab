"""What a round is allowed to get wrong once it meets the decision log.

The parse path grades what the payload alone can settle. Whether a decision is
open or resolved, and whether this round's wording is the one the user already
answered, is knowable only here — and it used to be a ``ValueError`` thrown
after the model call was paid for and after the user had answered the whole
frontier. These tests pin the repairs that replaced those raises, and the
backstops that are still raises.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from derivation_app.app_server_intake_session import (
    INTAKE_SESSION_PROMPT_VERSION,
    DecisionReopenRequest,
    IntakeInvariant,
    IntakeRoundProposal,
    IntakeRoundRepair,
    SpecificationAudit,
    ThreadReplacementReceipt,
)
from derivation_app.intake_session import (
    ConvergenceState,
    DecisionAnswer,
    DecisionEntry,
    DecisionStatus,
    IntakeQuestion,
    IntakeSession,
    IntakeSessionStatus,
    SQLiteIntakeStore,
    ThreadGeneration,
    ThreadGenerationStatus,
    intake_session_fingerprint,
)
from derivation_app.intake_session_service import (
    PersistentIntakeSessionService,
    _RoundOutcome,
)

from .test_intake_session_service import Auditor, question, specification

SEED_REF = "event_seed"


def round_of(
    *questions: IntakeQuestion,
    ready: bool = False,
    repairs: tuple[IntakeRoundRepair, ...] = (),
    critical_message_ref: str | None = None,
):
    """Build one advisor round bound to whatever the session can already see."""

    def build(session: IntakeSession, call: Mapping[str, Any]) -> IntakeRoundProposal:
        return IntakeRoundProposal(
            public_summary="The round says something.",
            problem_specification=specification(
                len(session.problem_specifications) + 1,
                ready=ready,
                critical_message_ref=(
                    critical_message_ref or call["visible_event_ids"][-1]
                ),
            ),
            questions=questions,
            reopen_requests=(),
            candidate_ready=ready,
            thread_id=call.get("resume_thread_id") or "thread-intake",
            turn_id=f"turn-{len(session.problem_specifications) + 1}",
            created_thread=call.get("resume_thread_id") is None,
            repairs=repairs,
        )

    return build


class ScriptedAdvisor:
    """Returns one scripted round per call, so a collision can be staged."""

    def __init__(self, *rounds) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    async def advance(self, session, **kwargs):
        self.calls.append(dict(kwargs))
        return self._rounds.pop(0)(session, kwargs)

    async def rehydrate(self, session, **kwargs):
        return ThreadReplacementReceipt(
            thread_id="thread-intake-replacement",
            turn_id="turn-rehydrate",
            canonical_state_sha256=intake_session_fingerprint(session),
        )


class DuplicatingAuditor(Auditor):
    """Blocks with two questions that name the same decision."""

    async def audit(self, specification, **kwargs):
        self.calls.append({"specification": specification, **kwargs})
        asked = replace(question("audit_scope"), grounded_in="target")
        return SpecificationAudit(
            passed=False,
            public_summary="A scope ambiguity remains.",
            blocking_questions=(
                asked,
                replace(asked, prompt="Which scope, restated?"),
            ),
            thread_id="thread-audit",
            turn_id="turn-audit",
        )


class RepairingAuditor(Auditor):
    """Passes, having repaired something in its own payload on the way."""

    async def audit(self, specification, **kwargs):
        audit = await super().audit(specification, **kwargs)
        self.session_id = kwargs.get("session_id")
        return replace(
            audit,
            repairs=(
                IntakeRoundRepair(
                    code=IntakeInvariant.TEXT_ENTRY_DROPPED,
                    detail="the audit returned a blank residual risk",
                ),
            ),
        )


def answered(*decision_ids: str, option: str = "minus") -> dict[str, DecisionAnswer]:
    return {
        decision_id: DecisionAnswer(selected_option_ids=(option,))
        for decision_id in decision_ids
    }


def repair_codes(payload: Mapping[str, Any]) -> list[str]:
    return [str(item["code"]) for item in payload.get("repairs", ())]


class ApplyPathRepairTests(unittest.IsolatedAsyncioTestCase):
    """Rounds that collide with the decision log, driven through the service."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.store = SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite")

    async def _cleanup(self) -> None:
        self.temp.cleanup()

    def service(
        self, *rounds, auditor: Auditor | None = None
    ) -> PersistentIntakeSessionService:
        self.advisor = ScriptedAdvisor(*rounds)
        return PersistentIntakeSessionService(
            store=self.store,
            advisor=self.advisor,
            auditor=auditor or Auditor(),
            id_factory=lambda: "intake_session_1",
        )

    def assistant_rounds(self, session_id: str) -> list[Mapping[str, Any]]:
        return [
            event.payload
            for event in self.store.events(session_id)
            if event.kind == "assistant_round"
        ]

    def decisions_for(
        self, session: IntakeSession, decision_id: str
    ) -> tuple[DecisionEntry, ...]:
        return tuple(
            entry for entry in session.decisions if entry.decision_id == decision_id
        )

    async def opened(self, *rounds, auditor: Auditor | None = None) -> IntakeSession:
        """Start a session whose first round asks ``sign`` and ``scope``."""

        self.svc = self.service(
            round_of(question("sign"), question("scope")),
            *rounds,
            auditor=auditor,
        )
        return await self.svc.start("Derive the response.", idempotency_key="start-1")

    async def test_a_clean_round_carries_no_repairs_field(self) -> None:
        started = await self.opened()

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertNotIn("repairs", payload)

    async def test_the_parse_paths_repairs_reach_the_event_chain(self) -> None:
        service = self.service(
            round_of(
                question("sign"),
                repairs=(
                    IntakeRoundRepair(
                        code=IntakeInvariant.PROBLEM_DEFAULT_BECAME_QUESTION,
                        detail="the round settled a problem-class default",
                        subject_id="default_benchmark",
                    ),
                ),
            )
        )
        started = await service.start("Derive it.", idempotency_key="start-1")

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(
            payload["repairs"],
            [
                {
                    "code": "problem_default_became_question",
                    "detail": "the round settled a problem-class default",
                    "subject_id": "default_benchmark",
                }
            ],
        )

    async def test_an_identical_repeat_of_an_answered_decision_is_dropped(self) -> None:
        started = await self.opened(round_of(question("sign")))

        advanced = await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(repair_codes(payload), ["repeated_decision_dropped"])
        self.assertEqual(payload["questions"], [])
        self.assertEqual(advanced.frontier, ())
        # The user's answer is untouched: the repeat carried nothing new, so
        # the decision stays at the revision their answer created.
        history = self.decisions_for(advanced, "sign")
        self.assertEqual(len(history), 2)
        self.assertIs(history[-1].status, DecisionStatus.RESOLVED)
        self.assertEqual(history[-1].revision, 2)
        self.assertEqual(history[-1].answer.selected_option_ids, ("minus",))

    async def test_every_repair_is_logged_with_its_session_and_code(self) -> None:
        started = await self.opened(round_of(question("sign")))

        with self.assertLogs(
            "derivation_app.app_server_intake_session", level="WARNING"
        ) as logs:
            await self.svc.submit_round(
                started.session_id,
                base_revision=started.revision,
                answers=answered("sign", "scope"),
                idempotency_key="round-1",
            )

        output = "\n".join(logs.output)
        self.assertIn("repeated_decision_dropped", output)
        self.assertIn(started.session_id, output)

    async def test_a_changed_question_after_an_answer_becomes_a_reopen(self) -> None:
        reworded = replace(
            question("sign"),
            prompt="Which sign convention does the measured spectrum use?",
        )
        started = await self.opened(round_of(reworded))

        advanced = await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(repair_codes(payload), ["resolved_decision_reopened"])
        _, resolved, reopened = self.decisions_for(advanced, "sign")
        # The old answer is superseded the way a filed reopen supersedes it,
        # and it is still in the log rather than overwritten.
        self.assertIs(resolved.status, DecisionStatus.SUPERSEDED)
        self.assertEqual(resolved.answer.selected_option_ids, ("minus",))
        self.assertIs(reopened.status, DecisionStatus.OPEN)
        self.assertEqual(reopened.revision, 3)
        self.assertEqual(reopened.supersedes_revision, 2)
        self.assertIn("asked again", reopened.reopen_reason)
        self.assertIsNone(reopened.answer)
        # And the user is the one who settles it again.
        self.assertEqual(
            tuple(item.prompt for item in advanced.frontier), (reworded.prompt,)
        )

    async def test_the_user_answers_the_reopened_decision_again(self) -> None:
        reworded = replace(
            question("sign"),
            prompt="Which sign convention does the measured spectrum use?",
        )
        started = await self.opened(round_of(reworded), round_of(ready=True))

        advanced = await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )
        settled = await self.svc.submit_round(
            advanced.session_id,
            base_revision=advanced.revision,
            answers=answered("sign", option="plus"),
            idempotency_key="round-2",
        )

        history = self.decisions_for(settled, "sign")
        self.assertIs(history[-1].status, DecisionStatus.RESOLVED)
        self.assertEqual(history[-1].revision, 4)
        self.assertEqual(history[-1].answer.selected_option_ids, ("plus",))
        # The superseded answer stays retrievable.
        self.assertEqual(history[1].answer.selected_option_ids, ("minus",))
        self.assertIs(settled.status, IntakeSessionStatus.CANDIDATE_READY)

    async def test_one_decision_asked_twice_in_a_round_keeps_the_first(self) -> None:
        # The parse path deduped the advisor's questions; the auditor's are
        # merged in after it, so the round can still name one decision twice.
        started = await self.opened(round_of(ready=True), auditor=DuplicatingAuditor())

        advanced = await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(repair_codes(payload), ["duplicate_question_dropped"])
        self.assertEqual(len(self.decisions_for(advanced, "audit_scope")), 1)
        self.assertEqual(
            tuple(item.decision_id for item in advanced.frontier), ("audit_scope",)
        )

    async def test_an_audit_repair_lands_on_the_same_rounds_repair_trail(self) -> None:
        auditor = RepairingAuditor()
        started = await self.opened(round_of(ready=True), auditor=auditor)

        await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(repair_codes(payload), ["text_entry_dropped"])
        # And the audit knows which session it is repairing, so its log lines
        # are as traceable as the round path's.
        self.assertEqual(auditor.session_id, started.session_id)

    async def test_an_invented_message_reference_no_longer_kills_the_round(
        self,
    ) -> None:
        """The last late round-killer: a citation the user could never open.

        It used to raise after ``advance`` had returned, so past both the repair
        pass and the corrective turn, and it destroyed the answered round.
        """

        started = await self.opened(round_of(critical_message_ref="event_invented"))

        advanced = await self.svc.submit_round(
            started.session_id,
            base_revision=started.revision,
            answers=answered("sign", "scope"),
            idempotency_key="round-1",
        )

        payload = self.assistant_rounds(started.session_id)[-1]
        self.assertEqual(repair_codes(payload), ["unknown_message_ref_dropped"])
        self.assertIn("event_invented", payload["repairs"][0]["detail"])
        specification = advanced.problem_specifications[-1]
        self.assertEqual(specification.critical_message_refs, ())
        # The answers the round was carrying are in the log, which is the whole
        # point of not refusing it.
        self.assertEqual(
            self.decisions_for(advanced, "scope")[-1].answer.selected_option_ids,
            ("minus",),
        )


def open_session() -> IntakeSession:
    """A session whose ``sign`` decision is still waiting on the user.

    No public command can currently deliver a round while one of its decisions
    is still open — every blocking frontier question is answered before the next
    round is applied. The repairs below are the ones that keep that true even
    if a future command stops enforcing it, so they are exercised at the seam
    rather than through ``submit_round``.
    """

    return IntakeSession(
        session_id="intake_open",
        revision=1,
        status=IntakeSessionStatus.ACTIVE,
        problem_specifications=(
            specification(1, ready=False, critical_message_ref=SEED_REF),
        ),
        decisions=(
            DecisionEntry(
                decision_id="sign",
                semantic_key="sign",
                status=DecisionStatus.OPEN,
                question=question("sign"),
            ),
        ),
        frontier=(question("sign"),),
        thread_generations=(
            ThreadGeneration(
                generation=1,
                status=ThreadGenerationStatus.ACTIVE,
                app_server_thread_id="thread-intake",
                prompt_version=INTAKE_SESSION_PROMPT_VERSION,
            ),
        ),
    )


def resolved_session() -> IntakeSession:
    return replace(
        open_session(),
        session_id="intake_resolved",
        decisions=(
            DecisionEntry(
                decision_id="sign",
                semantic_key="sign",
                status=DecisionStatus.RESOLVED,
                question=question("sign"),
                answer=DecisionAnswer(selected_option_ids=("minus",)),
            ),
        ),
        frontier=(),
    )


def proposal_for(
    *questions: IntakeQuestion,
    reopens: tuple[DecisionReopenRequest, ...] = (),
) -> IntakeRoundProposal:
    return IntakeRoundProposal(
        public_summary="The round says something.",
        problem_specification=specification(
            2, ready=False, critical_message_ref=SEED_REF
        ),
        questions=questions,
        reopen_requests=reopens,
        candidate_ready=False,
        thread_id="thread-intake",
        turn_id="turn-2",
        created_thread=False,
    )


class OpenDecisionRepairTests(unittest.TestCase):
    """Collisions with a decision the user has not answered yet."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = PersistentIntakeSessionService(
            store=SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite"),
            advisor=ScriptedAdvisor(),
            auditor=Auditor(),
        )

    def apply(self, session: IntakeSession, proposal: IntakeRoundProposal):
        repaired = self.service._repair_proposal(session, proposal)
        advanced, events = self.service._apply_proposal(
            session,
            repaired,
            status=IntakeSessionStatus.ACTIVE,
            pending=(),
            convergence=ConvergenceState(rounds=1),
        )
        payload = next(
            event.payload for event in events if event.kind == "assistant_round"
        )
        return advanced, payload

    def test_an_open_questions_wording_is_updated_in_place(self) -> None:
        reworded = replace(question("sign"), prompt="Which sign, precisely?")

        advanced, payload = self.apply(open_session(), proposal_for(reworded))

        self.assertEqual(repair_codes(payload), ["open_question_reworded"])
        # One revision, not two: nobody answered the old wording, so there is
        # no answer to supersede and no decision taken from the user.
        (entry,) = advanced.decisions
        self.assertIs(entry.status, DecisionStatus.OPEN)
        self.assertEqual(entry.revision, 1)
        self.assertEqual(entry.question.prompt, "Which sign, precisely?")
        self.assertEqual(advanced.frontier[0].prompt, "Which sign, precisely?")

    def test_a_reopen_of_an_already_open_decision_is_dropped(self) -> None:
        reworded = replace(question("sign"), prompt="Which sign, precisely?")
        proposal = proposal_for(
            reworded,
            reopens=(
                DecisionReopenRequest(
                    decision_id="sign",
                    reason="The user changed the requested scope.",
                    source_message_refs=(SEED_REF,),
                ),
            ),
        )

        advanced, payload = self.apply(open_session(), proposal)

        self.assertEqual(
            repair_codes(payload),
            ["redundant_reopen_dropped", "open_question_reworded"],
        )
        (entry,) = advanced.decisions
        self.assertIs(entry.status, DecisionStatus.OPEN)
        self.assertEqual(entry.revision, 1)
        self.assertIsNone(entry.reopen_reason)

    def test_a_round_may_not_rekey_a_decision_it_did_not_open(self) -> None:
        rekeyed = replace(question("sign"), semantic_key="sign_convention")

        advanced, payload = self.apply(open_session(), proposal_for(rekeyed))

        self.assertEqual(repair_codes(payload), ["decision_key_preserved"])
        (entry,) = advanced.decisions
        self.assertEqual(entry.semantic_key, "sign")
        self.assertEqual(entry.question.semantic_key, "sign")

    def test_a_reopen_without_source_messages_borrows_the_rounds(self) -> None:
        reworded = replace(question("sign"), prompt="Which sign, precisely?")
        proposal = proposal_for(
            reworded,
            reopens=(
                DecisionReopenRequest(
                    decision_id="sign",
                    reason="The measured spectrum changed the convention.",
                    source_message_refs=(),
                ),
            ),
        )

        advanced, payload = self.apply(resolved_session(), proposal)

        self.assertEqual(repair_codes(payload), ["reopen_provenance_backfilled"])
        _, reopened = advanced.decisions
        self.assertIs(reopened.status, DecisionStatus.OPEN)
        self.assertEqual(reopened.source_message_refs, (SEED_REF,))


class MessageReferenceRepairTests(unittest.TestCase):
    """Citations to events the host never showed the model.

    Only the Conversation Archive can grade these, so they are repaired between
    the round and the decision log, before ``_repair_proposal`` backfills a
    reopen that the pruning left without any provenance at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = PersistentIntakeSessionService(
            store=SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite"),
            advisor=ScriptedAdvisor(),
            auditor=Auditor(),
        )

    def repaired(
        self, proposal: IntakeRoundProposal, visible: tuple[str, ...]
    ) -> IntakeRoundProposal:
        return self.service._repair_message_refs(
            _RoundOutcome(proposal, (), 0, None),
            visible,
            session_id="intake_resolved",
        ).proposal

    def test_a_round_that_cites_only_real_events_is_left_alone(self) -> None:
        proposal = proposal_for(question("sign"))

        self.assertIs(self.repaired(proposal, (SEED_REF,)), proposal)

    def test_an_invented_reference_is_dropped_beside_the_real_ones(self) -> None:
        proposal = replace(
            proposal_for(question("sign")),
            reopen_requests=(
                DecisionReopenRequest(
                    decision_id="sign",
                    reason="The measured spectrum changed the convention.",
                    source_message_refs=("event_invented", SEED_REF),
                ),
            ),
        )

        repaired = self.repaired(proposal, (SEED_REF,))

        self.assertEqual(repaired.reopen_requests[0].source_message_refs, (SEED_REF,))
        self.assertEqual(
            [item.code.value for item in repaired.repairs],
            ["unknown_message_ref_dropped"],
        )
        self.assertIn("sign", repaired.repairs[0].subject_id or "")

    def test_a_reopen_stripped_of_every_reference_still_reaches_the_user(self) -> None:
        proposal = replace(
            proposal_for(replace(question("sign"), prompt="Which sign, precisely?")),
            reopen_requests=(
                DecisionReopenRequest(
                    decision_id="sign",
                    reason="The measured spectrum changed the convention.",
                    source_message_refs=("event_invented",),
                ),
            ),
        )

        session = resolved_session()
        repaired = self.service._repair_proposal(
            session, self.repaired(proposal, (SEED_REF,))
        )
        advanced, _ = self.service._apply_proposal(
            session,
            repaired,
            status=IntakeSessionStatus.ACTIVE,
            pending=(),
            convergence=ConvergenceState(rounds=1),
        )

        self.assertEqual(
            [item.code.value for item in repaired.repairs],
            ["unknown_message_ref_dropped", "reopen_provenance_backfilled"],
        )
        _, reopened = advanced.decisions
        self.assertIs(reopened.status, DecisionStatus.OPEN)
        self.assertEqual(reopened.source_message_refs, (SEED_REF,))


class BackstopsStayRaisesTests(unittest.TestCase):
    """What the apply path still refuses when handed an unrepaired round.

    The parse path drops both of these before they get here — see
    ``test_intake_contract_hardening`` — so these are the guards that keep a
    future caller from skipping ``_repair_proposal``.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = PersistentIntakeSessionService(
            store=SQLiteIntakeStore(Path(self.temp.name) / "control.sqlite"),
            advisor=ScriptedAdvisor(),
            auditor=Auditor(),
        )

    def apply(self, session: IntakeSession, proposal: IntakeRoundProposal):
        return self.service._apply_proposal(
            session,
            proposal,
            status=IntakeSessionStatus.ACTIVE,
            pending=(),
            convergence=ConvergenceState(rounds=1),
        )

    def test_a_reopen_without_a_replacement_question_is_refused(self) -> None:
        proposal = proposal_for(
            reopens=(
                DecisionReopenRequest(
                    decision_id="sign",
                    reason="The user changed the requested scope.",
                    source_message_refs=(SEED_REF,),
                ),
            ),
        )

        with self.assertRaisesRegex(
            ValueError, "every reopen request requires a replacement question"
        ):
            self.apply(resolved_session(), proposal)

    def test_a_reopen_of_a_decision_the_session_never_had_is_refused(self) -> None:
        proposal = proposal_for(
            question("nowhere"),
            reopens=(
                DecisionReopenRequest(
                    decision_id="nowhere",
                    reason="The user changed the requested scope.",
                    source_message_refs=(SEED_REF,),
                ),
            ),
        )

        with self.assertRaisesRegex(
            ValueError, "only a resolved decision can be reopened"
        ):
            self.apply(resolved_session(), proposal)

    def test_a_repeat_that_skipped_the_repair_pass_is_refused(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "model repeated or changed an existing decision without reopen"
        ):
            self.apply(resolved_session(), proposal_for(question("sign")))


if __name__ == "__main__":
    unittest.main()
