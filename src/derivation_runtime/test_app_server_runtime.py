"""Hermetic provider-runtime tests over a scripted App Server client."""

from __future__ import annotations

import asyncio
import copy
import json
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .app_server_client import SpawnedProcessIdentity
from .app_server_protocol import (
    AppServerProtocolError,
    AppServerRemoteError,
    ProtocolPin,
    ProviderEvent,
    normalize_notification,
)
from .app_server_runtime import CodexAppServerRuntime, LaunchSettings
from .launch_gate import AppServerCommand, GateResult, GateStatus
from .prompts import writer_output_schema, writer_user_prompt
from .scientific_runtime import SCIENTIFIC_TOOL_SPEC
from .types import (
    ArtifactRef,
    CheckRequest,
    ContentRef,
    InputPolicy,
    JudgeRequest,
    ModelRole,
    ModelSpec,
    ProviderForkError,
    ProviderLineage,
    ReconcileStatus,
    RunConfig,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeInvocationError,
    RuntimeSession,
    StepContent,
    StepSnapshot,
    WriterDecision,
    WriterRequest,
)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TurnScript:
    final_payload: Mapping[str, Any] | str | None
    status: str = "completed"
    extra_item_type: str | None = None
    reroute_to_model: str | None = None
    divergent_terminal: bool = False
    hold: bool = False
    error_message: str = "scripted provider failure"
    # Extra answer deltas streamed before the terminal event; a real structured
    # turn streams roughly one delta per token.
    extra_deltas: int = 0
    # The rendering the sealed turn is announced in.  Live ``turn/completed``
    # carries ``"summary"`` in production; a later ``thread/read`` of the same
    # turn may carry ``"full"``.
    terminal_items_view: str | None = None


class ScriptedAppServerClient:
    """Only the public methods consumed by CodexAppServerRuntime."""

    def __init__(
        self,
        scripts: Sequence[TurnScript],
        *,
        replay_forked_history: bool = False,
    ) -> None:
        self.scripts = list(scripts)
        # Opt-in because it changes what every existing fork test observes.  A
        # real App Server, forking a thread, restates the copied turns' terminal
        # events on the child under their *original* turn ids - as the child
        # rollout captured from a live App Server showed.
        self.replay_forked_history = replay_forked_history
        self.is_running = True
        self.server_version = "0.147.0"
        self.protocol_pin = ProtocolPin()
        self.command = authorized_command().argv
        self.cwd = ROOT
        self.env = dict(authorized_command().environment)
        self.process_identity = SpawnedProcessIdentity(
            canonical_executable="/fixture/codex",
            executable_st_dev=1,
            executable_st_ino=2,
            pid=3,
            spawn_started_monotonic_ns=4,
            spawn_completed_monotonic_ns=5,
            identity_source="resolved_argv0_stat",
        )
        self.thread_start_calls: list[dict[str, Any]] = []
        self.turn_start_calls: list[dict[str, Any]] = []
        self.thread_fork_calls: list[dict[str, Any]] = []
        self.thread_resume_calls: list[dict[str, Any]] = []
        self.thread_read_calls: list[dict[str, Any]] = []
        self.default_reasoning_effort = "low"
        self.force_resume_reasoning_effort: str | None = None
        self.interrupt_calls: list[dict[str, str]] = []
        self.threads: dict[str, dict[str, Any]] = {}
        self.missing_threads: set[str] = set()
        self._thread_counter = 0
        self._turn_counter = 0
        self._sequence = 0
        self._events: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self.force_start_ephemeral: bool | None = None
        self.force_start_turns: list[dict[str, Any]] | None = None
        self._emit_tasks: dict[str, asyncio.Task[None]] = {}

    async def thread_start(
        self,
        params: Mapping[str, Any],
        *,
        require_persistent: bool = True,
    ) -> dict[str, Any]:
        copied = copy.deepcopy(dict(params))
        self.thread_start_calls.append(copied)
        self._thread_counter += 1
        thread_id = f"thread-{self._thread_counter}"
        ephemeral = copied["ephemeral"]
        if require_persistent and ephemeral is not False:
            raise AssertionError("writer thread was not persistent")
        response_ephemeral = (
            ephemeral
            if self.force_start_ephemeral is None
            else self.force_start_ephemeral
        )
        thread = {
            "id": thread_id,
            "sessionId": thread_id,
            "ephemeral": response_ephemeral,
            "cwd": copied["cwd"],
            "modelProvider": copied["modelProvider"],
            "turns": copy.deepcopy(self.force_start_turns or []),
        }
        self.threads[thread_id] = thread
        return {
            "thread": copy.deepcopy(thread),
            "instructionSources": [],
            "reasoningEffort": copied["config"]["model_reasoning_effort"],
        }

    async def thread_resume(
        self,
        thread_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
    ) -> dict[str, Any]:
        self.thread_resume_calls.append(
            {"threadId": thread_id, **copy.deepcopy(dict(overrides or {}))}
        )
        thread = self.threads.setdefault(
            thread_id,
            {
                "id": thread_id,
                "sessionId": thread_id,
                "ephemeral": False,
                "cwd": str(ROOT),
                "modelProvider": "openai",
                "turns": [],
            },
        )
        return {
            "thread": copy.deepcopy(thread),
            "instructionSources": [],
            "reasoningEffort": (
                self.force_resume_reasoning_effort
                or dict(overrides or {})
                .get("config", {})
                .get("model_reasoning_effort", self.default_reasoning_effort)
            ),
        }

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.scripts:
            raise AssertionError("no scripted turn remains")
        script = self.scripts.pop(0)
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        self.turn_start_calls.append(
            {
                "threadId": thread_id,
                "input": copy.deepcopy(list(input_items)),
                **copy.deepcopy(dict(overrides or {})),
            }
        )
        turn = {"id": turn_id, "status": "inProgress", "items": []}
        self.threads[thread_id]["turns"].append(turn)
        self._emit_tasks[turn_id] = asyncio.create_task(
            self._emit_script(thread_id, turn_id, script)
        )
        return {"turn": copy.deepcopy(turn)}

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        self.thread_read_calls.append(
            {"threadId": thread_id, "includeTurns": include_turns}
        )
        if thread_id in self.missing_threads or thread_id not in self.threads:
            raise AppServerRemoteError(
                method="thread/read",
                request_id=1,
                code=-32602,
                message="thread not found",
            )
        thread = copy.deepcopy(self.threads[thread_id])
        if not include_turns:
            thread["turns"] = []
        return {"thread": thread}

    async def thread_fork(
        self,
        thread_id: str,
        last_turn_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        expected_reasoning_effort: str,
        require_persistent: bool = True,
        require_completed_turn: bool = True,
    ) -> dict[str, Any]:
        self.thread_fork_calls.append(
            {
                "threadId": thread_id,
                "lastTurnId": last_turn_id,
                **copy.deepcopy(dict(overrides or {})),
            }
        )
        self._thread_counter += 1
        child_id = f"thread-{self._thread_counter}"
        source_turns = self.threads[thread_id]["turns"]
        anchor_index = next(
            index
            for index, turn in enumerate(source_turns)
            if turn["id"] == last_turn_id
        )
        child = {
            "id": child_id,
            "sessionId": self.threads[thread_id]["sessionId"],
            "forkedFromId": thread_id,
            "ephemeral": False,
            "cwd": str(ROOT),
            "modelProvider": "openai",
            "turns": copy.deepcopy(source_turns[: anchor_index + 1]),
        }
        self.threads[child_id] = child
        if self.replay_forked_history:
            for turn in child["turns"]:
                await self._emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": child_id,
                            "turn": copy.deepcopy(turn),
                        },
                    }
                )
        return {
            "thread": copy.deepcopy(child),
            "instructionSources": [],
            "reasoningEffort": dict(overrides or {})
            .get("config", {})
            .get("model_reasoning_effort", self.default_reasoning_effort),
        }

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupt_calls.append({"threadId": thread_id, "turnId": turn_id})
        task = self._emit_tasks.get(turn_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        turn = self._turn(thread_id, turn_id)
        turn.update({"status": "interrupted", "items": []})
        await self._emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)},
            }
        )
        return {}

    async def next_event(self, *, timeout: float | None = None) -> ProviderEvent:
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    async def protocol_barrier(self) -> int:
        await asyncio.sleep(0)
        return self._sequence

    async def close(self) -> None:
        self.is_running = False
        for task in self._emit_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._emit_tasks.values(), return_exceptions=True)

    async def _emit_script(
        self, thread_id: str, turn_id: str, script: TurnScript
    ) -> None:
        await asyncio.sleep(0)
        await self._emit(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "status": "inProgress",
                        "items": [],
                    },
                },
            }
        )
        if script.reroute_to_model is not None:
            await self._emit(
                {
                    "method": "model/rerouted",
                    "params": {
                        "fromModel": "gpt-checker",
                        "toModel": script.reroute_to_model,
                        "reason": "highRiskCyberActivity",
                        "threadId": thread_id,
                        "turnId": turn_id,
                    },
                }
            )
        await self._emit(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": f"agent-{turn_id}",
                    "delta": "partial",
                },
            }
        )
        for _ in range(script.extra_deltas):
            await self._emit(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": f"agent-{turn_id}",
                        "delta": "abc",
                    },
                }
            )
        if script.hold:
            await asyncio.Event().wait()
            return

        items: list[dict[str, Any]] = []
        if script.extra_item_type is not None:
            items.append(
                {
                    "type": script.extra_item_type,
                    "id": f"extra-{turn_id}",
                }
            )

        if script.final_payload is not None:
            text = (
                script.final_payload
                if isinstance(script.final_payload, str)
                else json.dumps(
                    script.final_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            items.append(
                {
                    "type": "agentMessage",
                    "id": f"agent-{turn_id}",
                    "text": text,
                    "phase": "final_answer",
                }
            )
        turn = self._turn(thread_id, turn_id)
        turn.clear()
        turn.update(
            {
                "id": turn_id,
                "status": script.status,
                "items": items,
                "error": (
                    {"message": script.error_message}
                    if script.status == "failed"
                    else None
                ),
            }
        )
        if script.terminal_items_view is not None:
            turn["itemsView"] = script.terminal_items_view
        await self._emit(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
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
            }
        )
        await self._emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)},
            }
        )
        if script.divergent_terminal:
            changed = copy.deepcopy(turn)
            changed["items"][-1]["text"] = '{"changed":true}'
            await self._emit(
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": changed},
                }
            )

    async def _emit(self, message: Mapping[str, Any]) -> None:
        self._sequence += 1
        await self._events.put(normalize_notification(message, self._sequence))

    def _turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return next(
            item for item in self.threads[thread_id]["turns"] if item["id"] == turn_id
        )


