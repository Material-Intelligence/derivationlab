"""Provider-free Record 1.1 multi-turn and immutable repair acceptance."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from derivation_agent_record import ContractError, replay_events

from .control import ControlStore
from .fake import FakeFailure
from .orchestrator import DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import (
    CheckEvidence,
    CheckOutput,
    EvidenceSource,
    RunPhase,
    RuntimeInvariantError,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)


def harness(tmp_path, *, checker=True):
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=0),
        record_version="1.1",
        max_model_calls=None,
        checker_enabled=checker,
    )
    runtime = _runtime()
    record = RecordV1Writer(
        EventLogWriter(tmp_path / "events.jsonl", config.run_id), config
    )
    control = ControlStore(tmp_path / "control.sqlite")
    orchestrator = DerivationOrchestrator(
        config=config,
        task_text=TASK_TEXT,
        runtime=runtime,
        record=record,
        control=control,
    )
    return orchestrator, record, control, runtime


def repair(label, target):
    return replace(
        _writer_output(label, WriterDecision.CONTINUE),
        control=WriterControl(
            WriterDecision.REVISE, (), target, "Correct the earlier algebra."
        ),
    )


def wide(label, decision=WriterDecision.CONTINUE, *, revises=None):
    """A step large enough that three of them outgrow the exact-context window.

    ``bounded_transcript`` keeps at most 48000 characters of verbatim steps, so
    real derivations reach the point where the Writer's next turn no longer
    carries its whole route - which is what makes the orchestrator rotate the
    Branch onto a fresh provider thread.
    """

    return WriterOutput(
        StepContent(
            claim=f"Claim {label}",
            why=f"Reason for {label}",
            source=f"Toy source {label}",
            derivation=f"Deterministic derivation {label}. " + "x = y + 1. " * 1600,
            scope=f"Toy scope {label}",
        ),
        WriterControl(WriterDecision.REVISE, (), revises, "Correct the algebra.")
        if revises is not None
        else WriterControl(decision, ()),
        "stop",
        Usage({}),
    )


def test_rotating_a_revised_branch_keeps_its_writer_session_binding(tmp_path):
    """A branch revised twice outgrows the window and keeps its binding.

    Repeated revisions can leave the active branch carrying several sealed
    steps of wide exact transcript.  Once the next Writer turn no longer fits
    the bounded window, the orchestrator rotates the Branch onto a freshly
    rehydrated provider thread.  A runtime that kept the Branch bound to the
    thread its previous step ran on would refuse the turn with ``writer session
    differs from the Branch binding`` and end the run.  Nothing about forking
    is involved; a revision of a revision is enough, once the route is wide
    enough to be trimmed.
    """

    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): wide("root"),
        ("br_0001", 2): wide("first-repair", revises="step_0001"),
        ("br_0002", 2): wide("second"),
        ("br_0002", 3): wide("second-repair", revises="step_0003"),
        ("br_0003", 3): wide("third"),
        ("br_0003", 4): wide("closing", WriterDecision.COMPLETE),
    }
    try:
        query = asyncio.run(o.submit("Rotate a revised route off its first thread"))
        state = record.verify_complete()
        assert query.status().phase == RunPhase.REVIEW_READY
        # A revision of a revision: br_0003 replaces a step that itself replaced
        # one, and it is the branch that outgrows the window.
        assert state["branches"][2]["step_revision_ids"] == [
            "step_0002",
            "step_0004",
            "step_0005",
            "step_0006",
        ]
        assert state["step_revisions"][3]["replaces_step_revision_id"] == "step_0003"
        # The closing turn ran on a thread the branch was not bound to when it
        # sealed step_0005, and the move was announced rather than discovered.
        history = runtime.writer_sessions_by_branch["br_0003"]
        assert len(set(history)) == 2
        assert ("br_0003", history[-1]) in runtime.rebinds
        assert runtime.rehydrations[-1][1] == (
            "step_0004",
            "step_0005",
        ), "the rotated thread carries the trimmed exact prefix, not the whole route"
        bookmark = control.branch(o.config.run_id, "br_0003")
        assert bookmark.provider_session_id == history[-1]
        assert all(call["state"] == "finished" for call in state["model_calls"])
        assert_schemas(record)
    finally:
        control.close()


def test_unannounced_branch_session_switch_still_fails(tmp_path):
    """The same route with the announcement suppressed, which is the failure.

    Rotation is accepted because it is declared, not because a Branch may now
    change session quietly.  Drop the declaration and the run dies with the
    binding error the rotation announcement exists to prevent.
    """

    o, _record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): wide("root"),
        ("br_0001", 2): wide("first-repair", revises="step_0001"),
        ("br_0002", 2): wide("second"),
        ("br_0002", 3): wide("second-repair", revises="step_0003"),
        ("br_0003", 3): wide("third"),
        ("br_0003", 4): wide("closing", WriterDecision.COMPLETE),
    }
    runtime.rebind_branch_writer_session = (  # type: ignore[method-assign]
        lambda branch_id, session: None
    )
    try:
        with pytest.raises(
            RuntimeInvariantError,
            match="writer session differs from the Branch binding",
        ):
            asyncio.run(o.submit("Rotate a revised route without announcing it"))
    finally:
        control.close()


def assert_schemas(record):
    from jsonschema import Draft202012Validator

    schema_dir = Path(__file__).parents[1] / "derivation_agent_record/schemas"
    event_validator = Draft202012Validator(
        json.loads((schema_dir / "event-v1.1.schema.json").read_text())
    )
    canonical_validator = Draft202012Validator(
        json.loads((schema_dir / "canonical-v1.1.schema.json").read_text())
    )
    for event in record.events:
        event_validator.validate(event)
    canonical_validator.validate(record.verify_complete())


def test_checker_off_keeps_multiple_turns_and_self_revision(tmp_path):
    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("old", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("old-suffix", WriterDecision.CONTINUE),
        ("br_0001", 3): repair("corrected", "step_0001"),
        ("br_0002", 2): _writer_output("new-suffix", WriterDecision.COMPLETE),
    }
    try:
        query = asyncio.run(o.submit("Test a local mathematical goal"))
        state = record.verify_complete()
        assert state["checks"] == []
        assert query.status().phase == RunPhase.REVIEW_READY
        assert state["branches"][0]["step_revision_ids"] == ["step_0001", "step_0002"]
        assert state["branches"][1]["step_revision_ids"] == ["step_0003", "step_0004"]
        assert state["step_revisions"][2]["replaces_step_revision_id"] == "step_0001"
        assert state["human_actions"] == []
        assert state["candidates"][0]["provenance"]["content_class"] == "model_only"
        assert all(c["role"] != "checker" for c in state["model_calls"])
        assert_schemas(record)
    finally:
        control.close()


def test_hard_defect_returns_feedback_and_repair_not_kill(tmp_path):
    o, record, control, runtime = harness(tmp_path)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("old", WriterDecision.COMPLETE),
        ("br_0001", 2): repair("fixed", "step_0001"),
        ("br_0002", 2): _writer_output("finish", WriterDecision.COMPLETE),
    }

    def check(request):
        if request.target.step_revision_id == "step_0001":
            return CheckOutput(
                "hard_defect",
                "An explicit local inconsistency.",
                (CheckEvidence("scope_quote", "step_0001", "Toy scope old"),),
                "stop",
                Usage({}),
            )
        return CheckOutput(
            "ok", "The replacement is consistent.", (), "stop", Usage({})
        )

    runtime.check_factory = check
    try:
        asyncio.run(o.submit("Root"))
        state = record.verify_complete()
        assert not any(b["status"] == "killed" for b in state["branches"])
        second = [
            v.request
            for v in runtime._invocations.values()
            if v.invocation.role.value == "writer"
        ][1]
        assert second.checker_feedback[0]["verdict"] == "hard_defect"
        assert second.checker_feedback[0]["confirmed_refutation"] is False
        assert state["candidates"][0]["unresolved_check_ids"] == []
    finally:
        control.close()


def test_disagreement_remains_explicit_on_final_candidate(tmp_path):
    o, record, control, runtime = harness(tmp_path)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("disputed", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output(
            "conditional-conclusion", WriterDecision.COMPLETE
        ),
    }
    runtime.check_factory = lambda request: CheckOutput(
        "objection" if request.target.step_revision_id == "step_0001" else "ok",
        "Unresolved physical assumption."
        if request.target.step_revision_id == "step_0001"
        else "Conditional conclusion.",
        (),
        "stop",
        Usage({}),
    )
    try:
        asyncio.run(o.submit("Root"))
        candidate = record.verify_complete()["candidates"][0]
        assert candidate["status"] == "conditional"
        assert candidate["unresolved_check_ids"] == ["check_0001"]
    finally:
        control.close()


def test_ordinary_product_checker_receives_and_can_quote_current_gates(tmp_path):
    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    o, record, control, runtime = harness(tmp_path)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("endpoint", WriterDecision.COMPLETE),
    }
    seen = []

    def check(request):
        seen.append(request)
        assert request.completion_requirements == COMMON_WRITER_CLOSURE_REQUIREMENTS
        source_text = " ".join(source.text for source in request.evidence_sources)
        assert COMMON_WRITER_CLOSURE_REQUIREMENTS[5] in source_text
        return CheckOutput("ok", "Fine.", (), "stop", Usage({}))

    runtime.check_factory = check
    try:
        asyncio.run(o.submit("Root"))
        assert len(seen) == 1
        assert seen[0].completion_intent
        record.verify_complete()
    finally:
        control.close()


def test_check_request_records_completion_intent(tmp_path):
    """The completion-intent flag decides what the check enforces, so it has to
    survive in the record.  Without it, whether a completion review ever ran is
    only recoverable from whatever the checker happens to echo in prose."""

    o, record, control, runtime = harness(tmp_path)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("intermediate", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("endpoint", WriterDecision.COMPLETE),
    }
    runtime.check_factory = lambda request: CheckOutput(
        "ok", "Fine.", (), "stop", Usage({})
    )
    try:
        asyncio.run(o.submit("Root"))
        state = record.verify_complete()
        # The record itself must be unchanged: the request reason is the same
        # string every round, because it is rendered into the final judge packet.
        assert {item["reason"] for item in state["checks"]} == {
            "Required deterministic step check before candidate eligibility."
        }
        audit = [
            json.loads(line)
            for line in (tmp_path / "check_completion_intent.jsonl")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        assert audit == [
            {
                "check_id": "check_0001",
                "completion_intent": False,
                "target_step_revision_id": "step_0001",
            },
            {
                "check_id": "check_0002",
                "completion_intent": True,
                "target_step_revision_id": "step_0002",
            },
        ]
        intents = {
            request.target.step_revision_id: request.completion_intent
            for request in (
                value.request
                for value in runtime._invocations.values()
                if value.invocation.role.value == "checker"
            )
        }
        assert intents == {"step_0001": False, "step_0002": True}
    finally:
        control.close()


def test_model_blocked_is_not_completion(tmp_path):
    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): replace(
            _writer_output("gap", WriterDecision.CONTINUE),
            control=WriterControl(
                WriterDecision.BLOCKED, (), reason="Missing a needed identity."
            ),
        )
    }
    try:
        query = asyncio.run(o.submit("Root"))
        assert query.status().stop_reason == "model_blocked"
        assert record.verify_complete()["candidates"] == []
        assert control.run(o.config.run_id).phase == RunPhase.PAUSED
    finally:
        control.close()


def test_crash_after_finished_revision_recovers_once(tmp_path):
    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("old", WriterDecision.CONTINUE),
        ("br_0001", 2): repair("new", "step_0001"),
    }

    async def scenario():
        await o.initialize("Root")
        await o._run_writer("br_0001")
        with (
            patch.object(
                record, "apply_model_revision", side_effect=RuntimeError("crash")
            ),
            pytest.raises(RuntimeError, match="crash"),
        ):
            await o._run_writer("br_0001")
        invocations = len(runtime._invocations)
        assert o._repair_record_prefix()
        assert not o._repair_record_prefix()
        assert len(runtime._invocations) == invocations
        assert len(record.verify_complete()["branches"]) == 2

    try:
        asyncio.run(scenario())
    finally:
        control.close()


def test_record_version_cannot_be_mixed(tmp_path):
    o, record, control, _ = harness(tmp_path, checker=False)
    try:
        asyncio.run(o.initialize("Root"))
        events = record.events
        events[1]["schema_version"] = "derivation-agent-event-v1"
        with pytest.raises(ContractError, match="version cannot change"):
            replay_events(events)
    finally:
        control.close()


def test_unlimited_passes_legacy_call_cap_and_rotates_history(tmp_path):
    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", slot): _writer_output(
            str(slot),
            WriterDecision.COMPLETE if slot == 101 else WriterDecision.CONTINUE,
        )
        for slot in range(1, 102)
    }
    try:
        query = asyncio.run(o.submit("Root"))
        state = record.verify_complete()
        assert sum(call["role"] == "writer" for call in state["model_calls"]) == 101
        assert query.status().phase == RunPhase.REVIEW_READY
        assert runtime.rehydrations
        requests = [
            item.request
            for item in runtime._invocations.values()
            if item.invocation.role.value == "writer"
        ]
        assert len(requests[-1].full_transcript) == 100
        assert requests[-1].granularity == "one_task"
    finally:
        control.close()


def test_method_source_is_registered_and_quote_schema_validates(tmp_path):
    import jsonschema

    o, record, control, runtime = harness(tmp_path)
    runtime.evidence_sources = lambda: (
        EvidenceSource(
            "literature_quote", "src_method", "A frozen method identity, equation 7."
        ),
    )
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("disputed", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("conditional", WriterDecision.COMPLETE),
    }
    runtime.check_factory = lambda request: CheckOutput(
        "hard_defect" if request.target.step_revision_id == "step_0001" else "ok",
        "See the frozen method.",
        (CheckEvidence("literature_quote", "src_method", "equation 7"),)
        if request.target.step_revision_id == "step_0001"
        else (),
        "stop",
        Usage({}),
    )
    try:
        asyncio.run(o.submit("Root"))
        state = record.verify_complete()
        assert state["candidates"][0]["status"] == "conditional"
        assert state["source_evidence"][0]["source_id"] == "src_method"
        schema_dir = Path(__file__).parents[1] / "derivation_agent_record/schemas"
        event_schema = json.loads((schema_dir / "event-v1.1.schema.json").read_text())
        canonical_schema = json.loads(
            (schema_dir / "canonical-v1.1.schema.json").read_text()
        )
        for event in record.events:
            jsonschema.validate(event, event_schema)
        jsonschema.validate(state, canonical_schema)
    finally:
        control.close()


def test_local_repair_limit_is_same_lineage_even_with_new_wording(tmp_path):
    o, record, control, runtime = harness(tmp_path, checker=False)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("old", WriterDecision.CONTINUE),
        ("br_0001", 2): repair("repair-one", "step_0001"),
        ("br_0002", 2): repair("repair-two", "step_0002"),
        ("br_0003", 2): repair("third-wording", "step_0003"),
        ("br_0004", 2): repair("fourth-wording", "step_0004"),
    }

    async def scenario():
        await o.initialize("Root")
        await o._run_writer("br_0001")
        await o._run_writer("br_0001")
        await o._run_writer("br_0002")
        await o._run_writer("br_0003")
        # Three repairs of one lineage are inside the budget; the fourth is the
        # one that has to be deferred, and rewording the request does not buy
        # another attempt.
        await o._run_writer("br_0004")
        state = record.verify_complete()
        assert len(state["step_revisions"]) == 4
        assert record.events[-1]["type"] == "model_revision_deferred"
        assert not o._repair_record_prefix()
        runtime.writer_outputs[("br_0004", 2)] = _writer_output(
            "different-subgoal", WriterDecision.COMPLETE
        )
        await o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        assert record.verify_complete()["candidates"]
        assert_schemas(record)

    try:
        asyncio.run(scenario())
    finally:
        control.close()


@pytest.mark.parametrize("role", ["writer", "checker"])
def test_platform_failure_pauses_and_explicit_resume_retries(tmp_path, role):
    o, record, control, runtime = harness(tmp_path, checker=role == "checker")
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("finish", WriterDecision.COMPLETE)
    }
    failure = FakeFailure("usage_limit", "Platform quota exhausted", "", False)
    if role == "writer":
        runtime.writer_outputs[("br_0001", 1)] = failure
    else:
        runtime.check_factory = lambda request: failure
    try:
        query = asyncio.run(o.submit("Root"))
        assert query.status().phase == RunPhase.PAUSED
        assert record.verify_complete()["candidates"] == []
        runtime.writer_outputs[("br_0001", 1)] = _writer_output(
            "finish", WriterDecision.COMPLETE
        )
        runtime.check_factory = lambda request: CheckOutput(
            "ok", "Recovered tool", (), "stop", Usage({})
        )
        resumed = asyncio.run(
            o.resume(actor_id="tester", reason="Platform quota restored; resume.")
        )
        assert resumed.status().phase == RunPhase.REVIEW_READY
        state = record.verify_complete()
        assert state["candidates"][0]["status"] == "eligible"
        assert state["judgements"] == []
        assert_schemas(record)
        if role == "checker":
            assert state["checks"][0]["verdict"] == "instrument_failure"
            assert state["checks"][0]["required_for_candidate"] is False
    finally:
        control.close()


def test_latest_dispute_is_delivered_before_unchanged_completion(tmp_path):
    o, record, control, runtime = harness(tmp_path)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("old-dispute", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("new-dispute", WriterDecision.COMPLETE),
        ("br_0001", 3): _writer_output("new-dispute", WriterDecision.COMPLETE),
    }
    runtime.check_factory = lambda request: CheckOutput(
        "objection",
        f"Question about {request.target.step_revision_id}",
        (),
        "stop",
        Usage({}),
    )
    try:
        asyncio.run(o.submit("Root"))
        writers = [
            item.request
            for item in runtime._invocations.values()
            if item.invocation.role.value == "writer"
        ]
        assert writers[-1].checker_feedback[-1]["step_revision_id"] == "step_0002"
        state = record.verify_complete()
        assert len(state["step_revisions"]) == 2
        assert state["candidates"][0]["unresolved_check_ids"] == [
            "check_0001",
            "check_0002",
        ]
        assert any(
            event["type"] == "writer_route_completion" for event in record.events
        )
    finally:
        control.close()
