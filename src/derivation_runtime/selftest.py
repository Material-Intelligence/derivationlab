"""Standard-library acceptance tests for the runtime core vertical slice."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from derivation_agent_record import (
    ContractError,
    canonical_json,
    sha256_bytes,
    sha256_text,
)

from .control import ControlStore
from .fake import DeterministicFakeRuntime
from .orchestrator import DerivationOrchestrator
from .query import ReplayQuery
from .record import EventLogWriter, RecordV1Writer
from .types import (
    ArtifactRef,
    BranchAlternative,
    CheckOutput,
    CheckRequest,
    ContentRef,
    InputPolicy,
    JudgeOutput,
    JudgeRequest,
    ModelRole,
    ModelSpec,
    ReconcileStatus,
    RunConfig,
    RunPhase,
    RuntimeInvariantError,
    RuntimeInvocationError,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
    WriterRequest,
)

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
TASK_TEXT = (
    "Construct and compare internally consistent derivation routes for a toy invariant."
)


def _artifact(path: str) -> ArtifactRef:
    absolute = ROOT / path
    return ArtifactRef(path=path, sha256=sha256_bytes(absolute.read_bytes()))


def _config(
    *, max_active_branches: int, max_model_calls: int, retries: int
) -> RunConfig:
    return RunConfig(
        run_id="run_runtime_core_test",
        task=ContentRef("task_runtime_core", sha256_text(TASK_TEXT)),
        pack=ContentRef("pack_empty", sha256_text("")),
        code_commit="0" * 40,
        granularity="one_task",
        max_active_branches=max_active_branches,
        max_model_calls=max_model_calls,
        concurrency=1,
        retries=retries,
        writer=ModelSpec("fake", "fake:writer", "deterministic"),
        checker=ModelSpec("fake", "fake:checker", "deterministic"),
        judge=ModelSpec("fake", "fake:judge", "deterministic"),
        backend_name="deterministic-fake-runtime",
        backend_version="1",
        record_spec=_artifact("docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md"),
        event_schema=_artifact("docs/spec/derivation_agent_event_v1.schema.json"),
        canonical_schema=_artifact(
            "docs/spec/derivation_agent_canonical_v1.schema.json"
        ),
        input_policy=InputPolicy(reference_allowed=False, allowed_paths=()),
        credential_profile_id="fake-profile",
    )


def _step(label: str) -> StepContent:
    return StepContent(
        claim=f"Claim {label}",
        why=f"Reason for {label}",
        source=f"Toy source {label}",
        derivation=f"Deterministic derivation {label}",
        scope=f"Toy scope {label}",
    )


def _writer_output(
    label: str,
    decision: WriterDecision,
    alternatives: tuple[BranchAlternative, ...] = (),
) -> WriterOutput:
    return WriterOutput(
        content=_step(label),
        control=WriterControl(decision=decision, alternatives=alternatives),
        finish_reason="stop",
        usage=Usage({"input_tokens": 1, "output_tokens": 1}),
    )


def _runtime() -> DeterministicFakeRuntime:
    outputs = {
        ("br_0001", 1): _writer_output("root-1", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer_output(
            "root-2",
            WriterDecision.FORK,
            (BranchAlternative("Use a symmetry-based alternative."),),
        ),
        ("br_0001", 3): _writer_output("root-3", WriterDecision.COMPLETE),
        ("br_0002", 3): _writer_output("model-alt-3", WriterDecision.COMPLETE),
        ("br_0003", 2): _writer_output("human-direction-2", WriterDecision.COMPLETE),
        ("br_0003", 3): _writer_output("human-revision-3", WriterDecision.COMPLETE),
    }

    def check_factory(_: object) -> CheckOutput:
        return CheckOutput(
            verdict="ok",
            reason="The deterministic fixture contains no hard defect.",
            evidence=(),
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )

    def judge_factory(_: object) -> JudgeOutput:
        return JudgeOutput(
            verdict="pass",
            reason="The deterministic candidate satisfies the toy task.",
            score=1.0,
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )

    return DeterministicFakeRuntime(
        writer_outputs=outputs,
        check_factory=check_factory,
        judge_factory=judge_factory,
    )


class RuntimeCoreTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    def setUp(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix="runtime-core-test-", dir=RUNS)
        self.directory = Path(self._temp.name)
        self._stores: list[ControlStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            try:
                store.close()
            except sqlite3.ProgrammingError:  # type: ignore[name-defined]
                pass
        self._temp.cleanup()

    def components(
        self,
        *,
        max_active_branches: int = 2,
        max_model_calls: int = 20,
        retries: int = 0,
        runtime: DeterministicFakeRuntime | None = None,
        control_path: Path | None = None,
    ) -> tuple[
        DerivationOrchestrator,
        RecordV1Writer,
        ControlStore,
        DeterministicFakeRuntime,
    ]:
        config = _config(
            max_active_branches=max_active_branches,
            max_model_calls=max_model_calls,
            retries=retries,
        )
        selected_runtime = runtime or _runtime()
        log = EventLogWriter(self.directory / "events.jsonl", config.run_id)
        record = RecordV1Writer(log, config)
        control = ControlStore(control_path or (self.directory / "control.sqlite"))
        self._stores.append(control)
        orchestrator = DerivationOrchestrator(
            config=config,
            task_text=TASK_TEXT,
            runtime=selected_runtime,
            record=record,
            control=control,
        )
        return orchestrator, record, control, selected_runtime

    async def finished_writer_prefix_with_raw_control(
        self, raw_control: str
    ) -> tuple[DerivationOrchestrator, RecordV1Writer, ControlStore]:
        orchestrator, record, control, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        body = canonical_json(_step("raw-control-fixture").to_record())
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=raw_control,
        )
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=1,
            text=body,
        )
        record.finish_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            output_text=body,
            finish_reason="stop",
            usage={},
        )
        return orchestrator, record, control

    async def assert_raw_control_fails_closed(self, raw_control: str) -> None:
        orchestrator, record, _ = await self.finished_writer_prefix_with_raw_control(
            raw_control
        )
        before = len(record.events)
        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        snapshot = record.snapshot()
        self.assertEqual(snapshot["model_calls"][0]["state"], "finished")
        self.assertEqual(snapshot["step_revisions"], [])
        self.assertEqual(snapshot["summary"]["branch_count"], 1)

    @staticmethod
    def _step_by_id(state: object, step_revision_id: str) -> dict[str, object]:
        snapshot = state  # Keep test call sites concise without hiding setup.
        assert isinstance(snapshot, dict)
        return next(
            item
            for item in snapshot["step_revisions"]
            if item["step_revision_id"] == step_revision_id
        )

    async def test_autonomous_tree_then_human_expansion(self) -> None:
        orchestrator, record, control, runtime = self.components()
        query = await orchestrator.submit("Explore the root route.")

        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["branch_count"], 2)
        self.assertEqual(canonical["summary"]["candidate_count"], 2)
        self.assertEqual(canonical["summary"]["judgement_count"], 2)
        self.assertEqual(
            {item["status"] for item in canonical["branches"]}, {"completed"}
        )
        self.assertEqual(len(set(runtime.writer_sessions_by_branch["br_0001"])), 1)
        self.assertEqual(len(runtime.writer_sessions_by_branch["br_0001"]), 3)
        self.assertEqual(runtime.rehydrations, [])

        model_branch = next(
            item
            for item in canonical["branches"]
            if item["created_reason"] == "model_alternative"
        )
        source_event = next(
            item
            for item in record.events
            if item["event_id"] == model_branch["hypothesis"]["source_event_id"]
        )
        self.assertEqual(source_event["type"], "model_call_chunk")
        self.assertEqual(source_event["payload"]["channel"], "raw")

        query = await orchestrator.expand_from_step(
            parent_branch_id="br_0001",
            step_revision_id="step_0001",
            direction="Revisit the second step with an explicit conservation argument.",
            actor_id="fixture-user",
            reason="Post-run human review requested another route.",
        )
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["branch_count"], 3)
        human_branch = next(
            item
            for item in canonical["branches"]
            if item["created_reason"] == "human_direction"
        )
        self.assertEqual(human_branch["status"], "completed")
        self.assertEqual(human_branch["provenance"]["content_class"], "human_steered")
        self.assertEqual(human_branch["inherited_step_revision_ids"], ["step_0001"])
        self.assertEqual(control.run(orchestrator.config.run_id).phase, "review_ready")

    async def test_control_sqlite_deletion_preserves_replay_and_uses_rehydrate(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.submit("Explore the root route.")
        before = record.verify_complete()
        event_bytes = (self.directory / "events.jsonl").read_bytes()
        control_path = self.directory / "control.sqlite"
        control.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(control_path) + suffix)
            if path.exists():
                path.unlink()

        new_control = ControlStore(control_path)
        self._stores.append(new_control)
        new_control.rebuild_from_snapshot(
            before,
            credential_profile_id=orchestrator.config.credential_profile_id,
            phase=RunPhase.REVIEW_READY,
            stop_reason=None,
        )
        self.assertEqual(
            ReplayQuery.from_event_log(self.directory / "events.jsonl").canonical,
            before,
        )
        self.assertEqual((self.directory / "events.jsonl").read_bytes(), event_bytes)

        recovered = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=new_control,
        )
        await recovered.expand_from_step(
            parent_branch_id="br_0001",
            step_revision_id="step_0001",
            direction="Revisit the second step with an explicit conservation argument.",
            actor_id="fixture-user",
            reason="Exercise provider rehydration after bookmark deletion.",
        )
        self.assertTrue(runtime.rehydrations)
        self.assertEqual(runtime.rehydrations[-1][1], ("step_0001",))
        self.assertEqual(record.verify_complete()["summary"]["branch_count"], 3)

    async def test_same_active_branch_rehydrates_exact_prefix_after_control_loss(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        first_snapshot = record.verify_complete()
        first_history = tuple(runtime.writer_sessions_by_branch["br_0001"])
        self.assertEqual(len(first_history), 1)

        control_path = self.directory / "control.sqlite"
        control.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(control_path) + suffix)
            if path.exists():
                path.unlink()
        new_control = ControlStore(control_path)
        self._stores.append(new_control)
        new_control.rebuild_from_snapshot(
            first_snapshot,
            credential_profile_id=orchestrator.config.credential_profile_id,
            phase=RunPhase.RECOVERING,
            stop_reason=None,
        )
        recovered = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=new_control,
        )

        query = await recovered.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY)
        self.assertEqual(len(runtime.rehydrations), 1)
        self.assertEqual(runtime.rehydrations[0][1], ("step_0001",))
        history = runtime.writer_sessions_by_branch["br_0001"]
        self.assertEqual(len(history), 3)
        self.assertNotEqual(history[0], history[1])
        self.assertEqual(history[1], history[2])
        self.assertEqual(record.verify_complete()["summary"]["candidate_count"], 2)

    async def test_human_revision_replaces_on_new_branch_and_preserves_old_route(
        self,
    ) -> None:
        orchestrator, record, _, runtime = self.components()
        await orchestrator.submit("Explore the root route.")
        before = record.verify_complete()
        old_root = next(
            item for item in before["branches"] if item["branch_id"] == "br_0001"
        )
        old_steps = {
            item["step_revision_id"]: item["output_sha256"]
            for item in before["step_revisions"]
            if item["step_revision_id"] in old_root["step_revision_ids"]
        }
        old_candidate = next(
            item for item in before["candidates"] if item["branch_id"] == "br_0001"
        )
        replacement = StepContent(
            claim="Replacement claim for root step two",
            why="Human review identified a clearer invariant-preserving route.",
            source="Human-authored replacement fixture",
            derivation="Replace only slot two while preserving the exact first-step prefix.",
            scope="Toy replacement branch only.",
        )

        query = await orchestrator.revise_step(
            parent_branch_id="br_0001",
            step_revision_id="step_0002",
            replacement=replacement,
            actor_id="fixture-user",
            reason="Replace the second sealed step without modifying history.",
        )
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY)
        after = record.verify_complete()
        new_root = next(
            item for item in after["branches"] if item["branch_id"] == "br_0001"
        )
        self.assertEqual(new_root["step_revision_ids"], old_root["step_revision_ids"])
        self.assertEqual(
            {
                item["step_revision_id"]: item["output_sha256"]
                for item in after["step_revisions"]
                if item["step_revision_id"] in new_root["step_revision_ids"]
            },
            old_steps,
        )
        preserved_candidate = next(
            item
            for item in after["candidates"]
            if item["candidate_id"] == old_candidate["candidate_id"]
        )
        self.assertEqual(
            preserved_candidate["transcript_sha256"], old_candidate["transcript_sha256"]
        )

        revision_branch = next(
            item
            for item in after["branches"]
            if item["created_reason"] == "human_revision"
        )
        self.assertEqual(revision_branch["fork_mode"], "replace")
        self.assertEqual(revision_branch["inherited_step_revision_ids"], ["step_0001"])
        self.assertEqual(revision_branch["provenance"]["content_class"], "human_edited")
        replacement_step = next(
            item
            for item in after["step_revisions"]
            if item["replaces_step_revision_id"] == "step_0002"
        )
        self.assertEqual(replacement_step["step_slot"], 2)
        self.assertEqual(replacement_step["revision"], 2)
        self.assertEqual(replacement_step["origin"]["kind"], "human")
        self.assertEqual(replacement_step["content"], replacement.to_record())
        self.assertEqual(
            runtime.rehydrations[-1][1],
            ("step_0001", replacement_step["step_revision_id"]),
        )

    async def test_wrong_replace_prefix_is_rejected_without_branch_append(self) -> None:
        orchestrator, record, _, _ = self.components()
        await orchestrator.submit("Explore the root route.")
        replacement = _step("invalid-prefix-replacement")
        action = record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="revise_step",
            target={"step_revision_id": "step_0002"},
            reason="Exercise the exact-prefix validator.",
            content=canonical_json(replacement.to_record()),
        )
        before = len(record.events)
        with self.assertRaises(ContractError):
            record.create_child_branch(
                branch_id="br_invalid_prefix",
                parent_branch_id="br_0001",
                fork_mode="replace",
                anchor_step_revision_id="step_0002",
                inherited_step_revision_ids=[],
                hypothesis="Invalid prefix fixture",
                hypothesis_source="human",
                hypothesis_source_event_id=action["event_id"],
                created_reason="human_revision",
                human_action_id="act_0001",
                actor={"kind": "human", "id": "fixture-user"},
            )
        self.assertEqual(len(record.events), before)

    async def test_model_call_cap_is_derived_without_fake_park_event(self) -> None:
        orchestrator, record, control, _ = self.components(max_model_calls=2)
        query = await orchestrator.submit("Explore the root route.")
        status = query.status()
        self.assertEqual(status.phase, RunPhase.REVIEW_READY_DUE_TO_CAP)
        self.assertEqual(status.stop_reason, "max_model_calls")
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 2)
        self.assertEqual(canonical["branches"][0]["status"], "active")
        self.assertFalse(
            any(
                item["type"] == "branch_status_changed"
                and item["payload"]["to_status"] == "parked"
                for item in record.events
            )
        )
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "max_model_calls",
        )

    async def test_branch_cap_keeps_proposal_in_raw_call_only(self) -> None:
        orchestrator, record, _, _ = self.components(max_active_branches=1)
        await orchestrator.submit("Explore the root route.")
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["branch_count"], 1)
        writer_calls = [
            item for item in canonical["model_calls"] if item["role"] == "writer"
        ]
        fork_call = next(
            item
            for item in writer_calls
            if any(
                chunk["channel"] == "raw" and '"decision":"fork"' in chunk["text"]
                for chunk in item["chunks"]
            )
        )
        self.assertIn(
            "Use a symmetry-based alternative.", canonical_json(fork_call["chunks"])
        )

    async def test_soft_pause_and_resume_are_not_step_approval(self) -> None:
        orchestrator, record, _, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        orchestrator.request_soft_pause(
            actor_id="fixture-user", reason="Pause the whole run."
        )
        paused = await orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        self.assertEqual(paused.status().phase, RunPhase.PAUSED)
        self.assertEqual(record.verify_complete()["branches"][0]["status"], "paused")

        resumed = await orchestrator.resume(
            actor_id="fixture-user", reason="Resume autonomous exploration."
        )
        self.assertEqual(resumed.status().phase, RunPhase.REVIEW_READY)
        self.assertEqual(record.verify_complete()["summary"]["branch_count"], 2)

    async def test_rebuild_preserves_pause_intent_after_writer_seal(self) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        orchestrator.request_soft_pause(
            actor_id="fixture-user",
            reason="Stop after sealing the current writer turn.",
        )
        output = await runtime.collect_writer(invocation)
        body = canonical_json(output.content.to_record())
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=canonical_json(output.control.to_record()),
        )
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=1,
            text=body,
        )
        terminal = record.finish_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )
        record.seal_model_step(
            step_revision_id="step_0001",
            branch_id="br_0001",
            step_slot=1,
            content=output.content,
            model_call_id="call_0001",
        )
        control.finish_call(
            orchestrator.config.run_id,
            "call_0001",
            state="finished",
            record_terminal_seq=terminal["seq"],
            last_error=None,
        )
        control.record_step(
            orchestrator.config.run_id,
            "step_0001",
            "br_0001",
            invocation,
        )
        control.upsert_branch(
            orchestrator.config.run_id,
            "br_0001",
            runtime_state="active",
            provider_session_id=invocation.session.session_id,
            provider_lineage=invocation.session.lineage.value,
            last_operation_id=invocation.operation_id,
            last_step_revision_id="step_0001",
            attempt=0,
            last_error=None,
        )
        # Crash here: Record is ahead of the control head, while pause intent
        # is already durable in control.sqlite.
        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=_runtime(),
            record=record,
            control=control,
        )

        query = await restarted.reconcile_in_flight()
        self.assertIsNotNone(query)
        self.assertEqual(query.status().phase, RunPhase.PAUSED)
        canonical = record.verify_complete()
        self.assertEqual(canonical["branches"][0]["status"], "paused")
        self.assertEqual(canonical["summary"]["model_call_count"], 1)
        event_types = [item["type"] for item in record.events]
        self.assertLess(
            event_types.index("model_call_finished"),
            event_types.index("step_revision_sealed"),
        )
        self.assertLess(
            event_types.index("step_revision_sealed"),
            event_types.index("human_action_recorded"),
        )
        self.assertLess(
            event_types.index("human_action_recorded"),
            event_types.index("branch_status_changed"),
        )
        self.assertNotIn("check_requested", event_types)

    async def test_orphan_pause_action_repairs_existing_action_without_duplicate(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        control.request_pause(
            orchestrator.config.run_id,
            actor_id="fixture-user",
            reason="Pause action/status crash-window fixture.",
        )
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="pause_branch",
            target={"branch_id": "br_0001"},
            reason="Pause action/status crash-window fixture.",
            content=None,
        )

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        self.assertEqual(query.status().phase, RunPhase.PAUSED)
        canonical = record.verify_complete()
        self.assertEqual(canonical["branches"][0]["status"], "paused")
        self.assertEqual(len(canonical["human_actions"]), 1)
        transitions = [
            item
            for item in record.events
            if item["type"] == "branch_status_changed"
            and item["payload"]["human_action_id"] == "act_0001"
        ]
        self.assertEqual(len(transitions), 1)

    async def test_orphan_resume_action_repairs_status_and_clears_pause_intent(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        orchestrator.request_soft_pause(
            actor_id="fixture-user", reason="Prepare orphan resume fixture."
        )
        await orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0002",
            action="resume_branch",
            target={"branch_id": "br_0001"},
            reason="Resume action/status crash-window fixture.",
            content=None,
        )

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY)
        canonical = record.verify_complete()
        resume_actions = [
            item
            for item in canonical["human_actions"]
            if item["action"] == "resume_branch"
        ]
        self.assertEqual([item["action_id"] for item in resume_actions], ["act_0002"])
        transitions = [
            item
            for item in record.events
            if item["type"] == "branch_status_changed"
            and item["payload"]["human_action_id"] == "act_0002"
        ]
        self.assertEqual(len(transitions), 1)
        self.assertFalse(control.run(orchestrator.config.run_id).pause_requested)

    async def test_orphan_abort_action_aborts_and_parks_without_retry(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="abort_model_call",
            target={"model_call_id": "call_0001"},
            reason="Crash after abort intent and before provider interrupt.",
            content=None,
        )

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 1)
        self.assertEqual(canonical["model_calls"][0]["state"], "aborted")
        self.assertEqual(canonical["branches"][0]["status"], "parked")
        self.assertEqual(canonical["model_calls"][0]["failure"]["kind"], "human_abort")
        provider_state = await runtime.reconcile(invocation)
        self.assertEqual(provider_state.status, ReconcileStatus.INTERRUPTED)

    async def test_orphan_abort_without_provider_handle_fails_closed(self) -> None:
        orchestrator, record, control, _ = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        orchestrator._sync_head()
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="abort_model_call",
            target={"model_call_id": "call_0001"},
            reason="Abort intent outlived its provider handle.",
            content=None,
        )
        before = len(record.events)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        snapshot = record.snapshot()
        self.assertEqual(snapshot["model_calls"][0]["state"], "started")
        self.assertEqual(snapshot["branches"][0]["status"], "active")
        self.assertEqual(snapshot["summary"]["model_call_count"], 1)
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "interrupt_reconciliation_failed",
        )

    async def test_orphan_abort_already_interrupted_closes_without_second_interrupt(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="abort_model_call",
            target={"model_call_id": "call_0001"},
            reason="Provider interrupted before Record terminal append.",
            content=None,
        )
        await runtime.interrupt(invocation)
        original_interrupt = runtime.interrupt
        second_interrupt_attempts = 0

        async def counted_interrupt(target: object) -> object:
            nonlocal second_interrupt_attempts
            second_interrupt_attempts += 1
            return await original_interrupt(target)  # type: ignore[arg-type]

        runtime.interrupt = counted_interrupt  # type: ignore[method-assign,assignment]

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        self.assertEqual(second_interrupt_attempts, 0)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 1)
        self.assertEqual(canonical["model_calls"][0]["state"], "aborted")
        self.assertEqual(canonical["branches"][0]["status"], "parked")
        provider_state = await runtime.reconcile(invocation)
        self.assertEqual(provider_state.status, ReconcileStatus.INTERRUPTED)

    async def test_orphan_abort_with_durable_body_requires_manual_review(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        durable_body = "durable partial writer body"
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=0,
            text=durable_body,
        )
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="abort_model_call",
            target={"model_call_id": "call_0001"},
            reason="Abort after durable partial output.",
            content=None,
        )

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 1)
        self.assertEqual(canonical["model_calls"][0]["state"], "aborted")
        self.assertEqual(canonical["model_calls"][0]["output_text"], durable_body)
        self.assertEqual(canonical["branches"][0]["status"], "active")
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "partial_writer_failure_requires_review",
        )
        provider_state = await runtime.reconcile(invocation)
        self.assertEqual(provider_state.status, ReconcileStatus.INTERRUPTED)

    async def test_orphan_direction_action_fails_closed_without_branch(self) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.submit("Explore the root route.")
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="set_direction",
            target={"branch_id": "br_0001"},
            reason="Direction action lost its branch anchor.",
            content="A direction whose exact anchor was not durably recorded.",
        )
        before = len(record.events)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        self.assertEqual(record.snapshot()["summary"]["branch_count"], 2)
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "orphan_human_action",
        )

    async def test_orphan_revision_action_fails_closed_without_guessing_parent(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.submit("Explore the root route.")
        replacement = _step("orphan-revision")
        record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="revise_step",
            target={"step_revision_id": "step_0002"},
            reason="Revision action lost its selected parent route.",
            content=canonical_json(replacement.to_record()),
        )
        before = len(record.events)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        self.assertFalse(
            any(
                item["created_reason"] == "human_revision"
                for item in record.snapshot()["branches"]
            )
        )
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "orphan_human_action",
        )

    async def test_run_created_without_root_branch_fails_closed(self) -> None:
        orchestrator, record, control, _ = self.components()
        record.create_run()
        control.ensure_run(orchestrator.config, phase=RunPhase.SUBMITTED)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        run = control.run(orchestrator.config.run_id)
        self.assertEqual(run.phase, RunPhase.ERROR.value)
        self.assertEqual(run.stop_reason, "invalid_record_prefix")
        self.assertEqual(record.snapshot()["branches"], [])

    async def test_completed_provider_call_is_reconciled_before_strict_replay(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        prompt = orchestrator._writer_prompt(request)
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=prompt,
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        runtime.complete_without_collect(invocation.operation_id)

        with self.assertRaises(ContractError):
            record.verify_complete()
        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        self.assertEqual(record.verify_complete()["summary"]["branch_count"], 2)

    async def test_finished_writer_is_sealed_without_rerunning_same_slot(self) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        output = await runtime.collect_writer(invocation)
        body = canonical_json(output.content.to_record())
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=canonical_json(output.control.to_record()),
        )
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=1,
            text=body,
        )
        record.finish_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )

        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=_runtime(),
            record=record,
            control=control,
        )
        query = await restarted.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        first_slot_calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "writer"
            and call["target"] == {"branch_id": "br_0001", "step_slot": 1}
        ]
        self.assertEqual(
            [call["model_call_id"] for call in first_slot_calls], ["call_0001"]
        )
        first_step = next(
            step
            for step in canonical["step_revisions"]
            if step["branch_id"] == "br_0001" and step["step_slot"] == 1
        )
        self.assertEqual(first_step["origin"]["model_call_id"], "call_0001")
        self.assertEqual(first_step["content"], output.content.to_record())

    async def test_unknown_provider_state_retries_once_with_a_new_model_call(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        control.upsert_branch(
            orchestrator.config.run_id,
            "br_0001",
            runtime_state="running",
            provider_session_id=invocation.session.session_id,
            provider_lineage=invocation.session.lineage.value,
            last_operation_id=invocation.operation_id,
            last_step_revision_id=None,
            attempt=1,
            last_error=None,
        )
        orchestrator._sync_head()
        runtime.forget_operation(invocation.operation_id)

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        first_slot_calls = [
            item
            for item in canonical["model_calls"]
            if item["role"] == "writer"
            and item["target"] == {"branch_id": "br_0001", "step_slot": 1}
        ]
        self.assertEqual(len(first_slot_calls), 2)
        self.assertEqual(first_slot_calls[0]["state"], "failed")
        self.assertEqual(
            first_slot_calls[0]["failure"]["kind"], "unknown_provider_state"
        )
        self.assertEqual(first_slot_calls[1]["state"], "finished")
        retry_bookmark = control.call(
            orchestrator.config.run_id, first_slot_calls[1]["model_call_id"]
        )
        self.assertIsNotNone(retry_bookmark)
        self.assertEqual(retry_bookmark.attempt, 2)

    async def test_unsynced_raw_chunk_rebuild_reconciles_started_call(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()

        # Exact crash window: the raw control chunk is durable, while the
        # SQLite head still points at model_call_started.
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=canonical_json(
                _writer_output("root-1", WriterDecision.CONTINUE).control.to_record()
            ),
        )
        runtime.forget_operation(invocation.operation_id)
        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=runtime,
            record=record,
            control=control,
        )

        query = await restarted.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        first_slot_calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "writer"
            and call["target"] == {"branch_id": "br_0001", "step_slot": 1}
        ]
        self.assertEqual(
            [call["state"] for call in first_slot_calls], ["failed", "finished"]
        )
        self.assertEqual(
            first_slot_calls[0]["failure"]["kind"], "unknown_provider_state"
        )
        self.assertEqual([prefix for _, prefix in runtime.rehydrations], [()])
        history = runtime.writer_sessions_by_branch["br_0001"]
        self.assertEqual(len(history), 4)
        self.assertNotEqual(history[0], history[1])
        self.assertEqual(len(set(history[1:])), 1)
        control.rebuild_from_snapshot(
            canonical,
            credential_profile_id=orchestrator.config.credential_profile_id,
            phase=RunPhase.REVIEW_READY,
            stop_reason=None,
        )
        self.assertEqual(
            [
                control.call(orchestrator.config.run_id, call["model_call_id"]).state
                for call in first_slot_calls
            ],
            ["failed", "finished"],
        )
        self.assertEqual(control.in_flight_calls(orchestrator.config.run_id), [])

    async def test_unsynced_body_chunk_fails_closed_without_writer_retry(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()
        durable_body = canonical_json(_step("durable-unsealed-partial").to_record())
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=canonical_json(WriterControl(WriterDecision.CONTINUE, ()).to_record()),
        )
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=1,
            text=durable_body,
        )
        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=_runtime(),
            record=record,
            control=control,
        )

        with self.assertRaises(RuntimeInvocationError):
            await restarted.reconcile_in_flight()
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 1)
        call = canonical["model_calls"][0]
        self.assertEqual(call["state"], "failed")
        self.assertEqual(call["output_text"], durable_body)
        self.assertEqual(call["body_chars"], len(durable_body))
        self.assertEqual(canonical["branches"][0]["status"], "active")
        run = control.run(orchestrator.config.run_id)
        self.assertEqual(run.phase, RunPhase.ERROR.value)
        self.assertEqual(run.stop_reason, "partial_writer_failure_requires_review")

    async def test_finished_writer_invalid_shape_fails_closed_without_retry(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="raw",
            index=0,
            text=canonical_json(WriterControl(WriterDecision.CONTINUE, ()).to_record()),
        )
        malformed = canonical_json({"claim": "only one field"})
        record.chunk_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            channel="body",
            index=1,
            text=malformed,
        )
        record.finish_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            output_text=malformed,
            finish_reason="stop",
            usage={},
        )
        before = len(record.events)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        state = record.snapshot()
        self.assertEqual(state["model_calls"][0]["state"], "finished")
        self.assertEqual(state["step_revisions"], [])

    async def test_finished_writer_control_rejects_extra_key(self) -> None:
        await self.assert_raw_control_fails_closed(
            canonical_json(
                {"decision": "continue", "alternatives": [], "extra": "forbidden"}
            )
        )

    async def test_finished_writer_control_rejects_alternatives_object(self) -> None:
        await self.assert_raw_control_fails_closed(
            canonical_json(
                {
                    "decision": "fork",
                    "alternatives": {"hypothesis": "ignored dictionary key"},
                }
            )
        )

    async def test_finished_writer_control_rejects_noncanonical_json(self) -> None:
        await self.assert_raw_control_fails_closed(
            '{"decision": "continue", "alternatives": []}'
        )

    async def test_finished_writer_control_rejects_duplicate_key(self) -> None:
        await self.assert_raw_control_fails_closed(
            '{"alternatives":[],"decision":"continue","decision":"continue"}'
        )

    async def test_partial_failed_writer_prefix_fails_closed_before_retry(self) -> None:
        orchestrator, record, control, _ = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt="partial-writer-crash-fixture",
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        record.fail_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            failure_kind="provider_transport",
            message="writer failed after producing partial text",
            partial_output="partial scientific prose",
            retryable=True,
        )
        before = len(record.events)

        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.reconcile_in_flight()
        self.assertEqual(len(record.events), before)
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason,
            "partial_writer_failure_requires_review",
        )

    async def test_checker_unknown_state_retries_same_hashed_target(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        state = record.snapshot()
        step = next(
            item
            for item in state["step_revisions"]
            if item["step_revision_id"] == "step_0001"
        )
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Recovery retry fixture.",
        )
        state = record.snapshot()
        branch = next(
            item for item in state["branches"] if item["branch_id"] == "br_0001"
        )
        request = CheckRequest(
            run_id=orchestrator.config.run_id,
            check_id="check_0001",
            task_text=TASK_TEXT,
            target=orchestrator._step_snapshot(step),
            transcript=orchestrator._transcript(state, branch["step_revision_ids"]),
        )
        start = record.start_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            target={"check_id": "check_0001"},
            prompt=orchestrator._check_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0002",
            branch_id="br_0001",
            role="checker",
            target_kind="check",
            target_id="check_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_checker(request)
        control.attach_invocation(orchestrator.config.run_id, "call_0002", invocation)
        orchestrator._sync_head()
        runtime.forget_operation(invocation.operation_id)

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "checker"
            and call["target"] == {"check_id": "check_0001"}
        ]
        self.assertEqual([call["state"] for call in calls], ["failed", "finished"])
        check = next(
            item for item in canonical["checks"] if item["check_id"] == "check_0001"
        )
        self.assertEqual(check["checker_call_id"], calls[1]["model_call_id"])
        self.assertEqual(check["target_output_sha256"], step["output_sha256"])

    async def test_finished_checker_is_completed_without_rerunning_target(self) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        state = record.snapshot()
        step = next(
            item
            for item in state["step_revisions"]
            if item["step_revision_id"] == "step_0001"
        )
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Finished checker crash-window fixture.",
        )
        state = record.snapshot()
        branch = next(
            item for item in state["branches"] if item["branch_id"] == "br_0001"
        )
        request = CheckRequest(
            run_id=orchestrator.config.run_id,
            check_id="check_0001",
            task_text=TASK_TEXT,
            target=orchestrator._step_snapshot(step),
            transcript=orchestrator._transcript(state, branch["step_revision_ids"]),
        )
        start = record.start_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            target={"check_id": "check_0001"},
            prompt=orchestrator._check_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0002",
            branch_id="br_0001",
            role="checker",
            target_kind="check",
            target_id="check_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_checker(request)
        control.attach_invocation(orchestrator.config.run_id, "call_0002", invocation)
        orchestrator._sync_head()
        output = await runtime.collect_checker(invocation)
        body = canonical_json(
            {
                "verdict": output.verdict,
                "reason": output.reason,
                "evidence": [item.to_record() for item in output.evidence],
            }
        )
        record.finish_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )

        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=_runtime(),
            record=record,
            control=control,
        )
        query = await restarted.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "checker"
            and call["target"] == {"check_id": "check_0001"}
        ]
        self.assertEqual([call["model_call_id"] for call in calls], ["call_0002"])
        check = next(
            item for item in canonical["checks"] if item["check_id"] == "check_0001"
        )
        self.assertEqual(check["checker_call_id"], "call_0002")
        self.assertEqual(check["verdict"], output.verdict)

    async def test_failed_checker_terminal_event_repairs_instrument_failure(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components(retries=0)
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        step = self._step_by_id(record.snapshot(), "step_0001")
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Failed checker crash-window fixture.",
        )
        start = record.start_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            target={"check_id": "check_0001"},
            prompt="failed-checker-fixture",
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0002",
            branch_id="br_0001",
            role="checker",
            target_kind="check",
            target_id="check_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        record.fail_model_call(
            model_call_id="call_0002",
            role=ModelRole.CHECKER,
            failure_kind="unknown_provider_state",
            message="checker operation vanished",
            partial_output="",
            retryable=True,
        )

        restarted = DerivationOrchestrator(
            config=orchestrator.config,
            task_text=TASK_TEXT,
            runtime=_runtime(),
            record=record,
            control=control,
        )
        with self.assertRaisesRegex(
            RuntimeInvariantError, "blocks scientific advancement"
        ):
            await restarted.reconcile_in_flight()
        self.assertEqual(control.run(orchestrator.config.run_id).phase, "error")
        canonical = record.verify_complete()
        self.assertEqual(canonical["candidates"], [])
        check = next(
            item for item in canonical["checks"] if item["check_id"] == "check_0001"
        )
        self.assertEqual(check["verdict"], "instrument_failure")
        self.assertEqual(check["checker_call_id"], "call_0002")
        calls = [
            item
            for item in canonical["model_calls"]
            if item["role"] == "checker"
            and item["target"] == {"check_id": "check_0001"}
        ]
        self.assertEqual([item["model_call_id"] for item in calls], ["call_0002"])

    async def test_judge_unknown_state_retries_same_candidate_target(self) -> None:
        orchestrator, record, control, runtime = self.components(retries=1)
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        state = record.snapshot()
        step = next(
            item
            for item in state["step_revisions"]
            if item["step_revision_id"] == "step_0001"
        )
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Judge retry fixture prerequisite.",
        )
        await orchestrator._run_existing_check("br_0001", "check_0001")
        record.change_branch_status(
            branch_id="br_0001",
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        record.declare_candidate(
            candidate_id="cand_0001",
            branch_id="br_0001",
            reason="Judge retry fixture candidate.",
        )
        candidate = next(
            item
            for item in record.snapshot()["candidates"]
            if item["candidate_id"] == "cand_0001"
        )
        record.request_judgement(
            judgement_id="judge_0001",
            candidate_id="cand_0001",
            candidate_sha256=candidate["transcript_sha256"],
            reason="Judge retry fixture.",
        )
        state = record.snapshot()
        request = JudgeRequest(
            run_id=orchestrator.config.run_id,
            judgement_id="judge_0001",
            candidate_id="cand_0001",
            task_text=TASK_TEXT,
            transcript=orchestrator._transcript(
                state, candidate["transcript_step_revision_ids"]
            ),
        )
        start = record.start_model_call(
            model_call_id="call_0003",
            role=ModelRole.JUDGE,
            target={"judgement_id": "judge_0001"},
            prompt=orchestrator._judge_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0003",
            branch_id="br_0001",
            role="judge",
            target_kind="judgement",
            target_id="judge_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_judge(request)
        control.attach_invocation(orchestrator.config.run_id, "call_0003", invocation)
        orchestrator._sync_head()
        runtime.forget_operation(invocation.operation_id)

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "judge"
            and call["target"] == {"judgement_id": "judge_0001"}
        ]
        self.assertEqual([call["state"] for call in calls], ["failed", "finished"])
        judgement = next(
            item
            for item in canonical["judgements"]
            if item["judgement_id"] == "judge_0001"
        )
        self.assertEqual(judgement["judge_call_id"], calls[1]["model_call_id"])
        self.assertEqual(
            judgement["candidate_transcript_sha256"], candidate["transcript_sha256"]
        )

    async def test_finished_judge_is_completed_without_rerunning_target(self) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        state = record.snapshot()
        step = next(
            item
            for item in state["step_revisions"]
            if item["step_revision_id"] == "step_0001"
        )
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Finished judge crash-window prerequisite.",
        )
        await orchestrator._run_existing_check("br_0001", "check_0001")
        record.change_branch_status(
            branch_id="br_0001",
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        record.declare_candidate(
            candidate_id="cand_0001",
            branch_id="br_0001",
            reason="Finished judge crash-window candidate.",
        )
        candidate = next(
            item
            for item in record.snapshot()["candidates"]
            if item["candidate_id"] == "cand_0001"
        )
        record.request_judgement(
            judgement_id="judge_0001",
            candidate_id="cand_0001",
            candidate_sha256=candidate["transcript_sha256"],
            reason="Finished judge crash-window fixture.",
        )
        state = record.snapshot()
        request = JudgeRequest(
            run_id=orchestrator.config.run_id,
            judgement_id="judge_0001",
            candidate_id="cand_0001",
            task_text=TASK_TEXT,
            transcript=orchestrator._transcript(
                state, candidate["transcript_step_revision_ids"]
            ),
        )
        start = record.start_model_call(
            model_call_id="call_0003",
            role=ModelRole.JUDGE,
            target={"judgement_id": "judge_0001"},
            prompt=orchestrator._judge_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0003",
            branch_id="br_0001",
            role="judge",
            target_kind="judgement",
            target_id="judge_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_judge(request)
        control.attach_invocation(orchestrator.config.run_id, "call_0003", invocation)
        orchestrator._sync_head()
        output = await runtime.collect_judge(invocation)
        body = canonical_json(
            {"verdict": output.verdict, "reason": output.reason, "score": output.score}
        )
        record.finish_model_call(
            model_call_id="call_0003",
            role=ModelRole.JUDGE,
            output_text=body,
            finish_reason=output.finish_reason,
            usage=output.usage.to_record(),
        )

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        calls = [
            call
            for call in canonical["model_calls"]
            if call["role"] == "judge"
            and call["target"] == {"judgement_id": "judge_0001"}
        ]
        self.assertEqual([call["model_call_id"] for call in calls], ["call_0003"])
        judgement = next(
            item
            for item in canonical["judgements"]
            if item["judgement_id"] == "judge_0001"
        )
        self.assertEqual(judgement["judge_call_id"], "call_0003")
        self.assertEqual(judgement["verdict"], output.verdict)

    async def test_failed_judge_terminal_event_repairs_instrument_failure(self) -> None:
        orchestrator, record, control, _ = self.components(retries=0)
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        step = self._step_by_id(record.snapshot(), "step_0001")
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Failed judge crash-window prerequisite.",
        )
        await orchestrator._run_existing_check("br_0001", "check_0001")
        record.change_branch_status(
            branch_id="br_0001",
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        record.declare_candidate(
            candidate_id="cand_0001",
            branch_id="br_0001",
            reason="Failed judge crash-window candidate.",
        )
        candidate = next(
            item
            for item in record.snapshot()["candidates"]
            if item["candidate_id"] == "cand_0001"
        )
        record.request_judgement(
            judgement_id="judge_0001",
            candidate_id="cand_0001",
            candidate_sha256=candidate["transcript_sha256"],
            reason="Failed judge crash-window fixture.",
        )
        start = record.start_model_call(
            model_call_id="call_0003",
            role=ModelRole.JUDGE,
            target={"judgement_id": "judge_0001"},
            prompt="failed-judge-fixture",
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0003",
            branch_id="br_0001",
            role="judge",
            target_kind="judgement",
            target_id="judge_0001",
            attempt=1,
            record_start_seq=start["seq"],
        )
        record.fail_model_call(
            model_call_id="call_0003",
            role=ModelRole.JUDGE,
            failure_kind="unknown_provider_state",
            message="judge operation vanished",
            partial_output="",
            retryable=True,
        )

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        judgement = next(
            item
            for item in canonical["judgements"]
            if item["judgement_id"] == "judge_0001"
        )
        self.assertEqual(judgement["verdict"], "instrument_failure")
        self.assertEqual(judgement["judge_call_id"], "call_0003")
        calls = [
            item
            for item in canonical["model_calls"]
            if item["role"] == "judge"
            and item["target"] == {"judgement_id": "judge_0001"}
        ]
        self.assertEqual([item["model_call_id"] for item in calls], ["call_0003"])

    async def test_completed_branch_and_eligible_candidate_prefixes_are_repaired(
        self,
    ) -> None:
        orchestrator, record, _control, _ = self.components()
        await orchestrator.initialize("Explore the root route.")
        await orchestrator._run_writer("br_0001")
        step = self._step_by_id(record.snapshot(), "step_0001")
        record.request_check(
            check_id="check_0001",
            step_revision_id="step_0001",
            output_sha256=step["output_sha256"],
            reason="Candidate repair prerequisite.",
        )
        await orchestrator._run_existing_check("br_0001", "check_0001")
        record.change_branch_status(
            branch_id="br_0001",
            from_status="active",
            to_status="completed",
            reason_code="writer_complete",
            actor=RecordV1Writer.ACTORS[ModelRole.WRITER],
        )
        orchestrator._sync_head()

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["candidate_count"], 1)
        self.assertEqual(canonical["candidates"][0]["branch_id"], "br_0001")
        self.assertEqual(canonical["candidates"][0]["status"], "eligible")
        self.assertEqual(canonical["summary"]["judgement_count"], 1)
        self.assertEqual(canonical["judgements"][0]["state"], "completed")

    async def test_exact_call_cap_applies_complete_and_candidate_transitions(
        self,
    ) -> None:
        runtime = _runtime()
        runtime.writer_outputs[("br_0001", 1)] = _writer_output(
            "cap-terminal", WriterDecision.COMPLETE
        )
        orchestrator, record, control, _ = self.components(
            max_model_calls=2,
            runtime=runtime,
        )
        query = await orchestrator.submit("Explore the root route.")
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY_DUE_TO_CAP)
        canonical = record.verify_complete()
        self.assertEqual(canonical["summary"]["model_call_count"], 2)
        self.assertEqual(canonical["branches"][0]["status"], "completed")
        self.assertEqual(canonical["summary"]["candidate_count"], 1)
        self.assertEqual(canonical["candidates"][0]["status"], "eligible")
        self.assertEqual(canonical["judgements"][0]["state"], "requested")
        self.assertEqual(
            control.run(orchestrator.config.run_id).stop_reason, "max_model_calls"
        )

    async def test_cap_exhausted_human_branches_fail_before_record_mutation(
        self,
    ) -> None:
        orchestrator, record, _, _ = self.components(max_model_calls=2)
        query = await orchestrator.submit("Explore the root route.")
        self.assertEqual(query.status().phase, RunPhase.REVIEW_READY_DUE_TO_CAP)
        before = len(record.events)
        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.expand_from_step(
                parent_branch_id="br_0001",
                step_revision_id="step_0001",
                direction="This branch cannot run under an exhausted frozen cap.",
                actor_id="fixture-user",
                reason="Exercise cap fail-closed behavior.",
            )
        self.assertEqual(len(record.events), before)
        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.revise_step(
                parent_branch_id="br_0001",
                step_revision_id="step_0001",
                replacement=_step("cap-exhausted-replacement"),
                actor_id="fixture-user",
                reason="Exercise revision cap fail-closed behavior.",
            )
        self.assertEqual(len(record.events), before)

    async def test_reconcile_repairs_unsealed_human_revision_before_writer(
        self,
    ) -> None:
        orchestrator, record, control, _ = self.components()
        await orchestrator.submit("Explore the root route.")
        replacement = _step("crash-window-human-replacement")
        action = record.record_human_action(
            actor_id="fixture-user",
            action_id="act_0001",
            action="revise_step",
            target={"step_revision_id": "step_0002"},
            reason="Crash after branch creation and before replacement sealing.",
            content=canonical_json(replacement.to_record()),
        )
        record.create_child_branch(
            branch_id="br_0003",
            parent_branch_id="br_0001",
            fork_mode="replace",
            anchor_step_revision_id="step_0002",
            inherited_step_revision_ids=["step_0001"],
            hypothesis="Human replacement of step_0002: crash recovery fixture",
            hypothesis_source="human",
            hypothesis_source_event_id=action["event_id"],
            created_reason="human_revision",
            human_action_id="act_0001",
            actor={"kind": "human", "id": "fixture-user"},
        )
        control.upsert_branch(
            orchestrator.config.run_id,
            "br_0003",
            runtime_state="active",
            provider_session_id=None,
            provider_lineage=None,
            last_operation_id=None,
            last_step_revision_id="step_0001",
            attempt=0,
            last_error=None,
        )
        orchestrator._sync_head()

        query = await orchestrator.reconcile_in_flight()
        self.assertIsNotNone(query)
        canonical = record.verify_complete()
        branch = next(
            item for item in canonical["branches"] if item["branch_id"] == "br_0003"
        )
        repaired = next(
            item
            for item in canonical["step_revisions"]
            if item["step_revision_id"] == branch["step_revision_ids"][1]
        )
        self.assertEqual(repaired["origin"]["kind"], "human")
        self.assertEqual(repaired["origin"]["human_action_id"], "act_0001")
        self.assertEqual(repaired["replaces_step_revision_id"], "step_0002")
        self.assertEqual(repaired["content"], replacement.to_record())
        self.assertFalse(
            any(
                call["role"] == "writer"
                and call["target"] == {"branch_id": "br_0003", "step_slot": 2}
                for call in canonical["model_calls"]
            )
        )

    async def test_hard_interrupt_records_abort_and_parks_zero_body_writer(
        self,
    ) -> None:
        orchestrator, record, control, runtime = self.components()
        await orchestrator.initialize("Explore the root route.")
        request = WriterRequest(
            run_id=orchestrator.config.run_id,
            branch_id="br_0001",
            step_slot=1,
            task_text=TASK_TEXT,
            hypothesis="Explore the root route.",
            transcript=(),
        )
        start = record.start_model_call(
            model_call_id="call_0001",
            role=ModelRole.WRITER,
            target={"branch_id": "br_0001", "step_slot": 1},
            prompt=orchestrator._writer_prompt(request),
        )
        control.start_call(
            run_id=orchestrator.config.run_id,
            model_call_id="call_0001",
            branch_id="br_0001",
            role="writer",
            target_kind="writer_step",
            target_id="br_0001:1",
            attempt=1,
            record_start_seq=start["seq"],
        )
        invocation = await runtime.start_writer(request, None)
        control.attach_invocation(orchestrator.config.run_id, "call_0001", invocation)
        orchestrator._sync_head()

        await orchestrator.hard_interrupt(
            model_call_id="call_0001",
            actor_id="fixture-user",
            reason="Stop immediately for the hard-interrupt fixture.",
        )
        canonical = record.verify_complete()
        self.assertEqual(canonical["model_calls"][0]["state"], "aborted")
        self.assertEqual(canonical["branches"][0]["status"], "parked")
        self.assertEqual(canonical["human_actions"][0]["action"], "abort_model_call")
        control.rebuild_from_snapshot(
            canonical,
            credential_profile_id=orchestrator.config.credential_profile_id,
            phase=RunPhase.REVIEW_READY,
            stop_reason=None,
        )
        rebuilt = control.call(orchestrator.config.run_id, "call_0001")
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.state, "aborted")
        self.assertEqual(control.in_flight_calls(orchestrator.config.run_id), [])

    async def test_invalid_human_anchor_fails_before_record_mutation(self) -> None:
        orchestrator, record, _, _ = self.components()
        await orchestrator.submit("Explore the root route.")
        before = len(record.events)
        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.expand_from_step(
                parent_branch_id="br_0001",
                step_revision_id="step_missing",
                direction="Invalid direction",
                actor_id="fixture-user",
                reason="Exercise fail-closed behavior.",
            )
        self.assertEqual(len(record.events), before)
        with self.assertRaises(RuntimeInvariantError):
            await orchestrator.revise_step(
                parent_branch_id="br_0001",
                step_revision_id="step_missing",
                replacement=_step("invalid-anchor-replacement"),
                actor_id="fixture-user",
                reason="Exercise fail-closed replacement anchoring.",
            )
        self.assertEqual(len(record.events), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
