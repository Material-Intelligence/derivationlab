"""Lazy, process-local registry for isolated per-user derivation services."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from derivation_api.models import BuildInfoView, HealthView
from derivation_api.service import DerivationService
from derivation_api.site_access import (
    RequestServiceResolver,
    SessionInvalid,
)
from fastapi import Request

from .product_profile import ProductProfile

_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TenantRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantPaths:
    user_id: str
    root: Path
    profile_root: Path
    run_root: Path
    intake_root: Path

    @property
    def archive_root(self) -> Path:
        """Hold the read-only runs this tenant may browse, beside its own runs."""

        return self.root / "archives"

    @property
    def product_profile(self) -> ProductProfile:
        return ProductProfile.below(self.profile_root)


TenantServiceFactory = Callable[[TenantPaths], Awaitable[DerivationService]]


class TenantRuntimeRegistry:
    """Create one isolated service per internal user id, at most once."""

    def __init__(
        self,
        root: str | Path,
        factory: TenantServiceFactory,
        *,
        profiles_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.profiles_root = Path(profiles_root or (self.root / "profiles")).resolve()
        self._factory = factory
        self._services: dict[str, DerivationService] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False
        self._prepare_private_directory(self.root)
        self._prepare_private_directory(self.root / "users")
        self._prepare_private_directory(self.profiles_root)

    def paths_for(self, user_id: str) -> TenantPaths:
        if not _USER_ID_RE.fullmatch(user_id):
            raise TenantRuntimeError("user id is invalid")
        users_root = self.root / "users"
        tenant_root = users_root / user_id
        if tenant_root.is_symlink():
            raise TenantRuntimeError("tenant root must not be a symlink")
        self._prepare_private_directory(tenant_root)
        resolved = tenant_root.resolve()
        if resolved.parent != users_root.resolve():
            raise TenantRuntimeError("tenant root escapes the users directory")
        profile_root = self.profiles_root / user_id
        if profile_root.is_symlink():
            raise TenantRuntimeError("tenant profile root must not be a symlink")
        run_root = resolved / "runs"
        intake_root = resolved / "intakes"
        archive_root = resolved / "archives"
        for path in (profile_root, run_root, intake_root, archive_root):
            self._prepare_private_directory(path)
        return TenantPaths(
            user_id=user_id,
            root=resolved,
            profile_root=profile_root,
            run_root=run_root,
            intake_root=intake_root,
        )

    async def service_for(self, user_id: str) -> DerivationService:
        if self._closed:
            raise TenantRuntimeError("tenant registry is closed")
        existing = self._services.get(user_id)
        if existing is not None:
            return existing
        async with self._registry_lock:
            lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if self._closed:
                raise TenantRuntimeError("tenant registry is closed")
            existing = self._services.get(user_id)
            if existing is not None:
                return existing
            paths = self.paths_for(user_id)
            service = await self._factory(paths)
            try:
                await service.start()
            except BaseException:
                await service.close()
                raise
            async with self._registry_lock:
                closed_while_starting = self._closed
                if not closed_while_starting:
                    self._services[user_id] = service
            if closed_while_starting:
                await service.close()
                raise TenantRuntimeError("tenant registry closed while service started")
            return service

    async def close(self) -> None:
        async with self._registry_lock:
            if self._closed and not self._services:
                return
            self._closed = True
            services = list(self._services.items())
            self._services.clear()
        failures: list[tuple[str, DerivationService, BaseException]] = []
        for user_id, service in reversed(services):
            try:
                await service.close()
            except BaseException as exc:  # noqa: BLE001
                failures.append((user_id, service, exc))
        if failures:
            async with self._registry_lock:
                self._services.update(
                    (user_id, service) for user_id, service, _ in failures
                )
            raise TenantRuntimeError(
                f"{len(failures)} tenant service(s) failed to close"
            ) from failures[0][2]
        async with self._registry_lock:
            self._locks.clear()

    async def loaded_services(self) -> tuple[DerivationService, ...]:
        async with self._registry_lock:
            return tuple(self._services.values())

    @staticmethod
    def _prepare_private_directory(path: Path) -> None:
        if path.is_symlink():
            raise TenantRuntimeError("tenant directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir() or path.is_symlink():
            raise TenantRuntimeError("tenant directory is unavailable or unsafe")
        if os.name != "nt":
            details = path.stat()
            if details.st_uid != os.getuid():
                raise TenantRuntimeError("tenant directory has an unexpected owner")
            current_mode = stat.S_IMODE(details.st_mode)
            if current_mode != 0o700:
                path.chmod(0o700)
                if stat.S_IMODE(path.stat().st_mode) != 0o700:
                    raise TenantRuntimeError("tenant directory mode is not private")


class TenantRequestServiceResolver(RequestServiceResolver):
    """Resolve the authenticated request to its owner's isolated service."""

    def __init__(
        self,
        registry: TenantRuntimeRegistry,
        *,
        build_info: BuildInfoView | None = None,
    ) -> None:
        self.registry = registry
        self._build_info = build_info or BuildInfoView(
            schema_version="derivationlab-build-info-v1",
            version="dev",
            build_number="0",
            release_id="development",
            commit="0" * 40,
            openapi_sha256="0" * 64,
            product_mode="development",
        )
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        try:
            await self.registry.close()
        finally:
            self._started = False

    async def health(self) -> HealthView:
        services = await self.registry.loaded_services()
        statuses = await asyncio.gather(*(service.health() for service in services))
        healthy = self._started and all(status.status == "ok" for status in statuses)
        return HealthView(
            status="ok" if healthy else "degraded",
            service="derivation-tenant-server",
        )

    async def build_info(self) -> BuildInfoView:
        return self._build_info.model_copy(deep=True)

    async def resolve(self, request: Request) -> DerivationService:
        session = getattr(request.state, "site_session", None)
        if session is None:
            raise SessionInvalid("site session is unavailable")
        return await self.registry.service_for(session.account.user_id)


__all__ = [
    "TenantPaths",
    "TenantRequestServiceResolver",
    "TenantRuntimeError",
    "TenantRuntimeRegistry",
    "TenantServiceFactory",
]
