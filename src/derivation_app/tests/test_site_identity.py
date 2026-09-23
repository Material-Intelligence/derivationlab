from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argon2 import PasswordHasher

from derivation_app.site_identity import (
    AccountConflict,
    AccountLocked,
    InvalidCredentials,
    PasswordPolicyError,
    SessionInvalid,
    SiteAccountStatus,
    SiteIdentityStore,
    SiteRole,
    SourceRateLimited,
    TemporaryPasswordExpired,
)


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SiteIdentityStoreTests(unittest.TestCase):
    password = "correct-horse-battery-staple"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "identity.sqlite"
        self.clock = MutableClock()
        self.store = SiteIdentityStore(
            self.database,
            password_hasher=PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
            ),
            now=self.clock,
        )

    def create_user(self, *, temporary: bool = False):
        return self.store.create_account(
            username="alice",
            email="Alice@Example.edu",
            password=self.password,
            role=SiteRole.USER,
            temporary_password=temporary,
            actor_user_id="admin-1",
        )

    def test_read_closes_database_connection(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row

        with patch.object(self.store, "_connect", return_value=connection):
            self.store.list_accounts()

        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
            connection.execute("SELECT 1")

    def test_account_uses_argon2id_and_enforces_unique_canonical_identity(self) -> None:
        account = self.create_user()

        with sqlite3.connect(self.database) as connection:
            stored_hash = connection.execute(
                "SELECT password_hash FROM site_accounts WHERE user_id = ?",
                (account.user_id,),
            ).fetchone()[0]
            database_bytes = self.database.read_bytes()

        self.assertTrue(stored_hash.startswith("$argon2id$"))
        self.assertNotIn(self.password.encode(), database_bytes)
        self.assertEqual(account.email, "Alice@Example.edu")
        with self.assertRaises(AccountConflict):
            self.store.create_account(
                username="ALICE",
                email="different@example.edu",
                password="another-correct-horse-password",
            )
        with self.assertRaises(AccountConflict):
            self.store.create_account(
                username="different",
                email="alice@example.edu",
                password="another-correct-horse-password",
            )

    def test_rejects_short_common_and_predictable_passwords(self) -> None:
        for password in (
            "12345678901234",
            "passwordpassword",
            "Password123456!",
            "alicealicealice",
        ):
            with (
                self.subTest(password=password),
                self.assertRaises(PasswordPolicyError),
            ):
                self.store.create_account(
                    username="alice",
                    email="alice@example.edu",
                    password=password,
                )

    def test_administrator_created_password_is_permanent_by_default(self) -> None:
        account = self.store.create_account(
            username="bob",
            email="bob@example.edu",
            password="bob-correct-private-password",
            actor_user_id="admin-1",
        )

        grant = self.store.login("bob", "bob-correct-private-password", source="test")
        self.assertFalse(grant.account.must_change_password)
        with sqlite3.connect(self.database) as connection:
            flags = connection.execute(
                """
                SELECT password_temporary, temporary_password_expires_at,
                       must_change_password
                FROM site_accounts WHERE user_id = ?
                """,
                (account.user_id,),
            ).fetchone()
        self.assertEqual(flags, (0, None, 0))

    def test_login_accepts_username_or_email_and_persists_only_token_digest(
        self,
    ) -> None:
        account = self.create_user()

        first = self.store.login("ALICE", self.password, source="test")
        authenticated = self.store.authenticate_session(first.token)
        second = self.store.login("alice@example.edu", self.password, source="test")

        self.assertEqual(authenticated.account.user_id, account.user_id)
        self.assertEqual(second.account.user_id, account.user_id)
        with sqlite3.connect(self.database) as connection:
            persisted = connection.execute(
                "SELECT token_digest FROM site_sessions WHERE session_id = ?",
                (first.session_id,),
            ).fetchone()[0]
        self.assertEqual(len(persisted), 64)
        self.assertNotEqual(persisted, first.token)
        self.assertNotIn(first.token.encode(), self.database.read_bytes())

    def test_fifth_failure_locks_for_thirty_minutes(self) -> None:
        self.create_user()

        for _ in range(4):
            with self.assertRaises(InvalidCredentials):
                self.store.login("alice", "wrong-password-value", source="test")
        with self.assertRaises(AccountLocked):
            self.store.login("alice", "wrong-password-value", source="test")
        with self.assertRaises(AccountLocked):
            self.store.login("alice", self.password, source="test")

        self.clock.advance(30 * 60 + 1)
        grant = self.store.login("alice", self.password, source="test")
        self.assertEqual(grant.account.username, "alice")

    def test_failed_attempts_outside_window_do_not_accumulate(self) -> None:
        self.create_user()

        for _ in range(8):
            with self.assertRaises(InvalidCredentials):
                self.store.login("alice", "wrong-password-value", source="test")
            self.clock.advance(15 * 60 + 1)

        self.assertEqual(
            self.store.login("alice", self.password, source="test").account.username,
            "alice",
        )

    def test_twentieth_failure_rate_limits_one_source_without_storing_it(self) -> None:
        source = "http:198.51.100.22"
        for attempt in range(19):
            with self.subTest(attempt=attempt), self.assertRaises(InvalidCredentials):
                self.store.login(f"missing-{attempt}", "wrong-password", source=source)
        with self.assertRaises(SourceRateLimited):
            self.store.login("missing-final", "wrong-password", source=source)
        with self.assertRaises(SourceRateLimited):
            self.store.login("another-account", "wrong-password", source=source)

        with sqlite3.connect(self.database) as connection:
            digest = connection.execute(
                "SELECT source_digest FROM login_source_limits"
            ).fetchone()[0]
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, source)
        self.clock.advance(30 * 60 + 1)
        with self.assertRaises(InvalidCredentials):
            self.store.login("missing-after-lock", "wrong-password", source=source)

    def test_temporary_password_expires_and_change_revokes_existing_sessions(
        self,
    ) -> None:
        account = self.create_user(temporary=True)
        grant = self.store.login("alice", self.password, source="test")
        self.assertTrue(grant.account.must_change_password)

        replacement = "a-new-long-private-password"
        self.store.change_password(
            account.user_id,
            current_password=self.password,
            new_password=replacement,
            source="test",
        )
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(grant.token)
        refreshed = self.store.login("alice", replacement, source="test")
        self.assertFalse(refreshed.account.must_change_password)

        second_store = SiteIdentityStore(
            Path(self.temporary_directory.name) / "expired.sqlite",
            password_hasher=PasswordHasher(
                time_cost=1, memory_cost=8192, parallelism=1
            ),
            now=self.clock,
        )
        second_store.create_account(
            username="bob",
            email="bob@example.edu",
            password=self.password,
            temporary_password=True,
        )
        self.clock.advance(24 * 60 * 60 + 1)
        with self.assertRaises(TemporaryPasswordExpired):
            second_store.login("bob", self.password, source="test")

    def test_idle_absolute_logout_reset_and_disable_invalidate_sessions(self) -> None:
        account = self.create_user()
        idle = self.store.login("alice", self.password, source="test")
        self.clock.advance(12 * 60 * 60 + 1)
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(idle.token)

        active = self.store.login("alice", self.password, source="test")
        self.store.logout(active.token, source="test")
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(active.token)

        reset = self.store.login("alice", self.password, source="test")
        replacement = "temporary-reset-password-long"
        self.store.reset_password(
            account.user_id,
            new_password=replacement,
            actor_user_id="admin-1",
        )
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(reset.token)

        enabled = self.store.login("alice", replacement, source="test")
        self.assertFalse(enabled.account.must_change_password)
        disabled = self.store.set_account_status(
            account.user_id,
            SiteAccountStatus.DISABLED,
            actor_user_id="admin-1",
        )
        self.assertEqual(disabled.status, SiteAccountStatus.DISABLED)
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(enabled.token)

    def test_session_absolute_expiry_is_not_extended_by_activity(self) -> None:
        self.create_user()
        grant = self.store.login("alice", self.password, source="test")
        for _ in range(13):
            self.clock.advance(11 * 60 * 60)
            self.store.authenticate_session(grant.token)
        self.clock.value = grant.absolute_expires_at
        with self.assertRaises(SessionInvalid):
            self.store.authenticate_session(grant.token)

    def test_audit_is_metadata_only(self) -> None:
        self.create_user()
        grant = self.store.login("alice", self.password, source="test-browser")
        self.store.logout(grant.token, source="test-browser")

        events = self.store.list_audit_events()
        encoded = repr(events)
        self.assertEqual(
            [event.action for event in events],
            ["account.create", "session.login", "session.logout"],
        )
        self.assertNotIn(self.password, encoded)
        self.assertNotIn(grant.token, encoded)

    def test_database_path_is_private_and_rejects_symlinks(self) -> None:
        if os.name != "nt":
            self.assertEqual(self.database.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

        target = Path(self.temporary_directory.name) / "target.sqlite"
        target.touch()
        symlink = Path(self.temporary_directory.name) / "identity-link.sqlite"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(Exception, "must not be a symlink"):
            SiteIdentityStore(
                symlink,
                password_hasher=PasswordHasher(
                    time_cost=1, memory_cost=8192, parallelism=1
                ),
            )


if __name__ == "__main__":
    unittest.main()
