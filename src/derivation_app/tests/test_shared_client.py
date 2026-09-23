"""Holder-level tests: one profile lock, one child, many isolated runs."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from derivation_app.factory import ProductRuntimeFactory
from derivation_app.product_profile import (
    HolderWorkspaceMapping,
    ProductProfile,
    RunWorkspaceMapping,
    prepare_shared_run_launch,
)
from derivation_app.shared_client import SharedProfileClientHolder
from derivation_runtime.app_server_client import DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL
from derivation_runtime.app_server_runtime import LaunchSettings
from derivation_runtime.capabilities import BENCHMARK_SYMBOLIC_V1, SOURCE_READING_V1
from derivation_runtime.launch_gate import AppServerCommand, GateResult, GateStatus
from derivation_runtime.platform_policy import PlatformFamily
from derivation_runtime.test_app_server_runtime import config
from derivation_runtime.types import RuntimeInvariantError

ROOT = Path(__file__).resolve().parents[3]


def _command(cwd: Path) -> AppServerCommand:
    return AppServerCommand(
        argv=("/fixture/codex", "app-server"),
        cwd=str(cwd),
        environment={"HOME": "/fixture/home"},
    )


class SharedLaunchSettingsTests(unittest.TestCase):
    def _settings(self, *, workspace: Path, process_workspace: Path | None):
        return LaunchSettings(
            workspace=workspace,
            gate_result=GateResult(
                status=GateStatus.SUPPORTED, stage="model_turn", issues=()
            ),
            authorized_command=_command(
                workspace if process_workspace is None else process_workspace
            ),
            process_workspace=process_workspace,
        )

    def test_shared_settings_keep_the_run_workspace_out_of_the_holder(self) -> None:
        settings = self._settings(
            workspace=Path("/fixture/profile/workspaces/run_a"),
            process_workspace=Path("/fixture/profile/workspaces/holder_x"),
        )

        self.assertEqual(settings.workspace.name, "run_a")
        self.assertEqual(
            settings.authorized_command.cwd,
            "/fixture/profile/workspaces/holder_x",
        )

    def test_exclusive_settings_still_require_cwd_to_equal_the_workspace(self) -> None:
        with self.assertRaisesRegex(
            RuntimeInvariantError, "cwd differs from workspace"
        ):
            LaunchSettings(
                workspace=Path("/fixture/profile/workspaces/run_a"),
                gate_result=GateResult(
                    status=GateStatus.SUPPORTED, stage="model_turn", issues=()
                ),
                authorized_command=_command(Path("/fixture/elsewhere")),
            )

    def test_a_run_workspace_may_not_live_inside_the_holder_child(self) -> None:
        holder = Path("/fixture/profile/workspaces/holder_x")
        with self.assertRaisesRegex(RuntimeInvariantError, "must not contain a run"):
            self._settings(workspace=holder / "run_a", process_workspace=holder)


class PrepareSharedRunLaunchTests(unittest.TestCase):
    def test_shared_launch_keeps_the_holder_command_and_the_run_workspace(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="shared-launch-", dir=ROOT / "runs") as temp:
            root = Path(temp)
            profile = ProductProfile.below(root / "profile")
            holder_workspace = profile.workspaces / "holder_x"
            run_workspace = profile.workspaces / "run_a"
            evidence = root / "runs" / "run_a"
            for path in (
                holder_workspace / ".runtime_tmp",
                run_workspace / ".runtime_tmp",
                evidence,
            ):
                path.mkdir(parents=True)
            holder = SimpleNamespace(
                profile=profile,
                validation=object(),
                workspace_mapping=HolderWorkspaceMapping(
                    "holder_x", holder_workspace, holder_workspace / ".runtime_tmp"
                ),
                request=SimpleNamespace(
                    roots=SimpleNamespace(
                        repository=str(root.resolve()),
                        workspace=str(holder_workspace),
                    )
                ),
                command=_command(holder_workspace),
                capability_profile=SOURCE_READING_V1,
                lock=Mock(held=True),
            )
            run_mapping = RunWorkspaceMapping(
                "run_a", evidence, run_workspace, run_workspace / ".runtime_tmp"
            )

            prepared = prepare_shared_run_launch(
                holder,  # type: ignore[arg-type]
                run_mapping=run_mapping,
                repo_root=root,
                evidence_root=root / "runs",
            )

        self.assertTrue(prepared.shared)
        self.assertEqual(prepared.session_workspace, run_workspace)
        self.assertEqual(prepared.command, holder.command)
        self.assertEqual(prepared.workspace_mapping.run_id, "run_a")


class SharedProfileClientHolderTests(unittest.IsolatedAsyncioTestCase):
    def _holder(self, profile: ProductProfile) -> SharedProfileClientHolder:
        return SharedProfileClientHolder(
            profile=profile,
            repo_root=ROOT,
            platform=PlatformFamily.MACOS,
            architecture="arm64",
            app_server_executable=Path("/fixture/codex"),
            user_home=Path("/fixture/user"),
            capability_profile=SOURCE_READING_V1,
        )

    async def test_one_lock_one_child_and_a_run_release_keeps_it_alive(self) -> None:
        with TemporaryDirectory(prefix="holder-", dir=ROOT / "runs") as temp:
            root = Path(temp)
            profile = ProductProfile.below(root / "profile")
            holder_workspace = profile.workspaces / "holder_fixture"
            run_workspace = profile.workspaces / "run_a"
            evidence = root / "runs" / "run_a"
            for path in (
                holder_workspace / ".runtime_tmp",
                run_workspace / ".runtime_tmp",
                evidence,
            ):
                path.mkdir(parents=True)
            lock = Mock(held=True, runtime_owned=False)
            lock.acquire.return_value = lock
            client = Mock(is_running=True, returncode=None)
            client.start = AsyncMock()
            client.close = AsyncMock()
            client.account_read = AsyncMock(
                return_value={"account": {"type": "chatgpt"}}
            )
            prepared = SimpleNamespace(
                profile=profile,
                validation=object(),
                workspace_mapping=HolderWorkspaceMapping(
                    "holder_fixture",
                    holder_workspace,
                    holder_workspace / ".runtime_tmp",
                ),
                request=SimpleNamespace(
                    roots=SimpleNamespace(
                        repository=str(ROOT), workspace=str(holder_workspace)
                    )
                ),
                command=_command(holder_workspace),
                capability_profile=SOURCE_READING_V1,
                lock=lock,
            )
            broker = Mock()
            sessions: list[Mock] = []

            async def acquire_session(
                *, workspace, dynamic_tool_audit_path=None, on_release=None
            ):
                session = Mock(workspace=Path(workspace), on_release=on_release)
                session.close = AsyncMock(
                    side_effect=lambda: on_release() if on_release else None
                )
                sessions.append(session)
                return session

            broker.acquire_session = acquire_session
            broker.process_events = ()

            holder = self._holder(profile)
            holder.holder_id = "holder_fixture"
            with (
                patch(
                    "derivation_app.shared_client.ProfileInstanceLock",
                    return_value=lock,
                ) as lock_type,
                patch(
                    "derivation_app.shared_client.prepare_holder_workspace",
                    return_value=prepared.workspace_mapping,
                ),
                patch(
                    "derivation_app.shared_client.prepare_product_launch",
                    return_value=prepared,
                ),
                patch(
                    "derivation_app.shared_client.AppServerClient",
                    return_value=client,
                ) as client_type,
                patch(
                    "derivation_app.shared_client.SharedAppServerBroker",
                    return_value=broker,
                ),
            ):
                await holder.start()
                await holder.start()
                run_mapping = RunWorkspaceMapping(
                    "run_a", evidence, run_workspace, run_workspace / ".runtime_tmp"
                )
                run_prepared, session = await holder.acquire_run(
                    run_mapping=run_mapping,
                    dynamic_tool_audit_path=evidence / "dynamic_tool_calls.jsonl",
                )
                self.assertEqual(holder.leases, 1)
                await session.close()
                self.assertEqual(holder.leases, 0)
                # A finished run must not end the shared child or the lock.
                client.close.assert_not_awaited()
                lock.release.assert_not_called()

                client.is_running = False
                client.returncode = 0
                await holder.close()

        self.assertEqual(lock_type.call_count, 1)
        self.assertEqual(client_type.call_count, 1)
        self.assertTrue(run_prepared.shared)
        self.assertEqual(run_prepared.session_workspace, run_workspace)
        # The keepalive lease is what the holder itself releases at close.
        self.assertEqual(len(sessions), 2)
        lock.release.assert_called_once()


class SharedProfileClientHolderBudgetTests(unittest.IsolatedAsyncioTestCase):
    """The child's stdout budget is cumulative, so it is sized per holder."""

    def _holder(self, profile: ProductProfile, **kwargs) -> SharedProfileClientHolder:
        return SharedProfileClientHolder(
            profile=profile,
            repo_root=ROOT,
            platform=PlatformFamily.MACOS,
            architecture="arm64",
            app_server_executable=Path("/fixture/codex"),
            user_home=Path("/fixture/user"),
            capability_profile=SOURCE_READING_V1,
            **kwargs,
        )

    def test_a_single_run_holder_keeps_the_clients_own_default(self) -> None:
        holder = self._holder(ProductProfile.below(ROOT / "runs" / "budget-profile"))

        self.assertEqual(holder.expected_runs, 1)
        self.assertEqual(holder.protocol_bytes_budget, 64 * 1024 * 1024)
        self.assertEqual(
            holder.protocol_bytes_budget, DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL
        )

    def test_expected_runs_must_be_a_positive_int(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "budget-profile")

        for value in (0, -8):
            with self.assertRaisesRegex(ValueError, "positive number of runs"):
                self._holder(profile, expected_runs=value)
        for value in (8.0, "8", True, None):
            with self.assertRaisesRegex(TypeError, "must be an int"):
                self._holder(profile, expected_runs=value)

    async def test_a_round_of_eight_runs_buys_eight_times_the_budget(self) -> None:
        with TemporaryDirectory(prefix="budget-", dir=ROOT / "runs") as temp:
            root = Path(temp)
            profile = ProductProfile.below(root / "profile")
            holder_workspace = profile.workspaces / "holder_fixture"
            (holder_workspace / ".runtime_tmp").mkdir(parents=True)
            lock = Mock(held=True, runtime_owned=False)
            lock.acquire.return_value = lock
            client = Mock(is_running=True, returncode=None)
            client.start = AsyncMock()
            client.close = AsyncMock()
            client.account_read = AsyncMock(
                return_value={"account": {"type": "chatgpt"}}
            )
            mapping = HolderWorkspaceMapping(
                "holder_fixture", holder_workspace, holder_workspace / ".runtime_tmp"
            )
            prepared = SimpleNamespace(
                profile=profile,
                workspace_mapping=mapping,
                command=_command(holder_workspace),
                capability_profile=SOURCE_READING_V1,
                lock=lock,
            )
            broker = Mock(process_events=())
            broker.acquire_session = AsyncMock(return_value=Mock(close=AsyncMock()))

            holder = self._holder(profile, expected_runs=8)
            with (
                patch(
                    "derivation_app.shared_client.ProfileInstanceLock",
                    return_value=lock,
                ),
                patch(
                    "derivation_app.shared_client.prepare_holder_workspace",
                    return_value=mapping,
                ),
                patch(
                    "derivation_app.shared_client.prepare_product_launch",
                    return_value=prepared,
                ),
                patch(
                    "derivation_app.shared_client.AppServerClient",
                    return_value=client,
                ) as client_type,
                patch(
                    "derivation_app.shared_client.SharedAppServerBroker",
                    return_value=broker,
                ),
            ):
                await holder.start()
                client.is_running = False
                client.returncode = 0
                await holder.close()

        budget = client_type.call_args.kwargs["max_protocol_bytes_total"]
        self.assertEqual(budget, 8 * DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL)
        self.assertEqual(budget, holder.protocol_bytes_budget)


class HolderBackedRuntimeFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_borrows_a_session_and_never_locks_the_profile(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        session = Mock()
        prepared = SimpleNamespace(command=_command(ROOT), shared=True)
        lease = object()
        holder = Mock(
            capability_profile=BENCHMARK_SYMBOLIC_V1,
            client=Mock(is_running=True),
        )
        holder.acquire_run = AsyncMock(return_value=(prepared, session))
        evidence_directory = ROOT / "runs" / config().run_id

        with (
            patch("derivation_app.factory.provision_product_profile"),
            patch(
                "derivation_app.factory.prepare_run_workspace", return_value=object()
            ),
            patch("derivation_app.factory.ProfileInstanceLock") as lock_type,
            patch(
                "derivation_app.factory.create_shared_product_runtime",
                new=AsyncMock(return_value=lease),
            ) as create_runtime,
        ):
            factory = ProductRuntimeFactory(
                profile=profile,
                repo_root=ROOT,
                platform=PlatformFamily.MACOS,
                architecture="arm64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/user"),
                holder=holder,
            )
            result = await factory(config(), evidence_directory)

        self.assertIs(result, lease)
        lock_type.assert_not_called()
        holder.acquire_run.assert_awaited_once()
        self.assertEqual(
            holder.acquire_run.await_args.kwargs["dynamic_tool_audit_path"],
            evidence_directory / "dynamic_tool_calls.jsonl",
        )
        self.assertIs(create_runtime.await_args.kwargs["session"], session)

    async def test_capability_mismatch_with_the_holder_fails_closed(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        holder = Mock(capability_profile=SOURCE_READING_V1)
        holder.acquire_run = AsyncMock()

        with (
            patch("derivation_app.factory.provision_product_profile"),
            patch(
                "derivation_app.factory.prepare_run_workspace", return_value=object()
            ),
        ):
            factory = ProductRuntimeFactory(
                profile=profile,
                repo_root=ROOT,
                platform=PlatformFamily.MACOS,
                architecture="arm64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/user"),
                holder=holder,
            )
            with self.assertRaisesRegex(
                RuntimeInvariantError, "differs from the shared App Server holder"
            ):
                await factory(config(), ROOT / "runs" / config().run_id)

        holder.acquire_run.assert_not_awaited()

    def test_a_host_exclusive_lease_cannot_share_a_child(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        with (
            patch("derivation_app.factory.provision_product_profile"),
            self.assertRaisesRegex(ValueError, "cannot share an App Server child"),
        ):
            ProductRuntimeFactory(
                profile=profile,
                repo_root=ROOT,
                platform=PlatformFamily.MACOS,
                architecture="arm64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/user"),
                host_run_lease_path=ROOT / "runs" / "host.lease",
                holder=Mock(capability_profile=BENCHMARK_SYMBOLIC_V1),
            )


if __name__ == "__main__":
    unittest.main()
