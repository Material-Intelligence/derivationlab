"""A Checker quote accepted for whitespace reflow must still replay.

A Checker may quote a sentence fragment that spans a line break of the
registered source (below, the quote ``encloses only the charge $Q r^{3}/R^{3}$,
so the field`` crosses the line break after ``encloses``).  The evidence guard
tolerates whitespace reflow, so such a check passes validation, but Record
replay requires exact containment and would refuse ``check_completed`` with
"literature quote is not in registered source".  The host therefore records
such a quote as the exact source span it copies; the finished Checker call
keeps the provider body.  No model is called.

The last test here pins the interaction with formula-v2: a Writer step whose
content the host normalized before sealing and a Checker quote the host
rewrote before recording occur in one run and replay together.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from derivation_agent_record import replay_events

from .control import ControlStore
from .evidence import (
    _quotes_source,
    _source_span,
    evidence_catalog,
    validate_check_output,
)
from .orchestrator import DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .test_formula_gate_v2 import (
    CORRUPT,
    NORMALIZED,
    WHITELIST,
)
from .test_formula_gate_v2 import (
    SOURCE_ID as METHOD_SOURCE_ID,
)
from .test_formula_gate_v2 import (
    SOURCE_TEXT as METHOD_SOURCE_TEXT,
)
from .test_formula_gate_v2 import output as formula_output
from .types import (
    CheckEvidence,
    CheckOutput,
    CheckRequest,
    EvidenceSource,
    RunPhase,
    RuntimeInvocationError,
    Usage,
    WriterDecision,
)

# A synthetic source paragraph on a textbook topic (the field of a uniformly
# charged sphere). What the tests need from it: the Checker quote crosses the
# line break after "encloses", and the paragraph carries inline TeX.
REFLOW_SOURCE_ID = "src_synthetic_0001"
REFLOW_SOURCE_EXCERPT = (
    "\nInside a uniformly charged sphere of radius $R$, Gauss's law applied to\n"
    "a concentric surface of radius $r<R$, Eq.~(\\ref{eq:gauss}), encloses\n"
    "only the charge $Q r^{3}/R^{3}$, so the field grows linearly with $r$. Outside\n"
    "the sphere the whole of $Q$ is enclosed, and the field falls off as\n"
    "$1/r^{2}$, just as it would for a point charge placed at the centre."
)
REFLOW_CHECKER_QUOTE = "encloses only the charge $Q r^{3}/R^{3}$, so the field"
REFLOW_RECORDED_SPAN = "encloses\nonly the charge $Q r^{3}/R^{3}$, so the field"
REFLOW_EXACT_QUOTE = "the field grows linearly with $r$."

ROOT_HYPOTHESIS = "Explore the declared route."


def test_the_failing_quote_passes_the_guard_but_is_not_an_exact_substring() -> None:
    assert REFLOW_CHECKER_QUOTE not in REFLOW_SOURCE_EXCERPT
    assert _quotes_source(REFLOW_SOURCE_EXCERPT, REFLOW_CHECKER_QUOTE)
    assert _source_span(REFLOW_SOURCE_EXCERPT, REFLOW_CHECKER_QUOTE) == REFLOW_RECORDED_SPAN
    assert REFLOW_RECORDED_SPAN in REFLOW_SOURCE_EXCERPT


@pytest.mark.parametrize(
    "source,quote,span",
    [
        ("alpha\nbeta gamma", "alpha beta", "alpha\nbeta"),
        ("alpha beta gamma", "alpha   beta", "alpha beta"),
        ("x\t=\r\n  y + z", "x = y", "x\t=\r\n  y"),
        # A non-breaking space is whitespace to str.split, so it reflows too.
        ("a\u00a0b c", "a b c", "a\u00a0b c"),
        # A quote may start or end inside a source word, as exact copies can.
        ("the kernel\nis", "ernel is", "ernel\nis"),
        ("  lead\n\n  trail  ", "\n lead trail \n", "lead\n\n  trail"),
        # The first occurrence is used.
        ("p\nq, p  q", "p q", "p\nq"),
    ],
)
def test_a_reflowed_quote_maps_to_the_exact_source_span(source, quote, span) -> None:
    assert _quotes_source(source, quote)
    assert _source_span(source, quote) == span
    assert span in source
    assert " ".join(span.split()) == " ".join(quote.split())


def test_edits_are_still_rejected_and_exact_outputs_are_returned_unchanged() -> None:
    sources = (EvidenceSource("literature_quote", REFLOW_SOURCE_ID, REFLOW_SOURCE_EXCERPT),)

    def output(*quotes: str) -> CheckOutput:
        return CheckOutput(
            "ok",
            "Reviewed.",
            tuple(
                CheckEvidence("literature_quote", REFLOW_SOURCE_ID, quote)
                for quote in quotes
            ),
            "stop",
            Usage({}),
        )

    exact = output(REFLOW_EXACT_QUOTE, "linearly with $r$. Outside\nthe sphere")
    assert validate_check_output(exact, sources) is exact
    reflowed = output(REFLOW_EXACT_QUOTE, REFLOW_CHECKER_QUOTE)
    recorded = validate_check_output(reflowed, sources)
    assert recorded.evidence[0] is reflowed.evidence[0]
    assert [item.quote for item in recorded.evidence] == [
        REFLOW_EXACT_QUOTE,
        REFLOW_RECORDED_SPAN,
    ]
    assert (recorded.verdict, recorded.reason) == (reflowed.verdict, reflowed.reason)
    assert _source_span(REFLOW_SOURCE_EXCERPT, "encloses only the charge $Q r^{2}$") is None
    with pytest.raises(RuntimeInvocationError, match="not an exact substring"):
        validate_check_output(output("encloses only the charge $Q r^{2}/R^{2}$, so"), sources)
    with pytest.raises(RuntimeInvocationError, match="not an exact substring"):
        validate_check_output(output("only the charge $Q r^{3}/R^{3}$ so the field"), sources)


# ---------------------------------------------------------------------------
# End to end on the orchestrator
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = None
    for index, character in enumerate(text):
        if character.isspace():
            if start is not None:
                spans.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _reflow(text: str) -> str:
    """A quote of ``text`` that differs from it only in whitespace.

    Around the first whitespace run that is not a single space the source's
    whitespace is collapsed (a line break becomes a space, as in that run);
    without such a run a single space is widened to two.
    """

    tokens = _tokens(text)
    assert len(tokens) >= 2, text
    for k in range(len(tokens) - 1):
        if text[tokens[k][1] : tokens[k + 1][0]] != " ":
            window = text[
                tokens[max(0, k - 2)][0] : tokens[min(len(tokens) - 1, k + 3)][1]
            ]
            quote = " ".join(window.split())
            if quote not in text:
                return quote
    first, second = tokens[0], tokens[1]
    return text[first[0] : first[1]] + "  " + text[second[0] : second[1]]


KINDS = (
    "literature_quote",
    "ancestor_quote",
    "scope_quote",
    "hypothesis_quote",
    "task_constraint_quote",
)


def _reflowing_checker(provided: list[CheckOutput], source_id: str, quote: str):  # type: ignore[no-untyped-def]
    def review(request: CheckRequest) -> CheckOutput:
        target = request.target.step_revision_id
        evidence = [CheckEvidence("literature_quote", source_id, quote)]
        for kind in KINDS[1:]:
            source = next(
                item
                for item in request.evidence_sources
                if item.kind == kind
                and (
                    kind not in {"ancestor_quote", "scope_quote"}
                    or item.source_id == target
                )
            )
            reflowed = _reflow(source.text)
            assert reflowed not in source.text and _quotes_source(
                source.text, reflowed
            )
            evidence.append(CheckEvidence(kind, source.source_id, reflowed))
        output = CheckOutput(
            verdict="ok",
            reason="The cited spans exist; no defect found.",
            evidence=tuple(evidence),
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )
        provided.append(output)
        return output

    return review


def _orchestrator(
    directory: Path,
    provided: list[CheckOutput],
    *,
    control: ControlStore,
):  # type: ignore[no-untyped-def]
    config = replace(
        _config(max_active_branches=1, max_model_calls=40, retries=1),
        record_version="1.1",
        checker_enabled=True,
    )
    runtime = _runtime()
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("only", WriterDecision.COMPLETE)
    }
    runtime.check_factory = _reflowing_checker(
        provided, REFLOW_SOURCE_ID, REFLOW_CHECKER_QUOTE
    )
    runtime.evidence_sources = lambda: (  # type: ignore[attr-defined]
        EvidenceSource("literature_quote", REFLOW_SOURCE_ID, REFLOW_SOURCE_EXCERPT),
    )
    record = RecordV1Writer(
        EventLogWriter(directory / "events.jsonl", config.run_id), config
    )
    orchestrator = DerivationOrchestrator(
        config=config,
        task_text=TASK_TEXT,
        runtime=runtime,
        record=record,
        control=control,
    )
    return orchestrator, record


def _assert_recorded_as_exact_spans(
    record: RecordV1Writer,
    provided: list[CheckOutput],
    *,
    first_span: str = REFLOW_RECORDED_SPAN,
) -> None:
    canonical = record.verify_complete()
    replay_events(record.events)
    snapshot = record.snapshot()
    checks = [item for item in snapshot["checks"] if item["state"] == "completed"]
    assert len(checks) == len(provided) >= 1
    for check, output in zip(checks, provided):
        assert check["verdict"] == "ok"
        sources = evidence_catalog(
            snapshot, check["target_step_revision_id"], TASK_TEXT
        )
        call = next(
            item
            for item in snapshot["model_calls"]
            if item["model_call_id"] == check["checker_call_id"]
        )
        # The finished call keeps the provider's quotes; the check records spans.
        body = json.loads(call["output_text"])
        assert body["evidence"] == [item.to_record() for item in output.evidence]
        assert [item["kind"] for item in check["evidence"]] == list(KINDS)
        for recorded, given in zip(check["evidence"], output.evidence):
            texts = [
                item.text
                for item in sources
                if (item.kind, item.source_id) == (given.kind, given.source_id)
            ]
            assert recorded["quote"] != given.quote, given.kind
            assert any(recorded["quote"] in text for text in texts), given.kind
            assert " ".join(recorded["quote"].split()) == " ".join(given.quote.split())
        assert check["evidence"][0]["quote"] == first_span
    assert canonical["candidates"]


def test_reflowed_quotes_of_every_kind_are_recorded_as_exact_spans_and_replay(
    tmp_path: Path,
) -> None:
    provided: list[CheckOutput] = []
    control = ControlStore(tmp_path / "control.sqlite")
    try:
        orchestrator, record = _orchestrator(tmp_path, provided, control=control)
        query = asyncio.run(orchestrator.submit(ROOT_HYPOTHESIS))
        assert query.status().phase == RunPhase.REVIEW_READY
        assert all(
            item["state"] == "finished" for item in record.snapshot()["model_calls"]
        )
    finally:
        control.close()
    _assert_recorded_as_exact_spans(record, provided)


def test_crash_repair_records_the_same_spans_from_the_durable_provider_body(
    tmp_path: Path,
) -> None:
    """The failing run's Record stops after the finished Checker call.

    Restart repair re-parses that durable body, which still carries the reflowed
    quote, and must record the same exact spans the live path records.
    """

    provided: list[CheckOutput] = []
    control = ControlStore(tmp_path / "control.sqlite")
    try:
        orchestrator, record = _orchestrator(tmp_path, provided, control=control)
        with (
            patch.object(
                record, "complete_check", side_effect=RuntimeError("simulated crash")
            ),
            pytest.raises(RuntimeError, match="simulated crash"),
        ):
            asyncio.run(orchestrator.submit(ROOT_HYPOTHESIS))
        snapshot = record.snapshot()
        assert snapshot["checks"][0]["state"] == "requested"
        (checker_call,) = [
            item for item in snapshot["model_calls"] if item["role"] == "checker"
        ]
        assert checker_call["state"] == "finished"
        assert (
            REFLOW_CHECKER_QUOTE
            in json.loads(checker_call["output_text"])["evidence"][0]["quote"]
        )
    finally:
        control.close()
    with ControlStore(tmp_path / "control.sqlite") as restored:
        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=orchestrator.runtime,
            record=record,
            control=restored,
        )
        assert restarted._repair_record_prefix()
        asyncio.run(restarted.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        assert restored.run(orchestrator.config.run_id).phase == "review_ready"
    _assert_recorded_as_exact_spans(record, provided)


# ---------------------------------------------------------------------------
# formula-v2 interaction: two host rewrites in one run
# ---------------------------------------------------------------------------


def test_a_normalized_writer_step_and_a_reflowed_quote_coexist_and_replay(
    tmp_path: Path,
) -> None:
    """Both host rewrites happen in one run, each before its own Record entry.

    The Writer's raw output is sealed only after ``writer_output_normalized``
    carries its edits, and the Checker's quotes are rewritten only into
    ``check_completed``; both finished model calls keep the provider's bytes.
    The Checker quotes the *normalized* step, which is the content the host
    showed it, so the two rewrites compose instead of fighting.
    """

    method_quote = _reflow(METHOD_SOURCE_TEXT)
    assert method_quote not in METHOD_SOURCE_TEXT
    config = replace(
        _config(max_active_branches=1, max_model_calls=40, retries=1),
        record_version="1.1",
        checker_enabled=True,
        formula_validation_policy="formula-v2",
    )
    provided: list[CheckOutput] = []
    runtime = _runtime()
    runtime.writer_outputs = {("br_0001", 1): formula_output(CORRUPT)}
    runtime.check_factory = _reflowing_checker(
        provided, METHOD_SOURCE_ID, method_quote
    )
    runtime.evidence_sources = lambda: (  # type: ignore[attr-defined]
        EvidenceSource("literature_quote", METHOD_SOURCE_ID, METHOD_SOURCE_TEXT),
    )
    record = RecordV1Writer(
        EventLogWriter(tmp_path / "events.jsonl", config.run_id), config
    )
    with ControlStore(tmp_path / "control.sqlite") as control:
        orchestrator = DerivationOrchestrator(
            config=config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=control,
            formula_engine_whitelist=WHITELIST,
        )
        query = asyncio.run(orchestrator.submit(ROOT_HYPOTHESIS))
        assert query.status().phase == RunPhase.REVIEW_READY

    types = [event["type"] for event in record.events]
    assert types.index("writer_output_normalized") < types.index("step_revision_sealed")
    assert types.index("model_call_finished") < types.index("writer_output_normalized")
    assert types.index("step_revision_sealed") < types.index("check_completed")

    snapshot = record.snapshot()
    (step,) = snapshot["step_revisions"]
    # The Writer's raw body is the provider's; the sealed step is normalized.
    (writer_call,) = [
        item for item in snapshot["model_calls"] if item["role"] == "writer"
    ]
    assert json.loads(writer_call["output_text"])["derivation"] == CORRUPT
    assert writer_call["normalized_output"]["raw_output_sha256"] == (
        writer_call["output_sha256"]
    )
    assert writer_call["normalized_output"]["output_sha256"] == step["output_sha256"]
    assert step["content"]["derivation"] == NORMALIZED
    # The Checker's raw body keeps the reflowed quote; the check records spans.
    (checker_call,) = [
        item for item in snapshot["model_calls"] if item["role"] == "checker"
    ]
    assert (
        json.loads(checker_call["output_text"])["evidence"][0]["quote"] == method_quote
    )
    (check,) = snapshot["checks"]
    assert [item["kind"] for item in check["evidence"]] == list(KINDS)
    ancestor = next(
        item for item in check["evidence"] if item["kind"] == "ancestor_quote"
    )
    # The quote the Checker was shown, and the span recorded for it, live in the
    # normalized step, not in the raw output the provider returned.
    assert ancestor["quote"] in "\n\n".join(
        f"{field}:\n{step['content'][field]}"
        for field in ("claim", "why", "source", "derivation", "scope")
    )
    _assert_recorded_as_exact_spans(
        record,
        provided,
        first_span=_source_span(METHOD_SOURCE_TEXT, method_quote),
    )
