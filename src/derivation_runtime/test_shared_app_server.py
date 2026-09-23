"""Deterministic concurrency tests for the shared App Server broker."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from .app_server_protocol import (
    AppServerProtocolError,
    ProtocolPin,
    ProviderEvent,
    ProviderEventKind,
)
from .shared_app_server import SharedAppServerBroker


class _Tool:
    name = "source_read"
    spec: ClassVar[dict[str, Any]] = {
        "type": "function",
        "name": name,
        "description": "Read one isolated fixture source.",
        "inputSchema": {"type": "object", "properties": {}},
    }

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls: list[object] = []

    async def invoke(self, arguments: object) -> Mapping[str, Any]:
        self.calls.append(arguments)
        return {
            "contentItems": [{"type": "inputText", "text": self.marker}],
            "success": True,
        }


class _InterleavedClient:
    """One fake destructive event queue with scoped tool bindings."""

    def __init__(self) -> None:
        self.is_running = True
        self.server_version = "0.147.0"
        self.protocol_pin = ProtocolPin()
        self.command = ("/fixture/codex", "app-server", "--stdio")
        self.cwd = Path("/")
        self.env = {"PATH": "/fixture"}
        self.process_identity = None
        self.returncode: int | None = None
        self.close_count = 0
        self._events: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self._sequence = 0
        self._thread_counter = 0
        self._turn_counter = 0
        self.thread_workspaces: dict[str, str] = {}
        self.thread_tools: dict[str, dict[str, _Tool]] = {}
        self.thread_audits: dict[str, Path | None] = {}
        self.thread_turns: dict[str, list[dict[str, Any]]] = {}

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent:
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    async def emit(
        self,
        method: str,
        params: Mapping[str, Any],
        kind: ProviderEventKind,
    ) -> None:
        self._sequence += 1
        await self._events.put(
            ProviderEvent(
                sequence=self._sequence,
                kind=kind,
                method=method,
                params=dict(params),
                emitted_at_ms=None,
                raw={"method": method, "params": dict(params)},
            )
        )

    async def protocol_barrier(self) -> int:
        await asyncio.sleep(0)
        return self._sequence

    async def thread_start(
        self,
        params: Mapping[str, Any],
        *,
        require_persistent: bool = True,
        dynamic_tools: Sequence[_Tool] | None = None,
        dynamic_tool_audit_path: str | Path | None = None,
    ) -> dict[str, Any]:
        del require_persistent
        self._thread_counter += 1
        thread_id = f"thread-{self._thread_counter}"
        self.thread_workspaces[thread_id] = str(params["cwd"])
        self.thread_tools[thread_id] = {tool.name: tool for tool in dynamic_tools or ()}
        self.thread_audits[thread_id] = (
            None if dynamic_tool_audit_path is None else Path(dynamic_tool_audit_path)
        )
        self.thread_turns[thread_id] = []
        await self.emit(
            "thread/status/changed",
            {"threadId": thread_id, "status": {"type": "idle"}},
            ProviderEventKind.THREAD,
        )
        await asyncio.sleep(0)
        return {
            "thread": {
                "id": thread_id,
                "sessionId": thread_id,
                "ephemeral": params.get("ephemeral", False),
                "turns": [],
            }
        }

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_items, overrides
        self._turn_counter += 1
        turn = {"id": f"turn-{self._turn_counter}", "status": "inProgress", "items": []}
        self.thread_turns[thread_id].append(turn)
        await self.emit(
            "turn/started",
            {"threadId": thread_id, "turn": dict(turn)},
            ProviderEventKind.TURN,
        )
        await asyncio.sleep(0)
        return {"turn": dict(turn)}

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
        dynamic_tools: Sequence[_Tool] | None = None,
        dynamic_tool_audit_path: str | Path | None = None,
    ) -> dict[str, Any]:
        del expected_reasoning_effort, require_persistent
        assert overrides is not None
        self.thread_workspaces[thread_id] = str(overrides["cwd"])
        self.thread_tools[thread_id] = {tool.name: tool for tool in dynamic_tools or ()}
        self.thread_audits[thread_id] = (
            None if dynamic_tool_audit_path is None else Path(dynamic_tool_audit_path)
        )
        self.thread_turns.setdefault(thread_id, [])
        await self.emit(
            "thread/status/changed",
            {"threadId": thread_id, "status": {"type": "idle"}},
            ProviderEventKind.THREAD,
        )
        await asyncio.sleep(0)
        return {
            "thread": {
                "id": thread_id,
                "sessionId": thread_id,
                "ephemeral": False,
                "turns": list(self.thread_turns[thread_id]),
            }
        }

    async def invoke_tool(
        self, thread_id: str, tool_name: str, arguments: object
    ) -> Mapping[str, Any]:
        tool = self.thread_tools[thread_id].get(tool_name)
        if tool is None:
            raise AppServerProtocolError("tool is not selected for this thread")
        result = await tool.invoke(arguments)
        path = self.thread_audits[thread_id]
        if path is not None:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {"thread_id": thread_id, "tool": tool_name, "result": result},
                        sort_keys=True,
                    )
                    + "\n"
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
        dynamic_tools: Sequence[_Tool] | None = None,
        dynamic_tool_audit_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Copy the anchor's history into a child thread, ids and all.

        This is what the real App Server does, verified live: the child
        thread's snapshot repeats the parent's turns under their *original*
        ids.  A fake that renumbered them would hide the collision this file
        exists to pin.
        """

        del expected_reasoning_effort, require_persistent, require_completed_turn
        history = list(self.thread_turns[thread_id])
        cut = next(
            (index for index, turn in enumerate(history) if turn["id"] == last_turn_id),
            len(history) - 1,
        )
        self._thread_counter += 1
        child = f"thread-{self._thread_counter}"
        self.thread_workspaces[child] = str((overrides or {}).get("cwd", ""))
        self.thread_tools[child] = {tool.name: tool for tool in dynamic_tools or ()}
        self.thread_audits[child] = (
            None if dynamic_tool_audit_path is None else Path(dynamic_tool_audit_path)
        )
        self.thread_turns[child] = [dict(turn) for turn in history[: cut + 1]]
        return {
            "thread": {
                "id": child,
                "sessionId": child,
                "ephemeral": False,
                "forkedFromId": thread_id,
                "turns": list(self.thread_turns[child]),
            }
        }

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        return {
            "thread": {
                "id": thread_id,
                "turns": list(self.thread_turns[thread_id]) if include_turns else [],
            }
        }

    async def close(self) -> None:
        self.close_count += 1
        self.is_running = False
        self.returncode = 0


