"""Async JSONL stdio client for the pinned Codex App Server protocol."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from .app_server_protocol import (
    AccountLoginCompletion,
    AppServerEOFError,
    AppServerError,
    AppServerForkError,
    AppServerProcessError,
    AppServerProtocolError,
    AppServerRemoteError,
    AppServerStartupError,
    AppServerStateError,
    AppServerTimeoutError,
    ChatgptDeviceCodeLogin,
    DuplicateResponseError,
    EventBufferOverflowError,
    JsonObject,
    MalformedProtocolMessage,
    ProtocolPin,
    ProviderEvent,
    TurnNotCompletedError,
    UnexpectedResponseError,
    UnexpectedServerRequestError,
    UnknownProtocolStateError,
    extract_single_final_agent_message,
    normalize_notification,
    validate_account_login_completion,
    validate_account_rate_limits_response,
    validate_chatgpt_device_code_login_start,
    validate_thread_snapshot,
)

logger = logging.getLogger(__name__)


# A sealed turn is compared field by field, with one documented exception: the
# child renders the same turn in more than one *view*, and which view arrives
# depends on how the turn was observed rather than on anything the turn did.
#
# Observed against the product App Server: one completed turn stored from
# ``turn/completed`` carried ``itemsView: "summary"`` with only the final
# agentMessage, whose id was the provider message id.  The identical turn inside the very next
# ``thread/read`` and ``thread/fork`` snapshot carried ``itemsView: "full"``,
# the preceding userMessage as a new leading item, and positional item ids
# (``item-1``, ``item-2``).  Everything that states what the turn *did* was
# byte-identical across the two views: ``id``, ``status``, ``error``,
# ``startedAt``, ``completedAt``, ``durationMs``, and the final agentMessage's
# ``type``/``phase``/``text``.
#
# So the tolerated fields are exactly ``itemsView`` and, under a changed
# ``itemsView``, the item ``id``s plus items the wider view adds.  Two
# snapshots rendered in the *same* view must still be deep-equal: within one
# view a difference is a real change to a sealed turn.
_TURN_VIEW_FIELDS = frozenset({"items", "itemsView"})


def _turn_item_content(item: Any) -> Any:
    """One turn item without the id the view assigns to it."""

    if not isinstance(item, Mapping):
        return item
    return {key: value for key, value in item.items() if key != "id"}


def _is_ordered_subsequence(smaller: Sequence[Any], larger: Sequence[Any]) -> bool:
    remaining = iter(larger)
    return all(any(item == other for other in remaining) for item in smaller)


def same_terminal_turn(
    previous: Mapping[str, Any] | None, turn: Mapping[str, Any]
) -> bool:
    """Is ``turn`` the sealed ``previous`` turn, possibly in a wider view?

    Public because every holder of a recorded terminal turn has to compare it
    against a later rendering under exactly this rule - the runtime's fork
    anchor check included.  A second implementation of the rule would drift
    from this one, and the drift would only ever be visible in production.
    """

    if previous is None:
        return False
    if previous == turn:
        return True
    previous_view = previous.get("itemsView")
    current_view = turn.get("itemsView")
    if (
        not isinstance(previous_view, str)
        or not isinstance(current_view, str)
        or previous_view == current_view
    ):
        return False
    if {
        key: value for key, value in previous.items() if key not in _TURN_VIEW_FIELDS
    } != {key: value for key, value in turn.items() if key not in _TURN_VIEW_FIELDS}:
        return False
    previous_items = previous.get("items")
    current_items = turn.get("items")
    if not isinstance(previous_items, list) or not isinstance(current_items, list):
        return False
    try:
        if _turn_item_content(
            extract_single_final_agent_message(previous)
        ) != _turn_item_content(extract_single_final_agent_message(turn)):
            return False
    except AppServerProtocolError:
        # Only a completed turn carries a final message; an interrupted or
        # failed one is compared on its items alone.
        if previous.get("status") == "completed":
            return False
    narrow, wide = sorted((previous_items, current_items), key=len)
    return _is_ordered_subsequence(
        [_turn_item_content(item) for item in narrow],
        [_turn_item_content(item) for item in wide],
    )


@dataclass(frozen=True)
class ClientTimeouts:
    startup: float = 10.0
    request: float = 30.0
    close: float = 5.0

    def __post_init__(self) -> None:
        for name, value in (
            ("startup", self.startup),
            ("request", self.request),
            ("close", self.close),
        ):
            if value <= 0:
                raise ValueError(f"{name} timeout must be positive")


_DEFAULT_PROTOCOL_PIN = ProtocolPin()
_DEFAULT_CLIENT_TIMEOUTS = ClientTimeouts()

# Cumulative provider stdout one derivation run may cost.  The budget is
# counted per child, not per run, so a caller that lends one child to several
# runs must multiply this by the number of runs it will serve.
DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL = 64 * 1024 * 1024


@dataclass(frozen=True)
class SpawnedProcessIdentity:
    """Portable identity of the command used to start the App Server child.

    This detects accidental command/process mismatches. Platform conformance,
    signatures, and sandbox behavior belong in release tests, not startup.
    """

    canonical_executable: str
    executable_st_dev: int
    executable_st_ino: int
    pid: int
    spawn_started_monotonic_ns: int
    spawn_completed_monotonic_ns: int
    identity_source: Literal["resolved_argv0_stat"]


class _ClientState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class _PendingRequest:
    method: str
    future: asyncio.Future[Any]


class ClientDynamicTool(Protocol):
    """A client-owned dynamic tool with a pinned declaration and implementation."""

    name: str
    spec: Mapping[str, Any]

    async def invoke(self, arguments: object) -> Mapping[str, Any]: ...


class AppServerClient:
    """Own one App Server child and expose correlated async RPC calls.

    The browser-facing backend should create exactly one instance for its
    product-specific ``HOME``/``CODEX_HOME``.  This class intentionally does
    not merge environments or answer server-initiated approval requests.
    """

    def __init__(
        self,
        command: Sequence[str] = ("codex", "app-server", "--stdio"),
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        protocol_pin: ProtocolPin = _DEFAULT_PROTOCOL_PIN,
        timeouts: ClientTimeouts = _DEFAULT_CLIENT_TIMEOUTS,
        client_info: Mapping[str, str] | None = None,
        capabilities: Mapping[str, Any] | None = None,
        stderr_limit_bytes: int = 64 * 1024,
        event_queue_size: int = 2048,
        max_protocol_line_bytes: int = 8 * 1024 * 1024,
        max_protocol_bytes_total: int = DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL,
        max_pending_requests: int = 1024,
        max_correlation_entries: int = 65_536,
        dynamic_tool_timeout: float = 5.0,
        max_dynamic_tool_output_chars: int = 8_192,
        dynamic_tool_audit_path: str | os.PathLike[str] | None = None,
        rate_limit_observer: Callable[[JsonObject, bool], None] | None = None,
    ) -> None:
        if not command:
            raise ValueError("App Server command must not be empty")
        if stderr_limit_bytes <= 0:
            raise ValueError("stderr_limit_bytes must be positive")
        if event_queue_size <= 0:
            raise ValueError("event_queue_size must be positive")
        if max_protocol_line_bytes <= 0:
            raise ValueError("max_protocol_line_bytes must be positive")
        if max_protocol_bytes_total <= 0:
            raise ValueError("max_protocol_bytes_total must be positive")
        if min(max_pending_requests, max_correlation_entries) <= 0:
            raise ValueError("App Server resource limits must be positive")
        if dynamic_tool_timeout <= 0 or max_dynamic_tool_output_chars <= 0:
            raise ValueError("dynamic tool limits must be positive")

        self.command = tuple(os.fspath(part) for part in command)
        self.cwd = None if cwd is None else Path(cwd)
        self.env = None if env is None else dict(env)
        self.protocol_pin = protocol_pin
        self.timeouts = timeouts
        self.client_info = dict(
            client_info
            or {
                "name": "derivationlab",
                "title": "DerivationLab",
                "version": "0.1.0",
            }
        )
        for field in ("name", "title", "version"):
            if not isinstance(self.client_info.get(field), str):
                raise ValueError(  # noqa: TRY004
                    f"client_info requires string field {field!r}"
                )
        self.capabilities = dict(
            {"experimentalApi": True} if capabilities is None else capabilities
        )
        if self.capabilities.get("experimentalApi") is not True:
            raise ValueError(
                "pinned v2 protocol requires capabilities.experimentalApi=true"
            )
        self.stderr_limit_bytes = stderr_limit_bytes
        self.max_protocol_line_bytes = max_protocol_line_bytes
        self.max_protocol_bytes_total = max_protocol_bytes_total
        self.max_pending_requests = max_pending_requests
        self.max_correlation_entries = max_correlation_entries
        self.dynamic_tool_timeout = dynamic_tool_timeout
        self.max_dynamic_tool_output_chars = max_dynamic_tool_output_chars
        self.dynamic_tool_audit_path = (
            None
            if dynamic_tool_audit_path is None
            else Path(dynamic_tool_audit_path).resolve()
        )
        self.rate_limit_observer = rate_limit_observer
        if self.dynamic_tool_audit_path is not None:
            parent = self.dynamic_tool_audit_path.parent
            if not parent.is_dir():
                raise ValueError("dynamic tool audit parent must already exist")
            if self.dynamic_tool_audit_path.exists() and (
                self.dynamic_tool_audit_path.is_symlink()
                or not self.dynamic_tool_audit_path.is_file()
            ):
                raise ValueError("dynamic tool audit path must be a regular file")

        self._state = _ClientState.NEW
        self._process: asyncio.subprocess.Process | None = None
        self._process_identity: SpawnedProcessIdentity | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._responded_ids: set[int] = set()
        self._server_request_ids: set[int | str] = set()
        self._dynamic_call_ids: set[str] = set()
        self._dynamic_tools: dict[str, ClientDynamicTool] = {}
        self._thread_dynamic_tools: dict[str, frozenset[str]] = {}
        self._thread_tool_implementations: dict[str, dict[str, ClientDynamicTool]] = {}
        self._thread_dynamic_tool_audit_paths: dict[str, Path | None] = {}
        self._dynamic_tool_tasks: set[asyncio.Task[None]] = set()
        self._request_id = 0
        self._event_sequence = 0
        self._events: asyncio.Queue[ProviderEvent] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self._fatal_error: AppServerError | None = None
        self._fatal_event = asyncio.Event()
        self._stderr_tail = bytearray()
        self._protocol_bytes_read = 0
        self._turn_status: dict[str, str] = {}
        self._turn_snapshots: dict[str, JsonObject] = {}
        self._turn_thread_ids: dict[str, set[str]] = {}
        self._thread_turn_ids: dict[str, list[str]] = {}
        self._thread_session_ids: dict[str, str] = {}
        self._thread_parent_ids: dict[str, str] = {}
        self._server_version: str | None = None
        self._started_thread_ids: set[str] = set()
        self._known_thread_ids: set[str] = set()
        self._started_turn_ids: set[str] = set()
        self._login_owner_token = object()
        self._started_login_ids: set[str] = set()
        self._login_completions: dict[str, AccountLoginCompletion] = {}
        self._login_events: dict[str, asyncio.Event] = {}

    @property
    def server_version(self) -> str | None:
        return self._server_version

    @property
    def stderr_tail(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", errors="replace")

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def process_identity(self) -> SpawnedProcessIdentity | None:
        return self._process_identity

    @property
    def is_running(self) -> bool:
        return self._state == _ClientState.RUNNING and self._fatal_error is None

    def configure_dynamic_tools(self, tools: Sequence[ClientDynamicTool]) -> None:
        """Install the complete client-side tool set before any thread starts."""

        if self._started_thread_ids or self._known_thread_ids:
            raise AppServerStateError(
                "dynamic tools must be configured before the first thread"
            )
        configured = self._validated_dynamic_tools(tools)
        if self._dynamic_tools:
            if set(self._dynamic_tools) != set(configured) or any(
                self._dynamic_tools[name] is not configured[name] for name in configured
            ):
                raise AppServerStateError(
                    "dynamic tool set cannot change after configuration"
                )
            return
        self._dynamic_tools = configured

    @staticmethod
    def _validated_dynamic_tools(
        tools: Sequence[ClientDynamicTool],
    ) -> dict[str, ClientDynamicTool]:
        configured: dict[str, ClientDynamicTool] = {}
        for tool in tools:
            name = getattr(tool, "name", None)
            spec = getattr(tool, "spec", None)
            if (
                not isinstance(name, str)
                or not name
                or name in configured
                or not isinstance(spec, Mapping)
            ):
                raise AppServerStateError("dynamic tool declaration is invalid")
            expected = {
                "type": "function",
                "name": name,
                "description": spec.get("description"),
                "inputSchema": spec.get("inputSchema"),
            }
            if dict(spec) != expected:
                raise AppServerStateError(
                    f"dynamic tool {name!r} declaration differs from the pinned shape"
                )
            try:
                json.dumps(expected, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise AppServerStateError(
                    f"dynamic tool {name!r} declaration is not strict JSON"
                ) from exc
            configured[name] = tool
        return configured

    @staticmethod
    def _validated_dynamic_tool_audit_path(
        path: str | os.PathLike[str] | None,
    ) -> Path | None:
        if path is None:
            return None
        resolved = Path(path).resolve()
        if not resolved.parent.is_dir():
            raise AppServerStateError("dynamic tool audit parent must already exist")
        if resolved.exists() and (resolved.is_symlink() or not resolved.is_file()):
            raise AppServerStateError("dynamic tool audit path must be a regular file")
        return resolved

    def dynamic_tool_specs(self, names: Sequence[str]) -> list[JsonObject]:
        if len(set(names)) != len(names):
            raise AppServerStateError("dynamic tool selection contains duplicates")
        specs: list[JsonObject] = []
        for name in names:
            tool = self._dynamic_tools.get(name)
            if tool is None:
                raise AppServerStateError(
                    f"dynamic tool {name!r} is not configured on this client"
                )
            specs.append(copy.deepcopy(dict(tool.spec)))
        return specs

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._state != _ClientState.NEW:
            raise AppServerStateError(
                f"Cannot start App Server client in state {self._state.value!r}"
            )
        self._state = _ClientState.STARTING
        try:
            await asyncio.wait_for(self._start_inner(), self.timeouts.startup)
        except TimeoutError as exc:
            error = AppServerTimeoutError("startup", self.timeouts.startup)
            self._fail(error)
            await self.close()
            raise error from exc
        except BaseException:
            await self.close()
            raise

    async def _start_inner(self) -> None:
        executable = self._canonical_executable()
        try:
            executable_state = os.stat(executable)
        except OSError as exc:
            raise AppServerStartupError(
                f"Could not inspect App Server executable: {exc}"
            ) from exc
        spawn_started = time.monotonic_ns()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=None if self.cwd is None else os.fspath(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.max_protocol_line_bytes + 1,
            )
        except OSError as exc:
            raise AppServerStartupError(
                f"Could not start App Server command {self.command!r}: {exc}"
            ) from exc
        assert self._process is not None
        self._process_identity = SpawnedProcessIdentity(
            canonical_executable=executable,
            executable_st_dev=executable_state.st_dev,
            executable_st_ino=executable_state.st_ino,
            pid=self._process.pid,
            spawn_started_monotonic_ns=spawn_started,
            spawn_completed_monotonic_ns=time.monotonic_ns(),
            identity_source="resolved_argv0_stat",
        )

        await self._initialize_spawned_process()

    async def _initialize_spawned_process(self) -> None:
        """Start protocol readers after the child process exists."""

        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="app-server-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="app-server-stderr"
        )
        self._wait_task = asyncio.create_task(
            self._watch_process(), name="app-server-process"
        )

        initialize_params: JsonObject = {"clientInfo": dict(self.client_info)}
        if self.capabilities:
            initialize_params["capabilities"] = dict(self.capabilities)
        result = await self._request_inner("initialize", initialize_params)
        result_object = self._require_result_object("initialize", result)
        self._server_version = self.protocol_pin.assert_initialize_response(
            result_object,
            expected_client_name=self.client_info["name"],
        )
        await self._send_message({"method": "initialized", "params": {}})
        self._raise_if_failed()
        self._state = _ClientState.RUNNING

    def _canonical_executable(self) -> str:
        executable = self.command[0]
        if not os.path.isabs(executable):
            search_path = None if self.env is None else self.env.get("PATH")
            resolved = shutil.which(executable, path=search_path)
            if resolved is None:
                raise AppServerStartupError(
                    f"App Server executable {executable!r} was not found"
                )
            executable = resolved
        try:
            return os.fspath(Path(executable).resolve(strict=True))
        except OSError as exc:
            raise AppServerStartupError(
                f"Could not resolve App Server executable {executable!r}: {exc}"
            ) from exc

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        self._require_running()
        request_timeout = self.timeouts.request if timeout is None else timeout
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        try:
            return await asyncio.wait_for(
                self._request_inner(method, params or {}), request_timeout
            )
        except TimeoutError as exc:
            error = AppServerTimeoutError(method, request_timeout)
            raise error from exc

    async def model_list(self, *, page_limit: int = 100) -> tuple[JsonObject, ...]:
        """Return the complete validated App Server ``model/list`` catalog."""

        if page_limit < 1 or page_limit > 1000:
            raise ValueError("model/list page_limit must be between 1 and 1000")
        rows: list[JsonObject] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(100):
            params: JsonObject = {"limit": page_limit}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._require_result_object(
                "model/list", await self.request("model/list", params)
            )
            if set(result) != {"data", "nextCursor"}:
                raise MalformedProtocolMessage(
                    "model/list result differs from the pinned paginated shape"
                )
            data = result.get("data")
            if not isinstance(data, list) or any(
                not isinstance(row, dict) for row in data
            ):
                raise MalformedProtocolMessage(
                    "model/list data must be a list of objects"
                )
            rows.extend(data)
            if len(rows) > 10_000:
                raise AppServerProtocolError("model/list row limit exceeded")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tuple(rows)
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MalformedProtocolMessage(
                    "model/list nextCursor must be a non-empty string or null"
                )
            if next_cursor in seen_cursors:
                raise AppServerProtocolError("model/list repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise AppServerProtocolError("model/list page limit exceeded")

    async def _request_inner(self, method: str, params: Mapping[str, Any]) -> Any:
        self._raise_if_failed()
        if len(self._pending) >= self.max_pending_requests:
            error = AppServerProtocolError("pending request limit exceeded")
            self._fail(error)
            raise error
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(method=method, future=future)
        try:
            await self._send_message(
                {"method": method, "id": request_id, "params": dict(params)}
            )
            return await future
        except BaseException:
            pending = self._pending.pop(request_id, None)
            if pending is not None and not pending.future.done():
                pending.future.cancel()
            raise

    async def _send_message(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerStateError("App Server stdin is not available")
        try:
            encoded = (
                json.dumps(
                    dict(message),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AppServerStateError(f"Request is not valid JSON: {exc}") from exc
        async with self._write_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                error = AppServerEOFError(
                    "App Server stdin closed while sending a protocol message"
                )
                self._fail(error)
                raise error from exc

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise MalformedProtocolMessage(
                        f"protocol line exceeded {self.max_protocol_line_bytes} bytes"
                    ) from exc
                if not line:
                    if self._state not in {_ClientState.CLOSING, _ClientState.CLOSED}:
                        await asyncio.sleep(0)
                        if self._fatal_error is None:
                            self._fail(AppServerEOFError("App Server stdout closed"))
                    return
                if len(line) > self.max_protocol_line_bytes:
                    raise MalformedProtocolMessage(
                        f"protocol line exceeded {self.max_protocol_line_bytes} bytes"
                    )
                self._protocol_bytes_read += len(line)
                if self._protocol_bytes_read > self.max_protocol_bytes_total:
                    raise EventBufferOverflowError(
                        "cumulative provider stdout byte budget exceeded"
                    )
                if not line.endswith(b"\n"):
                    raise MalformedProtocolMessage("JSONL record is missing newline")
                try:
                    text = line.decode("utf-8", errors="strict").rstrip("\r\n")
                except UnicodeDecodeError as exc:
                    raise MalformedProtocolMessage("stdout is not UTF-8") from exc
                if not text.strip():
                    raise MalformedProtocolMessage("blank stdout line")

                def object_pairs(
                    pairs: list[tuple[str, Any]],
                ) -> JsonObject:
                    result: JsonObject = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError(f"duplicate JSON field {key!r}")
                        result[key] = value
                    return result

                try:
                    message = json.loads(
                        text,
                        object_pairs_hook=object_pairs,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f"non-finite JSON number {value}")
                        ),
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise MalformedProtocolMessage(
                        f"invalid strict JSON frame: {exc}", text[:240]
                    ) from exc
                if not isinstance(message, dict):
                    raise MalformedProtocolMessage(
                        "top-level JSON value must be an object", text[:240]
                    )
                self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            self._fail(exc)
        except BaseException as exc:  # noqa: BLE001 - fatal async task boundary
            self._fail(AppServerProtocolError(f"stdout reader failed: {exc}"))

    def _dispatch_message(self, message: JsonObject) -> None:
        has_id = "id" in message
        has_method = "method" in message
        if has_id and has_method:
            method = message.get("method")
            if not isinstance(method, str):
                raise MalformedProtocolMessage("server request method must be a string")
            self._dispatch_server_request(message, method)
            return
        if has_id:
            self._dispatch_response(message)
            return
        if has_method:
            self._event_sequence += 1
            event = normalize_notification(message, self._event_sequence)
            self._observe_event(event)
            try:
                self._events.put_nowait(event)
            except asyncio.QueueFull as exc:
                raise EventBufferOverflowError(
                    "Provider event queue is full; refusing to drop notifications"
                ) from exc
            return
        raise MalformedProtocolMessage(
            "message must be a response, notification, or server request"
        )

    def _dispatch_server_request(self, message: JsonObject, method: str) -> None:
        request_id = message.get("id")
        if method != "item/tool/call" or (
            not self._dynamic_tools and not self._thread_tool_implementations
        ):
            raise UnexpectedServerRequestError(method, request_id)
        if set(message) != {"id", "method", "params"}:
            raise MalformedProtocolMessage(
                "item/tool/call request differs from the pinned envelope"
            )
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise MalformedProtocolMessage(
                "item/tool/call request id must be an integer or string"
            )
        if isinstance(request_id, int) and not -(2**63) <= request_id < 2**63:
            raise MalformedProtocolMessage(
                "item/tool/call integer request id exceeds int64"
            )
        if isinstance(request_id, str) and (not request_id or len(request_id) > 128):
            raise MalformedProtocolMessage(
                "item/tool/call string request id is empty or too long"
            )
        if request_id in self._server_request_ids:
            raise AppServerProtocolError(f"duplicate server request id {request_id!r}")
        params = message.get("params")
        if not isinstance(params, dict):
            raise MalformedProtocolMessage("item/tool/call params must be an object")
        required = {"arguments", "callId", "threadId", "tool", "turnId"}
        param_keys = set(params)
        if param_keys != required and param_keys != required | {"namespace"}:
            raise MalformedProtocolMessage(
                "item/tool/call params differ from the pinned shape"
            )
        namespace = params.get("namespace")
        if namespace is not None:
            raise AppServerProtocolError("namespaced dynamic tools are not enabled")
        identifiers: dict[str, str] = {}
        for field in ("callId", "threadId", "tool", "turnId"):
            value = params.get(field)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise MalformedProtocolMessage(
                    f"item/tool/call {field} must be bounded non-empty text"
                )
            identifiers[field] = value
        call_id = identifiers["callId"]
        thread_id = identifiers["threadId"]
        tool_name = identifiers["tool"]
        turn_id = identifiers["turnId"]
        if call_id in self._dynamic_call_ids:
            raise AppServerProtocolError(f"duplicate dynamic tool call id {call_id!r}")
        if tool_name not in self._thread_dynamic_tools.get(thread_id, frozenset()):
            raise AppServerProtocolError(
                "dynamic tool call requested a tool not selected for its thread"
            )
        thread_tools = self._thread_tool_implementations.get(
            thread_id, self._dynamic_tools
        )
        if tool_name not in thread_tools:
            raise AppServerProtocolError(
                "dynamic tool call requested an unconfigured tool"
            )
        if self._turn_status.get(
            turn_id
        ) != "inProgress" or thread_id not in self._turn_thread_ids.get(turn_id, set()):
            raise AppServerProtocolError(
                "dynamic tool call does not belong to an active turn"
            )
        self._add_correlation(
            self._server_request_ids, request_id, "server request ids"
        )
        self._add_correlation(self._dynamic_call_ids, call_id, "dynamic call ids")
        task = asyncio.create_task(
            self._answer_dynamic_tool_request(
                request_id=request_id,
                call_id=call_id,
                thread_id=thread_id,
                turn_id=turn_id,
                tool_name=tool_name,
                arguments=params.get("arguments"),
            ),
            name=f"app-server-tool-{call_id}",
        )
        self._dynamic_tool_tasks.add(task)
        task.add_done_callback(self._dynamic_tool_tasks.discard)

    async def _answer_dynamic_tool_request(
        self,
        *,
        request_id: int | str,
        call_id: str,
        thread_id: str,
        turn_id: str,
        tool_name: str,
        arguments: object,
    ) -> None:
        try:
            tool = self._thread_tool_implementations.get(
                thread_id, self._dynamic_tools
            )[tool_name]
            try:
                raw_result = await asyncio.wait_for(
                    tool.invoke(copy.deepcopy(arguments)),
                    timeout=self.dynamic_tool_timeout,
                )
            except TimeoutError:
                raw_result = {
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": '{"error":"dynamic_tool_timeout"}',
                        }
                    ],
                    "success": False,
                }
            result = self._validate_dynamic_tool_result(raw_result)
            if (
                self._turn_status.get(turn_id) != "inProgress"
                or thread_id not in self._turn_thread_ids.get(turn_id, set())
                or tool_name
                not in self._thread_dynamic_tools.get(thread_id, frozenset())
                or call_id not in self._dynamic_call_ids
            ):
                raise AppServerProtocolError(
                    "dynamic tool result became late or lost correlation"
                )
            self._append_dynamic_tool_audit(
                request_id=request_id,
                call_id=call_id,
                thread_id=thread_id,
                turn_id=turn_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            )
            # Durable evidence is part of the tool-call contract: never let the
            # provider consume a result that the run cannot later account for.
            await self._send_message({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            self._fail(exc)
        except BaseException as exc:  # noqa: BLE001 - fatal task boundary
            self._fail(AppServerProtocolError(f"dynamic tool response failed: {exc}"))

    def _append_dynamic_tool_audit(
        self,
        *,
        request_id: int | str,
        call_id: str,
        thread_id: str,
        turn_id: str,
        tool_name: str,
        arguments: object,
        result: JsonObject,
    ) -> None:
        path = self._thread_dynamic_tool_audit_paths.get(
            thread_id, self.dynamic_tool_audit_path
        )
        if path is None:
            return
        record = {
            "schema_version": "derivation-dynamic-tool-call-v1",
            "request_id": request_id,
            "call_id": call_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "tool": tool_name,
            "arguments": copy.deepcopy(arguments),
            "result": copy.deepcopy(result),
        }
        encoded = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("dynamic tool audit append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_dynamic_tool_result(self, result: Mapping[str, Any]) -> JsonObject:
        if not isinstance(result, Mapping) or set(result) != {
            "contentItems",
            "success",
        }:
            raise AppServerProtocolError(
                "dynamic tool result differs from the pinned response shape"
            )
        success = result.get("success")
        content_items = result.get("contentItems")
        if not isinstance(success, bool) or not isinstance(content_items, list):
            raise AppServerProtocolError("dynamic tool result has invalid field types")
        if not 1 <= len(content_items) <= 4:
            raise AppServerProtocolError(
                "dynamic tool result must contain one to four content items"
            )
        normalized_items: list[JsonObject] = []
        total_chars = 0
        for item in content_items:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"type", "text"}
                or item.get("type") != "inputText"
                or not isinstance(item.get("text"), str)
            ):
                raise AppServerProtocolError(
                    "dynamic tool output must contain inputText items only"
                )
            text = item["text"]
            total_chars += len(text)
            if total_chars > self.max_dynamic_tool_output_chars:
                raise AppServerProtocolError("dynamic tool output exceeds its cap")
            normalized_items.append({"type": "inputText", "text": text})
        return {"contentItems": normalized_items, "success": success}

    def _dispatch_response(self, message: JsonObject) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise UnexpectedResponseError(request_id)
        if request_id in self._responded_ids:
            raise DuplicateResponseError(request_id)
        pending = self._pending.get(request_id)
        if pending is None:
            raise UnexpectedResponseError(request_id)

        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            raise MalformedProtocolMessage(
                "response must contain exactly one of result or error"
            )
        if has_error:
            error = message.get("error")
            if not isinstance(error, dict):
                raise MalformedProtocolMessage("response error must be an object")
            code = error.get("code")
            if code is not None and (
                isinstance(code, bool) or not isinstance(code, int)
            ):
                raise MalformedProtocolMessage("response error code must be an integer")
            remote_message = error.get("message")
            if not isinstance(remote_message, str):
                raise MalformedProtocolMessage(
                    "response error message must be a string"
                )
            self._add_correlation(self._responded_ids, request_id, "response ids")
            self._pending.pop(request_id)
            pending.future.set_exception(
                AppServerRemoteError(
                    method=pending.method,
                    request_id=request_id,
                    code=code,
                    message=remote_message,
                    data=error.get("data"),
                )
            )
            return
        self._add_correlation(self._responded_ids, request_id, "response ids")
        self._pending.pop(request_id)
        result = message.get("result")
        if pending.method == "account/rateLimits/read":
            try:
                validated = validate_account_rate_limits_response(result)
            except AppServerProtocolError:
                # The awaiting public method reports malformed quota data. Do
                # not publish it to the informational cache in the meantime.
                pass
            else:
                snapshot = validated["rateLimits"]
                assert isinstance(snapshot, dict)
                if self.rate_limit_observer is not None:
                    try:
                        # Publish before resolving the future so a later
                        # notification on the JSONL stream cannot be followed
                        # by this older full snapshot in the observer.
                        self.rate_limit_observer(copy.deepcopy(snapshot), False)
                    except Exception:
                        # A display-only observer cannot invalidate the
                        # provider connection.
                        logger.exception("account rate limit observer failed")
        pending.future.set_result(result)

    def _add_correlation(self, collection: set[Any], value: Any, label: str) -> None:
        if value not in collection and len(collection) >= self.max_correlation_entries:
            error = AppServerProtocolError(f"{label} limit exceeded")
            self._fail(error)
            raise error
        collection.add(value)

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            excess = len(self._stderr_tail) - self.stderr_limit_bytes
            if excess > 0:
                del self._stderr_tail[:excess]

    async def _watch_process(self) -> None:
        process = self._process
        assert process is not None
        returncode = await process.wait()
        if self._state not in {_ClientState.CLOSING, _ClientState.CLOSED}:
            await asyncio.sleep(0)
            if self._fatal_error is None:
                self._fail(AppServerProcessError(returncode, self.stderr_tail))

    def _fail(self, error: AppServerError) -> None:
        if self._fatal_error is not None:
            return
        self._fatal_error = error
        if self._state not in {_ClientState.CLOSING, _ClientState.CLOSED}:
            self._state = _ClientState.FAILED
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
        self._fatal_event.set()
        for event in self._login_events.values():
            event.set()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tuple(self._dynamic_tool_tasks):
            if task is not current and not task.done():
                task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    def _correlation_checkpoint(self) -> dict[str, Any]:
        """Copy every map a thread observation may touch, so it can be undone."""

        return {
            "turn_status": dict(self._turn_status),
            "turn_snapshots": copy.deepcopy(self._turn_snapshots),
            "turn_thread_ids": {
                turn_id: set(threads)
                for turn_id, threads in self._turn_thread_ids.items()
            },
            "thread_turn_ids": {
                thread_id: list(turns)
                for thread_id, turns in self._thread_turn_ids.items()
            },
            "thread_session_ids": dict(self._thread_session_ids),
            "thread_parent_ids": dict(self._thread_parent_ids),
            "known_thread_ids": set(self._known_thread_ids),
        }

    def _restore_correlation(self, checkpoint: Mapping[str, Any]) -> None:
        self._turn_status = dict(checkpoint["turn_status"])
        self._turn_snapshots = copy.deepcopy(checkpoint["turn_snapshots"])
        self._turn_thread_ids = {
            turn_id: set(threads)
            for turn_id, threads in checkpoint["turn_thread_ids"].items()
        }
        self._thread_turn_ids = {
            thread_id: list(turns)
            for thread_id, turns in checkpoint["thread_turn_ids"].items()
        }
        self._thread_session_ids = dict(checkpoint["thread_session_ids"])
        self._thread_parent_ids = dict(checkpoint["thread_parent_ids"])
        self._known_thread_ids = set(checkpoint["known_thread_ids"])

    def _reject(self, error: AppServerError, *, isolated: bool) -> None:
        """Fail the whole client, unless the breach is confined to one request.

        ``isolated`` is set only by a caller that has already proved the damage
        cannot reach another thread and that rolls back whatever it recorded.
        A malformed message is never isolated: the child is no longer speaking
        the pinned protocol, which is a statement about the connection itself.
        """

        if not isolated or isinstance(error, MalformedProtocolMessage):
            self._fail(error)
        raise error

    def _raise_if_failed(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error

    def _require_running(self) -> None:
        self._raise_if_failed()
        if self._state != _ClientState.RUNNING:
            raise AppServerStateError(
                f"App Server client is not running (state={self._state.value!r})"
            )

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent:
        if not self._events.empty():
            return self._events.get_nowait()
        self._raise_if_failed()
        if self._state in {_ClientState.CLOSING, _ClientState.CLOSED}:
            raise AppServerStateError("App Server client is closed")

        event_task = asyncio.create_task(self._events.get())
        fatal_task = asyncio.create_task(self._fatal_event.wait())
        try:
            waiter = asyncio.wait(
                {event_task, fatal_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if timeout is None:
                done, _pending = await waiter
            else:
                if timeout <= 0:
                    raise ValueError("event timeout must be positive")
                done, _pending = await asyncio.wait_for(waiter, timeout)
            if event_task in done:
                return event_task.result()
            self._raise_if_failed()
            raise AppServerStateError("App Server event stream ended")
        except TimeoutError as exc:
            raise AppServerTimeoutError("next_event", timeout or 0.0) from exc
        finally:
            for task in (event_task, fatal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(event_task, fatal_task, return_exceptions=True)

    async def wait_failed(self, *, timeout: float | None = None) -> AppServerError:
        if self._fatal_error is not None:
            return self._fatal_error
        try:
            if timeout is None:
                await self._fatal_event.wait()
            else:
                await asyncio.wait_for(self._fatal_event.wait(), timeout)
        except TimeoutError as exc:
            raise AppServerTimeoutError("wait_failed", timeout or 0.0) from exc
        assert self._fatal_error is not None
        return self._fatal_error

    async def close(self) -> None:
        async with self._close_lock:
            if self._state == _ClientState.CLOSED:
                return
            self._state = _ClientState.CLOSING
            close_error = AppServerStateError("App Server client is closing")
            for event in self._login_events.values():
                event.set()
            for pending in tuple(self._pending.values()):
                if not pending.future.done():
                    pending.future.set_exception(close_error)
            self._pending.clear()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeouts.close
            cleanup_incomplete = False

            async def wait_task(task: asyncio.Task[Any]) -> bool:
                remaining = max(0.0, deadline - loop.time())
                if remaining == 0.0:
                    return task.done()
                done, _pending = await asyncio.wait({task}, timeout=remaining)
                return bool(done)

            current = asyncio.current_task()
            process = self._process
            if process is not None:
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.close()
                    wait_closed = asyncio.create_task(process.stdin.wait_closed())
                    if not await wait_task(wait_closed):
                        wait_closed.cancel()
                        cleanup_incomplete = True
                if process.returncode is None:
                    process_wait = asyncio.create_task(process.wait())
                    if not await wait_task(process_wait):
                        with contextlib.suppress(ProcessLookupError):
                            process.terminate()
                        if not await wait_task(process_wait):
                            with contextlib.suppress(ProcessLookupError):
                                process.kill()
                            await wait_task(process_wait)
                    if not process_wait.done():
                        process_wait.cancel()
                    if process.returncode is None:
                        cleanup_incomplete = True

            tasks = [
                task
                for task in (
                    self._reader_task,
                    self._stderr_task,
                    self._wait_task,
                    *self._dynamic_tool_tasks,
                )
                if task is not None and task is not current
            ]
            if tasks:
                remaining = max(0.0, deadline - loop.time())
                _done, unfinished = await asyncio.wait(tasks, timeout=remaining)
                for task in unfinished:
                    task.cancel()
                if unfinished:
                    cleanup_incomplete = True
            if cleanup_incomplete:
                raise AppServerTimeoutError("close", self.timeouts.close)
            self._state = _ClientState.CLOSED

    def _require_result_object(self, method: str, result: Any) -> JsonObject:
        if not isinstance(result, dict):
            error = MalformedProtocolMessage(
                f"{method} result must be an object, got {type(result).__name__}"
            )
            self._fail(error)
            raise error
        return result

    @staticmethod
    def _validate_permission_fields(method: str, params: Mapping[str, Any]) -> None:
        permission_keys = {
            "permissions",
            "permissionProfile",
            "sandbox",
            "sandboxPolicy",
        }
        present = {key for key in permission_keys if params.get(key) is not None}
        if len(present) > 1:
            raise AppServerStateError(
                f"{method} must not combine permission selectors: {sorted(present)}"
            )

    def _validate_thread_result(
        self,
        method: str,
        result: JsonObject,
        *,
        expected_params: Mapping[str, Any],
        expected_reasoning_effort: str,
        require_persistent: bool,
        isolate_failures: bool = False,
    ) -> None:
        """Check one thread response against the authority that was requested.

        ``isolate_failures`` narrows the blast radius for the checks that only
        compare this response against what the client already knows about these
        threads.  The authority checks above them - permissions, sandbox,
        model, effort, instruction sources - stay process-wide whatever the
        caller asks for: a child that ignores the requested authority is not a
        problem confined to one request.
        """

        required_response_fields = {
            "activePermissionProfile",
            "approvalPolicy",
            "approvalsReviewer",
            "cwd",
            "instructionSources",
            "model",
            "modelProvider",
            "reasoningEffort",
            "runtimeWorkspaceRoots",
            "sandbox",
            "thread",
        }
        missing = sorted(required_response_fields - set(result))
        if missing:
            error = MalformedProtocolMessage(
                f"{method} result is missing authority fields: {missing}"
            )
            self._fail(error)
            raise error
        if "instructionSources" not in result:
            error = MalformedProtocolMessage(
                f"{method} result is missing instructionSources"
            )
            self._fail(error)
            raise error
        instruction_sources = result.get("instructionSources")
        if instruction_sources != []:
            error = AppServerProtocolError(
                f"{method} loaded forbidden instruction sources: "
                f"{instruction_sources!r}"
            )
            self._fail(error)
            raise error
        expected_profile = expected_params.get("permissions")
        active_profile = result.get("activePermissionProfile")
        if (
            not isinstance(active_profile, dict)
            or active_profile.get("id") != expected_profile
            or active_profile.get("extends") is not None
        ):
            error = AppServerProtocolError(
                f"{method} active permission profile drifted from the request"
            )
            self._fail(error)
            raise error
        for field in ("cwd", "model", "modelProvider", "approvalPolicy"):
            if result.get(field) != expected_params.get(field):
                error = AppServerProtocolError(
                    f"{method} effective {field} drifted from the request"
                )
                self._fail(error)
                raise error
        if "serviceTier" in expected_params and result.get(
            "serviceTier"
        ) != expected_params.get("serviceTier"):
            error = AppServerProtocolError(
                f"{method} effective serviceTier drifted from the request"
            )
            self._fail(error)
            raise error
        if (
            not isinstance(expected_reasoning_effort, str)
            or not expected_reasoning_effort
            or result.get("reasoningEffort") != expected_reasoning_effort
        ):
            error = AppServerProtocolError(
                f"{method} effective reasoningEffort drifted from the request"
            )
            self._fail(error)
            raise error
        approvals_reviewer = result.get("approvalsReviewer")
        if approvals_reviewer != expected_params.get("approvalsReviewer"):
            error = AppServerProtocolError(
                f"{method} effective approvalsReviewer drifted from the request"
            )
            self._fail(error)
            raise error
        if (
            "multiAgentMode" in result
            and result.get("multiAgentMode") != "explicitRequestOnly"
        ):
            error = AppServerProtocolError(
                f"{method} enabled an unsupported multi-agent mode"
            )
            self._fail(error)
            raise error
        expected_roots = expected_params.get("runtimeWorkspaceRoots")
        if result.get("runtimeWorkspaceRoots") != expected_roots:
            error = AppServerProtocolError(
                f"{method} effective runtime workspace roots drifted"
            )
            self._fail(error)
            raise error
        sandbox = result.get("sandbox")
        if not isinstance(sandbox, dict):
            error = MalformedProtocolMessage(
                f"{method} result is missing effective sandbox object"
            )
            self._fail(error)
            raise error
        writable_roots = sandbox.get("writableRoots")
        if (
            sandbox.get("type") != "workspaceWrite"
            or sandbox.get("networkAccess") is not False
            or not isinstance(writable_roots, list)
            or any(root not in expected_roots for root in writable_roots)
        ):
            error = AppServerProtocolError(
                f"{method} returned unsafe effective sandbox authority"
            )
            self._fail(error)
            raise error
        thread = result.get("thread")
        if not isinstance(thread, dict):
            error = MalformedProtocolMessage(
                f"{method} result is missing thread object"
            )
            self._fail(error)
            raise error
        try:
            validate_thread_snapshot(thread, method)
        except AppServerProtocolError as error:
            self._fail(error)
            raise
        expected_ephemeral = expected_params.get("ephemeral")
        if expected_ephemeral is None and require_persistent:
            expected_ephemeral = False
        if (
            isinstance(expected_ephemeral, bool)
            and thread.get("ephemeral") is not expected_ephemeral
        ):
            self._reject(
                AppServerProtocolError(
                    f"{method} changed the requested thread persistence"
                ),
                isolated=isolate_failures,
            )
        source_thread_id = expected_params.get("threadId")
        if method == "thread/resume" and thread.get("id") != source_thread_id:
            error = AppServerProtocolError(
                "thread/resume returned a different thread id"
            )
            self._fail(error)
            raise error
        if method == "thread/fork" and thread.get("id") == source_thread_id:
            self._reject(
                AppServerProtocolError("thread/fork reused the source thread id"),
                isolated=isolate_failures,
            )
        thread_id = thread["id"]
        if (
            method in {"thread/start", "thread/fork"}
            and thread_id in self._known_thread_ids
        ):
            self._reject(
                AppServerProtocolError(
                    f"{method} returned a previously observed thread id"
                ),
                isolated=isolate_failures,
            )
        if method == "thread/fork":
            if thread.get("forkedFromId") != source_thread_id:
                self._reject(
                    AppServerProtocolError(
                        "thread/fork response lacks exact forkedFromId lineage"
                    ),
                    isolated=isolate_failures,
                )
            source_turn_ids = self._thread_turn_ids.get(source_thread_id)
            anchor = expected_params.get("lastTurnId")
            if source_turn_ids is None or anchor not in source_turn_ids:
                self._reject(
                    AppServerProtocolError(
                        "thread/fork source prefix is not locally known"
                    ),
                    isolated=isolate_failures,
                )
            expected_prefix = source_turn_ids[: source_turn_ids.index(anchor) + 1]
            actual_prefix = [
                turn.get("id")
                for turn in thread.get("turns", [])
                if isinstance(turn, dict)
            ]
            if actual_prefix != expected_prefix:
                self._reject(
                    AppServerProtocolError(
                        "thread/fork response changed the source turn prefix"
                    ),
                    isolated=isolate_failures,
                )
            self._observe_thread_snapshot(
                thread,
                copied_from_thread_id=source_thread_id,
                isolate_failures=isolate_failures,
            )
        else:
            self._observe_thread_snapshot(thread)
        self._add_correlation(self._known_thread_ids, thread_id, "known thread ids")
        if method == "thread/start":
            if thread.get("turns") != []:
                error = AppServerProtocolError(
                    "thread/start returned a thread with hidden prior turns"
                )
                self._fail(error)
                raise error
            if thread_id in self._started_thread_ids:
                error = AppServerProtocolError(
                    "thread/start reused a thread id in this client lifetime"
                )
                self._fail(error)
                raise error
            self._add_correlation(
                self._started_thread_ids, thread_id, "started thread ids"
            )

    @staticmethod
    def _validate_thread_authority_request(
        method: str, params: Mapping[str, Any]
    ) -> None:
        for field in ("cwd", "model", "modelProvider", "permissions"):
            if not isinstance(params.get(field), str) or not params[field]:
                raise AppServerStateError(
                    f"{method} requires explicit non-empty {field}"
                )
        if params.get("approvalPolicy") != "never":
            raise AppServerStateError(f"{method} requires approvalPolicy='never'")
        if params.get("approvalsReviewer") != "user":
            raise AppServerStateError(f"{method} requires approvalsReviewer='user'")
        roots = params.get("runtimeWorkspaceRoots")
        if roots != [params["cwd"]]:
            raise AppServerStateError(
                f"{method} runtimeWorkspaceRoots must equal [cwd]"
            )

    def _selected_dynamic_tool_names(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        configured_tools: Mapping[str, ClientDynamicTool] | None = None,
    ) -> tuple[str, ...]:
        available = (
            self._dynamic_tools if configured_tools is None else configured_tools
        )
        raw_specs = params.get("dynamicTools", [])
        if raw_specs is None:
            raw_specs = []
        if not isinstance(raw_specs, list):
            raise AppServerStateError(f"{method} dynamicTools must be an array")
        names: list[str] = []
        for raw_spec in raw_specs:
            if not isinstance(raw_spec, Mapping):
                raise AppServerStateError(
                    f"{method} dynamic tool declaration must be an object"
                )
            name = raw_spec.get("name")
            if not isinstance(name, str) or name in names:
                raise AppServerStateError(
                    f"{method} dynamic tool names must be strings and unique"
                )
            tool = available.get(name)
            if tool is None or dict(raw_spec) != dict(tool.spec):
                raise AppServerStateError(
                    f"{method} requested an unconfigured dynamic tool"
                )
            names.append(name)
        return tuple(names)

    def _bind_thread_dynamic_tools(
        self,
        thread_id: str,
        names: Sequence[str],
        *,
        configured_tools: Mapping[str, ClientDynamicTool] | None = None,
        audit_path: str | os.PathLike[str] | None = None,
    ) -> None:
        selected = frozenset(names)
        available = (
            self._dynamic_tools if configured_tools is None else configured_tools
        )
        implementations = {name: available[name] for name in names}
        resolved_audit_path = (
            self.dynamic_tool_audit_path
            if configured_tools is None and audit_path is None
            else self._validated_dynamic_tool_audit_path(audit_path)
        )
        previous = self._thread_dynamic_tools.get(thread_id)
        if previous is not None and previous != selected:
            error = AppServerProtocolError(
                "provider thread changed its selected dynamic tool set"
            )
            self._fail(error)
            raise error
        if (
            previous is None
            and len(self._thread_dynamic_tools) >= self.max_correlation_entries
        ):
            error = AppServerProtocolError("dynamic tool thread binding limit exceeded")
            self._fail(error)
            raise error
        previous_implementations = self._thread_tool_implementations.get(thread_id)
        if previous_implementations is not None and (
            set(previous_implementations) != set(implementations)
            or any(
                previous_implementations[name] is not implementations[name]
                for name in implementations
            )
        ):
            error = AppServerProtocolError(
                "provider thread changed its dynamic tool implementations"
            )
            self._fail(error)
            raise error
        if (
            thread_id in self._thread_dynamic_tool_audit_paths
            and self._thread_dynamic_tool_audit_paths[thread_id] != resolved_audit_path
        ):
            error = AppServerProtocolError(
                "provider thread changed its dynamic tool audit path"
            )
            self._fail(error)
            raise error
        self._thread_dynamic_tools[thread_id] = selected
        self._thread_tool_implementations[thread_id] = implementations
        self._thread_dynamic_tool_audit_paths[thread_id] = resolved_audit_path

    def _observe_thread_snapshot(
        self,
        thread: Mapping[str, Any],
        *,
        copied_from_thread_id: str | None = None,
        isolate_failures: bool = False,
    ) -> None:
        try:
            validate_thread_snapshot(thread, "thread snapshot")
            thread_id = thread["id"]
            session_id = thread["sessionId"]
            forked_from_id = thread.get("forkedFromId")
            if copied_from_thread_id is None and isinstance(forked_from_id, str):
                copied_from_thread_id = forked_from_id
            previous_session_id = self._thread_session_ids.get(thread_id)
            if previous_session_id is not None and previous_session_id != session_id:
                raise AppServerProtocolError(
                    "thread snapshot changed its provider session id"
                )
            if copied_from_thread_id is not None:
                previous_parent = self._thread_parent_ids.get(thread_id)
                if (
                    previous_parent is not None
                    and previous_parent != copied_from_thread_id
                ):
                    raise AppServerProtocolError(
                        "forked thread changed its parent thread"
                    )
                ancestor = copied_from_thread_id
                seen: set[str] = set()
                while ancestor in self._thread_parent_ids:
                    if ancestor == thread_id or ancestor in seen:
                        raise AppServerProtocolError(
                            "forked thread lineage contains a cycle"
                        )
                    seen.add(ancestor)
                    ancestor = self._thread_parent_ids[ancestor]
                if ancestor == thread_id:
                    raise AppServerProtocolError(
                        "forked thread lineage contains a cycle"
                    )
            self._thread_session_ids[thread_id] = session_id
            turns = thread["turns"]
            turn_ids = [turn["id"] for turn in turns]
            previous_turn_ids = self._thread_turn_ids.get(thread_id)
            if (
                previous_turn_ids is not None
                and turn_ids[: len(previous_turn_ids)] != previous_turn_ids
            ):
                raise AppServerProtocolError(
                    "thread snapshot rewrote or truncated its turn lineage"
                )
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                self._record_turn_snapshot(
                    turn,
                    "thread snapshot",
                    thread_id=thread_id,
                    copied_from_thread_id=copied_from_thread_id,
                )
            if copied_from_thread_id is not None:
                self._thread_parent_ids[thread_id] = copied_from_thread_id
            self._thread_turn_ids[thread_id] = turn_ids
            self._validate_observed_fork_lineage(thread_id)
            for child_id, parent_id in self._thread_parent_ids.items():
                if parent_id == thread_id and child_id in self._thread_turn_ids:
                    self._validate_observed_fork_lineage(child_id)
        except AppServerProtocolError as error:
            self._reject(error, isolated=isolate_failures)

    def _validate_observed_fork_lineage(self, child_id: str) -> None:
        parent_id = self._thread_parent_ids.get(child_id)
        if parent_id is None:
            return
        parent_turns = self._thread_turn_ids.get(parent_id)
        child_turns = self._thread_turn_ids.get(child_id)
        if parent_turns is None or child_turns is None:
            return
        shared = 0
        for parent_turn, child_turn in zip(parent_turns, child_turns, strict=False):
            if parent_turn != child_turn:
                break
            shared += 1
        if shared == 0 or any(
            turn_id in parent_turns for turn_id in child_turns[shared:]
        ):
            raise AppServerProtocolError(
                "forked thread does not contain an exact parent-turn prefix"
            )

    def _record_turn_snapshot(
        self,
        turn: Mapping[str, Any],
        source: str,
        *,
        thread_id: str,
        copied_from_thread_id: str | None = None,
    ) -> None:
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or not isinstance(status, str):
            raise MalformedProtocolMessage(
                f"{source} turn requires string id and status"
            )
        if not isinstance(thread_id, str) or not thread_id:
            raise MalformedProtocolMessage(
                f"{source} turn requires a non-empty owner thread id"
            )
        previous_thread_ids = self._turn_thread_ids.get(turn_id, set())
        if (
            turn_id not in self._turn_status
            and len(self._turn_status) >= self.max_correlation_entries
        ):
            error = AppServerProtocolError("turn snapshot limit exceeded")
            self._fail(error)
            raise error
        if (
            previous_thread_ids
            and thread_id not in previous_thread_ids
            and (
                copied_from_thread_id is None
                or copied_from_thread_id not in previous_thread_ids
            )
            and not any(
                self._thread_parent_ids.get(owner) == thread_id
                for owner in previous_thread_ids
            )
        ):
            raise AppServerProtocolError(
                f"{source} reused turn {turn_id!r} for another thread"
            )
        previous_status = self._turn_status.get(turn_id)
        previous = self._turn_snapshots.get(turn_id)
        terminal_statuses = {"completed", "interrupted", "failed"}
        if previous_status in terminal_statuses:
            if previous_status == status and same_terminal_turn(previous, turn):
                self._turn_thread_ids.setdefault(turn_id, set()).add(thread_id)
                return
            raise AppServerProtocolError(f"{source} changed terminal turn {turn_id!r}")
        self._turn_status[turn_id] = status
        self._turn_snapshots[turn_id] = copy.deepcopy(dict(turn))
        self._turn_thread_ids.setdefault(turn_id, set()).add(thread_id)
        thread_turn_ids = self._thread_turn_ids.setdefault(thread_id, [])
        if turn_id not in thread_turn_ids:
            thread_turn_ids.append(turn_id)

    def _observe_event(self, event: ProviderEvent) -> None:
        if event.method == "account/rateLimits/updated":
            snapshot = event.params["rateLimits"]
            assert isinstance(snapshot, dict)
            if self.rate_limit_observer is not None:
                try:
                    self.rate_limit_observer(copy.deepcopy(snapshot), True)
                except Exception:
                    # Account usage is informational and must never poison an
                    # otherwise healthy scientific App Server connection.
                    logger.exception("account rate limit observer failed")
        if event.method == "account/login/completed":
            login_id = event.params.get("loginId")
            if login_id is None:
                if self._started_login_ids:
                    raise AppServerProtocolError(
                        "account/login/completed omitted the active device login id"
                    )
            else:
                assert isinstance(login_id, str)  # normalized by protocol boundary
                completion = validate_account_login_completion(
                    event.params, expected_login_id=login_id
                )
                if login_id in self._login_completions:
                    raise AppServerProtocolError(
                        "duplicate terminal notification for an account login"
                    )
                if len(self._login_completions) >= self.max_correlation_entries:
                    raise AppServerProtocolError(
                        "account login completion correlation limit exceeded"
                    )
                self._login_completions[login_id] = completion
                signal = self._login_events.get(login_id)
                if signal is not None:
                    signal.set()

        turn_id = event.params.get("turnId")
        turn = event.params.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
        if (
            isinstance(turn_id, str)
            and self._turn_status.get(turn_id) in {"completed", "interrupted", "failed"}
            and event.method not in {"turn/completed", "thread/tokenUsage/updated"}
        ):
            raise AppServerProtocolError(
                f"{event.method} arrived after terminal turn {turn_id!r}"
            )
        if event.method not in {"turn/started", "turn/completed"}:
            return
        assert isinstance(turn, dict)  # normalized by app_server_protocol
        thread_id = event.params.get("threadId")
        assert isinstance(thread_id, str)  # normalized by app_server_protocol
        self._record_turn_snapshot(turn, event.method, thread_id=thread_id)

    async def account_read(self, *, refresh_token: bool = False) -> JsonObject:
        result = await self.request("account/read", {"refreshToken": refresh_token})
        return self._require_result_object("account/read", result)

    async def account_rate_limits_read(self) -> JsonObject:
        return validate_account_rate_limits_response(
            await self.request("account/rateLimits/read")
        )

    async def account_login_start_chatgpt_device_code(
        self,
    ) -> ChatgptDeviceCodeLogin:
        """Start one managed ChatGPT device-code login without logging secrets."""

        result = self._require_result_object(
            "account/login/start",
            await self.request("account/login/start", {"type": "chatgptDeviceCode"}),
        )
        try:
            login = validate_chatgpt_device_code_login_start(
                result, owner_token=self._login_owner_token
            )
            if login.login_id in self._started_login_ids:
                raise AppServerProtocolError(
                    "account/login/start reused a login id in this client lifetime"
                )
            self._add_correlation(
                self._started_login_ids, login.login_id, "started account login ids"
            )
            if (
                login.login_id not in self._login_events
                and len(self._login_events) >= self.max_correlation_entries
            ):
                raise AppServerProtocolError(
                    "account login waiter correlation limit exceeded"
                )
            signal = self._login_events.setdefault(login.login_id, asyncio.Event())
            if login.login_id in self._login_completions:
                signal.set()
            return login
        except AppServerProtocolError as error:
            self._fail(error)
            raise

    def _validate_login_handle(self, login: ChatgptDeviceCodeLogin) -> str:
        if not isinstance(login, ChatgptDeviceCodeLogin):
            raise TypeError("login must be a ChatgptDeviceCodeLogin handle")
        if (
            login._owner_token is not self._login_owner_token
            or login.login_id not in self._started_login_ids
        ):
            raise AppServerStateError(
                "device-code login handle does not belong to this client"
            )
        return login.login_id

    async def wait_account_login(
        self, login: ChatgptDeviceCodeLogin, *, timeout: float
    ) -> AccountLoginCompletion:
        """Wait a finite interval for the exact login handle to terminate."""

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("account login timeout must be positive and finite")
        self._require_running()
        login_id = self._validate_login_handle(login)
        completion = self._login_completions.get(login_id)
        if completion is not None:
            return completion

        signal_task = asyncio.create_task(self._login_events[login_id].wait())
        fatal_task = asyncio.create_task(self._fatal_event.wait())
        try:
            done, _pending = await asyncio.wait_for(
                asyncio.wait(
                    {signal_task, fatal_task}, return_when=asyncio.FIRST_COMPLETED
                ),
                timeout,
            )
            if fatal_task in done:
                self._raise_if_failed()
            completion = self._login_completions.get(login_id)
            if completion is None:
                self._require_running()
                raise AppServerStateError(
                    "account login waiter ended without a terminal notification"
                )
            return completion
        except TimeoutError as exc:
            raise AppServerTimeoutError("account/login/completed", timeout) from exc
        finally:
            for task in (signal_task, fatal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(signal_task, fatal_task, return_exceptions=True)

    async def account_login_cancel(
        self, login: ChatgptDeviceCodeLogin
    ) -> Literal["canceled", "notFound"]:
        """Cancel the exact device-code login and validate the pinned status."""

        login_id = self._validate_login_handle(login)
        result = self._require_result_object(
            "account/login/cancel",
            await self.request("account/login/cancel", {"loginId": login_id}),
        )
        try:
            if set(result) != {"status"}:
                raise MalformedProtocolMessage(
                    "account/login/cancel result differs from the pinned shape"
                )
            status = result.get("status")
            if status not in {"canceled", "notFound"}:
                raise UnknownProtocolStateError(
                    "status", status, "account/login/cancel"
                )
            return status
        except AppServerProtocolError as error:
            self._fail(error)
            raise

    async def protocol_barrier(self) -> int:
        """Return the last notification sequence observed before an RPC response."""

        await self.account_read(refresh_token=False)
        self._raise_if_failed()
        return self._event_sequence

    async def thread_start(
        self,
        params: Mapping[str, Any],
        *,
        require_persistent: bool = True,
        dynamic_tools: Sequence[ClientDynamicTool] | None = None,
        dynamic_tool_audit_path: str | os.PathLike[str] | None = None,
    ) -> JsonObject:
        if require_persistent and params.get("ephemeral") is not False:
            raise AppServerStateError(
                "persistent thread/start requires explicit ephemeral=false"
            )
        self._validate_permission_fields("thread/start", params)
        self._validate_thread_authority_request("thread/start", params)
        configured_tools = (
            None
            if dynamic_tools is None
            else self._validated_dynamic_tools(dynamic_tools)
        )
        dynamic_tool_names = self._selected_dynamic_tool_names(
            "thread/start", params, configured_tools=configured_tools
        )
        result = self._require_result_object(
            "thread/start", await self.request("thread/start", params)
        )
        self._validate_thread_result(
            "thread/start",
            result,
            expected_params=params,
            expected_reasoning_effort=params.get("config", {}).get(
                "model_reasoning_effort"
            ),
            require_persistent=require_persistent,
        )
        self._bind_thread_dynamic_tools(
            result["thread"]["id"],
            dynamic_tool_names,
            configured_tools=configured_tools,
            audit_path=dynamic_tool_audit_path,
        )
        return result

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> JsonObject:
        result = self._require_result_object(
            "thread/read",
            await self.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": include_turns},
            ),
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            error = MalformedProtocolMessage(
                "thread/read result is missing thread object"
            )
            self._fail(error)
            raise error
        if thread.get("id") != thread_id:
            error = AppServerProtocolError("thread/read returned a different thread id")
            self._fail(error)
            raise error
        self._observe_thread_snapshot(thread)
        self._add_correlation(self._known_thread_ids, thread_id, "known thread ids")
        return result

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
        dynamic_tools: Sequence[ClientDynamicTool] | None = None,
        dynamic_tool_audit_path: str | os.PathLike[str] | None = None,
    ) -> JsonObject:
        params: JsonObject = {"threadId": thread_id}
        params.update(dict(overrides or {}))
        self._validate_permission_fields("thread/resume", params)
        self._validate_thread_authority_request("thread/resume", params)
        configured_tools = (
            None
            if dynamic_tools is None
            else self._validated_dynamic_tools(dynamic_tools)
        )
        dynamic_tool_names = self._selected_dynamic_tool_names(
            "thread/resume", params, configured_tools=configured_tools
        )
        result = self._require_result_object(
            "thread/resume", await self.request("thread/resume", params)
        )
        self._validate_thread_result(
            "thread/resume",
            result,
            expected_params=params,
            expected_reasoning_effort=expected_reasoning_effort,
            require_persistent=require_persistent,
        )
        self._bind_thread_dynamic_tools(
            result["thread"]["id"],
            dynamic_tool_names,
            configured_tools=configured_tools,
            audit_path=dynamic_tool_audit_path,
        )
        return result

    async def thread_fork(
        self,
        thread_id: str,
        last_turn_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
        require_completed_turn: bool = True,
        dynamic_tools: Sequence[ClientDynamicTool] | None = None,
        dynamic_tool_audit_path: str | os.PathLike[str] | None = None,
    ) -> JsonObject:
        status = self._turn_status.get(last_turn_id)
        if require_completed_turn and status != "completed":
            raise TurnNotCompletedError(last_turn_id, status)
        if require_completed_turn and thread_id not in self._turn_thread_ids.get(
            last_turn_id, set()
        ):
            raise AppServerProtocolError(
                "thread/fork lastTurnId belongs to another thread"
            )
        if require_completed_turn:
            snapshot = self._turn_snapshots.get(last_turn_id)
            if snapshot is None:
                raise TurnNotCompletedError(last_turn_id, None)
            extract_single_final_agent_message(snapshot)
        params: JsonObject = {
            "threadId": thread_id,
            "lastTurnId": last_turn_id,
        }
        params.update(dict(overrides or {}))
        if require_persistent and params.get("ephemeral") is not False:
            raise AppServerStateError(
                "persistent thread/fork requires explicit ephemeral=false"
            )
        self._validate_permission_fields("thread/fork", params)
        self._validate_thread_authority_request("thread/fork", params)
        configured_tools = (
            None
            if dynamic_tools is None
            else self._validated_dynamic_tools(dynamic_tools)
        )
        dynamic_tool_names = self._selected_dynamic_tool_names(
            "thread/fork", params, configured_tools=configured_tools
        )
        result = self._require_result_object(
            "thread/fork", await self.request("thread/fork", params)
        )
        # A fork this client refuses to adopt is one abandoned thread, not a
        # broken connection: the source thread, every other thread and every
        # in-flight request of a shared child are untouched by it.  So the
        # bookkeeping is rolled back and the rejection is raised for this call
        # alone.  Checks that say the *child* ignored the requested authority
        # keep failing the client - see ``_validate_thread_result``.
        checkpoint = self._correlation_checkpoint()
        try:
            self._validate_thread_result(
                "thread/fork",
                result,
                expected_params=params,
                expected_reasoning_effort=expected_reasoning_effort,
                require_persistent=require_persistent,
                isolate_failures=True,
            )
        except AppServerProtocolError as error:
            if self._fatal_error is not None:
                raise
            self._restore_correlation(checkpoint)
            raise AppServerForkError(str(error)) from error
        self._bind_thread_dynamic_tools(
            result["thread"]["id"],
            dynamic_tool_names,
            configured_tools=configured_tools,
            audit_path=dynamic_tool_audit_path,
        )
        return result

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        if not input_items:
            raise AppServerStateError("turn/start input must not be empty")
        params: JsonObject = {
            "threadId": thread_id,
            "input": [dict(item) for item in input_items],
        }
        params.update(dict(overrides or {}))
        self._validate_permission_fields("turn/start", params)
        # A notification may legitimately announce the newly created turn before
        # the matching response arrives, so checking the post-request registry is
        # too strict.  Snapshot the historical IDs first: anything already known
        # at this point is stale provider state and cannot be reused as a new turn.
        preexisting_turn_ids = set(self._turn_status)
        result = self._require_result_object(
            "turn/start", await self.request("turn/start", params)
        )
        turn = result.get("turn")
        if not isinstance(turn, dict):
            error = MalformedProtocolMessage("turn/start result is missing turn object")
            self._fail(error)
            raise error
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or status != "inProgress":
            error = MalformedProtocolMessage(
                "turn/start must return an inProgress turn with a string id"
            )
            self._fail(error)
            raise error
        if not turn_id or turn.get("items") != []:
            error = AppServerProtocolError(
                "turn/start returned a pre-populated or empty-id turn"
            )
            self._fail(error)
            raise error
        if turn_id in preexisting_turn_ids:
            error = AppServerProtocolError(
                "turn/start reused a turn id observed before the request"
            )
            self._fail(error)
            raise error
        if turn_id in self._started_turn_ids:
            error = AppServerProtocolError(
                "turn/start reused a turn id in this client lifetime"
            )
            self._fail(error)
            raise error
        self._add_correlation(self._started_turn_ids, turn_id, "started turn ids")
        observed_status = self._turn_status.get(turn_id)
        if observed_status in {"completed", "interrupted", "failed"}:
            if thread_id not in self._turn_thread_ids.get(turn_id, set()):
                error = AppServerProtocolError(
                    "turn/start response turn belongs to another thread"
                )
                self._fail(error)
                raise error
        else:
            self._record_turn_snapshot(turn, "turn/start response", thread_id=thread_id)
        return result

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonObject:
        owners = self._turn_thread_ids.get(turn_id)
        if owners is not None and thread_id not in owners:
            raise AppServerProtocolError(
                "turn/interrupt turnId belongs to another thread"
            )
        result = await self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        return self._require_result_object("turn/interrupt", result)

    async def command_exec(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        timeout_ms: int | None = None,
        permission_profile: str | None = None,
        sandbox_policy: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        if not command:
            raise AppServerStateError("command/exec command must not be empty")
        params: JsonObject = {
            "command": [os.fspath(part) for part in command],
            "cwd": os.fspath(cwd),
        }
        if timeout_ms is not None:
            params["timeoutMs"] = timeout_ms
        if permission_profile is not None:
            params["permissionProfile"] = permission_profile
        if sandbox_policy is not None:
            params["sandboxPolicy"] = dict(sandbox_policy)
        params.update(dict(extra or {}))
        self._validate_permission_fields("command/exec", params)
        result = self._require_result_object(
            "command/exec", await self.request("command/exec", params)
        )
        exit_code = result.get("exitCode")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            error = MalformedProtocolMessage(
                "command/exec result requires integer exitCode"
            )
            self._fail(error)
            raise error
        for field in ("stdout", "stderr"):
            if not isinstance(result.get(field), str):
                error = MalformedProtocolMessage(
                    f"command/exec result requires string {field}"
                )
                self._fail(error)
                raise error
        return result
