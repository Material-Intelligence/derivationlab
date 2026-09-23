from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from derivation_api.fake_service import FakeDerivationService
from derivation_api.models import CreateRunRequest, RunEvent, RuntimeOverlay, RunView
from derivation_api.sse import stream_sse

from .conftest import run_request

JS_SAFE_EVENT_ID = 9_007_199_254_740_991


def parse_messages(body: str) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    for frame in body.split("\n\n"):
        lines = frame.splitlines()
        identifier = next((line.removeprefix("id: ") for line in lines if line.startswith("id: ")), None)
        data = next((line.removeprefix("data: ") for line in lines if line.startswith("data: ")), None)
        if identifier is not None and data is not None:
            messages.append({"id": int(identifier), "data": json.loads(data)})
    return messages


def test_sse_replay_has_ids_retry_and_frontend_compatible_data(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    response = client.get(f"/api/runs/{run['id']}/events?follow=false")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.text.startswith("retry: 2000\n\n")
    messages = parse_messages(response.text)
    assert [message["id"] for message in messages] == [1, 2, 3, 4]
    assert [message["data"]["type"] for message in messages] == [
        "run.created",
        "run.updated",
        "step.sealed",
        "run.updated",
    ]
    assert all(message["data"]["event_id"] == message["id"] for message in messages)
    assert messages[-1]["data"]["run"]["rootStepId"] == "step-run-0001-001"
    assert messages[1]["data"]["overlay"]["activeCalls"][0]["callId"] == "call-run-0001-pending"
    assert messages[-1]["data"]["overlay"] == {
        "hard_interrupt_requested": False,
        "activeCalls": [],
    }


def test_last_event_id_reconnect_is_exclusive_and_takes_precedence(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    response = client.get(
        f"/api/runs/{run['id']}/events?after=0&follow=false",
        headers={"Last-Event-ID": "2"},
    )
    assert [message["id"] for message in parse_messages(response.text)] == [3, 4]


def test_invalid_last_event_id_is_structured_error(client: TestClient) -> None:
    run = client.post("/api/runs", json=run_request()).json()
    for invalid_cursor in ["not-a-number", "0" * 20, str(9_223_372_036_854_775_808)]:
        response = client.get(
            f"/api/runs/{run['id']}/events?follow=false",
            headers={"Last-Event-ID": invalid_cursor},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_event_cursor"


def test_event_id_boundary_is_safe_for_javascript_number_clients(client: TestClient) -> None:
    async def canonical_run() -> RunView:
        service = FakeDerivationService()
        await service.start()
        try:
            return await service.create_run(
                CreateRunRequest.model_validate(run_request()),
                idempotency_key=None,
            )
        finally:
            await service.close()

    canonical = asyncio.run(canonical_run())
    canonical_data = canonical.model_dump()
    run_at_boundary = {**canonical_data, "canonical_event_id": JS_SAFE_EVENT_ID}
    assert RunView.model_validate(run_at_boundary).canonical_event_id == JS_SAFE_EVENT_ID
    with pytest.raises(ValidationError):
        RunView.model_validate({**canonical_data, "canonical_event_id": JS_SAFE_EVENT_ID + 1})

    event = example_event().model_dump()
    event["event_id"] = JS_SAFE_EVENT_ID
    assert RunEvent.model_validate(event).event_id == JS_SAFE_EVENT_ID
    event["event_id"] = JS_SAFE_EVENT_ID + 1
    with pytest.raises(ValidationError):
        RunEvent.model_validate(event)

    run = client.post("/api/runs", json=run_request()).json()
    query_boundary = client.get(f"/api/runs/{run['id']}/events?after={JS_SAFE_EVENT_ID}&follow=false")
    assert query_boundary.status_code == 200
    query_unsafe = client.get(f"/api/runs/{run['id']}/events?after={JS_SAFE_EVENT_ID + 1}&follow=false")
    assert query_unsafe.status_code == 422
    assert query_unsafe.json()["error"]["code"] == "validation_failed"

    header_boundary = client.get(
        f"/api/runs/{run['id']}/events?follow=false",
        headers={"Last-Event-ID": str(JS_SAFE_EVENT_ID)},
    )
    assert header_boundary.status_code == 200
    header_unsafe = client.get(
        f"/api/runs/{run['id']}/events?follow=false",
        headers={"Last-Event-ID": str(JS_SAFE_EVENT_ID + 1)},
    )
    assert header_unsafe.status_code == 400
    assert header_unsafe.json()["error"]["code"] == "invalid_event_cursor"


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def example_event() -> RunEvent:
    return RunEvent(
        event_id=1,
        type="run.updated",
        run_id="run-test",
        occurred_at="2026-08-29T16:00:00Z",
        run=None,
        overlay=RuntimeOverlay(hard_interrupt_requested=False, active_calls=[]),
    )


def test_heartbeat_does_not_cancel_pending_event_and_close_releases_source() -> None:
    closed = False

    async def source() -> AsyncIterator[RunEvent]:
        nonlocal closed
        try:
            await asyncio.sleep(0.03)
            yield example_event()
        finally:
            closed = True

    async def scenario() -> None:
        stream = stream_sse(
            ConnectedRequest(),
            source(),
            heartbeat_seconds=0.005,
            retry_milliseconds=200,
        )
        assert await anext(stream) == "retry: 200\n\n"
        assert await anext(stream) == ": heartbeat\n\n"
        chunks: list[str] = []
        while not chunks or not chunks[-1].startswith("id: 1"):
            chunks.append(await anext(stream))
        assert chunks[-1].startswith("id: 1\ndata: ")
        await stream.aclose()

    asyncio.run(scenario())
    assert closed is True


def test_revoked_session_stops_stream_and_releases_pending_source() -> None:
    closed = False
    active = True

    async def source() -> AsyncIterator[RunEvent]:
        nonlocal closed
        try:
            await asyncio.sleep(10)
            yield example_event()
        finally:
            closed = True

    async def session_is_active() -> bool:
        return active

    async def scenario() -> None:
        nonlocal active
        stream = stream_sse(
            ConnectedRequest(),
            source(),
            heartbeat_seconds=0.005,
            retry_milliseconds=200,
            authorization_check=session_is_active,
        )
        assert await anext(stream) == "retry: 200\n\n"
        assert await anext(stream) == ": heartbeat\n\n"
        active = False
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(scenario())
    assert closed is True


def test_bounded_fake_subscription_drops_slow_client_for_reconnect() -> None:
    async def scenario() -> None:
        service = FakeDerivationService(subscriber_queue_size=1)
        await service.start()
        run = await service.create_run(
            CreateRunRequest.model_validate(run_request()),
            idempotency_key=None,
        )
        source = service.stream_events(run.id, after_event_id=0, follow=True)
        for _ in range(4):
            await anext(source)
        waiting = asyncio.create_task(anext(source))
        await asyncio.sleep(0)
        await service.publish_test_event(run.id)
        await asyncio.sleep(0)
        first_live = await waiting
        assert first_live.event_id == 5
        await service.publish_test_event(run.id)
        await service.publish_test_event(run.id)
        try:
            await anext(source)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("slow subscriber must be closed and reconnect from Last-Event-ID")
        assert await service.subscriber_count(run.id) == 0
        await service.close()

    asyncio.run(scenario())
