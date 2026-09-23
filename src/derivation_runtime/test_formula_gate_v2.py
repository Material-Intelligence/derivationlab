"""formula-v2 on the fake runtime: normalization recorded in Record 1.1,
separate format-rewrite budget, sealing with format issues instead of pausing,
crash recovery, and replay verification of ``writer_output_normalized``."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

from derivation_agent_record import (
    ContractError,
    canonical_json,
    compute_event_sha256,
    replay_events,
    sha256_text,
)

from .control import ControlStore
from .formula_validation import load_engine_whitelist
from .orchestrator import FORMULA_V2_MAX_FORMAT_REWRITES, DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime
from .types import (
    EvidenceSource,
    RunPhase,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)

# A synthetic vocabulary that describes no real engine: it is deliberately
# loaded without the preamble/platform check the committed file must pass.
WHITELIST = load_engine_whitelist(
    Path(__file__).with_name("fixtures") / "formula_engine_whitelist_fixture.json",
    verify=False,
)
SCHEMAS = Path(__file__).parents[1] / "derivation_agent_record/schemas"
SOURCE_ID = "src_method"
SOURCE_TEXT = r"""\def\la{\lambda}
\def\xv{{\bf x}}
\def\Bv{{\bm B}}
The propagator reads G(\xv,\la) = \sum_n \frac{f_n}{\la - E_n(\xv)} ."""


def output(
    derivation: str,
    decision: WriterDecision = WriterDecision.COMPLETE,
    *,
    source: str = "Toy source quoting the frozen method.",
    revises: str | None = None,
) -> WriterOutput:
    return WriterOutput(
        StepContent(
            claim="Claim for the toy response.",
            why="Reason for the toy response.",
            source=source,
            derivation=derivation,
            scope=f"Toy scope; support_refs: {SOURCE_ID}:4-4",
        ),
        WriterControl(
            decision, (), revises, "Correct the algebra." if revises else None
        ),
        "stop",
        Usage({}),
    )


CORRUPT = "$\x0bomega = \x07omega_0 + \\la$ and \x7f\\(\\xv\\)"
NORMALIZED = "$\\omega = \\omega_0 + \\lambda$ and \\({\\bf x}\\)"


def harness(tmp_path, *, checker=False, record_version="1.1", outputs=None):
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=1),
        record_version=record_version,
        checker_enabled=checker if record_version == "1.1" else True,
        formula_validation_policy="formula-v2",
    )
    runtime = _runtime()
    runtime.evidence_sources = lambda: (
        EvidenceSource("literature_quote", SOURCE_ID, SOURCE_TEXT),
    )
    runtime.writer_outputs = dict(outputs or {("br_0001", 1): output(CORRUPT)})
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
        formula_engine_whitelist=WHITELIST,
    )
    asyncio.run(orchestrator.initialize("root"))
    return orchestrator, record, control, runtime


def assert_schemas(record) -> dict:
    event_schema = json.loads((SCHEMAS / "event-v1.1.schema.json").read_text())
    canonical_schema = json.loads((SCHEMAS / "canonical-v1.1.schema.json").read_text())
    for event in record.events:
        jsonschema.validate(event, event_schema)
    state = record.verify_complete()
    jsonschema.validate(state, canonical_schema)
    return state


def writer_requests(runtime):
    return [
        item.request
        for item in runtime._invocations.values()
        if item.invocation.role.value == "writer"
    ]


def types(record):
    return [event["type"] for event in record.events]


# --------------------------------------------------------------------------- gate


@pytest.mark.parametrize("checker", [False, True])
def test_control_char_output_is_normalized_recorded_and_sealed(tmp_path, checker):
    o, record, control, runtime = harness(tmp_path, checker=checker)
    try:
        asyncio.run(o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        state = assert_schemas(record)
        assert control.run(o.config.run_id).phase == RunPhase.REVIEW_READY.value
        assert len(state["model_calls"]) == (2 if checker else 1)
        call = state["model_calls"][0]
        raw = json.loads(call["output_text"])
        assert raw["derivation"] == CORRUPT
        step = state["step_revisions"][0]
        assert step["content"]["derivation"] == NORMALIZED
        assert call["normalized_output"]["output_sha256"] == step["output_sha256"]
        assert call["normalized_output"]["raw_output_sha256"] == call["output_sha256"]
        sequence = types(record)
        finished = sequence.index("model_call_finished")
        normalized = sequence.index("writer_output_normalized")
        assert finished < normalized < sequence.index("step_revision_sealed")
        event = record.events[normalized]
        assert event["actor"] == {"kind": "system", "id": "record-runtime"}
        assert event["payload"]["policy"] == "formula-v2"
        assert event["payload"]["normalizer_version"] == "formula-normalization-v1"
        assert [r["kind"] for r in event["payload"]["replacements"]] == [
            "del_removed",
            "control_char_backslash",
            "control_char_backslash",
            "macro_expansion",
            "macro_expansion",
        ]
        (audit,) = control.formula_audits(o.config.run_id).values()
        assert audit["disposition"] == "accepted"
        assert audit["output_sha256"] == call["output_sha256"]
        assert audit["normalized_output_sha256"] == step["output_sha256"]
        if checker:
            checks = [
                item.request
                for item in runtime._invocations.values()
                if item.invocation.role.value == "checker"
            ]
            assert checks[0].target.content.derivation == NORMALIZED
            assert state["checks"][0]["target_output_sha256"] == step["output_sha256"]
    finally:
        control.close()


def test_residual_error_gets_two_rewrites_then_seals_with_format_issues(tmp_path):
    bad = output("$x = \\zorp + \x05omega$")
    o, record, control, runtime = harness(tmp_path, outputs={("br_0001", 1): bad})
    try:
        asyncio.run(o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        run = control.run(o.config.run_id)
        assert run.phase == RunPhase.REVIEW_READY.value
        assert run.stop_reason != "formula_validation_failed"
        audits = list(control.formula_audits(o.config.run_id).values())
        assert [a["disposition"] for a in audits] == [
            "rejected",
            "rejected",
            "accepted_with_format_issues",
        ]
        assert [a["format_rewrites_used"] for a in audits] == [0, 1, 2]
        assert FORMULA_V2_MAX_FORMAT_REWRITES == 2
        requests = writer_requests(runtime)
        assert len(requests) == 3
        assert requests[0].formula_feedback == ()
        for request in requests[1:]:
            assert request.formula_feedback
            assert all(i["severity"] == "error" for i in request.formula_feedback)
            assert "zorp" in request.formula_feedback[0]["excerpt"]
        state = assert_schemas(record)
        (step,) = state["step_revisions"]
        assert step["content"]["derivation"] == "$x = \\zorp + \\omega$"
        assert (
            step["origin"]["model_call_id"] == state["model_calls"][-1]["model_call_id"]
        )
        assert types(record).count("writer_output_normalized") == 1
        assert any(
            i["severity"] == "error" and "zorp" in i["excerpt"]
            for i in audits[-1]["issues"]
        )
    finally:
        control.close()


def test_corrected_rewrite_is_accepted_and_seals_the_new_call(tmp_path):
    o, record, control, runtime = harness(
        tmp_path, outputs={("br_0001", 1): output("$\\zorp$")}
    )
    try:
        asyncio.run(o._run_writer("br_0001"))
        assert not record.snapshot()["step_revisions"]
        assert control.branch(o.config.run_id, "br_0001").last_error == (
            "Formula format correction required"
        )
        runtime.writer_outputs[("br_0001", 1)] = output("$\\mu$")
        asyncio.run(o._run_writer("br_0001"))
        state = record.snapshot()
        assert state["step_revisions"][0]["content"]["derivation"] == "$\\mu$"
        assert "writer_output_normalized" not in types(record)
        assert not o._repair_record_prefix()
    finally:
        control.close()


def test_source_quotation_warnings_do_not_rewrite_and_are_not_expanded(tmp_path):
    source = "Quoted: $G(\\xv,\\la) = \\frac{1}{\\la - E(\\xv)}$ and $\\Bv$"
    o, record, control, _ = harness(
        tmp_path, outputs={("br_0001", 1): output("$\\omega$", source=source)}
    )
    try:
        asyncio.run(o._run_writer("br_0001"))
        state = record.snapshot()
        assert state["step_revisions"][0]["content"]["source"] == source
        (audit,) = control.formula_audits(o.config.run_id).values()
        assert audit["disposition"] == "accepted"
        assert {i["severity"] for i in audit["issues"]} == {"warning"}
        assert all(i["quotation"] == "source_field" for i in audit["issues"])
    finally:
        control.close()


def test_record_1_0_validates_raw_output_without_normalization_event(tmp_path):
    o, record, control, _ = harness(tmp_path, record_version="1.0")
    try:
        for _ in range(3):
            asyncio.run(o._run_writer("br_0001"))
        state = record.verify_complete()
        assert "writer_output_normalized" not in types(record)
        assert state["step_revisions"][0]["content"]["derivation"] == CORRUPT
        assert [
            a["disposition"] for a in control.formula_audits(o.config.run_id).values()
        ] == ["rejected", "rejected", "accepted_with_format_issues"]
        assert control.run(o.config.run_id).stop_reason != "formula_validation_failed"
    finally:
        control.close()


# --------------------------------------------------------------------------- recovery


def test_crash_before_normalization_event_repairs_without_model_call(tmp_path):
    o, record, control, _ = harness(tmp_path)
    try:
        with (
            patch.object(
                record, "normalize_writer_output", side_effect=RuntimeError("crash")
            ),
            pytest.raises(RuntimeError, match="crash"),
        ):
            asyncio.run(o._run_writer("br_0001"))
        assert len(control.formula_audits(o.config.run_id)) == 1
        assert o._repair_record_prefix()
        state = record.snapshot()
        assert len(state["model_calls"]) == 1
        assert state["step_revisions"][0]["content"]["derivation"] == NORMALIZED
        assert types(record).count("writer_output_normalized") == 1
        assert not o._repair_record_prefix()
    finally:
        control.close()


def test_crash_after_normalization_event_seals_once(tmp_path):
    o, record, control, _ = harness(tmp_path)
    try:
        with (
            patch.object(record, "seal_model_step", side_effect=RuntimeError("crash")),
            pytest.raises(RuntimeError, match="crash"),
        ):
            asyncio.run(o._run_writer("br_0001"))
        assert types(record)[-1] == "writer_output_normalized"
        restarted = DerivationOrchestrator(
            config=o.config,
            task_text=TASK_TEXT,
            runtime=o.runtime,
            record=record,
            control=control,
            formula_engine_whitelist=WHITELIST,
        )
        assert restarted._repair_record_prefix()
        assert types(record).count("writer_output_normalized") == 1
        assert (
            record.snapshot()["step_revisions"][0]["content"]["derivation"]
            == NORMALIZED
        )
    finally:
        control.close()


def restart_with(o, record, control, whitelist):
    return DerivationOrchestrator(
        config=o.config,
        task_text=TASK_TEXT,
        runtime=o.runtime,
        record=record,
        control=control,
        formula_engine_whitelist=whitelist,
    )


def test_changed_normalizer_cannot_silently_reinterpret_an_audit(tmp_path):
    o, record, control, _ = harness(tmp_path)
    try:
        with (
            patch.object(
                record, "normalize_writer_output", side_effect=RuntimeError("crash")
            ),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(o._run_writer("br_0001"))
        narrower = replace(WHITELIST, commands=WHITELIST.commands - {"omega"})
        with pytest.raises(Exception, match="does not reproduce"):
            restart_with(o, record, control, narrower)._repair_record_prefix()
        assert not record.snapshot()["step_revisions"]
    finally:
        control.close()


def test_recorded_normalization_is_sealed_even_after_normalizer_change(tmp_path):
    o, record, control, _ = harness(tmp_path)
    try:
        with (
            patch.object(record, "seal_model_step", side_effect=RuntimeError("crash")),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(o._run_writer("br_0001"))
        narrower = replace(WHITELIST, commands=WHITELIST.commands - {"omega"})
        assert restart_with(o, record, control, narrower)._repair_record_prefix()
        state = record.verify_complete()
        assert state["step_revisions"][0]["content"]["derivation"] == NORMALIZED
    finally:
        control.close()


def test_rejected_output_is_not_renormalized_on_restart(tmp_path):
    o, record, control, _ = harness(
        tmp_path, outputs={("br_0001", 1): output("$\\zorp$")}
    )
    try:
        asyncio.run(o._run_writer("br_0001"))
        broken = restart_with(o, record, control, WHITELIST)
        with patch.object(
            broken, "_formula_v2_normalization", side_effect=AssertionError
        ):
            assert not broken._repair_record_prefix()
        assert not record.snapshot()["step_revisions"]
    finally:
        control.close()


# --------------------------------------------------------------------------- revisions


def test_revision_and_unchanged_completion_use_normalized_hash(tmp_path):
    outputs = {
        ("br_0001", 1): output("$\\alpha$", WriterDecision.CONTINUE),
        ("br_0001", 2): output(CORRUPT, WriterDecision.REVISE, revises="step_0001"),
        ("br_0002", 2): output(CORRUPT, WriterDecision.COMPLETE),
    }
    o, record, control, _ = harness(tmp_path, outputs=outputs)
    try:
        asyncio.run(o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        state = assert_schemas(record)
        sequence = types(record)
        assert "model_revision_applied" in sequence
        assert "writer_route_completion" in sequence
        assert sequence.count("writer_output_normalized") == 2
        revised = next(s for s in state["step_revisions"] if s["revision"] == 2)
        assert revised["content"]["derivation"] == NORMALIZED
        completion = next(
            e for e in record.events if e["type"] == "writer_route_completion"
        )
        call = next(
            c
            for c in state["model_calls"]
            if c["model_call_id"] == completion["payload"]["model_call_id"]
        )
        assert call["output_sha256"] != revised["output_sha256"]
        assert call["normalized_output"]["output_sha256"] == revised["output_sha256"]
        assert state["candidates"]
    finally:
        control.close()


# --------------------------------------------------------------------------- replay


def rehash(events):
    previous = None
    for seq, event in enumerate(events, start=1):
        event["seq"] = seq
        event["event_id"] = f"evt_{seq:08d}"
        event["prev_event_sha256"] = previous
        event["event_sha256"] = compute_event_sha256(event)
        previous = event["event_sha256"]
    return events


@pytest.fixture
def sealed_events(tmp_path):
    o, record, control, _ = harness(tmp_path)
    try:
        asyncio.run(o._run_writer("br_0001"))
        events = record.events
    finally:
        control.close()
    replay_events(events)
    return events


def normalization_index(events):
    return next(
        i for i, e in enumerate(events) if e["type"] == "writer_output_normalized"
    )


def tampered(events, mutate):
    copied = copy.deepcopy(events)
    mutate(copied, normalization_index(copied))
    return rehash(copied)


def set_payload(key, value):
    def mutate(events, index):
        events[index]["payload"][key] = value

    return mutate


def set_replacement(position, key, value):
    def mutate(events, index):
        events[index]["payload"]["replacements"][position][key] = value

    return mutate


def raise_content_consistently(events, index):
    payload = events[index]["payload"]
    payload["content"]["claim"] = "A different claim."
    payload["output_sha256"] = sha256_text(canonical_json(payload["content"]))


@pytest.mark.parametrize(
    "mutate,message",
    [
        (set_payload("raw_output_sha256", "0" * 64), "raw hash differs"),
        (set_payload("output_sha256", "0" * 64), "normalized content hash mismatch"),
        (raise_content_consistently, "do not reproduce normalized content"),
        (set_payload("replacements", []), "non-empty replacement list"),
        (set_payload("policy", ""), "normalization policy"),
        (set_replacement(0, "original", "x"), "original does not match"),
        (set_replacement(0, "kind", "guess"), "unsupported replacement kind"),
        (set_replacement(1, "replacement", "\\\\"), "invalid control restoration"),
        (set_replacement(0, "source_id", SOURCE_ID), "cannot cite a source"),
        (set_replacement(3, "source_id", "src_unknown"), "source is not registered"),
        (set_replacement(3, "source_line", 99), "invalid source line"),
        (set_replacement(3, "field", "source"), "never macro-expanded"),
        (set_replacement(0, "start", -1), "offsets outside field"),
        # A forged expansion: the cited definition is real, the replacement is
        # not what it produces. Sealed scientific content, rewritten by the
        # host, would otherwise replay clean.
        (
            set_replacement(3, "replacement", r"{\bf p}_{\rm forged}"),
            "not to",
        ),
        (set_replacement(3, "replacement", r"{\bf x}{\bf x}"), "not to"),
        # A real definition, but not of this macro: line 1 defines \la.
        (set_replacement(3, "source_line", 1), "does not define"),
    ],
)
def test_replay_rejects_tampered_normalization(sealed_events, mutate, message):
    with pytest.raises(ContractError, match=message):
        replay_events(tampered(sealed_events, mutate))


def test_replay_requires_the_normalization_event_for_a_normalized_seal(sealed_events):
    def drop(events, index):
        del events[index]

    with pytest.raises(ContractError, match="differs from sealed step"):
        replay_events(tampered(sealed_events, drop))


def test_replay_rejects_duplicate_or_late_normalization(sealed_events):
    def duplicate(events, index):
        events.insert(index + 1, copy.deepcopy(events[index]))

    with pytest.raises(ContractError, match="already normalized"):
        replay_events(tampered(sealed_events, duplicate))

    def move_after_seal(events, index):
        event = events.pop(index)
        seal = next(
            i for i, e in enumerate(events) if e["type"] == "step_revision_sealed"
        )
        events.insert(seal + 1, event)

    with pytest.raises(ContractError):
        replay_events(tampered(sealed_events, move_after_seal))


def test_replay_rejects_normalization_in_record_1_0(sealed_events):
    def downgrade(events, index):
        for event in events:
            event["schema_version"] = "derivation-agent-event-v1"

    with pytest.raises(ContractError):
        replay_events(tampered(sealed_events, downgrade))


def test_records_without_normalization_keep_raw_hash_semantics(tmp_path):
    o, record, control, _ = harness(
        tmp_path, outputs={("br_0001", 1): output("$\\alpha$")}
    )
    try:
        asyncio.run(o._run_writer("br_0001"))
        state = record.verify_complete()
        call = state["model_calls"][0]
        assert "normalized_output" not in call
        assert call["output_sha256"] == state["step_revisions"][0]["output_sha256"]
    finally:
        control.close()


def test_runtime_document_grouping_scopes_a_cited_part(tmp_path):
    """A manuscript part cited alone uses the macros of its definitions part."""

    outputs = {("br_0001", 1): output("$\\Ev$")}
    o, _record, control, _runtime_unused = harness(tmp_path, outputs=outputs)
    control.close()

    def run(grouped: bool, directory):
        directory.mkdir()
        config = o.config
        runtime = _runtime()
        runtime.evidence_sources = lambda: (
            EvidenceSource("literature_quote", "src_defs", "\\def\\Ev{{\\bf E}}"),
            EvidenceSource("literature_quote", SOURCE_ID, "Manuscript text."),
        )
        if grouped:
            runtime.evidence_source_documents = lambda: {
                "src_defs": "doi:paper",
                SOURCE_ID: "doi:paper",
            }
        runtime.writer_outputs = dict(outputs)
        record = RecordV1Writer(
            EventLogWriter(directory / "events.jsonl", config.run_id), config
        )
        store = ControlStore(directory / "control.sqlite")
        orchestrator = DerivationOrchestrator(
            config=config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=store,
            formula_engine_whitelist=WHITELIST,
        )
        try:
            asyncio.run(orchestrator.initialize("root"))
            asyncio.run(orchestrator._run_writer("br_0001"))
            state = record.verify_complete()
            return (
                state["step_revisions"][0]["content"]["derivation"]
                if state["step_revisions"]
                else None
            )
        finally:
            store.close()

    assert run(True, tmp_path / "with_documents") == "${\\bf E}$"
    # Without the grouping the definitions part is not cited: \Ev stays an
    # error and the output goes back for a format rewrite.
    assert run(False, tmp_path / "without_documents") is None


def test_a_legitimate_expansion_replays_and_names_its_definition(sealed_events):
    """The honest edit list of the same run still verifies, unchanged."""

    event = sealed_events[normalization_index(sealed_events)]
    expansions = [
        item
        for item in event["payload"]["replacements"]
        if item["kind"] == "macro_expansion"
    ]
    assert [item["original"] for item in expansions] == ["\\xv", "\\la"]
    assert [item["replacement"] for item in expansions] == ["{\\bf x}", "\\lambda"]
    assert {item["source_id"] for item in expansions} == {SOURCE_ID}
    # Line 2 is ``\def\xv{{\bf x}}`` and line 1 is ``\def\la{\lambda}``.
    assert [item["source_line"] for item in expansions] == [2, 1]
    replay_events(sealed_events)


def test_the_audit_report_describes_what_was_sealed(tmp_path):
    """The audit says what was applied, not what was computed."""

    o, _record, control, _ = harness(tmp_path)
    try:
        asyncio.run(o._run_writer("br_0001"))
        audits = control.formula_audits(o.config.run_id)
    finally:
        control.close()
    report = next(iter(audits.values()))["normalization"]
    assert report["normalization_applied"] is True
    assert report["replacement_count"] == 5
    assert "withheld_reason" not in report


def test_an_emptied_field_keeps_the_raw_text_and_says_so_in_the_audit(tmp_path):
    """Removing corruption must never empty a field, and never claim it did."""

    o, record, control, _ = harness(tmp_path)
    try:
        state = record.snapshot()
        # A source field that is nothing but an ANSI escape: normalization
        # would delete it whole, so the raw text is sealed instead.
        raw = {**output("$x=1$").content.to_record(), "source": "\x1b[0m"}
        fields, replacements, _quotations, report = o._formula_v2_normalization(
            state, raw
        )
    finally:
        control.close()
    assert fields == raw and replacements == []
    assert report["normalization_applied"] is False
    assert report["withheld_reason"] == "normalization_emptied_a_field"
    assert report["replacement_count"] == 0 and report["replacement_kinds"] == {}
    assert report["withheld_replacement_count"] == 1
    assert report["withheld_replacement_kinds"] == {"ansi_escape_removed": 1}


def test_an_escape_swallow_warning_rides_along_as_advice(tmp_path):
    """No deterministic repair exists, so the Writer is told, not blocked."""

    # ``\tau`` written with one backslash decodes to TAB + ``au``; the brace is
    # what actually blocks the seal and asks for the rewrite.
    swallowed = output("$\tau + a{b$")
    o, _record, control, _ = harness(tmp_path, outputs={("br_0001", 1): swallowed})
    try:
        asyncio.run(o._run_writer("br_0001"))
        feedback = o._formula_feedback("br_0001", 1)
        audits = control.formula_audits(o.config.run_id)
    finally:
        control.close()
    audit = next(iter(audits.values()))
    assert audit["disposition"] == "rejected"
    severities = {item["code"]: item["severity"] for item in audit["issues"]}
    assert severities["possible_escape_swallow"] == "warning"
    assert severities["syntax_error"] == "error"

    codes = [item["code"] for item in feedback]
    assert "syntax_error" in codes and "possible_escape_swallow" in codes
    blocking = [item for item in feedback if not item.get("advisory")]
    advisory = [item for item in feedback if item.get("advisory")]
    # The advice never becomes a blocking diagnostic, so it cannot spend a
    # format rewrite or stop the step being sealed.
    assert [item["code"] for item in advisory] == ["possible_escape_swallow"]
    assert all(item["severity"] == "error" for item in blocking)