def config() -> RunConfig:
    artifact = ArtifactRef("fixture", "a" * 64)
    return RunConfig(
        run_id="run-app-server-runtime",
        task=ContentRef("task", "b" * 64),
        pack=ContentRef("pack", "c" * 64),
        code_commit="d" * 40,
        granularity="one_claim",
        max_active_branches=2,
        max_model_calls=20,
        concurrency=1,
        retries=1,
        writer=ModelSpec("openai", "gpt-writer", "high"),
        checker=ModelSpec("openai", "gpt-checker", "medium"),
        judge=ModelSpec("openai", "gpt-judge", "xhigh"),
        backend_name="codex-app-server",
        backend_version="0.147.0",
        record_spec=artifact,
        event_schema=artifact,
        canonical_schema=artifact,
        input_policy=InputPolicy(False, ()),
        credential_profile_id="test-profile",
    )


def authorized_command() -> AppServerCommand:
    return AppServerCommand(
        argv=("/fixture/codex", "app-server", "--strict-config", "--stdio"),
        cwd=str(ROOT),
        environment={
            "HOME": "/fixture/host-home",
            "CODEX_HOME": "/fixture/codex-home",
        },
    )


def settings(**overrides: Any) -> LaunchSettings:
    turn_timeout = overrides.pop("turn_timeout", 1.0)
    return LaunchSettings(
        workspace=ROOT,
        gate_result=GateResult(
            status=GateStatus.SUPPORTED,
            stage="model_turn",
            issues=(),
        ),
        authorized_command=authorized_command(),
        turn_timeout=turn_timeout,
        **overrides,
    )


def assert_closed_thread_config(
    testcase: unittest.TestCase,
    call: Mapping[str, Any],
    *,
    expect_reasoning_effort: bool = True,
) -> None:
    expected = {
        "features": {
            "code_mode_host": False,
            "js_repl": False,
            "shell_tool": False,
            "unified_exec": False,
        },
        "project_doc_max_bytes": 0,
        "skills": {"include_instructions": False},
        "mcp_servers": {},
        "web_search": "disabled",
        "apps": {
            "_default": {
                "enabled": False,
                "open_world_enabled": False,
                "destructive_enabled": False,
            }
        },
    }
    if expect_reasoning_effort:
        expected["model_reasoning_effort"] = call["config"]["model_reasoning_effort"]
    testcase.assertEqual(call["config"], expected)


def step_payload(label: str) -> dict[str, str]:
    return {
        "claim": f"Claim {label}",
        "why": f"Why {label}",
        "source": f"Source {label}",
        "derivation": f"Derivation {label}",
        "scope": f"Scope {label}",
    }


def writer_payload(
    label: str,
    decision: str = "continue",
    alternatives: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "content": step_payload(label),
        "control": {
            "decision": decision,
            "alternatives": list(alternatives),
        },
    }


