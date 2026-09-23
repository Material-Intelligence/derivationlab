"""Server-side website identities and opaque sessions for a small, administrator-managed server pilot.

This module deliberately knows nothing about HTTP cookies or scientific runs.
It owns the durable authentication facts that both layers need: Argon2id
password hashes, lockout state, revocable sessions, and metadata-only audit
events. Raw passwords and session tokens are never persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from derivation_api.site_access import (
    AccountConflict,
    AccountLocked,
    AccountNotFound,
    AuthenticatedSession,
    IdentityError,
    InvalidCredentials,
    PasswordPolicyError,
    SessionGrant,
    SessionInvalid,
    SiteAccount,
    SiteAccountStatus,
    SitePermissionDenied,
    SiteRole,
    SourceRateLimited,
    TemporaryPasswordExpired,
)
from zxcvbn import zxcvbn

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_MIN_PASSWORD_LENGTH = 15
_MAX_PASSWORD_LENGTH = 128
_MIN_PASSWORD_STRENGTH_SCORE = 3
_FAILURE_WINDOW_SECONDS = 15 * 60
_LOCK_SECONDS = 30 * 60
_SOURCE_FAILURE_LIMIT = 20
_SOURCE_FAILURE_WINDOW_SECONDS = 15 * 60
_SOURCE_LOCK_SECONDS = 30 * 60
_SESSION_IDLE_SECONDS = 12 * 60 * 60
_SESSION_ABSOLUTE_SECONDS = 7 * 24 * 60 * 60
_TEMPORARY_PASSWORD_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class AuditEvent:
    event_id: int
    occurred_at: float
    actor_user_id: str | None
    action: str
    target_type: str
    target_id: str | None
    outcome: str
    source: str
    details: dict[str, object]


def _canonical_email(value: str) -> str:
    canonical = value.strip().casefold()
    local, separator, domain = canonical.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "." not in domain
        or any(character.isspace() for character in canonical)
        or len(canonical) > 254
    ):
        raise ValueError("email must be a valid address")
    return canonical


def _validate_username(value: str) -> tuple[str, str]:
    display = value.strip()
    if not _USERNAME_RE.fullmatch(display):
        raise ValueError(
            "username must be 3-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return display, display.casefold()


def _password_user_inputs(username: str, email: str) -> tuple[str, ...]:
    local, _, domain = email.casefold().partition("@")
    return tuple(
        value
        for value in (
            "DerivationLab",
            username.strip(),
            local,
            domain,
        )
        if value
    )


def validate_password(password: str, *, user_inputs: tuple[str, ...] = ()) -> None:
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError("password must contain at least 15 characters")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError("password must contain at most 128 characters")
    strength = zxcvbn(
        password,
        user_inputs=list(user_inputs),
        max_length=_MAX_PASSWORD_LENGTH,
    )
    if int(strength["score"]) < _MIN_PASSWORD_STRENGTH_SCORE:
        raise PasswordPolicyError("password is too common or predictable")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class SiteIdentityStore:
    """SQLite WAL identity authority for a small, administrator-managed pilot."""

    def __init__(
        self,
        path: str | Path,
        *,
        password_hasher: PasswordHasher | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        requested = Path(path).expanduser().absolute()
        if os.path.lexists(requested) and requested.is_symlink():
            raise IdentityError("identity database must not be a symlink")
        if requested.parent.is_symlink():
            raise IdentityError("identity database directory must not be a symlink")
        requested.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            parent_details = requested.parent.stat()
            if parent_details.st_uid != os.getuid():
                raise IdentityError(
                    "identity database directory has an unexpected owner"
                )
            requested.parent.chmod(0o700)
        self.path = requested
        self._hasher = password_hasher or PasswordHasher()
        self._now = now
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))
        self._initialize()
        if self.path.is_symlink() or not self.path.is_file():
            raise IdentityError("identity database is unavailable or unsafe")
        if os.name != "nt":
            details = self.path.stat()
            if details.st_uid != os.getuid() or not stat.S_ISREG(details.st_mode):
                raise IdentityError("identity database ownership or type is unsafe")
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection, connection:
            yield connection

    def _initialize(self) -> None:
        with self._managed_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS site_accounts (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_canonical TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    email_canonical TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
                    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
                    password_hash TEXT NOT NULL,
                    password_temporary INTEGER NOT NULL CHECK (password_temporary IN (0, 1)),
                    temporary_password_expires_at REAL,
                    must_change_password INTEGER NOT NULL CHECK (must_change_password IN (0, 1)),
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    failed_login_window_started_at REAL,
                    locked_until REAL,
                    session_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS site_sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES site_accounts(user_id),
                    token_digest TEXT NOT NULL UNIQUE,
                    session_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    idle_expires_at REAL NOT NULL,
                    absolute_expires_at REAL NOT NULL,
                    revoked_at REAL
                );

                CREATE INDEX IF NOT EXISTS site_sessions_account
                    ON site_sessions(account_id);

                CREATE TABLE IF NOT EXISTS login_source_limits (
                    source_digest TEXT PRIMARY KEY,
                    failed_login_count INTEGER NOT NULL,
                    window_started_at REAL NOT NULL,
                    locked_until REAL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    actor_user_id TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    outcome TEXT NOT NULL,
                    source TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )

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
    ) -> SiteAccount:
        display_username, canonical_username = _validate_username(username)
        display_email = email.strip()
        canonical_email = _canonical_email(display_email)
        validate_password(
            password,
            user_inputs=_password_user_inputs(display_username, display_email),
        )
        password_hash = self._hasher.hash(password)
        now = self._now()
        user_id = uuid.uuid4().hex
        temporary_expires_at = (
            now + _TEMPORARY_PASSWORD_SECONDS if temporary_password else None
        )
        try:
            with self._managed_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO site_accounts (
                        user_id, username, username_canonical, email, email_canonical,
                        role, status, password_hash, password_temporary,
                        temporary_password_expires_at, must_change_password,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        display_username,
                        canonical_username,
                        display_email,
                        canonical_email,
                        role.value,
                        password_hash,
                        int(temporary_password),
                        temporary_expires_at,
                        int(temporary_password),
                        now,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    occurred_at=now,
                    actor_user_id=actor_user_id,
                    action="account.create",
                    target_type="site_account",
                    target_id=user_id,
                    outcome="success",
                    source=source,
                )
        except sqlite3.IntegrityError as exc:
            raise AccountConflict("username or email already exists") from exc
        return self.get_account(user_id)

    def get_account(self, user_id: str) -> SiteAccount:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM site_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise AccountNotFound("account is unavailable")
        return self._account(row)

    def list_accounts(self) -> list[SiteAccount]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM site_accounts ORDER BY username_canonical, user_id"
            ).fetchall()
        return [self._account(row) for row in rows]

    def login(
        self,
        identifier: str,
        password: str,
        *,
        source: str = "unknown",
    ) -> SessionGrant:
        canonical = identifier.strip().casefold()
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._source_is_locked(connection, source, now):
                self._audit(
                    connection,
                    occurred_at=now,
                    actor_user_id=None,
                    action="session.login",
                    target_type="site_account",
                    target_id=None,
                    outcome="source_rate_limited",
                    source=source,
                )
                connection.commit()
                raise SourceRateLimited("too many login attempts from this source")
            row = connection.execute(
                """
                SELECT * FROM site_accounts
                WHERE username_canonical = ? OR email_canonical = ?
                """,
                (canonical, canonical),
            ).fetchone()
            if row is None:
                self._verify_without_result(password, self._dummy_hash)
                source_locked = self._record_source_failure(connection, source, now)
                self._audit(
                    connection,
                    occurred_at=now,
                    actor_user_id=None,
                    action="session.login",
                    target_type="site_account",
                    target_id=None,
                    outcome=(
                        "source_rate_limited"
                        if source_locked
                        else "invalid_credentials"
                    ),
                    source=source,
                )
                connection.commit()
                if source_locked:
                    raise SourceRateLimited("too many login attempts from this source")
                raise InvalidCredentials("invalid username, email, or password")

            user_id = str(row["user_id"])
            locked_until = row["locked_until"]
            if locked_until is not None and float(locked_until) > now:
                source_locked = self._record_source_failure(connection, source, now)
                self._audit_login(connection, row, now, "locked", source)
                connection.commit()
                if source_locked:
                    raise SourceRateLimited("too many login attempts from this source")
                raise AccountLocked("account is temporarily locked")
            if row["status"] != SiteAccountStatus.ACTIVE.value:
                self._verify_without_result(password, str(row["password_hash"]))
                source_locked = self._record_source_failure(connection, source, now)
                self._audit_login(connection, row, now, "disabled", source)
                connection.commit()
                if source_locked:
                    raise SourceRateLimited("too many login attempts from this source")
                raise InvalidCredentials("invalid username, email, or password")
            if not self._verify_without_result(password, str(row["password_hash"])):
                locked = self._record_failed_login(connection, row, now)
                source_locked = self._record_source_failure(connection, source, now)
                self._audit_login(
                    connection,
                    row,
                    now,
                    "locked" if locked else "invalid_credentials",
                    source,
                )
                connection.commit()
                if source_locked:
                    raise SourceRateLimited("too many login attempts from this source")
                if locked:
                    raise AccountLocked("account is temporarily locked")
                raise InvalidCredentials("invalid username, email, or password")

            temporary_expires_at = row["temporary_password_expires_at"]
            if (
                bool(row["password_temporary"])
                and temporary_expires_at is not None
                and float(temporary_expires_at) <= now
            ):
                self._audit_login(
                    connection, row, now, "temporary_password_expired", source
                )
                connection.commit()
                raise TemporaryPasswordExpired("temporary password has expired")

            connection.execute(
                """
                UPDATE site_accounts
                SET failed_login_count = 0,
                    failed_login_window_started_at = NULL,
                    locked_until = NULL,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (now, user_id),
            )
            connection.execute(
                "DELETE FROM login_source_limits WHERE source_digest = ?",
                (_source_digest(source),),
            )
            token = secrets.token_urlsafe(32)
            session_id = uuid.uuid4().hex
            absolute_expires_at = now + _SESSION_ABSOLUTE_SECONDS
            idle_expires_at = min(now + _SESSION_IDLE_SECONDS, absolute_expires_at)
            connection.execute(
                """
                INSERT INTO site_sessions (
                    session_id, account_id, token_digest, session_version,
                    created_at, last_seen_at, idle_expires_at, absolute_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    _token_digest(token),
                    int(row["session_version"]),
                    now,
                    now,
                    idle_expires_at,
                    absolute_expires_at,
                ),
            )
            self._audit_login(connection, row, now, "success", source)
            refreshed = connection.execute(
                "SELECT * FROM site_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            assert refreshed is not None
        return SessionGrant(
            token=token,
            session_id=session_id,
            account=self._account(refreshed),
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )

    def authenticate_session(
        self,
        token: str,
        *,
        touch: bool = True,
    ) -> AuthenticatedSession:
        if not token:
            raise SessionInvalid("session is unavailable")
        now = self._now()
        with self._managed_connection() as connection:
            if touch:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT s.*, a.username, a.email, a.role, a.status,
                       a.must_change_password, a.created_at AS account_created_at,
                       a.updated_at AS account_updated_at,
                       a.session_version AS account_session_version
                FROM site_sessions AS s
                JOIN site_accounts AS a ON a.user_id = s.account_id
                WHERE s.token_digest = ?
                """,
                (_token_digest(token),),
            ).fetchone()
            if row is None or not self._session_row_is_valid(row, now):
                raise SessionInvalid("session is unavailable")
            idle_expires_at = float(row["idle_expires_at"])
            if touch:
                idle_expires_at = min(
                    now + _SESSION_IDLE_SECONDS, float(row["absolute_expires_at"])
                )
                connection.execute(
                    """
                    UPDATE site_sessions
                    SET last_seen_at = ?, idle_expires_at = ?
                    WHERE session_id = ?
                    """,
                    (now, idle_expires_at, row["session_id"]),
                )
        return AuthenticatedSession(
            session_id=str(row["session_id"]),
            account=SiteAccount(
                user_id=str(row["account_id"]),
                username=str(row["username"]),
                email=str(row["email"]),
                role=SiteRole(str(row["role"])),
                status=SiteAccountStatus(str(row["status"])),
                must_change_password=bool(row["must_change_password"]),
                created_at=float(row["account_created_at"]),
                updated_at=float(row["account_updated_at"]),
            ),
            idle_expires_at=idle_expires_at,
            absolute_expires_at=float(row["absolute_expires_at"]),
        )

    def logout(
        self,
        token: str,
        *,
        actor_user_id: str | None = None,
        source: str = "unknown",
    ) -> None:
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT session_id, account_id FROM site_sessions WHERE token_digest = ?",
                (_token_digest(token),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE site_sessions SET revoked_at = ? WHERE session_id = ?",
                (now, row["session_id"]),
            )
            self._audit(
                connection,
                occurred_at=now,
                actor_user_id=actor_user_id or str(row["account_id"]),
                action="session.logout",
                target_type="site_session",
                target_id=str(row["session_id"]),
                outcome="success",
                source=source,
            )

    def change_password(
        self,
        user_id: str,
        *,
        current_password: str,
        new_password: str,
        source: str = "unknown",
    ) -> None:
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM site_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None or row["status"] != SiteAccountStatus.ACTIVE.value:
                raise AccountNotFound("account is unavailable")
            validate_password(
                new_password,
                user_inputs=_password_user_inputs(
                    str(row["username"]), str(row["email"])
                ),
            )
            if not self._verify_without_result(
                current_password, str(row["password_hash"])
            ):
                self._audit_login(
                    connection, row, now, "invalid_current_password", source
                )
                connection.commit()
                raise InvalidCredentials("current password is invalid")
            self._replace_password(
                connection,
                row,
                new_password,
                now=now,
                temporary=False,
            )
            self._audit(
                connection,
                occurred_at=now,
                actor_user_id=user_id,
                action="account.password_change",
                target_type="site_account",
                target_id=user_id,
                outcome="success",
                source=source,
            )

    def reset_password(
        self,
        user_id: str,
        *,
        new_password: str,
        actor_user_id: str,
        source: str = "local-admin",
    ) -> None:
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM site_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise AccountNotFound("account is unavailable")
            validate_password(
                new_password,
                user_inputs=_password_user_inputs(
                    str(row["username"]), str(row["email"])
                ),
            )
            self._replace_password(
                connection,
                row,
                new_password,
                now=now,
                temporary=False,
            )
            self._audit(
                connection,
                occurred_at=now,
                actor_user_id=actor_user_id,
                action="account.password_reset",
                target_type="site_account",
                target_id=user_id,
                outcome="success",
                source=source,
            )

    def set_account_status(
        self,
        user_id: str,
        status: SiteAccountStatus,
        *,
        actor_user_id: str,
        source: str = "local-admin",
    ) -> SiteAccount:
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT role, status FROM site_accounts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if target is None:
                raise AccountNotFound("account is unavailable")
            if (
                status is SiteAccountStatus.DISABLED
                and target["role"] == SiteRole.ADMIN.value
                and target["status"] == SiteAccountStatus.ACTIVE.value
            ):
                active_admins = connection.execute(
                    """
                    SELECT COUNT(*) FROM site_accounts
                    WHERE role = 'admin' AND status = 'active'
                    """
                ).fetchone()[0]
                if int(active_admins) <= 1:
                    raise SitePermissionDenied(
                        "the last active administrator cannot be disabled"
                    )
            cursor = connection.execute(
                """
                UPDATE site_accounts
                SET status = ?, session_version = session_version + 1, updated_at = ?
                WHERE user_id = ?
                """,
                (status.value, now, user_id),
            )
            if cursor.rowcount != 1:
                raise AccountNotFound("account is unavailable")
            connection.execute(
                "UPDATE site_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self._audit(
                connection,
                occurred_at=now,
                actor_user_id=actor_user_id,
                action="account.status_change",
                target_type="site_account",
                target_id=user_id,
                outcome="success",
                source=source,
                details={"status": status.value},
            )
        return self.get_account(user_id)

    def list_audit_events(self) -> list[AuditEvent]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY event_id"
            ).fetchall()
        return [
            AuditEvent(
                event_id=int(row["event_id"]),
                occurred_at=float(row["occurred_at"]),
                actor_user_id=row["actor_user_id"],
                action=str(row["action"]),
                target_type=str(row["target_type"]),
                target_id=row["target_id"],
                outcome=str(row["outcome"]),
                source=str(row["source"]),
                details=json.loads(str(row["details_json"])),
            )
            for row in rows
        ]

    def record_admin_content_access(
        self,
        *,
        actor_user_id: str,
        owner_user_id: str,
        content_type: str,
        content_id: str | None,
        outcome: str,
        source: str,
    ) -> None:
        allowed_types = {
            "run_catalog",
            "run",
            "intake_catalog",
            "intake_session",
        }
        if content_type not in allowed_types:
            raise IdentityError("admin content type is invalid")
        if outcome not in {"success", "not_found", "failure"}:
            raise IdentityError("admin content access outcome is invalid")
        now = self._now()
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = connection.execute(
                "SELECT role, status FROM site_accounts WHERE user_id = ?",
                (actor_user_id,),
            ).fetchone()
            if (
                actor is None
                or actor["role"] != SiteRole.ADMIN.value
                or actor["status"] != SiteAccountStatus.ACTIVE.value
            ):
                raise SitePermissionDenied("administrator access is required")
            if outcome == "success":
                owner = connection.execute(
                    "SELECT 1 FROM site_accounts WHERE user_id = ?",
                    (owner_user_id,),
                ).fetchone()
                if owner is None:
                    raise AccountNotFound("account is unavailable")
            self._audit(
                connection,
                occurred_at=now,
                actor_user_id=actor_user_id,
                action="tenant.content_view",
                target_type=content_type,
                target_id=content_id,
                outcome=outcome,
                source=source,
                details={"owner_user_id": owner_user_id},
            )

    def _replace_password(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        new_password: str,
        *,
        now: float,
        temporary: bool,
    ) -> None:
        temporary_expires_at = now + _TEMPORARY_PASSWORD_SECONDS if temporary else None
        connection.execute(
            """
            UPDATE site_accounts
            SET password_hash = ?, password_temporary = ?,
                temporary_password_expires_at = ?, must_change_password = ?,
                failed_login_count = 0, failed_login_window_started_at = NULL,
                locked_until = NULL, session_version = session_version + 1,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                self._hasher.hash(new_password),
                int(temporary),
                temporary_expires_at,
                int(temporary),
                now,
                row["user_id"],
            ),
        )
        connection.execute(
            "UPDATE site_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
            (now, row["user_id"]),
        )

    def _record_failed_login(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: float,
    ) -> bool:
        window_started = row["failed_login_window_started_at"]
        if (
            window_started is None
            or now - float(window_started) >= _FAILURE_WINDOW_SECONDS
        ):
            count = 1
            window_started = now
        else:
            count = int(row["failed_login_count"]) + 1
        locked = count >= 5
        connection.execute(
            """
            UPDATE site_accounts
            SET failed_login_count = ?, failed_login_window_started_at = ?,
                locked_until = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                count,
                window_started,
                now + _LOCK_SECONDS if locked else None,
                now,
                row["user_id"],
            ),
        )
        return locked

    def _source_is_locked(
        self,
        connection: sqlite3.Connection,
        source: str,
        now: float,
    ) -> bool:
        row = connection.execute(
            "SELECT locked_until FROM login_source_limits WHERE source_digest = ?",
            (_source_digest(source),),
        ).fetchone()
        return (
            row is not None
            and row["locked_until"] is not None
            and float(row["locked_until"]) > now
        )

    def _record_source_failure(
        self,
        connection: sqlite3.Connection,
        source: str,
        now: float,
    ) -> bool:
        digest = _source_digest(source)
        row = connection.execute(
            "SELECT * FROM login_source_limits WHERE source_digest = ?",
            (digest,),
        ).fetchone()
        if (
            row is None
            or now - float(row["window_started_at"]) >= _SOURCE_FAILURE_WINDOW_SECONDS
        ):
            count = 1
            window_started_at = now
        else:
            count = int(row["failed_login_count"]) + 1
            window_started_at = float(row["window_started_at"])
        locked = count >= _SOURCE_FAILURE_LIMIT
        connection.execute(
            """
            INSERT INTO login_source_limits (
                source_digest, failed_login_count, window_started_at, locked_until
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(source_digest) DO UPDATE SET
                failed_login_count = excluded.failed_login_count,
                window_started_at = excluded.window_started_at,
                locked_until = excluded.locked_until
            """,
            (
                digest,
                count,
                window_started_at,
                now + _SOURCE_LOCK_SECONDS if locked else None,
            ),
        )
        return locked

    def _audit_login(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        occurred_at: float,
        outcome: str,
        source: str,
    ) -> None:
        self._audit(
            connection,
            occurred_at=occurred_at,
            actor_user_id=str(row["user_id"]) if outcome == "success" else None,
            action="session.login",
            target_type="site_account",
            target_id=str(row["user_id"]),
            outcome=outcome,
            source=source,
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        occurred_at: float,
        actor_user_id: str | None,
        action: str,
        target_type: str,
        target_id: str | None,
        outcome: str,
        source: str,
        details: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                occurred_at, actor_user_id, action, target_type,
                target_id, outcome, source, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_at,
                actor_user_id,
                action,
                target_type,
                target_id,
                outcome,
                source,
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _account(row: sqlite3.Row) -> SiteAccount:
        return SiteAccount(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            email=str(row["email"]),
            role=SiteRole(str(row["role"])),
            status=SiteAccountStatus(str(row["status"])),
            must_change_password=bool(row["must_change_password"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _session_row_is_valid(row: sqlite3.Row, now: float) -> bool:
        return (
            row["revoked_at"] is None
            and row["status"] == SiteAccountStatus.ACTIVE.value
            and int(row["session_version"]) == int(row["account_session_version"])
            and float(row["idle_expires_at"]) > now
            and float(row["absolute_expires_at"]) > now
        )

    def _verify_without_result(self, password: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            return False


__all__ = [
    "AccountConflict",
    "AccountLocked",
    "AccountNotFound",
    "AuditEvent",
    "AuthenticatedSession",
    "IdentityError",
    "InvalidCredentials",
    "PasswordPolicyError",
    "SessionGrant",
    "SessionInvalid",
    "SiteAccount",
    "SiteAccountStatus",
    "SiteIdentityStore",
    "SiteRole",
    "TemporaryPasswordExpired",
    "validate_password",
]
