"""Project strict Record V1 snapshots into the local HTTP/UI view models."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from derivation_api.models import (
    BranchView,
    BudgetView,
    EdgeView,
    FrozenRunConfig,
    RouteView,
    RunStatus,
    RuntimeConfig,
    RunView,
    StatusHistoryItem,
    StepChecks,
    StepProvenance,
    StepView,
    TypesetLayerView,
)
from derivation_api.models import (
    RunPhase as ApiRunPhase,
)
from derivation_api.models import (
    StepContent as ApiStepContent,
)
from pydantic import ValidationError

from derivation_runtime.control import CallBookmark, StepBookmark
from derivation_runtime.query import ReplayQuery

from .route_typeset import (
    typeset_content_sha256,
    typeset_field_text,
    verified_layers,
)

BRANCH_KINDS = {
    "root": "root",
    "model_alternative": "model_fork",
    "human_direction": "human_direction",
    "human_revision": "human_revision",
    "model_revision": "model_revision",
    "instrument_retry": "instrument_retry",
}
EDGE_KINDS = {
    "root": "continuation",
    "model_alternative": "model_fork",
    "human_direction": "human_direction",
    "human_revision": "human_revision",
    "model_revision": "model_revision",
    # The API keeps instrument_retry as branch provenance, while its visual
    # edge uses the closest allowed non-human fork kind.
    "instrument_retry": "model_fork",
}


def _short_title(text: str) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= 120 else f"{normalized[:117]}..."


def _run_status(phase: str) -> RunStatus:
    if phase in {"review_ready", "review_ready_due_to_cap"}:
        return "review_ready"
    if phase == "paused":
        return "paused"
    if phase == "interrupted":
        return "interrupted"
    if phase == "error":
        return "error"
    return "running"


def _route_status(branch_status: str) -> str:
    if branch_status == "completed":
        return "complete"
    if branch_status in {"killed", "parked"}:
        return "failed"
    return "active"


def _checks_for_step(canonical: Mapping[str, Any], step_revision_id: str) -> StepChecks:
    checks = [
        item
        for item in canonical["checks"]
        if item["target_step_revision_id"] == step_revision_id
    ]
    completed = [item for item in checks if item["state"] == "completed"]
    if not canonical["run"]["configuration"].get("checker_enabled", True):
        physics = "not_requested"
    elif not completed:
        physics = "pending"
    elif all(item["verdict"] == "ok" for item in completed):
        physics = "passed"
    else:
        physics = "failed"
    return StepChecks(
        schema="passed",
        physics=physics,
        provenance="passed",
    )


def _model_call_for_step(
    canonical: Mapping[str, Any], step: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    call_id = step["origin"].get("model_call_id")
    if call_id is None:
        return None
    return next(
        (item for item in canonical["model_calls"] if item["model_call_id"] == call_id),
        None,
    )


def _step_provenance(
    canonical: Mapping[str, Any],
    step: Mapping[str, Any],
    bookmark: StepBookmark | None,
    call_bookmark: CallBookmark | None,
    sealed_at: str,
) -> StepProvenance | None:
    call = _model_call_for_step(canonical, step)
    if call is None:
        return None
    provider_session_id = (
        bookmark.provider_session_id
        if bookmark is not None
        else call_bookmark.provider_session_id
        if call_bookmark is not None
        else None
    )
    provider_operation_id = (
        bookmark.provider_operation_id
        if bookmark is not None
        else call_bookmark.provider_operation_id
        if call_bookmark is not None
        else None
    )
    if provider_session_id is None or provider_operation_id is None:
        return None
    return StepProvenance(
        model=call["model"],
        thread_id=provider_session_id,
        turn_id=provider_operation_id,
        created_at=sealed_at,
    )


def _edge_kind(
    branch: Mapping[str, Any],
    step: Mapping[str, Any],
    steps_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    if step["branch_id"] != branch["branch_id"]:
        return "continuation"
    if step["step_revision_id"] in branch["inherited_step_revision_ids"]:
        return "continuation"
    local_steps = [
        step_id
        for step_id in branch["step_revision_ids"]
        if steps_by_id[step_id]["branch_id"] == branch["branch_id"]
    ]
    if (
        branch["created_reason"] != "root"
        and local_steps
        and step["step_revision_id"] == local_steps[0]
    ):
        return EDGE_KINDS[branch["created_reason"]]
    return "continuation"


def build_run_view(
    *,
    canonical: Mapping[str, Any],
    question: str,
    api_config: FrozenRunConfig,
    runtime_config: RuntimeConfig,
    record_events: Sequence[Mapping[str, Any]],
    phase: str | None,
    pause_requested: bool,
    hard_interrupt_requested: bool,
    error_message: str | None,
    step_bookmark: Callable[[str], StepBookmark | None],
    call_bookmark: Callable[[str], CallBookmark | None],
) -> RunView:
    """Build a UI view only from a strict replay plus disposable handles.

    Scientific content, branch topology, hashes, and provenance classes all
    come from ``canonical``.  Provider handles are optional decoration; losing
    ``control.sqlite`` never changes the scientific projection.
    """

    query = ReplayQuery(canonical)
    selected_phase = phase or query.status().phase.value
    event_count = int(canonical["event_log"]["event_count"])
    event_by_id = {item["event_id"]: item for item in record_events[:event_count]}
    branches_by_id = {item["branch_id"]: item for item in canonical["branches"]}
    steps_by_id = {
        item["step_revision_id"]: item for item in canonical["step_revisions"]
    }

    ordered_steps = sorted(
        canonical["step_revisions"],
        key=lambda item: (
            int(event_by_id[item["sealed_event_id"]]["seq"]),
            item["step_revision_id"],
        ),
    )
    steps: list[StepView] = []
    for order, step in enumerate(ordered_steps):
        branch = branches_by_id[step["branch_id"]]
        content = ApiStepContent.model_validate(step["content"])
        sealed_at = event_by_id[step["sealed_event_id"]]["recorded_at"]
        steps.append(
            StepView(
                id=step["step_revision_id"],
                revision_id=step["step_revision_id"],
                branch_id=step["branch_id"],
                order=order,
                title=_short_title(content.claim),
                status="sealed",
                content=content,
                output_sha256=step["output_sha256"],
                input=branch["hypothesis"]["text"]
                if step["step_slot"] == 1
                else question,
                reasoning_summary=content.derivation,
                output=content.claim,
                checks=_checks_for_step(canonical, step["step_revision_id"]),
                provenance=_step_provenance(
                    canonical,
                    step,
                    step_bookmark(step["step_revision_id"]),
                    call_bookmark(step["origin"].get("model_call_id"))
                    if step["origin"].get("model_call_id") is not None
                    else None,
                    sealed_at,
                ),
            )
        )

    ordered_branches = sorted(
        canonical["branches"],
        key=lambda item: (int(item["status_history"][0]["seq"]), item["branch_id"]),
    )
    branches: list[BranchView] = []
    for branch in ordered_branches:
        try:
            kind = BRANCH_KINDS[branch["created_reason"]]
        except KeyError as exc:
            raise ValueError(
                f"unsupported Record V1 branch reason: {branch['created_reason']!r}"
            ) from exc
        branches.append(
            BranchView(
                branch_id=branch["branch_id"],
                parent_branch_id=branch["parent_branch_id"],
                anchor_step_revision_id=branch["anchor_step_revision_id"],
                kind=cast(Any, kind),
                status=branch["status"],
                status_history=[
                    StatusHistoryItem(seq=item["seq"], status=item["to"])
                    for item in branch["status_history"]
                ],
                step_revision_ids=list(branch["step_revision_ids"]),
            )
        )

    edges: list[EdgeView] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for branch in ordered_branches:
        step_ids = branch["step_revision_ids"]
        for from_id, to_id in pairwise(step_ids):
            kind = _edge_kind(branch, steps_by_id[to_id], steps_by_id)
            key = (from_id, to_id, kind)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                EdgeView(
                    id=f"edge_{len(edges) + 1:04d}",
                    from_step_id=from_id,
                    to_step_id=to_id,
                    order=len(edges),
                    kind=cast(Any, kind),
                )
            )

    routes: list[RouteView] = []
    branch_views = {item.branch_id: item for item in branches}
    for index, route in enumerate(query.routes(), start=1):
        branch_view = branch_views[route.branch_id]
        routes.append(
            RouteView(
                id=f"route_{route.branch_id}",
                label=f"Route {index}",
                node_ids=list(route.step_revision_ids),
                status=cast(Any, _route_status(route.status)),
                branch_id=route.branch_id,
                status_history=[
                    item.model_copy(deep=True) for item in branch_view.status_history
                ],
            )
        )

    root_branch = next(
        item for item in ordered_branches if item["parent_branch_id"] is None
    )
    root_step_id = (
        root_branch["step_revision_ids"][0]
        if root_branch["step_revision_ids"]
        else None
    )
    updated_at = (
        record_events[event_count - 1]["recorded_at"]
        if event_count
        else canonical["run"]["created_at"]
    )
    if (
        error_message is None
        and selected_phase == "paused"
        and any(
            branch["status_history"][-1]["reason"] == "runtime_failure"
            for branch in canonical["branches"]
        )
    ):
        failed = [call for call in canonical["model_calls"] if call.get("failure")]
        if failed:
            error_message = failed[-1]["failure"]["message"]
    return RunView(
        id=canonical["run"]["run_id"],
        question=question,
        status=_run_status(selected_phase),
        phase=cast(ApiRunPhase, selected_phase),
        config=api_config.model_copy(deep=True),
        runtime=runtime_config.model_copy(deep=True),
        root_step_id=root_step_id,
        steps=steps,
        edges=edges,
        branches=branches,
        routes=routes,
        budget=BudgetView(
            used_steps=len(steps),
            max_steps=canonical["run"]["configuration"]["max_model_calls"],
        ),
        canonical_event_id=event_count,
        pause_requested=pause_requested,
        hard_interrupt_requested=hard_interrupt_requested,
        created_at=canonical["run"]["created_at"],
        updated_at=updated_at,
        error_message=error_message,
    )


_CONTENT_FIELDS = ("claim", "why", "source", "derivation", "scope")


def run_view_routes(view: RunView) -> dict[str, list[dict[str, Any]]]:
    """The routes of one view as the step records a typeset layer is bound to.

    A route names its steps by node id, which is the step's own id, not its
    revision id; a layer is bound to the revision id and that revision's
    ``output_sha256``. A route with a node this view does not carry is left
    out rather than half-described.
    """

    steps = {step.id: step for step in view.steps}
    routes: dict[str, list[dict[str, Any]]] = {}
    for route in view.routes:
        if any(node_id not in steps for node_id in route.node_ids):
            continue
        routes[route.id] = [
            {
                "step_revision_id": steps[node_id].revision_id,
                "output_sha256": steps[node_id].output_sha256,
                "content": steps[node_id].content.model_dump(mode="json"),
            }
            for node_id in route.node_ids
            if steps[node_id].content is not None
        ]
    return routes


def run_view_typeset_layers(
    run_directory: Path, view: RunView
) -> dict[str, dict[str, Any]] | None:
    """Verified typeset layers for the routes of one run view.

    ``None`` when the run has none a consumer would accept, including when the
    layer files cannot be read at all: a reader that finds nothing renders
    exactly what it rendered before typesetting existed.
    """

    try:
        found = verified_layers(
            Path(run_directory), run_id=view.id, routes=run_view_routes(view)
        )
    except (KeyError, OSError, ValueError):
        return None
    return found or None


def typeset_run_view(view: RunView, layers: Mapping[str, Mapping[str, Any]]) -> RunView:
    """Show each route's verified typeset math instead of the sealed fragment.

    Only the math fragments a layer repaired or expanded change; prose, ids,
    and every hash stay what the Record sealed. In particular
    ``StepView.output_sha256`` keeps identifying the recorded content, and the
    steps whose text was substituted carry ``typeset=True`` so a client never
    has to guess whether it is looking at the sealed text.

    A step that sits on more than one route takes its text from the first
    route of this view that carries a layer for it, so the same Record and the
    same layer files always produce the same view.
    """

    if not layers:
        return view
    identities: list[TypesetLayerView] = []
    by_revision: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for route in view.routes:
        layer = layers.get(route.id)
        if layer is None:
            continue
        identities.append(
            TypesetLayerView(
                route_id=route.id,
                content_sha256=typeset_content_sha256(layer),
                status=str(layer.get("status") or "unknown"),
            )
        )
        grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
        for entry in layer.get("formulas", ()):
            fields = grouped.setdefault(entry["step_revision_id"], {})
            fields.setdefault(entry["field"], []).append(entry)
        for revision_id, fields in grouped.items():
            by_revision.setdefault(revision_id, fields)
    if not identities:
        return view

    steps: list[StepView] = []
    for step in view.steps:
        fields = by_revision.get(step.revision_id)
        if fields is None or step.content is None:
            steps.append(step)
            continue
        recorded = step.content
        values = {
            name: typeset_field_text(getattr(recorded, name), fields.get(name, ()))
            for name in _CONTENT_FIELDS
        }
        if all(values[name] == getattr(recorded, name) for name in _CONTENT_FIELDS):
            steps.append(step)
            continue
        try:
            content = ApiStepContent.model_validate(values)
        except ValidationError:
            # A layer that cannot be projected into the API contract is not
            # allowed to cost a reader the recorded step.
            steps.append(step)
            continue
        steps.append(
            step.model_copy(
                update={
                    "content": content,
                    # Title, summary and output are projections of the content;
                    # leaving them on the sealed text would show the same
                    # formula twice, once repaired and once broken.
                    "title": _short_title(content.claim),
                    "reasoning_summary": content.derivation,
                    "output": content.claim,
                    "typeset": True,
                }
            )
        )
    return view.model_copy(update={"steps": steps, "typeset_layers": identities})


def displayed_run_view(run_directory: Path, view: RunView) -> RunView:
    """The view the product shows: typeset math where a layer verifies.

    Absent or mismatched layers return ``view`` itself, so a run without
    typesetting is served byte for byte as before.
    """

    layers = run_view_typeset_layers(run_directory, view)
    if not layers:
        return view
    return typeset_run_view(view, layers)


__all__ = [
    "build_run_view",
    "displayed_run_view",
    "run_view_routes",
    "run_view_typeset_layers",
    "typeset_run_view",
]
