"""Route references resolve by slot, on the host side.

A step is sealed with an identifier that did not exist while it was being
written, so a ledger entry inside the last step of a route cannot name that
step, and after a revision it cannot name the step it replaced either without
pointing at a revision that is no longer on the route.  Left alone, the
Checker reads a self-reference to a superseded revision as an invalid route
reference, the Writer spends its remaining repair budget on a one-token rename,
and the branch ends up declared blocked.

The fix has three parts and none of them is switchable: the
Writer is told to cite a route position, the host hands the Checker the slot
and the superseded revisions of every route step, and the Checker is told that
such a reference resolves rather than counting as a defect.
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
    checker_user_prompt,
    writer_user_prompt,
)
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import (
    CheckOutput,
    CheckRequest,
    RunConfig,
    StepContent,
    StepSnapshot,
    Usage,
    WriterControl,
    WriterDecision,
    WriterRequest,
)


def _content(label: str) -> StepContent:
    return StepContent(
        claim=f"Claim {label}",
        why=f"Why {label}",
        source=f"Source {label}",
        derivation=f"Derivation {label}",
        scope=f"Scope {label}",
    )


def _snapshot(step_revision_id: str, slot: int, superseded: tuple[str, ...]):
    return StepSnapshot(
        step_revision_id=step_revision_id,
        content=_content(step_revision_id),
        step_slot=slot,
        superseded_step_revision_ids=superseded,
    )


def test_both_role_catalogs_carry_the_slot_and_its_superseded_revisions() -> None:
    route = (
        _snapshot("step_0001", 1, ()),
        _snapshot("step_0006", 2, ("step_0005", "step_0004")),
    )
    writer = json.loads(
        writer_user_prompt(
            WriterRequest(
                run_id="run-slots",
                branch_id="branch-1",
                step_slot=3,
                task_text="Derive the invariant.",
                hypothesis="Use the declared route.",
                transcript=route,
            )
        )
    )
    checker = json.loads(
        checker_user_prompt(
            CheckRequest(
                run_id="run-slots",
                check_id="check-1",
                task_text="Derive the invariant.",
                target=route[-1],
                transcript=route,
            )
        )
    )
    expected = [
        {
            "step_revision_id": "step_0001",
            "step_slot": 1,
            "superseded_step_revision_ids": [],
        },
        {
            "step_revision_id": "step_0006",
            "step_slot": 2,
            "superseded_step_revision_ids": ["step_0005", "step_0004"],
        },
    ]
    assert [
        {key: item[key] for key in expected[0]} for item in writer["transcript_catalog"]
    ] == expected
    # The Checker's catalog is lineage only; the step content is already in its
    # transcript, and duplicating it would only invite quoting the copy.
    assert checker["transcript_catalog"] == expected
    assert "step_slot" in writer["history_read_rule"]


def _harness(
    tmp_path: Path,
) -> tuple[DerivationOrchestrator, RecordV1Writer, ControlStore, object]:
    config: RunConfig = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=0),
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


def _revision(label: str, target: str):
    return replace(
        _writer_output(label, WriterDecision.CONTINUE),
        control=WriterControl(
            WriterDecision.REVISE, (), target, "Correct the earlier algebra."
        ),
    )


def test_the_host_walks_the_replacement_chain_of_a_twice_revised_slot(
    tmp_path: Path,
) -> None:
    orchestrator, record, control, runtime = _harness(tmp_path)
    checks: list[CheckRequest] = []

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
    runtime.writer_outputs = {
        # br_0001 writes slots 1 and 2, then revises slot 2 twice. Each
        # revision seals a new step in the same slot on a new branch.
        ("br_0001", 1): _writer_output("one", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output("two", WriterDecision.CONTINUE),
        ("br_0001", 3): _revision("two-fixed", "step_0002"),
        ("br_0002", 3): _revision("two-fixed-again", "step_0003"),
        ("br_0003", 3): _writer_output("finish", WriterDecision.COMPLETE),
    }
    try:
        asyncio.run(orchestrator.submit("Test a local mathematical goal"))
        state = record.verify_complete()
    finally:
        control.close()

    branches = {item["branch_id"]: item for item in state["branches"]}
    assert branches["br_0003"]["step_revision_ids"] == [
        "step_0001",
        "step_0004",
        "step_0005",
    ]
    lineage = {
        item.target.step_revision_id: (
            item.target.step_slot,
            item.target.superseded_step_revision_ids,
        )
        for item in checks
    }
    assert lineage["step_0001"] == (1, ())
    assert lineage["step_0002"] == (2, ())
    assert lineage["step_0003"] == (2, ("step_0002",))
    # Two revisions deep: both earlier revisions of slot 2 are offered, newest
    # first, so a reference to either resolves to step_0004.
    assert lineage["step_0004"] == (2, ("step_0003", "step_0002"))
    final = next(item for item in checks if item.target.step_revision_id == "step_0005")
    catalog = json.loads(checker_user_prompt(final))["transcript_catalog"]
    assert catalog == [
        {
            "step_revision_id": "step_0001",
            "step_slot": 1,
            "superseded_step_revision_ids": [],
        },
        {
            "step_revision_id": "step_0004",
            "step_slot": 2,
            "superseded_step_revision_ids": ["step_0003", "step_0002"],
        },
        {
            "step_revision_id": "step_0005",
            "step_slot": 3,
            "superseded_step_revision_ids": [],
        },
    ]


def test_the_repair_budget_is_three_per_lineage() -> None:
    config = _config(max_active_branches=1, max_model_calls=10, retries=0)
    assert config.max_local_repairs == 3
    assert (
        WriterRequest(
            run_id="run-slots",
            branch_id="branch-1",
            step_slot=1,
            task_text="Derive the invariant.",
            hypothesis="Use the declared route.",
            transcript=(),
        ).max_local_repairs
        == 3
    )


def test_the_slot_texts_match_the_rule_contract() -> None:
    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    rule_texts = (
        Path(__file__).resolve().parent / "fixtures" / "prompt_rule_texts_v1.md"
    )
    text = rule_texts.read_text(encoding="utf-8")
    section = text[
        text.index("#### Closure requirement 2") : text.index("### Dimensional closure")
    ]
    blocks = re.findall(r"```\n(.*?)\n```", section, re.DOTALL)
    assert len(blocks) == 2, "the step-reference section must supply the ledger gate and the Checker rule"
    ledger_block, slot_block = blocks

    def flat(value: str) -> str:
        return " ".join(value.split())

    assert flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[1]) == flat(ledger_block)
    assert flat(slot_block) in flat(CHECKER_DEVELOPER_INSTRUCTIONS)
