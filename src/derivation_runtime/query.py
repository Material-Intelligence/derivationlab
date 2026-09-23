"""Provider-free replay and tree/route queries for API and UI adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from derivation_agent_record import load_events, replay_events

from .types import RunPhase, RuntimeInvariantError

TERMINAL_BRANCH_STATUSES = {"completed", "killed", "parked"}


@dataclass(frozen=True)
class DerivedRunStatus:
    phase: RunPhase
    stop_reason: str | None
    active_branch_ids: tuple[str, ...]
    paused_branch_ids: tuple[str, ...]


@dataclass(frozen=True)
class Route:
    branch_id: str
    step_revision_ids: tuple[str, ...]
    status: str
    created_seq: int


class ReplayQuery:
    """Read-only domain view built solely from a verified Record V1 log."""

    def __init__(self, canonical: Mapping[str, Any]) -> None:
        self.canonical = dict(canonical)
        self._branches = {
            item["branch_id"]: item for item in self.canonical["branches"]
        }
        self._steps = {
            item["step_revision_id"]: item for item in self.canonical["step_revisions"]
        }

    @classmethod
    def from_event_log(cls, path: str | Path) -> ReplayQuery:
        return cls(replay_events(load_events(path)).canonical)

    def status(self) -> DerivedRunStatus:
        active = tuple(
            item["branch_id"]
            for item in self.canonical["branches"]
            if item["status"] == "active"
        )
        paused = tuple(
            item["branch_id"]
            for item in self.canonical["branches"]
            if item["status"] == "paused"
        )
        call_count = self.canonical["summary"]["model_call_count"]
        call_cap = self.canonical["run"]["configuration"]["max_model_calls"]
        judged_candidate_ids = {
            item["candidate_id"]
            for item in self.canonical["judgements"]
            if item["state"] == "completed"
        }
        eligible_without_judgement = self.canonical[
            "schema_version"
        ] == "derivation-agent-canonical-v1" and any(
            item["status"] == "eligible"
            and item["candidate_id"] not in judged_candidate_ids
            for item in self.canonical["candidates"]
        )
        if (
            call_cap is not None
            and call_count >= call_cap
            and (active or eligible_without_judgement)
        ):
            return DerivedRunStatus(
                phase=RunPhase.REVIEW_READY_DUE_TO_CAP,
                stop_reason="max_model_calls",
                active_branch_ids=active,
                paused_branch_ids=paused,
            )
        if active:
            return DerivedRunStatus(
                phase=RunPhase.AUTONOMOUS_EXPLORATION,
                stop_reason=None,
                active_branch_ids=active,
                paused_branch_ids=paused,
            )
        if paused:
            runtime_failure = any(
                item["status"] == "paused"
                and item["status_history"][-1]["reason"] == "runtime_failure"
                for item in self.canonical["branches"]
            )
            return DerivedRunStatus(
                phase=RunPhase.PAUSED,
                stop_reason="runtime_failure" if runtime_failure else "soft_pause",
                active_branch_ids=active,
                paused_branch_ids=paused,
            )
        if any(
            item["status"] == "parked"
            and item["status_history"][-1]["reason"] == "writer_blocked"
            for item in self.canonical["branches"]
        ):
            return DerivedRunStatus(
                phase=RunPhase.PAUSED,
                stop_reason="model_blocked",
                active_branch_ids=active,
                paused_branch_ids=paused,
            )
        return DerivedRunStatus(
            phase=RunPhase.REVIEW_READY,
            stop_reason=None,
            active_branch_ids=active,
            paused_branch_ids=paused,
        )

    def routes(self) -> tuple[Route, ...]:
        candidates = [
            branch
            for branch in self._branches.values()
            if branch["status"] in TERMINAL_BRANCH_STATUSES
            or not any(
                child["parent_branch_id"] == branch["branch_id"]
                for child in self._branches.values()
            )
        ]
        routes = [
            Route(
                branch_id=branch["branch_id"],
                step_revision_ids=tuple(branch["step_revision_ids"]),
                status=branch["status"],
                created_seq=int(branch["status_history"][0]["seq"]),
            )
            for branch in candidates
        ]
        return tuple(
            sorted(routes, key=lambda item: (item.created_seq, item.branch_id))
        )

    def route_for_node(self, step_revision_id: str) -> Route:
        if step_revision_id not in self._steps:
            raise KeyError(step_revision_id)
        matches = [
            route
            for route in self.routes()
            if step_revision_id in route.step_revision_ids
        ]
        if not matches:
            raise RuntimeInvariantError(
                f"sealed node {step_revision_id} belongs to no visible route"
            )
        return matches[0]

    def route_for_edge(
        self, from_step_revision_id: str, to_step_revision_id: str
    ) -> Route:
        matches: list[Route] = []
        for route in self.routes():
            pairs = tuple(zip(route.step_revision_ids, route.step_revision_ids[1:]))
            if (from_step_revision_id, to_step_revision_id) in pairs:
                matches.append(route)
        if not matches:
            raise KeyError((from_step_revision_id, to_step_revision_id))
        return matches[0]

    def tree(self) -> dict[str, Any]:
        return {
            "run_id": self.canonical["run"]["run_id"],
            "status": {
                "phase": self.status().phase.value,
                "stop_reason": self.status().stop_reason,
            },
            "branches": [
                {
                    "branch_id": item["branch_id"],
                    "parent_branch_id": item["parent_branch_id"],
                    "anchor_step_revision_id": item["anchor_step_revision_id"],
                    "status": item["status"],
                    "step_revision_ids": list(item["step_revision_ids"]),
                    "provenance": item["provenance"],
                }
                for item in sorted(
                    self._branches.values(),
                    key=lambda branch: (
                        int(branch["status_history"][0]["seq"]),
                        branch["branch_id"],
                    ),
                )
            ],
            "steps": [
                {
                    "step_revision_id": item["step_revision_id"],
                    "branch_id": item["branch_id"],
                    "step_slot": item["step_slot"],
                    "content": item["content"],
                    "output_sha256": item["output_sha256"],
                }
                for item in sorted(
                    self._steps.values(),
                    key=lambda step: (
                        step["sealed_event_id"],
                        step["step_revision_id"],
                    ),
                )
            ],
            "routes": [
                {
                    "branch_id": route.branch_id,
                    "step_revision_ids": list(route.step_revision_ids),
                    "status": route.status,
                }
                for route in self.routes()
            ],
        }


__all__ = ["TERMINAL_BRANCH_STATUSES", "DerivedRunStatus", "ReplayQuery", "Route"]
