"""The typeset layer exists before the service reports a run review-ready."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from derivation_api.models import CreateRunRequest
from fastapi.testclient import TestClient

from derivation_app.factory import create_fake_service, create_http_app
from derivation_app.route_typeset import (
    STATUS_FAILED,
    STATUS_REPAIRED,
    layer_path,
    typeset_content_sha256,
)
from derivation_app.tests.test_report_bundle import _run_request
from derivation_app.tests.test_route_typeset import RuleRunner
from derivation_runtime.fake import DeterministicFakeRuntime
from derivation_runtime.types import (
    CheckOutput,
    FormulaRepairOutput,
    JudgeOutput,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)

DERIVATION = r"The endpoint follows as \(x^2^3\) under the stated assumptions."


def _writer_output() -> WriterOutput:
    return WriterOutput(
        content=StepContent(
            claim="The deterministic endpoint is reached.",
            why="The fixture advances one segment without a provider.",
            source="Fixture source text without mathematics.",
            derivation=DERIVATION,
            scope="Integration verification only; this is not a scientific claim.",
        ),
        control=WriterControl(decision=WriterDecision.COMPLETE, alternatives=()),
        finish_reason="stop",
        usage=Usage({"input_tokens": 1, "output_tokens": 1}),
    )


def _runtime_factory(repair: bool):
    def factory(_config, _directory) -> DeterministicFakeRuntime:
        return DeterministicFakeRuntime(
            writer_outputs={("br_0001", 1): _writer_output()},
            check_factory=lambda _request: CheckOutput(
                verdict="ok",
                reason="The deterministic step satisfies the fixture.",
                evidence=(),
                finish_reason="stop",
                usage=Usage({"input_tokens": 1, "output_tokens": 1}),
            ),
            judge_factory=lambda _request: JudgeOutput(
                verdict="pass",
                reason="The deterministic candidate met its objective.",
                score=1.0,
                finish_reason="stop",
                usage=Usage({"input_tokens": 1, "output_tokens": 1}),
            ),
            **(
                {
                    "formula_repair_factory": lambda request: FormulaRepairOutput(
                        corrections=tuple(
                            (formula_id, r"{x^2}^3")
                            for formula_id in request.formula_ids
                        ),
                        finish_reason="stop",
                        usage=Usage({"input_tokens": 1, "output_tokens": 1}),
                        raw_output='{"corrections":[]}',
                    )
                }
                if repair
                else {}
            ),
        )

    return factory


def _service(tmp_path: Path, *, repair: bool = True, run_id: str = "run-typeset-svc"):
    service = create_fake_service(
        run_root=tmp_path / "active",
        storage_root=tmp_path,
        run_id_factory=lambda: run_id,
    )
    service.formula_validation_policy = "formula-v2"
    service.runtime_factory = _runtime_factory(repair)
    service.report_exporter.runner = RuleRunner([r"^2^3"])
    return service


def _command() -> CreateRunRequest:
    command = _run_request()
    command["config"]["record_version"] = "1.1"
    return CreateRunRequest.model_validate(command)


def test_the_layer_is_written_before_the_run_is_review_ready(tmp_path):
    run_id = "run-typeset-svc"
    service = _service(tmp_path)

    async def scenario() -> dict:
        await service.start()
        try:
            created = await service.create_run(_command(), idempotency_key=None)
            view = await service.wait_for_phase(
                created.id,
                {"review_ready", "review_ready_due_to_cap", "paused", "error"},
                timeout=30.0,
            )
            assert view.phase == "review_ready", view.error_message
            path = layer_path(tmp_path / "active" / run_id, "route_br_0001")
            # The consumer renders the candidate the moment it sees this phase.
            assert path.is_file()
            return json.loads(path.read_text(encoding="utf-8"))
        finally:
            await service.close()

    layer = asyncio.run(scenario())
    entry = next(
        item for item in layer["formulas"] if item["field"] == "derivation"
    )
    assert entry["status"] == STATUS_REPAIRED
    assert entry["typeset"] == r"{x^2}^3"
    assert entry["original"] == r"x^2^3"
    assert layer["run_id"] == run_id
    assert [item["kind"] for item in layer["calls"]] == ["formula_repair"]
    # The Record keeps the recorded formula: the layer is not a Record event.
    events = [
        json.loads(line)
        for line in (tmp_path / "active" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not any("typeset" in event["type"] for event in events)
    sealed = next(
        event for event in events if event["type"] == "step_revision_sealed"
    )
    assert sealed["payload"]["content"]["derivation"] == DERIVATION


def test_a_run_without_repair_support_still_reaches_review_ready(tmp_path):
    run_id = "run-typeset-svc"
    service = _service(tmp_path, repair=False)

    async def scenario() -> dict:
        await service.start()
        try:
            created = await service.create_run(_command(), idempotency_key=None)
            view = await service.wait_for_phase(
                created.id,
                {"review_ready", "review_ready_due_to_cap", "paused", "error"},
                timeout=30.0,
            )
            assert view.phase == "review_ready"
            return json.loads(
                layer_path(
                    tmp_path / "active" / run_id, "route_br_0001"
                ).read_text(encoding="utf-8")
            )
        finally:
            await service.close()

    layer = asyncio.run(scenario())
    entry = next(
        item for item in layer["formulas"] if item["field"] == "derivation"
    )
    assert entry["status"] == STATUS_FAILED
    assert entry["typeset"] == entry["original"]
    assert any(item["code"] == "formula_failed" for item in layer["flags"])


def test_a_restart_rebuilds_a_missing_layer_before_review_ready(tmp_path):
    run_id = "run-typeset-svc"
    service = _service(tmp_path)

    async def generate() -> None:
        await service.start()
        try:
            created = await service.create_run(_command(), idempotency_key=None)
            await service.wait_for_phase(
                created.id,
                {"review_ready", "review_ready_due_to_cap", "paused", "error"},
                timeout=30.0,
            )
        finally:
            await service.close()

    asyncio.run(generate())
    path = layer_path(tmp_path / "active" / run_id, "route_br_0001")
    first = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()

    recovered = _service(tmp_path)

    async def restart() -> dict:
        await recovered.start()
        try:
            context = recovered._runs[run_id]
            assert recovered._view(context).status == "running"
            assert context.driver_task is not None
            await context.driver_task
            view = await recovered.get_run(run_id)
            assert view.phase == "review_ready"
            assert path.is_file()
            return json.loads(path.read_text(encoding="utf-8"))
        finally:
            await recovered.close()

    second = asyncio.run(restart())
    assert [item["status"] for item in second["formulas"]] == [
        item["status"] for item in first["formulas"]
    ]
    # The second attempt writes its own compile evidence and is charged its
    # own repair call, without overwriting the first attempt's evidence.
    assert first["evidence_directory"] != second["evidence_directory"]
    journal = (
        tmp_path / "active" / run_id / "typeset" / "model_calls.jsonl"
    ).read_text(encoding="utf-8")
    assert journal.count("\n") == 2


TYPESET_DERIVATION = DERIVATION.replace(r"x^2^3", r"{x^2}^3")


async def _review_ready(service, run_id: str) -> None:
    created = await service.create_run(_command(), idempotency_key=None)
    view = await service.wait_for_phase(
        created.id,
        {"review_ready", "review_ready_due_to_cap", "paused", "error"},
        timeout=30.0,
    )
    assert created.id == run_id
    assert view.phase == "review_ready", view.error_message


def test_the_app_shows_the_typeset_formula_and_says_that_it_did(tmp_path):
    """A reader browsing this run sees the repaired formula, marked as such."""

    run_id = "run-typeset-svc"
    service = _service(tmp_path)

    async def scenario():
        await service.start()
        try:
            await _review_ready(service, run_id)
            shown = await service.get_run(run_id)
            sealed = await service.sealed_run(run_id)
            # The SSE payload a live reader receives is the same text as the
            # catalog read, or the desk would flip between the two.
            published = service._runs[run_id].events[-1].run
            return shown, sealed, published
        finally:
            await service.close()

    shown, sealed, published = asyncio.run(scenario())
    layer = json.loads(
        layer_path(tmp_path / "active" / run_id, "route_br_0001").read_text(
            encoding="utf-8"
        )
    )

    step = shown.steps[0]
    assert step.content is not None
    assert step.content.derivation == TYPESET_DERIVATION
    assert step.typeset is True
    # Title, summary and result are projections of the content, so they carry
    # the same repaired text rather than a second, broken copy of it.
    assert step.reasoning_summary == TYPESET_DERIVATION
    assert step.output == step.content.claim
    assert [
        (item.route_id, item.content_sha256, item.status)
        for item in shown.typeset_layers
    ] == [("route_br_0001", typeset_content_sha256(layer), layer["status"])]

    sealed_step = sealed.steps[0]
    assert sealed_step.content is not None
    assert sealed_step.content.derivation == DERIVATION
    assert sealed_step.typeset is False
    assert sealed.typeset_layers == []
    # The Record's own identity is untouched: the hash still names the sealed
    # content, and nothing but the math fragments differs.
    assert step.output_sha256 == sealed_step.output_sha256
    assert step.revision_id == sealed_step.revision_id
    assert step.content.claim == sealed_step.content.claim
    assert published.steps[0].content.derivation == TYPESET_DERIVATION
    assert published.steps[0].typeset is True


def test_the_http_api_serves_the_typeset_text_with_its_marker(tmp_path):
    run_id = "run-typeset-svc"
    service = _service(tmp_path)
    with TestClient(create_http_app(service)) as client:
        command = _command().model_dump(mode="json", by_alias=True)
        assert client.post("/api/runs", json=command).status_code == 201
        for _ in range(3000):
            view = client.get(f"/api/runs/{run_id}").json()
            if view["phase"] not in {"submitted", "autonomous_exploration"}:
                break
            time.sleep(0.01)
        assert view["phase"] == "review_ready", view.get("errorMessage")
        step = view["steps"][0]
        assert step["content"]["derivation"] == TYPESET_DERIVATION
        assert step["typeset"] is True
        assert [item["route_id"] for item in view["typeset_layers"]] == [
            "route_br_0001"
        ]


def test_an_absent_or_mismatched_layer_serves_the_sealed_text_byte_for_byte(
    tmp_path,
):
    run_id = "run-typeset-svc"
    service = _service(tmp_path)

    async def scenario():
        await service.start()
        try:
            await _review_ready(service, run_id)
            path = layer_path(tmp_path / "active" / run_id, "route_br_0001")
            sealed = (await service.sealed_run(run_id)).model_dump(
                mode="json", by_alias=True
            )
            # A layer that cites a Record this run no longer has is not a
            # partial result; it is ignored entirely.
            layer = json.loads(path.read_text(encoding="utf-8"))
            layer["steps"][0]["output_sha256"] = "0" * 64
            path.write_text(json.dumps(layer), encoding="utf-8")
            mismatched = (await service.get_run(run_id)).model_dump(
                mode="json", by_alias=True
            )
            path.unlink()
            absent = (await service.get_run(run_id)).model_dump(
                mode="json", by_alias=True
            )
            return sealed, mismatched, absent
        finally:
            await service.close()

    sealed, mismatched, absent = asyncio.run(scenario())
    assert mismatched == sealed
    assert absent == sealed
    assert sealed["steps"][0]["content"]["derivation"] == DERIVATION
    assert sealed["steps"][0]["typeset"] is False
    assert sealed["typeset_layers"] == []


def test_a_layer_that_repaired_nothing_leaves_every_step_unmarked(tmp_path):
    """The marker means substituted text, not merely "a layer exists"."""

    run_id = "run-typeset-svc"
    service = _service(tmp_path, repair=False)

    async def scenario():
        await service.start()
        try:
            await _review_ready(service, run_id)
            shown = await service.get_run(run_id)
            sealed = await service.sealed_run(run_id)
            return shown, sealed
        finally:
            await service.close()

    shown, sealed = asyncio.run(scenario())
    assert shown.steps[0].content is not None
    assert shown.steps[0].content.derivation == DERIVATION
    assert shown.steps[0].typeset is False
    assert [item.route_id for item in shown.typeset_layers] == ["route_br_0001"]
    assert (
        shown.model_dump(mode="json", by_alias=True, exclude={"typeset_layers"})
        == sealed.model_dump(mode="json", by_alias=True, exclude={"typeset_layers"})
    )
