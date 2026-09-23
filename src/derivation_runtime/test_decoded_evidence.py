"""Regression for a Checker source-quote serialization failure.

The fixture is written by hand in the shape of a failure first met live;
nothing in it was captured from a run (its ``provenance`` field says so). The
Checker quoted the decoded step text, while the runtime compared it with the
JSON-encoded step.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from derivation_agent_record import canonical_json, sha256_text
from derivation_agent_record.model import ancestor_evidence_text

from .app_server_runtime import CodexAppServerRuntime, _preparation_documents
from .control import ControlStore
from .evidence import evidence_catalog, validate_check_output
from .orchestrator import DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import _config, _runtime
from .types import (
    CheckEvidence,
    CheckOutput,
    ContentRef,
    EvidenceSource,
    RuntimeInvariantError,
    RuntimeInvocationError,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)


def fixture():
    return json.loads(
        (
            Path(__file__).with_name("fixtures") / "checker_decoded_quote_failure.json"
        ).read_text()
    )


def test_failed_checker_payload_shape_passes_decoded_runtime_and_replay(tmp_path):
    data = fixture()
    assert data["provenance"].startswith("synthetic:")
    assert sha256_text(data["task_text"]) == data["task_sha256"]
    content = data["step"]["content"]
    assert sha256_text(canonical_json(content)) == data["step"]["output_sha256"]
    payload = json.loads(data["checker_raw_output"])
    config = replace(
        _config(max_active_branches=1, max_model_calls=10, retries=0),
        record_version="1.1",
        task=ContentRef(data["task_id"], data["task_sha256"]),
    )
    runtime = _runtime()
    runtime.evidence_sources = lambda: (
        EvidenceSource(
            "literature_quote",
            data["literature_excerpt"]["source_id"],
            data["literature_excerpt"]["text"],
        ),
    )
    runtime.writer_outputs = {
        ("br_0001", 1): WriterOutput(
            StepContent(**content),
            WriterControl(WriterDecision.COMPLETE, ()),
            "stop",
            Usage({}),
        )
    }
    real_output = CheckOutput(
        payload["verdict"],
        payload["reason"],
        tuple(CheckEvidence(**item) for item in payload["evidence"]),
        "stop",
        Usage({}),
        raw_output=data["checker_raw_output"],
    )
    runtime.check_factory = lambda request: real_output
    record = RecordV1Writer(
        EventLogWriter(tmp_path / "events.jsonl", config.run_id), config
    )
    control = ControlStore(tmp_path / "control.sqlite")
    orchestrator = DerivationOrchestrator(
        config=config,
        task_text=data["task_text"],
        runtime=runtime,
        record=record,
        control=control,
    )
    try:
        asyncio.run(
            orchestrator.submit("Regression using a hand-written Checker payload.")
        )
        state = record.verify_complete()
        assert state["checks"][0]["verdict"] == "ok"
        assert state["candidates"][0]["status"] == "eligible"
        assert all(call["state"] == "finished" for call in state["model_calls"])
        sources = evidence_catalog(state, "step_0001", data["task_text"])
        validate_check_output(real_output, sources)
        old_sources = tuple(
            replace(source, text=canonical_json(content))
            if source.kind == "ancestor_quote"
            else source
            for source in sources
        )
        failing_kinds = [
            evidence.kind
            for evidence in real_output.evidence
            if evidence.quote
            not in next(
                source.text
                for source in old_sources
                if (source.kind, source.source_id)
                == (evidence.kind, evidence.source_id)
            )
        ]
        assert failing_kinds == ["ancestor_quote"]
        with pytest.raises(RuntimeInvocationError, match="not an exact substring"):
            validate_check_output(real_output, old_sources)
        mutated = replace(
            real_output,
            evidence=(
                replace(
                    real_output.evidence[2],
                    quote=real_output.evidence[2].quote.replace("3*a**2*b", "2*a**2*b"),
                ),
            ),
        )
        with pytest.raises(RuntimeInvocationError, match="not an exact substring"):
            validate_check_output(mutated, sources)
    finally:
        control.close()


@pytest.mark.parametrize("field", ["claim", "why", "source", "derivation", "scope"])
def test_readable_fields_preserve_quotes_backslashes_and_newlines(field):
    content = {key: key for key in ("claim", "why", "source", "derivation", "scope")}
    exact = 'α says "x"; \\partial_x f\nsecond line'
    content[field] = exact
    assert exact in ancestor_evidence_text(content, record_version="1.1")
    assert exact not in ancestor_evidence_text(content, record_version="1.0")
    assert ancestor_evidence_text(content, record_version="1.0") == canonical_json(
        content
    )


def test_writer_parser_restores_tex_commands_decoded_as_json_controls():
    content = {key: key for key in ("claim", "why", "source", "derivation", "scope")}
    content["derivation"] = "sum over {\x08f k}; use \x0crac{a}{b}"
    parsed = CodexAppServerRuntime._parse_step_content(content)
    assert parsed.derivation == r"sum over {\bf k}; use \frac{a}{b}"


def test_compact_or_reader_preparation_audits_null_as_no_full_documents():
    assert _preparation_documents({"documents": None}) == []
    with pytest.raises(RuntimeInvariantError, match="documents are invalid"):
        _preparation_documents({"documents": "not-a-list"})


def test_whitespace_reflow_is_tolerated_but_edits_are_not() -> None:
    """A reflowed quote is still a copy; anything else is still rejected.

    A checker draft rejected here loses its verdict and its reasoning, which
    survive only in the failed call's partial output, so discarding a whole
    substantive check over a line break costs real evidence.  Everything that
    is not whitespace must still match contiguously.
    """

    from .evidence import _quotes_source

    source = (
        "The ground-state energy is\n  E_0=\\frac{\\hbar\\omega}{2}(2n+1), taken at\nn = 0 here."
    )
    assert _quotes_source(source, "E_0=\\frac{\\hbar\\omega}{2}(2n+1), taken at")
    assert _quotes_source(
        source,
        "The ground-state energy is E_0=\\frac{\\hbar\\omega}{2}(2n+1), taken at n = 0 here.",
    )
    assert _quotes_source(source, "energy is\nE_0=\\frac{\\hbar\\omega}{2}(2n+1)")
    # A rewritten formula, a paraphrase and an invented span all stay rejected.
    assert not _quotes_source(source, "E_0=\\frac{\\hbar\\omega}{2}(2n-1)")
    assert not _quotes_source(source, "The ground-state energy is taken at n = 0 here.")
    assert not _quotes_source(source, "The ground-state energy is well known.")


def test_completion_requirements_are_quotable_sources() -> None:
    """The checker is shown the completion requirements, so it may quote them.

    While they were absent from the catalog, a checker that copied one verbatim
    was failed as invalid output and its entire verdict, evidence and reasoning
    were discarded with it.
    """

    from .evidence import evidence_catalog

    snapshot = {
        "schema_version": "derivation-agent-canonical-v1.1",
        "run": {"task": {"id": "task_run_1"}},
        "step_revisions": [
            {
                "step_revision_id": "step_0001",
                "branch_id": "br_0001",
                "content": {
                    "claim": "c",
                    "why": "w",
                    "source": "s",
                    "derivation": "d",
                    "scope": "sc",
                },
            }
        ],
        "branches": [
            {
                "branch_id": "br_0001",
                "step_revision_ids": ["step_0001"],
                "parent_branch_id": None,
                "hypothesis": {"text": "h"},
            }
        ],
        "source_evidence": [],
    }
    requirements = ("State every boundary condition.", "Give the ground-state energy.")
    catalog = evidence_catalog(snapshot, "step_0001", "task text", requirements)
    texts = [item.text for item in catalog if item.kind == "task_constraint_quote"]
    ids = {item.source_id for item in catalog if item.kind == "task_constraint_quote"}
    # The record requires task evidence to carry the run's task id, so every
    # requirement shares it and appears as its own text under that id.
    assert ids == {"task_run_1"}
    assert texts == ["task text", *requirements]
    # Kept apart, so no quote can match by spanning two of them.
    assert not any("State every boundary condition. Give" in text for text in texts)
    # Omitting them keeps the previous catalog exactly.
    assert [
        item.text
        for item in evidence_catalog(snapshot, "step_0001", "task text")
        if item.kind == "task_constraint_quote"
    ] == ["task text"]
