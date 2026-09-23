"""Disposable SQLite control bookmarks for the derivation runtime.

This database never stores scientific prose or provider transcripts.  Deleting
it loses native resume handles, not Record V1 scientific truth.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .record import utc_now
from .types import RunConfig, RunPhase, RuntimeInvocation

SCHEMA_VERSION = 6

RECORD_CALL_STATE_TO_CONTROL = {
    "started": "starting",
    "finished": "finished",
    "failed": "failed",
    "aborted": "aborted",
}
CONTROL_TERMINAL_CALL_STATES = frozenset({"finished", "failed", "aborted"})


def _control_call_state(record_state: str) -> str:
    try:
        return RECORD_CALL_STATE_TO_CONTROL[record_state]
    except KeyError as exc:
        raise RuntimeError(
            f"unsupported Record model-call state {record_state!r}"
        ) from exc


def _event_seq(event_id: str | None) -> int | None:
    if event_id is None:
        return None
    prefix = "evt_"
    if not event_id.startswith(prefix) or not event_id[len(prefix) :].isdigit():
        raise RuntimeError(f"invalid Record event id {event_id!r}")
    return int(event_id[len(prefix) :])


@dataclass(frozen=True)
class RunBookmark:
    run_id: str
    phase: str
    pause_requested: bool
    pause_actor_id: str | None
    pause_reason: str | None
    stop_reason: str | None
    credential_profile_id: str
    record_event_seq: int
    record_event_sha256: str | None
    last_error: str | None


@dataclass(frozen=True)
class BranchBookmark:
    run_id: str
    branch_id: str
    runtime_state: str
    provider_session_id: str | None
    provider_lineage: str | None
    last_operation_id: str | None
    last_step_revision_id: str | None
    attempt: int
    last_error: str | None


@dataclass(frozen=True)
class StepBookmark:
    run_id: str
    step_revision_id: str
    branch_id: str
    provider_session_id: str | None
    provider_operation_id: str | None
    provider_lineage: str | None


@dataclass(frozen=True)
class RuntimeDisposition:
    """One host decision the record cannot state in a payload of its own."""

    run_id: str
    branch_id: str
    kind: str
    detail: str
    recorded_at: str


@dataclass(frozen=True)
class CallBookmark:
    run_id: str
    model_call_id: str
    branch_id: str | None
    role: str
    target_kind: str
    target_id: str
    attempt: int
    state: str
    provider_session_id: str | None
    provider_operation_id: str | None
    provider_lineage: str | None
    record_start_seq: int
    record_terminal_seq: int | None
    last_error: str | None


class ControlStore:
    """One-process SQLite bookmark store with an explicit migration table."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._invocation_observer: Callable[[str, str], None] | None = None
        self._migrate()

    def set_invocation_observer(
        self,
        observer: Callable[[str, str], None] | None,
    ) -> None:
        """Observe provider-handle attachment without persisting UI state."""

        self._invocation_observer = observer

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"control.sqlite schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current < 1:
                connection.executescript(
                    """
                    CREATE TABLE runtime_runs (
                        run_id TEXT PRIMARY KEY,
                        phase TEXT NOT NULL,
                        pause_requested INTEGER NOT NULL CHECK (pause_requested IN (0, 1)),
                        pause_actor_id TEXT,
                        pause_reason TEXT,
                        stop_reason TEXT,
                        credential_profile_id TEXT NOT NULL,
                        record_event_seq INTEGER NOT NULL,
                        record_event_sha256 TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE runtime_branches (
                        run_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        runtime_state TEXT NOT NULL,
                        provider_session_id TEXT,
                        provider_lineage TEXT,
                        last_operation_id TEXT,
                        last_step_revision_id TEXT,
                        attempt INTEGER NOT NULL,
                        last_error TEXT,
                        claimed_at TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, branch_id),
                        FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                    );

                    CREATE TABLE runtime_steps (
                        run_id TEXT NOT NULL,
                        step_revision_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        provider_session_id TEXT,
                        provider_operation_id TEXT,
                        provider_lineage TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, step_revision_id),
                        FOREIGN KEY (run_id, branch_id)
                            REFERENCES runtime_branches(run_id, branch_id) ON DELETE CASCADE
                    );

                    CREATE TABLE runtime_calls (
                        run_id TEXT NOT NULL,
                        model_call_id TEXT NOT NULL,
                        branch_id TEXT,
                        role TEXT NOT NULL,
                        target_kind TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        provider_session_id TEXT,
                        provider_operation_id TEXT,
                        provider_lineage TEXT,
                        record_start_seq INTEGER NOT NULL,
                        record_terminal_seq INTEGER,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, model_call_id),
                        FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                    );

                    CREATE INDEX runtime_calls_in_flight
                        ON runtime_calls(run_id, state, record_start_seq);
                    CREATE INDEX runtime_steps_by_branch
                        ON runtime_steps(run_id, branch_id);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, utc_now()),
                )
                current = 1
            if current < 2:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, utc_now()),
                )
                current = 2
            if current < 3:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(runtime_runs)")
                }
                for column in (
                    "execution_attestation_sequence",
                    "execution_attestation_sha256",
                    "terminal_provider_event_sequence",
                ):
                    if column in columns:
                        connection.execute(
                            f"ALTER TABLE runtime_runs DROP COLUMN {column}"
                        )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, utc_now()),
                )
                current = 3
            if current < 4:
                # Append-only, and deliberately outside the run's cascade: every
                # other table here is disposable because it can be rebuilt from
                # the record, while a disposition is a host decision the record
                # states only by omission.  A rebuild must therefore not erase
                # it.
                connection.executescript(
                    """
                    CREATE TABLE runtime_dispositions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );

                    CREATE INDEX runtime_dispositions_by_run
                        ON runtime_dispositions(run_id, id);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, utc_now()),
                )

            if current < 5:
                connection.execute(
                    "CREATE TABLE formula_audits (run_id TEXT NOT NULL, "
                    "model_call_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "PRIMARY KEY(run_id, model_call_id))"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, utc_now()),
                )
            if current < 6:
                connection.execute(
                    "CREATE TABLE formula_audit_rechecks (id INTEGER PRIMARY KEY, "
                    "run_id TEXT NOT NULL, model_call_id TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, utc_now()),
                )

    def formula_audits(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Durable host decisions; deliberately survive bookmark rebuilds."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT model_call_id, payload FROM formula_audits WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            rows += self._connection.execute(
                "SELECT model_call_id, payload FROM formula_audit_rechecks WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return {row["model_call_id"]: json.loads(row["payload"]) for row in rows}

    def record_formula_audit(
        self,
        run_id: str,
        model_call_id: str,
        payload: Mapping[str, Any],
        *,
        recheck_infrastructure: bool = False,
    ) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)
        if recheck_infrastructure:
            previous = self.formula_audits(run_id).get(model_call_id)
            if (
                previous is None
                or previous["disposition"] != "infrastructure_failure"
                or previous["output_sha256"] != payload["output_sha256"]
                or previous["policy"] != payload["policy"]
            ):
                raise RuntimeError(
                    "only unchanged infrastructure failures can be rechecked"
                )
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO formula_audit_rechecks(run_id, model_call_id, payload) VALUES (?, ?, ?)",
                    (run_id, model_call_id, encoded),
                )
            return
        with self.transaction() as connection:
            old = connection.execute(
                "SELECT payload FROM formula_audits WHERE run_id=? AND model_call_id=?",
                (run_id, model_call_id),
            ).fetchone()
            if old is not None:
                if old["payload"] != encoded:
                    raise RuntimeError("formula audit is immutable")
                return
            connection.execute(
                "INSERT INTO formula_audits VALUES (?, ?, ?)",
                (run_id, model_call_id, encoded),
            )

    def record_disposition(
        self, run_id: str, branch_id: str, *, kind: str, detail: str
    ) -> None:
        """Append one host decision to the control-plane log."""

        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_dispositions("
                "run_id, branch_id, kind, detail, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, branch_id, kind, detail, utc_now()),
            )

    def dispositions(
        self, run_id: str, *, kind: str | None = None
    ) -> list[RuntimeDisposition]:
        query = "SELECT * FROM runtime_dispositions WHERE run_id=?"
        parameters: tuple[Any, ...] = (run_id,)
        if kind is not None:
            query += " AND kind=?"
            parameters += (kind,)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY id", parameters)
            return [
                RuntimeDisposition(
                    run_id=row["run_id"],
                    branch_id=row["branch_id"],
                    kind=row["kind"],
                    detail=row["detail"],
                    recorded_at=row["recorded_at"],
                )
                for row in rows
            ]

    def ensure_run(self, config: RunConfig, *, phase: RunPhase) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_runs(
                    run_id, phase, pause_requested, pause_actor_id, pause_reason,
                    stop_reason, credential_profile_id, record_event_seq,
                    record_event_sha256, last_error, created_at, updated_at
                ) VALUES (?, ?, 0, NULL, NULL, NULL, ?, 0, NULL, NULL, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (config.run_id, phase.value, config.credential_profile_id, now, now),
            )

    def update_phase(
        self,
        run_id: str,
        phase: RunPhase,
        *,
        stop_reason: str | None,
        last_error: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE runtime_runs SET phase=?, stop_reason=?, last_error=?, updated_at=? WHERE run_id=?",
                (phase.value, stop_reason, last_error, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run {run_id}")

    def sync_record_head(
        self, run_id: str, event_seq: int, event_sha256: str | None
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE runtime_runs SET record_event_seq=?, record_event_sha256=?, updated_at=? WHERE run_id=?",
                (event_seq, event_sha256, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run {run_id}")

    def request_pause(self, run_id: str, *, actor_id: str, reason: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_runs
                SET pause_requested=1, pause_actor_id=?, pause_reason=?, updated_at=?
                WHERE run_id=?
                """,
                (actor_id, reason, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run {run_id}")

    def clear_pause(self, run_id: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_runs
                SET pause_requested=0, pause_actor_id=NULL, pause_reason=NULL, updated_at=?
                WHERE run_id=?
                """,
                (utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run {run_id}")

    def run(self, run_id: str) -> RunBookmark:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        return RunBookmark(
            run_id=row["run_id"],
            phase=row["phase"],
            pause_requested=bool(row["pause_requested"]),
            pause_actor_id=row["pause_actor_id"],
            pause_reason=row["pause_reason"],
            stop_reason=row["stop_reason"],
            credential_profile_id=row["credential_profile_id"],
            record_event_seq=row["record_event_seq"],
            record_event_sha256=row["record_event_sha256"],
            last_error=row["last_error"],
        )

    def upsert_branch(
        self,
        run_id: str,
        branch_id: str,
        *,
        runtime_state: str,
        provider_session_id: str | None,
        provider_lineage: str | None,
        last_operation_id: str | None,
        last_step_revision_id: str | None,
        attempt: int,
        last_error: str | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_branches(
                    run_id, branch_id, runtime_state, provider_session_id,
                    provider_lineage, last_operation_id, last_step_revision_id,
                    attempt, last_error, claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(run_id, branch_id) DO UPDATE SET
                    runtime_state=excluded.runtime_state,
                    provider_session_id=COALESCE(excluded.provider_session_id, runtime_branches.provider_session_id),
                    provider_lineage=COALESCE(excluded.provider_lineage, runtime_branches.provider_lineage),
                    last_operation_id=COALESCE(excluded.last_operation_id, runtime_branches.last_operation_id),
                    last_step_revision_id=COALESCE(excluded.last_step_revision_id, runtime_branches.last_step_revision_id),
                    attempt=excluded.attempt,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    branch_id,
                    runtime_state,
                    provider_session_id,
                    provider_lineage,
                    last_operation_id,
                    last_step_revision_id,
                    attempt,
                    last_error,
                    utc_now(),
                ),
            )

    def branch(self, run_id: str, branch_id: str) -> BranchBookmark | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_branches WHERE run_id=? AND branch_id=?",
                (run_id, branch_id),
            ).fetchone()
        if row is None:
            return None
        return BranchBookmark(
            run_id=row["run_id"],
            branch_id=row["branch_id"],
            runtime_state=row["runtime_state"],
            provider_session_id=row["provider_session_id"],
            provider_lineage=row["provider_lineage"],
            last_operation_id=row["last_operation_id"],
            last_step_revision_id=row["last_step_revision_id"],
            attempt=row["attempt"],
            last_error=row["last_error"],
        )

    def record_step(
        self,
        run_id: str,
        step_revision_id: str,
        branch_id: str,
        invocation: RuntimeInvocation,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_steps(
                    run_id, step_revision_id, branch_id, provider_session_id,
                    provider_operation_id, provider_lineage, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_revision_id) DO UPDATE SET
                    provider_session_id=excluded.provider_session_id,
                    provider_operation_id=excluded.provider_operation_id,
                    provider_lineage=excluded.provider_lineage,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    step_revision_id,
                    branch_id,
                    invocation.session.session_id,
                    invocation.operation_id,
                    invocation.session.lineage.value,
                    utc_now(),
                ),
            )

    def step(self, run_id: str, step_revision_id: str) -> StepBookmark | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_steps WHERE run_id=? AND step_revision_id=?",
                (run_id, step_revision_id),
            ).fetchone()
        if row is None:
            return None
        return StepBookmark(
            run_id=row["run_id"],
            step_revision_id=row["step_revision_id"],
            branch_id=row["branch_id"],
            provider_session_id=row["provider_session_id"],
            provider_operation_id=row["provider_operation_id"],
            provider_lineage=row["provider_lineage"],
        )

    def next_attempt(
        self, run_id: str, role: str, target_kind: str, target_id: str
    ) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT MAX(attempt) AS attempt FROM runtime_calls
                WHERE run_id=? AND role=? AND target_kind=? AND target_id=?
                """,
                (run_id, role, target_kind, target_id),
            ).fetchone()
        return int(row["attempt"] or 0) + 1

    def start_call(
        self,
        *,
        run_id: str,
        model_call_id: str,
        branch_id: str | None,
        role: str,
        target_kind: str,
        target_id: str,
        attempt: int,
        record_start_seq: int,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_calls(
                    run_id, model_call_id, branch_id, role, target_kind,
                    target_id, attempt, state, provider_session_id,
                    provider_operation_id, provider_lineage, record_start_seq,
                    record_terminal_seq, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', NULL, NULL, NULL, ?, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    model_call_id,
                    branch_id,
                    role,
                    target_kind,
                    target_id,
                    attempt,
                    record_start_seq,
                    now,
                    now,
                ),
            )

    def attach_invocation(
        self, run_id: str, model_call_id: str, invocation: RuntimeInvocation
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_calls
                SET state='running', provider_session_id=?, provider_operation_id=?,
                    provider_lineage=?, updated_at=?
                WHERE run_id=? AND model_call_id=? AND state='starting'
                """,
                (
                    invocation.session.session_id,
                    invocation.operation_id,
                    invocation.session.lineage.value,
                    utc_now(),
                    run_id,
                    model_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"call {model_call_id} is not in starting state")
        if self._invocation_observer is not None:
            self._invocation_observer(run_id, model_call_id)

    def finish_call(
        self,
        run_id: str,
        model_call_id: str,
        *,
        state: str,
        record_terminal_seq: int,
        last_error: str | None,
    ) -> None:
        if state not in CONTROL_TERMINAL_CALL_STATES:
            raise ValueError(f"invalid terminal control call state {state!r}")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_calls
                SET state=?, record_terminal_seq=?, last_error=?, updated_at=?
                WHERE run_id=? AND model_call_id=? AND state IN ('starting', 'running')
                """,
                (
                    state,
                    record_terminal_seq,
                    last_error,
                    utc_now(),
                    run_id,
                    model_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"call {model_call_id} is not in-flight")

    def call(self, run_id: str, model_call_id: str) -> CallBookmark | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_calls WHERE run_id=? AND model_call_id=?",
                (run_id, model_call_id),
            ).fetchone()
        return self._call_from_row(row) if row is not None else None

    def in_flight_calls(self, run_id: str) -> list[CallBookmark]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM runtime_calls
                WHERE run_id=? AND state IN ('starting', 'running')
                ORDER BY record_start_seq, model_call_id
                """,
                (run_id,),
            ).fetchall()
        return [self._call_from_row(row) for row in rows]

    @staticmethod
    def _call_from_row(row: sqlite3.Row) -> CallBookmark:
        return CallBookmark(
            run_id=row["run_id"],
            model_call_id=row["model_call_id"],
            branch_id=row["branch_id"],
            role=row["role"],
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            attempt=row["attempt"],
            state=row["state"],
            provider_session_id=row["provider_session_id"],
            provider_operation_id=row["provider_operation_id"],
            provider_lineage=row["provider_lineage"],
            record_start_seq=row["record_start_seq"],
            record_terminal_seq=row["record_terminal_seq"],
            last_error=row["last_error"],
        )

    def rebuild_from_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        credential_profile_id: str,
        phase: RunPhase,
        stop_reason: str | None,
        pause_requested: bool = False,
        pause_actor_id: str | None = None,
        pause_reason: str | None = None,
        preserve_existing: bool = False,
    ) -> None:
        """Rebuild disposable rows without inventing provider handles."""

        if pause_requested and (not pause_actor_id or not pause_reason):
            raise ValueError("preserved pause intent requires actor and reason")
        if not pause_requested and (
            pause_actor_id is not None or pause_reason is not None
        ):
            raise ValueError("pause metadata requires pause_requested")

        run_id = snapshot["run"]["run_id"]
        head = snapshot["event_log"]
        steps_by_id = {
            item["step_revision_id"]: item for item in snapshot["step_revisions"]
        }
        checks_by_id = {item["check_id"]: item for item in snapshot["checks"]}
        candidates_by_id = {
            item["candidate_id"]: item for item in snapshot["candidates"]
        }
        judgements_by_id = {
            item["judgement_id"]: item for item in snapshot["judgements"]
        }
        now = utc_now()
        with self.transaction() as connection:
            if not preserve_existing:
                connection.execute("DELETE FROM runtime_runs WHERE run_id=?", (run_id,))
            connection.execute(
                """
                INSERT INTO runtime_runs(
                    run_id, phase, pause_requested, pause_actor_id, pause_reason,
                    stop_reason, credential_profile_id, record_event_seq,
                    record_event_sha256, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    record_event_seq=excluded.record_event_seq,
                    record_event_sha256=excluded.record_event_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    phase.value,
                    int(pause_requested),
                    pause_actor_id,
                    pause_reason,
                    stop_reason,
                    credential_profile_id,
                    head["event_count"],
                    head["head_event_sha256"],
                    now,
                    now,
                ),
            )
            for branch in snapshot["branches"]:
                connection.execute(
                    """
                    INSERT INTO runtime_branches(
                        run_id, branch_id, runtime_state, provider_session_id,
                        provider_lineage, last_operation_id, last_step_revision_id,
                        attempt, last_error, claimed_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, 0, NULL, NULL, ?)
                    ON CONFLICT(run_id, branch_id) DO NOTHING
                    """,
                    (
                        run_id,
                        branch["branch_id"],
                        branch["status"],
                        branch["step_revision_ids"][-1]
                        if branch["step_revision_ids"]
                        else None,
                        now,
                    ),
                )
            for step in snapshot["step_revisions"]:
                connection.execute(
                    """
                    INSERT INTO runtime_steps(
                        run_id, step_revision_id, branch_id, provider_session_id,
                        provider_operation_id, provider_lineage, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
                    ON CONFLICT(run_id, step_revision_id) DO NOTHING
                    """,
                    (run_id, step["step_revision_id"], step["branch_id"], now),
                )
            attempt_counts: dict[tuple[str, str, str], int] = {}
            for call in sorted(
                snapshot["model_calls"], key=lambda item: item["started_event_id"]
            ):
                if call["role"] == "writer":
                    target_kind = "writer_step"
                    target_id = (
                        f"{call['target']['branch_id']}:{call['target']['step_slot']}"
                    )
                    branch_id = call["target"]["branch_id"]
                elif call["role"] == "checker":
                    target_kind = "check"
                    target_id = call["target"]["check_id"]
                    check = checks_by_id[target_id]
                    branch_id = steps_by_id[check["target_step_revision_id"]][
                        "branch_id"
                    ]
                else:
                    target_kind = "judgement"
                    target_id = call["target"]["judgement_id"]
                    judgement = judgements_by_id[target_id]
                    branch_id = candidates_by_id[judgement["candidate_id"]]["branch_id"]
                attempt_key = (call["role"], target_kind, target_id)
                attempt_counts[attempt_key] = attempt_counts.get(attempt_key, 0) + 1
                connection.execute(
                    """
                    INSERT INTO runtime_calls(
                        run_id, model_call_id, branch_id, role, target_kind,
                        target_id, attempt, state, provider_session_id,
                        provider_operation_id, provider_lineage, record_start_seq,
                        record_terminal_seq, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, ?, ?)
                    ON CONFLICT(run_id, model_call_id) DO UPDATE SET
                        state=CASE WHEN excluded.state='starting' THEN runtime_calls.state ELSE excluded.state END,
                        record_terminal_seq=excluded.record_terminal_seq,
                        updated_at=excluded.updated_at
                    """,
                    (
                        run_id,
                        call["model_call_id"],
                        branch_id,
                        call["role"],
                        target_kind,
                        str(target_id),
                        attempt_counts[attempt_key],
                        _control_call_state(call["state"]),
                        _event_seq(call["started_event_id"]),
                        _event_seq(call.get("terminal_event_id")),
                        now,
                        now,
                    ),
                )

    def reconcile_from_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Merge validated Record truth without losing live handles or run errors."""
        bookmark = self.run(snapshot["run"]["run_id"])
        self.rebuild_from_snapshot(
            snapshot,
            credential_profile_id=bookmark.credential_profile_id,
            phase=RunPhase(bookmark.phase),
            stop_reason=bookmark.stop_reason,
            preserve_existing=True,
        )


__all__ = [
    "CONTROL_TERMINAL_CALL_STATES",
    "RECORD_CALL_STATE_TO_CONTROL",
    "SCHEMA_VERSION",
    "BranchBookmark",
    "CallBookmark",
    "ControlStore",
    "RunBookmark",
    "RuntimeDisposition",
    "StepBookmark",
]
