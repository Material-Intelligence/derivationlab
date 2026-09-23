from __future__ import annotations

import os
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

from derivation_api.models import ImportExistingAccountRequest
from derivation_api.service import DerivationServiceError

from derivation_app import account as account_module
from derivation_app.account import CodexAccountBackend, ProductAccountManager
from derivation_app.product_profile import (
    ProductProfile,
    ProfileInstanceLock,
    ProfileLockHeld,
    provision_product_profile,
)
from derivation_runtime.platform_policy import PlatformFamily


class FakeLoginSession:
    def __init__(
        self,
        profile: ProductProfile,
        *,
        outcomes: list[str],
        verification_url: str = "https://auth.openai.com/device",
    ) -> None:
        self.profile = profile
        self.login_id = "provider-login-secret"
        self.user_code = "SAFE-CODE"
        self.verification_url = verification_url
        self.outcomes = outcomes
        self.closed = False
        self.cancel_count = 0

    async def poll(self):  # type: ignore[no-untyped-def]
        outcome = self.outcomes.pop(0)
        if outcome == "succeeded":
            auth = self.profile.codex_home / "auth.json"
            auth.write_bytes(b"device-credential")
            if os.name != "nt":
                auth.chmod(0o600)
        return outcome

    async def cancel(self):  # type: ignore[no-untyped-def]
        self.cancel_count += 1
        return "canceled"

    async def account_is_chatgpt(self) -> bool:
        return (self.profile.codex_home / "auth.json").is_file()

    async def close(self) -> None:
        self.closed = True


class FakeAccountBackend:
    def __init__(
        self,
        *,
        product_profile: ProductProfile,
        product_signed_in: bool = False,
        isolated_valid: bool = True,
        login_factory: Callable[[ProductProfile], FakeLoginSession] | None = None,
    ) -> None:
        self.product_profile = product_profile
        self.product_signed_in = product_signed_in
        self.isolated_valid = isolated_valid
        self.login_factory = login_factory
        self.probes: list[Path] = []
        self.login: FakeLoginSession | None = None
        self.rate_limit_reads = 0

    async def account_is_chatgpt(self, profile: ProductProfile) -> bool:
        self.probes.append(profile.root)
        if profile.root == self.product_profile.root:
            return self.product_signed_in
        return self.isolated_valid

    async def start_device_login(self, profile: ProductProfile) -> FakeLoginSession:
        factory = self.login_factory or (
            lambda value: FakeLoginSession(value, outcomes=["pending", "succeeded"])
        )
        self.login = factory(profile)
        return self.login

    async def read_rate_limits(self, profile: ProductProfile):  # type: ignore[no-untyped-def]
        self.rate_limit_reads += 1
        return {
            "planType": "plus",
            "primary": {"usedPercent": 31, "windowDurationMins": 10_080},
        }


class ProductAccountManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repository = root / "repo"
        (self.repository / "runs").mkdir(parents=True)
        self.profile = ProductProfile.below(root / "product-profile")
        provision_product_profile(self.profile, repo_root=self.repository)
        self.source = root / "global-codex" / "auth.json"
        self.source.parent.mkdir()

    def write_private(self, path: Path, content: bytes) -> None:
        path.write_bytes(content)
        if os.name != "nt":
            path.chmod(0o600)

    def manager(
        self,
        backend: FakeAccountBackend,
        *,
        monotonic: Callable[[], float] | None = None,
    ) -> ProductAccountManager:
        options = {}
        if monotonic is not None:
            options["monotonic"] = monotonic
        return ProductAccountManager(
            profile=self.profile,
            repo_root=self.repository,
            source_auth=self.source,
            backend=backend,
            **options,  # type: ignore[arg-type]
        )

    async def test_import_copies_opaque_source_after_isolated_chatgpt_check(
        self,
    ) -> None:
        self.write_private(self.source, b"opaque-source")
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend)

        result = await manager.import_existing_account(
            ImportExistingAccountRequest(confirm_import=True),
            idempotency_key="import-1",
        )

        self.assertEqual(result.status, "signed_in")
        self.assertEqual(self.source.read_bytes(), b"opaque-source")
        target = self.profile.codex_home / "auth.json"
        self.assertEqual(target.read_bytes(), b"opaque-source")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotEqual(backend.probes, [self.profile.root])

    async def test_rate_limits_read_after_sign_in_and_throttle_provider_refresh(
        self,
    ) -> None:
        target = self.profile.codex_home / "auth.json"
        self.write_private(target, b"current")
        backend = FakeAccountBackend(
            product_profile=self.profile,
            product_signed_in=True,
        )
        manager = self.manager(backend)

        first = await manager.rate_limits()
        second = await manager.rate_limits()

        self.assertEqual(first.windows[0].remaining_percent, 69)
        self.assertEqual(second, first)
        self.assertEqual(backend.rate_limit_reads, 1)

    async def test_failed_verification_keeps_existing_product_credential(self) -> None:
        self.write_private(self.source, b"candidate")
        target = self.profile.codex_home / "auth.json"
        self.write_private(target, b"previous")
        backend = FakeAccountBackend(
            product_profile=self.profile,
            product_signed_in=False,
            isolated_valid=False,
        )
        manager = self.manager(backend)

        with self.assertRaisesRegex(DerivationServiceError, "not a reusable"):
            await manager.import_existing_account(
                ImportExistingAccountRequest(confirm_import=True),
                idempotency_key="import-fail",
            )

        self.assertEqual(target.read_bytes(), b"previous")
        self.assertFalse((self.profile.root / "credential-backups").exists())

    async def test_source_path_replacement_after_open_cannot_change_copied_bytes(
        self,
    ) -> None:
        self.write_private(self.source, b"opened-bytes")
        replacement = self.source.parent / "replacement.json"
        self.write_private(replacement, b"replacement-bytes")
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend)
        original_copy = account_module._copy_descriptor
        replaced = False

        def replace_path_then_copy(descriptor: int, destination: Path) -> None:
            nonlocal replaced
            if not replaced:
                os.replace(replacement, self.source)
                replaced = True
            original_copy(descriptor, destination)

        with patch(
            "derivation_app.account._copy_descriptor",
            side_effect=replace_path_then_copy,
        ):
            await manager.import_existing_account(
                ImportExistingAccountRequest(confirm_import=True),
                idempotency_key="race-safe",
            )

        self.assertEqual(
            (self.profile.codex_home / "auth.json").read_bytes(),
            b"opened-bytes",
        )
        self.assertEqual(self.source.read_bytes(), b"replacement-bytes")

    async def test_replacement_is_backed_up_once_and_idempotency_replays(self) -> None:
        self.write_private(self.source, b"candidate")
        target = self.profile.codex_home / "auth.json"
        self.write_private(target, b"previous")
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend)
        command = ImportExistingAccountRequest(confirm_import=True)

        first = await manager.import_existing_account(
            command,
            idempotency_key="same-import",
        )
        replay = await manager.import_existing_account(
            command,
            idempotency_key="same-import",
        )

        self.assertEqual(first, replay)
        backups = list((self.profile.root / "credential-backups").glob("*/auth.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"previous")
        self.assertEqual(target.read_bytes(), b"candidate")

    async def test_signed_in_product_is_never_overwritten(self) -> None:
        self.write_private(self.source, b"candidate")
        target = self.profile.codex_home / "auth.json"
        self.write_private(target, b"current")
        backend = FakeAccountBackend(
            product_profile=self.profile,
            product_signed_in=True,
        )
        manager = self.manager(backend)

        with self.assertRaisesRegex(DerivationServiceError, "already signed in"):
            await manager.import_existing_account(
                ImportExistingAccountRequest(confirm_import=True),
                idempotency_key="blocked",
            )

        self.assertEqual(target.read_bytes(), b"current")

    async def test_source_symlink_and_public_mode_are_not_importable(self) -> None:
        actual = self.source.parent / "actual.json"
        self.write_private(actual, b"opaque")
        self.source.symlink_to(actual)
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend)

        account = await manager.account()
        self.assertFalse(account.import_available)
        with self.assertRaisesRegex(DerivationServiceError, "unavailable or unsafe"):
            await manager.import_existing_account(
                ImportExistingAccountRequest(confirm_import=True),
                idempotency_key="unsafe",
            )

        self.source.unlink()
        self.source.write_bytes(b"opaque")
        if os.name != "nt":
            self.source.chmod(0o644)
            self.assertFalse((await manager.account()).import_available)

    async def test_device_login_poll_success_publishes_only_after_completion(
        self,
    ) -> None:
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend)

        started = await manager.start_device_login()
        target = self.profile.codex_home / "auth.json"
        self.assertFalse(target.exists())
        self.assertNotEqual(started.login_id, "provider-login-secret")
        self.assertEqual(
            (await manager.get_device_login(started.login_id)).status, "pending"
        )
        self.assertFalse(target.exists())

        completed = await manager.get_device_login(started.login_id)

        self.assertEqual(completed.status, "signed_in")
        self.assertEqual(target.read_bytes(), b"device-credential")
        assert backend.login is not None
        self.assertTrue(backend.login.closed)

    async def test_device_login_cancel_and_expiry_are_bounded(self) -> None:
        clock = [10.0]
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend, monotonic=lambda: clock[0])
        first = await manager.start_device_login()

        canceled = await manager.cancel_device_login(first.login_id)

        self.assertEqual(canceled.status, "canceled")
        assert backend.login is not None
        self.assertTrue(backend.login.closed)

        second = await manager.start_device_login()
        clock[0] += 901
        expired = await manager.get_device_login(second.login_id)
        self.assertEqual(expired.status, "expired")

    async def test_unsafe_device_url_is_rejected_without_leaking_session(self) -> None:
        backend = FakeAccountBackend(
            product_profile=self.profile,
            login_factory=lambda profile: FakeLoginSession(
                profile,
                outcomes=["pending"],
                verification_url="http://evil.example/device\n",
            ),
        )
        manager = self.manager(backend)

        with self.assertRaisesRegex(DerivationServiceError, "could not be started"):
            await manager.start_device_login()

        assert backend.login is not None
        self.assertTrue(backend.login.closed)

    async def test_expired_login_remains_terminal_when_provider_cancel_fails(
        self,
    ) -> None:
        clock = [10.0]
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend, monotonic=lambda: clock[0])
        started = await manager.start_device_login()
        assert backend.login is not None
        backend.login.cancel = AsyncMock(side_effect=RuntimeError("provider exited"))
        clock[0] += 901

        self.assertEqual(
            (await manager.get_device_login(started.login_id)).status, "expired"
        )
        self.assertEqual(
            (await manager.get_device_login(started.login_id)).status, "expired"
        )
        self.assertTrue(backend.login.closed)
        self.assertFalse(backend.login.profile.root.exists())
        self.assertFalse(manager.product_auth.exists())

    async def test_locked_account_probe_does_not_leak_workspace(self) -> None:
        backend = CodexAccountBackend(
            repo_root=self.repository,
            platform=PlatformFamily.MACOS,
            architecture="arm64",
            app_server_executable=Path("/unused/codex"),
            user_home=Path(self.temporary.name),
        )
        before = set(self.profile.workspaces.iterdir())
        with ProfileInstanceLock(self.profile), self.assertRaises(ProfileLockHeld):
            await backend.account_is_chatgpt(self.profile)
        self.assertEqual(set(self.profile.workspaces.iterdir()), before)

    async def test_account_workspace_failure_releases_profile_lock(self) -> None:
        backend = CodexAccountBackend(
            repo_root=self.repository,
            platform=PlatformFamily.MACOS,
            architecture="arm64",
            app_server_executable=Path("/unused/codex"),
            user_home=Path(self.temporary.name),
        )
        with (
            patch.object(
                account_module,
                "prepare_intake_workspace",
                side_effect=OSError("disk error"),
            ),
            self.assertRaises(OSError),
        ):
            await backend.account_is_chatgpt(self.profile)
        with ProfileInstanceLock(self.profile):
            pass

    async def test_expired_login_cleans_temporary_profile_when_provider_close_fails(
        self,
    ) -> None:
        clock = [10.0]
        backend = FakeAccountBackend(product_profile=self.profile)
        manager = self.manager(backend, monotonic=lambda: clock[0])
        started = await manager.start_device_login()
        assert backend.login is not None
        backend.login.close = AsyncMock(side_effect=OSError("provider pipe closed"))
        clock[0] += 901

        self.assertEqual(
            (await manager.get_device_login(started.login_id)).status, "expired"
        )
        self.assertEqual(
            (await manager.get_device_login(started.login_id)).status, "expired"
        )
        self.assertFalse(backend.login.profile.root.exists())
        self.assertFalse(manager.product_auth.exists())


if __name__ == "__main__":
    unittest.main()
