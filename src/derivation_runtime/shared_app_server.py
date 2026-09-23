"""Fail-closed multiplexing for independent runtimes on one App Server child.

Only this broker consumes the destructive ``AppServerClient.next_event`` queue.
Each acquired session owns its thread and turn namespace, dynamic-tool objects,
audit destination, and runtime workspace.  The underlying child is closed only
after the final session lease is released.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_server_client import AppServerClient, ClientDynamicTool
from .app_server_protocol import (
    AppServerError,
    AppServerForkError,
    AppServerProtocolError,
    AppServerStateError,
    AppServerTimeoutError,
    JsonObject,
    ProviderEvent,
)

# App Server emits a small number of notifications that belong to the child
# process rather than to any one thread.  They are the only threadless events a
# broker may accept: everything else without a routable thread stays fatal, so
# an unattributable model action can never be silently split between runs.
# Keeping this list explicit (rather than derived) is intentional; a protocol
# upgrade must re-review each addition.
PROCESS_SCOPE_METHODS = frozenset(
    {
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "app/list/updated",
        "configWarning",
        "deprecationNotice",
        "mcpServer/oauthLogin/completed",
        "mcpServer/startupStatus/updated",
        "remoteControl/status/changed",
        "skills/changed",
        "warning",
        "windows/worldWritableWarning",
        "windowsSandbox/setupCompleted",
    }
)


@dataclass(frozen=True)
class _Fatal:
    error: AppServerError


@dataclass
class _TurnOwner:
    """The single session that owns a turn, and the threads it appears on.

    One turn belongs to exactly one session for the life of the broker; that is
    what keeps two runs sharing a child from reading each other's events.  It
    can appear on more than one thread of that session, because ``thread/fork``
    copies the anchor's history into the child under the original turn ids.
    """

    session_id: int
    thread_ids: set[str]

    def covers(self, session_id: int, thread_id: str) -> bool:
        return self.session_id == session_id and thread_id in self.thread_ids


@dataclass
class _SessionState:
    workspace: Path
    audit_path: Path | None
    queue: asyncio.Queue[ProviderEvent | _Fatal]
    on_release: Callable[[], None] | None = None
    tools: dict[str, ClientDynamicTool] = field(default_factory=dict)
    threads: set[str] = field(default_factory=set)
    turns: set[str] = field(default_factory=set)
    last_sequence: int = 0
    closed: bool = False


class SharedAppServerBroker:
    """Own one client event pump and issue isolated, refcounted sessions."""

    def __init__(
        self,
        client: AppServerClient,
        *,
        session_event_queue_size: int = 2048,
        pending_event_limit: int = 2048,
        process_event_limit: int = 4096,
        process_event_observer: Callable[[ProviderEvent], None] | None = None,
    ) -> None:
        if not client.is_running:
            raise AppServerStateError("shared App Server client must be running")
        if (
            session_event_queue_size <= 0
            or pending_event_limit <= 0
            or process_event_limit <= 0
        ):
            raise ValueError("shared App Server broker limits must be positive")
        self.client = client
        self._session_event_queue_size = session_event_queue_size
        self._pending_event_limit = pending_event_limit
        self._process_event_limit = process_event_limit
        self._process_event_observer = process_event_observer
        self._process_events: list[ProviderEvent] = []
        self._sessions: dict[int, _SessionState] = {}
        self._thread_owners: dict[str, int] = {}
        # A turn id maps to the one session that owns it and to every thread
        # of that session the turn is visible on.  It is more than one thread
        # exactly after a fork: ``thread/fork`` copies the anchor history into
        # the child thread with the *same* turn ids, so the parent thread and
        # the child thread both state those turns.  Cross-session ownership
        # stays exclusive, which is the invariant that matters: a thread has
        # one owning session, and a fork is only allowed to the session that
        # already owns the parent thread.
        self._turn_owners: dict[str, _TurnOwner] = {}
        self._next_session_id = 0
        self._pump_task: asyncio.Task[None] | None = None
        self._fatal_error: AppServerError | None = None
        self._closed = False
        self._release_lock = asyncio.Lock()
        self._thread_registration_lock = asyncio.Lock()
        self._turn_registration_lock = asyncio.Lock()
        self._pending_thread_owner: int | None = None
        self._pending_thread_events: dict[str, list[ProviderEvent]] = {}
        self._pending_turn_owner: tuple[int, str] | None = None
        self._pending_turn_events: dict[str, list[ProviderEvent]] = {}
        self._routed_sequence = 0
        self._progress = asyncio.Condition()

    @property
    def fatal_error(self) -> AppServerError | None:
        return self._fatal_error

    @property
    def process_events(self) -> tuple[ProviderEvent, ...]:
        """Threadless child-process notifications kept out of every session."""

        return tuple(self._process_events)

    async def acquire_session(
        self,
        *,
        workspace: str | os.PathLike[str],
        dynamic_tool_audit_path: str | os.PathLike[str] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> SharedAppServerSession:
        """Acquire one runtime-facing view without starting another child."""

        if self._closed:
            raise AppServerStateError("shared App Server broker is closed")
        self._raise_if_failed()
        resolved_workspace = Path(workspace).resolve()
        if not resolved_workspace.is_dir():
            raise AppServerStateError("shared session workspace must be a directory")
        audit_path = AppServerClient._validated_dynamic_tool_audit_path(
            dynamic_tool_audit_path
        )
        self._next_session_id += 1
        session_id = self._next_session_id
        self._sessions[session_id] = _SessionState(
            workspace=resolved_workspace,
            audit_path=audit_path,
            queue=asyncio.Queue(maxsize=self._session_event_queue_size),
            on_release=on_release,
        )
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(
                self._pump(), name="shared-app-server-event-broker"
            )
        return SharedAppServerSession(self, session_id)

    def _state(self, session_id: int) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None or state.closed:
            raise AppServerStateError("shared App Server session is closed")
        self._raise_if_failed()
        return state

    def _raise_if_failed(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error

    async def _pump(self) -> None:
        error: AppServerError | None = None
        try:
            while True:
                event = await self.client.next_event()
                self._route_event(event)
                async with self._progress:
                    self._routed_sequence = max(self._routed_sequence, event.sequence)
                    self._progress.notify_all()
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            error = exc
        except BaseException as exc:  # noqa: BLE001 - fatal broker boundary
            error = AppServerProtocolError(f"shared event broker failed: {exc}")
        if error is not None:
            await self._poison(error)

    async def _poison(self, error: AppServerError) -> None:
        if self._fatal_error is not None:
            return
        self._fatal_error = error
        fatal = _Fatal(error)
        for state in self._sessions.values():
            while not state.queue.empty():
                state.queue.get_nowait()
            state.queue.put_nowait(fatal)
        async with self._progress:
            self._progress.notify_all()
        await self.client.close()

    @staticmethod
    def _event_turn_id(event: ProviderEvent) -> str | None:
        turn = event.params.get("turn")
        if isinstance(turn, Mapping) and isinstance(turn.get("id"), str):
            return turn["id"]
        turn_id = event.params.get("turnId")
        return turn_id if isinstance(turn_id, str) else None

    @staticmethod
    def _event_thread_id(event: ProviderEvent) -> str | None:
        thread_id = event.params.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
        # ``thread/started`` carries its identity inside the thread snapshot.
        thread = event.params.get("thread")
        if isinstance(thread, Mapping):
            nested = thread.get("id")
            if isinstance(nested, str) and nested:
                return nested
        return None

    def _route_process_event(self, event: ProviderEvent) -> None:
        if event.method not in PROCESS_SCOPE_METHODS:
            raise AppServerProtocolError(
                f"shared broker received an unscoped event: {event.method}"
            )
        if len(self._process_events) >= self._process_event_limit:
            raise AppServerProtocolError("shared broker process event buffer is full")
        self._process_events.append(event)
        if self._process_event_observer is not None:
            self._process_event_observer(event)

    def _route_event(self, event: ProviderEvent) -> None:
        thread_id = self._event_thread_id(event)
        if thread_id is None:
            self._route_process_event(event)
            return
        owner = self._thread_owners.get(thread_id)
        if owner is None:
            if self._pending_thread_owner is None:
                raise AppServerProtocolError(
                    f"shared broker received an event for unknown thread {thread_id!r}"
                )
            self._buffer_pending(
                self._pending_thread_events, thread_id, event, "thread"
            )
            return
        state = self._sessions.get(owner)
        if state is None or state.closed:
            raise AppServerProtocolError(
                "shared broker received an event for a released session"
            )
        turn_id = self._event_turn_id(event)
        if turn_id is not None:
            turn_owner = self._turn_owners.get(turn_id)
            if turn_owner is None:
                if self._pending_turn_owner != (owner, thread_id):
                    raise AppServerProtocolError(
                        f"shared broker received an event for unknown turn {turn_id!r}"
                    )
                self._buffer_pending(self._pending_turn_events, turn_id, event, "turn")
                return
            if not turn_owner.covers(owner, thread_id):
                raise AppServerProtocolError(
                    "shared broker observed cross-thread turn correlation"
                )
        self._deliver(owner, event)

    def _buffer_pending(
        self,
        buffer: dict[str, list[ProviderEvent]],
        correlation_id: str,
        event: ProviderEvent,
        label: str,
    ) -> None:
        count = sum(len(events) for events in buffer.values())
        if count >= self._pending_event_limit:
            raise AppServerProtocolError(
                f"shared broker pending {label} event buffer is full"
            )
        buffer.setdefault(correlation_id, []).append(event)

    def _deliver(self, owner: int, event: ProviderEvent) -> None:
        state = self._sessions[owner]
        if state.queue.full():
            raise AppServerProtocolError("shared session event queue is full")
        state.last_sequence = max(state.last_sequence, event.sequence)
        state.queue.put_nowait(event)

    def _validate_workspace(
        self, state: _SessionState, params: Mapping[str, Any]
    ) -> None:
        expected = str(state.workspace)
        if params.get("cwd") != expected:
            raise AppServerStateError(
                "shared session thread cwd differs from its workspace"
            )
        if params.get("runtimeWorkspaceRoots") != [expected]:
            raise AppServerStateError(
                "shared session runtimeWorkspaceRoots differ from its workspace"
            )

    async def _thread_start(
        self,
        session_id: int,
        params: Mapping[str, Any],
        *,
        require_persistent: bool,
    ) -> JsonObject:
        state = self._state(session_id)
        self._validate_workspace(state, params)
        async with self._thread_registration_lock:
            self._pending_thread_owner = session_id
            self._pending_thread_events = {}
            try:
                result = await self.client.thread_start(
                    params,
                    require_persistent=require_persistent,
                    dynamic_tools=tuple(state.tools.values()),
                    dynamic_tool_audit_path=state.audit_path,
                )
                self._claim_thread(session_id, result)
                return result
            except AppServerProtocolError as exc:
                if self._fatal_error is None:
                    await self._poison(exc)
                raise
            finally:
                self._pending_thread_owner = None
                self._pending_thread_events = {}

    async def _thread_resume(
        self,
        session_id: int,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None,
        expected_reasoning_effort: str,
        require_persistent: bool,
    ) -> JsonObject:
        state = self._state(session_id)
        self._validate_workspace(state, overrides or {})
        owner = self._thread_owners.get(thread_id)
        if owner is not None and owner != session_id:
            raise AppServerStateError(
                "shared session cannot resume another session's provider thread"
            )
        async with self._thread_registration_lock:
            self._pending_thread_owner = session_id
            self._pending_thread_events = {}
            try:
                result = await self.client.thread_resume(
                    thread_id,
                    overrides=overrides,
                    expected_reasoning_effort=expected_reasoning_effort,
                    require_persistent=require_persistent,
                    dynamic_tools=tuple(state.tools.values()),
                    dynamic_tool_audit_path=state.audit_path,
                )
                if owner is None:
                    self._claim_thread(session_id, result)
                else:
                    self._claim_snapshot_turns(session_id, thread_id, result)
                return result
            except AppServerProtocolError as exc:
                if self._fatal_error is None:
                    await self._poison(exc)
                raise
            finally:
                self._pending_thread_owner = None
                self._pending_thread_events = {}

    async def _thread_fork(
        self,
        session_id: int,
        thread_id: str,
        last_turn_id: str,
        *,
        overrides: Mapping[str, Any] | None,
        expected_reasoning_effort: str,
        require_persistent: bool,
        require_completed_turn: bool,
    ) -> JsonObject:
        state = self._state(session_id)
        self._require_thread_owner(session_id, thread_id)
        self._validate_workspace(state, overrides or {})
        async with self._thread_registration_lock:
            self._pending_thread_owner = session_id
            self._pending_thread_events = {}
            try:
                result = await self.client.thread_fork(
                    thread_id,
                    last_turn_id,
                    overrides=overrides,
                    expected_reasoning_effort=expected_reasoning_effort,
                    require_persistent=require_persistent,
                    require_completed_turn=require_completed_turn,
                    dynamic_tools=tuple(state.tools.values()),
                    dynamic_tool_audit_path=state.audit_path,
                )
                self._claim_thread(session_id, result)
                return result
            except AppServerForkError:
                # The client proved the rejection is confined to the forked
                # thread it refused to adopt, so the borrowing run loses its
                # fork and every other run keeps the child.
                raise
            except AppServerProtocolError as exc:
                if self._fatal_error is None:
                    await self._poison(exc)
                raise
            finally:
                self._pending_thread_owner = None
                self._pending_thread_events = {}

    def _claim_thread(self, session_id: int, result: Mapping[str, Any]) -> None:
        thread = result.get("thread")
        if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
            raise AppServerProtocolError(
                "shared broker could not claim thread response"
            )
        thread_id = thread["id"]
        existing = self._thread_owners.get(thread_id)
        if existing is not None and existing != session_id:
            raise AppServerProtocolError("shared broker thread was claimed twice")
        unexpected = set(self._pending_thread_events) - {thread_id}
        if unexpected:
            raise AppServerProtocolError(
                f"shared broker observed unclaimed early threads: {sorted(unexpected)}"
            )
        self._thread_owners[thread_id] = session_id
        state = self._sessions[session_id]
        state.threads.add(thread_id)
        self._claim_snapshot_turns(session_id, thread_id, result)
        for event in self._pending_thread_events.get(thread_id, ()):
            self._route_event(event)

    def _claim_snapshot_turns(
        self, session_id: int, thread_id: str, result: Mapping[str, Any]
    ) -> None:
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            raise AppServerProtocolError("shared broker thread snapshot is missing")
        turns = thread.get("turns", [])
        if not isinstance(turns, list):
            raise AppServerProtocolError("shared broker thread turns are malformed")
        for turn in turns:
            if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
                raise AppServerProtocolError("shared broker turn snapshot is malformed")
            self._claim_turn_id(session_id, thread_id, turn["id"])

    async def _turn_start(
        self,
        session_id: int,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None,
    ) -> JsonObject:
        self._state(session_id)
        self._require_thread_owner(session_id, thread_id)
        async with self._turn_registration_lock:
            self._pending_turn_owner = (session_id, thread_id)
            self._pending_turn_events = {}
            try:
                result = await self.client.turn_start(
                    thread_id, input_items, overrides=overrides
                )
                turn = result.get("turn")
                if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
                    raise AppServerProtocolError(
                        "shared broker could not claim turn response"
                    )
                turn_id = turn["id"]
                unexpected = set(self._pending_turn_events) - {turn_id}
                if unexpected:
                    raise AppServerProtocolError(
                        f"shared broker observed unclaimed early turns: {sorted(unexpected)}"
                    )
                self._claim_turn_id(session_id, thread_id, turn_id)
                for event in self._pending_turn_events.get(turn_id, ()):
                    self._route_event(event)
                return result
            except AppServerProtocolError as exc:
                if self._fatal_error is None:
                    await self._poison(exc)
                raise
            finally:
                self._pending_turn_owner = None
                self._pending_turn_events = {}

    def _claim_turn_id(self, session_id: int, thread_id: str, turn_id: str) -> None:
        """Record one more thread of ``session_id`` that states ``turn_id``.

        A second *session* claiming a turn is broker state corruption and stays
        refused.  A second *thread* of the same session is what a fork produces:
        ``thread/fork`` returns a child whose snapshot repeats the anchor's
        turns under their original ids, and ``thread/read`` on that child
        repeats them again.  Refusing those cost a live fork probe its whole
        run, because the refusal is an ``AppServerProtocolError``, which poisons
        the shared child for every run borrowing it.
        """

        existing = self._turn_owners.get(turn_id)
        if existing is None:
            self._turn_owners[turn_id] = _TurnOwner(session_id, {thread_id})
        elif existing.session_id != session_id:
            raise AppServerProtocolError("shared broker turn was claimed twice")
        else:
            existing.thread_ids.add(thread_id)
        self._sessions[session_id].turns.add(turn_id)

    def _require_thread_owner(self, session_id: int, thread_id: str) -> None:
        if self._thread_owners.get(thread_id) != session_id:
            raise AppServerStateError("shared session does not own the provider thread")

    async def _thread_read(
        self, session_id: int, thread_id: str, *, include_turns: bool
    ) -> JsonObject:
        self._state(session_id)
        self._require_thread_owner(session_id, thread_id)
        try:
            result = await self.client.thread_read(
                thread_id, include_turns=include_turns
            )
            if include_turns:
                self._claim_snapshot_turns(session_id, thread_id, result)
            return result
        except AppServerProtocolError as exc:
            if self._fatal_error is None:
                await self._poison(exc)
            raise

    async def _turn_interrupt(
        self, session_id: int, thread_id: str, turn_id: str
    ) -> JsonObject:
        self._state(session_id)
        self._require_thread_owner(session_id, thread_id)
        owner = self._turn_owners.get(turn_id)
        if owner is None or not owner.covers(session_id, thread_id):
            raise AppServerStateError("shared session does not own the provider turn")
        return await self.client.turn_interrupt(thread_id, turn_id)

    async def _next_event(
        self, session_id: int, *, timeout: float | None
    ) -> ProviderEvent:
        state = self._state(session_id)
        try:
            if timeout is None:
                item = await state.queue.get()
            else:
                if timeout <= 0:
                    raise ValueError("event timeout must be positive")
                item = await asyncio.wait_for(state.queue.get(), timeout)
        except TimeoutError as exc:
            raise AppServerTimeoutError("next_event", timeout or 0.0) from exc
        if isinstance(item, _Fatal):
            raise item.error
        return item

    async def _protocol_barrier(self, session_id: int) -> int:
        state = self._state(session_id)
        target = await self.client.protocol_barrier()
        async with self._progress:
            await self._progress.wait_for(
                lambda: self._routed_sequence >= target or self._fatal_error is not None
            )
        self._raise_if_failed()
        return state.last_sequence

    async def _release(self, session_id: int) -> None:
        async with self._release_lock:
            state = self._sessions.get(session_id)
            if state is None or state.closed:
                return
            state.closed = True
            if state.on_release is not None:
                state.on_release()
            if any(not candidate.closed for candidate in self._sessions.values()):
                return
            self._closed = True
            if self._pump_task is not None and not self._pump_task.done():
                self._pump_task.cancel()
                await asyncio.gather(self._pump_task, return_exceptions=True)
            await self.client.close()


class SharedAppServerSession:
    """Runtime-facing, thread-scoped lease on a shared App Server broker."""

    def __init__(self, broker: SharedAppServerBroker, session_id: int) -> None:
        self._broker = broker
        self._session_id = session_id

    @property
    def broker(self) -> SharedAppServerBroker:
        return self._broker

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def _state(self) -> _SessionState:
        return self._broker._state(self._session_id)

    @property
    def is_running(self) -> bool:
        state = self._broker._sessions.get(self._session_id)
        return bool(
            state is not None
            and not state.closed
            and self._broker.client.is_running
            and self._broker.fatal_error is None
        )

    @property
    def server_version(self) -> str | None:
        return self._broker.client.server_version

    @property
    def protocol_pin(self) -> Any:
        return self._broker.client.protocol_pin

    @property
    def command(self) -> tuple[str, ...]:
        return self._broker.client.command

    @property
    def cwd(self) -> Path:
        cwd = self._broker.client.cwd
        if cwd is None:
            raise AppServerStateError("shared App Server client has no process cwd")
        return cwd

    @property
    def workspace(self) -> Path:
        return self._state.workspace

    @property
    def env(self) -> Mapping[str, str] | None:
        return self._broker.client.env

    @property
    def process_identity(self) -> Any:
        return self._broker.client.process_identity

    @property
    def returncode(self) -> int | None:
        return self._broker.client.returncode

    @property
    def dynamic_tool_audit_path(self) -> Path | None:
        return self._state.audit_path

    def configure_dynamic_tools(self, tools: Sequence[ClientDynamicTool]) -> None:
        state = self._state
        configured = AppServerClient._validated_dynamic_tools(tools)
        if state.tools:
            if set(state.tools) != set(configured) or any(
                state.tools[name] is not configured[name] for name in configured
            ):
                raise AppServerStateError(
                    "shared session dynamic tool set cannot change"
                )
            return
        if state.threads:
            raise AppServerStateError(
                "shared session tools must be configured before its first thread"
            )
        state.tools = configured

    def dynamic_tool_specs(self, names: Sequence[str]) -> list[JsonObject]:
        state = self._state
        if len(set(names)) != len(names):
            raise AppServerStateError("dynamic tool selection contains duplicates")
        result: list[JsonObject] = []
        for name in names:
            tool = state.tools.get(name)
            if tool is None:
                raise AppServerStateError(
                    f"dynamic tool {name!r} is not configured for this session"
                )
            result.append(copy.deepcopy(dict(tool.spec)))
        return result

    async def thread_start(
        self, params: Mapping[str, Any], *, require_persistent: bool = True
    ) -> JsonObject:
        return await self._broker._thread_start(
            self._session_id, params, require_persistent=require_persistent
        )

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
    ) -> JsonObject:
        return await self._broker._thread_resume(
            self._session_id,
            thread_id,
            overrides=overrides,
            expected_reasoning_effort=expected_reasoning_effort,
            require_persistent=require_persistent,
        )

    async def thread_fork(
        self,
        thread_id: str,
        last_turn_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
        require_completed_turn: bool = True,
    ) -> JsonObject:
        return await self._broker._thread_fork(
            self._session_id,
            thread_id,
            last_turn_id,
            overrides=overrides,
            expected_reasoning_effort=expected_reasoning_effort,
            require_persistent=require_persistent,
            require_completed_turn=require_completed_turn,
        )

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        return await self._broker._turn_start(
            self._session_id, thread_id, input_items, overrides=overrides
        )

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> JsonObject:
        return await self._broker._thread_read(
            self._session_id, thread_id, include_turns=include_turns
        )

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonObject:
        return await self._broker._turn_interrupt(self._session_id, thread_id, turn_id)

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent:
        return await self._broker._next_event(self._session_id, timeout=timeout)

    async def protocol_barrier(self) -> int:
        return await self._broker._protocol_barrier(self._session_id)

    async def close(self) -> None:
        await self._broker._release(self._session_id)


__all__ = ["SharedAppServerBroker", "SharedAppServerSession"]
