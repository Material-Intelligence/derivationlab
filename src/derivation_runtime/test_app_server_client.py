"""Hermetic contract tests for the Codex App Server stdio boundary."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from .app_server_client import (
    AppServerClient,
    ClientTimeouts,
)
from .app_server_protocol import (
    KNOWN_NOTIFICATION_METHODS,
    AppServerError,
    AppServerForkError,
    AppServerProtocolError,
    AppServerStateError,
    AppServerTimeoutError,
    DuplicateResponseError,
    FinalAgentMessageError,
    MalformedProtocolMessage,
    ProtocolIdentityError,
    ProtocolVersionError,
    ProviderEvent,
    TurnNotCompletedError,
    UnexpectedServerRequestError,
    UnknownProtocolStateError,
    extract_single_final_agent_message,
    normalize_notification,
    validate_rate_limit_snapshot,
)
from .scientific_runtime import SCIENTIFIC_TOOL_NAME, SCIENTIFIC_TOOL_SPEC

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
FAKE_SERVER = HERE / "test_app_server_fake.py"
PINNED_SCHEMA = (
    HERE / "protocol_schema" / "codex_app_server_protocol.v2.schemas.json"
)


class FakeScientificTool:
    name = SCIENTIFIC_TOOL_NAME
    spec = SCIENTIFIC_TOOL_SPEC

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.arguments: list[object] = []

    async def invoke(self, arguments: object) -> dict[str, Any]:
        self.arguments.append(arguments)
        if self.delay:
            await asyncio.sleep(self.delay)
        if not isinstance(arguments, dict) or arguments.get("operation") not in {
            "differentiate",
            "simplify",
            "expand",
            "factor",
        }:
            return {
                "contentItems": [
                    {"type": "inputText", "text": '{"error":"invalid_operation"}'}
                ],
                "success": False,
            }
        return {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": '{"operation":"differentiate","result":"3*x**2"}',
                }
            ],
            "success": True,
        }


class AppServerClientTests(unittest.IsolatedAsyncioTestCase):
    def test_rate_limit_validation_rejects_bad_numbers_but_allows_other_windows(
        self,
    ) -> None:
        valid = validate_rate_limit_snapshot(
            {
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 1_440,
                    "resetsAt": 1_788_123_456,
                }
            },
            method="account/rateLimits/read",
        )
        self.assertEqual(valid["primary"]["windowDurationMins"], 1_440)
        for window in (
            {"usedPercent": True},
            {"usedPercent": 101},
            {"usedPercent": 31, "resetsAt": -1},
        ):
            with (
                self.subTest(window=window),
                self.assertRaises(MalformedProtocolMessage),
            ):
                validate_rate_limit_snapshot(
                    {"primary": window},
                    method="account/rateLimits/read",
                )

    def thread_params(self, *, ephemeral: bool = False) -> dict[str, Any]:
        return {
            "cwd": str(REPO_ROOT),
            "model": "gpt-fixture",
            "modelProvider": "openai",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": "strict_run_workspace",
            "runtimeWorkspaceRoots": [str(REPO_ROOT)],
            "ephemeral": ephemeral,
            "config": {"model_reasoning_effort": "high"},
        }

    def resume_overrides(self) -> dict[str, Any]:
        params = self.thread_params()
        params.pop("ephemeral")
        return params

    def make_client(
        self,
        scenario: str,
        *,
        startup: float = 2.0,
        request: float = 0.3,
        close: float = 0.3,
        stderr_limit_bytes: int = 1024,
        max_protocol_bytes_total: int = 64 * 1024 * 1024,
        dynamic_tool_timeout: float = 5.0,
    ) -> AppServerClient:
        return AppServerClient(
            (sys.executable, str(FAKE_SERVER), scenario),
            cwd=REPO_ROOT,
            timeouts=ClientTimeouts(
                startup=startup,
                request=request,
                close=close,
            ),
            stderr_limit_bytes=stderr_limit_bytes,
            max_protocol_bytes_total=max_protocol_bytes_total,
            dynamic_tool_timeout=dynamic_tool_timeout,
        )

    async def start_client(self, scenario: str, **kwargs: Any) -> AppServerClient:
        client = self.make_client(scenario, **kwargs)
        self.addAsyncCleanup(client.close)
        await client.start()
        return client

    async def drain_until(
        self, client: AppServerClient, method: str
    ) -> tuple[list[ProviderEvent], ProviderEvent]:
        events: list[ProviderEvent] = []
        while True:
            event = await client.next_event(timeout=1.0)
            events.append(event)
            if event.method == method:
                return events, event

    async def test_happy_flow_preserves_events_and_completion_gate(self) -> None:
        client = await self.start_client("happy")
        self.assertEqual(client.server_version, "0.147.0")
        warning = await client.next_event(timeout=1.0)
        self.assertEqual(warning.method, "configWarning")
        self.assertEqual(warning.raw["emittedAtMs"], 1)

        models = await client.model_list(page_limit=1)
        self.assertEqual(
            [row["model"] for row in models],
            ["gpt-5.6-sol", "gpt-5.5"],
        )
        self.assertEqual(
            models[0]["supportedReasoningEfforts"][-1]["reasoningEffort"],
            "ultra",
        )

        account = await client.account_read()
        self.assertEqual(account["account"]["type"], "chatgpt")
        started = await client.thread_start(self.thread_params())
        self.assertEqual(started["instructionSources"], [])
        self.assertFalse(started["thread"]["ephemeral"])

        turn_result = await client.turn_start(
            "thread-1", [{"type": "text", "text": "derive"}]
        )
        self.assertEqual(turn_result["turn"]["status"], "inProgress")
        events, completed = await self.drain_until(client, "turn/completed")
        self.assertEqual(
            [event.method for event in events],
            [
                "turn/started",
                "item/started",
                "item/agentMessage/delta",
                "item/completed",
                "turn/completed",
            ],
        )
        final_item = extract_single_final_agent_message(completed.params["turn"])
        self.assertEqual(len(json.loads(final_item["text"])["columns"]), 5)

        snapshot = await client.thread_read("thread-1")
        self.assertEqual(snapshot["thread"]["turns"][0]["status"], "completed")
        forked = await client.thread_fork(
            "thread-1",
            "turn-1",
            overrides=self.thread_params(),
            expected_reasoning_effort="high",
        )
        self.assertEqual(forked["thread"]["id"], "thread-2")
        resumed = await client.thread_resume(
            "thread-1",
            overrides=self.resume_overrides(),
            expected_reasoning_effort="high",
        )
        self.assertEqual(resumed["instructionSources"], [])
        executed = await client.command_exec(
            ["printf", "probe"],
            cwd=REPO_ROOT,
            permission_profile="strict_run_workspace",
        )
        self.assertEqual(executed["exitCode"], 0)
        self.assertEqual(
            executed["receivedParams"]["permissionProfile"],
            "strict_run_workspace",
        )
        self.assertNotIn("permissions", executed["receivedParams"])
        self.assertEqual(await client.turn_interrupt("thread-1", "turn-1"), {})

    async def test_rate_limit_read_and_sparse_notification_reach_observer(self) -> None:
        observed: list[tuple[dict[str, Any], bool]] = []
        client = self.make_client("happy")
        client.rate_limit_observer = lambda snapshot, sparse: observed.append(
            (snapshot, sparse)
        )
        self.addAsyncCleanup(client.close)
        await client.start()

        result = await client.account_rate_limits_read()
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 31)
        _events, update = await self.drain_until(client, "account/rateLimits/updated")
        self.assertEqual(update.params["rateLimits"]["primary"]["usedPercent"], 32)
        self.assertEqual([sparse for _snapshot, sparse in observed], [False, True])
        self.assertEqual(
            [snapshot["primary"]["usedPercent"] for snapshot, _ in observed],
            [31, 32],
        )

    def test_terminal_turn_allows_late_read_only_token_usage_only(self) -> None:
        client = self.make_client("happy")
        client._record_turn_snapshot(
            {
                "id": "turn-1",
                "status": "completed",
                "items": [],
                "error": None,
            },
            "fixture",
            thread_id="thread-1",
        )
        token_usage = normalize_notification(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "last": {
                            "cachedInputTokens": 0,
                            "inputTokens": 3,
                            "outputTokens": 2,
                            "reasoningOutputTokens": 0,
                            "totalTokens": 5,
                        },
                        "total": {
                            "cachedInputTokens": 0,
                            "inputTokens": 3,
                            "outputTokens": 2,
                            "reasoningOutputTokens": 0,
                            "totalTokens": 5,
                        },
                    },
                },
            },
            1,
        )
        client._observe_event(token_usage)

        late_delta = normalize_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "delta": "late content",
                },
            },
            2,
        )
        with self.assertRaisesRegex(AppServerProtocolError, "after terminal"):
            client._observe_event(late_delta)

    async def test_out_of_order_notifications_and_responses_correlate(self) -> None:
        client = await self.start_client("out_of_order")
        await client.next_event(timeout=1.0)
        account_task = asyncio.create_task(client.account_read())
        thread_task = asyncio.create_task(client.thread_start(self.thread_params()))
        account, thread = await asyncio.gather(account_task, thread_task)
        self.assertTrue(account["requiresOpenaiAuth"])
        self.assertEqual(thread["thread"]["id"], "thread-1")
        interleaved = await client.next_event(timeout=1.0)
        self.assertEqual(interleaved.method, "warning")

    async def test_thread_read_rejects_mismatched_thread_identity(self) -> None:
        client = await self.start_client("thread_read_id_drift")
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await client.thread_read("thread-expected")

    async def test_malformed_stdout_poison_connection(self) -> None:
        client = await self.start_client("malformed")
        await client.next_event(timeout=1.0)
        with self.assertRaises(MalformedProtocolMessage):
            await client.account_read()
        self.assertIsInstance(
            await client.wait_failed(timeout=1.0), MalformedProtocolMessage
        )

    async def test_raw_frames_reject_duplicate_keys_and_nonfinite_numbers(self) -> None:
        scenarios = (
            "duplicate_raw_id",
            "duplicate_raw_method",
            "duplicate_raw_thread_id",
            "duplicate_raw_turn_id",
            "duplicate_raw_call_id",
            "duplicate_raw_arguments",
            "nonfinite_raw_usage",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                client = await self.start_client(scenario)
                await client.next_event(timeout=1.0)
                with self.assertRaises(MalformedProtocolMessage):
                    await client.account_read()
                await client.close()

    async def test_malformed_response_poison_connection_and_pending_call(self) -> None:
        client = await self.start_client("malformed_response")
        await client.next_event(timeout=1.0)
        with self.assertRaises(MalformedProtocolMessage):
            await client.account_read()
        self.assertIsInstance(
            await client.wait_failed(timeout=1.0), MalformedProtocolMessage
        )

    async def test_child_crash_fails_pending_call_and_captures_stderr(self) -> None:
        client = await self.start_client("crash")
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerError):
            await client.account_read()
        await client.wait_failed(timeout=1.0)
        for _attempt in range(20):
            if "scripted child crash" in client.stderr_tail:
                break
            await asyncio.sleep(0.01)
        self.assertIn("scripted child crash", client.stderr_tail)

    async def test_request_timeout_is_bounded(self) -> None:
        client = await self.start_client("timeout", request=0.05)
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerTimeoutError) as raised:
            await client.account_read()
        self.assertEqual(raised.exception.operation, "account/read")

    async def test_interactive_approval_request_fails_closed(self) -> None:
        client = await self.start_client("approval")
        await client.next_event(timeout=1.0)
        with self.assertRaises(UnexpectedServerRequestError):
            await client.account_read()

    async def test_dynamic_tool_request_fails_closed(self) -> None:
        client = await self.start_client("dynamic_tool")
        await client.next_event(timeout=1.0)
        with self.assertRaises(UnexpectedServerRequestError):
            await client.account_read()

    async def _start_dynamic_tool_turn(
        self,
        scenario: str,
        *,
        tool: FakeScientificTool | None = None,
        dynamic_tool_timeout: float = 0.2,
    ) -> tuple[AppServerClient, FakeScientificTool]:
        client = self.make_client(scenario, dynamic_tool_timeout=dynamic_tool_timeout)
        self.addAsyncCleanup(client.close)
        configured_tool = tool or FakeScientificTool()
        client.configure_dynamic_tools([configured_tool])
        await client.start()
        await client.next_event(timeout=1.0)
        params = self.thread_params()
        params["dynamicTools"] = client.dynamic_tool_specs([SCIENTIFIC_TOOL_NAME])
        await client.thread_start(params)
        await client.turn_start(
            "thread-1", [{"type": "text", "text": "differentiate x**3"}]
        )
        return client, configured_tool

    async def test_dynamic_scientific_tool_returns_correlated_result(self) -> None:
        runs = REPO_ROOT / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="dynamic-tool-audit-", dir=runs
        ) as root:
            audit_path = Path(root) / "dynamic_tool_calls.jsonl"
            client = self.make_client("dynamic_tool_valid")
            client.dynamic_tool_audit_path = audit_path
            self.addAsyncCleanup(client.close)
            tool = FakeScientificTool()
            client.configure_dynamic_tools([tool])
            await client.start()
            await client.next_event(timeout=1.0)
            params = self.thread_params()
            params["dynamicTools"] = client.dynamic_tool_specs([SCIENTIFIC_TOOL_NAME])
            await client.thread_start(params)
            await client.turn_start(
                "thread-1", [{"type": "text", "text": "differentiate x**3"}]
            )
            await self.drain_until(client, "turn/completed")
            account = await client.account_read()
            response = account["toolResponse"]
            self.assertEqual(response["id"], "tool-request-1")
            self.assertEqual(
                response["result"],
                {
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": ('{"operation":"differentiate","result":"3*x**2"}'),
                        }
                    ],
                    "success": True,
                },
            )
            self.assertEqual(
                tool.arguments,
                [
                    {
                        "operation": "differentiate",
                        "expression": "x**3",
                        "variable": "x",
                    }
                ],
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["schema_version"], "derivation-dynamic-tool-call-v1")
            self.assertEqual(audit["tool"], SCIENTIFIC_TOOL_NAME)
            self.assertEqual(audit["arguments"], tool.arguments[0])
            self.assertEqual(audit["result"], response["result"])

    async def test_dynamic_tool_audit_failure_prevents_result_delivery(self) -> None:
        client = self.make_client("dynamic_tool_valid")
        tool = FakeScientificTool()
        client.configure_dynamic_tools([tool])
        client._turn_status["turn-1"] = "inProgress"
        client._turn_thread_ids["turn-1"] = {"thread-1"}
        client._thread_dynamic_tools["thread-1"] = frozenset({SCIENTIFIC_TOOL_NAME})
        client._dynamic_call_ids.add("call-1")
        send = AsyncMock()
        with (
            patch.object(client, "_send_message", send),
            patch.object(
                client,
                "_append_dynamic_tool_audit",
                side_effect=OSError("simulated fsync failure"),
            ),
        ):
            await client._answer_dynamic_tool_request(
                request_id="request-1",
                call_id="call-1",
                thread_id="thread-1",
                turn_id="turn-1",
                tool_name=SCIENTIFIC_TOOL_NAME,
                arguments={
                    "operation": "differentiate",
                    "expression": "x**3",
                    "variable": "x",
                },
            )
        send.assert_not_awaited()
        self.assertIsInstance(client._fatal_error, AppServerProtocolError)
        self.assertIn("simulated fsync failure", str(client._fatal_error))

    async def test_dynamic_scientific_tool_bad_call_is_bounded_failure(self) -> None:
        client, _tool = await self._start_dynamic_tool_turn("dynamic_tool_bad")
        await self.drain_until(client, "turn/completed")
        response = (await client.account_read())["toolResponse"]
        self.assertFalse(response["result"]["success"])
        self.assertEqual(
            response["result"]["contentItems"],
            [{"type": "inputText", "text": '{"error":"invalid_operation"}'}],
        )

    async def test_dynamic_scientific_tool_timeout_is_bounded_failure(self) -> None:
        client, _tool = await self._start_dynamic_tool_turn(
            "dynamic_tool_timeout",
            tool=FakeScientificTool(delay=1.0),
            dynamic_tool_timeout=0.03,
        )
        await self.drain_until(client, "turn/completed")
        response = (await client.account_read())["toolResponse"]
        self.assertFalse(response["result"]["success"])
        self.assertEqual(
            response["result"]["contentItems"],
            [{"type": "inputText", "text": '{"error":"dynamic_tool_timeout"}'}],
        )

    async def test_dynamic_tool_duplicate_late_and_bad_correlation_fail_closed(
        self,
    ) -> None:
        for scenario in (
            "dynamic_tool_duplicate",
            "dynamic_tool_late",
            "dynamic_tool_result_late",
            "dynamic_tool_wrong_thread",
            "dynamic_tool_wrong_turn",
            "dynamic_tool_wrong_tool",
        ):
            with self.subTest(scenario=scenario):
                tool = (
                    FakeScientificTool(delay=0.05)
                    if scenario == "dynamic_tool_result_late"
                    else None
                )
                client, _tool = await self._start_dynamic_tool_turn(scenario, tool=tool)
                self.assertIsInstance(
                    await client.wait_failed(timeout=1.0),
                    AppServerProtocolError,
                )
                await client.close()

    async def test_dynamic_tool_declaration_cannot_expand_authority(self) -> None:
        client = await self.start_client("happy")
        tool = FakeScientificTool()
        client.configure_dynamic_tools([tool])
        params = self.thread_params()
        widened = dict(SCIENTIFIC_TOOL_SPEC)
        widened["description"] = "Execute arbitrary Python."
        params["dynamicTools"] = [widened]
        with self.assertRaisesRegex(AppServerStateError, "unconfigured dynamic tool"):
            await client.thread_start(params)

        params["dynamicTools"] = [
            {
                "type": "function",
                "name": "shell",
                "description": "Execute commands",
                "inputSchema": {"type": "object"},
            }
        ]
        with self.assertRaisesRegex(AppServerStateError, "unconfigured dynamic tool"):
            await client.thread_start(params)

    async def test_duplicate_response_poison_connection(self) -> None:
        client = await self.start_client("duplicate")
        await client.next_event(timeout=1.0)
        with contextlib.suppress(DuplicateResponseError):
            await client.account_read()
        self.assertIsInstance(
            await client.wait_failed(timeout=1.0), DuplicateResponseError
        )

    async def test_unknown_notification_is_forward_compatible(self) -> None:
        client = await self.start_client("unknown_notification")
        await client.next_event(timeout=1.0)
        account = await client.account_read()
        self.assertEqual(account, {"account": None, "requiresOpenaiAuth": True})
        event = await client.next_event(timeout=1.0)
        self.assertEqual(event.method, "future/required")
        await client.close()

    async def test_unknown_item_state_still_fails(self) -> None:
        client = await self.start_client("unknown_item")
        await client.next_event(timeout=1.0)
        with self.assertRaises(UnknownProtocolStateError):
            await client.account_read()
        await client.close()

        snapshot_client = await self.start_client("unknown_snapshot_item")
        await snapshot_client.next_event(timeout=1.0)
        with self.assertRaises(UnknownProtocolStateError):
            await snapshot_client.thread_read("thread-1")
        await snapshot_client.close()

    async def test_clean_close_is_idempotent(self) -> None:
        client = await self.start_client("clean")
        await client.next_event(timeout=1.0)
        await client.close()
        await client.close()
        self.assertEqual(client.returncode, 0)

    async def test_correlation_memory_limit_fails_closed(self) -> None:
        client = AppServerClient(
            (sys.executable, str(FAKE_SERVER), "happy"),
            cwd=REPO_ROOT,
            timeouts=ClientTimeouts(startup=1.0, request=1.0, close=1.0),
            max_correlation_entries=1,
        )
        self.addAsyncCleanup(client.close)
        await client.start()
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await client.account_read()

    async def test_cumulative_stdout_byte_budget_fails_closed(self) -> None:
        client = await self.start_client("byte_flood", max_protocol_bytes_total=1024)
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await client.account_read()

    async def test_spawned_process_identity_is_portable_and_immutable(self) -> None:
        client = await self.start_client("clean")
        identity = client.process_identity
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(
            identity.canonical_executable, str(Path(sys.executable).resolve())
        )
        self.assertEqual(identity.identity_source, "resolved_argv0_stat")
        self.assertGreater(identity.pid, 0)
        with self.assertRaises(FrozenInstanceError):
            identity.pid = 9  # type: ignore[misc]

    async def test_startup_timeout_and_protocol_pin_are_bounded(self) -> None:
        timeout_client = self.make_client("startup_timeout", startup=0.05)
        self.addAsyncCleanup(timeout_client.close)
        with self.assertRaises(AppServerTimeoutError):
            await timeout_client.start()

        version_client = self.make_client("bad_version")
        self.addAsyncCleanup(version_client.close)
        with self.assertRaises(ProtocolVersionError):
            await version_client.start()

        identity_client = self.make_client("bad_identity")
        self.addAsyncCleanup(identity_client.close)
        with self.assertRaises(ProtocolIdentityError):
            await identity_client.start()

    async def test_thread_result_guards_instruction_sources_and_persistence(
        self,
    ) -> None:
        for scenario, error_type in (
            ("missing_instruction_sources", MalformedProtocolMessage),
            ("forbidden_instruction_sources", AppServerProtocolError),
            ("ephemeral_thread", AppServerProtocolError),
        ):
            with self.subTest(scenario=scenario):
                client = await self.start_client(scenario)
                await client.next_event(timeout=1.0)
                with self.assertRaises(error_type):
                    await client.thread_start(self.thread_params())
                await client.close()

    async def test_thread_start_validates_effective_authority_and_ephemeral(
        self,
    ) -> None:
        for scenario, error_type, ephemeral in (
            ("missing_authority", MalformedProtocolMessage, False),
            ("profile_drift", AppServerProtocolError, False),
            ("ephemeral_drift", AppServerProtocolError, True),
            ("effort_drift", AppServerProtocolError, False),
        ):
            with self.subTest(scenario=scenario):
                client = await self.start_client(scenario)
                await client.next_event(timeout=1.0)
                with self.assertRaises(error_type):
                    await client.thread_start(
                        self.thread_params(ephemeral=ephemeral),
                        require_persistent=not ephemeral,
                    )
                await client.close()

        prepopulated = await self.start_client("prepopulated_thread")
        await prepopulated.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await prepopulated.thread_start(self.thread_params())

        reused = await self.start_client("happy")
        await reused.next_event(timeout=1.0)
        await reused.thread_start(self.thread_params())
        with self.assertRaises(AppServerProtocolError):
            await reused.thread_start(self.thread_params())

    async def test_turn_start_rejects_hidden_items_and_reused_ids(self) -> None:
        prepopulated = await self.start_client("prepopulated_turn")
        await prepopulated.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await prepopulated.turn_start(
                "thread-1", [{"type": "text", "text": "derive"}]
            )

        reused = await self.start_client("happy")
        await reused.next_event(timeout=1.0)
        await reused.thread_start(self.thread_params())
        await reused.turn_start("thread-1", [{"type": "text", "text": "first"}])
        await self.drain_until(reused, "turn/completed")
        with self.assertRaises(AppServerProtocolError):
            await reused.turn_start("thread-1", [{"type": "text", "text": "second"}])

        historical = await self.start_client("happy")
        await historical.next_event(timeout=1.0)
        snapshot = await historical.thread_read("thread-1")
        self.assertEqual(snapshot["thread"]["turns"][0]["id"], "turn-1")
        with self.assertRaises(AppServerProtocolError):
            await historical.turn_start(
                "thread-1", [{"type": "text", "text": "reuse history"}]
            )

    async def test_divergent_duplicate_terminal_poison_connection(self) -> None:
        client = await self.start_client("divergent_terminal")
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await client.account_read()

    async def test_post_terminal_mutation_and_server_request_fail_closed(self) -> None:
        mutable = await self.start_client("late_mutable")
        await mutable.next_event(timeout=1.0)
        with self.assertRaises(AppServerProtocolError):
            await mutable.account_read()

        control = await self.start_client("late_control")
        await control.next_event(timeout=1.0)
        with self.assertRaises(UnexpectedServerRequestError):
            await control.account_read()

    async def test_fork_anchor_must_belong_to_source_thread(self) -> None:
        client = await self.start_client("happy")
        await client.next_event(timeout=1.0)
        await client.thread_start(self.thread_params())
        await client.turn_start("thread-1", [{"type": "text", "text": "derive"}])
        await self.drain_until(client, "turn/completed")
        overrides = self.thread_params()
        with self.assertRaises(AppServerProtocolError):
            await client.thread_fork(
                "thread-other",
                "turn-1",
                overrides=overrides,
                expected_reasoning_effort="high",
            )

    async def test_thread_resume_validates_cwd_and_workspace_roots(self) -> None:
        for scenario in ("cwd_drift", "roots_drift"):
            with self.subTest(scenario=scenario):
                client = await self.start_client(scenario)
                await client.next_event(timeout=1.0)
                with self.assertRaises(AppServerProtocolError):
                    await client.thread_resume(
                        "thread-1",
                        overrides=self.resume_overrides(),
                        expected_reasoning_effort="high",
                    )
                await client.close()

    async def test_thread_start_validates_effective_service_tier(self) -> None:
        accepted = await self.start_client("happy")
        await accepted.next_event(timeout=1.0)
        params = self.thread_params()
        params["serviceTier"] = "priority"
        result = await accepted.thread_start(params)
        self.assertEqual(result["serviceTier"], "priority")

        drifted = await self.start_client("service_tier_drift")
        await drifted.next_event(timeout=1.0)
        with self.assertRaisesRegex(AppServerProtocolError, "serviceTier drifted"):
            await drifted.thread_start(params)

    async def test_thread_fork_validates_effective_sandbox(self) -> None:
        client = await self.start_client("sandbox_drift")
        await client.next_event(timeout=1.0)
        await client.thread_read("thread-1")
        with self.assertRaises(AppServerProtocolError):
            await client.thread_fork(
                "thread-1",
                "turn-1",
                overrides=self.thread_params(),
                expected_reasoning_effort="high",
            )

    async def test_permission_selectors_are_mutually_exclusive(self) -> None:
        client = await self.start_client("happy")
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerStateError):
            await client.thread_start(
                {
                    **self.thread_params(),
                    "sandbox": "read-only",
                }
            )
        with self.assertRaises(AppServerStateError):
            await client.command_exec(
                ["true"],
                cwd=REPO_ROOT,
                permission_profile="strict",
                sandbox_policy={"type": "readOnly"},
            )

    async def test_fork_requires_observed_completed_turn(self) -> None:
        client = await self.start_client("happy")
        await client.next_event(timeout=1.0)
        with self.assertRaises(AppServerStateError):
            await client.thread_start({"cwd": str(REPO_ROOT)})
        with self.assertRaises(TurnNotCompletedError):
            await client.thread_fork(
                "thread-1", "turn-never-seen", expected_reasoning_effort="high"
            )
        await client.thread_read("thread-1")
        with self.assertRaises(AppServerStateError):
            await client.thread_fork(
                "thread-1", "turn-1", expected_reasoning_effort="high"
            )

    async def _completed_summary_turn(self, scenario: str) -> AppServerClient:
        client = await self.start_client(scenario)
        await client.next_event(timeout=1.0)
        await client.thread_start(self.thread_params())
        await client.turn_start("thread-1", [{"type": "text", "text": "derive"}])
        await self.drain_until(client, "turn/completed")
        return client

    async def test_a_wider_view_of_a_sealed_turn_is_the_same_turn(self) -> None:
        # The first live failure of a fork probe: the turn stored
        # from turn/completed is the summary view, and the first thread/read or
        # thread/fork afterwards returns the full view of that same turn.
        client = await self._completed_summary_turn("fork_view_expansion")
        stored = client._turn_snapshots["turn-1"]
        self.assertEqual(stored["itemsView"], "summary")

        snapshot = await client.thread_read("thread-1")
        self.assertEqual(snapshot["thread"]["turns"][0]["itemsView"], "full")
        forked = await client.thread_fork(
            "thread-1",
            "turn-1",
            overrides=self.thread_params(),
            expected_reasoning_effort="high",
        )
        self.assertEqual(forked["thread"]["id"], "thread-2")
        self.assertTrue(client.is_running)
        # The first sealed snapshot is what the client keeps: a wider rendering
        # is accepted, never adopted.
        self.assertEqual(client._turn_snapshots["turn-1"], stored)

    async def test_a_changed_answer_in_the_same_view_is_still_rejected(self) -> None:
        from .app_server_client import same_terminal_turn

        stored = {
            "id": "turn-1",
            "status": "completed",
            "itemsView": "summary",
            "startedAt": 1,
            "completedAt": 2,
            "items": [
                {
                    "type": "agentMessage",
                    "id": "msg_1",
                    "text": "one",
                    "phase": "final_answer",
                }
            ],
        }
        same_view = json.loads(json.dumps(stored))
        same_view["items"][0]["text"] = "two"
        self.assertFalse(same_terminal_turn(stored, same_view))

        wider = json.loads(json.dumps(stored))
        wider["itemsView"] = "full"
        wider["items"][0]["id"] = "item-1"
        self.assertTrue(same_terminal_turn(stored, wider))

        # A wider view may add items, never change the answer or the envelope.
        rewritten = json.loads(json.dumps(wider))
        rewritten["items"][0]["text"] = "two"
        self.assertFalse(same_terminal_turn(stored, rewritten))
        dropped = json.loads(json.dumps(wider))
        dropped["items"] = []
        self.assertFalse(same_terminal_turn(stored, dropped))
        retimed = json.loads(json.dumps(wider))
        retimed["completedAt"] = 3
        self.assertFalse(same_terminal_turn(stored, retimed))

    async def test_a_rejected_fork_does_not_poison_the_shared_client(self) -> None:
        client = await self._completed_summary_turn("fork_content_drift")
        before = json.loads(json.dumps(client._turn_snapshots["turn-1"]))
        known_threads = set(client._known_thread_ids)

        with self.assertRaises(AppServerForkError) as caught:
            await client.thread_fork(
                "thread-1",
                "turn-1",
                overrides=self.thread_params(),
                expected_reasoning_effort="high",
            )
        self.assertIn("changed terminal turn", str(caught.exception))
        # Nothing the fork touched survives, and the child keeps serving.
        self.assertTrue(client.is_running)
        self.assertIsNone(client._fatal_error)
        self.assertEqual(client._turn_snapshots["turn-1"], before)
        self.assertEqual(client._known_thread_ids, known_threads)
        self.assertNotIn("thread-2", client._thread_parent_ids)
        self.assertEqual(client._thread_turn_ids["thread-1"], ["turn-1"])
        # Another run borrowing the same client is still served.
        account = await client.account_read()
        self.assertEqual(account["account"]["type"], "chatgpt")

    async def test_a_fork_that_breaks_authority_still_fails_the_client(self) -> None:
        # Isolation is for what the fork response says about these threads. A
        # child that ignores the requested sandbox authority is a statement
        # about the process, and stays fatal.
        client = await self.start_client("sandbox_drift")
        await client.next_event(timeout=1.0)
        await client.thread_read("thread-1")
        with self.assertRaises(AppServerProtocolError) as caught:
            await client.thread_fork(
                "thread-1",
                "turn-1",
                overrides=self.thread_params(),
                expected_reasoning_effort="high",
            )
        self.assertNotIsInstance(caught.exception, AppServerForkError)
        self.assertIsNotNone(client._fatal_error)

    async def test_stderr_capture_is_bounded_tail(self) -> None:
        client = await self.start_client("stderr_bound", stderr_limit_bytes=64)
        await client.next_event(timeout=1.0)
        await client.account_read()
        for _attempt in range(20):
            if "TAIL_MARKER" in client.stderr_tail:
                break
            await asyncio.sleep(0.01)
        self.assertLessEqual(len(client.stderr_tail.encode("utf-8")), 64)
        self.assertTrue(client.stderr_tail.endswith("TAIL_MARKER"))


class FinalAgentMessageTests(unittest.TestCase):
    def final_item(self, item_id: str = "final-1") -> dict[str, Any]:
        return {
            "type": "agentMessage",
            "id": item_id,
            "text": "{}",
            "phase": "final_answer",
        }

    def test_requires_completed_status_and_exactly_one_final_item(self) -> None:
        with self.assertRaises(FinalAgentMessageError):
            extract_single_final_agent_message(
                {"status": "failed", "items": [self.final_item()]}
            )
        with self.assertRaises(FinalAgentMessageError):
            extract_single_final_agent_message(
                {
                    "status": "completed",
                    "items": [self.final_item("a"), self.final_item("b")],
                }
            )
        with self.assertRaises(FinalAgentMessageError):
            extract_single_final_agent_message(
                {
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": "a",
                            "text": "{}",
                            "phase": "commentary",
                        }
                    ],
                }
            )


class ProtocolSchemaPinTests(unittest.TestCase):
    @staticmethod
    def _lineage_thread(
        thread_id: str,
        turn_ids: tuple[str, ...],
        *,
        parent: str | None = None,
        session_id: str = "session-tree",
    ) -> dict[str, Any]:
        thread: dict[str, Any] = {
            "id": thread_id,
            "sessionId": session_id,
            "ephemeral": False,
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": f"item-{turn_id}",
                            "text": "{}",
                            "phase": "final_answer",
                        }
                    ],
                }
                for turn_id in turn_ids
            ],
        }
        if parent is not None:
            thread["forkedFromId"] = parent
        return thread

    def test_thread_read_lineage_accepts_both_orders_and_rejects_forgery(self) -> None:
        source = self._lineage_thread("source", ("turn-1", "turn-2"))
        child = self._lineage_thread(
            "child",
            ("turn-1",),
            parent="source",
            session_id="fork-session",
        )
        for order in ((source, child), (child, source)):
            client = AppServerClient(("/fixture/codex",))
            for thread in order:
                client._observe_thread_snapshot(thread)

        forged = self._lineage_thread("child", ("turn-2",), parent="source")
        client = AppServerClient(("/fixture/codex",))
        client._observe_thread_snapshot(source)
        with self.assertRaises(AppServerProtocolError):
            client._observe_thread_snapshot(forged)

        cyclic_a = self._lineage_thread("a", ("turn-1",), parent="b")
        cyclic_b = self._lineage_thread("b", ("turn-1",), parent="a")
        client = AppServerClient(("/fixture/codex",))
        client._observe_thread_snapshot(cyclic_a)
        with self.assertRaises(AppServerProtocolError):
            client._observe_thread_snapshot(cyclic_b)

    def test_remote_control_notification_requires_disabled_unbound_state(self) -> None:
        valid = {
            "installationId": "install-1",
            "serverName": "server-1",
            "status": "disabled",
            "environmentId": None,
        }
        event = normalize_notification(
            {"method": "remoteControl/status/changed", "params": valid}, 1
        )
        self.assertEqual(event.params, valid)
        for params in (
            {**valid, "status": "connected"},
            {**valid, "environmentId": "environment-1"},
            {**valid, "installationId": ""},
            {key: value for key, value in valid.items() if key != "serverName"},
        ):
            with self.subTest(params=params), self.assertRaises(AppServerProtocolError):
                normalize_notification(
                    {
                        "method": "remoteControl/status/changed",
                        "params": params,
                    },
                    2,
                )

    def test_supported_notification_fields_have_pinned_value_types(self) -> None:
        malformed = (
            (
                "item/reasoning/summaryTextDelta",
                {
                    "delta": "x",
                    "itemId": "item-1",
                    "summaryIndex": "bad",
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                },
            ),
            (
                "process/outputDelta",
                {
                    "capReached": "false",
                    "deltaBase64": 3,
                    "processHandle": "process-1",
                    "stream": "other",
                },
            ),
            (
                "process/exited",
                {
                    "exitCode": "0",
                    "processHandle": "process-1",
                    "stderr": "",
                    "stderrCapReached": False,
                    "stdout": "",
                    "stdoutCapReached": False,
                },
            ),
            ("warning", {"message": 123}),
            ("configWarning", {"summary": 123}),
        )
        for method, params in malformed:
            with (
                self.subTest(method=method),
                self.assertRaises(MalformedProtocolMessage),
            ):
                normalize_notification({"method": method, "params": params}, 1)

    def test_mcp_startup_status_notification_uses_pinned_schema(self) -> None:
        valid = {
            "name": "fixture-server",
            "status": "ready",
            "threadId": "thread-1",
            "error": None,
            "failureReason": None,
        }
        event = normalize_notification(
            {"method": "mcpServer/startupStatus/updated", "params": valid},
            1,
        )
        self.assertEqual(event.params, valid)

    def test_model_rerouted_requires_exact_pinned_identity_shape(self) -> None:
        valid = {
            "fromModel": "gpt-requested",
            "toModel": "gpt-fallback",
            "reason": "highRiskCyberActivity",
            "threadId": "thread-1",
            "turnId": "turn-1",
        }
        event = normalize_notification({"method": "model/rerouted", "params": valid}, 1)
        self.assertEqual(event.params, valid)

        invalid_shapes = (
            {key: value for key, value in valid.items() if key != "turnId"},
            {**valid, "threadId": ""},
            {**valid, "unexpected": True},
        )
        for params in invalid_shapes:
            with (
                self.subTest(params=params),
                self.assertRaises(MalformedProtocolMessage),
            ):
                normalize_notification(
                    {"method": "model/rerouted", "params": params}, 2
                )
        with self.assertRaises(UnknownProtocolStateError):
            normalize_notification(
                {
                    "method": "model/rerouted",
                    "params": {**valid, "reason": "futureReason"},
                },
                3,
            )

    def test_terminal_notification_requires_consistent_identity(self) -> None:
        terminal = {
            "id": "turn-1",
            "status": "completed",
            "items": [],
            "error": None,
        }
        with self.assertRaises(MalformedProtocolMessage):
            normalize_notification(
                {"method": "turn/completed", "params": {"turn": terminal}},
                1,
            )
        with self.assertRaises(AppServerProtocolError):
            normalize_notification(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-other",
                        "turn": terminal,
                    },
                },
                2,
            )

    def test_notification_allowlist_and_command_permission_keys_match_schema(
        self,
    ) -> None:
        schema = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
        definitions = schema["definitions"]
        notification_methods = {
            variant["properties"]["method"]["enum"][0]
            for variant in definitions["ServerNotification"]["oneOf"]
        }
        self.assertEqual(notification_methods, KNOWN_NOTIFICATION_METHODS)
        command_fields = set(definitions["CommandExecParams"]["properties"])
        self.assertIn("permissionProfile", command_fields)
        self.assertIn("sandboxPolicy", command_fields)
        self.assertNotIn("permissions", command_fields)


if __name__ == "__main__":
    unittest.main()
