from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from derivation_api.fake_service import FakeDerivationService

from derivation_app.tenant_runtime import TenantRuntimeError, TenantRuntimeRegistry


class TrackingService(FakeDerivationService):
    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker
        self.starts = 0
        self.closes = 0

    async def start(self) -> None:
        self.starts += 1
        await super().start()

    async def close(self) -> None:
        self.closes += 1
        await super().close()


class FailOnceCloseService(TrackingService):
    async def close(self) -> None:
        self.closes += 1
        if self.closes == 1:
            raise RuntimeError("fixture close failed")
        await FakeDerivationService.close(self)


class TenantRuntimeRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.created: list[tuple[object, TrackingService]] = []

        async def factory(paths):
            await asyncio.sleep(0)
            service = TrackingService(paths.user_id)
            self.created.append((paths, service))
            return service

        self.root = Path(self.temporary_directory.name) / "server-data"
        self.profiles_root = Path(self.temporary_directory.name) / "server-profiles"
        self.registry = TenantRuntimeRegistry(
            self.root,
            factory,
            profiles_root=self.profiles_root,
        )

    async def _cleanup(self) -> None:
        await self.registry.close()
        self.temporary_directory.cleanup()

    async def test_two_users_receive_distinct_services_and_private_roots(self) -> None:
        alice_id = "a" * 32
        bob_id = "b" * 32

        alice = await self.registry.service_for(alice_id)
        bob = await self.registry.service_for(bob_id)
        alice_paths = self.registry.paths_for(alice_id)
        bob_paths = self.registry.paths_for(bob_id)

        self.assertIsNot(alice, bob)
        self.assertNotEqual(alice_paths.root, bob_paths.root)
        self.assertEqual(alice_paths.product_profile.root, alice_paths.profile_root)
        self.assertEqual(alice_paths.profile_root.parent, self.profiles_root.resolve())
        self.assertEqual(alice_paths.run_root.parent, alice_paths.root)
        self.assertEqual(alice_paths.intake_root.parent, alice_paths.root)
        if os.name != "nt":
            for path in (
                self.root,
                self.root / "users",
                self.profiles_root,
                alice_paths.root,
                alice_paths.profile_root,
                alice_paths.run_root,
                alice_paths.intake_root,
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    async def test_concurrent_requests_create_and_start_one_service(self) -> None:
        user_id = "c" * 32

        services = await asyncio.gather(
            *[self.registry.service_for(user_id) for _ in range(12)]
        )

        self.assertTrue(all(service is services[0] for service in services))
        self.assertEqual(len(self.created), 1)
        self.assertEqual(services[0].starts, 1)

    async def test_close_is_idempotent_and_closes_each_service_once(self) -> None:
        services = [
            await self.registry.service_for("d" * 32),
            await self.registry.service_for("e" * 32),
        ]

        await self.registry.close()
        await self.registry.close()

        self.assertEqual([service.closes for service in services], [1, 1])
        with self.assertRaises(TenantRuntimeError):
            await self.registry.service_for("f" * 32)

    async def test_close_retries_the_same_service_after_one_failure(self) -> None:
        user_id = "9" * 32
        service = FailOnceCloseService(user_id)

        async def factory(_paths):
            return service

        registry = TenantRuntimeRegistry(self.root / "retry", factory)
        await registry.service_for(user_id)
        with self.assertRaisesRegex(TenantRuntimeError, "failed to close"):
            await registry.close()
        await registry.close()
        self.assertEqual(service.closes, 2)

    async def test_close_during_start_does_not_leave_a_live_service(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        service = TrackingService("8" * 32)

        async def factory(_paths):
            started.set()
            await release.wait()
            return service

        registry = TenantRuntimeRegistry(self.root / "race", factory)
        pending = asyncio.create_task(registry.service_for("8" * 32))
        await started.wait()
        await registry.close()
        release.set()
        with self.assertRaisesRegex(TenantRuntimeError, "closed while service started"):
            await pending
        self.assertEqual(service.starts, 1)
        self.assertEqual(service.closes, 1)

    async def test_rejects_malformed_or_symlinked_user_directory(self) -> None:
        for user_id in ("../escape", "not-hex", "a" * 31):
            with self.subTest(user_id=user_id), self.assertRaises(TenantRuntimeError):
                self.registry.paths_for(user_id)

        user_id = "f" * 32
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (self.root / "users" / user_id).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(TenantRuntimeError):
            self.registry.paths_for(user_id)


if __name__ == "__main__":
    unittest.main()