def _thread_params(workspace: Path) -> dict[str, Any]:
    return {
        "cwd": str(workspace),
        "runtimeWorkspaceRoots": [str(workspace)],
        "ephemeral": False,
        "dynamicTools": [dict(_Tool.spec)],
    }


class SharedAppServerBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_forked_thread_repeats_the_anchor_turn_without_a_second_claim(
        self,
    ) -> None:
        """A fork copies turn ids; the broker must not read that as a collision.

        A live fork probe died here after the two anchor-comparison fixes
        already landed: ``thread/fork`` succeeded, the child thread existed, and
        the broker then refused the child's copied history with "shared broker
        turn was claimed twice" - an ``AppServerProtocolError``, so it also
        poisoned the shared child for every other run borrowing it.  A turn
        belongs to one session; it may appear on several threads of that
        session, and after a fork it always does.
        """

        with tempfile.TemporaryDirectory(prefix="shared-app-server-fork-") as root:
            workspace = Path(root).resolve()
            client = _InterleavedClient()
            broker = SharedAppServerBroker(client)  # type: ignore[arg-type]
            session = await broker.acquire_session(workspace=workspace)
            session.configure_dynamic_tools([_Tool("fork-anchor")])

            started = await session.thread_start(_thread_params(workspace))
            parent = started["thread"]["id"]
            turn = (await session.turn_start(parent, [{"type": "text", "text": "a"}]))[
                "turn"
            ]["id"]
            await client.emit(
                "turn/completed",
                {
                    "threadId": parent,
                    "turn": {"id": turn, "status": "completed", "items": []},
                },
                ProviderEventKind.TURN,
            )

            forked = await session.thread_fork(
                parent,
                turn,
                overrides={
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                },
                expected_reasoning_effort="medium",
            )
            child = forked["thread"]["id"]
            self.assertNotEqual(child, parent)
            self.assertEqual([item["id"] for item in forked["thread"]["turns"]], [turn])
            # Reading the child back repeats the copied turn a third time.
            await session.thread_read(child)

            # The anchor turn now routes from either thread, to the one session
            # that owns both, and the broker is not poisoned.
            await client.emit(
                "thread/tokenUsage/updated",
                {"threadId": child, "turnId": turn, "tokenUsage": {"total": 7}},
                ProviderEventKind.TURN,
            )
            await client.emit(
                "thread/tokenUsage/updated",
                {"threadId": parent, "turnId": turn, "tokenUsage": {"total": 8}},
                ProviderEventKind.TURN,
            )
            routed: list[tuple[str | None, str | None]] = []
            while len(routed) < 2:
                event = await session.next_event(timeout=1)
                if event.method == "thread/tokenUsage/updated":
                    routed.append(
                        (event.params.get("threadId"), event.params.get("turnId"))
                    )
            self.assertEqual(routed, [(child, turn), (parent, turn)])
            self.assertTrue(session.is_running)

            # A second session still cannot take that turn: cross-session
            # exclusivity is the invariant the collision check is really for,
            # and relaxing the same-session case must not relax this one.
            other = await broker.acquire_session(workspace=workspace)
            other_thread = (await other.thread_start(_thread_params(workspace)))[
                "thread"
            ]["id"]
            with self.assertRaises(AppServerProtocolError) as caught:
                broker._claim_turn_id(other.session_id, other_thread, turn)
            self.assertIn("claimed twice", str(caught.exception))
            await other.close()
            await session.close()

    async def test_fresh_broker_session_can_claim_a_persisted_thread_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="shared-app-server-resume-") as root:
            workspace = Path(root).resolve()
            client = _InterleavedClient()
            broker = SharedAppServerBroker(client)  # type: ignore[arg-type]
            session = await broker.acquire_session(workspace=workspace)
            session.configure_dynamic_tools([_Tool("resume-only")])

            result = await session.thread_resume(
                "persisted-thread",
                overrides={
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                },
                expected_reasoning_effort="high",
            )

            self.assertEqual(result["thread"]["id"], "persisted-thread")
            event = await session.next_event(timeout=1)
            self.assertEqual(event.params["threadId"], "persisted-thread")
            await session.thread_read("persisted-thread")
            await session.close()

    async def test_interleaved_threads_keep_events_tools_audits_and_lifetime_isolated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="shared-app-server-") as root:
            base = Path(root)
            workspace_a = base / "a"
            workspace_b = base / "b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            workspace_a = workspace_a.resolve()
            workspace_b = workspace_b.resolve()
            audit_a = workspace_a / "tools.jsonl"
            audit_b = workspace_b / "tools.jsonl"
            client = _InterleavedClient()
            broker = SharedAppServerBroker(client)  # type: ignore[arg-type]
            session_a, session_b = await asyncio.gather(
                broker.acquire_session(
                    workspace=workspace_a, dynamic_tool_audit_path=audit_a
                ),
                broker.acquire_session(
                    workspace=workspace_b, dynamic_tool_audit_path=audit_b
                ),
            )
            tool_a = _Tool("A-only")
            tool_b = _Tool("B-only")
            session_a.configure_dynamic_tools([tool_a])
            session_b.configure_dynamic_tools([tool_b])

            started_a, started_b = await asyncio.gather(
                session_a.thread_start(_thread_params(workspace_a)),
                session_b.thread_start(_thread_params(workspace_b)),
            )
            thread_a = started_a["thread"]["id"]
            thread_b = started_b["thread"]["id"]
            turn_a_result, turn_b_result = await asyncio.gather(
                session_a.turn_start(thread_a, [{"type": "text", "text": "a"}]),
                session_b.turn_start(thread_b, [{"type": "text", "text": "b"}]),
            )
            turn_a = turn_a_result["turn"]["id"]
            turn_b = turn_b_result["turn"]["id"]

            await client.emit(
                "item/agentMessage/delta",
                {"threadId": thread_b, "turnId": turn_b, "delta": "B"},
                ProviderEventKind.STREAM,
            )
            await client.emit(
                "item/agentMessage/delta",
                {"threadId": thread_a, "turnId": turn_a, "delta": "A"},
                ProviderEventKind.STREAM,
            )
            await client.emit(
                "thread/tokenUsage/updated",
                {"threadId": thread_a, "turnId": turn_a, "tokenUsage": {"total": 11}},
                ProviderEventKind.TURN,
            )
            await client.emit(
                "thread/tokenUsage/updated",
                {"threadId": thread_b, "turnId": turn_b, "tokenUsage": {"total": 22}},
                ProviderEventKind.TURN,
            )
            result_a, result_b = await asyncio.gather(
                client.invoke_tool(thread_a, "source_read", {"source": "a"}),
                client.invoke_tool(thread_b, "source_read", {"source": "b"}),
            )
            await client.emit(
                "turn/completed",
                {
                    "threadId": thread_b,
                    "turn": {"id": turn_b, "status": "completed", "items": []},
                },
                ProviderEventKind.TURN,
            )
            await client.emit(
                "turn/completed",
                {
                    "threadId": thread_a,
                    "turn": {"id": turn_a, "status": "completed", "items": []},
                },
                ProviderEventKind.TURN,
            )

            events_a = [await session_a.next_event(timeout=1) for _ in range(5)]
            events_b = [await session_b.next_event(timeout=1) for _ in range(5)]
            self.assertEqual(
                [event.params.get("threadId") for event in events_a], [thread_a] * 5
            )
            self.assertEqual(
                [event.params.get("threadId") for event in events_b], [thread_b] * 5
            )
            self.assertEqual(
                [
                    event.params.get("delta")
                    for event in events_a
                    if "delta" in event.params
                ],
                ["A"],
            )
            self.assertEqual(
                [
                    event.params.get("delta")
                    for event in events_b
                    if "delta" in event.params
                ],
                ["B"],
            )
            self.assertEqual(result_a["contentItems"][0]["text"], "A-only")
            self.assertEqual(result_b["contentItems"][0]["text"], "B-only")
            self.assertEqual(tool_a.calls, [{"source": "a"}])
            self.assertEqual(tool_b.calls, [{"source": "b"}])
            self.assertIn("A-only", audit_a.read_text())
            self.assertNotIn("B-only", audit_a.read_text())
            self.assertIn("B-only", audit_b.read_text())
            self.assertNotIn("A-only", audit_b.read_text())
            self.assertEqual(client.thread_workspaces[thread_a], str(workspace_a))
            self.assertEqual(client.thread_workspaces[thread_b], str(workspace_b))

            await session_a.close()
            self.assertEqual(client.close_count, 0)
            self.assertTrue(session_b.is_running)
            await session_b.thread_read(thread_b)
            await session_b.close()
            self.assertEqual(client.close_count, 1)

    async def test_unscoped_or_unknown_thread_event_poison_every_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shared-app-server-fatal-") as root:
            workspace = Path(root)
            client = _InterleavedClient()
            broker = SharedAppServerBroker(client)  # type: ignore[arg-type]
            session = await broker.acquire_session(workspace=workspace)
            # A threadless command-execution notice belongs to exactly one run
            # and cannot be attributed, so it must stay fatal.
            await client.emit(
                "process/exited",
                {"processHandle": "p1", "exitCode": 0},
                ProviderEventKind.PROCESS,
            )
            with self.assertRaisesRegex(AppServerProtocolError, "unscoped event"):
                await session.next_event(timeout=1)
            self.assertEqual(client.close_count, 1)

    async def test_process_scope_events_reach_no_session(self) -> None:
        """Child-process notices are kept, never delivered as a run's events."""

        with tempfile.TemporaryDirectory(prefix="shared-app-server-process-") as root:
            workspace = Path(root).resolve()
            client = _InterleavedClient()
            observed: list[str] = []
            broker = SharedAppServerBroker(
                client,  # type: ignore[arg-type]
                process_event_observer=lambda event: observed.append(event.method),
            )
            session = await broker.acquire_session(workspace=workspace)
            thread = await session.thread_start(
                {
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                }
            )
            thread_id = thread["thread"]["id"]
            await client.emit(
                "account/rateLimits/updated",
                {"rateLimits": {"primary": None}},
                ProviderEventKind.ACCOUNT,
            )
            await client.emit(
                "warning", {"message": "child notice"}, ProviderEventKind.SYSTEM
            )
            await client.emit(
                "thread/status/changed",
                {"threadId": thread_id, "status": {"type": "active"}},
                ProviderEventKind.THREAD,
            )
            methods = [(await session.next_event(timeout=1)).method for _ in range(2)]
            # thread/start's own status notice, then the second one: neither of
            # the two threadless child notices was interleaved into the session.
            self.assertEqual(
                methods, ["thread/status/changed", "thread/status/changed"]
            )
            self.assertEqual(observed, ["account/rateLimits/updated", "warning"])
            self.assertEqual(
                [item.method for item in broker.process_events],
                ["account/rateLimits/updated", "warning"],
            )
            self.assertIsNone(broker.fatal_error)
            await session.close()

    async def test_thread_started_routes_by_its_snapshot_identity(self) -> None:
        """``thread/started`` carries no threadId; its snapshot still binds it."""

        with tempfile.TemporaryDirectory(prefix="shared-app-server-started-") as root:
            workspace = Path(root).resolve()
            client = _InterleavedClient()
            broker = SharedAppServerBroker(client)  # type: ignore[arg-type]
            session = await broker.acquire_session(workspace=workspace)
            thread = await session.thread_start(
                {
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                }
            )
            thread_id = thread["thread"]["id"]
            await client.emit(
                "thread/started",
                {"thread": {"id": thread_id, "turns": []}},
                ProviderEventKind.THREAD,
            )
            methods = [(await session.next_event(timeout=1)).method for _ in range(2)]
            self.assertEqual(methods, ["thread/status/changed", "thread/started"])
            self.assertIsNone(broker.fatal_error)
            await session.close()


if __name__ == "__main__":
    unittest.main()
