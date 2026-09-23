"""Private file-credential transactions and managed App Server account login.

Credential bytes are opaque to DerivationLab.  They are copied through
no-follow file descriptors and accepted only after an isolated Codex App
Server reports a ChatGPT account.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from derivation_api.models import (
    AccountRateLimitsView,
    AccountView,
    DeviceLoginCancelView,
    DeviceLoginStartView,
    DeviceLoginStatusView,
    ImportExistingAccountRequest,
)
from derivation_api.service import DerivationServiceError, ErrorKind

from derivation_runtime.app_server_client import (
    AppServerClient,
    AppServerTimeoutError,
    ClientTimeouts,
)
from derivation_runtime.app_server_protocol import ChatgptDeviceCodeLogin
from derivation_runtime.capabilities import INTAKE_V1
from derivation_runtime.platform_policy import PlatformFamily

from .account_rate_limits import AccountRateLimitStore
from .product_profile import (
    IntakeWorkspaceMapping,
    ProductProfile,
    ProfileConflict,
    ProfileInstanceLock,
    ProfileLockHeld,
    prepare_intake_workspace,
    prepare_product_launch,
    provision_product_profile,
)

_IMPORT_DIRECTORY = ".credential-import"
_BACKUP_DIRECTORY = "credential-backups"
_DEVICE_LOGIN_TTL = timedelta(minutes=15)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write while copying a credential")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_regular(
    path: Path,
    *,
    require_private_mode: bool = True,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProfileConflict("credential is unavailable or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ProfileConflict("credential must be a regular file")
        if os.name != "nt":
            if details.st_uid != os.getuid():
                raise ProfileConflict("credential must be owned by the current user")
            if require_private_mode and stat.S_IMODE(details.st_mode) != 0o600:
                raise ProfileConflict("credential must have mode 0600")
        return descriptor, details
    except BaseException:
        os.close(descriptor)
        raise


def _copy_descriptor(descriptor: int, destination: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    output: int | None = None
    try:
        output = os.open(destination, flags, 0o600)
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            _write_all(output, block)
        if os.name != "nt":
            os.fchmod(output, 0o600)
        os.fsync(output)
        os.close(output)
        output = None
    finally:
        if output is not None:
            os.close(output)
        if destination.exists() and destination.is_symlink():
            destination.unlink(missing_ok=True)


def _private_file_available(path: Path) -> bool:
    try:
        descriptor, _ = _open_private_regular(path)
    except ProfileConflict:
        return False
    os.close(descriptor)
    return True


def _validate_profile_directories(profile: ProductProfile) -> None:
    for path in (
        profile.root,
        profile.home,
        profile.codex_home,
        profile.runtime,
        profile.workspaces,
    ):
        if path.is_symlink() or not path.is_dir():
            raise ProfileConflict("product profile directory is unavailable or unsafe")
        if os.name != "nt":
            details = path.stat()
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700:
                raise ProfileConflict(
                    "product profile directory ownership or mode is unsafe"
                )


class DeviceLoginBackendSession(Protocol):
    login_id: str
    user_code: str
    verification_url: str

    async def poll(self) -> Literal["pending", "succeeded", "failed"]: ...

    async def cancel(self) -> Literal["canceled", "not_found"]: ...

    async def account_is_chatgpt(self) -> bool: ...

    async def close(self) -> None: ...


class AccountAppServerBackend(Protocol):
    async def account_is_chatgpt(self, profile: ProductProfile) -> bool: ...

    async def read_rate_limits(
        self, profile: ProductProfile
    ) -> Mapping[str, object]: ...

    async def start_device_login(
        self, profile: ProductProfile
    ) -> DeviceLoginBackendSession: ...


@dataclass
class _OpenedClient:
    profile: ProductProfile
    mapping: IntakeWorkspaceMapping
    lock: ProfileInstanceLock
    client: AppServerClient

    async def close(self) -> None:
        failure: BaseException | None = None
        try:
            if self.client.is_running or self.client.returncode is None:
                await self.client.close()
        except BaseException as exc:  # noqa: BLE001
            failure = exc
        if self.lock.held:
            self.lock.release()
        workspace = self.mapping.sandbox_workspace
        if workspace.parent == self.profile.workspaces and workspace.name.startswith(
            "intake_"
        ):
            shutil.rmtree(workspace, ignore_errors=False)
        if failure is not None:
            raise failure


class _CodexDeviceLoginSession:
    def __init__(
        self,
        opened: _OpenedClient,
        handle: ChatgptDeviceCodeLogin,
        rate_limit_refresh_claim: Callable[[], bool] | None,
    ) -> None:
        self._opened = opened
        self._handle = handle
        self._rate_limit_refresh_claim = rate_limit_refresh_claim
        self.login_id = handle.login_id
        self.user_code = handle.user_code
        self.verification_url = handle.verification_url

    async def poll(self) -> Literal["pending", "succeeded", "failed"]:
        try:
            result = await self._opened.client.wait_account_login(
                self._handle,
                timeout=0.05,
            )
        except AppServerTimeoutError:
            return "pending"
        return "succeeded" if result.success else "failed"

    async def cancel(self) -> Literal["canceled", "not_found"]:
        status = await self._opened.client.account_login_cancel(self._handle)
        return "not_found" if status == "notFound" else "canceled"

    async def account_is_chatgpt(self) -> bool:
        value = await self._opened.client.account_read(refresh_token=False)
        account = value.get("account") if isinstance(value, Mapping) else None
        signed_in = isinstance(account, Mapping) and account.get("type") == "chatgpt"
        if signed_in and (
            self._rate_limit_refresh_claim is None or self._rate_limit_refresh_claim()
        ):
            with suppress(OSError, RuntimeError):
                async with asyncio.timeout(2.0):
                    await self._opened.client.account_rate_limits_read()
        return signed_in

    async def close(self) -> None:
        await self._opened.close()


@dataclass(frozen=True)
class CodexAccountBackend:
    """Open short-lived, tool-free App Server clients for account operations."""

    repo_root: Path
    platform: PlatformFamily
    architecture: str
    app_server_executable: Path
    user_home: Path
    client_timeouts: ClientTimeouts = dataclass_field(default_factory=ClientTimeouts)
    rate_limit_observer: Callable[[dict[str, object], bool], None] | None = None
    rate_limit_refresh_claim: Callable[[], bool] | None = None

    async def _open(self, profile: ProductProfile) -> _OpenedClient:
        provision_product_profile(profile, repo_root=self.repo_root)
        lock = ProfileInstanceLock(profile).acquire()
        mapping: IntakeWorkspaceMapping | None = None
        client: AppServerClient | None = None
        try:
            mapping = prepare_intake_workspace(
                profile,
                intake_id=f"intake_account_{uuid.uuid4().hex}",
                repo_root=self.repo_root,
            )
            prepared = prepare_product_launch(
                profile,
                lock=lock,
                repo_root=self.repo_root,
                platform=self.platform,
                architecture=self.architecture,
                app_server_executable=self.app_server_executable,
                workspace_mapping=mapping,
                repository=self.repo_root,
                user_home=self.user_home,
                capability_profile=INTAKE_V1,
            )
            client = AppServerClient(
                prepared.command.argv,
                cwd=prepared.command.cwd,
                env=prepared.command.environment,
                timeouts=self.client_timeouts,
                **(
                    {"rate_limit_observer": self.rate_limit_observer}
                    if self.rate_limit_observer is not None
                    else {}
                ),
            )
            await client.start()
            return _OpenedClient(profile, mapping, lock, client)
        except BaseException:
            if client is not None and (client.is_running or client.returncode is None):
                await client.close()
            if lock.held:
                lock.release()
            if mapping is not None:
                shutil.rmtree(mapping.sandbox_workspace, ignore_errors=True)
            raise

    async def account_is_chatgpt(self, profile: ProductProfile) -> bool:
        opened = await self._open(profile)
        try:
            value = await opened.client.account_read(refresh_token=False)
            account = value.get("account") if isinstance(value, Mapping) else None
            signed_in = (
                isinstance(account, Mapping) and account.get("type") == "chatgpt"
            )
            if signed_in and (
                self.rate_limit_refresh_claim is None or self.rate_limit_refresh_claim()
            ):
                with suppress(OSError, RuntimeError):
                    async with asyncio.timeout(2.0):
                        await opened.client.account_rate_limits_read()
            return signed_in
        finally:
            await opened.close()

    async def read_rate_limits(self, profile: ProductProfile) -> Mapping[str, object]:
        opened = await self._open(profile)
        try:
            result = await opened.client.account_rate_limits_read()
            snapshot = result["rateLimits"]
            assert isinstance(snapshot, Mapping)
            return snapshot
        finally:
            await opened.close()

    async def start_device_login(
        self, profile: ProductProfile
    ) -> DeviceLoginBackendSession:
        opened = await self._open(profile)
        try:
            handle = await opened.client.account_login_start_chatgpt_device_code()
            return _CodexDeviceLoginSession(
                opened,
                handle,
                self.rate_limit_refresh_claim,
            )
        except BaseException:
            await opened.close()
            raise


@dataclass
class _ManagedLogin:
    public_id: str
    temporary_root: Path
    temporary_profile: ProductProfile
    backend: DeviceLoginBackendSession
    expires_at: datetime
    expires_monotonic: float


class ProductAccountManager:
    """Account service with transactional import and isolated device login."""

    def __init__(
        self,
        *,
        profile: ProductProfile,
        repo_root: str | Path,
        source_auth: str | Path | None,
        backend: AccountAppServerBackend,
        rate_limit_store: AccountRateLimitStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self.repo_root = Path(repo_root).resolve()
        self.source_auth = (
            None if source_auth is None else Path(source_auth).expanduser().absolute()
        )
        self.backend = backend
        self.rate_limit_store = rate_limit_store or AccountRateLimitStore()
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._imports: dict[str, AccountView] = {}
        self._logins: dict[str, _ManagedLogin] = {}
        self._terminal_logins: dict[str, DeviceLoginStatusView] = {}

    @property
    def product_auth(self) -> Path:
        return self.profile.codex_home / "auth.json"

    async def start(self) -> None:
        provision_product_profile(self.profile, repo_root=self.repo_root)

    async def close(self) -> None:
        async with self._lock:
            logins = list(self._logins.values())
            self._logins.clear()
            self._terminal_logins.clear()
        for login in logins:
            with suppress(BaseException):
                await login.backend.cancel()
            await self._close_login(login)

    def _import_available(self) -> bool:
        return self.source_auth is not None and _private_file_available(
            self.source_auth
        )

    async def account(self) -> AccountView:
        import_available = self._import_available()
        try:
            _validate_profile_directories(self.profile)
        except ProfileConflict:
            return AccountView(
                status="unavailable",
                credential_store="file",
                import_available=import_available,
                diagnostic="account_check_failed",
            )
        if not os.path.lexists(self.product_auth):
            return AccountView(
                status="signed_out",
                credential_store="file",
                import_available=import_available,
                diagnostic=(
                    "source_auth_available"
                    if import_available
                    else "source_auth_unavailable"
                ),
            )
        if not _private_file_available(self.product_auth):
            return AccountView(
                status="reauth_required",
                credential_store="file",
                import_available=import_available,
                diagnostic="product_auth_invalid",
            )
        try:
            signed_in = await self.backend.account_is_chatgpt(self.profile)
        except (ProfileConflict, ProfileLockHeld, OSError, RuntimeError):
            return AccountView(
                status="unavailable",
                credential_store="file",
                import_available=import_available,
                diagnostic="account_check_failed",
            )
        return AccountView(
            status="signed_in" if signed_in else "reauth_required",
            credential_store="file",
            import_available=import_available,
            diagnostic="ready" if signed_in else "product_auth_invalid",
        )

    async def rate_limits(self) -> AccountRateLimitsView:
        if not os.path.lexists(self.product_auth):
            return self.rate_limit_store.view(signed_out=True)
        if not _private_file_available(self.product_auth):
            self.rate_limit_store.clear()
            return self.rate_limit_store.view()
        cached = self.rate_limit_store.view()
        if cached.status == "available":
            return cached
        if self.rate_limit_store.claim_provider_refresh():
            try:
                snapshot = await self.backend.read_rate_limits(self.profile)
            except (ProfileConflict, ProfileLockHeld, OSError, RuntimeError):
                pass
            else:
                self.rate_limit_store.observe(dict(snapshot), False)
        return self.rate_limit_store.view()

    def _make_temporary_profile(self, prefix: str) -> tuple[Path, ProductProfile]:
        staging_parent = self.profile.root / _IMPORT_DIRECTORY
        staging_parent.mkdir(parents=True, exist_ok=True)
        if staging_parent.is_symlink() or not staging_parent.is_dir():
            raise ProfileConflict("credential staging root is unsafe")
        if os.name != "nt":
            staging_parent.chmod(0o700)
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=staging_parent))
        if os.name != "nt":
            root.chmod(0o700)
        isolated = ProductProfile.below(root)
        provision_product_profile(isolated, repo_root=self.repo_root)
        return root, isolated

    def _copy_opaque_auth(self, source: Path, destination: Path) -> None:
        descriptor, _ = _open_private_regular(source)
        try:
            _copy_descriptor(descriptor, destination)
        finally:
            os.close(descriptor)

    def _backup_existing_auth(self) -> Path | None:
        if not os.path.lexists(self.product_auth):
            return None
        descriptor, before = _open_private_regular(
            self.product_auth,
            require_private_mode=False,
        )
        os.close(descriptor)
        backup_root = self.profile.root / _BACKUP_DIRECTORY
        backup_root.mkdir(parents=True, exist_ok=True)
        if backup_root.is_symlink() or not backup_root.is_dir():
            raise ProfileConflict("credential backup root is unsafe")
        if os.name != "nt":
            backup_root.chmod(0o700)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = backup_root / timestamp
        destination.mkdir(mode=0o700)
        _fsync_directory(backup_root)
        current = os.lstat(self.product_auth)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ProfileConflict("product credential changed during replacement")
        os.replace(self.product_auth, destination / "auth.json")
        if os.name != "nt":
            (destination / "auth.json").chmod(0o600)
        backup_descriptor, _ = _open_private_regular(destination / "auth.json")
        try:
            os.fsync(backup_descriptor)
        finally:
            os.close(backup_descriptor)
        _fsync_directory(self.profile.codex_home)
        _fsync_directory(destination)
        return destination / "auth.json"

    def _publish_validated_auth(self, validated_auth: Path) -> None:
        pending = self.profile.codex_home / f".auth.pending.{uuid.uuid4().hex}"
        self._copy_opaque_auth(validated_auth, pending)
        backup: Path | None = None
        try:
            backup = self._backup_existing_auth()
            os.replace(pending, self.product_auth)
            if os.name != "nt":
                self.product_auth.chmod(0o600)
            _fsync_directory(self.profile.codex_home)
        except BaseException:
            if os.path.lexists(self.product_auth):
                descriptor, _ = _open_private_regular(self.product_auth)
                os.close(descriptor)
                self.product_auth.unlink()
            if backup is not None and backup.exists():
                os.replace(backup, self.product_auth)
                _fsync_directory(self.product_auth.parent)
                _fsync_directory(backup.parent)
            raise
        finally:
            pending.unlink(missing_ok=True)

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView:
        del command
        async with self._lock:
            if idempotency_key is not None and idempotency_key in self._imports:
                return self._imports[idempotency_key].model_copy(deep=True)
            current = await self.account()
            if current.status == "signed_in":
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "account_already_signed_in",
                    "The product account is already signed in.",
                )
            if current.status == "unavailable":
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "account_check_unavailable",
                    "The product account cannot be checked while another account operation is active.",
                )
            if not current.import_available:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "existing_account_unavailable",
                    "The fixed existing Codex credential is unavailable or unsafe.",
                )
            self.rate_limit_store.clear()
            root, isolated = self._make_temporary_profile("import-")
            try:
                assert self.source_auth is not None
                self._copy_opaque_auth(
                    self.source_auth, isolated.codex_home / "auth.json"
                )
                if not await self.backend.account_is_chatgpt(isolated):
                    raise DerivationServiceError(
                        ErrorKind.INVALID_STATE,
                        "existing_account_not_chatgpt",
                        "The existing Codex credential is not a reusable ChatGPT login.",
                    )
                self._publish_validated_auth(isolated.codex_home / "auth.json")
            except DerivationServiceError:
                raise
            except (ProfileConflict, ProfileLockHeld, OSError, RuntimeError) as exc:
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "account_import_failed",
                    "The existing Codex login could not be imported safely.",
                ) from exc
            finally:
                shutil.rmtree(root, ignore_errors=True)
            result = AccountView(
                status="signed_in",
                credential_store="file",
                import_available=True,
                diagnostic="ready",
            )
            if idempotency_key is not None:
                self._imports[idempotency_key] = result.model_copy(deep=True)
            return result

    async def start_device_login(self) -> DeviceLoginStartView:
        async with self._lock:
            current = await self.account()
            if current.status == "signed_in":
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "account_already_signed_in",
                    "The product account is already signed in.",
                )
            self.rate_limit_store.clear()
            root, isolated = self._make_temporary_profile("device-")
            backend: DeviceLoginBackendSession | None = None
            managed: _ManagedLogin | None = None
            try:
                backend = await self.backend.start_device_login(isolated)
                public_id = f"login-{uuid.uuid4().hex}"
                expires_at = datetime.now(UTC) + _DEVICE_LOGIN_TTL
                managed = _ManagedLogin(
                    public_id=public_id,
                    temporary_root=root,
                    temporary_profile=isolated,
                    backend=backend,
                    expires_at=expires_at,
                    expires_monotonic=self._monotonic()
                    + _DEVICE_LOGIN_TTL.total_seconds(),
                )
                response = DeviceLoginStartView(
                    login_id=public_id,
                    verification_url=backend.verification_url,
                    user_code=backend.user_code,
                    expires_at=expires_at.isoformat().replace("+00:00", "Z"),
                )
                self._logins[public_id] = managed
                return response
            except Exception as exc:
                if backend is not None:
                    with suppress(Exception):
                        await backend.close()
                shutil.rmtree(root, ignore_errors=True)
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "device_login_start_failed",
                    "A separate account login could not be started safely.",
                ) from exc

    async def _close_login(self, login: _ManagedLogin) -> None:
        try:
            await login.backend.close()
        finally:
            shutil.rmtree(login.temporary_root, ignore_errors=True)

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView:
        async with self._lock:
            login = self._logins.get(login_id)
            if login is None:
                terminal = self._terminal_logins.get(login_id)
                if terminal is not None:
                    return terminal.model_copy(deep=True)
                raise DerivationServiceError(
                    ErrorKind.NOT_FOUND,
                    "device_login_not_found",
                    "The device login does not exist.",
                )
            if self._monotonic() >= login.expires_monotonic:
                # Expiry is local and final even if the provider process has exited.
                terminal = DeviceLoginStatusView(status="expired")
                self._logins.pop(login_id, None)
                self._terminal_logins[login_id] = terminal
                try:
                    with suppress(OSError, RuntimeError):
                        await login.backend.cancel()
                finally:
                    with suppress(OSError, RuntimeError):
                        await self._close_login(login)
                return terminal.model_copy(deep=True)
            try:
                outcome = await login.backend.poll()
            except (OSError, RuntimeError) as exc:
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "device_login_poll_failed",
                    "The separate account login status is temporarily unavailable.",
                ) from exc
            if outcome == "pending":
                return DeviceLoginStatusView(status="pending")
            if outcome == "failed":
                await self._close_login(login)
                terminal = DeviceLoginStatusView(
                    status="failed",
                    diagnostic="The separate account login did not complete.",
                )
                self._logins.pop(login_id, None)
                self._terminal_logins[login_id] = terminal
                return terminal.model_copy(deep=True)
            try:
                if not await login.backend.account_is_chatgpt():
                    raise ProfileConflict(
                        "device login did not produce a ChatGPT account"
                    )
                self._publish_validated_auth(
                    login.temporary_profile.codex_home / "auth.json"
                )
            except (ProfileConflict, ProfileLockHeld, OSError, RuntimeError):
                await self._close_login(login)
                terminal = DeviceLoginStatusView(
                    status="failed",
                    diagnostic="The separate account login could not be verified.",
                )
                self._logins.pop(login_id, None)
                self._terminal_logins[login_id] = terminal
                return terminal.model_copy(deep=True)
            await self._close_login(login)
            terminal = DeviceLoginStatusView(status="signed_in")
            self._logins.pop(login_id, None)
            self._terminal_logins[login_id] = terminal
            return terminal.model_copy(deep=True)

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView:
        async with self._lock:
            login = self._logins.get(login_id)
            if login is None:
                return DeviceLoginCancelView(status="not_found")
            try:
                status = await login.backend.cancel()
                await self._close_login(login)
            except (OSError, RuntimeError) as exc:
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "device_login_cancel_failed",
                    "The separate account login could not be canceled safely.",
                ) from exc
            self._logins.pop(login_id, None)
            self._terminal_logins[login_id] = DeviceLoginStatusView(status="canceled")
            return DeviceLoginCancelView(status=status)


__all__ = [
    "AccountAppServerBackend",
    "CodexAccountBackend",
    "DeviceLoginBackendSession",
    "ProductAccountManager",
]
