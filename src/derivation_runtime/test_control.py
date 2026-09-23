from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from .control import SCHEMA_VERSION, ControlStore


class ControlStoreMigrationTests(unittest.TestCase):
    def test_v2_attestation_columns_are_removed_without_losing_run_bookmark(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "control.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES (2, '2026-08-30T00:00:00Z');
                CREATE TABLE runtime_runs (
                    run_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    pause_requested INTEGER NOT NULL,
                    pause_actor_id TEXT,
                    pause_reason TEXT,
                    stop_reason TEXT,
                    credential_profile_id TEXT NOT NULL,
                    record_event_seq INTEGER NOT NULL,
                    record_event_sha256 TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    execution_attestation_sequence INTEGER NOT NULL DEFAULT 0,
                    execution_attestation_sha256 TEXT,
                    terminal_provider_event_sequence INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO runtime_runs VALUES (
                    'run_legacy', 'paused', 1, 'human', 'review', NULL,
                    'profile', 7, 'head', NULL, 'created', 'updated',
                    3, 'legacy', 9
                );
                """
            )
            connection.commit()
            connection.close()

            with ControlStore(path) as store:
                bookmark = store.run("run_legacy")
                columns = {
                    row[1]
                    for row in store._connection.execute(
                        "PRAGMA table_info(runtime_runs)"
                    )
                }
                version = store._connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]

            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(bookmark.record_event_seq, 7)
            self.assertTrue(bookmark.pause_requested)
            self.assertFalse(any("attestation" in name for name in columns))
            self.assertNotIn("terminal_provider_event_sequence", columns)


if __name__ == "__main__":
    unittest.main()
