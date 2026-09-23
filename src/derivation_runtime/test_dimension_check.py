"""The switchable dimensional-closure obligation.

Dimensional closure is not otherwise checked by either role; this switchable
obligation makes the Writer state the check and the Checker recompute it.  The
tests care equally about a run with the flag on carrying the contracted
paragraphs and about a run with the flag off rendering prompts byte-identical to
the prompts without the obligation.

The wording is fixed by the prompt contract kept in
``fixtures/prompt_rule_texts_v1.md``, section "Dimensional
closure"; ``test_dimension_paragraphs_match_the_rule_contract`` compares
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
    DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS,
    DIMENSION_CHECK_CHECKER_PARAGRAPH,
    DIMENSION_CHECK_WRITER_DEVELOPER_INSTRUCTIONS,
    DIMENSION_CHECK_WRITER_PARAGRAPH,
    INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
    INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS,
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
            run_id="run-dimension-check",
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
            run_id="run-dimension-check",
            check_id="check-1",
            task_text="Derive the invariant.",
            target=target,
            transcript=(target,),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def test_dimension_paragraphs_match_the_rule_contract() -> None:
    text = RULE_TEXTS.read_text(encoding="utf-8")
    # The contract file holds several sections, so this one is bounded on both
    # sides rather than read to the end of the file.
    section = text[
        text.index("### Dimensional closure") : text.index(
            "## Fork guidance and scope consistency"
        )
    ]
    blocks = re.findall(r"```\n(.*?)\n```", section, re.DOTALL)
    assert len(blocks) == 2, "the dimensional-closure section must supply exactly the Writer and Checker texts"
    writer_block, checker_block = blocks
    # Only the line wrapping may differ; not one word of physics or process.
    assert _flat(DIMENSION_CHECK_WRITER_PARAGRAPH) == _flat(writer_block)
    assert _flat(DIMENSION_CHECK_CHECKER_PARAGRAPH) == _flat(checker_block)


def test_the_obligation_adds_exactly_one_paragraph_to_each_baseline_text() -> None:
    writer = DIMENSION_CHECK_WRITER_DEVELOPER_INSTRUCTIONS
    checker = DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS
    assert writer != WRITER_DEVELOPER_INSTRUCTIONS
    assert checker != CHECKER_DEVELOPER_INSTRUCTIONS
    assert (
        writer.replace(DIMENSION_CHECK_WRITER_PARAGRAPH + "\n", "", 1)
        == WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert (
        checker.replace(DIMENSION_CHECK_CHECKER_PARAGRAPH + "\n", "", 1)
        == CHECKER_DEVELOPER_INSTRUCTIONS
    )
    assert writer_developer_instructions(dimension_check=True) == writer
    assert checker_developer_instructions(dimension_check=True) == checker


def test_the_two_obligations_compose_in_a_fixed_order() -> None:
    # Both splice at the same anchor, so the order has to be decided once in
    # the selector rather than at each call site: two runs with the same flags
    # must not differ by paragraph order alone.
    writer = writer_developer_instructions(
        intent_ledger_first=True, dimension_check=True
    )
    flat = _flat(writer)
    assert flat.index("dependency ledger before returning complete") < flat.index(
        "must be an intent ledger"
    )
    assert flat.index("must be an intent ledger") < flat.index(
        "one explicit dimensional-analysis line"
    )
    assert flat.index("one explicit dimensional-analysis line") < flat.index(
        "When reading_preparation delivers full documents"
    )
    # Each obligation alone still equals its own single-paragraph constant.
    assert (
        writer_developer_instructions(intent_ledger_first=True)
        == INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS
    )
    checker = _flat(
        checker_developer_instructions(intent_ledger_first=True, dimension_check=True)
    )
    assert checker.index("resolves to that slot's current revision") < checker.index(
        "the run declares intent_ledger_first"
    )
    assert checker.index("the run declares intent_ledger_first") < checker.index(
        "the run declares dimension_check"
    )
    assert checker.index("the run declares dimension_check") < checker.index(
        "Regardless of completion intent"
    )
    assert (
        checker_developer_instructions(intent_ledger_first=True)
        == INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS
    )


def test_developer_texts_are_the_untouched_baseline_when_the_flag_is_off() -> None:
    assert writer_developer_instructions(dimension_check=False) == (
        WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert checker_developer_instructions(dimension_check=False) == (
        CHECKER_DEVELOPER_INSTRUCTIONS
    )
    transcript = (_target(),)
    assert rehydrated_writer_instructions(transcript).startswith(
        WRITER_DEVELOPER_INSTRUCTIONS
    )
    assert rehydrated_writer_instructions(transcript, dimension_check=True).startswith(
        DIMENSION_CHECK_WRITER_DEVELOPER_INSTRUCTIONS
    )


def test_rendered_prompts_are_byte_identical_when_the_flag_is_off() -> None:
    assert "dimension_check" not in writer_user_prompt(_writer_request())
    assert "dimension_check" not in checker_user_prompt(_check_request())


def test_rendered_prompts_carry_only_the_new_key_when_the_flag_is_on() -> None:
    writer_off = json.loads(writer_user_prompt(_writer_request()))
    writer_on = json.loads(writer_user_prompt(_writer_request(dimension_check=True)))
    assert writer_on == {**writer_off, "dimension_check": True}

    checker_off = json.loads(checker_user_prompt(_check_request()))
    checker_on = json.loads(checker_user_prompt(_check_request(dimension_check=True)))
    assert checker_on == {**checker_off, "dimension_check": True}


def test_run_config_rejects_a_non_boolean_obligation() -> None:
    base = _config(max_active_branches=1, max_model_calls=10, retries=0)
    assert base.dimension_check is False
    with pytest.raises(ValueError, match="dimension_check must be boolean"):
        replace(base, dimension_check=1)  # type: ignore[arg-type]


def _harness(tmp_path: Path, *, dimension_check: bool):
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=0),
        record_version="1.1",
        max_model_calls=None,
        dimension_check=dimension_check,
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


@pytest.mark.parametrize("flag", [False, True])
def test_the_frozen_run_input_reaches_every_writer_and_check_request(
    tmp_path: Path, flag: bool
) -> None:
    orchestrator, record, control, runtime = _harness(tmp_path, dimension_check=flag)
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
    assert {item.dimension_check for item in writers} == {flag}
    assert {item.dimension_check for item in checks} == {flag}
