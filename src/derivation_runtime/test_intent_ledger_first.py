"""The switchable intent-ledger-first obligation.

The obligation is switchable, so the tests below care about two things in
equal measure: that a run with the flag on really carries the contracted
paragraphs into the developer texts and the rendered prompts, and that a run
with the flag off is byte-identical to the baseline.  A silent difference in the
baseline would change every prompt hash of every run that never asked for the
obligation, which is why the off case is asserted rather than assumed.

The wording is fixed by the prompt contract kept in
``fixtures/prompt_rule_texts_v1.md``, section "Intent ledger
first"; ``test_intent_ledger_paragraphs_match_the_rule_contract`` compares
against that file directly so the code cannot drift from the contract.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from .control import ControlStore
from .orchestrator import DerivationOrchestrator
from .prompts import (
    CHECKER_DEVELOPER_INSTRUCTIONS,
    INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
    INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH,
    INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS,
    INTENT_LEDGER_FIRST_WRITER_PARAGRAPH,
    WRITER_DEVELOPER_INSTRUCTIONS,
    checker_developer_instructions,
    checker_user_prompt,
    rehydrated_writer_instructions,
    writer_developer_instructions,
    writer_user_prompt,
)
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import (
    CheckOutput,
    CheckRequest,
    StepContent,
    StepSnapshot,
    Usage,
    WriterControl,
    WriterDecision,
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
            run_id="run-intent-ledger",
            branch_id="branch-1",
            step_slot=1,
            task_text="Derive the invariant.",
            hypothesis="Use the declared route.",
            transcript=(),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def _check_request(**overrides: object) -> CheckRequest:
    target = _target()
    return replace(
        CheckRequest(
            run_id="run-intent-ledger",
            check_id="check-1",
            task_text="Derive the invariant.",
            target=target,
            transcript=(target,),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def test_intent_ledger_paragraphs_match_the_rule_contract() -> None:
    text = RULE_TEXTS.read_text(encoding="utf-8")
    # The contract file holds several sections, so this one is bounded on both
    # sides rather than read to the end of the file.
    section = text[
        text.index("## Intent ledger first") : text.index(
            "## Step references and dimensional closure"
        )
    ]
    blocks = re.findall(r"```\n(.*?)\n```", section, re.DOTALL)
    assert len(blocks) == 2, "the intent-ledger section must supply exactly the Writer and Checker texts"
    writer_block, checker_block = blocks
    # Only the line wrapping may differ; not one word of physics or process.
    assert _flat(INTENT_LEDGER_FIRST_WRITER_PARAGRAPH) == _flat(writer_block)
    assert _flat(INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH) == _flat(checker_block)


def test_the_obligation_adds_exactly_one_paragraph_to_each_baseline_text() -> None:
    writer = INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS
    checker = INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS
    assert writer != WRITER_DEVELOPER_INSTRUCTIONS
    assert checker != CHECKER_DEVELOPER_INSTRUCTIONS
    assert (
        writer.replace(INTENT_LEDGER_FIRST_WRITER_PARAGRAPH + "\n", "", 1)
        == WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert (
        checker.replace(INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH + "\n", "", 1)
        == CHECKER_DEVELOPER_INSTRUCTIONS
    )
    # The Writer paragraph follows the sentence that introduces the dependency
    # ledger, and the Checker paragraph follows the ledger-verification block,
    # so each role reads its ledger rules as one passage.
    flat_writer = _flat(writer)
    assert flat_writer.index("dependency ledger before returning complete") < (
        flat_writer.index("must be an intent ledger")
    )
    assert flat_writer.index("must be an intent ledger") < flat_writer.index(
        "When reading_preparation delivers full documents"
    )
    flat_checker = _flat(checker)
    assert flat_checker.index("do not add entries on its behalf") < (
        flat_checker.index("the run declares intent_ledger_first")
    )
    assert flat_checker.index("the run declares intent_ledger_first") < (
        flat_checker.index("Regardless of completion intent")
    )


def test_developer_texts_are_the_untouched_baseline_when_the_flag_is_off() -> None:
    assert writer_developer_instructions() == WRITER_DEVELOPER_INSTRUCTIONS
    assert checker_developer_instructions() == CHECKER_DEVELOPER_INSTRUCTIONS
    assert (
        writer_developer_instructions(intent_ledger_first=False)
        == WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert (
        checker_developer_instructions(intent_ledger_first=False)
        == CHECKER_DEVELOPER_INSTRUCTIONS
    )
    transcript = (_target(),)
    assert rehydrated_writer_instructions(transcript).startswith(
        WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert rehydrated_writer_instructions(
        transcript, intent_ledger_first=True
    ).startswith(INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS)


def test_rendered_prompts_are_byte_identical_when_the_flag_is_off() -> None:
    writer = writer_user_prompt(_writer_request())
    checker = checker_user_prompt(_check_request())
    # The exact top-level key set of both roles. A key added by a switchable
    # obligation would change every prompt hash even for a baseline run, so
    # the baseline key set is asserted rather than assumed. The always-on
    # route-lineage catalog is part of the baseline. Ordinary
    # product runs now also receive the generic closure gates without needing
    # a research reading preparation; this is independent of either flag.
    assert set(json.loads(writer)) == {
        "role",
        "run_id",
        "branch_id",
        "step_slot",
        "task",
        "hypothesis",
        "granularity",
        "checker_enabled",
        "record_version",
        "checker_feedback",
        "feedback_catalog",
        "full_feedback_count",
        "feedback_read_rule",
        "repair_attempts",
        "max_local_repairs",
        "exhausted_revision_ids",
        "transcript",
        "transcript_catalog",
        "full_transcript_length",
        "history_read_rule",
        "reading_preparation",
        "completion_requirements",
    }
    assert set(json.loads(checker)) == {
        "role",
        "run_id",
        "check_id",
        "task",
        "target",
        "transcript",
        # Always-on route lineage: the Checker resolves a slot reference
        # itself instead of failing a step for naming a superseded revision.
        "transcript_catalog",
        "evidence_sources",
        "evidence_rule",
        "completion_requirements",
        "candidate_completion_intent",
    }
    assert "intent_ledger_first" not in writer
    assert "intent_ledger_first" not in checker
    assert "target_is_route_first_step" not in checker


def test_rendered_prompts_carry_only_the_new_keys_when_the_flag_is_on() -> None:
    writer_off = json.loads(writer_user_prompt(_writer_request()))
    writer_on = json.loads(
        writer_user_prompt(_writer_request(intent_ledger_first=True))
    )
    assert writer_on == {**writer_off, "intent_ledger_first": True}

    checker_off = json.loads(checker_user_prompt(_check_request()))
    for first_step in (True, False):
        checker_on = json.loads(
            checker_user_prompt(
                _check_request(
                    intent_ledger_first=True,
                    target_is_route_first_step=first_step,
                )
            )
        )
        assert checker_on == {
            **checker_off,
            "intent_ledger_first": True,
            "target_is_route_first_step": first_step,
        }


def test_run_config_rejects_a_non_boolean_obligation() -> None:
    base = _config(max_active_branches=1, max_model_calls=10, retries=0)
    assert base.intent_ledger_first is False
    with pytest.raises(ValueError, match="intent_ledger_first must be boolean"):
        replace(base, intent_ledger_first=1)  # type: ignore[arg-type]


def _harness(tmp_path: Path, *, intent_ledger_first: bool):
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=0),
        record_version="1.1",
        max_model_calls=None,
        intent_ledger_first=intent_ledger_first,
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


def _revision(label: str, target: str):
    return replace(
        _writer_output(label, WriterDecision.CONTINUE),
        control=WriterControl(
            WriterDecision.REVISE, (), target, "Correct the earlier algebra."
        ),
    )


def _capture(runtime) -> tuple[list[CheckRequest], list[WriterRequest]]:
    checks: list[CheckRequest] = []
    writers: list[WriterRequest] = []

    def check(request: CheckRequest) -> CheckOutput:
        checks.append(request)
        return CheckOutput(
            verdict="ok",
            reason="The deterministic fixture contains no hard defect.",
            evidence=(),
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )

    runtime.check_factory = check
    original = runtime.start_writer

    async def start_writer(request: WriterRequest, session):
        writers.append(request)
        return await original(request, session)

    runtime.start_writer = start_writer  # type: ignore[method-assign]
    return checks, writers


@pytest.mark.parametrize("flag", [False, True])
def test_the_frozen_run_input_reaches_every_writer_and_check_request(
    tmp_path: Path, flag: bool
) -> None:
    orchestrator, record, control, runtime = _harness(
        tmp_path, intent_ledger_first=flag
    )
    checks, writers = _capture(runtime)
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("one", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("two", WriterDecision.COMPLETE),
    }
    try:
        asyncio.run(orchestrator.submit("Test a local mathematical goal"))
        record.verify_complete()
    finally:
        control.close()
    assert writers and checks
    assert {item.intent_ledger_first for item in writers} == {flag}
    assert {item.intent_ledger_first for item in checks} == {flag}


def test_route_first_step_survives_a_revision_and_a_fork(tmp_path: Path) -> None:
    orchestrator, record, control, runtime = _harness(
        tmp_path, intent_ledger_first=True
    )
    checks, _writers = _capture(runtime)
    runtime.writer_outputs = {
        # br_0001 opens with step_0001 and continues with step_0002; revising
        # step_0002 starts br_0002, whose route keeps step_0001 in front.
        ("br_0001", 1): _writer_output("one", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("two", WriterDecision.CONTINUE),
        ("br_0001", 3): _revision("two-fixed", "step_0002"),
        # Revising the opening step starts br_0003 with an empty inherited
        # prefix, so its replacement is a route-opening step again.
        ("br_0002", 3): _revision("one-fixed", "step_0001"),
        ("br_0003", 2): _writer_output("finish", WriterDecision.COMPLETE),
    }
    try:
        asyncio.run(orchestrator.submit("Test a local mathematical goal"))
        state = record.verify_complete()
    finally:
        control.close()

    branches = {item["branch_id"]: item for item in state["branches"]}
    assert branches["br_0002"]["step_revision_ids"][0] == "step_0001"
    assert branches["br_0003"]["step_revision_ids"][0] == "step_0004"
    first_step_flags = {
        item.target.step_revision_id: item.target_is_route_first_step for item in checks
    }
    # step_0001 opens br_0001; step_0003 replaces step_0002 in the middle of
    # br_0002's route; step_0004 replaces step_0001 and therefore opens br_0003.
    assert first_step_flags["step_0001"] is True
    assert first_step_flags["step_0002"] is False
    assert first_step_flags["step_0003"] is False
    assert first_step_flags["step_0004"] is True
