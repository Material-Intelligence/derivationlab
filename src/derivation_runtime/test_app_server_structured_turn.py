from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .app_server_protocol import (
    AppServerProtocolError,
    AppServerTimeoutError,
    ProviderEvent,
    ProviderEventKind,
)
from .app_server_structured_turn import (
    StructuredTurnSpec,
    run_app_server_structured_turn,
)


def terminal_event(
    text: str,
    *,
    thread_id: str = "thread-intake",
    turn_id: str = "turn-intake",
) -> ProviderEvent:
    item = {
        "type": "agentMessage",
        "id": "item-final",
        "text": text,
        "phase": "final_answer",
    }
    params = {
        "threadId": thread_id,
        "turn": {
            "id": turn_id,
            "status": "completed",
            "items": [item],
            "error": None,
        },
    }
    return ProviderEvent(
        sequence=1,
        kind=ProviderEventKind.TURN,
        method="turn/completed",
        params=params,
        emitted_at_ms=None,
        raw={"method": "turn/completed", "params": params},
    )


def unscoped_event(method: str, kind: ProviderEventKind) -> ProviderEvent:
    return ProviderEvent(
        sequence=1,
        kind=kind,
        method=method,
        params={"name": "example", "status": "disabled"},
        emitted_at_ms=None,
        raw={"method": method, "params": {}},
    )


def scoped_item_event(
    item_type: str,
    *,
    turn_id: str = "turn-intake",
) -> ProviderEvent:
    params = {
        "threadId": "thread-intake",
        "turnId": turn_id,
        "item": {"id": "item-forbidden", "type": item_type},
    }
    return ProviderEvent(
        sequence=1,
        kind=ProviderEventKind.ITEM,
        method="item/started",
        params=params,
        emitted_at_ms=None,
        raw={"method": "item/started", "params": params},
    )


