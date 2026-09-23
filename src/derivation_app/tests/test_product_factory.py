from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from derivation_api.models import CreateRunRequest

from derivation_app.factory import (
    ProductIntakeTurnRunner,
    ProductModelCatalogLoader,
    ProductRuntimeFactory,
    ProductTenantServiceFactory,
    _product_create_run_defaults,
    _require_disjoint_server_roots,
    create_fake_service,
    create_product_service,
)
from derivation_app.model_catalog import ModelCatalogCache, fixture_catalog
from derivation_app.product_profile import IntakeWorkspaceMapping, ProductProfile
from derivation_app.tenant_runtime import TenantPaths
from derivation_runtime.app_server_client import ClientTimeouts
from derivation_runtime.launch_gate import AppServerCommand
from derivation_runtime.platform_policy import PlatformFamily
from derivation_runtime.test_app_server_runtime import config

ROOT = Path(__file__).resolve().parents[3]


def _archive_run_request(objective: str) -> CreateRunRequest:
    return CreateRunRequest.model_validate(
        {
            "problem": {
                "problem_id": "tenant-archive-fixture",
                "version": 1,
                "supersedes_version": None,
                "objective": objective,
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
    )


async def _stage_archive_run(
    *,
    run_id: str,
    source_root: Path,
    destination: Path,
    objective: str,
    timeout: float = 30.0,
) -> None:
    """Produce one real Record V1 run and park it as a read-only archive."""

    source_root.mkdir(parents=True, exist_ok=True)
    service = create_fake_service(
        run_root=source_root,
        repo_root=ROOT,
        run_id_factory=lambda: run_id,
    )
    await service.start()
    try:
        await service.create_run(
            _archive_run_request(objective),
            idempotency_key=None,
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            view = await service.get_run(run_id)
            if view.phase == "review_ready":
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"archive fixture {run_id} never became reviewable"
                )
            await asyncio.sleep(0.01)
    finally:
        await service.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    (source_root / run_id).rename(destination)


class ProductRuntimeFactoryTests(unittest.IsolatedAsyncioTestCase):
    def test_product_run_default_allows_one_hundred_model_calls(self) -> None:
        defaults = _product_create_run_defaults(fixture_catalog())

        self.assertEqual(defaults.config.max_model_calls, 100)

    def test_server_roots_must_be_disjoint(self) -> None:
        root = Path("/fixture/server")
        _require_disjoint_server_roots(
            root / "state", root / "profiles", root / "control"
        )
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            _require_disjoint_server_roots(
                root / "state", root / "state" / "profiles", root / "control"
            )

    async def test_product_catalog_loader_uses_live_then_last_known_good(self) -> None:
        rows = (
            {
                "id": "gpt-5.6-sol",
                "model": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "ultra"},
                ],
            },
        )
        with TemporaryDirectory() as directory:
            runner = SimpleNamespace(list_models=AsyncMock(return_value=rows))
            loader = ProductModelCatalogLoader(
                runner=runner,
                cache=ModelCatalogCache(Path(directory) / "models.json"),
            )
            live = await loader()
            self.assertEqual(live.source, "app_server")
            self.assertEqual(live.models, ("gpt-5.6-sol",))

            runner.list_models.side_effect = RuntimeError("offline")
            cached = await loader()
            self.assertEqual(cached.source, "last_known_good")
            self.assertEqual(cached.models, live.models)

    def test_product_service_preserves_reviewed_native_executable_path(self) -> None:
        executable = Path("/opt/derivationlab/bin/codex")
        with TemporaryDirectory(prefix="factory-profile-", dir=ROOT / "runs") as temp:
            profile = ProductProfile.below(temp)
            with (
                patch(
                    "derivation_app.factory.ProductRuntimeFactory"
                ) as runtime_factory_type,
                patch("derivation_app.factory.ProductIntakeTurnRunner"),
            ):
                service = create_product_service(
                    run_root=ROOT / "runs" / "factory-service",
                    repo_root=ROOT,
                    profile=profile,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=executable,
                    user_home=Path("/fixture/user"),
                    credential_profile_id="fixture-profile",
                    code_commit="d" * 40,
                )

        passed = runtime_factory_type.call_args.kwargs["app_server_executable"]
        self.assertEqual(passed, executable)
        self.assertEqual(service.archive_root, (ROOT / "runs").resolve())
        # New product and tool-enabled Record 1.1 runs use the static formula-v2 gate; the policy
        # is frozen per run in its manifest, never in the request hash.
        self.assertEqual(service.formula_validation_policy, "formula-v2")

    async def test_server_product_service_exposes_only_the_tenant_archive(
        self,
    ) -> None:
        """Server exposes only the tenant's own archive."""

        executable = Path("/opt/derivationlab/bin/codex")
        own_run_id = "run_tenant_owned_archive"
        with TemporaryDirectory(prefix="server-factory-", dir=ROOT / "runs") as temp:
            root = Path(temp)
            tenant = root / "users" / ("a" * 32)
            neighbour = root / "users" / ("b" * 32)
            await _stage_archive_run(
                run_id=own_run_id,
                source_root=root / "staging" / "own",
                destination=tenant / "archives" / "examples" / own_run_id,
                objective="Expose one example run inside the tenant that owns it.",
            )
            await _stage_archive_run(
                run_id="run_other_tenant_archive",
                source_root=root / "staging" / "other",
                destination=neighbour
                / "archives"
                / "examples"
                / "run_other_tenant_archive",
                objective="Stay invisible to every tenant but its own owner.",
            )

            profile = ProductProfile.below(tenant / "profile")
            intake_root = tenant / "intakes"
            with (
                patch("derivation_app.factory.ProductRuntimeFactory"),
                patch("derivation_app.factory.ProductIntakeTurnRunner"),
            ):
                service = create_product_service(
                    run_root=tenant / "runs",
                    storage_root=tenant,
                    archive_root=tenant / "archives",
                    repo_root=ROOT,
                    profile=profile,
                    platform=PlatformFamily.LINUX,
                    architecture="x86_64",
                    app_server_executable=executable,
                    user_home=Path("/fixture/server-user"),
                    credential_profile_id="fixture-tenant",
                    code_commit="e" * 40,
                    intake_root=intake_root,
                    allow_existing_account_import=False,
                    enable_default_archive=False,
                )

            self.assertEqual(service.storage_root, tenant.resolve())
            self.assertEqual(service.archive_root, (tenant / "archives").resolve())
            self.assertIsNone(service.account_service.source_auth)
            self.assertEqual(
                service.intake_session_service.store.path,
                intake_root.resolve() / "control.sqlite",
            )

            # Start the discovery path only: the product runtime and the live
            # model catalog both need a real App Server, which this test replaces.
            service.model_catalog_loader = None
            with patch("derivation_app.account.provision_product_profile"):
                await service.start()
                try:
                    catalog = await service.list_runs()
                    rejections = dict(service._archive_rejections)
                finally:
                    await service.close()

        self.assertEqual([row.id for row in catalog], [own_run_id])
        self.assertTrue(catalog[0].read_only)
        self.assertEqual(rejections, {})

    async def test_tenant_factory_passes_only_user_scoped_product_paths(self) -> None:
        with TemporaryDirectory(prefix="tenant-factory-", dir=ROOT / "runs") as temp:
            root = Path(temp)
            paths = TenantPaths(
                user_id="a" * 32,
                root=root,
                profile_root=root / "profile",
                run_root=root / "runs",
                intake_root=root / "intakes",
            )
            build_info = SimpleNamespace()
            marker = object()
            factory = ProductTenantServiceFactory(
                repo_root=ROOT,
                platform=PlatformFamily.LINUX,
                architecture="x86_64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/server-user"),
                code_commit="f" * 40,
                build_info=build_info,
            )
            with (
                patch(
                    "derivation_app.factory.provision_product_profile",
                    return_value=SimpleNamespace(scientific_runtime=object()),
                ),
                patch(
                    "derivation_app.factory.create_product_service",
                    return_value=marker,
                ) as create_service,
            ):
                result = await factory(paths)

        self.assertIs(result, marker)
        kwargs = create_service.call_args.kwargs
        self.assertEqual(kwargs["run_root"], paths.run_root)
        self.assertEqual(kwargs["storage_root"], paths.root)
        self.assertEqual(kwargs["intake_root"], paths.intake_root)
        self.assertEqual(kwargs["archive_root"], paths.archive_root)
        self.assertEqual(paths.archive_root, paths.root / "archives")
        self.assertFalse(kwargs["allow_existing_account_import"])
        self.assertFalse(kwargs["enable_default_archive"])

    async def test_factory_prepares_run_starts_exact_client_and_returns_lease(
        self,
    ) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        command = AppServerCommand(
            argv=("/fixture/codex", "app-server"),
            cwd=str(ROOT),
            environment={"HOME": "/fixture/home"},
        )
        mapping = object()
        prepared = SimpleNamespace(command=command)
        lock = Mock(held=True, runtime_owned=False)
        lock.acquire.return_value = lock
        client = Mock(is_running=True)
        client.start = AsyncMock()
        client.close = AsyncMock()
        lease = object()
        evidence_directory = ROOT / "runs" / config().run_id

        with (
            patch("derivation_app.factory.provision_product_profile"),
            patch(
                "derivation_app.factory.prepare_run_workspace",
                return_value=mapping,
            ) as prepare_workspace,
            patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
            patch(
                "derivation_app.factory.prepare_product_launch",
                return_value=prepared,
            ) as prepare_launch,
            patch(
                "derivation_app.factory.AppServerClient", return_value=client
            ) as client_type,
            patch(
                "derivation_app.factory.create_product_runtime",
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
                client_timeouts=ClientTimeouts(),
            )
            result = await factory(config(), evidence_directory)

        self.assertIs(result, lease)
        prepare_workspace.assert_called_once()
        prepare_launch.assert_called_once()
        client_type.assert_called_once_with(
            command.argv,
            cwd=command.cwd,
            env=command.environment,
            timeouts=factory.client_timeouts,
            dynamic_tool_audit_path=evidence_directory / "dynamic_tool_calls.jsonl",
        )
        client.start.assert_awaited_once()
        create_runtime.assert_awaited_once()
        lock.release.assert_not_called()

    async def test_factory_releases_lock_if_client_start_fails(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        command = AppServerCommand(
            argv=("/fixture/codex", "app-server"),
            cwd=str(ROOT),
            environment={"HOME": "/fixture/home"},
        )
        lock = Mock(held=True, runtime_owned=False)
        lock.acquire.return_value = lock
        client = Mock(is_running=False)
        client.start = AsyncMock(side_effect=RuntimeError("start failed"))
        client.close = AsyncMock()

        with (
            patch("derivation_app.factory.provision_product_profile"),
            patch(
                "derivation_app.factory.prepare_run_workspace", return_value=object()
            ),
            patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
            patch(
                "derivation_app.factory.prepare_product_launch",
                return_value=SimpleNamespace(command=command),
            ),
            patch("derivation_app.factory.AppServerClient", return_value=client),
        ):
            factory = ProductRuntimeFactory(
                profile=profile,
                repo_root=ROOT,
                platform=PlatformFamily.MACOS,
                architecture="arm64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/user"),
            )
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                await factory(config(), ROOT / "runs" / config().run_id)

        lock.release.assert_called_once()
        client.close.assert_not_awaited()

    async def test_factory_preserves_transferred_lock_on_cleanup_error(self) -> None:
        profile = ProductProfile.below(ROOT / "runs" / "factory-profile")
        command = AppServerCommand(
            argv=("/fixture/codex", "app-server"),
            cwd=str(ROOT),
            environment={"HOME": "/fixture/home"},
        )
        lock = Mock(held=True, runtime_owned=True)
        lock.acquire.return_value = lock
        client = Mock(is_running=True)
        client.start = AsyncMock()
        client.close = AsyncMock()

        with (
            patch("derivation_app.factory.provision_product_profile"),
            patch(
                "derivation_app.factory.prepare_run_workspace", return_value=object()
            ),
            patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
            patch(
                "derivation_app.factory.prepare_product_launch",
                return_value=SimpleNamespace(command=command),
            ),
            patch("derivation_app.factory.AppServerClient", return_value=client),
            patch(
                "derivation_app.factory.create_product_runtime",
                new=AsyncMock(side_effect=RuntimeError("cleanup retained")),
            ),
        ):
            factory = ProductRuntimeFactory(
                profile=profile,
                repo_root=ROOT,
                platform=PlatformFamily.MACOS,
                architecture="arm64",
                app_server_executable=Path("/fixture/codex"),
                user_home=Path("/fixture/user"),
            )
            with self.assertRaisesRegex(RuntimeError, "cleanup retained"):
                await factory(config(), ROOT / "runs" / config().run_id)

        lock.release.assert_not_called()
        client.close.assert_not_awaited()

    async def test_intake_runner_closes_client_releases_lock_and_removes_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            workspace = profile.workspaces / "intake_fixture"
            runtime_temp = workspace / ".runtime_tmp"
            runtime_temp.mkdir(parents=True)
            mapping = IntakeWorkspaceMapping("intake_fixture", workspace, runtime_temp)
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
                        return_value=SimpleNamespace(payload={"patches": []})
                    ),
                ),
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                result = await runner("instructions", "prompt", {"type": "object"})

            self.assertEqual(result, {"patches": []})
            client.close.assert_awaited_once()
            lock.release.assert_called_once()
            self.assertFalse(workspace.exists())

    async def test_intake_runner_cleans_up_after_structured_turn_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            workspace = profile.workspaces / "intake_fixture"
            runtime_temp = workspace / ".runtime_tmp"
            runtime_temp.mkdir(parents=True)
            mapping = IntakeWorkspaceMapping("intake_fixture", workspace, runtime_temp)
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
                    new=AsyncMock(side_effect=RuntimeError("bad model output")),
                ),
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                with self.assertRaisesRegex(RuntimeError, "bad model output"):
                    await runner("instructions", "prompt", {"type": "object"})

            client.close.assert_awaited_once()
            lock.release.assert_called_once()
            self.assertFalse(workspace.exists())

    async def test_intake_runner_does_not_create_workspace_when_profile_is_busy(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ProductProfile.below(root / "profile")
            profile.workspaces.mkdir(parents=True)
            lock = Mock()
            lock.acquire.side_effect = RuntimeError("profile busy")

            with (
                patch("derivation_app.factory.provision_product_profile"),
                patch("derivation_app.factory.ProfileInstanceLock", return_value=lock),
                patch(
                    "derivation_app.factory.prepare_intake_workspace"
                ) as prepare_workspace,
            ):
                runner = ProductIntakeTurnRunner(
                    profile=profile,
                    repo_root=root,
                    platform=PlatformFamily.MACOS,
                    architecture="arm64",
                    app_server_executable=Path("/fixture/codex"),
                    user_home=Path("/fixture/user"),
                )
                with self.assertRaisesRegex(RuntimeError, "profile busy"):
                    await runner("instructions", "prompt", {"type": "object"})

            prepare_workspace.assert_not_called()
            self.assertEqual(list(profile.workspaces.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
