"""Synthetic checker contract and durable completion regressions; no providers."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from .control import ControlStore
from .evidence import evidence_catalog, validate_check_output
from .orchestrator import DerivationOrchestrator
from .prompts import checker_output_schema, checker_user_prompt
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import (
    BranchAlternative,
    CheckEvidence,
    CheckOutput,
    CheckRequest,
    ModelRole,
    RunPhase,
    RuntimeInvariantError,
    RuntimeInvocationError,
    Usage,
    WriterDecision,
)


@pytest.mark.parametrize("stage", ["branch", "candidate", "finished_judge"])
def test_unmocked_prefix_freezes_other_completed_branch_after_checker_failure(
    harness, stage
):
    orchestrator, record, control, runtime = harness()
    declare_candidate = stage != "branch"
    alternative = BranchAlternative("Independent sibling")
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("root", WriterDecision.FORK, (alternative,)),
        ("br_0001", 2): _writer_output("ready", WriterDecision.COMPLETE),
        ("br_0002", 2): _writer_output("bad", WriterDecision.COMPLETE),
    }
    runtime.check_factory = lambda request: (
        output(source_id="target.claim")
        if request.target.content.claim == "Claim bad"
        else CheckOutput("ok", "Valid fixture", (), "stop", Usage({}))
    )

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")
        await orchestrator._run_check("br_0001", "step_0001")
        state = record.snapshot()
        await orchestrator._ensure_model_forks(
            state["branches"][0], "step_0001", state["model_calls"][0], (alternative,)
        )
        await orchestrator._run_writer("br_0001")
        await orchestrator._run_check("br_0001", "step_0002")
        record.change_branch_status(
            branch_id="br_0001",
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        if declare_candidate:
            record.declare_candidate(
                candidate_id="cand_0001", branch_id="br_0001", reason="Ready fixture"
            )
        if stage == "finished_judge":
            candidate = record.snapshot()["candidates"][0]
            record.request_judgement(
                judgement_id="judge_0001",
                candidate_id="cand_0001",
                candidate_sha256=candidate["transcript_sha256"],
                reason="Ready fixture",
            )
            with (
                patch.object(
                    record, "complete_judgement", side_effect=RuntimeError("crash")
                ),
                pytest.raises(RuntimeError, match="crash"),
            ):
                await orchestrator._run_existing_judgement("judge_0001")
        await orchestrator._run_writer("br_0002")
        await orchestrator._run_check("br_0002", "step_0003")
        state = record.snapshot()
        assert state["checks"][-1]["verdict"] == "instrument_failure"
        if declare_candidate:
            assert state["candidates"][0]["status"] == "eligible"
        terminal_id = state["model_calls"][-1]["model_call_id"]
        with control.transaction() as connection:
            connection.execute(
                "UPDATE runtime_calls SET state='running', record_terminal_seq=NULL WHERE model_call_id=?",
                (terminal_id,),
            )
        head, invocations = record.head, len(runtime._invocations)
        assert orchestrator._repair_record_prefix() is False
        assert record.head == head
        assert control.call(orchestrator.config.run_id, terminal_id).state == "failed"
        for _ in range(2):
            with pytest.raises(
                RuntimeInvariantError, match="blocks scientific advancement"
            ):
                await orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        assert record.head == head
        assert len(runtime._invocations) == invocations
        assert len(record.snapshot()["candidates"]) == int(declare_candidate)
        assert len(record.snapshot()["judgements"]) == int(stage == "finished_judge")
        if stage == "finished_judge":
            assert record.snapshot()["judgements"][0]["state"] == "requested"

    asyncio.run(run())


def output(
    kind="ancestor_quote",
    source_id="step_0001",
    quote="Claim first",
    verdict="hard_defect",
):
    return CheckOutput(
        verdict,
        "Quoted contradiction.",
        (CheckEvidence(kind, source_id, quote),),
        "stop",
        Usage({}),
    )


@pytest.fixture
def harness(tmp_path):
    stores = []

    def make(*, retries=0, budget=8, check=None, record_version="1.0"):
        config = _config(max_active_branches=2, max_model_calls=budget, retries=retries)
        config = replace(config, record_version=record_version)
        runtime = _runtime()
        runtime.writer_outputs = {
            ("br_0001", 1): _writer_output("first", WriterDecision.COMPLETE)
        }
        runtime.check_factory = check or (lambda _: output())
        record = RecordV1Writer(
            EventLogWriter(tmp_path / "events.jsonl", config.run_id), config
        )
        control = ControlStore(tmp_path / "control.sqlite")
        stores.append(control)
        orchestrator = DerivationOrchestrator(
            config=config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=control,
        )
        return orchestrator, record, control, runtime

    yield make
    for store in stores:
        store.close()


def test_catalog_and_provider_prompt(harness):
    orchestrator, record, _, _ = harness()

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")

    asyncio.run(run())
    state = record.snapshot()
    sources = evidence_catalog(state, "step_0001", TASK_TEXT)
    for value in (
        output(),
        output("scope_quote", quote="Toy scope"),
        output("hypothesis_quote", "br_0001", "Root hypothesis"),
        output("task_constraint_quote", orchestrator.config.task.id, TASK_TEXT),
    ):
        validate_check_output(value, sources)
    step = orchestrator._step_snapshot(state["step_revisions"][0])
    request = CheckRequest(
        orchestrator.config.run_id,
        "check_0001",
        TASK_TEXT,
        step,
        (step,),
        sources,
        completion_requirements=("State the domain of validity.",),
    )
    prompt = json.loads(checker_user_prompt(request))
    assert prompt["target"]["step_revision_id"] == "step_0001"
    assert prompt["transcript"][0]["step_revision_id"] == "step_0001"
    assert prompt["evidence_sources"] == [item.to_record() for item in sources]
    assert prompt["completion_requirements"] == ["State the domain of validity."]
    assert prompt["candidate_completion_intent"] is False
    assert "copy one short contiguous exact substring" in prompt["evidence_rule"]
    assert "never retype, normalize, join lines" in prompt["evidence_rule"]
    enum = checker_output_schema(request)["properties"]["evidence"]["items"][
        "properties"
    ]["source_id"]["enum"]
    assert set(enum) == {"step_0001", "br_0001", orchestrator.config.task.id}


def test_orchestrator_passes_writer_completion_gates_to_checker(harness):
    captured = []

    def check(request):
        captured.append(request)
        return CheckOutput("ok", "Complete.", (), "stop", Usage({}))

    orchestrator, record, _, runtime = harness(check=check)
    runtime.writer_preparation = lambda *, include_full: {
        "completion_requirements": ["Keep every term of the expansion."]
    }

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")
        await orchestrator._run_check("br_0001", "step_0001")

    asyncio.run(run())
    assert record.snapshot()["checks"][0]["verdict"] == "ok"
    assert captured[0].completion_requirements == (
        "Keep every term of the expansion.",
    )
    assert captured[0].completion_intent is True


def test_orchestrator_marks_continue_writer_tip_as_intermediate(harness):
    captured = []

    def check(request):
        captured.append(request)
        return CheckOutput("ok", "Complete.", (), "stop", Usage({}))

    orchestrator, _, _, runtime = harness(check=check)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("intermediate", WriterDecision.CONTINUE)
    }

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")
        await orchestrator._run_check("br_0001", "step_0001")

    asyncio.run(run())
    assert captured[0].completion_intent is False


@pytest.mark.parametrize(
    "bad",
    [
        output("hypothesis_quote", "target.claim"),
        output("hypothesis_quote", "step_0001"),
        output(source_id="step_other_branch"),
        output(quote="invented quotation"),
        output("task_constraint_quote", "task_runtime_core", "invented task quote"),
    ],
)
def test_invalid_evidence_is_instrument_failure_and_keeps_response(harness, bad):
    orchestrator, record, control, runtime = harness(check=lambda _: bad)
    with pytest.raises(RuntimeInvariantError, match="blocks scientific advancement"):
        asyncio.run(orchestrator.submit("Root hypothesis"))
    state = record.verify_complete()
    assert state["checks"][0]["verdict"] == "instrument_failure"
    call = state["model_calls"][-1]
    assert call["state"] == "failed"
    assert call["failure"]["kind"] == "invalid_model_output"
    assert bad.evidence[0].quote in json.dumps(call)
    assert not state["candidates"] and not state["judgements"]
    assert len(runtime._invocations) == 2
    assert not control.in_flight_calls(orchestrator.config.run_id)
    assert control.run(orchestrator.config.run_id).phase == "error"


@pytest.mark.parametrize("budget,expected_calls,failed", [(2, 2, True), (3, 3, False)])
def test_invalid_retry_obeys_call_budget(harness, budget, expected_calls, failed):
    attempts = []

    def check(request):
        attempts.append(request)
        return output(source_id="target.claim") if len(attempts) == 1 else output()

    orchestrator, record, control, runtime = harness(
        retries=1, budget=budget, check=check
    )
    if failed:
        with pytest.raises(RuntimeInvariantError):
            asyncio.run(orchestrator.submit("Root hypothesis"))
    else:
        asyncio.run(orchestrator.submit("Root hypothesis"))
    state = record.verify_complete()
    assert len(runtime._invocations) == expected_calls
    assert state["checks"][0]["verdict"] == (
        "instrument_failure" if failed else "hard_defect"
    )
    assert not control.in_flight_calls(orchestrator.config.run_id)


def test_record_v1_1_retries_checker_serialization_before_pausing(harness):
    attempts = []

    def check(request):
        attempts.append(request)
        if len(attempts) == 1:
            return output(source_id="target.claim")
        return CheckOutput("ok", "Exact retry accepted.", (), "stop", Usage({}))

    orchestrator, record, control, runtime = harness(
        retries=1,
        budget=4,
        check=check,
        record_version="1.1",
    )
    asyncio.run(orchestrator.submit("Root hypothesis"))
    state = record.verify_complete()
    checker_calls = [call for call in state["model_calls"] if call["role"] == "checker"]
    assert [call["state"] for call in checker_calls] == ["failed", "finished"]
    assert state["checks"][0]["verdict"] == "ok"
    assert state["branches"][0]["status"] == "completed"
    assert len(runtime._invocations) == 3
    assert not control.in_flight_calls(orchestrator.config.run_id)


def test_crash_after_provider_finish_replays_once_without_provider(harness):
    orchestrator, record, control, runtime = harness()
    with (
        patch.object(record, "complete_check", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError, match="crash"),
    ):
        asyncio.run(orchestrator.submit("Root hypothesis"))
    assert record.snapshot()["model_calls"][-1]["state"] == "finished"
    assert not control.in_flight_calls(orchestrator.config.run_id)
    calls = len(runtime._invocations)
    asyncio.run(orchestrator.reconcile_in_flight())
    head = record.head
    asyncio.run(orchestrator.reconcile_in_flight())
    assert record.head == head
    assert len(runtime._invocations) == calls
    assert record.verify_complete()["checks"][0]["verdict"] == "hard_defect"


def test_sqlite_completion_crash_reconciles_without_losing_other_live_handle(harness):
    orchestrator, record, control, runtime = harness()

    async def run():
        await orchestrator.initialize("Root hypothesis")
        with (
            patch.object(
                control, "finish_call", side_effect=RuntimeError("sqlite crash")
            ),
            pytest.raises(RuntimeError, match="sqlite crash"),
        ):
            await orchestrator._run_writer("br_0001")
        control.reconcile_from_snapshot(record.snapshot())
        orchestrator._repair_record_prefix()
        step = record.snapshot()["step_revisions"][0]
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Fixture",
        )
        start = record.start_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            target={"check_id": "check_0001"},
            prompt="fixture",
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0002",
            branch_id="br_0001",
            role="checker",
            target_kind="check",
            target_id="check_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        snap = orchestrator._step_snapshot(step)
        invocation = await runtime.start_checker(
            CheckRequest(
                orchestrator.config.run_id, "check_0001", TASK_TEXT, snap, (snap,)
            )
        )
        control.attach_invocation(orchestrator.config.run_id, "call_0002", invocation)
        control.update_phase(
            orchestrator.config.run_id,
            RunPhase.ERROR,
            stop_reason="historical",
            last_error="preserve me",
        )
        for _ in range(2):
            control.reconcile_from_snapshot(record.snapshot())
        bookmark = control.call(orchestrator.config.run_id, "call_0002")
        assert bookmark.state == "running"
        assert bookmark.provider_operation_id == invocation.operation_id
        assert control.run(orchestrator.config.run_id).last_error == "preserve me"
        assert (
            control.run(orchestrator.config.run_id).record_event_seq == record.head[0]
        )

    asyncio.run(run())


def test_reconciled_provider_output_uses_same_semantic_gate(harness):
    orchestrator, record, control, runtime = harness(
        check=lambda _: output(source_id="target.claim")
    )

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")
        with (
            patch.object(
                runtime, "collect_checker", side_effect=RuntimeError("disconnect")
            ),
            pytest.raises(RuntimeError, match="disconnect"),
        ):
            await orchestrator._run_check("br_0001", "step_0001")
        invocation = next(
            item
            for item in runtime._invocations.values()
            if item.invocation.role is ModelRole.CHECKER
        )
        invocation.state = "completed"
        with pytest.raises(
            RuntimeInvariantError, match="blocks scientific advancement"
        ):
            await orchestrator.reconcile_in_flight()

    asyncio.run(run())
    assert record.verify_complete()["checks"][0]["verdict"] == "instrument_failure"
    assert len(runtime._invocations) == 2
    assert not control.in_flight_calls(orchestrator.config.run_id)


def test_catalog_excludes_real_sibling_ids_and_future_steps(harness):
    orchestrator, record, _, _ = harness()

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")

    asyncio.run(run())
    state = record.snapshot()
    first = state["step_revisions"][0]
    # A complete snapshot may include later route steps and unrelated branches.
    state["step_revisions"].extend(
        [
            {**first, "step_revision_id": "step_future"},
            {**first, "step_revision_id": "step_sibling", "branch_id": "br_sibling"},
        ]
    )
    state["branches"][0]["step_revision_ids"].append("step_future")
    state["branches"].append(
        {
            **state["branches"][0],
            "branch_id": "br_sibling",
            "step_revision_ids": ["step_sibling"],
        }
    )
    sources = evidence_catalog(state, "step_0001", TASK_TEXT)
    for bad in (
        output(source_id="step_future"),
        output(source_id="step_sibling"),
        output("hypothesis_quote", "br_sibling", "Root hypothesis"),
    ):
        with pytest.raises(RuntimeInvocationError, match="outside the frozen catalog"):
            validate_check_output(bad, sources)


def test_checker_failure_blocks_pending_judge_before_scheduling(harness):
    raw = ' { "verdict": "hard_defect", "reason": "raw response", "evidence": [] } '
    bad = replace(output(source_id="target.claim"), raw_output=raw)
    orchestrator, record, _, _ = harness(check=lambda _: bad)

    async def run():
        await orchestrator.initialize("Root hypothesis")
        await orchestrator._run_writer("br_0001")
        await orchestrator._run_check("br_0001", "step_0001")
        snapshot = record.snapshot()
        snapshot["judgements"].append(
            {
                "judgement_id": "pending",
                "state": "requested",
                "requested_event_id": "evt_9999",
            }
        )
        judge = AsyncMock(side_effect=AssertionError("must not invoke judge"))
        advance = AsyncMock(side_effect=AssertionError("must not advance any branch"))
        with (
            patch.object(orchestrator, "_repair_record_prefix"),
            patch.object(record, "snapshot", return_value=snapshot),
            patch.object(orchestrator, "_run_existing_judgement", judge),
            patch.object(orchestrator, "_advance_branch", advance),
            pytest.raises(RuntimeInvariantError, match="blocks scientific advancement"),
        ):
            await orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        judge.assert_not_called()
        advance.assert_not_called()

    asyncio.run(run())
    failure = next(
        event for event in record.events if event["type"] == "model_call_failed"
    )
    assert failure["payload"]["partial_output_text"] == raw