class ScriptedClient:
    def __init__(self, events: list[ProviderEvent | BaseException]) -> None:
        self.events = list(events)
        self.thread_start_params: dict[str, Any] | None = None
        self.thread_resume_params: dict[str, Any] | None = None
        self.turn_start_params: dict[str, Any] | None = None
        self.interrupts: list[tuple[str, str]] = []

    async def thread_start(
        self, params: Mapping[str, Any], *, require_persistent: bool = True
    ) -> dict[str, Any]:
        self.thread_start_params = dict(params)
        return {
            "thread": {
                "id": "thread-intake",
                "ephemeral": params.get("ephemeral"),
                "turns": [],
            }
        }

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
    ) -> dict[str, Any]:
        self.thread_resume_params = {
            "thread_id": thread_id,
            "expected_reasoning_effort": expected_reasoning_effort,
            "require_persistent": require_persistent,
            **dict(overrides or {}),
        }
        return {
            "thread": {
                "id": thread_id,
                "ephemeral": False,
                "turns": [
                    {
                        "id": "turn-previous",
                        "status": "completed",
                        "items": [],
                    }
                ],
            }
        }

    async def turn_start(
        self,
        thread_id: str,
        input_items: list[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.turn_start_params = {
            "thread_id": thread_id,
            "input": [dict(item) for item in input_items],
            **dict(overrides or {}),
        }
        return {
            "turn": {
                "id": "turn-intake",
                "status": "inProgress",
                "items": [],
            }
        }

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent:
        del timeout
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupts.append((thread_id, turn_id))
        return {"turn": {"id": turn_id, "status": "interrupted"}}


def spec() -> StructuredTurnSpec:
    return StructuredTurnSpec(
        workspace="/private/intake",
        permission_profile="strict_run_workspace",
        model_provider="openai",
        model="gpt-test",
        effort="low",
        developer_instructions="Return only the requested object.",
        prompt="Advance the draft.",
        output_schema={
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
            "required": ["ready"],
            "additionalProperties": False,
        },
        timeout_seconds=0.1,
    )


class StructuredTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_free_ephemeral_turn_returns_strict_object(self) -> None:
        client = ScriptedClient([terminal_event('{"ready":false}')])

        result = await run_app_server_structured_turn(client, spec())

        self.assertEqual(result.payload, {"ready": False})
        self.assertTrue(result.created_thread)
        assert client.thread_start_params is not None
        self.assertIs(client.thread_start_params["ephemeral"], True)
        self.assertEqual(client.thread_start_params["dynamicTools"], [])
        self.assertEqual(client.thread_start_params["selectedCapabilityRoots"], [])
        self.assertEqual(
            client.thread_start_params["multiAgentMode"], "explicitRequestOnly"
        )
        assert client.turn_start_params is not None
        self.assertEqual(client.turn_start_params["outputSchema"], spec().output_schema)

    async def test_persistent_thread_is_created_then_resumed_by_id(self) -> None:
        first_client = ScriptedClient([terminal_event('{"ready":false}')])
        persistent_spec = replace(spec(), persistent_thread=True)

        first = await run_app_server_structured_turn(first_client, persistent_spec)

        self.assertEqual(first.thread_id, "thread-intake")
        self.assertTrue(first.created_thread)
        assert first_client.thread_start_params is not None
        self.assertIs(first_client.thread_start_params["ephemeral"], False)
        self.assertIsNone(first_client.thread_resume_params)

        second_client = ScriptedClient([terminal_event('{"ready":true}')])
        resumed_spec = replace(
            persistent_spec,
            resume_thread_id=first.thread_id,
        )

        second = await run_app_server_structured_turn(second_client, resumed_spec)

        self.assertEqual(second.thread_id, first.thread_id)
        self.assertFalse(second.created_thread)
        self.assertIsNone(second_client.thread_start_params)
        assert second_client.thread_resume_params is not None
        self.assertEqual(
            second_client.thread_resume_params["thread_id"], first.thread_id
        )

    async def test_fast_service_tier_maps_to_priority_on_create_and_resume(
        self,
    ) -> None:
        first_client = ScriptedClient([terminal_event('{"ready":false}')])
        persistent_spec = replace(spec(), persistent_thread=True, service_tier="fast")

        first = await run_app_server_structured_turn(first_client, persistent_spec)
        assert first_client.thread_start_params is not None
        self.assertEqual(first_client.thread_start_params["serviceTier"], "priority")

        second_client = ScriptedClient([terminal_event('{"ready":true}')])
        await run_app_server_structured_turn(
            second_client,
            replace(persistent_spec, resume_thread_id=first.thread_id),
        )
        assert second_client.thread_resume_params is not None
        self.assertEqual(
            second_client.thread_resume_params["serviceTier"],
            "priority",
        )
        self.assertEqual(second_client.thread_resume_params["dynamicTools"], [])
        self.assertEqual(
            second_client.thread_resume_params["selectedCapabilityRoots"], []
        )

    async def test_resume_ignores_only_known_terminal_turn_replay(self) -> None:
        client = ScriptedClient(
            [
                terminal_event('{"ignored":true}', turn_id="turn-previous"),
                terminal_event('{"ready":true}'),
            ]
        )

        result = await run_app_server_structured_turn(
            client,
            replace(
                spec(),
                persistent_thread=True,
                resume_thread_id="thread-intake",
            ),
        )

        self.assertEqual(result.payload, {"ready": True})

    async def test_resume_rejects_forbidden_tool_from_historical_turn(self) -> None:
        client = ScriptedClient(
            [scoped_item_event("commandExecution", turn_id="turn-previous")]
        )

        with self.assertRaisesRegex(AppServerProtocolError, "historical tool"):
            await run_app_server_structured_turn(
                client,
                replace(
                    spec(),
                    persistent_thread=True,
                    resume_thread_id="thread-intake",
                ),
            )

    async def test_process_wide_mcp_startup_status_is_not_a_tool_call(self) -> None:
        client = ScriptedClient(
            [
                unscoped_event(
                    "mcpServer/startupStatus/updated", ProviderEventKind.TOOL
                ),
                terminal_event('{"ready":false}'),
            ]
        )

        result = await run_app_server_structured_turn(client, spec())

        self.assertEqual(result.payload, {"ready": False})

    async def test_other_unscoped_tool_event_still_fails_closed(self) -> None:
        client = ScriptedClient(
            [unscoped_event("mcpServer/toolCall", ProviderEventKind.TOOL)]
        )

        with self.assertRaisesRegex(AppServerProtocolError, "unscoped model event"):
            await run_app_server_structured_turn(client, spec())

    async def test_scoped_builtin_tool_is_interrupted_on_first_item(self) -> None:
        client = ScriptedClient(
            [
                scoped_item_event("commandExecution"),
                terminal_event('{"ready":false}'),
            ]
        )

        with self.assertRaisesRegex(AppServerProtocolError, "forbidden tool"):
            await run_app_server_structured_turn(client, spec())

        self.assertEqual(client.interrupts, [("thread-intake", "turn-intake")])
        self.assertEqual(len(client.events), 1, "terminal event must remain unconsumed")

    async def test_wrong_thread_or_turn_fails_closed(self) -> None:
        for event in (
            terminal_event('{"ready":false}', thread_id="thread-other"),
            terminal_event('{"ready":false}', turn_id="turn-other"),
        ):
            with (
                self.subTest(event=event.params),
                self.assertRaisesRegex(AppServerProtocolError, "another"),
            ):
                await run_app_server_structured_turn(ScriptedClient([event]), spec())

    async def test_bad_or_duplicate_json_fails_closed(self) -> None:
        for text in ("not-json", '{"ready":false,"ready":true}'):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(AppServerProtocolError, "strict JSON"),
            ):
                await run_app_server_structured_turn(
                    ScriptedClient([terminal_event(text)]), spec()
                )

    async def test_timeout_interrupts_exact_turn(self) -> None:
        client = ScriptedClient(
            [AppServerTimeoutError("next_event", spec().timeout_seconds)]
        )

        with self.assertRaises(AppServerTimeoutError):
            await run_app_server_structured_turn(client, spec())

        self.assertEqual(client.interrupts, [("thread-intake", "turn-intake")])


if __name__ == "__main__":
    unittest.main()
