"""One strict, tool-free structured turn over an authorized App Server client.

This is an internal App Server module, not a public provider gateway.  It owns
the protocol work between ``thread/start`` and one terminal structured result;
product-profile authorization and process lifetime remain with the caller.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .app_server_protocol import (
    AppServerProtocolError,
    AppServerTimeoutError,
    JsonObject,
    ProviderEvent,
    ProviderEventKind,
    extract_single_final_agent_message,
)


class _StructuredTurnClient(Protocol):
    async def thread_start(
        self, params: Mapping[str, Any], *, require_persistent: bool = True
    ) -> JsonObject: ...

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
    ) -> JsonObject: ...

    async def turn_start(
        self,
        thread_id: str,
        input_items: list[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> JsonObject: ...

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent: ...

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonObject: ...


@dataclass(frozen=True)
class StructuredTurnSpec:
    workspace: str
    permission_profile: str
    model_provider: str
    model: str
    effort: str
    developer_instructions: str
    prompt: str
    output_schema: Mapping[str, Any]
    service_tier: str = "standard"
    thread_config: Mapping[str, Any] = field(default_factory=dict)
    persistent_thread: bool = False
    resume_thread_id: str | None = None
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name in (
            "workspace",
            "permission_profile",
            "model_provider",
            "model",
            "effort",
            "service_tier",
            "developer_instructions",
            "prompt",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if not isinstance(self.output_schema, Mapping) or not self.output_schema:
            raise ValueError("output_schema must be a non-empty object")
        if self.resume_thread_id is not None:
            if (
                not isinstance(self.resume_thread_id, str)
                or not self.resume_thread_id.strip()
            ):
                raise ValueError("resume_thread_id must be non-empty text")
            if not self.persistent_thread:
                raise ValueError("only persistent structured threads can be resumed")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.service_tier not in {"standard", "fast"}:
            raise ValueError("service_tier must be standard or fast")


@dataclass(frozen=True)
class StructuredTurnResult:
    payload: JsonObject
    thread_id: str
    turn_id: str
    created_thread: bool
    usage: JsonObject = field(default_factory=dict)


_FORBIDDEN_INTAKE_ITEM_TYPES = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageView",
        "sleep",
        "imageGeneration",
    }
)

# App Server reports process-wide MCP startup state even when this thread has
# no selected capability roots and no callable tools.  It is lifecycle
# telemetry, not a model action, and therefore has no turn id.  Keep this
# allow-list exact: scoped MCP/tool items remain forbidden below.
_ALLOWED_UNSCOPED_LIFECYCLE_EVENTS = frozenset({"mcpServer/startupStatus/updated"})


def _strict_json_object(text: str) -> JsonObject:
    def object_pairs(pairs: list[tuple[str, Any]]) -> JsonObject:
        value: JsonObject = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON field {key!r}")
            value[key] = item
        return value

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise AppServerProtocolError(
            f"structured turn final message is not strict JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AppServerProtocolError("structured turn result must be a JSON object")
    return parsed


def _event_turn_id(event: ProviderEvent) -> str | None:
    turn = event.params.get("turn")
    if isinstance(turn, Mapping) and isinstance(turn.get("id"), str):
        return turn["id"]
    turn_id = event.params.get("turnId")
    return turn_id if isinstance(turn_id, str) else None


def _forbidden_item_type(event: ProviderEvent) -> str | None:
    item = event.params.get("item")
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    if isinstance(item_type, str) and item_type in _FORBIDDEN_INTAKE_ITEM_TYPES:
        return item_type
    return None


async def run_app_server_structured_turn(
    client: _StructuredTurnClient,
    spec: StructuredTurnSpec,
) -> StructuredTurnResult:
    """Run one tool-free structured turn on a fresh or resumed thread."""

    config = dict(spec.thread_config)
    config["model_reasoning_effort"] = spec.effort
    thread_params: JsonObject = {
        "cwd": spec.workspace,
        "model": spec.model,
        "modelProvider": spec.model_provider,
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "permissions": spec.permission_profile,
        "runtimeWorkspaceRoots": [spec.workspace],
        "selectedCapabilityRoots": [],
        "dynamicTools": [],
        "config": config,
        "allowProviderModelFallback": False,
        "multiAgentMode": "explicitRequestOnly",
    }
    if spec.service_tier == "fast":
        thread_params["serviceTier"] = "priority"
    created_thread = spec.resume_thread_id is None
    historical_terminal_turn_ids: set[str] = set()
    if created_thread:
        thread_params["ephemeral"] = not spec.persistent_thread
        thread_params["developerInstructions"] = spec.developer_instructions
        thread_result = await client.thread_start(
            thread_params,
            require_persistent=spec.persistent_thread,
        )
    else:
        thread_result = await client.thread_resume(
            spec.resume_thread_id,
            overrides=thread_params,
            expected_reasoning_effort=spec.effort,
            require_persistent=True,
        )
    thread = thread_result.get("thread")
    expected_ephemeral = not spec.persistent_thread
    if (
        not isinstance(thread, Mapping)
        or not isinstance(thread.get("id"), str)
        or thread.get("ephemeral") is not expected_ephemeral
    ):
        raise AppServerProtocolError(
            "structured thread start/resume returned invalid persistence state"
        )
    if created_thread and thread.get("turns") != []:
        raise AppServerProtocolError("new structured thread returned hidden turns")
    if not created_thread:
        if thread["id"] != spec.resume_thread_id:
            raise AppServerProtocolError("thread/resume returned a different thread")
        turns = thread.get("turns")
        if not isinstance(turns, list) or any(
            not isinstance(turn, Mapping)
            or not isinstance(turn.get("id"), str)
            or turn.get("status") not in {"completed", "interrupted", "failed"}
            for turn in turns
        ):
            raise AppServerProtocolError(
                "resumed structured thread contains an invalid historical turn"
            )
        historical_terminal_turn_ids = {turn["id"] for turn in turns}
    thread_id = thread["id"]
    turn_result = await client.turn_start(
        thread_id,
        [{"type": "text", "text": spec.prompt}],
        overrides={
            "model": spec.model,
            "effort": spec.effort,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": spec.permission_profile,
            "outputSchema": dict(spec.output_schema),
        },
    )
    turn = turn_result.get("turn")
    if (
        not isinstance(turn, Mapping)
        or not isinstance(turn.get("id"), str)
        or turn.get("status") != "inProgress"
        or turn.get("items") != []
    ):
        raise AppServerProtocolError("structured turn/start returned invalid state")
    turn_id = turn["id"]
    deadline = time.monotonic() + spec.timeout_seconds
    terminal: Mapping[str, Any] | None = None
    usage: JsonObject = {}
    try:
        while terminal is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeoutError(
                    "structured intake turn", spec.timeout_seconds
                )
            event = await client.next_event(timeout=remaining)
            observed_turn_id = _event_turn_id(event)
            observed_thread_id = event.params.get("threadId")
            if observed_turn_id is None:
                # Initialization/account/thread notifications cannot mutate the
                # structured result and may precede the turn response in queue.
                if event.method in _ALLOWED_UNSCOPED_LIFECYCLE_EVENTS:
                    continue
                if event.kind in {
                    ProviderEventKind.TURN,
                    ProviderEventKind.ITEM,
                    ProviderEventKind.STREAM,
                    ProviderEventKind.TOOL,
                    ProviderEventKind.PROCESS,
                }:
                    raise AppServerProtocolError(
                        f"unscoped model event during structured turn: {event.method}"
                    )
                continue
            if observed_thread_id != thread_id:
                raise AppServerProtocolError(
                    "structured turn observed an event for another thread"
                )
            if observed_turn_id in historical_terminal_turn_ids:
                forbidden_item_type = _forbidden_item_type(event)
                if forbidden_item_type is not None or event.kind in {
                    ProviderEventKind.TOOL,
                    ProviderEventKind.PROCESS,
                }:
                    raise AppServerProtocolError(
                        "resumed structured thread replayed a forbidden historical tool"
                    )
                # App Server may replay notifications for terminal turns returned
                # by thread/resume before it emits events for the newly started
                # turn. They cannot contribute to the current structured result.
                continue
            if observed_turn_id != turn_id:
                raise AppServerProtocolError(
                    "structured turn observed an event for another turn"
                )
            forbidden_item_type = _forbidden_item_type(event)
            if forbidden_item_type is not None or event.kind in {
                ProviderEventKind.TOOL,
                ProviderEventKind.PROCESS,
            }:
                try:
                    await client.turn_interrupt(thread_id, turn_id)
                except BaseException as interrupt_error:
                    raise AppServerProtocolError(
                        "intake turn attempted a forbidden tool and interrupt failed"
                    ) from interrupt_error
                label = forbidden_item_type or event.method
                raise AppServerProtocolError(
                    f"intake turn attempted to use a forbidden tool: {label}"
                )
            if event.method == "thread/tokenUsage/updated":
                usage_value = event.params.get("tokenUsage")
                if not isinstance(usage_value, dict):
                    raise AppServerProtocolError("token usage must be an object")
                usage = json.loads(json.dumps(usage_value))
            if event.method == "turn/completed":
                terminal_value = event.params.get("turn")
                if not isinstance(terminal_value, Mapping):
                    raise AppServerProtocolError(
                        "turn/completed is missing a terminal turn object"
                    )
                terminal = terminal_value
    except AppServerTimeoutError:
        try:
            await client.turn_interrupt(thread_id, turn_id)
        except BaseException as interrupt_error:
            raise AppServerProtocolError(
                "structured turn timed out and interrupt failed"
            ) from interrupt_error
        raise

    items = terminal.get("items")
    if not isinstance(items, list):
        raise AppServerProtocolError("structured terminal turn lacks items")
    for item in items:
        if (
            isinstance(item, Mapping)
            and item.get("type") in _FORBIDDEN_INTAKE_ITEM_TYPES
        ):
            raise AppServerProtocolError(
                "intake turn attempted to use a forbidden tool"
            )
    final = extract_single_final_agent_message(terminal)
    return StructuredTurnResult(
        payload=_strict_json_object(final["text"]),
        thread_id=thread_id,
        turn_id=turn_id,
        created_thread=created_thread,
        usage=usage,
    )


__all__ = [
    "StructuredTurnResult",
    "StructuredTurnSpec",
    "run_app_server_structured_turn",
]
