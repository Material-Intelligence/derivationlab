"""A turn refused for want of context room is retried once on a fresh thread.

Scenario: the first Writer output of slot 1 is rejected by the formula gate
(say U+0005 in place of backslashes), and its retry goes to the SAME provider
thread, which already holds the slot-1 turn with a large source delivery (for
example 150,000 input tokens against a 256,000 token window).  The retry prompt
delivers the sources again, so the provider refuses the turn with "Codex ran
out of room in the model's context window. ..." (provider_failed, retryable,
zero body), and without this rule Record 1.1 would pause the branch.  The host
retries such a failure once on a thread rehydrated from the Record, as a
context rotation does.  No model is called.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from .control import ControlStore
from .fake import DeterministicFakeRuntime, FakeFailure
from .orchestrator import DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import (
    CheckOutput,
    CheckRequest,
    FormulaValidationResult,
    ProviderLineage,
    RunPhase,
    WriterDecision,
    WriterOutput,
    WriterRequest,
)

# The provider's context-window refusal text.
CONTEXT_WINDOW_FAILURE = FakeFailure(
    failure_kind="provider_failed",
    message=(
        "Codex ran out of room in the model's context window. Start a new thread "
        "or clear earlier history before retrying."
    ),
    partial_output="",
    retryable=True,
)
OTHER_PROVIDER_FAILURE = FakeFailure(
    failure_kind="provider_failed",
    message="provider turn failed",
    partial_output="",
    retryable=True,
)
CONTROL_CHARACTER = FormulaValidationResult(
    issues=(
        {
            "code": "control_character",
            "severity": "error",
            "field": "derivation",
            "formula_index": 1,
            "start": 0,
            "end": 1,
            "message": "Control character U+0005",
            "excerpt": "\\x05",
        },
    )
)

ROOT_HYPOTHESIS = "Explore the declared route."


class SequencedFakeRuntime(DeterministicFakeRuntime):
    """Serve successive outputs for the same branch slot (retries after feedback)."""

    def __init__(
        self,
        sequences: dict[tuple[str, int], list[WriterOutput | FakeFailure]],
        **kwargs: Any,
    ) -> None:
        super().__init__(writer_outputs={}, **kwargs)
        self.sequences = {key: list(value) for key, value in sequences.items()}

    async def start_writer(self, request, session):  # type: ignore[no-untyped-def]
        key = (request.branch_id, request.step_slot)
        queue = self.sequences.get(key)
        if queue:
            self.writer_outputs[key] = queue.pop(0) if len(queue) > 1 else queue[0]
        return await super().start_writer(request, session)


def _output(label: str) -> WriterOutput:
    return _writer_output(label, WriterDecision.COMPLETE)


def _reject_first() -> Any:
    calls = {"count": 0}

    def validator(_content: Any) -> FormulaValidationResult:
        calls["count"] += 1
        return CONTROL_CHARACTER if calls["count"] == 1 else FormulaValidationResult()

    return validator


def _harness(
    directory: Path,
    writer_sequence: list[Any],
    *,
    check_factory=None,  # type: ignore[no-untyped-def]
    retries: int = 1,
):  # type: ignore[no-untyped-def]
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=retries),
        record_version="1.1",
        checker_enabled=True,
        formula_validation_policy="formula-v1",
    )
    defaults = _runtime()
    runtime = SequencedFakeRuntime(
        {("br_0001", 1): writer_sequence},
        check_factory=check_factory or defaults.check_factory,
        judge_factory=defaults.judge_factory,
    )
    requests: list[WriterRequest] = []
    sessions: list[Any] = []
    original = runtime.start_writer

    async def start_writer(request, session):  # type: ignore[no-untyped-def]
        requests.append(request)
        invocation = await original(request, session)
        sessions.append(invocation.session)
        return invocation

    runtime.start_writer = start_writer  # type: ignore[method-assign]
    record = RecordV1Writer(
        EventLogWriter(directory / "events.jsonl", config.run_id), config
    )
    control = ControlStore(directory / "control.sqlite")
    orchestrator = DerivationOrchestrator(
        config=config,
        task_text=TASK_TEXT,
        runtime=runtime,
        record=record,
        control=control,
        formula_validator=_reject_first(),
    )
    return orchestrator, record, control, runtime, requests, sessions


def _paused(record: RecordV1Writer) -> bool:
    return any(
        entry["to"] == "paused"
        for branch in record.snapshot()["branches"]
        for entry in branch["status_history"]
    )


def test_the_exhausted_retry_after_a_format_rejection_resumes_on_a_fresh_thread(
    tmp_path: Path,
) -> None:
    o, record, control, runtime, requests, sessions = _harness(
        tmp_path,
        [_output("u0005"), CONTEXT_WINDOW_FAILURE, _output("rewritten")],
    )
    try:
        query = asyncio.run(o.submit(ROOT_HYPOTHESIS))
        assert query.status().phase == RunPhase.REVIEW_READY
        assert control.run(o.config.run_id).stop_reason is None
    finally:
        control.close()
    canonical = record.verify_complete()
    writer_calls = [item for item in canonical["model_calls"] if item["role"] == "writer"]
    assert [item["state"] for item in writer_calls] == ["finished", "failed", "finished"]
    assert writer_calls[1]["failure"]["message"] == CONTEXT_WINDOW_FAILURE.message
    assert not _paused(record)
    (step,) = canonical["step_revisions"]
    assert step["origin"]["model_call_id"] == writer_calls[2]["model_call_id"]
    # The format retry stayed on the slot's thread, exactly as before; the turn
    # after the exhausted one runs on a thread rehydrated from the Record.
    assert sessions[0] == sessions[1]
    assert sessions[2] != sessions[1]
    assert sessions[2].lineage is ProviderLineage.REHYDRATED
    assert runtime.rehydrations == [(sessions[2].session_id, ())]
    assert runtime.rebinds == [("br_0001", sessions[2].session_id)]
    # The fresh turn is the same request: same slot, same format feedback.
    assert [(item.branch_id, item.step_slot) for item in requests] == [("br_0001", 1)] * 3
    assert requests[1].formula_feedback == requests[2].formula_feedback
    assert requests[1].formula_feedback == CONTROL_CHARACTER.issues
    assert requests[1] == requests[2]


def test_a_second_exhausted_context_on_the_slot_still_pauses(tmp_path: Path) -> None:
    o, record, control, _runtime, _requests, sessions = _harness(
        tmp_path,
        [_output("u0005"), CONTEXT_WINDOW_FAILURE, CONTEXT_WINDOW_FAILURE],
    )
    try:
        asyncio.run(o.submit(ROOT_HYPOTHESIS))
        run = control.run(o.config.run_id)
        assert (run.phase, run.stop_reason) == ("paused", "runtime_failure")
        assert not o._repair_record_prefix()
    finally:
        control.close()
    writer_calls = [
        item for item in record.verify_complete()["model_calls"] if item["role"] == "writer"
    ]
    assert [item["state"] for item in writer_calls] == ["finished", "failed", "failed"]
    assert len(sessions) == 3 and sessions[2].lineage is ProviderLineage.REHYDRATED
    assert _paused(record)


def test_other_writer_provider_failures_still_pause(tmp_path: Path) -> None:
    o, record, control, _runtime, _requests, sessions = _harness(
        tmp_path, [_output("u0005"), OTHER_PROVIDER_FAILURE, _output("x")]
    )
    try:
        asyncio.run(o.submit(ROOT_HYPOTHESIS))
        run = control.run(o.config.run_id)
        assert (run.phase, run.stop_reason) == ("paused", "runtime_failure")
    finally:
        control.close()
    assert len(sessions) == 2 and _paused(record)


def test_restart_after_the_exhausted_call_repairs_to_the_same_retry(
    tmp_path: Path,
) -> None:
    o, record, control, runtime, _requests, sessions = _harness(
        tmp_path,
        [_output("u0005"), CONTEXT_WINDOW_FAILURE, _output("rewritten")],
    )
    asyncio.run(o.initialize(ROOT_HYPOTHESIS))
    asyncio.run(o._run_writer("br_0001"))
    asyncio.run(o._run_writer("br_0001"))
    assert record.snapshot()["model_calls"][-1]["state"] == "failed"
    control.close()
    with ControlStore(tmp_path / "control.sqlite") as restored:
        restarted = DerivationOrchestrator(
            config=o.config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=restored,
            formula_validator=lambda _content: FormulaValidationResult(),
        )
        # Repair derives the retry from the Record and does not pause.
        assert not restarted._repair_record_prefix()
        query = asyncio.run(restarted.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        assert query.status().phase == RunPhase.REVIEW_READY
    assert not _paused(record)
    assert sessions[2].lineage is ProviderLineage.REHYDRATED
    record.verify_complete()


def _checker_sequence(outcomes: list[Any], fallback):  # type: ignore[no-untyped-def]
    queue = list(outcomes)

    def factory(request: CheckRequest) -> CheckOutput | FakeFailure:
        return queue.pop(0) if queue else fallback(request)

    return factory


def test_an_exhausted_checker_turn_is_retried_without_charging_the_retry_budget(
    tmp_path: Path,
) -> None:
    """retries=1: an ordinary failure spends the budget, the exhausted one does not."""

    ok_checker = _runtime().check_factory
    o, record, control, _app_runtime, _requests, _sessions = _harness(
        tmp_path,
        [_output("clean")],
        check_factory=_checker_sequence(
            [OTHER_PROVIDER_FAILURE, CONTEXT_WINDOW_FAILURE], ok_checker
        ),
    )
    o._formula_validator = lambda _content: FormulaValidationResult()
    try:
        query = asyncio.run(o.submit(ROOT_HYPOTHESIS))
        assert query.status().phase == RunPhase.REVIEW_READY
    finally:
        control.close()
    canonical = record.verify_complete()
    checker_calls = [
        item for item in canonical["model_calls"] if item["role"] == "checker"
    ]
    assert [item["state"] for item in checker_calls] == ["failed", "failed", "finished"]
    assert canonical["checks"][0]["verdict"] == "ok"


def test_an_ordinary_checker_failure_after_the_excused_one_still_ends_the_check(
    tmp_path: Path,
) -> None:
    ok_checker = _runtime().check_factory
    o, record, control, _app_runtime, _requests, _sessions = _harness(
        tmp_path,
        [_output("clean")],
        check_factory=_checker_sequence(
            [CONTEXT_WINDOW_FAILURE, OTHER_PROVIDER_FAILURE, OTHER_PROVIDER_FAILURE],
            ok_checker,
        ),
    )
    o._formula_validator = lambda _content: FormulaValidationResult()
    try:
        asyncio.run(o.submit(ROOT_HYPOTHESIS))
        run = control.run(o.config.run_id)
        assert run.phase == "paused"
    finally:
        control.close()
    canonical = record.verify_complete()
    checker_calls = [
        item for item in canonical["model_calls"] if item["role"] == "checker"
    ]
    assert [item["state"] for item in checker_calls] == ["failed", "failed", "failed"]
    assert canonical["checks"][0]["verdict"] == "instrument_failure"


def test_a_context_window_message_from_a_non_retryable_or_bodied_call_is_ordinary(
    tmp_path: Path,
) -> None:
    """The classifier needs all of: provider_failed, retryable, zero body, message."""

    o, _record, control, _app_runtime, _requests, _sessions = _harness(
        tmp_path, [_output("clean")]
    )
    try:
        exhausted = {
            "state": "failed",
            "body_chars": 0,
            "role": "writer",
            "failure": {
                "kind": "provider_failed",
                "retryable": True,
                "message": CONTEXT_WINDOW_FAILURE.message,
            },
        }
        assert o._context_window_exhausted(exhausted)
        assert o._context_window_excuse([exhausted]) == 1
        for mutation in (
            {"body_chars": 12},
            {"state": "finished"},
            {"failure": {**exhausted["failure"], "retryable": False}},
            {"failure": {**exhausted["failure"], "kind": "invalid_model_output"}},
            {"failure": {**exhausted["failure"], "message": "the thread is too long"}},
        ):
            other = {**exhausted, **mutation}
            assert not o._context_window_exhausted(other), mutation
            assert o._context_window_excuse([other]) == 0, mutation
        # A judge target never spends the excuse, whatever its message says.
        assert o._context_window_excuse([{**exhausted, "role": "judge"}]) == 0
    finally:
        control.close()
