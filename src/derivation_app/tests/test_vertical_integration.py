from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from derivation_api.models import (
    CreateBranchRequest,
    CreateIntakeSessionRequest,
    CreateRunRequest,
    FinalizeIntakeSessionRequest,
    IntakeAnswerInput,
    IntakeRevisionRequest,
    SubmitIntakeRoundRequest,
)
from derivation_api.service import DerivationServiceError, ErrorKind
from fastapi.testclient import TestClient

from derivation_agent_record import canonical_json
from derivation_app.app_server_intake_session import (
    IntakeRoundMode,
    SpecificationAudit,
)
from derivation_app.factory import (
    _deterministic_intake_question,
    _DeterministicIntakeSessionAdvisor,
    _DeterministicSpecificationAuditor,
    create_fake_service,
    create_http_app,
    deterministic_runtime,
    fake_create_run_defaults,
)
from derivation_app.intake_session import SQLiteIntakeStore
from derivation_app.intake_session_service import PersistentIntakeSessionService
from derivation_app.product_profile import ProfileLockHeld
from derivation_app.projection import _edge_kind
from derivation_app.service import RuntimeDerivationService, _confirmed_intake_problem
from derivation_runtime.platform_policy import PINNED_CODEX_VERSION
from derivation_runtime.query import ReplayQuery
from derivation_runtime.types import (
    ReconcileResult,
    ReconcileStatus,
    RunConfig,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeSession,
    WriterOutput,
)

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "runs"


def run_request() -> dict[str, Any]:
    return {
        "problem": {
            "problem_id": "bounded-toy-invariant",
            "version": 1,
            "supersedes_version": None,
            "objective": "Construct deterministic routes for a bounded toy invariant.",
            "givens": ["The deterministic fixture is available."],
            "assumptions": [],
            "scope": "Application integration only.",
            "deliverable": "A checked route tree.",
            "allowed_tools": [],
            "allowed_references": [],
            "success_criteria": ["At least one route reaches terminal judgement."],
            "source_pack": None,
            "confirmed_by_user": True,
        },
        "config": {
            "granularity": "one_task",
            "writer": {
                "provider": "fake",
                "model": "fake:writer",
                "effort": "deterministic",
            },
            "checker": {
                "provider": "fake",
                "model": "fake:checker",
                "effort": "deterministic",
            },
            "judge": {
                "provider": "fake",
                "model": "fake:judge",
                "effort": "deterministic",
            },
            "backend": {"name": "deterministic-fake-runtime", "version": "1"},
            "max_model_calls": 64,
            "max_active_branches": 2,
            "reference_allowed": False,
            "allowed_paths": [],
        },
        "runtime": {
            "auth_mode": "chatgpt",
            "concurrency": 1,
            "retries": 0,
            "max_run_seconds": None,
        },
    }


