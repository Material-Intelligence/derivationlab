"""Read-only projections of persisted tenant content for administrators."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from derivation_api.models import (
    IntakeSessionView,
    RunCommandCapabilities,
    RunSummary,
    RunView,
)
from derivation_api.service import DerivationServiceError, ErrorKind

from derivation_agent_record import ContractError, load_events, sha256_text

from .intake_session import _session_from_json, intake_session_payload
from .projection import build_run_view, displayed_run_view
from .service import (
    _confirmed_intake_problem,
    _last_complete_canonical,
    _parse_manifest,
)

_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FilesystemTenantContentReader:
    """Project existing evidence without creating or starting runtime services."""

    def __init__(self, tenant_root: str | Path) -> None:
        self.root = Path(tenant_root).resolve()

    async def list_runs(self, user_id: str) -> list[RunSummary]:
        return await asyncio.to_thread(self._list_runs, user_id)

    async def get_run(self, user_id: str, run_id: str) -> RunView:
        return await asyncio.to_thread(self._get_run, user_id, run_id)

    async def list_intakes(self, user_id: str) -> list[IntakeSessionView]:
        return await asyncio.to_thread(self._list_intakes, user_id)

    async def get_intake(self, user_id: str, session_id: str) -> IntakeSessionView:
        return await asyncio.to_thread(self._get_intake, user_id, session_id)

    def _tenant_directory(self, user_id: str) -> Path:
        if _USER_ID_RE.fullmatch(user_id) is None:
            raise self._not_found("tenant", user_id)
        users_root = self.root / "users"
        tenant_root = users_root / user_id
        if users_root.is_symlink() or tenant_root.is_symlink():
            raise self._unavailable("tenant storage contains a symbolic link")
        if tenant_root.exists() and (
            not tenant_root.is_dir()
            or tenant_root.resolve().parent != users_root.resolve()
        ):
            raise self._unavailable("tenant storage path is unsafe")
        return tenant_root

    def _run_root(self, user_id: str) -> Path:
        tenant_root = self._tenant_directory(user_id)
        run_root = tenant_root / "runs"
        if run_root.is_symlink() or (run_root.exists() and not run_root.is_dir()):
            raise self._unavailable("tenant run storage path is unsafe")
        return run_root

    def _list_runs(self, user_id: str) -> list[RunSummary]:
        run_root = self._run_root(user_id)
        if not run_root.exists():
            return []
        views: list[RunView] = []
        try:
            for directory in run_root.iterdir():
                manifest = directory / "manifest.json"
                if manifest.exists():
                    views.append(self._load_run(run_root, manifest))
        except DerivationServiceError:
            raise
        except (
            ContractError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise self._unavailable("tenant run evidence could not be read") from exc
        return sorted(
            (self._summary(view) for view in views),
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )

    def _get_run(self, user_id: str, run_id: str) -> RunView:
        if _CONTENT_ID_RE.fullmatch(run_id) is None:
            raise self._not_found("run", run_id)
        run_root = self._run_root(user_id)
        manifest = run_root / run_id / "manifest.json"
        if not manifest.exists():
            raise self._not_found("run", run_id)
        try:
            return self._load_run(run_root, manifest)
        except DerivationServiceError:
            raise
        except (
            ContractError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise self._unavailable("tenant run evidence could not be read") from exc

    def _load_run(self, run_root: Path, manifest_path: Path) -> RunView:
        self._validate_run_directory(run_root, manifest_path)
        value, command, _capability_profile, config = _parse_manifest(manifest_path)
        events = load_events(manifest_path.parent / "events.jsonl")
        canonical = _last_complete_canonical(events)
        run_record = canonical["run"]
        expected_record = {
            "run_id": config.run_id,
            "task": config.task.to_record(),
            "pack": config.pack.to_record(),
            "code_commit": config.code_commit,
            "configuration": config.record_configuration(),
            "input_policy": config.input_policy.to_record(),
            "record_spec": config.record_spec.to_record(),
            "event_schema": config.event_schema.to_record(),
            "canonical_schema": config.canonical_schema.to_record(),
        }
        if any(
            run_record.get(key) != expected for key, expected in expected_record.items()
        ):
            raise RuntimeError("run manifest differs from Record V1")
        if sha256_text(command.task_text) != config.task.sha256:
            raise RuntimeError("run problem differs from Record V1 task")
        hard_interrupt_requested = value.get("hard_interrupt_requested", False)
        error_message = value.get("error_message")
        if not isinstance(hard_interrupt_requested, bool):
            raise TypeError("invalid hard interrupt marker")
        if error_message is not None and not isinstance(error_message, str):
            raise TypeError("invalid run error message")
        phase = (
            "interrupted"
            if hard_interrupt_requested
            else "error"
            if error_message
            else None
        )
        view = build_run_view(
            canonical=canonical,
            question=command.question,
            api_config=command.config,
            runtime_config=command.runtime,
            record_events=events,
            phase=phase,
            pause_requested=False,
            hard_interrupt_requested=hard_interrupt_requested,
            error_message=error_message,
            step_bookmark=lambda _step_id: None,
            call_bookmark=lambda _call_id: None,
        )
        if view.id != config.run_id:
            raise RuntimeError("Record run id differs from manifest")
        # An administrator reads the same text the account's own reader shows:
        # the verified typeset math where a layer exists, the sealed text
        # otherwise. The Record and every hash in the view are unchanged.
        view = displayed_run_view(manifest_path.parent, view)
        return view.model_copy(
            deep=True,
            update={"read_only": True, "commands": RunCommandCapabilities()},
        )

    @staticmethod
    def _validate_run_directory(run_root: Path, manifest_path: Path) -> None:
        directory = manifest_path.parent
        if _CONTENT_ID_RE.fullmatch(directory.name) is None:
            raise RuntimeError("invalid run directory name")
        if directory != run_root / directory.name:
            raise RuntimeError("run directory is not an exact storage child")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("run directory is unsafe")
        if directory.resolve().parent != run_root.resolve():
            raise RuntimeError("run directory escapes tenant storage")
        event_log = directory / "events.jsonl"
        for required in (manifest_path, event_log):
            if required.is_symlink() or not required.is_file():
                raise RuntimeError("run evidence file is unsafe")

    def _intake_database(self, user_id: str) -> Path:
        tenant_root = self._tenant_directory(user_id)
        intake_root = tenant_root / "intakes"
        database = intake_root / "control.sqlite"
        if intake_root.is_symlink() or (
            intake_root.exists() and not intake_root.is_dir()
        ):
            raise self._unavailable("tenant intake storage path is unsafe")
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise self._unavailable("tenant intake database path is unsafe")
        return database

    def _connect_read_only(self, database: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _list_intakes(self, user_id: str) -> list[IntakeSessionView]:
        database = self._intake_database(user_id)
        if not database.exists():
            return []
        try:
            with closing(self._connect_read_only(database)) as connection:
                rows = connection.execute(
                    "SELECT snapshot_json FROM intake_sessions ORDER BY session_id"
                ).fetchall()
                return [
                    self._intake_view(connection, row["snapshot_json"]) for row in rows
                ]
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            raise self._unavailable("tenant intake evidence could not be read") from exc

    def _get_intake(self, user_id: str, session_id: str) -> IntakeSessionView:
        if _CONTENT_ID_RE.fullmatch(session_id) is None:
            raise self._not_found("intake session", session_id)
        database = self._intake_database(user_id)
        if not database.exists():
            raise self._not_found("intake session", session_id)
        try:
            with closing(self._connect_read_only(database)) as connection:
                row = connection.execute(
                    "SELECT snapshot_json FROM intake_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise self._not_found("intake session", session_id)
                return self._intake_view(connection, row["snapshot_json"])
        except DerivationServiceError:
            raise
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            raise self._unavailable("tenant intake evidence could not be read") from exc

    def _intake_view(
        self, connection: sqlite3.Connection, snapshot_json: str
    ) -> IntakeSessionView:
        session = _session_from_json(snapshot_json)
        rows = connection.execute(
            """
            SELECT event_id, kind, payload_json FROM intake_events
            WHERE session_id = ? ORDER BY sequence
            """,
            (session.session_id,),
        ).fetchall()
        payload: dict[str, Any] = intake_session_payload(session)
        payload["conversation"] = [
            {
                "event_id": row["event_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        payload["frozen_problem"] = (
            _confirmed_intake_problem(session).model_dump(mode="json")
            if session.status.value == "confirmed"
            else None
        )
        return IntakeSessionView.model_validate(payload)

    @staticmethod
    def _summary(view: RunView) -> RunSummary:
        return RunSummary(
            id=view.id,
            question=view.question,
            status=view.status,
            phase=view.phase,
            step_count=len(view.steps),
            route_count=len(view.routes),
            read_only=True,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    @staticmethod
    def _not_found(kind: str, identifier: str) -> DerivationServiceError:
        return DerivationServiceError(
            ErrorKind.NOT_FOUND,
            "tenant_content_not_found",
            f"Tenant {kind} {identifier!r} does not exist.",
        )

    @staticmethod
    def _unavailable(message: str) -> DerivationServiceError:
        return DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "tenant_content_unavailable",
            message,
        )


__all__ = ["FilesystemTenantContentReader"]
