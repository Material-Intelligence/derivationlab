"""Service isolation and diagnostics regressions for checker recovery."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from derivation_api.models import CreateRunRequest

from derivation_app.factory import deterministic_runtime, fake_create_run_defaults
from derivation_app.service import RuntimeDerivationService
from derivation_app.tests.test_vertical_integration import ROOT, run_request
from derivation_runtime.control import ControlStore
from derivation_runtime.record import RecordV1Writer


class FailingWriter:
    def __init__(self, config):
        self.inner = deterministic_runtime(config)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def collect_writer(self, invocation):
        raise RuntimeError("private provider content must not enter operational log")


class CheckerRecoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_repairs_finished_checker_bookmark_without_model_or_event_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {
                "run_root": root,
                "storage_root": root,
                "repo_root": ROOT,
                "code_commit": "0" * 40,
                "credential_profile_id": "fake-profile",
                "create_run_defaults": fake_create_run_defaults(),
            }
            service = RuntimeDerivationService(
                **settings,
                runtime_factory=lambda config, _: deterministic_runtime(config),
            )
            await service.start()
            try:
                with patch.object(
                    RecordV1Writer,
                    "complete_check",
                    side_effect=RuntimeError("injected materialization failure"),
                ):
                    created = await service.create_run(
                        CreateRunRequest.model_validate(run_request()),
                        idempotency_key=None,
                    )
                    await service.wait_for_phase(created.id, {"error"}, timeout=5)
                context = service._context(created.id)
                checker = next(
                    call
                    for call in context.record.snapshot()["model_calls"]
                    if call["role"] == "checker"
                )
                self.assertEqual(checker["state"], "finished")
                self.assertEqual(
                    context.control.call(created.id, checker["model_call_id"]).state,
                    "finished",
                )
                evidence = service.event_log_path(created.id).read_bytes()
                manifest_path = root / created.id / "manifest.json"
                manifest = manifest_path.read_bytes()
            finally:
                await service.close()

            # Reproduce the old release's stale SQLite state on synthetic data.
            with ControlStore(root / created.id / "control.sqlite") as control:
                with control.transaction() as connection:
                    connection.execute(
                        "UPDATE runtime_calls SET state='running', record_terminal_seq=NULL WHERE model_call_id=?",
                        (checker["model_call_id"],),
                    )
                control.sync_record_head(created.id, 0, None)

            def forbidden_factory(config, path):
                raise AssertionError("historical error must not start any runtime")

            for _ in range(2):
                restored = RuntimeDerivationService(
                    **settings, runtime_factory=forbidden_factory
                )
                await restored.start()
                try:
                    context = restored._context(created.id)
                    self.assertEqual(
                        (await restored.get_run(created.id)).phase, "error"
                    )
                    self.assertEqual(
                        context.control.call(
                            created.id, checker["model_call_id"]
                        ).state,
                        "finished",
                    )
                    self.assertEqual(
                        context.control.run(created.id).record_event_seq,
                        len(context.record.events),
                    )
                    self.assertEqual(
                        restored.event_log_path(created.id).read_bytes(), evidence
                    )
                    self.assertEqual(manifest_path.read_bytes(), manifest)
                finally:
                    await restored.close()

    async def test_failed_run_is_readable_and_does_not_degrade_service(self):
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            service = RuntimeDerivationService(
                run_root=run_root,
                storage_root=run_root,
                repo_root=ROOT,
                runtime_factory=lambda config, _: FailingWriter(config),
                code_commit="0" * 40,
                credential_profile_id="fake-profile",
                create_run_defaults=fake_create_run_defaults(),
                run_id_factory=lambda: "run_failure_isolation",
            )
            self.assertEqual((await service.health()).status, "degraded")
            await service.start()
            try:
                with self.assertLogs("derivation_app.service", level="ERROR") as logs:
                    created = await service.create_run(
                        CreateRunRequest.model_validate(run_request()),
                        idempotency_key=None,
                    )
                    failed = await service.wait_for_phase(
                        created.id, {"error"}, timeout=5
                    )
                self.assertEqual(failed.phase, "error")
                self.assertEqual((await service.get_run(created.id)).phase, "error")
                self.assertEqual((await service.health()).status, "ok")
                record = logs.records[0]
                self.assertEqual(record.run_id, created.id)
                self.assertEqual(record.failure_code, "runtime_error")
                self.assertEqual(record.error_type, "RuntimeError")
                self.assertEqual(record.release_id, "development")
                self.assertNotIn("private provider content", "\n".join(logs.output))
                self.assertIsNone(record.exc_info)
                events = service.event_log_path(created.id).read_bytes()
            finally:
                await service.close()
            self.assertEqual((await service.health()).status, "degraded")

            calls = []

            def forbidden_factory(config, path):
                calls.append(path)
                raise AssertionError("historical error must never launch a provider")

            restored = RuntimeDerivationService(
                run_root=run_root,
                storage_root=run_root,
                repo_root=ROOT,
                runtime_factory=forbidden_factory,
                code_commit="0" * 40,
                credential_profile_id="fake-profile",
                create_run_defaults=fake_create_run_defaults(),
                preserve_provider_handles_on_start=True,
            )
            await restored.start()
            try:
                await asyncio.sleep(0)
                self.assertEqual((await restored.get_run(created.id)).phase, "error")
                self.assertEqual((await restored.health()).status, "ok")
                self.assertEqual(
                    restored.event_log_path(created.id).read_bytes(), events
                )
                self.assertEqual(calls, [])
                restored._closing = True
                self.assertEqual((await restored.health()).status, "degraded")
                restored._closing = False
                restored._close_failed = True
                self.assertEqual((await restored.health()).status, "degraded")
                restored._close_failed = False
            finally:
                await restored.close()