def wait_http(client: TestClient, run_id: str, predicate, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if predicate(payload):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach the expected HTTP state")


class GateWriterRuntime:
    """Pause the first writer collect after its provider handle is attached."""

    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._gated = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        if not self._gated:
            self._gated = True
            self.started.set()
            await self.release.wait()
        return await self.inner.collect_writer(invocation)


class GateProviderHandleRuntime:
    """Expose the starting-to-interruptible provider-handle transition."""

    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.starting = asyncio.Event()
        self.release_start = asyncio.Event()
        self.collecting = asyncio.Event()
        self.release_collect = asyncio.Event()

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def start_writer(self, request, session):  # type: ignore[no-untyped-def]
        self.starting.set()
        await self.release_start.wait()
        return await self.inner.start_writer(request, session)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        self.collecting.set()
        await self.release_collect.wait()
        return await self.inner.collect_writer(invocation)


class ThreadGateWriterRuntime:
    """A TestClient-thread gate for HTTP pause/resume coverage."""

    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.started = threading.Event()
        self.release = threading.Event()
        self._gated = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        if not self._gated:
            self._gated = True
            self.started.set()
            await asyncio.to_thread(self.release.wait)
        return await self.inner.collect_writer(invocation)


class FailCompletionService(RuntimeDerivationService):
    """Inject one crash at the effect-to-idempotency-completion boundary."""

    def __init__(self, *args, fail_scope: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.fail_scope = fail_scope
        self.failed = False

    def _idempotency_store(self, scope, key, payload, response) -> None:  # type: ignore[no-untyped-def]
        if key is not None and scope.startswith(self.fail_scope) and not self.failed:
            self.failed = True
            raise RuntimeError("injected crash before idempotency completion")
        super()._idempotency_store(scope, key, payload, response)


class RunningThenCompletedRuntime:
    """Keep provider state across a simulated service-process restart."""

    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.first_collect_started = asyncio.Event()
        self._first_collect = True
        self.reconcile_calls = 0

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        if self._first_collect:
            self._first_collect = False
            self.first_collect_started.set()
            await asyncio.Event().wait()
        return await self.inner.collect_writer(invocation)

    async def reconcile(self, invocation: RuntimeInvocation) -> ReconcileResult:
        self.reconcile_calls += 1
        if self.reconcile_calls == 1:
            return ReconcileResult(
                status=ReconcileStatus.RUNNING,
                output=None,
                partial_output="",
                failure_kind=None,
                message=None,
                retryable=False,
            )
        output = await self.inner.collect_writer(invocation)
        return ReconcileResult(
            status=ReconcileStatus.COMPLETED,
            output=output,
            partial_output="",
            failure_kind=None,
            message=None,
            retryable=False,
        )


class RegistrationRequiredRuntime:
    """Require restored Branch ownership before any resumed writer turn."""

    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.registered: dict[str, RuntimeSession] = {}
        self.session_owners: dict[str, str] = {}
        self.recovered_starts: list[tuple[str, RuntimeSession]] = []

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    def register_recovered_writer_session(
        self,
        session: RuntimeSession,
        branch_id: str,
    ) -> None:
        existing = self.registered.get(branch_id)
        if existing is not None and existing != session:
            raise RuntimeInvariantError("Branch recovered with a different session")
        owner = self.session_owners.get(session.session_id)
        if owner is not None and owner != branch_id:
            raise RuntimeInvariantError("recovered session belongs to another Branch")
        self.registered[branch_id] = session
        self.session_owners[session.session_id] = branch_id

    async def start_writer(self, request, session):  # type: ignore[no-untyped-def]
        if session is not None and session.session_id in self.session_owners:
            if self.registered.get(request.branch_id) != session:
                raise RuntimeInvariantError(
                    "resumed writer turn started before restored Branch ownership"
                )
            self.recovered_starts.append((request.branch_id, session))
        return await self.inner.start_writer(request, session)


class ExplodingWriterRuntime:
    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        raise RuntimeInvariantError("injected provider invariant failure")


class ProviderConfiguredFakeRuntime:
    """Exercise the provider-neutral Engine under a non-fake RunConfig."""

    def __init__(self, config: RunConfig) -> None:
        fake_config = replace(
            config,
            writer=replace(
                config.writer,
                provider="fake",
                model="fake:writer",
                effort="deterministic",
            ),
            checker=replace(
                config.checker,
                provider="fake",
                model="fake:checker",
                effort="deterministic",
            ),
            judge=replace(
                config.judge,
                provider="fake",
                model="fake:judge",
                effort="deterministic",
            ),
            backend_name="deterministic-fake-runtime",
            backend_version="1",
        )
        self.inner = deterministic_runtime(fake_config)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)


class CloseFailsOnceRuntime:
    def __init__(self, config: RunConfig) -> None:
        self.inner = deterministic_runtime(config)
        self.close_calls = 0

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("injected close failure")


class RetryableLeaseStyleRuntime:
    def __init__(self) -> None:
        self.close_calls = 0

    @property
    def close_failed(self) -> bool:
        return self.close_calls == 1

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("late health failure after child exit")


class SingleRuntimeOwner:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def acquire(self) -> None:
        if self.active:
            raise ProfileLockHeld("single test profile is already owned")
        self.active = 1
        self.max_active = max(self.max_active, self.active)

    def release(self) -> None:
        if self.active != 1:
            raise RuntimeError("single test profile ownership was lost")
        self.active = 0


class SingleOwnerRuntime:
    def __init__(
        self,
        config: RunConfig,
        owner: SingleRuntimeOwner,
        *,
        gate_first_writer: bool = False,
    ) -> None:
        owner.acquire()
        self.inner = deterministic_runtime(config)
        self.owner = owner
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gate_first_writer = gate_first_writer
        self._gated = False
        self._closed = False
        self.close_calls = 0

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        if self.gate_first_writer and not self._gated:
            self._gated = True
            self.started.set()
            await self.release.wait()
        return await self.inner.collect_writer(invocation)

    async def close(self) -> None:
        self.close_calls += 1
        if not self._closed:
            self._closed = True
            self.owner.release()


class SingleOwnerIntakeAdvisor:
    def __init__(self, owner: SingleRuntimeOwner) -> None:
        self.owner = owner
        self.inner = _DeterministicIntakeSessionAdvisor()

    async def advance(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.owner.acquire()
        try:
            return await self.inner.advance(*args, **kwargs)
        finally:
            self.owner.release()

    async def rehydrate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.owner.acquire()
        try:
            return await self.inner.rehydrate(*args, **kwargs)
        finally:
            self.owner.release()


class _BlockingOnceSpecificationAuditor:
    """Reject the first candidate specification, then behave normally."""

    def __init__(self) -> None:
        self.calls = 0

    async def audit(self, specification, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls > 1 or kwargs.get("mode") is IntakeRoundMode.FINALIZE:
            return SpecificationAudit(
                passed=True,
                public_summary="The specification is executable.",
                blocking_questions=(),
                thread_id="thread-fixture-audit",
                turn_id=f"turn-audit-{specification.version}",
            )
        return SpecificationAudit(
            passed=False,
            public_summary="One target-defining ambiguity remains.",
            blocking_questions=(
                replace(
                    _deterministic_intake_question(
                        "audit-regime",
                        title="Included physics",
                        prompt="Should phonon-assisted transitions be inside the target?",
                    ),
                    grounded_in="absorption coefficient",
                ),
            ),
            thread_id="thread-fixture-audit",
            turn_id=f"turn-audit-{specification.version}",
        )


class HttpVerticalIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="derivation-app-http-", dir=RUNS)
        self.run_root = Path(self.temp.name) / "runs"
        self.run_root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persistent_intake_session_survives_service_restart(self) -> None:
        first_service = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
        )
        with TestClient(create_http_app(first_service)) as client:
            created_response = client.post(
                "/api/intake/sessions",
                json={
                    "initial_message": "Derive the bounded tensor response.",
                    "model": "deterministic",
                    "effort": "none",
                },
                headers={"Idempotency-Key": "persistent-intake-create"},
            )
            self.assertEqual(created_response.status_code, 201, created_response.text)
            created = created_response.json()
            self.assertEqual(created["model"], "deterministic")
            self.assertEqual(created["effort"], "none")
            answers = {
                question["decision_id"]: {
                    "selected_option_ids": ["recommended"],
                    "custom_text": (
                        "DECISION_PROJECTION_SENTINEL" if index == 0 else None
                    ),
                }
                for index, question in enumerate(created["frontier"])
            }
            round_response = client.post(
                f"/api/intake/sessions/{created['session_id']}/rounds",
                json={"base_revision": created["revision"], "answers": answers},
                headers={"Idempotency-Key": "persistent-intake-round"},
            )
            self.assertEqual(round_response.status_code, 200, round_response.text)
            candidate = round_response.json()
            self.assertEqual(candidate["status"], "candidate_ready")

        restarted_service = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
        )
        with TestClient(create_http_app(restarted_service)) as client:
            resumed = client.get(f"/api/intake/sessions/{candidate['session_id']}")
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(resumed.json(), candidate)
            confirmed = client.post(
                f"/api/intake/sessions/{candidate['session_id']}/confirm",
                json={"base_revision": candidate["revision"]},
                headers={"Idempotency-Key": "persistent-intake-confirm"},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["status"], "confirmed")
            assert restarted_service.intake_session_service is not None
            frozen = _confirmed_intake_problem(
                restarted_service.intake_session_service.get(candidate["session_id"])
            )
            self.assertIn("DECISION_PROJECTION_SENTINEL", frozen.task_text())
            task_text = frozen.task_text()
            self.assertIn("## Refinement ladder", task_text)
            self.assertIn(
                "- rung 0 — Textbook-simplest baseline — "
                "Nothing; this is the comparable baseline.",
                task_text,
            )
            self.assertLess(
                task_text.index("rung 0 —"),
                task_text.index("rung 1 —"),
            )
            self.assertIn("## Declared defaults", task_text)
            self.assertIn("- Unit system: Work in SI units throughout.", task_text)
            intake_events = restarted_service.intake_session_service.store.events(
                candidate["session_id"]
            )
            intake_thread_id = candidate["thread_generations"][0][
                "app_server_thread_id"
            ]
            self.assertTrue(
                any(
                    event.payload.get("thread_id") == intake_thread_id
                    for event in intake_events
                )
            )
            self.assertNotIn(intake_thread_id, frozen.task_text())
            mismatched = run_request()
            mismatched["problem"]["problem_id"] = candidate["session_id"]
            rejected = client.post("/api/runs", json=mismatched)
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertEqual(
                rejected.json()["error"]["code"],
                "intake_run_projection_mismatch",
            )
            request = run_request()
            request["problem"] = frozen.model_dump(mode="json")
            created_run = client.post(
                "/api/runs",
                json=request,
                headers={"Idempotency-Key": "run-from-persistent-intake"},
            )
            self.assertEqual(created_run.status_code, 201, created_run.text)
            intake_directory = self.run_root / created_run.json()["id"] / "intake"
            self.assertEqual(
                {path.name for path in intake_directory.iterdir()},
                {
                    "problem_specification.json",
                    "decision_log.json",
                    "conversation.jsonl",
                    "handoff_manifest.json",
                },
            )
            manifest = json.loads(
                (intake_directory / "handoff_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["run_id"], created_run.json()["id"])
            self.assertEqual(manifest["schema_version"], "intake-handoff-v3")
            self.assertTrue(manifest["decision_log"]["latest_decision_revisions"])
            self.assertEqual(
                manifest["conversation_archive"]["terminal_event"]["kind"],
                "session_confirmed",
            )
            self.assertEqual(
                manifest["conversation_archive"]["confirmation_event_id"],
                manifest["conversation_archive"]["terminal_event"]["event_id"],
            )
            for name, digest in manifest["files"].items():
                self.assertEqual(
                    hashlib.sha256((intake_directory / name).read_bytes()).hexdigest(),
                    digest,
                )
            completed_run = wait_http(
                client,
                created_run.json()["id"],
                lambda value: value["phase"] == "review_ready",
            )
            writer_thread_ids = {
                step["provenance"]["threadId"] for step in completed_run["steps"]
            }
            intake_thread_ids = {
                generation["thread_id"] for generation in manifest["thread_generations"]
            }
            self.assertTrue(writer_thread_ids)
            self.assertTrue(writer_thread_ids.isdisjoint(intake_thread_ids))
            replayed_run = client.post(
                "/api/runs",
                json=request,
                headers={"Idempotency-Key": "run-from-persistent-intake-retry"},
            )
            self.assertEqual(replayed_run.status_code, 201, replayed_run.text)
            self.assertEqual(replayed_run.json()["id"], created_run.json()["id"])
            changed_request = copy.deepcopy(request)
            changed_request["config"]["max_model_calls"] += 1
            changed = client.post(
                "/api/runs",
                json=changed_request,
                headers={"Idempotency-Key": "run-from-persistent-intake-changed"},
            )
            self.assertEqual(changed.status_code, 409, changed.text)
            self.assertEqual(
                changed.json()["error"]["code"],
                "intake_run_already_created",
            )

    def stage_archive_run(self, run_id: str, *, objective: str) -> Path:
        source_root = Path(self.temp.name) / "archive-source"
        source_root.mkdir(exist_ok=True)
        service = create_fake_service(
            run_root=source_root,
            repo_root=ROOT,
            run_id_factory=lambda: run_id,
        )
        request = run_request()
        request["problem"]["objective"] = objective
        with TestClient(create_http_app(service)) as client:
            created = client.post("/api/runs", json=request)
            self.assertEqual(created.status_code, 201, created.text)
            wait_http(
                client,
                run_id,
                lambda value: value["phase"] == "review_ready",
            )
        archive = Path(self.temp.name) / "archive" / "benchmarks" / run_id
        archive.parent.mkdir(parents=True, exist_ok=True)
        (source_root / run_id).rename(archive)
        return archive

    def test_explicit_fake_server_has_a_distinct_health_identity(self) -> None:
        service = create_fake_service(run_root=self.run_root, repo_root=ROOT)
        with TestClient(create_http_app(service)) as client:
            self.assertEqual(
                client.get("/healthz").json(),
                {"status": "ok", "service": "derivation-runtime-adapter-fake"},
            )

    def test_archive_catalog_is_strict_read_only_and_keeps_active_runs_writable(
        self,
    ) -> None:
        archive_id = "run_archive_drude"
        archive = self.stage_archive_run(
            archive_id,
            objective="Derive the Drude conductivity sigma_xx(omega).",
        )
        archive_root = Path(self.temp.name) / "archive"

        old = archive_root / "legacy" / "run_old_manifest"
        old.mkdir(parents=True)
        (old / "manifest.json").write_text(
            json.dumps({"schema_version": "derivation-app-run-manifest-v1"}),
            encoding="utf-8",
        )
        (old / "events.jsonl").write_text("", encoding="utf-8")

        invalid = self.stage_archive_run(
            "run_invalid_record",
            objective="This archive will have a broken Record V1 log.",
        )
        (invalid / "events.jsonl").write_text("not-json\n", encoding="utf-8")

        before = {
            path.relative_to(archive): path.read_bytes()
            for path in archive.iterdir()
            if path.is_file()
        }
        active_root = Path(self.temp.name) / "active"
        active_root.mkdir()
        service = RuntimeDerivationService(
            run_root=active_root,
            archive_root=archive_root,
            repo_root=ROOT,
            runtime_factory=lambda config, _directory: deterministic_runtime(config),
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_active_catalog",
            service_identity="derivation-runtime-adapter-fake",
            preserve_provider_handles_on_start=False,
        )

        with TestClient(create_http_app(service)) as client:
            catalog = client.get("/api/runs")
            self.assertEqual(catalog.status_code, 200, catalog.text)
            self.assertEqual(
                [item["id"] for item in catalog.json()],
                [archive_id],
            )
            self.assertTrue(catalog.json()[0]["read_only"])
            self.assertIn("sigma_xx", catalog.json()[0]["question"])

            archived_view = client.get(f"/api/runs/{archive_id}")
            self.assertEqual(archived_view.status_code, 200, archived_view.text)
            self.assertGreater(len(archived_view.json()["steps"]), 0)
            self.assertTrue(archived_view.json()["read_only"])
            self.assertEqual(
                archived_view.json()["commands"],
                {
                    "can_pause": False,
                    "can_resume": False,
                    "can_interrupt": False,
                    "branchable_step_revision_ids": [],
                },
            )
            replay = client.get(f"/api/runs/{archive_id}/events?follow=false")
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.text.startswith("retry:"))
            anchor = archived_view.json()["steps"][0]["revisionId"]
            commands = [
                ("pause", None),
                ("resume", None),
                ("interrupt", None),
                (
                    "branches",
                    {
                        "from_step_revision_id": anchor,
                        "kind": "human_direction",
                        "instruction": "Try a different gauge-safe route.",
                    },
                ),
            ]
            for command, payload in commands:
                response = client.post(
                    f"/api/runs/{archive_id}/{command}",
                    json=payload,
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "archive_run_read_only",
                )

            active_request = run_request()
            active_request["config"]["max_active_branches"] = 4
            created = client.post("/api/runs", json=active_request)
            self.assertEqual(created.status_code, 201, created.text)
            active = wait_http(
                client,
                "run_active_catalog",
                lambda value: value["phase"] == "review_ready",
            )
            active_anchor = active["steps"][0]["revisionId"]
            branch = client.post(
                "/api/runs/run_active_catalog/branches",
                json={
                    "from_step_revision_id": active_anchor,
                    "kind": "human_direction",
                    "instruction": "Exercise the writable active path.",
                },
            )
            self.assertEqual(branch.status_code, 201, branch.text)

            refreshed = client.get("/api/runs").json()
            self.assertEqual(refreshed[0]["id"], "run_active_catalog")
            self.assertFalse(refreshed[0]["read_only"])
            self.assertEqual(
                {item["id"] for item in refreshed}, {archive_id, "run_active_catalog"}
            )

        after = {
            path.relative_to(archive): path.read_bytes()
            for path in archive.iterdir()
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_submit_tree_human_direction_and_revision_preserve_record(self) -> None:
        service = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
            run_id_factory=lambda: "run_app_e2e",
        )
        app = create_http_app(service)
        with TestClient(app) as client:
            created = client.post(
                "/api/runs",
                json=run_request(),
                headers={"Idempotency-Key": "submit-e2e"},
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.json()["phase"], "submitted")
            run_id = created.json()["id"]
            initial = wait_http(
                client,
                run_id,
                lambda value: value["phase"] == "review_ready",
            )
            self.assertEqual(len(initial["branches"]), 2)
            self.assertEqual(len(initial["routes"]), 2)
            self.assertEqual(
                {item["status"] for item in initial["routes"]}, {"complete"}
            )

            initial_bytes = service.event_log_path(run_id).read_bytes()
            initial_canonical = service.strict_canonical(run_id)
            root = next(
                item
                for item in initial_canonical["branches"]
                if item["branch_id"] == "br_0001"
            )
            old_step_hashes = {
                item["step_revision_id"]: item["output_sha256"]
                for item in initial_canonical["step_revisions"]
                if item["step_revision_id"] in root["step_revision_ids"]
            }
            old_candidate = next(
                item
                for item in initial_canonical["candidates"]
                if item["branch_id"] == "br_0001"
            )

            direction = client.post(
                f"/api/runs/{run_id}/branches",
                json={
                    "from_step_revision_id": "step_0001",
                    "kind": "human_direction",
                    "instruction": "Re-evaluate the second step from an explicit conservation law.",
                },
                headers={"Idempotency-Key": "direction-e2e"},
            )
            self.assertEqual(direction.status_code, 201, direction.text)
            after_direction = wait_http(
                client,
                run_id,
                lambda value: (
                    value["phase"] == "review_ready" and len(value["branches"]) == 3
                ),
            )
            direction_bytes = service.event_log_path(run_id).read_bytes()
            self.assertTrue(direction_bytes.startswith(initial_bytes))
            self.assertIn(
                "human_direction",
                {item["kind"] for item in after_direction["branches"]},
            )

            replacement = {
                "claim": "The human replacement keeps the toy invariant bounded.",
                "why": "The original second step hid a boundary assumption.",
                "source": "Direct human review of the sealed second step.",
                "derivation": "Replace only slot two and retain the exact first-step prefix.",
                "scope": "The bounded deterministic fixture only.",
            }
            revision = client.post(
                f"/api/runs/{run_id}/branches",
                json={
                    "from_step_revision_id": "step_0002",
                    "kind": "human_revision",
                    "instruction": json.dumps(
                        replacement, ensure_ascii=False, indent=2
                    ),
                },
                headers={"Idempotency-Key": "revision-e2e"},
            )
            self.assertEqual(revision.status_code, 201, revision.text)
            final = wait_http(
                client,
                run_id,
                lambda value: (
                    value["phase"] == "review_ready" and len(value["branches"]) == 4
                ),
            )
            final_bytes = service.event_log_path(run_id).read_bytes()
            self.assertTrue(final_bytes.startswith(direction_bytes))
            revision_branch = next(
                item for item in final["branches"] if item["kind"] == "human_revision"
            )
            replacement_step = next(
                item
                for item in final["steps"]
                if item["branch_id"] == revision_branch["branch_id"]
                and item["content"] == replacement
            )
            self.assertIsNone(replacement_step["provenance"])

            final_canonical = service.strict_canonical(run_id)
            preserved_root = next(
                item
                for item in final_canonical["branches"]
                if item["branch_id"] == "br_0001"
            )
            self.assertEqual(
                preserved_root["step_revision_ids"], root["step_revision_ids"]
            )
            self.assertEqual(
                {
                    item["step_revision_id"]: item["output_sha256"]
                    for item in final_canonical["step_revisions"]
                    if item["step_revision_id"] in preserved_root["step_revision_ids"]
                },
                old_step_hashes,
            )
            preserved_candidate = next(
                item
                for item in final_canonical["candidates"]
                if item["candidate_id"] == old_candidate["candidate_id"]
            )
            self.assertEqual(
                preserved_candidate["transcript_sha256"],
                old_candidate["transcript_sha256"],
            )

            replay_one = ReplayQuery.from_event_log(
                service.event_log_path(run_id)
            ).canonical
            replay_two = ReplayQuery.from_event_log(
                service.event_log_path(run_id)
            ).canonical
            self.assertEqual(canonical_json(replay_one), canonical_json(replay_two))
            self.assertEqual(replay_one, final_canonical)

            stream = client.get(f"/api/runs/{run_id}/events?follow=false")
            self.assertEqual(stream.status_code, 200, stream.text)
            payloads = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith("data: ")
            ]
            live = next(item for item in payloads if item["overlay"]["activeCalls"])
            self.assertEqual(live["run"]["steps"], [])
            self.assertEqual(live["run"]["canonical_event_id"], 2)
            self.assertGreater(
                final["canonical_event_id"], live["run"]["canonical_event_id"]
            )
            active_labels = {
                call["label"]
                for payload in payloads
                for call in payload["overlay"]["activeCalls"]
            }
            self.assertIn("Checker model call", active_labels)
            self.assertIn("Judge model call", active_labels)
            sealed = next(
                payload
                for payload in payloads
                if payload["type"] == "step.sealed" and payload["run"]["steps"]
            )
            self.assertIsNotNone(sealed["run"]["steps"][-1]["provenance"])

        restarted = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
            run_id_factory=lambda: "must_not_allocate_a_second_run",
        )
        with TestClient(create_http_app(restarted)) as client:
            replayed = client.post(
                "/api/runs",
                json=run_request(),
                headers={"Idempotency-Key": "submit-e2e"},
            )
            self.assertEqual(replayed.status_code, 201, replayed.text)
            self.assertEqual(replayed.json(), created.json())
            self.assertFalse(
                (self.run_root / "must_not_allocate_a_second_run").exists()
            )

    def test_runtime_contract_rejects_unsupported_modes_before_service(self) -> None:
        service = create_fake_service(run_root=self.run_root, repo_root=ROOT)
        with TestClient(create_http_app(service)) as client:
            invalid_values = (
                ("auth_mode", "api_key"),
                ("concurrency", 2),
                ("max_run_seconds", 1),
            )
            for field, value in invalid_values:
                payload = run_request()
                payload["runtime"][field] = value
                response = client.post("/api/runs", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(list(self.run_root.iterdir()), [])

    def test_http_resume_replays_same_idempotency_key(self) -> None:
        gates: list[ThreadGateWriterRuntime] = []

        def gated_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> ThreadGateWriterRuntime:
            runtime = ThreadGateWriterRuntime(config)
            gates.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=gated_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_http_resume",
            preserve_provider_handles_on_start=False,
        )
        with TestClient(create_http_app(service)) as client:
            created = client.post("/api/runs", json=run_request())
            self.assertEqual(created.status_code, 201, created.text)
            run_id = created.json()["id"]
            self.assertTrue(gates[0].started.wait(timeout=2.0))
            paused_request = client.post(f"/api/runs/{run_id}/pause")
            self.assertEqual(paused_request.status_code, 200, paused_request.text)
            gates[0].release.set()
            wait_http(client, run_id, lambda value: value["phase"] == "paused")

        resumed_service = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
        )
        with TestClient(create_http_app(resumed_service)) as client:
            resumed = client.post(
                f"/api/runs/{run_id}/resume",
                headers={"Idempotency-Key": "resume-http"},
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(resumed.headers["Idempotency-Key"], "resume-http")
            first_response = resumed.json()
            self.assertFalse(first_response["pause_requested"])
            wait_http(client, run_id, lambda value: value["phase"] == "review_ready")
            replayed = client.post(
                f"/api/runs/{run_id}/resume",
                headers={"Idempotency-Key": "resume-http"},
            )
            self.assertEqual(replayed.status_code, 200, replayed.text)
            self.assertEqual(replayed.json(), first_response)


class RuntimeControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(
            prefix="derivation-app-control-", dir=RUNS
        )
        self.run_root = Path(self.temp.name) / "runs"
        self.run_root.mkdir()
        self.command = CreateRunRequest.model_validate(run_request())

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_review_terminal_releases_owner_for_intake_and_second_run(
        self,
    ) -> None:
        owner = SingleRuntimeOwner()
        runtimes: list[SingleOwnerRuntime] = []
        run_ids = iter(("run_sequential_one", "run_sequential_two"))

        def owned_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> SingleOwnerRuntime:
            runtime = SingleOwnerRuntime(
                config,
                owner,
                gate_first_writer=not runtimes,
            )
            runtimes.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=owned_factory,
            intake_session_service=PersistentIntakeSessionService(
                store=SQLiteIntakeStore(
                    Path(self.temp.name) / "intakes" / "control.sqlite"
                ),
                advisor=SingleOwnerIntakeAdvisor(owner),
                auditor=_DeterministicSpecificationAuditor(),
            ),
            code_commit="0" * 40,
            credential_profile_id="single-owner-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: next(run_ids),
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        first = await service.create_run(self.command, idempotency_key=None)
        await asyncio.wait_for(runtimes[0].started.wait(), timeout=2.0)

        with self.assertRaises(DerivationServiceError) as busy:
            await service.create_intake_session(
                CreateIntakeSessionRequest(
                    initial_message="Derive a second bounded response.",
                    model="gpt-5.4",
                    effort="low",
                ),
                idempotency_key="intake-busy",
            )
        self.assertEqual(busy.exception.code, "intake_profile_busy")
        self.assertEqual(owner.active, 1)

        runtimes[0].release.set()
        await service.wait_for_phase(first.id, {"review_ready"}, timeout=5.0)
        first_context = service._runs[first.id]
        self.assertIsNone(first_context.runtime)
        self.assertIsNone(first_context.orchestrator)
        self.assertEqual(owner.active, 0)

        intake = await service.create_intake_session(
            CreateIntakeSessionRequest(
                initial_message="Derive a second bounded response.",
                model="gpt-5.4",
                effort="low",
            ),
            idempotency_key="intake-ready",
        )
        self.assertEqual(intake.status, "active")
        self.assertEqual(len(intake.frontier), 2)
        self.assertEqual(owner.active, 0)

        second = await service.create_run(self.command, idempotency_key=None)
        await service.wait_for_phase(second.id, {"review_ready"}, timeout=5.0)
        self.assertEqual(len(runtimes), 2)
        self.assertTrue(all(runtime.close_calls == 1 for runtime in runtimes))
        self.assertEqual(owner.active, 0)
        self.assertEqual(owner.max_active, 1)
        await service.close()

    async def test_terminal_intake_cleanup_is_idempotent_for_confirm_replay(
        self,
    ) -> None:
        cleanup_calls: list[str] = []
        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=deterministic_runtime,
            intake_session_service=PersistentIntakeSessionService(
                store=SQLiteIntakeStore(
                    Path(self.temp.name) / "intakes" / "control.sqlite"
                ),
                advisor=_DeterministicIntakeSessionAdvisor(),
                auditor=_DeterministicSpecificationAuditor(),
            ),
            intake_workspace_cleanup=cleanup_calls.append,
            code_commit="0" * 40,
            credential_profile_id="fixture-profile",
            create_run_defaults=fake_create_run_defaults(),
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        started = await service.create_intake_session(
            CreateIntakeSessionRequest(
                initial_message="Derive a bounded response.",
                model="gpt-5.4",
                effort="low",
            ),
            idempotency_key="intake-cleanup-start",
        )
        answers = {
            item.decision_id: IntakeAnswerInput(
                selected_option_ids=item.recommended_option_ids,
                custom_text=None,
            )
            for item in started.frontier
        }
        ready = await service.submit_intake_round(
            started.session_id,
            SubmitIntakeRoundRequest(
                base_revision=started.revision,
                answers=answers,
            ),
            idempotency_key="intake-cleanup-round",
        )
        command = IntakeRevisionRequest(base_revision=ready.revision)
        confirmed = await service.confirm_intake_session(
            started.session_id,
            command,
            idempotency_key="intake-cleanup-confirm",
        )
        replay = await service.confirm_intake_session(
            started.session_id,
            command,
            idempotency_key="intake-cleanup-confirm",
        )

        self.assertEqual(confirmed, replay)
        self.assertEqual(
            cleanup_calls,
            [started.session_id, started.session_id],
        )
        await service.close()

    async def test_refused_intake_round_logs_why_the_api_answered_conflict(
        self,
    ) -> None:
        """409 is all the browser sees, so the reason has to reach the log."""

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=deterministic_runtime,
            intake_session_service=PersistentIntakeSessionService(
                store=SQLiteIntakeStore(
                    Path(self.temp.name) / "intakes" / "control.sqlite"
                ),
                advisor=_DeterministicIntakeSessionAdvisor(),
                auditor=_DeterministicSpecificationAuditor(),
            ),
            code_commit="0" * 40,
            credential_profile_id="fixture-profile",
            create_run_defaults=fake_create_run_defaults(),
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        started = await service.create_intake_session(
            CreateIntakeSessionRequest(
                initial_message="Derive a bounded response.",
                model="gpt-5.4",
                effort="low",
            ),
            idempotency_key="intake-conflict-start",
        )
        stale = SubmitIntakeRoundRequest(
            base_revision=started.revision + 5,
            answers={
                item.decision_id: IntakeAnswerInput(
                    selected_option_ids=item.recommended_option_ids
                )
                for item in started.frontier
            },
        )

        with (
            self.assertLogs("derivation_app.service", level="WARNING") as logs,
            self.assertRaises(DerivationServiceError) as raised,
        ):
            await service.submit_intake_round(
                started.session_id,
                stale,
                idempotency_key="intake-conflict-round",
            )

        self.assertIs(raised.exception.kind, ErrorKind.INVALID_STATE)
        recorded = "\n".join(logs.output)
        self.assertIn("Intake command conflict", recorded)
        self.assertIn(f"expected revision {started.revision}", recorded)
        self.assertIn(f"received_base_revision={started.revision + 5}", recorded)
        self.assertIn(started.frontier[0].decision_id, recorded)
        self.assertIn(started.session_id, recorded)
        await service.close()

    async def test_convergence_required_session_finalizes_over_the_api_adapter(
        self,
    ) -> None:
        """The real state machine, not just the fixture, reaches and leaves the gate."""

        auditor = _BlockingOnceSpecificationAuditor()
        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=deterministic_runtime,
            intake_session_service=PersistentIntakeSessionService(
                store=SQLiteIntakeStore(
                    Path(self.temp.name) / "intakes" / "control.sqlite"
                ),
                advisor=_DeterministicIntakeSessionAdvisor(),
                auditor=auditor,
                max_audit_rejections=1,
            ),
            code_commit="0" * 40,
            credential_profile_id="fixture-profile",
            create_run_defaults=fake_create_run_defaults(),
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        started = await service.create_intake_session(
            CreateIntakeSessionRequest(
                initial_message="Derive the absorption coefficient.",
                model="gpt-5.4",
                effort="low",
            ),
            idempotency_key="intake-ladder-start",
        )
        stalled = await service.submit_intake_round(
            started.session_id,
            SubmitIntakeRoundRequest(
                base_revision=started.revision,
                answers={
                    item.decision_id: IntakeAnswerInput(strategy="simplest_first")
                    for item in started.frontier
                },
            ),
            idempotency_key="intake-ladder-round",
        )

        self.assertEqual(stalled.status, "convergence_required")
        self.assertEqual(stalled.frontier, [])
        self.assertEqual(stalled.convergence.audit_rejections, 1)
        self.assertEqual(stalled.convergence.reason, "max_audit_rejections")
        self.assertEqual(len(stalled.pending_problem_questions), 1)
        pending = stalled.pending_problem_questions[0]
        self.assertEqual(pending.decision_class, "problem")
        self.assertTrue(pending.why_it_matters)

        listed = await service.list_intake_sessions(status="convergence_required")
        self.assertEqual([item.session_id for item in listed], [started.session_id])

        finalized = await service.finalize_intake_session(
            started.session_id,
            FinalizeIntakeSessionRequest(
                base_revision=stalled.revision,
                answers={
                    pending.decision_id: IntakeAnswerInput(
                        selected_option_ids=["recommended"]
                    )
                },
            ),
            idempotency_key="intake-ladder-finalize",
        )

        self.assertEqual(finalized.status, "candidate_ready")
        self.assertEqual(finalized.pending_problem_questions, [])
        self.assertTrue(finalized.convergence.finalized_by_user)
        specification = finalized.problem_specifications[-1]
        self.assertEqual(
            [item.rung for item in specification.refinement_ladder], [0, 1]
        )
        self.assertEqual(
            [item.default_id for item in specification.declared_defaults],
            ["unit-system", "broadening"],
        )

        confirmed = await service.confirm_intake_session(
            started.session_id,
            IntakeRevisionRequest(base_revision=finalized.revision),
            idempotency_key="intake-ladder-confirm",
        )

        self.assertEqual(confirmed.status, "confirmed")
        assert confirmed.frozen_problem is not None
        self.assertIn("## Refinement ladder", confirmed.frozen_problem.task_text())
        self.assertIn(
            "Strategy: simplest_first",
            confirmed.frozen_problem.task_text(),
        )
        await service.close()

    async def test_human_branch_reallocates_and_rehydrates_after_terminal_release(
        self,
    ) -> None:
        owner = SingleRuntimeOwner()
        runtimes: list[SingleOwnerRuntime] = []

        def owned_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> SingleOwnerRuntime:
            runtime = SingleOwnerRuntime(config, owner)
            runtimes.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=owned_factory,
            code_commit="0" * 40,
            credential_profile_id="single-owner-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_terminal_rehydrate",
            preserve_provider_handles_on_start=True,
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        await service.wait_for_phase(created.id, {"review_ready"}, timeout=5.0)
        context = service._runs[created.id]
        self.assertIsNone(context.runtime)
        self.assertIsNone(context.orchestrator)
        self.assertEqual(runtimes[0].close_calls, 1)

        await service.create_branch(
            created.id,
            CreateBranchRequest.model_validate(
                {
                    "from_step_revision_id": "step_0001",
                    "kind": "human_direction",
                    "instruction": "Re-derive from the sealed first step.",
                }
            ),
            idempotency_key=None,
        )
        final = await service.wait_for_phase(
            created.id,
            {"review_ready", "error"},
            timeout=5.0,
        )

        self.assertEqual(final.phase, "review_ready", final.error_message)
        self.assertEqual(len(runtimes), 2)
        self.assertEqual(
            runtimes[1].inner.rehydrations,
            [("fake_session_0001", ("step_0001",))],
        )
        self.assertTrue(all(runtime.close_calls == 1 for runtime in runtimes))
        self.assertIsNone(context.runtime)
        self.assertIsNone(context.orchestrator)
        self.assertEqual(owner.active, 0)
        self.assertEqual(owner.max_active, 1)
        await service.close()

    async def test_soft_pause_restart_and_resume(self) -> None:
        gates: list[GateWriterRuntime] = []

        def gated_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> GateWriterRuntime:
            runtime = GateWriterRuntime(config)
            gates.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=gated_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_pause_restart",
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        await asyncio.wait_for(gates[0].started.wait(), timeout=2.0)
        requested = await service.pause_run(created.id, idempotency_key="pause-once")
        self.assertTrue(requested.pause_requested)
        self.assertFalse(requested.hard_interrupt_requested)
        gates[0].release.set()
        paused = await service.wait_for_phase(created.id, {"paused"}, timeout=5.0)
        self.assertEqual(paused.phase, "paused")
        self.assertTrue(all(item.status == "paused" for item in paused.branches))
        with self.assertRaises(DerivationServiceError) as interrupted:
            await service.interrupt_run(created.id, idempotency_key=None)
        self.assertEqual(interrupted.exception.code, "run_not_interruptible")
        self.assertEqual(
            interrupted.exception.details,
            {"phase": "paused", "active_calls": 0},
        )
        paused_bytes = service.event_log_path(created.id).read_bytes()
        before_restart_events = [
            item
            async for item in service.stream_events(
                created.id,
                after_event_id=0,
                follow=False,
            )
        ]
        before_restart_cursor = before_restart_events[-1].event_id
        await service.close()

        resumed_service = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
        )
        await resumed_service.start()
        restored = await resumed_service.get_run(created.id)
        self.assertEqual(restored.phase, "paused")
        reconnect = [
            item
            async for item in resumed_service.stream_events(
                created.id,
                after_event_id=before_restart_cursor,
                follow=False,
            )
        ]
        self.assertEqual(len(reconnect), 1)
        self.assertGreater(reconnect[0].event_id, before_restart_cursor)
        self.assertEqual(reconnect[0].run.phase, "paused")
        await resumed_service.resume_run(created.id, idempotency_key="resume-once")
        final = await resumed_service.wait_for_phase(
            created.id, {"review_ready"}, timeout=5.0
        )
        self.assertEqual(final.phase, "review_ready")
        self.assertFalse(final.pause_requested)
        self.assertTrue(
            resumed_service.event_log_path(created.id)
            .read_bytes()
            .startswith(paused_bytes)
        )
        self.assertEqual(
            ReplayQuery.from_event_log(
                resumed_service.event_log_path(created.id)
            ).canonical,
            resumed_service.strict_canonical(created.id),
        )
        await resumed_service.close()

    async def test_hard_interrupt_aborts_live_call_and_keeps_strict_replay(
        self,
    ) -> None:
        gates: list[GateWriterRuntime] = []

        def gated_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> GateWriterRuntime:
            runtime = GateWriterRuntime(config)
            gates.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=gated_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_hard_interrupt",
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        await asyncio.wait_for(gates[0].started.wait(), timeout=2.0)
        interrupted = await service.interrupt_run(
            created.id, idempotency_key="interrupt-once"
        )
        self.assertEqual(interrupted.phase, "interrupted")
        self.assertTrue(interrupted.hard_interrupt_requested)
        self.assertFalse(interrupted.pause_requested)
        canonical = service.strict_canonical(created.id)
        self.assertEqual(canonical["model_calls"][0]["state"], "aborted")
        self.assertEqual(canonical["branches"][0]["status"], "parked")
        self.assertEqual(
            ReplayQuery.from_event_log(service.event_log_path(created.id)).canonical,
            canonical,
        )
        events = [
            item
            async for item in service.stream_events(
                created.id,
                after_event_id=0,
                follow=False,
            )
        ]
        self.assertTrue(
            any(item.overlay and item.overlay.active_calls for item in events)
        )
        self.assertTrue(events[-1].overlay.hard_interrupt_requested)
        await service.close()

    async def test_interrupt_affordance_matches_provider_handle_readiness(
        self,
    ) -> None:
        runtimes: list[GateProviderHandleRuntime] = []

        def gated_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> GateProviderHandleRuntime:
            runtime = GateProviderHandleRuntime(config)
            runtimes.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=gated_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_interrupt_readiness",
            preserve_provider_handles_on_start=False,
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        runtime = runtimes[0]
        await asyncio.wait_for(runtime.starting.wait(), timeout=2.0)

        starting = await service.get_run(created.id)
        self.assertFalse(starting.commands.can_interrupt)
        with self.assertRaises(DerivationServiceError) as unavailable:
            await service.interrupt_run(created.id, idempotency_key=None)
        self.assertEqual(unavailable.exception.code, "interrupt_handle_not_ready")

        runtime.release_start.set()
        await asyncio.wait_for(runtime.collecting.wait(), timeout=2.0)
        ready = await service.get_run(created.id)
        self.assertTrue(ready.commands.can_interrupt)
        events = [
            item
            async for item in service.stream_events(
                created.id,
                after_event_id=0,
                follow=False,
            )
        ]
        self.assertTrue(any(item.run.commands.can_interrupt for item in events))
        interrupted = await service.interrupt_run(
            created.id, idempotency_key="interrupt-ready"
        )
        self.assertEqual(interrupted.phase, "interrupted")
        self.assertFalse(interrupted.commands.can_interrupt)
        await service.close()

    async def test_restart_registers_branch_session_before_resumed_turn(self) -> None:
        payload = run_request()
        payload["config"]["max_model_calls"] = 3
        command = CreateRunRequest.model_validate(payload)
        gates: list[GateWriterRuntime] = []

        def gated_factory(
            config: RunConfig,
            _evidence_directory: Path,
        ) -> GateWriterRuntime:
            runtime = GateWriterRuntime(config)
            gates.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=gated_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_recovered_writer_registration",
        )
        await service.start()
        created = await service.create_run(command, idempotency_key=None)
        await asyncio.wait_for(gates[0].started.wait(), timeout=2.0)
        await service.pause_run(created.id, idempotency_key="pause-registration")
        gates[0].release.set()
        await service.wait_for_phase(created.id, {"paused"}, timeout=5.0)
        await service.close()

        restored_runtimes: list[RegistrationRequiredRuntime] = []

        def restored_factory(
            config: RunConfig,
            _evidence_directory: Path,
        ) -> RegistrationRequiredRuntime:
            runtime = RegistrationRequiredRuntime(config)
            restored_runtimes.append(runtime)
            return runtime

        recovered = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=restored_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
        )
        await recovered.start()
        await recovered.resume_run(created.id, idempotency_key="resume-registration")
        final = await recovered.wait_for_phase(
            created.id,
            {"review_ready_due_to_cap", "error"},
            timeout=5.0,
        )
        self.assertEqual(final.phase, "review_ready_due_to_cap", final.error_message)
        self.assertEqual(len(restored_runtimes), 1)
        self.assertIsNone(recovered._runs[created.id].runtime)
        self.assertIsNone(recovered._runs[created.id].orchestrator)
        restored = restored_runtimes[0]
        self.assertIn("br_0001", restored.registered)
        self.assertEqual(
            restored.session_owners[restored.registered["br_0001"].session_id],
            "br_0001",
        )
        self.assertEqual(
            restored.recovered_starts,
            [("br_0001", restored.registered["br_0001"])],
        )
        await recovered.close()

    async def test_running_reconcile_is_polled_until_completed(self) -> None:
        runtimes: list[RunningThenCompletedRuntime] = []

        def persistent_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> RunningThenCompletedRuntime:
            if not runtimes:
                runtimes.append(RunningThenCompletedRuntime(config))
            return runtimes[0]

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=persistent_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_reconcile_poll",
            recovery_poll_initial_seconds=0.001,
            recovery_poll_max_seconds=0.002,
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        await asyncio.wait_for(runtimes[0].first_collect_started.wait(), timeout=2.0)
        context = service._runs[created.id]
        assert context.driver_task is not None
        context.driver_task.cancel()
        await asyncio.gather(context.driver_task, return_exceptions=True)
        await service.close()

        recovered = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=persistent_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            recovery_poll_initial_seconds=0.001,
            recovery_poll_max_seconds=0.002,
        )
        await recovered.start()
        final = await recovered.wait_for_phase(
            created.id, {"review_ready"}, timeout=5.0
        )
        self.assertEqual(final.phase, "review_ready")
        self.assertGreaterEqual(runtimes[0].reconcile_calls, 2)
        self.assertEqual(
            ReplayQuery.from_event_log(recovered.event_log_path(created.id)).canonical,
            recovered.strict_canonical(created.id),
        )
        await recovered.close()

    async def test_error_phase_is_durable_and_never_auto_recovers(self) -> None:
        def exploding_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> ExplodingWriterRuntime:
            return ExplodingWriterRuntime(config)

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=exploding_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_durable_error",
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        failed = await service.wait_for_phase(created.id, {"error"}, timeout=5.0)
        self.assertEqual(failed.error_message, "injected provider invariant failure")
        record_bytes = service.event_log_path(created.id).read_bytes()
        await service.close()

        recovered = create_fake_service(run_root=self.run_root, repo_root=ROOT)
        await recovered.start()
        await asyncio.sleep(0.05)
        restored = await recovered.get_run(created.id)
        self.assertEqual(restored.phase, "error")
        self.assertEqual(restored.error_message, "injected provider invariant failure")
        self.assertEqual(
            recovered.event_log_path(created.id).read_bytes(), record_bytes
        )
        await recovered.close()

    async def test_provider_configured_runtime_seals_without_extra_evidence_log(
        self,
    ) -> None:
        command_payload = run_request()
        for role in ("writer", "checker", "judge"):
            command_payload["config"][role] = {
                "provider": "openai",
                "model": f"gpt-test-{role}",
                "effort": "medium",
            }
        command_payload["config"]["backend"] = {
            "name": "codex-app-server",
            "version": PINNED_CODEX_VERSION,
        }
        command = CreateRunRequest.model_validate(command_payload)

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=lambda config, _directory: ProviderConfiguredFakeRuntime(
                config
            ),
            code_commit="0" * 40,
            credential_profile_id="provider-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_without_extra_evidence_log",
        )
        await service.start()
        created = await service.create_run(command, idempotency_key=None)
        final = await service.wait_for_phase(
            created.id, {"review_ready", "error"}, timeout=5.0
        )
        self.assertEqual(final.phase, "review_ready", final.error_message)
        event_types = [
            event["type"] for event in service._runs[created.id].record.events
        ]
        self.assertIn("model_call_started", event_types)
        self.assertIn("model_call_finished", event_types)
        self.assertIn("step_revision_sealed", event_types)
        self.assertFalse(
            (
                service._runs[created.id].directory / "execution_attestations.jsonl"
            ).exists()
        )
        await service.close()

    async def test_pending_create_intent_blocks_duplicate_after_restart(self) -> None:
        service = FailCompletionService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=lambda config, _path: deterministic_runtime(config),
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_pending_create",
            preserve_provider_handles_on_start=False,
            fail_scope="create_run",
        )
        await service.start()
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            await service.create_run(self.command, idempotency_key="create-crash")
        await service.close()

        restarted = create_fake_service(
            run_root=self.run_root,
            repo_root=ROOT,
            run_id_factory=lambda: "would_be_duplicate",
        )
        await restarted.start()
        with self.assertRaises(DerivationServiceError) as pending:
            await restarted.create_run(
                self.command,
                idempotency_key="create-crash",
            )
        self.assertEqual(pending.exception.code, "idempotent_command_pending")
        self.assertFalse((self.run_root / "would_be_duplicate").exists())
        self.assertEqual(
            sorted(path.name for path in self.run_root.glob("run_*")),
            ["run_pending_create"],
        )
        await restarted.close()

    async def test_pending_branch_intent_blocks_duplicate_after_restart(self) -> None:
        service = FailCompletionService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=lambda config, _path: deterministic_runtime(config),
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_pending_branch",
            preserve_provider_handles_on_start=False,
            fail_scope="create_branch:",
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        await service.wait_for_phase(created.id, {"review_ready"}, timeout=5.0)
        branch = CreateBranchRequest.model_validate(
            {
                "from_step_revision_id": "step_0001",
                "kind": "human_direction",
                "instruction": "Create exactly one crash-boundary route.",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            await service.create_branch(
                created.id,
                branch,
                idempotency_key="branch-crash",
            )
        await service.wait_for_phase(created.id, {"review_ready"}, timeout=5.0)
        self.assertEqual(len((await service.get_run(created.id)).branches), 3)
        await service.close()

        restarted = create_fake_service(run_root=self.run_root, repo_root=ROOT)
        await restarted.start()
        with self.assertRaises(DerivationServiceError) as pending:
            await restarted.create_branch(
                created.id,
                branch,
                idempotency_key="branch-crash",
            )
        self.assertEqual(pending.exception.code, "idempotent_command_pending")
        self.assertEqual(len((await restarted.get_run(created.id)).branches), 3)
        await restarted.close()

    async def test_shutdown_retries_an_advertised_closed_child_once(self) -> None:
        runtime = RetryableLeaseStyleRuntime()

        await RuntimeDerivationService._close_runtime(runtime)  # type: ignore[arg-type]

        self.assertEqual(runtime.close_calls, 2)
        self.assertFalse(runtime.close_failed)

    async def test_close_failure_retains_runtime_until_retry(self) -> None:
        runtimes: list[CloseFailsOnceRuntime] = []

        def close_failing_factory(
            config: RunConfig, _evidence_directory: Path
        ) -> CloseFailsOnceRuntime:
            runtime = CloseFailsOnceRuntime(config)
            runtimes.append(runtime)
            return runtime

        service = RuntimeDerivationService(
            run_root=self.run_root,
            repo_root=ROOT,
            runtime_factory=close_failing_factory,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            run_id_factory=lambda: "run_close_retry",
        )
        await service.start()
        created = await service.create_run(self.command, idempotency_key=None)
        context = service._runs[created.id]
        assert context.driver_task is not None
        await asyncio.wait_for(context.driver_task, timeout=5.0)

        self.assertEqual((await service.health()).status, "degraded")
        self.assertIs(context.runtime, runtimes[0])
        self.assertIsNotNone(context.orchestrator)
        self.assertEqual(runtimes[0].close_calls, 1)
        with self.assertRaises(DerivationServiceError) as unavailable:
            await service.get_run(created.id)
        self.assertEqual(unavailable.exception.code, "service_shutdown_incomplete")
        with self.assertRaisesRegex(RuntimeError, "retry close"):
            await service.start()

        await service.close()
        self.assertEqual(runtimes[0].close_calls, 2)
        await service.start()
        self.assertEqual((await service.get_run(created.id)).phase, "review_ready")
        await service.close()

    def test_instrument_retry_uses_supported_visual_edge_kind(self) -> None:
        branch = {
            "branch_id": "br_0002",
            "created_reason": "instrument_retry",
            "inherited_step_revision_ids": ["step_0001"],
            "step_revision_ids": ["step_0001", "step_0002"],
        }
        steps = {
            "step_0001": {"step_revision_id": "step_0001", "branch_id": "br_0001"},
            "step_0002": {"step_revision_id": "step_0002", "branch_id": "br_0002"},
        }
        self.assertEqual(_edge_kind(branch, steps["step_0002"], steps), "model_fork")


if __name__ == "__main__":
    unittest.main()
