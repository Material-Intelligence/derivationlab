"""Scripted JSONL child used by AppServerClient's hermetic tests."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("expected object")  # noqa: TRY004
    return value


def response(request: dict[str, Any], result: Any) -> None:
    send({"id": request["id"], "result": result})


def initialize(scenario: str) -> bool:
    request = read_message()
    if request is None:
        return False
    if request.get("method") != "initialize":
        raise RuntimeError("first request must initialize")
    if (
        request.get("params", {}).get("capabilities", {}).get("experimentalApi")
        is not True
    ):
        raise RuntimeError("experimentalApi capability is required")
    if scenario == "startup_timeout":
        time.sleep(60)
        return False
    if scenario == "malformed_initialize":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        return False

    version = "0.148.0" if scenario == "bad_version" else "0.147.0"
    client_name = request["params"]["clientInfo"]["name"]
    if scenario == "bad_identity":
        client_name = "different_client"
    send(
        {
            "method": "configWarning",
            "params": {"summary": "fake warning", "details": None},
            "emittedAtMs": 1,
        }
    )
    response(
        request,
        {
            "userAgent": f"{client_name}/{version} (fake; live-shaped)",
            "codexHome": "/fixture/codex-home",
            "platformFamily": "unix",
            "platformOs": "macos",
        },
    )
    initialized = read_message()
    return initialized is not None and initialized.get("method") == "initialized"


def thread_result(
    thread_id: str,
    *,
    request_params: dict[str, Any],
    instruction_sources: list[str] | None = None,
    include_instruction_sources: bool = True,
    ephemeral: bool = False,
) -> dict[str, Any]:
    cwd = request_params["cwd"]
    result: dict[str, Any] = {
        "activePermissionProfile": {
            "id": request_params["permissions"],
            "extends": None,
        },
        "approvalPolicy": request_params["approvalPolicy"],
        "approvalsReviewer": "user",
        "cwd": cwd,
        "model": request_params["model"],
        "modelProvider": request_params["modelProvider"],
        "reasoningEffort": request_params.get("config", {}).get(
            "model_reasoning_effort", "high"
        ),
        "runtimeWorkspaceRoots": request_params["runtimeWorkspaceRoots"],
        "sandbox": {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": [cwd],
            "excludeSlashTmp": True,
            "excludeTmpdirEnvVar": True,
        },
        "thread": {
            "id": thread_id,
            "sessionId": "thread-1",
            "ephemeral": ephemeral,
            "turns": [],
        },
    }
    if "serviceTier" in request_params:
        result["serviceTier"] = request_params["serviceTier"]
    if include_instruction_sources:
        result["instructionSources"] = instruction_sources or []
    return result


def handle_happy(request: dict[str, Any]) -> None:
    method = request.get("method")
    if method == "account/rateLimits/read":
        response(
            request,
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "primary": {
                        "usedPercent": 31,
                        "windowDurationMins": 10080,
                        "resetsAt": 1788123456,
                    },
                    "secondary": None,
                },
                "rateLimitsByLimitId": None,
                "rateLimitResetCredits": None,
            },
        )
        send(
            {
                "method": "account/rateLimits/updated",
                "params": {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 32,
                            "windowDurationMins": 10080,
                            "resetsAt": 1788123456,
                        },
                    }
                },
            }
        )
        return
    if method == "account/read":
        response(
            request,
            {
                "account": {
                    "type": "chatgpt",
                    "email": None,
                    "planType": "pro",
                },
                "requiresOpenaiAuth": True,
            },
        )
        return
    if method == "model/list":
        cursor = request["params"].get("cursor")
        if cursor is None:
            response(
                request,
                {
                    "data": [
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "displayName": "GPT-5.6-Sol",
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "low"},
                                {"reasoningEffort": "medium"},
                                {"reasoningEffort": "ultra"},
                            ],
                        }
                    ],
                    "nextCursor": "page-2",
                },
            )
        else:
            response(
                request,
                {
                    "data": [
                        {
                            "id": "gpt-5.5",
                            "model": "gpt-5.5",
                            "displayName": "GPT-5.5",
                            "isDefault": False,
                            "defaultReasoningEffort": "xhigh",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "low"},
                                {"reasoningEffort": "xhigh"},
                            ],
                        }
                    ],
                    "nextCursor": None,
                },
            )
        return
    if method == "thread/start":
        response(
            request,
            thread_result("thread-1", request_params=request["params"]),
        )
        return
    if method == "thread/resume":
        result = thread_result(
            request["params"]["threadId"],
            request_params=request["params"],
        )
        result["thread"]["turns"] = [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "id": "item-1",
                        "text": '{"columns":[1,2,3,4,5]}',
                        "phase": "final_answer",
                    }
                ],
                "error": None,
            }
        ]
        response(
            request,
            result,
        )
        return
    if method == "turn/start":
        turn = {"id": "turn-1", "status": "inProgress", "items": [], "error": None}
        send(
            {
                "method": "turn/started",
                "params": {"threadId": request["params"]["threadId"], "turn": turn},
            }
        )
        response(request, {"turn": turn})
        final_item = {
            "type": "agentMessage",
            "id": "item-1",
            "text": '{"columns":[1,2,3,4,5]}',
            "phase": "final_answer",
        }
        send(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": final_item,
                    "startedAtMs": 10,
                },
            }
        )
        send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "delta": "done",
                },
            }
        )
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": final_item,
                    "completedAtMs": 11,
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [final_item],
                        "error": None,
                    },
                },
            }
        )
        return
    if method == "thread/read":
        final_item = {
            "type": "agentMessage",
            "id": "item-1",
            "text": '{"columns":[1,2,3,4,5]}',
            "phase": "final_answer",
        }
        response(
            request,
            {
                "thread": {
                    "id": request["params"]["threadId"],
                    "sessionId": "thread-1",
                    "ephemeral": False,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [final_item],
                            "error": None,
                        }
                    ],
                }
            },
        )
        return
    if method == "thread/fork":
        final_item = {
            "type": "agentMessage",
            "id": "item-1",
            "text": '{"columns":[1,2,3,4,5]}',
            "phase": "final_answer",
        }
        result = thread_result("thread-2", request_params=request["params"])
        result["thread"].update(
            {
                "forkedFromId": request["params"]["threadId"],
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [final_item],
                        "error": None,
                    }
                ],
            }
        )
        response(
            request,
            result,
        )
        return
    if method == "turn/interrupt":
        response(request, {})
        return
    if method == "command/exec":
        response(
            request,
            {
                "exitCode": 0,
                "stdout": "probe-ok\n",
                "stderr": "",
                "receivedParams": request["params"],
            },
        )
        return
    send(
        {
            "id": request["id"],
            "error": {"code": -32601, "message": f"unsupported {method}"},
        }
    )


def run(scenario: str) -> int:
    if not initialize(scenario):
        return 0
    if scenario == "bad_version":
        return 0
    if scenario == "bad_identity":
        return 0
    if scenario == "clean":
        while read_message() is not None:
            pass
        sys.stderr.write("CLEAN_EOF\n")
        sys.stderr.flush()
        return 0
    if scenario == "out_of_order":
        first = read_message()
        second = read_message()
        if first is None or second is None:
            return 2
        requests = {first["method"]: first, second["method"]: second}
        response(
            requests["thread/start"],
            thread_result(
                "thread-1",
                request_params=requests["thread/start"]["params"],
            ),
        )
        send({"method": "warning", "params": {"message": "interleaved"}})
        response(
            requests["account/read"],
            {"account": None, "requiresOpenaiAuth": True},
        )
        while read_message() is not None:
            pass
        return 0

    observed_tool_response: dict[str, Any] | None = None
    dynamic_scenarios = {
        "dynamic_tool_valid",
        "dynamic_tool_bad",
        "dynamic_tool_timeout",
        "dynamic_tool_duplicate",
        "dynamic_tool_late",
        "dynamic_tool_result_late",
        "dynamic_tool_wrong_thread",
        "dynamic_tool_wrong_turn",
        "dynamic_tool_wrong_tool",
    }
    for request in iter(read_message, None):
        if scenario == "byte_flood":
            for index in range(3):
                send(
                    {
                        "method": "warning",
                        "params": {"message": f"{index}:" + ("x" * 600)},
                    }
                )
            time.sleep(60)
            return 0
        raw_frames = {
            "duplicate_raw_id": '{"id":2,"id":3,"result":{}}',
            "duplicate_raw_method": (
                '{"method":"warning","method":"configWarning","params":{"message":"x"}}'
            ),
            "duplicate_raw_thread_id": (
                '{"method":"item/agentMessage/delta","params":{'
                '"delta":"x","itemId":"item-1","threadId":"thread-1",'
                '"threadId":"thread-2","turnId":"turn-1"}}'
            ),
            "duplicate_raw_turn_id": (
                '{"method":"item/agentMessage/delta","params":{'
                '"delta":"x","itemId":"item-1","threadId":"thread-1",'
                '"turnId":"turn-1","turnId":"turn-2"}}'
            ),
            "duplicate_raw_call_id": (
                '{"id":"request-1","method":"item/tool/call","params":{'
                '"arguments":{"decision":"continue","alternatives":[]},'
                '"callId":"call-1","callId":"call-2","namespace":null,'
                '"threadId":"thread-1","tool":"emit_writer_control",'
                '"turnId":"turn-1"}}'
            ),
            "duplicate_raw_arguments": (
                '{"id":"request-1","method":"item/tool/call","params":{'
                '"arguments":{"decision":"continue","alternatives":[]},'
                '"arguments":{"decision":"complete","alternatives":[]},'
                '"callId":"call-1","namespace":null,"threadId":"thread-1",'
                '"tool":"emit_writer_control","turnId":"turn-1"}}'
            ),
            "nonfinite_raw_usage": (
                '{"method":"thread/tokenUsage/updated","params":{'
                '"threadId":"thread-1","turnId":"turn-1",'
                '"tokenUsage":{"total":NaN}}}'
            ),
        }
        if scenario in raw_frames:
            sys.stdout.write(raw_frames[scenario] + "\n")
            sys.stdout.flush()
            continue
        if scenario == "dynamic_tool":
            send(
                {
                    "id": "tool-request-1",
                    "method": "item/tool/call",
                    "params": {
                        "arguments": {},
                        "callId": "tool-call-1",
                        "namespace": None,
                        "threadId": "thread-1",
                        "tool": "scientific_compute",
                        "turnId": "turn-1",
                    },
                }
            )
            time.sleep(60)
            return 0
        if scenario in dynamic_scenarios:
            method = request.get("method")
            if method == "thread/start":
                response(
                    request,
                    thread_result("thread-1", request_params=request["params"]),
                )
                continue
            if method == "turn/start":
                turn = {
                    "id": "turn-1",
                    "status": "inProgress",
                    "items": [],
                    "error": None,
                }
                send(
                    {
                        "method": "turn/started",
                        "params": {"threadId": "thread-1", "turn": turn},
                    }
                )
                response(request, {"turn": turn})
                call = {
                    "id": "tool-request-1",
                    "method": "item/tool/call",
                    "params": {
                        "arguments": (
                            {"operation": "unknown", "expression": "x**3"}
                            if scenario == "dynamic_tool_bad"
                            else {
                                "operation": "differentiate",
                                "expression": "x**3",
                                "variable": "x",
                            }
                        ),
                        "callId": "tool-call-1",
                        "namespace": None,
                        "threadId": (
                            "thread-other"
                            if scenario == "dynamic_tool_wrong_thread"
                            else "thread-1"
                        ),
                        "tool": (
                            "unconfigured_tool"
                            if scenario == "dynamic_tool_wrong_tool"
                            else "scientific_compute"
                        ),
                        "turnId": (
                            "turn-other"
                            if scenario == "dynamic_tool_wrong_turn"
                            else "turn-1"
                        ),
                    },
                }
                if scenario == "dynamic_tool_late":
                    send(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread-1",
                                "turn": {
                                    "id": "turn-1",
                                    "status": "completed",
                                    "items": [],
                                    "error": None,
                                },
                            },
                        }
                    )
                send(call)
                if scenario == "dynamic_tool_duplicate":
                    send({**call, "id": "tool-request-2"})
                if scenario == "dynamic_tool_result_late":
                    send(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread-1",
                                "turn": {
                                    "id": "turn-1",
                                    "status": "completed",
                                    "items": [],
                                    "error": None,
                                },
                            },
                        }
                    )
                if scenario not in {
                    "dynamic_tool_duplicate",
                    "dynamic_tool_late",
                    "dynamic_tool_result_late",
                    "dynamic_tool_wrong_thread",
                    "dynamic_tool_wrong_turn",
                    "dynamic_tool_wrong_tool",
                }:
                    observed_tool_response = read_message()
                    final_item = {
                        "type": "agentMessage",
                        "id": "item-1",
                        "text": "{}",
                        "phase": "final_answer",
                    }
                    send(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread-1",
                                "turn": {
                                    "id": "turn-1",
                                    "status": "completed",
                                    "items": [final_item],
                                    "error": None,
                                },
                            },
                        }
                    )
                continue
            if method == "account/read":
                response(
                    request,
                    {
                        "account": None,
                        "requiresOpenaiAuth": True,
                        "toolResponse": observed_tool_response,
                    },
                )
                continue
            handle_happy(request)
            continue
        if scenario == "duplicate_control":
            control_request = {
                "method": "item/tool/call",
                "params": {
                    "arguments": {
                        "decision": "continue",
                        "alternatives": [],
                    },
                    "callId": "control-call-1",
                    "namespace": None,
                    "threadId": "thread-1",
                    "tool": "emit_writer_control",
                    "turnId": "turn-1",
                },
            }
            send({"id": "control-request-1", **control_request})
            send(
                {
                    "id": "control-request-2",
                    **control_request,
                    "params": {
                        **control_request["params"],
                        "callId": "control-call-2",
                    },
                }
            )
            time.sleep(60)
            return 0
        if scenario in {"late_mutable", "late_control"}:
            terminal = {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "id": "item-1",
                        "text": "{}",
                        "phase": "final_answer",
                    }
                ],
                "error": None,
            }
            send(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": terminal},
                }
            )
            if scenario == "late_mutable":
                send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "delta": "late",
                            "itemId": "item-1",
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                        },
                    }
                )
            else:
                send(
                    {
                        "id": "late-control",
                        "method": "item/tool/call",
                        "params": {
                            "arguments": {
                                "decision": "continue",
                                "alternatives": [],
                            },
                            "callId": "late-call",
                            "namespace": None,
                            "threadId": "thread-1",
                            "tool": "emit_writer_control",
                            "turnId": "turn-1",
                        },
                    }
                )
            time.sleep(60)
            return 0
        if scenario == "malformed":
            sys.stdout.write("this-is-not-json\n")
            sys.stdout.flush()
            time.sleep(60)
            return 0
        if scenario == "malformed_response":
            send({"id": request["id"]})
            time.sleep(60)
            return 0
        if scenario == "crash":
            sys.stderr.write("scripted child crash\n")
            sys.stderr.flush()
            os._exit(7)
        if scenario == "timeout":
            continue
        if scenario == "approval":
            send(
                {
                    "id": 900,
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "thread-1", "questions": []},
                }
            )
            time.sleep(60)
            return 0
        if scenario == "duplicate":
            response(request, {"account": None, "requiresOpenaiAuth": True})
            response(request, {"account": None, "requiresOpenaiAuth": True})
            continue
        if scenario == "unknown_notification":
            send({"method": "future/required", "params": {}})
            response(request, {"account": None, "requiresOpenaiAuth": True})
            continue
        if scenario == "unknown_item":
            send(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "futureItem", "id": "item-1"},
                        "startedAtMs": 10,
                    },
                }
            )
            time.sleep(60)
            return 0
        if scenario == "unknown_snapshot_item":
            response(
                request,
                {
                    "thread": {
                        "id": "thread-1",
                        "sessionId": "thread-1",
                        "ephemeral": False,
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [{"type": "futureItem", "id": "item-1"}],
                            }
                        ],
                    }
                },
            )
            continue
        if (
            scenario == "prepopulated_thread"
            and request.get("method") == "thread/start"
        ):
            result = thread_result("thread-1", request_params=request["params"])
            result["thread"]["turns"] = [
                {
                    "id": "hidden-turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": "hidden-item",
                            "text": "{}",
                            "phase": "final_answer",
                        }
                    ],
                    "error": None,
                }
            ]
            response(request, result)
            continue
        if scenario == "prepopulated_turn" and request.get("method") == "turn/start":
            response(
                request,
                {
                    "turn": {
                        "id": "turn-1",
                        "status": "inProgress",
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": "hidden-item",
                                "text": "hidden",
                                "phase": "commentary",
                            }
                        ],
                        "error": None,
                    }
                },
            )
            continue
        if scenario == "divergent_terminal":
            first = {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "id": "item-1",
                        "text": "{}",
                        "phase": "final_answer",
                    }
                ],
                "error": None,
            }
            send(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": first},
                }
            )
            second = json.loads(json.dumps(first))
            second["items"][0]["text"] = '{"changed":true}'
            send(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": second},
                }
            )
            response(request, {"account": None, "requiresOpenaiAuth": True})
            continue
        if scenario == "missing_instruction_sources":
            response(
                request,
                thread_result(
                    "thread-1",
                    request_params=request["params"],
                    include_instruction_sources=False,
                ),
            )
            continue
        if scenario == "forbidden_instruction_sources":
            response(
                request,
                thread_result(
                    "thread-1",
                    request_params=request["params"],
                    instruction_sources=["/fixture/AGENTS.md"],
                ),
            )
            continue
        if scenario == "ephemeral_thread":
            response(
                request,
                thread_result(
                    "thread-1",
                    request_params=request["params"],
                    ephemeral=True,
                ),
            )
            continue
        if scenario in {
            "missing_authority",
            "profile_drift",
            "cwd_drift",
            "roots_drift",
            "sandbox_drift",
            "ephemeral_drift",
            "effort_drift",
            "service_tier_drift",
        } and request.get("method") in {
            "thread/start",
            "thread/resume",
            "thread/fork",
        }:
            params = request["params"]
            result = thread_result(
                (
                    "thread-2"
                    if request["method"] == "thread/fork"
                    else params.get("threadId", "thread-1")
                ),
                request_params=params,
                ephemeral=bool(params.get("ephemeral", False)),
            )
            if scenario == "missing_authority":
                result.pop("activePermissionProfile")
            elif scenario == "profile_drift":
                result["activePermissionProfile"]["id"] = "unsafe-profile"
            elif scenario == "cwd_drift":
                result["cwd"] = "/outside"
            elif scenario == "roots_drift":
                result["runtimeWorkspaceRoots"] = [params["cwd"], "/outside"]
            elif scenario == "sandbox_drift":
                result["sandbox"] = {
                    "type": "workspaceWrite",
                    "networkAccess": True,
                    "writableRoots": [params["cwd"], "/outside"],
                }
            elif scenario == "ephemeral_drift":
                result["thread"]["ephemeral"] = not bool(params.get("ephemeral", False))
            elif scenario == "effort_drift":
                result["reasoningEffort"] = "different"
            elif scenario == "service_tier_drift":
                result["serviceTier"] = "different"
            response(request, result)
            continue
        if (
            scenario == "thread_read_id_drift"
            and request.get("method") == "thread/read"
        ):
            response(
                request,
                {
                    "thread": {
                        "id": "wrong-thread",
                        "ephemeral": False,
                        "turns": [],
                    }
                },
            )
            continue
        # One completed turn seen three ways, as the product App Server really
        # renders it (observed against a live App Server): ``turn/completed``
        # streams the summary view, ``thread/read`` and ``thread/fork`` return
        # the full view with the user message restored and positional item ids.
        if scenario in {"fork_view_expansion", "fork_content_drift"} and request.get(
            "method"
        ) in {"turn/start", "thread/read", "thread/fork"}:
            summary_turn = {
                "id": "turn-1",
                "status": "completed",
                "itemsView": "summary",
                "startedAt": 1700000000,
                "completedAt": 1700000005,
                "durationMs": 5000,
                "items": [
                    {
                        "type": "agentMessage",
                        "id": "msg_fake_0001",
                        "text": '{"columns":[1,2,3,4,5]}',
                        "phase": "final_answer",
                        "memoryCitation": None,
                    }
                ],
                "error": None,
            }
            full_turn = json.loads(json.dumps(summary_turn))
            full_turn["itemsView"] = "full"
            full_turn["items"] = [
                {
                    "type": "userMessage",
                    "id": "item-1",
                    "clientId": None,
                    "content": [
                        {"type": "text", "text": "derive", "text_elements": []}
                    ],
                },
                {
                    "type": "agentMessage",
                    "id": "item-2",
                    "text": '{"columns":[1,2,3,4,5]}',
                    "phase": "final_answer",
                    "memoryCitation": None,
                },
            ]
            if request["method"] == "turn/start":
                response(
                    request,
                    {"turn": {"id": "turn-1", "status": "inProgress", "items": []}},
                )
                send(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-1", "turn": summary_turn},
                    }
                )
                continue
            if request["method"] == "thread/read":
                response(
                    request,
                    {
                        "thread": {
                            "id": request["params"]["threadId"],
                            "sessionId": "thread-1",
                            "ephemeral": False,
                            "turns": [full_turn],
                        }
                    },
                )
                continue
            copied = json.loads(json.dumps(full_turn))
            if scenario == "fork_content_drift":
                copied["items"][1]["text"] = '{"columns":[9,9,9,9,9]}'
            result = thread_result("thread-2", request_params=request["params"])
            result["thread"].update(
                {
                    "forkedFromId": request["params"]["threadId"],
                    "turns": [copied],
                }
            )
            response(request, result)
            continue
        if scenario == "stderr_bound":
            sys.stderr.write("x" * 8192 + "TAIL_MARKER")
            sys.stderr.flush()
            response(request, {"account": None, "requiresOpenaiAuth": True})
            continue
        handle_happy(request)

    sys.stderr.write("CLEAN_EOF\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
