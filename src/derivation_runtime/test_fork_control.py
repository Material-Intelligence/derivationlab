"""Forking: the guidance, the option a capped run must not offer, and the trace.

A Writer may choose the ``fork`` control decision at a genuinely
underdetermined ingredient, with sensible alternative routes, in a run whose
configuration caps the active branches at one.  Before this change the fork
never became a branch: ``_ensure_model_forks`` reached the cap and hit
``continue``, and nothing at all was recorded.  The parent branch then carried
on as though it had said ``continue`` and could end up declaring itself
blocked.

Three changes are tested here.  The Writer is told, generically, to fork before
reporting blocked.  A run that can hold only one active branch no longer offers
``fork`` at all - neither in the output schema nor in the developer text - so the
model is never handed an option the host will discard.  And a fork the cap
refuses at runtime leaves a durable control-plane line and reaches the Writer as
a runtime note on its next turn.

The wording is fixed by the prompt contract kept in
``fixtures/prompt_rule_texts_v1.md``, section "Fork guidance
and scope consistency"; ``test_fork_texts_match_the_rule_contract`` compares
against that file directly so the code cannot drift from the contract.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

from .control import ControlStore
from .orchestrator import DerivationOrchestrator
from .prompts import (
    CHECKER_DEVELOPER_INSTRUCTIONS,
    FORK_SUPPRESSED_WRITER_NOTE,
    FORK_UNAVAILABLE_WRITER_PARAGRAPH,
    WRITER_DEVELOPER_INSTRUCTIONS,
    rehydrated_writer_instructions,
    writer_developer_instructions,
    writer_output_schema,
    writer_user_prompt,
)
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime
from .types import (
    ProviderForkError,
    RunPhase,
    RuntimeInvocationError,
    StepContent,
    StepSnapshot,
    WriterRequest,
)

RULE_TEXTS = Path(__file__).resolve().parent / "fixtures" / "prompt_rule_texts_v1.md"


def _flat(value: str) -> str:
    return " ".join(value.split())


def _target() -> StepSnapshot:
    return StepSnapshot(
        step_revision_id="step-one",
        content=StepContent(
            claim="Claim one",
            why="Why one",
            source="Source one",
            derivation="Derivation one",
            scope="Scope one",
        ),
    )


def _writer_request(**overrides: object) -> WriterRequest:
    return replace(
        WriterRequest(
            run_id="run-fork-control",
            branch_id="branch-1",
            step_slot=1,
            task_text="Derive the invariant.",
            hypothesis="Use the declared route.",
            transcript=(),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def test_fork_texts_match_the_rule_contract() -> None:
    text = RULE_TEXTS.read_text(encoding="utf-8")
    section = text[text.index("## Fork guidance and scope consistency") :]

    def blocks(start: str, end: str | None) -> list[str]:
        part = section[section.index(start) :]
        if end is not None:
            part = part[: part.index(end)]
        return re.findall(r"```\n(.*?)\n```", part, re.DOTALL)

    guidance, unavailable = blocks(
        "#### Fork guidance, Writer sentence", "### Trace of a suppressed fork"
    )
    assert _flat(guidance) in _flat(WRITER_DEVELOPER_INSTRUCTIONS)
    assert _flat(FORK_UNAVAILABLE_WRITER_PARAGRAPH) == _flat(unavailable)

    (note,) = blocks(
        "#### Suppressed fork, Writer runtime note text", "### Declared scope consistent"
    )
    assert _flat(FORK_SUPPRESSED_WRITER_NOTE) == _flat(note)

    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    # The scope-consistency subsection is the last one in the file.
    scope_rule, gate_sentence = blocks(
        "#### Scope consistency, Checker paragraph", None
    )
    assert _flat(scope_rule) in _flat(CHECKER_DEVELOPER_INSTRUCTIONS)
    assert _flat(gate_sentence) in _flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[6])


def test_the_cap_paragraph_is_exactly_one_added_paragraph() -> None:
    capped = writer_developer_instructions(fork_available=False)
    assert capped != WRITER_DEVELOPER_INSTRUCTIONS
    assert (
        capped.replace(FORK_UNAVAILABLE_WRITER_PARAGRAPH + "\n", "", 1)
        == WRITER_DEVELOPER_INSTRUCTIONS
    )
    # It lands with the rule it qualifies, ahead of the closure obligations.
    flat = _flat(capped)
    assert flat.index("report blocked only when no such route remains") < flat.index(
        "Forking is unavailable in this run"
    )
    assert flat.index("Forking is unavailable in this run") < flat.index(
        "Review checker_feedback as fallible scientific criticism"
    )


def test_a_run_that_can_fork_renders_the_untouched_baseline() -> None:
    assert writer_developer_instructions() == WRITER_DEVELOPER_INSTRUCTIONS
    assert writer_developer_instructions(fork_available=True) == (
        WRITER_DEVELOPER_INSTRUCTIONS
    )
    transcript = (_target(),)
    assert rehydrated_writer_instructions(transcript).startswith(
        WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert rehydrated_writer_instructions(transcript, fork_available=False).startswith(
        writer_developer_instructions(fork_available=False)
    )


def test_the_output_schema_offers_fork_only_where_a_branch_can_exist() -> None:
    def decisions(**overrides: object) -> list[str]:
        schema = writer_output_schema(_writer_request(**overrides))
        return schema["properties"]["control"]["properties"]["decision"]["enum"]

    # Record 1.0 narrows the enum on its own; the removal composes with it
    # rather than working around it.
    assert decisions() == ["continue", "fork", "complete"]
    assert decisions(fork_available=False) == ["continue", "complete"]
    # Under 1.1 with nothing revisable yet, only revise is already gone.
    assert decisions(record_version="1.1") == [
        "continue",
        "fork",
        "complete",
        "blocked",
    ]
    # Removing the option leaves the rest of the enum untouched and ordered.
    assert decisions(record_version="1.1", fork_available=False) == [
        "continue",
        "complete",
        "blocked",
    ]


def test_the_rendered_prompt_is_unchanged_without_a_runtime_note() -> None:
    assert "runtime_notes" not in writer_user_prompt(_writer_request())
    assert "runtime_notes" not in writer_user_prompt(
        _writer_request(fork_available=False)
    )


def test_a_runtime_note_is_the_only_new_key_in_the_rendered_prompt() -> None:
    note = {
        "kind": "fork_suppressed",
        "reason": "max_active_branches",
        "alternatives": ["Close the ingredient the other way."],
        "guidance": FORK_SUPPRESSED_WRITER_NOTE,
    }
    without = json.loads(writer_user_prompt(_writer_request()))
    with_note = json.loads(writer_user_prompt(_writer_request(runtime_notes=(note,))))
    assert with_note == {**without, "runtime_notes": [note]}


def _harness(tmp_path: Path, *, max_active_branches: int):
    config = replace(
        _config(
            max_active_branches=max_active_branches,
            max_model_calls=100,
            retries=0,
        ),
        record_version="1.1",
        max_model_calls=None,
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


def _run(
    tmp_path: Path,
    *,
    max_active_branches: int,
    fork_error: str | None = None,
    fork_exception: Exception | None = None,
):
    orchestrator, record, control, runtime = _harness(
        tmp_path, max_active_branches=max_active_branches
    )
    requests: list[WriterRequest] = []
    original = runtime.start_writer

    async def start_writer(request: WriterRequest, session):
        requests.append(request)
        return await original(request, session)

    runtime.start_writer = start_writer  # type: ignore[method-assign]
    if fork_error is not None or fork_exception is not None:
        raised = (
            fork_exception
            if fork_exception is not None
            else ProviderForkError(fork_error or "")
        )
        attempts: list[str] = []

        async def fork(session, completed_operation_id: str):
            attempts.append(completed_operation_id)
            raise raised

        runtime.fork = fork  # type: ignore[method-assign]
        orchestrator.fork_attempts = attempts  # type: ignore[attr-defined]
    asyncio.run(orchestrator.submit("Explore the root route."))
    return requests, record.verify_complete(), control, orchestrator


# The deterministic fixture forks once, at slot 2 of the root branch, with one
# named alternative.  Under a cap of one that fork cannot be created; under a cap
# of two it can.
FIXTURE_ALTERNATIVE = "Use a symmetry-based alternative."


def test_a_suppressed_fork_reaches_the_control_plane_and_the_next_turn(
    tmp_path: Path,
) -> None:
    requests, canonical, control, orchestrator = _run(tmp_path, max_active_branches=1)
    # Nothing explores the alternative, exactly as before.
    assert canonical["summary"]["branch_count"] == 1

    # The control plane keeps the disposition, with the alternative it refused.
    (line,) = control.dispositions(orchestrator.config.run_id)
    assert line.kind == "fork_suppressed"
    assert line.branch_id == "br_0001"
    recorded = json.loads(line.detail)
    assert recorded["fork_suppressed"] is True
    assert recorded["reason"] == "max_active_branches"
    assert recorded["alternatives"] == [FIXTURE_ALTERNATIVE]
    assert recorded["anchor_step_revision_id"] == "step_0002"

    # And the Writer is told on the turn that follows the fork, once.
    noted = [item for item in requests if item.runtime_notes]
    assert len(noted) == 1
    assert noted[0].step_slot == 3
    (note,) = noted[0].runtime_notes
    assert note["kind"] == "fork_suppressed"
    assert note["reason"] == "max_active_branches"
    assert note["alternatives"] == [FIXTURE_ALTERNATIVE]
    assert note["guidance"] == FORK_SUPPRESSED_WRITER_NOTE
    # A capped run never offered the decision in the first place.
    assert {item.fork_available for item in requests} == {False}
    control.close()


def test_a_fork_within_the_cap_creates_the_branch_and_leaves_no_note(
    tmp_path: Path,
) -> None:
    requests, canonical, control, orchestrator = _run(tmp_path, max_active_branches=2)
    children = [
        item
        for item in canonical["branches"]
        if item["created_reason"] == "model_alternative"
    ]
    assert [item["hypothesis"]["text"] for item in children] == [FIXTURE_ALTERNATIVE]
    assert control.dispositions(orchestrator.config.run_id) == []
    assert not any(item.runtime_notes for item in requests)
    assert {item.fork_available for item in requests} == {True}
    control.close()


# What a live fork probe first met: the App Server refused the
# first live fork this client ever asked for.  The refusal is not a reason to
# lose a derivation that was going fine, so it is traced exactly like a fork the
# cap refused - the only difference is why.
FORK_REFUSAL = "thread snapshot changed terminal turn 'turn-fixture-0001'"


def test_a_refused_fork_is_traced_and_the_parent_branch_continues(
    tmp_path: Path,
) -> None:
    requests, canonical, control, orchestrator = _run(
        tmp_path, max_active_branches=2, fork_error=FORK_REFUSAL
    )
    # The run reached its own end rather than dying with the fork.
    assert orchestrator.fork_attempts  # the fork really was attempted
    assert canonical["summary"]["branch_count"] == 1
    assert canonical["summary"]["candidate_count"] == 1

    (line,) = control.dispositions(orchestrator.config.run_id)
    assert line.kind == "fork_suppressed"
    assert line.branch_id == "br_0001"
    recorded = json.loads(line.detail)
    assert recorded["reason"] == "fork_failed"
    assert recorded["error"] == FORK_REFUSAL
    assert recorded["alternatives"] == [FIXTURE_ALTERNATIVE]
    assert recorded["anchor_step_revision_id"] == "step_0002"

    # And the Writer is told what happened, in the same shape as a capped fork.
    noted = [item for item in requests if item.runtime_notes]
    assert len(noted) == 1
    (note,) = noted[0].runtime_notes
    assert note["kind"] == "fork_suppressed"
    assert note["reason"] == "fork_failed"
    assert note["error"] == FORK_REFUSAL
    assert note["alternatives"] == [FIXTURE_ALTERNATIVE]
    assert note["guidance"] == FORK_SUPPRESSED_WRITER_NOTE
    # The run could fork, so the option was offered; only the attempt failed.
    assert {item.fork_available for item in requests} == {True}
    control.close()


# What the probe met on its second attempt: the fork request never went
# out, because preparing it failed - the anchor read back in a wider itemsView.
# A provider call that fails while opening an *optional* alternative branch is
# the same disposition as a refused fork: recorded, and the parent carries on.
# A client that is really broken still ends the run at the next call the
# derivation depends on.
FORK_PREPARATION_FAILURE = "fork anchor changed after live collection"


def test_a_failed_fork_preparation_is_traced_and_the_run_survives(
    tmp_path: Path,
) -> None:
    _requests, canonical, control, orchestrator = _run(
        tmp_path,
        max_active_branches=2,
        fork_exception=RuntimeInvocationError(
            "provider_protocol_error",
            FORK_PREPARATION_FAILURE,
            partial_output="",
            retryable=False,
        ),
    )
    assert orchestrator.fork_attempts
    assert canonical["summary"]["branch_count"] == 1
    assert canonical["summary"]["candidate_count"] == 1

    (line,) = control.dispositions(orchestrator.config.run_id)
    assert line.kind == "fork_suppressed"
    recorded = json.loads(line.detail)
    assert recorded["reason"] == "fork_failed"
    assert recorded["error"] == FORK_PREPARATION_FAILURE
    assert recorded["alternatives"] == [FIXTURE_ALTERNATIVE]
    control.close()


def test_the_disposition_outlives_a_control_plane_rebuild(tmp_path: Path) -> None:
    # Every other control table is disposable because the record can rebuild it.
    # This one cannot be rebuilt verbatim - the record states the suppression
    # only by omission - so the rebuild must leave it alone.
    orchestrator, record, control, _ = _harness(tmp_path, max_active_branches=1)
    asyncio.run(orchestrator.submit("Explore the root route."))
    run_id = orchestrator.config.run_id
    assert len(control.dispositions(run_id)) == 1

    snapshot = record.snapshot()
    control.rebuild_from_snapshot(
        snapshot,
        credential_profile_id=orchestrator.config.credential_profile_id,
        phase=RunPhase.REVIEW_READY,
        stop_reason=None,
    )
    assert len(control.dispositions(run_id)) == 1
    assert control.dispositions(run_id, kind="other") == []
    control.close()


def test_the_note_is_re_derived_from_the_record_rather_than_remembered(
    tmp_path: Path,
) -> None:
    # A resumed process has no memory of the suppression, so the note has to be
    # a function of the record alone: the Writer's fork control is immutable in
    # its call, and a created alternative is a branch anchored at the same step.
    orchestrator, record, control, _ = _harness(tmp_path, max_active_branches=1)
    try:
        asyncio.run(orchestrator.submit("Explore the root route."))
        state = record.snapshot()
        branch = next(
            item for item in state["branches"] if item["branch_id"] == "br_0001"
        )
        forked = replace_tip(branch, slot=2)
        (note,) = orchestrator._fork_suppression_note(state, forked)
        assert note["alternatives"] == [FIXTURE_ALTERNATIVE]
        # At the real tip the last step is no longer the fork, so no note.
        assert orchestrator._fork_suppression_note(state, branch) == ()
    finally:
        control.close()


def replace_tip(branch: dict, *, slot: int) -> dict:
    """The same branch as it stood when its tip was the given slot."""

    return {**branch, "step_revision_ids": branch["step_revision_ids"][:slot]}