def rendered_in_full_view(turn: Mapping[str, Any]) -> dict[str, Any]:
    """The sealed turn as the App Server re-renders it on a later read.

    Observed against a live App Server: the wider
    view prepends the user message, renumbers every item id positionally and
    leaves everything that states what the turn did byte-identical.
    """

    wider = copy.deepcopy(dict(turn))
    wider["itemsView"] = "full"
    items = [
        {
            "type": "userMessage",
            "id": "placeholder",
            "clientId": None,
            "content": [
                {
                    "type": "text",
                    "text": "the writer prompt",
                    "text_elements": [],
                }
            ],
        },
        *copy.deepcopy(wider["items"]),
    ]
    for index, item in enumerate(items, start=1):
        item["id"] = f"item-{index}"
    wider["items"] = items
    return wider


def snapshot(label: str) -> StepSnapshot:
    return StepSnapshot(
        step_revision_id=f"step-{label}",
        content=StepContent(**step_payload(label)),
    )


def writer_request(
    slot: int,
    transcript: Sequence[StepSnapshot] = (),
    *,
    branch_id: str = "branch-1",
) -> WriterRequest:
    return WriterRequest(
        run_id="run-app-server-runtime",
        branch_id=branch_id,
        step_slot=slot,
        task_text="Derive the invariant.",
        hypothesis="Use the symmetry route.",
        transcript=tuple(transcript),
    )


class CodexAppServerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(
        self,
        scripts: Sequence[TurnScript],
        *,
        run_config: RunConfig | None = None,
        replay_forked_history: bool = False,
    ) -> tuple[CodexAppServerRuntime, ScriptedAppServerClient]:
        client = ScriptedAppServerClient(
            scripts, replay_forked_history=replay_forked_history
        )
        runtime = CodexAppServerRuntime._from_test_client(
            config=run_config or config(),
            client=client,
            settings=settings(),  # type: ignore[arg-type]
        )
        self.addAsyncCleanup(runtime.close)
        return runtime, client

    async def test_fast_service_tier_maps_to_app_server_priority(self) -> None:
        runtime, client = self.runtime(
            [TurnScript(writer_payload("fast", decision="complete"))],
            run_config=replace(config(), service_tier="fast"),
        )

        invocation = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(invocation)

        self.assertEqual(client.thread_start_calls[0]["serviceTier"], "priority")

    async def test_writer_uses_one_structured_output_with_scientific_tool(self) -> None:
        runtime, client = self.runtime(
            [TurnScript(writer_payload("one", decision="complete"))]
        )

        invocation = await runtime.start_writer(writer_request(1), None)
        output = await runtime.collect_writer(invocation)

        self.assertEqual(output.content.claim, "Claim one")
        self.assertEqual(output.control.decision, WriterDecision.COMPLETE)
        self.assertEqual(
            client.thread_start_calls[0]["dynamicTools"], [SCIENTIFIC_TOOL_SPEC]
        )
        self.assertEqual(
            client.turn_start_calls[0]["outputSchema"],
            writer_output_schema(writer_request(1)),
        )

    async def test_writer_request_must_agree_with_the_frozen_branch_cap(self) -> None:
        # The developer text is rendered from the run configuration and the
        # output schema from the request, so a disagreement would either offer a
        # decision the run cannot honour or withhold one it can.
        runtime, _ = self.runtime(
            [TurnScript(writer_payload("one", decision="complete"))]
        )
        with self.assertRaises(RuntimeInvariantError) as raised:
            await runtime.start_writer(
                replace(writer_request(1), fork_available=False), None
            )
        self.assertIn("fork availability", str(raised.exception))

    async def test_a_single_branch_run_is_offered_no_fork_decision(self) -> None:
        runtime, client = self.runtime(
            [TurnScript(writer_payload("one", decision="complete"))],
            run_config=replace(config(), max_active_branches=1),
        )
        request = replace(writer_request(1), fork_available=False)

        await runtime.collect_writer(await runtime.start_writer(request, None))

        instructions = client.thread_start_calls[0]["developerInstructions"]
        self.assertIn("Forking is unavailable in this run", instructions)
        self.assertNotIn(
            "fork",
            client.turn_start_calls[0]["outputSchema"]["properties"]["control"][
                "properties"
            ]["decision"]["enum"],
        )

    def test_writer_schema_forces_revision_of_unresolved_hard_defect(self) -> None:
        step = snapshot("one")
        request = replace(
            writer_request(2, (step,)),
            record_version="1.1",
            full_transcript=(step,),
            checker_feedback=(
                {
                    "verdict": "hard_defect",
                    "step_revision_id": "step-one",
                },
            ),
        )

        control = writer_output_schema(request)["properties"]["control"]["properties"]
        self.assertEqual(control["decision"]["enum"], ["revise"])
        self.assertEqual(
            control["revise_step_revision_id"],
            {"type": "string", "enum": ["step-one"]},
        )

    async def test_same_writer_session_runs_two_explicit_model_turns(self) -> None:
        runtime, client = self.runtime(
            [
                TurnScript(writer_payload("one")),
                TurnScript(writer_payload("two", decision="complete")),
            ]
        )
        first_request = writer_request(1)
        first = await runtime.start_writer(first_request, None)
        first_output = await runtime.collect_writer(first)
        second = await runtime.start_writer(
            writer_request(2, (snapshot("one"),)), first.session
        )
        second_output = await runtime.collect_writer(second)

        self.assertEqual(first.session, second.session)
        self.assertEqual(first_output.control.decision, WriterDecision.CONTINUE)
        self.assertEqual(second_output.control.decision, WriterDecision.COMPLETE)
        self.assertEqual(first_output.content.claim, "Claim one")
        self.assertEqual(len(client.thread_start_calls), 1)
        start = client.thread_start_calls[0]
        self.assertFalse(start["ephemeral"])
        self.assertIs(start["allowProviderModelFallback"], False)
        self.assertNotIn("environments", start)
        self.assertEqual(start["selectedCapabilityRoots"], [])
        self.assertEqual(start["permissions"], "strict_run_workspace")
        assert_closed_thread_config(self, start)
        self.assertEqual(start["dynamicTools"], [SCIENTIFIC_TOOL_SPEC])
        self.assertEqual(
            [call["threadId"] for call in client.turn_start_calls],
            [first.session.session_id, first.session.session_id],
        )
        self.assertEqual(
            client.turn_start_calls[0]["input"][0]["text"],
            writer_user_prompt(first_request),
        )
        for call in client.turn_start_calls:
            self.assertEqual(call["model"], "gpt-writer")
            self.assertEqual(call["effort"], "high")
            self.assertEqual(call["permissions"], "strict_run_workspace")
            self.assertNotIn("sandboxPolicy", call)
            self.assertNotIn("environments", call)

    async def test_writer_session_is_owned_by_exact_branch(self) -> None:
        runtime, _client = self.runtime([TurnScript(writer_payload("owned"))])
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)
        with self.assertRaises(RuntimeInvariantError):
            await runtime.start_writer(
                writer_request(2, branch_id="branch-2"), first.session
            )
        with self.assertRaises(RuntimeInvariantError):
            await runtime.start_writer(
                writer_request(2), RuntimeSession("unknown", ProviderLineage.NATIVE)
            )

    async def test_rotated_branch_context_moves_the_writer_binding(self) -> None:
        """A rotated Branch context moves the Writer's session binding.

        The orchestrator rotates a Branch's provider context once its exact
        transcript outgrows the bounded window: it rehydrates the bounded
        prefix into a fresh thread and advances the same Branch there.  The
        Branch was already bound to the thread its earlier steps ran on, so
        without the announcement the next turn arrived on a session the runtime
        would have no reason to accept and the run would die mid-derivation.
        """

        runtime, client = self.runtime(
            [
                TurnScript(writer_payload("before-rotation")),
                TurnScript(writer_payload("after-rotation", decision="complete")),
            ]
        )
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)

        rotated = await runtime.rehydrate([snapshot("before-rotation")])
        self.assertNotEqual(rotated.session_id, first.session.session_id)
        with self.assertRaisesRegex(
            RuntimeInvariantError, "writer session differs from the Branch binding"
        ):
            await runtime.start_writer(writer_request(2, [snapshot("a")]), rotated)

        runtime.rebind_branch_writer_session("branch-1", rotated)
        second = await runtime.start_writer(writer_request(2, [snapshot("a")]), rotated)
        output = await runtime.collect_writer(second)

        self.assertEqual(output.content.claim, "Claim after-rotation")
        self.assertEqual(second.session.session_id, rotated.session_id)
        self.assertEqual(
            [call["threadId"] for call in client.turn_start_calls],
            [first.session.session_id, rotated.session_id],
        )

    async def test_rotation_leaves_the_superseded_session_unable_to_advance(
        self,
    ) -> None:
        # The invariant still has to mean something after a rebind: the thread
        # the Branch left keeps its ownership - it is still a valid fork anchor
        # for the steps it sealed - but it can no longer take the Branch's next
        # turn, and no other Branch can be moved onto the rotated thread.
        runtime, _client = self.runtime([TurnScript(writer_payload("bound"))])
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)
        rotated = await runtime.rehydrate([snapshot("bound")])
        runtime.rebind_branch_writer_session("branch-1", rotated)

        with self.assertRaisesRegex(
            RuntimeInvariantError, "writer session differs from the Branch binding"
        ):
            await runtime.start_writer(writer_request(2), first.session)
        with self.assertRaisesRegex(
            RuntimeInvariantError, "already owned by another Branch"
        ):
            runtime.rebind_branch_writer_session("branch-2", rotated)
        with self.assertRaisesRegex(RuntimeInvariantError, "not a live writer thread"):
            runtime.rebind_branch_writer_session(
                "branch-1", RuntimeSession("unknown", ProviderLineage.REHYDRATED)
            )
        with self.assertRaisesRegex(RuntimeInvariantError, "must be non-empty"):
            runtime.rebind_branch_writer_session("  ", rotated)

    async def test_unlimited_calls_reach_provider_across_many_writer_turns(
        self,
    ) -> None:
        turn_count = 35
        runtime, client = self.runtime(
            [TurnScript(writer_payload(str(slot))) for slot in range(turn_count)],
            run_config=replace(config(), record_version="1.1", max_model_calls=None),
        )
        for slot in range(turn_count):
            invocation = await runtime.start_writer(
                replace(
                    writer_request(1, branch_id=f"branch-{slot}"),
                    record_version="1.1",
                ),
                None,
            )
            output = await runtime.collect_writer(invocation)
            self.assertEqual(output.content.claim, f"Claim {slot}")

        # Exercise both turn and thread tracking beyond the fixture's finite limits.
        self.assertEqual(len(client.turn_start_calls), turn_count)
        self.assertEqual(len(client.thread_start_calls), turn_count)

    async def test_finite_call_limit_rejects_before_extra_provider_turn(self) -> None:
        for record_version in ("1.0", "1.1"):
            with self.subTest(record_version=record_version):
                runtime, client = self.runtime(
                    [TurnScript(writer_payload("one"))],
                    run_config=replace(
                        config(), record_version=record_version, max_model_calls=1
                    ),
                )
                first = await runtime.start_writer(
                    replace(writer_request(1), record_version=record_version), None
                )
                await runtime.collect_writer(first)
                with self.assertRaisesRegex(
                    RuntimeInvariantError, "max_model_calls tracking limit exceeded"
                ):
                    await runtime.start_writer(
                        replace(writer_request(2), record_version=record_version),
                        first.session,
                    )
                self.assertEqual(len(client.turn_start_calls), 1)

    async def test_completed_writer_turn_can_fork(self) -> None:
        runtime, client = self.runtime(
            [
                TurnScript(
                    writer_payload(
                        "fork-anchor",
                        decision="fork",
                        alternatives=("Try a dual construction.",),
                    )
                )
            ]
        )
        invocation = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(invocation)
        child = await runtime.fork(invocation.session, invocation.operation_id)
        self.assertEqual(child.lineage, ProviderLineage.FORKED)
        self.assertNotEqual(child.session_id, invocation.session.session_id)
        fork_call = client.thread_fork_calls[0]
        self.assertEqual(fork_call["lastTurnId"], invocation.operation_id)
        self.assertFalse(fork_call["ephemeral"])
        assert_closed_thread_config(self, fork_call)
        self.assertEqual(
            fork_call["config"]["model_reasoning_effort"], config().writer.effort
        )

    async def test_a_fork_replaying_the_anchor_on_the_child_keeps_the_router_alive(
        self,
    ) -> None:
        """The child's copy of the anchor must not be read as a stray event.

        A live fork probe (its third attempt) got
        past both anchor-comparison fixes, forked a real tool-using Writer turn,
        and then stopped: forking makes the App Server restate the copied turn's
        terminal events on the *child* thread under the anchor's own turn id.
        The router routed that by turn id to the parent's operation, found a
        threadId that was not the parent's, and raised - which ends the router
        task for good.  Every later turn then completed at the provider and was
        never observed, so the branch burned its whole turn timeout, failed on
        an interrupt the provider had nothing left to interrupt, and the run
        died on the stale in-flight entry the next Writer turn tripped over.
        """

        runtime, client = self.runtime(
            [
                TurnScript(
                    writer_payload(
                        "fork-anchor",
                        decision="fork",
                        alternatives=("Try a dual construction.",),
                    ),
                    terminal_items_view="summary",
                ),
                TurnScript(writer_payload("after-fork", decision="complete")),
            ],
            replay_forked_history=True,
        )
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)

        original_fork = client.thread_fork

        async def fork_with_early_replay(*args, **kwargs):
            result = await original_fork(*args, **kwargs)
            # Deliver copied history before the awaiting runtime sees the
            # validated response and knows the new child thread id.
            await asyncio.sleep(0)
            return result

        client.thread_fork = fork_with_early_replay
        child = await runtime.fork(first.session, first.operation_id)
        self.assertEqual(child.lineage, ProviderLineage.FORKED)

        # The parent thread must still be able to run and complete its next
        # turn.  Before the fix this hung until the turn timeout, because the
        # router that would have delivered the completion was already dead.
        second = await runtime.start_writer(writer_request(2), first.session)
        sealed = await runtime.collect_writer(second)

        self.assertEqual(sealed.content.claim, "Claim after-fork")
        self.assertIsNone(runtime._router_error)
        # The copied history was dropped, not folded into the sealed anchor.
        anchor_state = runtime._operations[first.operation_id]
        self.assertEqual(anchor_state.terminal_turn["itemsView"], "summary")

    async def test_pending_fork_does_not_adopt_unrelated_replayed_thread(self) -> None:
        runtime, client = self.runtime([TurnScript(writer_payload("anchor"))])
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)
        original_fork = client.thread_fork

        async def fork_with_unrelated_replay(*args, **kwargs):
            result = await original_fork(*args, **kwargs)
            await client._emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "unrelated-thread",
                        "turn": copy.deepcopy(result["thread"]["turns"][0]),
                    },
                }
            )
            await asyncio.sleep(0)
            self.assertEqual(len(runtime._pending_fork_events), 1)
            return result

        client.thread_fork = fork_with_unrelated_replay
        with self.assertRaisesRegex(RuntimeInvocationError, "threadId does not match"):
            await runtime.fork(first.session, first.operation_id)
        self.assertNotIn("unrelated-thread", runtime._forked_parents)
        self.assertIsNotNone(runtime._router_error)
        with self.assertRaises(RuntimeInvocationError):
            await runtime.close()

    async def test_pending_fork_never_buffers_a_live_wrong_thread_event(self) -> None:
        runtime, _client = self.runtime([TurnScript(writer_payload("live"), hold=True)])
        invocation = await runtime.start_writer(writer_request(1), None)
        runtime._pending_forks[invocation.session.session_id] = 1
        event = normalize_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "unrelated-thread",
                    "turnId": invocation.operation_id,
                    "itemId": "unexpected",
                    "delta": "wrong route",
                },
            },
            999,
        )
        with self.assertRaisesRegex(AppServerProtocolError, "threadId does not match"):
            runtime._route_event(event)
        self.assertEqual(runtime._pending_fork_events, [])

    async def test_rejected_fork_response_does_not_approve_buffered_history(
        self,
    ) -> None:
        runtime, client = self.runtime(
            [TurnScript(writer_payload("anchor"))],
            replay_forked_history=True,
        )
        first = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(first)
        original_fork = client.thread_fork

        async def fork_with_invalid_response(*args, **kwargs):
            result = await original_fork(*args, **kwargs)
            await asyncio.sleep(0)
            self.assertEqual(len(runtime._pending_fork_events), 1)
            result["thread"]["ephemeral"] = True
            return result

        client.thread_fork = fork_with_invalid_response
        with self.assertRaisesRegex(RuntimeInvocationError, "threadId does not match"):
            await runtime.fork(first.session, first.operation_id)
        self.assertEqual(runtime._forked_parents, {})
        self.assertEqual(runtime._pending_forks, {})
        with self.assertRaises(RuntimeInvocationError):
            await runtime.close()

    async def test_a_fork_anchor_re_rendered_in_the_full_view_still_forks(
        self,
    ) -> None:
        # A live fork probe (second attempt) died here: the anchor read back for
        # the fork was the same sealed turn in a wider view, and a deep equality
        # against the recorded one called that a changed anchor.
        runtime, client = self.runtime(
            [
                TurnScript(
                    writer_payload(
                        "fork-anchor",
                        decision="fork",
                        alternatives=("Try a dual construction.",),
                    ),
                    terminal_items_view="summary",
                )
            ]
        )
        invocation = await runtime.start_writer(writer_request(1), None)
        sealed = await runtime.collect_writer(invocation)
        thread = client.threads[invocation.session.session_id]
        thread["turns"] = [
            rendered_in_full_view(turn)
            if turn["id"] == invocation.operation_id
            else turn
            for turn in thread["turns"]
        ]

        child = await runtime.fork(invocation.session, invocation.operation_id)

        self.assertEqual(child.lineage, ProviderLineage.FORKED)
        self.assertEqual(
            client.thread_fork_calls[0]["lastTurnId"], invocation.operation_id
        )
        # The wider view is tolerated, never adopted: the answer that was
        # forked from is still the one the run sealed.
        self.assertEqual(sealed.content.claim, "Claim fork-anchor")

    async def test_a_changed_fork_anchor_fails_the_fork_and_not_the_run(self) -> None:
        # The tolerance is for the rendering only.  A different final answer in
        # the wider view is a changed anchor, and must still refuse the fork -
        # as a fork-scoped failure the orchestrator traces, not a dead run.
        runtime, client = self.runtime(
            [
                TurnScript(
                    writer_payload(
                        "fork-anchor",
                        decision="fork",
                        alternatives=("Try a dual construction.",),
                    ),
                    terminal_items_view="summary",
                ),
                TurnScript(writer_payload("after-the-refusal", decision="complete")),
            ]
        )
        invocation = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(invocation)
        thread = client.threads[invocation.session.session_id]
        rewritten = rendered_in_full_view(
            client._turn(invocation.session.session_id, invocation.operation_id)
        )
        rewritten["items"][-1]["text"] = json.dumps(
            writer_payload("rewritten", decision="complete"),
            sort_keys=True,
            separators=(",", ":"),
        )
        thread["turns"] = [
            rewritten if turn["id"] == invocation.operation_id else turn
            for turn in thread["turns"]
        ]

        with self.assertRaises(ProviderForkError) as raised:
            await runtime.fork(invocation.session, invocation.operation_id)

        self.assertIn(
            "fork anchor changed after live collection", str(raised.exception)
        )
        self.assertEqual(client.thread_fork_calls, [])
        # The refusal is confined to the fork: the same client and the parent
        # session still serve the branch that asked for it.
        following = await runtime.start_writer(writer_request(2), invocation.session)
        self.assertEqual(
            (await runtime.collect_writer(following)).content.claim,
            "Claim after-the-refusal",
        )

    async def test_invalid_five_field_output_fails_closed(self) -> None:
        payload = writer_payload("invalid")
        payload["content"].pop("scope")
        runtime, _client = self.runtime([TurnScript(payload)])
        invocation = await runtime.start_writer(writer_request(1), None)
        with self.assertRaises(RuntimeInvocationError) as raised:
            await runtime.collect_writer(invocation)
        self.assertEqual(raised.exception.failure_kind, "invalid_model_output")

    async def test_missing_and_invalid_writer_control_fail_closed(self) -> None:
        missing = {"content": step_payload("missing")}
        invalid = writer_payload("invalid-control")
        invalid["control"] = {
            "decision": "complete",
            "alternatives": ["Unexpected alternative."],
        }
        for script in (TurnScript(missing), TurnScript(invalid)):
            with self.subTest(script=script):
                runtime, _client = self.runtime([script])
                invocation = await runtime.start_writer(writer_request(1), None)
                with self.assertRaises(RuntimeInvocationError) as raised:
                    await runtime.collect_writer(invocation)
                self.assertEqual(raised.exception.failure_kind, "invalid_model_output")
                await runtime.close()

    async def test_terminal_provider_tools_are_observable_for_every_role(self) -> None:
        writer_runtime, _writer_client = self.runtime(
            [
                TurnScript(
                    writer_payload("web", decision="complete"),
                    extra_item_type="webSearch",
                )
            ]
        )
        writer = await writer_runtime.start_writer(writer_request(1), None)
        writer_output = await writer_runtime.collect_writer(writer)
        self.assertEqual(writer_output.content.claim, "Claim web")

        target = snapshot("target")
        checker_runtime, _checker_client = self.runtime(
            [
                TurnScript(
                    {"verdict": "ok", "reason": "No defect.", "evidence": []},
                    extra_item_type="commandExecution",
                )
            ]
        )
        checker = await checker_runtime.start_checker(
            CheckRequest(
                run_id=config().run_id,
                check_id="check-tool-denied",
                task_text="Derive the invariant.",
                target=target,
                transcript=(target,),
            )
        )
        checker_output = await checker_runtime.collect_checker(checker)
        self.assertEqual(checker_output.verdict, "ok")

        judge_runtime, _judge_client = self.runtime(
            [
                TurnScript(
                    {"verdict": "pass", "reason": "Complete.", "score": 0.9},
                    extra_item_type="mcpToolCall",
                )
            ]
        )
        judge = await judge_runtime.start_judge(
            JudgeRequest(
                run_id=config().run_id,
                judgement_id="judge-tool-denied",
                candidate_id="candidate-tool-denied",
                task_text="Derive the invariant.",
                transcript=(target,),
            )
        )
        judge_output = await judge_runtime.collect_judge(judge)
        self.assertEqual(judge_output.verdict, "pass")

    async def test_model_reroute_is_recorded_as_diagnostic_event(self) -> None:
        target = snapshot("target")
        runtime, client = self.runtime(
            [
                TurnScript(
                    {"verdict": "ok", "reason": "No defect.", "evidence": []},
                    reroute_to_model="gpt-unconfigured",
                )
            ]
        )
        checker = await runtime.start_checker(
            CheckRequest(
                run_id=config().run_id,
                check_id="check-rerouted",
                task_text="Derive the invariant.",
                target=target,
                transcript=(target,),
            )
        )
        output = await runtime.collect_checker(checker)
        self.assertEqual(output.verdict, "ok")
        self.assertIn(
            "model/rerouted", {event.method for event in runtime.provider_events}
        )
        self.assertIs(client.thread_start_calls[0]["allowProviderModelFallback"], False)

    async def test_public_health_barrier_tolerates_late_diagnostic_event(self) -> None:
        runtime, client = self.runtime(
            [TurnScript(writer_payload("sealed", decision="complete"))]
        )
        invocation = await runtime.start_writer(writer_request(1), None)
        await runtime.collect_writer(invocation)
        await client._emit(
            {
                "method": "model/rerouted",
                "params": {
                    "fromModel": "gpt-writer",
                    "toModel": "gpt-other",
                    "reason": "highRiskCyberActivity",
                    "threadId": invocation.session.session_id,
                    "turnId": invocation.operation_id,
                },
            }
        )
        await runtime.assert_healthy()
        self.assertIn(
            "model/rerouted", {event.method for event in runtime.provider_events}
        )

    async def test_checker_and_judge_use_independent_minimal_threads(self) -> None:
        runtime, client = self.runtime(
            [
                TurnScript({"verdict": "ok", "reason": "No defect.", "evidence": []}),
                TurnScript({"verdict": "pass", "reason": "Complete.", "score": 0.9}),
            ]
        )
        target = snapshot("target")
        check = await runtime.start_checker(
            CheckRequest(
                run_id=config().run_id,
                check_id="check-1",
                task_text="Derive the invariant.",
                target=target,
                transcript=(target,),
            )
        )
        check_output = await runtime.collect_checker(check)
        judge = await runtime.start_judge(
            JudgeRequest(
                run_id=config().run_id,
                judgement_id="judge-1",
                candidate_id="candidate-1",
                task_text="Derive the invariant.",
                transcript=(target,),
            )
        )
        judge_output = await runtime.collect_judge(judge)

        self.assertNotEqual(check.session.session_id, judge.session.session_id)
        self.assertEqual(check_output.verdict, "ok")
        self.assertEqual(judge_output.verdict, "pass")
        self.assertEqual(
            [call["ephemeral"] for call in client.thread_start_calls], [True, True]
        )
        self.assertEqual(
            [call["dynamicTools"] for call in client.thread_start_calls], [[], []]
        )
        for call in client.thread_start_calls:
            assert_closed_thread_config(self, call)
            self.assertIs(call["allowProviderModelFallback"], False)
            self.assertNotIn("environments", call)
            self.assertEqual(call["selectedCapabilityRoots"], [])
        self.assertEqual(client.turn_start_calls[0]["model"], "gpt-checker")
        self.assertEqual(client.turn_start_calls[0]["effort"], "medium")
        self.assertEqual(client.turn_start_calls[1]["model"], "gpt-judge")
        self.assertEqual(client.turn_start_calls[1]["effort"], "xhigh")

    async def test_checker_and_judge_reject_persistent_thread_responses(self) -> None:
        target = snapshot("target")
        cases = (
            (
                "checker",
                lambda runtime: runtime.start_checker(
                    CheckRequest(
                        run_id=config().run_id,
                        check_id="check-ephemeral",
                        task_text="Derive the invariant.",
                        target=target,
                        transcript=(target,),
                    )
                ),
            ),
            (
                "judge",
                lambda runtime: runtime.start_judge(
                    JudgeRequest(
                        run_id=config().run_id,
                        judgement_id="judge-ephemeral",
                        candidate_id="candidate-ephemeral",
                        task_text="Derive the invariant.",
                        transcript=(target,),
                    )
                ),
            ),
        )
        for role, start in cases:
            with self.subTest(role=role):
                runtime, client = self.runtime([])
                client.force_start_ephemeral = False
                with self.assertRaises(RuntimeInvariantError):
                    await start(runtime)
                await runtime.close()

    async def test_launch_settings_bind_command_and_close_thread_config(self) -> None:
        launch_settings = settings()
        command = authorized_command()
        with self.assertRaises(RuntimeInvariantError):
            CodexAppServerRuntime(
                config=config(),
                client=ScriptedAppServerClient([]),  # type: ignore[arg-type]
                settings=launch_settings,
            )
        substitutions = (
            AppServerCommand(
                argv=("/attacker/codex", *command.argv[1:]),
                cwd=command.cwd,
                environment=command.environment,
            ),
            AppServerCommand(
                argv=command.argv,
                cwd=command.cwd,
                environment={**command.environment, "HOME": "/attacker/home"},
            ),
            AppServerCommand(
                argv=command.argv,
                cwd=command.cwd,
                environment={
                    **command.environment,
                    "CODEX_HOME": "/attacker/codex-home",
                },
            ),
        )
        for substitution in substitutions:
            with (
                self.subTest(command=substitution),
                self.assertRaises(RuntimeInvariantError),
            ):
                launch_settings.assert_authorized_command(substitution)

        returned_config = launch_settings.thread_config
        returned_config["web_search"] = "live"  # type: ignore[index]
        self.assertEqual(launch_settings.thread_config["web_search"], "disabled")
        self.assertEqual(launch_settings.thread_config["mcp_servers"], {})
        with self.assertRaises(TypeError):
            LaunchSettings(
                workspace=ROOT,
                gate_result=launch_settings.gate_result,
                authorized_command=command,
                thread_config={"mcp_servers": {"unsafe": {}}},  # type: ignore[call-arg]
            )

    async def test_resumed_writer_uses_closed_thread_config(self) -> None:
        client = ScriptedAppServerClient(
            [TurnScript(writer_payload("resumed", decision="complete"))]
        )
        thread_id = "persisted-writer"
        client.threads[thread_id] = {
            "id": thread_id,
            "sessionId": thread_id,
            "ephemeral": False,
            "cwd": str(ROOT),
            "modelProvider": "openai",
            "turns": [],
        }
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=client,
            settings=settings(),  # type: ignore[arg-type]
        )
        self.addAsyncCleanup(runtime.close)
        runtime.register_recovered_writer_session(
            RuntimeSession(thread_id, ProviderLineage.NATIVE), "branch-1"
        )

        invocation = await runtime.start_writer(
            writer_request(1),
            RuntimeSession(thread_id, ProviderLineage.NATIVE),
        )
        await runtime.collect_writer(invocation)

        self.assertEqual(len(client.thread_resume_calls), 1)
        resume_call = client.thread_resume_calls[0]
        self.assertEqual(resume_call["threadId"], thread_id)
        assert_closed_thread_config(self, resume_call)
        self.assertEqual(
            resume_call["config"]["model_reasoning_effort"], config().writer.effort
        )

    async def test_scripted_resume_uses_actual_config_or_server_default(self) -> None:
        client = ScriptedAppServerClient([])
        omitted = await client.thread_resume(
            "persisted", overrides={"config": {}}, expected_reasoning_effort="high"
        )
        explicit = await client.thread_resume(
            "persisted",
            overrides={"config": {"model_reasoning_effort": "xhigh"}},
            expected_reasoning_effort="high",
        )
        self.assertEqual(omitted["reasoningEffort"], "low")
        self.assertEqual(explicit["reasoningEffort"], "xhigh")
        self.assertEqual(client.turn_start_calls, [])

    async def test_cold_resume_rejects_reported_effort_drift_before_inference(
        self,
    ) -> None:
        runtime, client = self.runtime([])
        client.force_resume_reasoning_effort = "low"
        session = RuntimeSession("persisted", ProviderLineage.NATIVE)
        runtime.register_recovered_writer_session(session, "branch-1")
        with self.assertRaisesRegex(RuntimeInvariantError, "reasoningEffort differs"):
            await runtime.start_writer(writer_request(1), session)
        self.assertEqual(
            client.thread_resume_calls[0]["config"]["model_reasoning_effort"], "high"
        )
        self.assertEqual(client.turn_start_calls, [])

    async def test_cold_reconcile_pins_each_role_effort_without_new_turn(self) -> None:
        for role in ModelRole:
            with self.subTest(role=role.value):
                runtime, client = self.runtime([])
                thread_id = f"persisted-{role.value}"
                client.threads[thread_id] = {
                    "id": thread_id,
                    "sessionId": thread_id,
                    "ephemeral": role is not ModelRole.WRITER,
                    "cwd": str(ROOT),
                    "modelProvider": "openai",
                    "turns": [
                        {"id": "running-turn", "status": "inProgress", "items": []}
                    ],
                }
                result = await runtime.reconcile(
                    RuntimeInvocation(
                        RuntimeSession(thread_id, ProviderLineage.NATIVE),
                        "running-turn",
                        role,
                    )
                )
                self.assertEqual(result.status, ReconcileStatus.RUNNING)
                self.assertEqual(
                    client.thread_resume_calls[0]["config"]["model_reasoning_effort"],
                    config().model_for(role).effort,
                )
                self.assertEqual(
                    client.thread_read_calls,
                    [{"threadId": thread_id, "includeTurns": True}],
                )
                self.assertEqual(client.turn_start_calls, [])

    async def test_global_early_event_buffers_are_bounded(self) -> None:
        runtime, _client = self.runtime([])
        runtime.settings = settings(max_early_turn_ids=1, max_early_events=1)
        first = normalize_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-unknown",
                    "turnId": "turn-unknown-1",
                    "itemId": "item-1",
                    "delta": "one",
                },
            },
            1,
        )
        second = normalize_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-unknown",
                    "turnId": "turn-unknown-2",
                    "itemId": "item-2",
                    "delta": "two",
                },
            },
            2,
        )
        runtime._route_event(first)
        with self.assertRaises(AppServerProtocolError):
            runtime._route_event(second)

    async def test_a_long_structured_turn_fits_the_default_operation_event_buffer(
        self,
    ) -> None:
        """A long structured Writer turn must not fill the operation buffer.

        A Writer turn of about 30,000 answer characters already overflows an
        8192-event operation buffer.  A 100,000-character structured turn at a
        pessimistic two characters per routed event needs 50,000 events; the
        default must admit it, and the buffer must stay a finite guard.
        """

        defaults = settings()
        self.assertGreaterEqual(defaults.max_events_per_operation, 2 * 50_000)
        self.assertLess(defaults.max_events_per_operation, 2**20)
        runtime, _client = self.runtime(
            [TurnScript(writer_payload("long"), extra_deltas=50_000)],
            run_config=replace(config(), record_version="1.1"),
        )
        invocation = await runtime.start_writer(writer_request(1), None)
        output = await runtime.collect_writer(invocation)
        self.assertEqual(output.content.claim, step_payload("long")["claim"])
        state = runtime._operations[invocation.operation_id]
        self.assertGreater(len(state.events), 50_000)

        bounded, _client = self.runtime(
            [TurnScript(writer_payload("long"), extra_deltas=64)],
            run_config=replace(config(), record_version="1.1"),
        )
        bounded.settings = settings(max_events_per_operation=32)
        invocation = await bounded.start_writer(writer_request(1), None)
        with self.assertRaises(RuntimeInvocationError) as caught:
            await bounded.collect_writer(invocation)
        self.assertIn("operation event buffer is full", str(caught.exception))
        with self.assertRaises(RuntimeInvocationError):
            await bounded.close()

    async def test_unscoped_audit_history_has_no_unlimited_run_lifetime_cap(
        self,
    ) -> None:
        for call_limit in (None, 20):
            with self.subTest(call_limit=call_limit):
                runtime, client = self.runtime(
                    [TurnScript(writer_payload("one"))],
                    run_config=replace(
                        config(), record_version="1.1", max_model_calls=call_limit
                    ),
                )
                runtime.settings = settings(max_unscoped_events=2)
                invocation = await runtime.start_writer(writer_request(1), None)
                await runtime.collect_writer(invocation)
                thread = client.threads[invocation.session.session_id]
                notifications = [
                    normalize_notification(
                        {"method": "thread/started", "params": {"thread": thread}},
                        runtime._routed_sequence + sequence,
                    )
                    for sequence in (1, 2, 3)
                ]
                for event in notifications[:2]:
                    runtime._route_event(event)
                if call_limit is None:
                    runtime._route_event(notifications[2])
                    self.assertEqual(
                        [
                            event
                            for event in runtime.provider_events
                            if event.method == "thread/started"
                        ],
                        notifications,
                    )
                else:
                    with self.assertRaisesRegex(
                        AppServerProtocolError, "unscoped event buffer is full"
                    ):
                        runtime._route_event(notifications[2])

    async def test_interrupt_and_reconcile_terminal_states(self) -> None:
        runtime, client = self.runtime(
            [
                TurnScript(None, hold=True),
                TurnScript(None, status="failed"),
                TurnScript(writer_payload("complete", decision="complete")),
            ]
        )
        running = await runtime.start_writer(writer_request(1), None)
        await asyncio.sleep(0)
        running_state = await runtime.reconcile(running)
        self.assertEqual(running_state.status, ReconcileStatus.RUNNING)
        interrupted = await runtime.interrupt(running)
        self.assertIsInstance(interrupted.partial_output, str)
        await asyncio.sleep(0)
        interrupted_state = await runtime.reconcile(running)
        self.assertEqual(interrupted_state.status, ReconcileStatus.INTERRUPTED)

        failed = await runtime.start_writer(writer_request(2), running.session)
        await client._emit_tasks[failed.operation_id]
        failed_state = await runtime.reconcile(failed)
        self.assertEqual(failed_state.status, ReconcileStatus.FAILED)
        self.assertEqual(failed_state.failure_kind, "provider_failed")

        completed = await runtime.start_writer(writer_request(3), running.session)
        completed_output = await runtime.collect_writer(completed)
        completed_state = await runtime.reconcile(completed)
        self.assertEqual(completed_state.status, ReconcileStatus.COMPLETED)
        self.assertEqual(completed_state.output, completed_output)

        missing_invocation = RuntimeInvocation(
            RuntimeSession("missing-thread", ProviderLineage.NATIVE),
            "missing-turn",
            ModelRole.WRITER,
        )
        client.missing_threads.add("missing-thread")
        missing_state = await runtime.reconcile(missing_invocation)
        self.assertEqual(missing_state.status, ReconcileStatus.MISSING)

    async def test_timeout_requires_interrupted_snapshot_before_retry(self) -> None:
        client = ScriptedAppServerClient(
            [
                TurnScript(None, hold=True),
                TurnScript(writer_payload("retry", decision="complete")),
            ]
        )
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=client,
            settings=settings(turn_timeout=0.02),  # type: ignore[arg-type]
        )
        self.addAsyncCleanup(runtime.close)
        first = await runtime.start_writer(writer_request(1), None)
        with self.assertRaises(RuntimeInvocationError) as timeout_error:
            await runtime.collect_writer(first)
        self.assertEqual(timeout_error.exception.failure_kind, "provider_timeout")
        self.assertTrue(timeout_error.exception.retryable)
        second = await runtime.start_writer(writer_request(2), first.session)
        output = await runtime.collect_writer(second)
        self.assertEqual(output.content.claim, "Claim retry")

    async def test_restart_reconcile_uses_completed_provider_snapshot(self) -> None:
        client = ScriptedAppServerClient([])
        thread_id = "persisted-thread"
        turn_id = "persisted-turn"
        client.threads[thread_id] = {
            "id": thread_id,
            "sessionId": thread_id,
            "ephemeral": False,
            "cwd": str(ROOT),
            "modelProvider": "openai",
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": "persisted-answer",
                            "text": json.dumps(
                                writer_payload(
                                    "persisted",
                                    decision="fork",
                                    alternatives=("Try the dual route.",),
                                )
                            ),
                            "phase": "final_answer",
                        },
                    ],
                }
            ],
        }
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=client,
            settings=settings(),  # type: ignore[arg-type]
        )
        self.addAsyncCleanup(runtime.close)

        result = await runtime.reconcile(
            RuntimeInvocation(
                RuntimeSession(thread_id, ProviderLineage.NATIVE),
                turn_id,
                ModelRole.WRITER,
            )
        )

        self.assertEqual(result.status, ReconcileStatus.COMPLETED)
        self.assertIsNotNone(result.output)
        assert result.output is not None
        self.assertEqual(result.output.usage.to_record(), {})

    async def test_rehydrate_uses_exact_sealed_transcript(self) -> None:
        runtime, client = self.runtime([])
        transcript = (snapshot("one"), snapshot("two"))
        session = await runtime.rehydrate(transcript)
        self.assertEqual(session.lineage, ProviderLineage.REHYDRATED)
        instructions = client.thread_start_calls[0]["developerInstructions"]
        self.assertIn('"step_revision_id":"step-one"', instructions)
        self.assertIn('"claim":"Claim two"', instructions)
        self.assertFalse(client.thread_start_calls[0]["ephemeral"])


if __name__ == "__main__":
    unittest.main()
