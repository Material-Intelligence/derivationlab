from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from derivation_app.factory import ProductIntakeTurnRunner
from derivation_app.product_profile import IntakeWorkspaceMapping, ProductProfile
from derivation_runtime.app_server_structured_turn import StructuredTurnResult
from derivation_runtime.launch_gate import AppServerCommand
from derivation_runtime.platform_policy import PlatformFamily


class ProductIntakeRoundRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_round_keeps_workspace_but_releases_process_owner(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            workspace = profile.workspaces / "intake_session_1"
            runtime_temp = workspace / ".runtime_tmp"
            runtime_temp.mkdir(parents=True)
            mapping = IntakeWorkspaceMapping(
                "intake_session_1", workspace, runtime_temp
            )
            command = AppServerCommand(
                argv=("/fixture/codex", "app-server"),
                cwd=str(workspace),
                environment={"HOME": "/fixture/home"},
            )
            lock = Mock(held=True)
            lock.acquire.return_value = lock
            client = Mock(is_running=True, returncode=0)

            async def close_client() -> None:
                client.is_running = False

            client.start = AsyncMock()
            client.close = AsyncMock(side_effect=close_client)
            authorization = SimpleNamespace(
                settings=SimpleNamespace(
                    workspace=workspace,
                    permission_profile="strict_run_workspace",
                    thread_config={},
                    turn_timeout=1.0,
                )
            )
            structured = StructuredTurnResult(
                payload={"questions": []},
                thread_id="thread-intake",
                turn_id="turn-intake",
                created_thread=True,
            )
            with (
                patch("derivation_app.factory.provision_product_profile"),
                patch(
                    "derivation_app.factory.prepare_intake_workspace",
                    return_value=mapping,
                ),
                patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
                patch(
                    "derivation_app.factory.prepare_product_launch",
                    return_value=SimpleNamespace(command=command),
                ),
                patch("derivation_app.factory.AppServerClient", return_value=client),
                patch(
                    "derivation_app.factory.collect_and_authorize_intake_turn",
                    new=AsyncMock(return_value=authorization),
                ) as authorize,
                patch(
                    "derivation_app.factory.run_app_server_structured_turn",
                    new=AsyncMock(return_value=structured),
                ) as run_turn,
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                result = await runner.run_round(
                    intake_id="intake_session_1",
                    developer_instructions="instructions",
                    prompt="prompt",
                    output_schema={"type": "object"},
                    resume_thread_id=None,
                )

            self.assertEqual(result, structured)
            client.close.assert_awaited_once()
            lock.release.assert_called_once()
            self.assertTrue(workspace.is_dir())
            spec = run_turn.await_args.args[1]
            self.assertTrue(spec.persistent_thread)
            self.assertIsNone(spec.resume_thread_id)
            self.assertEqual(authorize.await_args.kwargs["model"], "gpt-5.6-sol")
            self.assertEqual(authorize.await_args.kwargs["effort"], "high")
            self.assertEqual(spec.model, "gpt-5.6-sol")
            self.assertEqual(spec.effort, "high")
            self.assertEqual(spec.service_tier, "fast")

    async def test_next_round_resumes_same_thread(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            workspace = profile.workspaces / "intake_session_1"
            runtime_temp = workspace / ".runtime_tmp"
            runtime_temp.mkdir(parents=True)
            mapping = IntakeWorkspaceMapping(
                "intake_session_1", workspace, runtime_temp
            )
            command = AppServerCommand(
                argv=("/fixture/codex", "app-server"),
                cwd=str(workspace),
                environment={"HOME": "/fixture/home"},
            )
            lock = Mock(held=True)
            lock.acquire.return_value = lock
            client = Mock(is_running=True, returncode=0)

            async def close_client() -> None:
                client.is_running = False

            client.start = AsyncMock()
            client.close = AsyncMock(side_effect=close_client)
            authorization = SimpleNamespace(
                settings=SimpleNamespace(
                    workspace=workspace,
                    permission_profile="strict_run_workspace",
                    thread_config={},
                    turn_timeout=1.0,
                )
            )
            with (
                patch("derivation_app.factory.provision_product_profile"),
                patch(
                    "derivation_app.factory.prepare_intake_workspace",
                    return_value=mapping,
                ),
                patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
                patch(
                    "derivation_app.factory.prepare_product_launch",
                    return_value=SimpleNamespace(command=command),
                ),
                patch("derivation_app.factory.AppServerClient", return_value=client),
                patch(
                    "derivation_app.factory.collect_and_authorize_intake_turn",
                    new=AsyncMock(return_value=authorization),
                ),
                patch(
                    "derivation_app.factory.run_app_server_structured_turn",
                    new=AsyncMock(
                        return_value=StructuredTurnResult(
                            payload={"questions": []},
                            thread_id="thread-intake",
                            turn_id="turn-2",
                            created_thread=False,
                        )
                    ),
                ) as run_turn,
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                await runner.run_round(
                    intake_id="intake_session_1",
                    developer_instructions="instructions",
                    prompt="answer",
                    output_schema={"type": "object"},
                    resume_thread_id="thread-intake",
                )

            spec = run_turn.await_args.args[1]
            self.assertEqual(spec.resume_thread_id, "thread-intake")
            self.assertTrue(workspace.is_dir())

    async def test_explicit_model_effort_reach_authorization_and_structured_turn(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            workspace = profile.workspaces / "intake_session_1"
            runtime_temp = workspace / ".runtime_tmp"
            runtime_temp.mkdir(parents=True)
            mapping = IntakeWorkspaceMapping(
                "intake_session_1", workspace, runtime_temp
            )
            command = AppServerCommand(
                argv=("/fixture/codex", "app-server"),
                cwd=str(workspace),
                environment={"HOME": "/fixture/home"},
            )
            lock = Mock(held=True)
            lock.acquire.return_value = lock
            client = Mock(is_running=True, returncode=0)

            async def close_client() -> None:
                client.is_running = False

            client.start = AsyncMock()
            client.close = AsyncMock(side_effect=close_client)
            authorization = SimpleNamespace(
                settings=SimpleNamespace(
                    workspace=workspace,
                    permission_profile="strict_run_workspace",
                    thread_config={},
                    turn_timeout=1.0,
                )
            )
            structured = StructuredTurnResult(
                payload={"questions": []},
                thread_id="thread-intake",
                turn_id="turn-intake",
                created_thread=True,
            )
            with (
                patch("derivation_app.factory.provision_product_profile"),
                patch(
                    "derivation_app.factory.prepare_intake_workspace",
                    return_value=mapping,
                ),
                patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
                patch(
                    "derivation_app.factory.prepare_product_launch",
                    return_value=SimpleNamespace(command=command),
                ),
                patch("derivation_app.factory.AppServerClient", return_value=client),
                patch(
                    "derivation_app.factory.collect_and_authorize_intake_turn",
                    new=AsyncMock(return_value=authorization),
                ) as authorize,
                patch(
                    "derivation_app.factory.run_app_server_structured_turn",
                    new=AsyncMock(return_value=structured),
                ) as run_turn,
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                await runner.run_round(
                    intake_id="intake_session_1",
                    developer_instructions="instructions",
                    prompt="prompt",
                    output_schema={"type": "object"},
                    resume_thread_id=None,
                    model="gpt-5-codex",
                    effort="high",
                )

            self.assertEqual(authorize.await_args.kwargs["model"], "gpt-5-codex")
            self.assertEqual(authorize.await_args.kwargs["effort"], "high")
            spec = run_turn.await_args.args[1]
            self.assertEqual(spec.model, "gpt-5-codex")
            self.assertEqual(spec.effort, "high")
            self.assertNotEqual(spec.model, "gpt-5.4")
            self.assertNotEqual(spec.effort, "low")

    def test_terminal_cleanup_is_explicit_and_scoped(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            workspace = profile.workspaces / "intake_session_1"
            (workspace / ".runtime_tmp").mkdir(parents=True)
            with patch("derivation_app.factory.provision_product_profile"):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )

            runner.remove_session_workspace("intake_session_1")

            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
