"""Provider-free formatting gate and crash recovery contracts."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from .control import ControlStore
from .orchestrator import DerivationOrchestrator
from .record import EventLogWriter, RecordV1Writer
from .selftest import TASK_TEXT, _config, _runtime, _writer_output
from .types import FormulaValidationResult, RunPhase, WriterDecision

ERROR = FormulaValidationResult(
    issues=(
        {
            "severity": "error",
            "code": "unbalanced_math",
            "field": "derivation",
            "message": "Close the math delimiter.",
        },
    )
)


def harness(tmp_path, *, retries=1, checker=True, policy="formula-v1", validator=None):
    config = replace(
        _config(max_active_branches=1, max_model_calls=100, retries=retries),
        record_version="1.1",
        checker_enabled=checker,
        formula_validation_policy=policy,
    )
    runtime = _runtime()
    runtime.writer_outputs = {
        ("br_0001", 1): _writer_output("original", WriterDecision.COMPLETE)
    }
    record = RecordV1Writer(
        EventLogWriter(tmp_path / "events.jsonl", config.run_id), config
    )
    control = ControlStore(tmp_path / "control.sqlite")
    o = DerivationOrchestrator(
        config=config,
        task_text=TASK_TEXT,
        runtime=runtime,
        record=record,
        control=control,
        formula_validator=validator,
    )
    asyncio.run(o.initialize("root"))
    return o, record, control, runtime


@pytest.mark.parametrize("checker", [False, True])
def test_rejection_preserves_output_and_repairs_same_slot(tmp_path, checker):
    results = iter([ERROR, FormulaValidationResult()])
    o, record, control, runtime = harness(
        tmp_path, checker=checker, validator=lambda _: next(results)
    )
    try:
        asyncio.run(o._run_writer("br_0001"))
        state = record.snapshot()
        assert state["model_calls"][0]["state"] == "finished"
        assert "original" in state["model_calls"][0]["output_text"]
        assert not state["step_revisions"] and not state["checks"]
        assert len(state["branches"]) == 1
        assert o._formula_feedback("br_0001", 1) == ERROR.issues
        runtime.writer_outputs[("br_0001", 1)] = _writer_output(
            "corrected", WriterDecision.COMPLETE
        )
        asyncio.run(o._run_writer("br_0001"))
        assert len(record.snapshot()["step_revisions"]) == 1
        assert len(record.snapshot()["model_calls"]) == 2
        assert not o._repair_record_prefix()
    finally:
        control.close()


@pytest.mark.parametrize("retries,attempts", [(0, 1), (1, 2)])
def test_retry_ceiling_pauses_without_sealing(tmp_path, retries, attempts):
    o, record, control, _ = harness(
        tmp_path, retries=retries, validator=lambda _: ERROR
    )
    try:
        for _ in range(attempts):
            asyncio.run(o._run_writer("br_0001"))
        assert control.run(o.config.run_id).phase == "paused"
        assert not record.snapshot()["step_revisions"]
        assert len(control.formula_audits(o.config.run_id)) == attempts
        o._repair_record_prefix()
        assert not record.snapshot()["step_revisions"]
    finally:
        control.close()


def test_restart_keeps_rejection_and_budget(tmp_path):
    o, record, control, runtime = harness(tmp_path, validator=lambda _: ERROR)
    asyncio.run(o._run_writer("br_0001"))
    control.close()
    with ControlStore(tmp_path / "control.sqlite") as restored:
        restarted = DerivationOrchestrator(
            config=o.config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=restored,
            formula_validator=lambda _: ERROR,
        )
        restarted._repair_record_prefix()
        assert len(restored.formula_audits(o.config.run_id)) == 1
        asyncio.run(restarted._run_writer("br_0001"))
        assert restored.run(o.config.run_id).phase == "paused"
        assert not record.snapshot()["step_revisions"]


def test_crash_after_finished_before_audit_runs_gate_during_repair(tmp_path):
    o, record, control, _ = harness(tmp_path, retries=0, validator=lambda _: ERROR)
    try:
        with (
            patch.object(
                o, "_formula_gate", side_effect=RuntimeError("simulated crash")
            ),
            pytest.raises(RuntimeError, match="simulated crash"),
        ):
            asyncio.run(o._run_writer("br_0001"))
        o._repair_record_prefix()
        assert not record.snapshot()["step_revisions"]
        assert control.run(o.config.run_id).phase == "paused"
    finally:
        control.close()


def test_missing_validator_pauses_immediately(tmp_path):
    o, _record, control, _ = harness(tmp_path)
    try:
        asyncio.run(o._run_writer("br_0001"))
        assert control.run(o.config.run_id).phase == "paused"
        assert (
            next(iter(control.formula_audits(o.config.run_id).values()))["disposition"]
            == "infrastructure_failure"
        )
    finally:
        control.close()


def test_historical_policy_absent_does_not_check(tmp_path):
    def forbidden(_):
        raise AssertionError("old runs must not validate")

    o, record, control, _ = harness(tmp_path, policy=None, validator=forbidden)
    try:
        asyncio.run(o._run_writer("br_0001"))
        assert len(record.snapshot()["step_revisions"]) == 1
        assert control.formula_audits(o.config.run_id) == {}
    finally:
        control.close()


def test_drive_stops_at_exhausted_gate_and_resume_does_not_reset_budget(tmp_path):
    o, record, control, _ = harness(tmp_path, validator=lambda _: ERROR)
    try:
        asyncio.run(o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        assert control.run(o.config.run_id).phase == RunPhase.PAUSED.value
        assert len(record.snapshot()["model_calls"]) == 2
        assert not record.snapshot()["step_revisions"]
        asyncio.run(o.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION))
        assert control.run(o.config.run_id).phase == RunPhase.PAUSED.value
        assert len(record.snapshot()["model_calls"]) == 2
    finally:
        control.close()


def test_warning_only_output_seals_and_audits(tmp_path):
    warning = FormulaValidationResult(
        issues=(
            {"severity": "warning", "field": "source", "code": "unsupported_macro"},
        )
    )
    o, record, control, _ = harness(tmp_path, validator=lambda _: warning)
    try:
        asyncio.run(o._run_writer("br_0001"))
        assert len(record.snapshot()["step_revisions"]) == 1
        assert next(iter(control.formula_audits(o.config.run_id).values()))[
            "issues"
        ] == list(warning.issues)
    finally:
        control.close()


def test_explicit_resume_rechecks_infrastructure_without_new_writer_call(tmp_path):
    o, record, control, _ = harness(tmp_path, checker=False)
    try:
        asyncio.run(o._run_writer("br_0001"))
        original = record.snapshot()["model_calls"][0]["output_text"]
        o._formula_validator = lambda _: FormulaValidationResult()
        asyncio.run(o.resume(actor_id="tester", reason="Compiler restored"))
        state = record.snapshot()
        assert len(state["model_calls"]) == 1
        assert state["model_calls"][0]["output_text"] == original
        assert len(state["step_revisions"]) == 1
        assert (
            next(iter(control.formula_audits(o.config.run_id).values()))["disposition"]
            == "accepted"
        )
        assert (
            control._connection.execute(
                "SELECT COUNT(*) FROM formula_audit_rechecks"
            ).fetchone()[0]
            == 1
        )
        assert (
            "infrastructure_failure"
            in control._connection.execute(
                "SELECT payload FROM formula_audits"
            ).fetchone()[0]
        )
    finally:
        control.close()
