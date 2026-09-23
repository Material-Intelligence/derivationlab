"""Provider-neutral contracts for website identities and tenant resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fastapi import Request

from .models import BuildInfoView, HealthView, IntakeSessionView, RunSummary, RunView
from .service import DerivationService


class SiteRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class SiteAccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class IdentityError(RuntimeError):
    """Base class for expected identity failures safe to map at an API edge."""


class PasswordPolicyError(IdentityError):
    pass


class AccountConflict(IdentityError):
    pass


class AccountNotFound(IdentityError):
    pass


class InvalidCredentials(IdentityError):
    pass


class AccountLocked(IdentityError):
    pass


class SourceRateLimited(IdentityError):
    pass


class TemporaryPasswordExpired(IdentityError):
    pass


class SessionInvalid(IdentityError):
    pass


class SitePermissionDenied(IdentityError):
    pass


@dataclass(frozen=True)
class SiteAccount:
    user_id: str
    username: str
    email: str
    role: SiteRole
    status: SiteAccountStatus
    must_change_password: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class SessionGrant:
    token: str
    session_id: str
    account: SiteAccount
    idle_expires_at: float
    absolute_expires_at: float


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    account: SiteAccount
    idle_expires_at: float
    absolute_expires_at: float


class SiteIdentityAuthority(Protocol):
    def create_account(
        self,
        *,
        username: str,
        email: str,
        password: str,
        role: SiteRole = SiteRole.USER,
        temporary_password: bool = False,
        actor_user_id: str | None = None,
        source: str = "local-admin",
    ) -> SiteAccount: ...

    def list_accounts(self) -> list[SiteAccount]: ...

    def get_account(self, user_id: str) -> SiteAccount: ...

    def login(
        self,
        identifier: str,
        password: str,
        *,
        source: str = "unknown",
    ) -> SessionGrant: ...

    def authenticate_session(
        self,
        token: str,
        *,
        touch: bool = True,
    ) -> AuthenticatedSession: ...

    def logout(
        self,
        token: str,
        *,
        actor_user_id: str | None = None,
        source: str = "unknown",
    ) -> None: ...

    def change_password(
        self,
        user_id: str,
        *,
        current_password: str,
        new_password: str,
        source: str = "unknown",
    ) -> None: ...

    def reset_password(
        self,
        user_id: str,
        *,
        new_password: str,
        actor_user_id: str,
        source: str = "local-admin",
    ) -> None: ...

    def set_account_status(
        self,
        user_id: str,
        status: SiteAccountStatus,
        *,
        actor_user_id: str,
        source: str = "local-admin",
    ) -> SiteAccount: ...

    def record_admin_content_access(
        self,
        *,
        actor_user_id: str,
        owner_user_id: str,
        content_type: str,
        content_id: str | None,
        outcome: str,
        source: str,
    ) -> None: ...


class RequestServiceResolver(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> HealthView: ...

    async def build_info(self) -> BuildInfoView: ...

    async def resolve(self, request: Request) -> DerivationService: ...


class TenantContentReader(Protocol):
    """Read persisted tenant evidence without starting a tenant runtime."""

    async def list_runs(self, user_id: str) -> list[RunSummary]: ...

    async def get_run(self, user_id: str, run_id: str) -> RunView: ...

    async def list_intakes(self, user_id: str) -> list[IntakeSessionView]: ...

    async def get_intake(self, user_id: str, session_id: str) -> IntakeSessionView: ...


__all__ = [
    "AccountConflict",
    "AccountLocked",
    "AccountNotFound",
    "AuthenticatedSession",
    "IdentityError",
    "InvalidCredentials",
    "PasswordPolicyError",
    "RequestServiceResolver",
    "SessionGrant",
    "SessionInvalid",
    "SiteAccount",
    "SiteAccountStatus",
    "SiteIdentityAuthority",
    "SitePermissionDenied",
    "SiteRole",
    "SourceRateLimited",
    "TemporaryPasswordExpired",
    "TenantContentReader",
]
