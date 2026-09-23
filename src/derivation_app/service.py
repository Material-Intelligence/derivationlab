"""Concrete HTTP service adapter over the provider-neutral derivation runtime."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import stat
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from derivation_api.models import (
    AccountRateLimitsView,
    AccountRateLimitWindowView,
    AccountView,
    ActiveCallOverlay,
    BuildInfoView,
    CapabilitiesView,
    CreateBranchRequest,
    CreateIntakeSessionRequest,
    CreateRunDefaultsView,
    CreateRunRequest,
    DeviceLoginCancelView,
    DeviceLoginStartView,
    DeviceLoginStatusView,
    ExportReportRequest,
    FinalizeIntakeSessionRequest,
    FrozenProblemInput,
    HealthView,
    ImportExistingAccountRequest,
    IntakeRevisionRequest,
    IntakeSessionStatusValue,
    IntakeSessionView,
    ProblemPresetsView,
    QuitReadinessView,
    ReportBundleView,
    RunCommandCapabilities,
    RunEvent,
    RunSummary,
    RuntimeOverlay,
    RunView,
    SubmitIntakeRoundRequest,
)
from derivation_api.models import (
    RunPhase as ApiRunPhase,
)
from derivation_api.service import DerivationServiceError, ErrorKind

from derivation_agent_record import (
    ContractError,
    canonical_json,
    load_events,
    replay_events,
    sha256_bytes,
    sha256_text,
)
from derivation_runtime.capabilities import (
    CapabilityProfile,
    resolve_capability_profile,
)
from derivation_runtime.control import ControlStore
from derivation_runtime.orchestrator import DerivationOrchestrator
from derivation_runtime.query import ReplayQuery
from derivation_runtime.record import EventLogWriter, RecordV1Writer, utc_now
from derivation_runtime.types import (
    ArtifactRef,
    ContentRef,
    FormulaValidationResult,
    InputPolicy,
    ModelRuntime,
    ModelSpec,
    ProviderLineage,
    RunConfig,
    RunPhase,
    RuntimeInvariantError,
    RuntimeSession,
    StepContent,
)

from .intake_session import (
    AnswerStrategy,
    DecisionAnswer,
    DecisionStatus,
    IntakeCommandRejected,
    IntakeIdempotencyConflict,
    IntakeSession,
    IntakeSessionNotFound,
    IntakeStaleRevision,
    PendingSubmission,
    intake_session_payload,
)
from .intake_session_service import PersistentIntakeSessionService
from .model_catalog import (
    ModelCatalog,
    ModelOption,
    product_default_selection,
    validate_model_effort,
    validate_model_service_tier,
)
from .product_profile import ProfileLockHeld
from .projection import (
    build_run_view,
    displayed_run_view,
    run_view_typeset_layers,
)
from .reporting import ReportExporter, ReportSource
from .route_typeset import (
    pending_typeset_routes,
    typeset_completed_routes,
)

logger = logging.getLogger(__name__)

PHASES: list[ApiRunPhase] = [
    "submitted",
    "autonomous_exploration",
    "human_expansion",
    "paused",
    "recovering",
    "review_ready",
    "review_ready_due_to_cap",
    "interrupted",
    "error",
]
MANIFEST_SCHEMA = "derivation-app-run-manifest-v2"
SSE_CURSOR_SCHEMA = "derivation-app-sse-cursor-v1"
IDEMPOTENCY_SCHEMA = "derivation-app-idempotency-v1"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RuntimeFactory = Callable[
    [RunConfig, Path],
    ModelRuntime | Awaitable[ModelRuntime],
]
RunIdFactory = Callable[[], str]


class AccountService(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def account(self) -> AccountView: ...

    async def rate_limits(self) -> AccountRateLimitsView: ...

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView: ...

    async def start_device_login(self) -> DeviceLoginStartView: ...

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView: ...

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView: ...


class UnavailableAccountService:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def account(self) -> AccountView:
        return AccountView(
            status="unavailable",
            credential_store="file",
            import_available=False,
            diagnostic="account_check_failed",
        )

    async def rate_limits(self) -> AccountRateLimitsView:
        return AccountRateLimitsView(
            status="temporarily_unavailable",
            windows=[],
            diagnostic="rate_limits_unavailable",
        )

    @staticmethod
    def _unavailable() -> DerivationServiceError:
        return DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "account_service_unavailable",
            "Account management is unavailable in this service.",
        )

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView:
        del command, idempotency_key
        raise self._unavailable()

    async def start_device_login(self) -> DeviceLoginStartView:
        raise self._unavailable()

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView:
        del login_id
        raise self._unavailable()

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView:
        del login_id
        raise self._unavailable()


class DeterministicAccountService(UnavailableAccountService):
    """Fake-only signed-in account state; it never reads a credential file."""

    async def account(self) -> AccountView:
        return AccountView(
            status="signed_in",
            credential_store="file",
            import_available=False,
            diagnostic="ready",
        )

    async def rate_limits(self) -> AccountRateLimitsView:
        return AccountRateLimitsView(
            status="available",
            plan_type="plus",
            windows=[
                AccountRateLimitWindowView(
                    kind="weekly",
                    used_percent=31,
                    remaining_percent=69,
                    window_duration_mins=10_080,
                    resets_at="2026-09-07T19:21:00Z",
                )
            ],
            observed_at="2026-09-04T16:00:00Z",
            diagnostic="ready",
        )


class ObservedEventLogWriter(EventLogWriter):
    """EventLogWriter with a synchronous post-append observer.

    The superclass remains the sole durable/hash-chain writer.  Observation is
    downstream only and cannot change the event before it is validated/fsynced.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        super().__init__(path, run_id)
        self._observer: Callable[[Mapping[str, Any]], None] | None = None

    def set_observer(self, observer: Callable[[Mapping[str, Any]], None]) -> None:
        self._observer = observer

    def append(
        self, event_type: str, actor: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        event = super().append(event_type, actor, payload)
        if self._observer is not None:
            self._observer(event)
        return event


@dataclass(frozen=True)
class IdempotencyEntry:
    fingerprint: str
    run_id: str
    response: RunView | None


@dataclass
class RunContext:
    run_id: str
    directory: Path
    command: CreateRunRequest
    config: RunConfig
    capability_profile: CapabilityProfile
    record: RecordV1Writer
    control: ControlStore
    last_canonical: dict[str, Any]
    runtime: ModelRuntime | None = None
    orchestrator: DerivationOrchestrator | None = None
    provider_handles_live: bool = True
    observer_enabled: bool = False
    hard_interrupt_requested: bool = False
    error_message: str | None = None
    presentation_phase: str | None = None
    last_event_id: int = 0
    driver_task: asyncio.Task[None] | None = None
    events: list[RunEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[RunEvent | None]] = field(default_factory=set)
    branch_waiters: set[asyncio.Event] = field(default_factory=set)
    change_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class ArchivedRun:
    """Strictly replayed Record V1 projection with no writable runtime handles."""

    run_id: str
    directory: Path
    command: CreateRunRequest
    view: RunView


def _fingerprint(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _api_intake_session(
    session: IntakeSession,
    service: PersistentIntakeSessionService,
) -> IntakeSessionView:
    payload = intake_session_payload(session)
    payload["conversation"] = [
        {
            "event_id": event.event_id,
            "kind": event.kind,
            "payload": dict(event.payload),
        }
        for event in service.store.events(session.session_id)
    ]
    payload["frozen_problem"] = (
        _confirmed_intake_problem(session).model_dump(mode="json")
        if session.status.value == "confirmed"
        else None
    )
    # Read-only: the journal lives beside the event chain, so it is reported
    # with the session but never reconstructed from it.
    held = service.pending_submission(session.session_id, session=session)
    payload["pending_submission"] = None if held is None else _pending_submission(held)
    return IntakeSessionView.model_validate(payload)


def _pending_submission(held: PendingSubmission) -> dict[str, Any]:
    return {
        "kind": held.kind.value,
        "base_revision": held.base_revision,
        "submitted_at": held.submitted_at,
        "answers": {
            decision_id: {
                "selected_option_ids": list(answer.selected_option_ids),
                "custom_text": answer.custom_text,
                "source_message_refs": list(answer.source_message_refs),
                "strategy": None if answer.strategy is None else answer.strategy.value,
            }
            for decision_id, answer in held.answers.items()
        },
        "user_message": held.user_message,
        "failure_reason": held.failure_reason,
    }


def _confirmed_intake_problem(session: IntakeSession) -> FrozenProblemInput:
    if session.status.value != "confirmed" or not session.problem_specifications:
        raise ValueError("Run creation requires a confirmed IntakeSession")
    specification = session.problem_specifications[-1]
    sections = {section.name: section for section in specification.sections}

    def content(name: str) -> str:
        section = sections.get(name)
        if section is None:
            raise ValueError(f"confirmed Intake specification is missing {name}")
        if section.content is not None:
            return section.content
        if section.not_applicable_reason is not None:
            return f"Not applicable: {section.not_applicable_reason}"
        raise ValueError(f"confirmed Intake specification section {name} is empty")

    latest_decisions = {}
    for decision in session.decisions:
        latest_decisions[decision.decision_id] = decision
    accepted_decisions = []
    for decision_id in sorted(latest_decisions):
        decision = latest_decisions[decision_id]
        if decision.status is not DecisionStatus.RESOLVED or decision.answer is None:
            continue
        options = {option.option_id: option for option in decision.question.options}
        selected = [
            (
                f"{options[option_id].label} — {options[option_id].impact}"
                if option_id in options
                else option_id
            )
            for option_id in decision.answer.selected_option_ids
        ]
        answer_parts = [*selected]
        if decision.answer.custom_text is not None:
            answer_parts.append(decision.answer.custom_text)
        if decision.answer.strategy is not None:
            answer_parts.append(f"Strategy: {decision.answer.strategy.value}")
        accepted_decisions.append(
            f"{decision.question.title} [{decision.decision_id}]: "
            f"{decision.question.prompt} Accepted answer: " + " | ".join(answer_parts)
        )

    return FrozenProblemInput(
        problem_id=session.session_id,
        version=specification.version,
        supersedes_version=specification.supersedes_version,
        objective=(
            f"Purpose:\n{content('purpose')}\n\n"
            f"Scientific target:\n{content('scientific_target')}"
        ),
        givens=[
            f"Givens and starting point:\n{content('givens_and_starting_point')}",
            f"Notation and conventions:\n{content('notation_and_conventions')}",
        ],
        assumptions=[
            f"Assumptions and regime:\n{content('assumptions_and_regime')}",
            f"Agent discretion:\n{content('agent_discretion')}",
        ],
        accepted_decisions=accepted_decisions,
        declared_defaults=[
            {
                "default_id": item.default_id,
                "decision_class": item.decision_class.value,
                "title": item.title,
                "statement": item.statement,
                "rationale": item.rationale,
                "alternatives": list(item.alternatives),
            }
            for item in specification.declared_defaults
        ],
        refinement_ladder=[
            {
                "rung": item.rung,
                "name": item.name,
                "relaxes": item.relaxes,
                "default_ids": list(item.default_ids),
                "decision_ids": list(item.decision_ids),
                "parallel_branch": item.parallel_branch,
            }
            for item in specification.refinement_ladder
        ],
        scope=content("scope_and_non_goals"),
        deliverable=content("required_output"),
        allowed_tools=["scientific_compute"],
        allowed_references=[],
        success_criteria=[content("validation_criteria")],
        source_pack=None,
        confirmed_by_user=True,
    )


def _run_config_dict(config: RunConfig) -> dict[str, Any]:
    return {
        "run_id": config.run_id,
        "task": config.task.to_record(),
        "pack": config.pack.to_record(),
        "code_commit": config.code_commit,
        "granularity": config.granularity,
        "max_active_branches": config.max_active_branches,
        "max_model_calls": config.max_model_calls,
        "record_version": config.record_version,
        "checker_enabled": config.checker_enabled,
        "max_local_repairs": config.max_local_repairs,
        "concurrency": config.concurrency,
        "retries": config.retries,
        "service_tier": config.service_tier,
        "reading_mode": config.reading_mode,
        "generation_context_sha256": config.generation_context_sha256,
        "intent_ledger_first": config.intent_ledger_first,
        "dimension_check": config.dimension_check,
        "formula_validation_policy": config.formula_validation_policy,
        "writer": config.writer.to_record(),
        "checker": config.checker.to_record(),
        "judge": config.judge.to_record(),
        "backend_name": config.backend_name,
        "backend_version": config.backend_version,
        "record_spec": config.record_spec.to_record(),
        "event_schema": config.event_schema.to_record(),
        "canonical_schema": config.canonical_schema.to_record(),
        "input_policy": config.input_policy.to_record(),
        "credential_profile_id": config.credential_profile_id,
    }


def _run_config_from_dict(value: Mapping[str, Any]) -> RunConfig:
    def model(name: str) -> ModelSpec:
        item = cast(Mapping[str, Any], value[name])
        return ModelSpec(item["provider"], item["model"], item["effort"])

    def artifact(name: str) -> ArtifactRef:
        item = cast(Mapping[str, Any], value[name])
        return ArtifactRef(item["path"], item["sha256"])

    task = cast(Mapping[str, Any], value["task"])
    pack = cast(Mapping[str, Any], value["pack"])
    policy = cast(Mapping[str, Any], value["input_policy"])
    return RunConfig(
        run_id=value["run_id"],
        task=ContentRef(task["id"], task["sha256"]),
        pack=ContentRef(pack["id"], pack["sha256"]),
        code_commit=value["code_commit"],
        granularity=value["granularity"],
        max_active_branches=value["max_active_branches"],
        max_model_calls=value["max_model_calls"],
        record_version=value.get("record_version", "1.0"),
        checker_enabled=value.get("checker_enabled", True),
        max_local_repairs=value.get("max_local_repairs", 2),
        concurrency=value["concurrency"],
        retries=value["retries"],
        service_tier=value.get("service_tier", "standard"),
        reading_mode=value.get("reading_mode", "on_demand"),
        generation_context_sha256=value.get("generation_context_sha256"),
        intent_ledger_first=value.get("intent_ledger_first", False),
        dimension_check=value.get("dimension_check", False),
        formula_validation_policy=value.get("formula_validation_policy"),
        writer=model("writer"),
        checker=model("checker"),
        judge=model("judge"),
        backend_name=value["backend_name"],
        backend_version=value["backend_version"],
        record_spec=artifact("record_spec"),
        event_schema=artifact("event_schema"),
        canonical_schema=artifact("canonical_schema"),
        input_policy=InputPolicy(
            reference_allowed=policy["reference_allowed"],
            allowed_paths=tuple(policy["allowed_paths"]),
        ),
        credential_profile_id=value["credential_profile_id"],
    )


def _last_complete_canonical(events: list[dict[str, Any]]) -> dict[str, Any]:
    for end in range(len(events), 0, -1):
        try:
            return replay_events(events[:end]).canonical
        except ContractError:
            continue
    raise RuntimeInvariantError("Record V1 log has no strict-replayable prefix")


def _parse_manifest(
    manifest_path: Path,
) -> tuple[dict[str, Any], CreateRunRequest, CapabilityProfile, RunConfig]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if value["schema_version"] != MANIFEST_SCHEMA:
            raise ValueError("unsupported manifest schema")
        command = CreateRunRequest.model_validate(
            {
                "problem": value["problem"],
                "config": value["api_config"],
                "runtime": value["runtime_config"],
            }
        )
        capability_profile = resolve_capability_profile(
            command.runtime.capability_profile
        )
        stored_capability = value.get("capability_profile")
        stored_capability_sha256 = value.get("capability_profile_sha256")
        if stored_capability is not None and (
            stored_capability != capability_profile.to_dict()
            or stored_capability_sha256 != capability_profile.sha256
        ):
            raise ValueError("capability profile manifest binding differs")
        config = _run_config_from_dict(value["run_config"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid derivation run manifest: {manifest_path}") from exc
    if config.run_id != manifest_path.parent.name:
        raise RuntimeError(f"manifest run id does not match directory: {manifest_path}")
    return value, command, capability_profile, config


class RuntimeDerivationService:
    """Bind FastAPI commands to one orchestrator and Record per local run."""

    def __init__(
        self,
        *,
        run_root: str | Path,
        storage_root: str | Path | None = None,
        archive_root: str | Path | None = None,
        runtime_factory: RuntimeFactory,
        intake_session_service: PersistentIntakeSessionService | None = None,
        intake_workspace_cleanup: Callable[[str], None] | None = None,
        code_commit: str,
        credential_profile_id: str,
        create_run_defaults: CreateRunDefaultsView,
        model_catalog_loader: Callable[[], Awaitable[ModelCatalog]] | None = None,
        build_info: BuildInfoView | None = None,
        account_service: AccountService | None = None,
        repo_root: str | Path | None = None,
        run_id_factory: RunIdFactory | None = None,
        subscriber_queue_size: int = 256,
        service_identity: str = "derivation-runtime-adapter",
        preserve_provider_handles_on_start: bool = True,
        recovery_poll_initial_seconds: float = 0.05,
        recovery_poll_max_seconds: float = 1.0,
        report_exporter: ReportExporter | None = None,
        formula_validation_policy: str | None = None,
    ) -> None:
        self.repo_root = Path(
            repo_root or Path(__file__).resolve().parents[2]
        ).resolve()
        requested_run_root = Path(run_root)
        if requested_run_root.is_symlink():
            raise ValueError("run_root must not be a symlink")
        self.run_root = requested_run_root.resolve()
        allowed_root = Path(storage_root or (self.repo_root / "runs")).resolve()
        if allowed_root.is_symlink():
            raise ValueError("storage_root must not be a symlink")
        self.storage_root = allowed_root
        if not self.run_root.is_relative_to(allowed_root):
            raise ValueError("run_root must be inside storage_root")
        self.archive_root: Path | None = None
        if archive_root is not None:
            requested_archive_root = Path(archive_root)
            if requested_archive_root.is_symlink():
                raise ValueError("archive_root must not be a symlink")
            resolved_archive_root = requested_archive_root.resolve()
            if not resolved_archive_root.is_relative_to(allowed_root):
                raise ValueError("archive_root must be inside storage_root")
            self.archive_root = resolved_archive_root
        if len(code_commit) != 40 or any(
            character not in "0123456789abcdef" for character in code_commit
        ):
            raise ValueError("code_commit must be a lowercase 40-character Git hash")
        if not credential_profile_id.strip():
            raise ValueError("credential_profile_id must be non-empty")
        if subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        if not service_identity.strip():
            raise ValueError("service_identity must be non-empty")
        if recovery_poll_initial_seconds <= 0:
            raise ValueError("recovery_poll_initial_seconds must be positive")
        if recovery_poll_max_seconds < recovery_poll_initial_seconds:
            raise ValueError(
                "recovery_poll_max_seconds must be at least the initial interval"
            )
        self.runtime_factory = runtime_factory
        self.intake_session_service = intake_session_service
        self.intake_workspace_cleanup = intake_workspace_cleanup
        self.code_commit = code_commit
        self.credential_profile_id = credential_profile_id
        self.create_run_defaults = create_run_defaults.model_copy(deep=True)
        self.model_catalog = ModelCatalog(
            options=tuple(
                ModelOption(
                    model=option.model,
                    display_name=option.display_name,
                    default_effort=option.default_effort,
                    supported_efforts=tuple(option.supported_efforts),
                    default_service_tier=option.default_service_tier,
                    supported_service_tiers=tuple(option.supported_service_tiers),
                    is_default=option.is_default,
                )
                for option in self.create_run_defaults.model_options
            ),
            source=self.create_run_defaults.model_catalog_source,
            refreshed_at=self.create_run_defaults.model_catalog_refreshed_at,
        )
        self.model_catalog_loader = model_catalog_loader
        self.build_identity = (
            build_info
            or BuildInfoView(
                schema_version="derivationlab-build-info-v1",
                version="dev",
                build_number="0",
                release_id="development",
                commit=code_commit,
                openapi_sha256="0" * 64,
                product_mode="development",
            )
        ).model_copy(deep=True)
        if self.build_identity.commit != code_commit:
            raise ValueError("build_info commit must match the runtime code_commit")
        self.account_service = account_service or UnavailableAccountService()
        self.run_id_factory = run_id_factory or (lambda: f"run_{uuid.uuid4().hex}")
        self.subscriber_queue_size = subscriber_queue_size
        self.service_identity = service_identity
        self.preserve_provider_handles_on_start = preserve_provider_handles_on_start
        self.recovery_poll_initial_seconds = recovery_poll_initial_seconds
        self.recovery_poll_max_seconds = recovery_poll_max_seconds
        self.report_exporter = report_exporter or ReportExporter.from_repository_lock(
            repo_root=self.repo_root,
            report_root=self.run_root / "_report_bundles",
            allowed_root=self.storage_root,
        )
        if formula_validation_policy not in {None, "formula-v1", "formula-v2"}:
            raise ValueError("unsupported formula validation policy")
        self.formula_validation_policy = formula_validation_policy
        self._runs: dict[str, RunContext] = {}
        self._archives: dict[str, ArchivedRun] = {}
        self._archive_rejections: dict[Path, str] = {}
        self._idempotency: dict[tuple[str, str], IdempotencyEntry] = {}
        self._started = False
        self._closing = False
        self._close_failed = False
        self._service_lock = asyncio.Lock()
        self._intake_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._closing or self._close_failed:
            raise RuntimeError(
                "service shutdown is incomplete; retry close before start"
            )
        if self._started:
            return
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)
        await self.account_service.start()
        if self.run_root.is_symlink():
            raise RuntimeError("run_root must not be a symlink")
        self._runs.clear()
        self._archives.clear()
        self._archive_rejections.clear()
        self._idempotency.clear()
        try:
            if self.model_catalog_loader is not None:
                self.model_catalog = await self.model_catalog_loader()
                default, effort, service_tier = product_default_selection(
                    self.model_catalog
                )
                role_updates = {
                    role_name: self.create_run_defaults.config.model_dump()[role_name]
                    | {"model": default.model, "effort": effort}
                    for role_name in ("writer", "checker", "judge")
                }
                defaults_payload = self.create_run_defaults.model_dump()
                defaults_payload["config"] |= role_updates
                defaults_payload["runtime"]["service_tier"] = service_tier
                defaults_payload |= {
                    "model_options": [
                        {
                            "model": option.model,
                            "display_name": option.display_name,
                            "is_default": option.is_default,
                            "default_effort": option.default_effort,
                            "supported_efforts": list(option.supported_efforts),
                            "default_service_tier": option.default_service_tier,
                            "supported_service_tiers": list(
                                option.supported_service_tiers
                            ),
                        }
                        for option in self.model_catalog.options
                    ],
                    "model_catalog_source": self.model_catalog.source,
                    "model_catalog_refreshed_at": self.model_catalog.refreshed_at,
                    "allowed_models": list(self.model_catalog.models),
                    "allowed_efforts": list(self.model_catalog.efforts),
                }
                self.create_run_defaults = CreateRunDefaultsView.model_validate(
                    defaults_payload
                )
            for manifest_path in sorted(self.run_root.glob("*/manifest.json")):
                context = self._load_context(manifest_path)
                self._runs[context.run_id] = context
            self._discover_archives()
            self._load_idempotency_entries()
            self._started = True
            for context in self._runs.values():
                context.observer_enabled = True
                self._publish(context, "run.updated")
                self._schedule_recovery(context)
        except Exception:
            self._started = False
            for context in self._runs.values():
                task = context.driver_task
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if context.runtime is not None:
                    await self._close_runtime(context.runtime)
                context.control.close()
            self._runs.clear()
            self._archives.clear()
            self._archive_rejections.clear()
            self._idempotency.clear()
            await self.account_service.close()
            raise

    async def close(self) -> None:
        async with self._close_lock:
            if not self._started and not self._close_failed:
                return
            self._closing = True
            try:
                tasks: list[asyncio.Task[None]] = []
                for context in self._runs.values():
                    task = context.driver_task
                    if task is not None and not task.done():
                        if context.orchestrator is None:
                            # A lazily scheduled recovery coroutine may not
                            # have allocated a runtime yet. Cancellation here
                            # has no provider-side operation to orphan.
                            task.cancel()
                            tasks.append(task)
                            continue
                        context.orchestrator.request_soft_pause(
                            actor_id="service-shutdown",
                            reason=(
                                "Graceful local service shutdown requested a safe boundary."
                            ),
                        )
                        tasks.append(task)
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=5.0)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                for context in self._runs.values():
                    with context.lock:
                        for queue in context.subscribers:
                            while not queue.empty():
                                queue.get_nowait()
                            queue.put_nowait(None)
                        context.subscribers.clear()
                        previous = context.change_event
                        context.change_event = asyncio.Event()
                        previous.set()

                runtime_errors: list[str] = []
                outcomes: dict[int, BaseException | None] = {}
                for context in self._runs.values():
                    runtime = context.runtime
                    if runtime is None:
                        continue
                    identity = id(runtime)
                    if identity not in outcomes:
                        try:
                            await self._close_runtime(runtime)
                        except BaseException as exc:  # noqa: BLE001
                            outcomes[identity] = exc
                        else:
                            outcomes[identity] = None
                    failure = outcomes[identity]
                    if failure is None:
                        context.runtime = None
                        context.orchestrator = None
                    else:
                        runtime_errors.append(f"{context.run_id}: {failure}")
                if runtime_errors:
                    self._close_failed = True
                    raise RuntimeError(
                        "failed to close derivation runtimes; runtime references and "
                        "instance locks were retained for retry: "
                        + "; ".join(runtime_errors)
                    )

                for context in self._runs.values():
                    context.control.close()
                await self.account_service.close()
                self._started = False
                self._close_failed = False
            except BaseException:
                self._close_failed = True
                raise
            finally:
                self._closing = False

    async def health(self) -> HealthView:
        return HealthView(
            status="degraded"
            if not self._started or self._closing or self._close_failed
            else "ok",
            service=self.service_identity,
        )

    async def build_info(self) -> BuildInfoView:
        return self.build_identity.model_copy(deep=True)

    async def quit_readiness(self) -> QuitReadinessView:
        active = 0
        for context in self._runs.values():
            with context.lock:
                task = context.driver_task
                model_calls = context.record.snapshot()["model_calls"]
                if (task is not None and not task.done()) or any(
                    item["state"] == "started" for item in model_calls
                ):
                    active += 1
        return QuitReadinessView(
            safe_to_quit=active == 0,
            active_run_count=active,
        )

    async def account(self) -> AccountView:
        return await self.account_service.account()

    async def account_rate_limits(self) -> AccountRateLimitsView:
        return await self.account_service.rate_limits()

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView:
        return await self.account_service.import_existing_account(
            command,
            idempotency_key=idempotency_key,
        )

    async def start_device_login(self) -> DeviceLoginStartView:
        return await self.account_service.start_device_login()

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView:
        return await self.account_service.get_device_login(login_id)

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView:
        return await self.account_service.cancel_device_login(login_id)

    async def capabilities(self) -> CapabilitiesView:
        return CapabilitiesView(
            api_version="derivation-http-v1",
            command_transport="http",
            event_transport="sse",
            branch_kinds=["human_direction", "human_revision"],
            phases=PHASES,
            idempotency_header="Idempotency-Key",
            sse_cursor_header="Last-Event-ID",
            replay_query="follow=false",
            create_run_defaults=self.create_run_defaults.model_copy(deep=True),
        )

    async def get_problem_presets(self) -> ProblemPresetsView:
        from .problem_sources import build_problem_presets

        return await asyncio.to_thread(build_problem_presets, self.repo_root)

    def _require_intake_service(self) -> PersistentIntakeSessionService:
        self._require_started()
        if self.intake_session_service is None:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "intake_service_unavailable",
                "Persistent AI Problem Intake is not configured for this service.",
            )
        return self.intake_session_service

    @staticmethod
    def _log_intake_conflict(
        exc: Exception,
        context: Mapping[str, object] | None,
    ) -> None:
        """Say why an Intake command was refused, since the API only says 409.

        A conflict reaches the browser as one opaque status code, so the reason
        — the revision the store expected against the one the command carried,
        or the validation message and the decisions it names — has to be
        recoverable from the server log alone.
        """

        details = ", ".join(
            f"{key}={value!r}" for key, value in sorted((context or {}).items())
        )
        logger.warning(
            "Intake command conflict (%s): %s: %s%s",
            type(exc).__name__,
            exc.__class__.__module__.rsplit(".", 1)[-1],
            exc,
            f" [{details}]" if details else "",
        )

    @staticmethod
    def _raise_intake_error(
        exc: Exception,
        context: Mapping[str, object] | None = None,
    ) -> None:
        if isinstance(
            exc,
            (
                IntakeStaleRevision,
                IntakeIdempotencyConflict,
                IntakeCommandRejected,
                ValueError,
            ),
        ):
            RuntimeDerivationService._log_intake_conflict(exc, context)
        if isinstance(exc, IntakeSessionNotFound):
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "intake_session_not_found",
                f"IntakeSession {str(exc)!r} does not exist.",
            ) from exc
        if isinstance(exc, IntakeStaleRevision):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_stale_revision",
                str(exc),
            ) from exc
        if isinstance(exc, IntakeIdempotencyConflict):
            raise DerivationServiceError(
                ErrorKind.IDEMPOTENCY_CONFLICT,
                "intake_idempotency_conflict",
                "The Intake idempotency key was reused with different input.",
            ) from exc
        if isinstance(exc, IntakeCommandRejected):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                exc.code,
                str(exc),
            ) from exc
        if isinstance(exc, ProfileLockHeld):
            raise DerivationServiceError(
                ErrorKind.CONFLICT,
                "intake_profile_busy",
                "Another model runtime currently owns the product profile.",
            ) from exc
        if isinstance(exc, ValueError):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_command_invalid",
                str(exc),
            ) from exc
        logger.exception("Persistent AI Problem Intake command failed closed")
        raise DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "intake_command_failed",
            "The persistent AI Intake command failed closed.",
            details={"error_type": type(exc).__name__},
        ) from exc

    async def create_intake_session(
        self,
        command: CreateIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            model, effort = validate_model_effort(
                command.model,
                command.effort,
                catalog=self.model_catalog,
            )
            _, service_tier = validate_model_service_tier(
                model,
                command.service_tier,
                catalog=self.model_catalog,
            )
            async with self._intake_lock:
                session = await service.start(
                    command.initial_message,
                    model=model,
                    effort=effort,
                    service_tier=service_tier,
                    idempotency_key=idempotency_key,
                )
            return _api_intake_session(session, service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(exc)

    async def get_intake_session(self, session_id: str) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            return _api_intake_session(service.get(session_id), service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(exc)

    async def list_intake_sessions(
        self, *, status: IntakeSessionStatusValue
    ) -> list[IntakeSessionView]:
        service = self._require_intake_service()
        if status not in {"active", "convergence_required", "candidate_ready"}:
            return []
        try:
            sessions = service.list_active()
            return [
                _api_intake_session(session, service)
                for session in sessions
                if session.status.value == status
            ]
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(exc)

    async def submit_intake_round(
        self,
        session_id: str,
        command: SubmitIntakeRoundRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            answers = {
                decision_id: DecisionAnswer(
                    selected_option_ids=tuple(answer.selected_option_ids),
                    custom_text=answer.custom_text,
                    strategy=(
                        None
                        if answer.strategy is None
                        else AnswerStrategy(answer.strategy)
                    ),
                )
                for decision_id, answer in command.answers.items()
            }
            async with self._intake_lock:
                session = await service.submit_round(
                    session_id,
                    base_revision=command.base_revision,
                    answers=answers,
                    user_message=command.user_message,
                    idempotency_key=idempotency_key,
                )
            return _api_intake_session(session, service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(
                exc,
                {
                    "command": "submit_round",
                    "session_id": session_id,
                    "received_base_revision": command.base_revision,
                    "answered_decision_ids": sorted(command.answers),
                    "has_correction": command.user_message is not None,
                },
            )

    async def finalize_intake_session(
        self,
        session_id: str,
        command: FinalizeIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            answers = {
                decision_id: DecisionAnswer(
                    selected_option_ids=tuple(answer.selected_option_ids),
                    custom_text=answer.custom_text,
                    strategy=(
                        None
                        if answer.strategy is None
                        else AnswerStrategy(answer.strategy)
                    ),
                )
                for decision_id, answer in command.answers.items()
            }
            async with self._intake_lock:
                session = await service.finalize(
                    session_id,
                    base_revision=command.base_revision,
                    answers=answers,
                    idempotency_key=idempotency_key,
                )
            return _api_intake_session(session, service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(
                exc,
                {
                    "command": "finalize",
                    "session_id": session_id,
                    "received_base_revision": command.base_revision,
                    "answered_decision_ids": sorted(command.answers),
                },
            )

    async def confirm_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            async with self._intake_lock:
                session = service.confirm(
                    session_id,
                    base_revision=command.base_revision,
                    idempotency_key=idempotency_key,
                )
                if self.intake_workspace_cleanup is not None:
                    self.intake_workspace_cleanup(session_id)
            return _api_intake_session(session, service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(
                exc,
                {
                    "command": "confirm",
                    "session_id": session_id,
                    "received_base_revision": command.base_revision,
                },
            )

    async def cancel_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        service = self._require_intake_service()
        try:
            async with self._intake_lock:
                session = service.cancel(
                    session_id,
                    base_revision=command.base_revision,
                    idempotency_key=idempotency_key,
                )
                if self.intake_workspace_cleanup is not None:
                    self.intake_workspace_cleanup(session_id)
            return _api_intake_session(session, service)
        except DerivationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed API boundary
            self._raise_intake_error(
                exc,
                {
                    "command": "cancel",
                    "session_id": session_id,
                    "received_base_revision": command.base_revision,
                },
            )

    async def create_run(
        self,
        command: CreateRunRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView:
        self._require_started()
        async with self._service_lock:
            cached = self._idempotency_lookup("create_run", idempotency_key, command)
            if cached is not None:
                return cached
            intake_session = None
            existing_intake_run: RunView | None = None
            if command.problem.problem_id.startswith(("intake_", "intake-")):
                intake_service = self._require_intake_service()
                try:
                    intake_session = intake_service.get(command.problem.problem_id)
                    expected_problem = _confirmed_intake_problem(intake_session)
                except Exception as exc:  # noqa: BLE001 - mapped Intake boundary
                    self._raise_intake_error(exc)
                if command.problem != expected_problem:
                    raise DerivationServiceError(
                        ErrorKind.INVALID_STATE,
                        "intake_run_projection_mismatch",
                        "Run input differs from the confirmed Intake specification.",
                    )
                matches = [
                    (context.command, self._view(context))
                    for context in self._runs.values()
                    if context.command.problem.problem_id == intake_session.session_id
                ]
                matches.extend(
                    (archive.command, archive.view.model_copy(deep=True))
                    for archive in self._archives.values()
                    if archive.command.problem.problem_id == intake_session.session_id
                )
                if len(matches) > 1:
                    raise RuntimeInvariantError(
                        "one confirmed IntakeSession is bound to multiple Runs"
                    )
                if matches:
                    prior_command, existing_intake_run = matches[0]
                    if prior_command != command:
                        raise DerivationServiceError(
                            ErrorKind.INVALID_STATE,
                            "intake_run_already_created",
                            "This confirmed IntakeSession already created a Run with different runtime configuration.",
                            details={"run_id": existing_intake_run.id},
                        )
                    return existing_intake_run
            run_id = self.run_id_factory()
            if not RUN_ID_RE.fullmatch(run_id):
                raise DerivationServiceError(
                    ErrorKind.CONFLICT,
                    "run_id_invalid",
                    "Generated run id is not a portable path component.",
                )
            if (
                run_id in self._runs
                or run_id in self._archives
                or (self.run_root / run_id).exists()
            ):
                raise DerivationServiceError(
                    ErrorKind.CONFLICT,
                    "run_id_collision",
                    f"Generated run id {run_id!r} already exists.",
                )
            config = self._config_for_command(run_id, command)
            intake_handoff = None
            if intake_session is not None:
                intake_service = self._require_intake_service()
                intake_handoff = intake_service.handoff(
                    intake_session.session_id,
                    run_id=run_id,
                )
            self._idempotency_accept(
                "create_run",
                idempotency_key,
                command,
                run_id=run_id,
            )
            directory = self.run_root / run_id
            directory.mkdir(parents=False, exist_ok=False)
            if intake_handoff is not None:
                intake_directory = directory / "intake"
                intake_directory.mkdir(parents=False, exist_ok=False)
                for name, content in intake_handoff.files.items():
                    target = intake_directory / name
                    if target.parent != intake_directory or target.exists():
                        raise RuntimeInvariantError(
                            "refusing unsafe Intake handoff export target"
                        )
                    target.write_bytes(content)
            context = self._new_context(directory, command, config)
            self._runs[run_id] = context
            try:
                orchestrator = await self._ensure_orchestrator(context)
                await orchestrator.initialize(command.task_text)
                context.last_canonical = context.record.verify_complete()
                context.presentation_phase = RunPhase.SUBMITTED.value
                context.observer_enabled = True
                self._write_manifest(context)
                self._publish(context, "run.created")
                response = self._displayed_view(context)
                self._idempotency_store(
                    "create_run", idempotency_key, command, response
                )
                self._schedule_driver(
                    context,
                    orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION),
                    running_phase=RunPhase.AUTONOMOUS_EXPLORATION,
                )
                return response
            except Exception as operation_error:
                if context.runtime is not None:
                    try:
                        await self._close_runtime(context.runtime)
                    except BaseException as close_error:
                        self._close_failed = True
                        raise RuntimeError(
                            "run creation failed and its runtime could not be closed; "
                            "the service retained the runtime for close retry; "
                            f"operation={operation_error!r}; cleanup={close_error!r}"
                        ) from close_error
                    context.runtime = None
                    context.orchestrator = None
                context.control.close()
                self._runs.pop(run_id, None)
                raise

    async def get_run(self, run_id: str) -> RunView:
        """The run as the product shows it: typeset math where a layer verifies.

        The Record is untouched and every hash in the view still identifies the
        recorded content; the steps whose math came from a typeset layer say so
        (``StepView.typeset``), and the layers themselves are named in
        ``RunView.typeset_layers``. A caller that needs the sealed text - an
        evidence pipeline, an exporter - asks :meth:`sealed_run` instead.
        """

        self._require_started()
        context = self._runs.get(run_id)
        if context is not None:
            return self._displayed(context.directory, self._view(context))
        archive = self._archives.get(run_id)
        if archive is not None:
            return self._displayed(
                archive.directory, archive.view.model_copy(deep=True)
            )
        raise self._not_found(run_id)

    async def sealed_run(self, run_id: str) -> RunView:
        """The Record's own projection, with no typeset layer applied.

        Evidence keeps its own accessor: a snapshot that is hashed, archived or
        compared against the Record must be the text the Record sealed, byte
        for byte, whatever a display layer would render.
        """

        self._require_started()
        context = self._runs.get(run_id)
        if context is not None:
            return self._view(context)
        archive = self._archives.get(run_id)
        if archive is not None:
            return archive.view.model_copy(deep=True)
        raise self._not_found(run_id)

    async def list_runs(self) -> list[RunSummary]:
        self._require_started()
        rows = [
            self._summary(self._view(context), read_only=False)
            for context in self._runs.values()
        ]
        rows.extend(
            self._summary(archive.view, read_only=True)
            for archive in self._archives.values()
        )
        return sorted(rows, key=lambda item: (item.updated_at, item.id), reverse=True)

    async def export_report(
        self,
        run_id: str,
        command: ExportReportRequest,
    ) -> ReportBundleView:
        """Derive one immutable ReportBundle without mutating Record V1."""

        self._require_started()
        context = self._runs.get(run_id)
        archive = self._archives.get(run_id)
        if context is None and archive is None:
            raise self._not_found(run_id)
        if context is not None:
            view = self._view(context)
            source = ReportSource(
                run=view,
                problem=context.command.problem.model_copy(deep=True),
                typeset=self._typeset_layers(context.directory, view),
            )
        else:
            assert archive is not None
            archived_view = archive.view.model_copy(deep=True)
            source = ReportSource(
                run=archived_view,
                problem=archive.command.problem.model_copy(deep=True),
                typeset=self._typeset_layers(archive.directory, archived_view),
            )
        if command.selected_route_id not in {route.id for route in source.run.routes}:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "report_route_not_found",
                "The explicitly confirmed route does not exist in this Run snapshot.",
                details={
                    "run_id": run_id,
                    "selected_route_id": command.selected_route_id,
                },
            )
        try:
            return await asyncio.to_thread(
                self.report_exporter.export,
                source,
                selected_route_id=command.selected_route_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.exception(
                "ReportBundle export failed before a result could be returned"
            )
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "report_export_failed",
                "ReportBundle export failed closed.",
                details={"error_type": type(exc).__name__},
            ) from exc

    async def read_report_pdf(self, run_id: str, export_id: str) -> bytes:
        """Return only a validated PDF belonging to the requested run/export."""

        self._require_started()
        if run_id not in self._runs and run_id not in self._archives:
            raise self._not_found(run_id)
        try:
            return await asyncio.to_thread(
                self.report_exporter.read_pdf, run_id, export_id
            )
        except FileNotFoundError as exc:
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "report_pdf_not_found",
                "The requested ReportBundle PDF does not exist.",
                details={"run_id": run_id, "export_id": export_id},
            ) from exc
        except (OSError, ValueError) as exc:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "report_download_failed",
                "ReportBundle PDF download failed closed.",
                details={"error_type": type(exc).__name__},
            ) from exc

    async def pause_run(self, run_id: str, *, idempotency_key: str | None) -> RunView:
        context = self._writable_context(run_id)
        async with context.command_lock:
            scope = f"pause:{run_id}"
            cached = self._idempotency_lookup(scope, idempotency_key, {})
            if cached is not None:
                return cached
            view = self._view(context)
            if view.phase not in {
                "submitted",
                "autonomous_exploration",
                "human_expansion",
            }:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_pausable",
                    "Soft pause is valid only while the scheduler is active.",
                    details={"phase": view.phase},
                )
            if view.pause_requested:
                self._idempotency_accept(
                    scope, idempotency_key, {}, run_id=context.run_id
                )
                response = self._displayed(context.directory, view)
                self._idempotency_store(scope, idempotency_key, {}, response)
                return response
            self._idempotency_accept(scope, idempotency_key, {}, run_id=context.run_id)
            orchestrator = await self._ensure_orchestrator(context)
            orchestrator.request_soft_pause(
                actor_id="local-user",
                reason="The local user requested a soft pause at the next sealed boundary.",
            )
            self._publish(context, "run.updated")
            response = self._displayed_view(context)
            self._idempotency_store(scope, idempotency_key, {}, response)
            return response

    async def resume_run(
        self,
        run_id: str,
        *,
        idempotency_key: str | None,
    ) -> RunView:
        """Resume a safely paused run through the frozen HTTP service seam."""

        context = self._writable_context(run_id)
        async with context.command_lock:
            scope = f"resume:{run_id}"
            cached = self._idempotency_lookup(scope, idempotency_key, {})
            if cached is not None:
                return cached
            view = self._view(context)
            if view.phase != "paused":
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_resumable",
                    "Only a safely paused run can be resumed.",
                    details={"phase": view.phase},
                )
            self._idempotency_accept(scope, idempotency_key, {}, run_id=context.run_id)
            orchestrator = await self._ensure_orchestrator(context)
            self._schedule_driver(
                context,
                orchestrator.resume(
                    actor_id="local-user",
                    reason="The local user resumed autonomous exploration.",
                ),
                running_phase=RunPhase.AUTONOMOUS_EXPLORATION,
            )
            await asyncio.sleep(0)
            response = self._displayed_view(context)
            self._idempotency_store(scope, idempotency_key, {}, response)
            return response

    async def interrupt_run(
        self, run_id: str, *, idempotency_key: str | None
    ) -> RunView:
        context = self._writable_context(run_id)
        async with context.command_lock:
            scope = f"interrupt:{run_id}"
            cached = self._idempotency_lookup(scope, idempotency_key, {})
            if cached is not None:
                return cached
            view = self._view(context)
            snapshot = context.record.snapshot()
            in_flight_ids = [
                item["model_call_id"]
                for item in snapshot["model_calls"]
                if item["state"] == "started"
            ]
            if not in_flight_ids:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_interruptible",
                    "Hard interrupt requires at least one current in-flight call.",
                    details={"phase": view.phase, "active_calls": 0},
                )

            unready_call_ids = self._interrupt_unready_call_ids(context, in_flight_ids)
            if unready_call_ids:
                raise DerivationServiceError(
                    ErrorKind.UNAVAILABLE,
                    "interrupt_handle_not_ready",
                    "A provider operation is starting but has no interrupt handle yet; retry shortly.",
                    details={"model_call_ids": unready_call_ids},
                )

            self._idempotency_accept(scope, idempotency_key, {}, run_id=context.run_id)
            task = context.driver_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            orchestrator = await self._ensure_orchestrator(context)
            for model_call_id in in_flight_ids:
                await orchestrator.hard_interrupt(
                    model_call_id=model_call_id,
                    actor_id="local-user",
                    reason="The local user requested a hard interruption.",
                )
            context.hard_interrupt_requested = True
            context.presentation_phase = "interrupted"
            self._refresh_canonical(context)
            self._write_manifest(context)
            self._publish(context, "run.updated")
            response = self._displayed_view(context)
            self._idempotency_store(scope, idempotency_key, {}, response)
            return response

    async def create_branch(
        self,
        run_id: str,
        command: CreateBranchRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView:
        context = self._writable_context(run_id)
        async with context.command_lock:
            scope = f"create_branch:{run_id}"
            cached = self._idempotency_lookup(scope, idempotency_key, command)
            if cached is not None:
                return cached
            view = self._view(context)
            if view.phase not in {"review_ready", "review_ready_due_to_cap"}:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_review_ready",
                    "Human expansion is allowed only after autonomous review readiness.",
                    details={"phase": view.phase},
                )
            query = ReplayQuery(context.last_canonical)
            try:
                route = query.route_for_node(command.from_step_revision_id)
            except KeyError as exc:
                raise DerivationServiceError(
                    ErrorKind.NOT_FOUND,
                    "step_revision_not_found",
                    f"StepRevision {command.from_step_revision_id!r} is not sealed in this run.",
                ) from exc

            if command.kind == "human_direction":
                replacement = None
            else:
                replacement = command.revision_content()
                if replacement is None:
                    raise RuntimeInvariantError(
                        "human_revision omitted its validated content"
                    )
                if command.canonical_instruction() != canonical_json(
                    replacement.model_dump()
                ):
                    raise RuntimeInvariantError(
                        "human_revision canonical instruction differs from content"
                    )
            self._idempotency_accept(
                scope, idempotency_key, command, run_id=context.run_id
            )
            waiter = asyncio.Event()
            context.branch_waiters.add(waiter)
            orchestrator = await self._ensure_orchestrator(context)
            if command.kind == "human_direction":
                operation = orchestrator.expand_from_step(
                    parent_branch_id=route.branch_id,
                    step_revision_id=command.from_step_revision_id,
                    direction=command.instruction,
                    actor_id="local-user",
                    reason="Post-run human review requested a new direction.",
                )
            else:
                assert replacement is not None
                operation = orchestrator.revise_step(
                    parent_branch_id=route.branch_id,
                    step_revision_id=command.from_step_revision_id,
                    replacement=StepContent(**replacement.model_dump()),
                    actor_id="local-user",
                    reason="Post-run human review replaced one sealed step on a new route.",
                )
            task = self._schedule_driver(
                context,
                operation,
                running_phase=RunPhase.HUMAN_EXPANSION,
            )
            waiter_task = asyncio.create_task(waiter.wait())
            try:
                done, _ = await asyncio.wait(
                    {waiter_task, task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if waiter_task not in done or not waiter.is_set():
                    raise DerivationServiceError(
                        ErrorKind.UNAVAILABLE,
                        "branch_creation_failed",
                        context.error_message
                        or "The runtime did not create the requested branch.",
                    )
            finally:
                context.branch_waiters.discard(waiter)
                if not waiter_task.done():
                    waiter_task.cancel()
                await asyncio.gather(waiter_task, return_exceptions=True)
            response = self._displayed_view(context)
            self._idempotency_store(scope, idempotency_key, command, response)
            return response

    def stream_events(
        self,
        run_id: str,
        *,
        after_event_id: int,
        follow: bool,
    ) -> AsyncIterator[RunEvent]:
        self._require_started()
        archive = self._archives.get(run_id)
        if archive is not None:

            async def archive_stream() -> AsyncIterator[RunEvent]:
                if not follow:
                    return
                never = asyncio.Event()
                await never.wait()
                if False:  # pragma: no cover - marks this as an async generator.
                    yield cast(RunEvent, None)

            return archive_stream()
        context = self._context(run_id)

        async def stream() -> AsyncIterator[RunEvent]:
            queue: asyncio.Queue[RunEvent | None] | None = None
            with context.lock:
                backlog = [
                    item.model_copy(deep=True)
                    for item in context.events
                    if item.event_id > after_event_id
                ]
                if follow:
                    queue = asyncio.Queue(maxsize=self.subscriber_queue_size)
                    context.subscribers.add(queue)
            try:
                for item in backlog:
                    yield item
                if queue is None:
                    return
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    yield item
            finally:
                if queue is not None:
                    with context.lock:
                        context.subscribers.discard(queue)

        return stream()

    async def wait_for_phase(
        self,
        run_id: str,
        phases: set[str],
        *,
        timeout: float = 10.0,
    ) -> RunView:
        """Testing/embedding hook that waits on service state, not polling files."""

        context = self._context(run_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            view = self._view(context)
            if view.phase in phases:
                return view
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"run {run_id} did not enter {sorted(phases)}; current phase is {view.phase}"
                )
            with context.lock:
                changed = context.change_event
            await asyncio.wait_for(changed.wait(), timeout=remaining)

    def event_log_path(self, run_id: str) -> Path:
        return self._context(run_id).directory / "events.jsonl"

    def strict_canonical(self, run_id: str) -> dict[str, Any]:
        return self._context(run_id).record.verify_complete()

    def _new_context(
        self,
        directory: Path,
        command: CreateRunRequest,
        config: RunConfig,
    ) -> RunContext:
        event_log = ObservedEventLogWriter(directory / "events.jsonl", config.run_id)
        record = RecordV1Writer(event_log, config)
        control = ControlStore(directory / "control.sqlite")
        capability_profile = resolve_capability_profile(
            command.runtime.capability_profile
        )
        context = RunContext(
            run_id=config.run_id,
            directory=directory,
            command=command.model_copy(deep=True),
            config=config,
            capability_profile=capability_profile,
            record=record,
            control=control,
            last_canonical={},
        )
        event_log.set_observer(lambda event: self._on_record_event(context, event))
        control.set_invocation_observer(
            lambda _run_id, _model_call_id: self._on_invocation_attached(context)
        )
        return context

    def _on_invocation_attached(self, context: RunContext) -> None:
        """Publish the moment a Record-active call becomes interruptible."""

        if not context.observer_enabled:
            return
        try:
            self._publish(context, "run.updated")
        except Exception:
            logger.exception(
                "failed to publish provider-handle readiness for run %s",
                context.run_id,
            )

    async def _ensure_orchestrator(
        self,
        context: RunContext,
    ) -> DerivationOrchestrator:
        if context.orchestrator is not None:
            return context.orchestrator
        async with context.runtime_lock:
            if context.orchestrator is not None:
                return context.orchestrator
            produced = self.runtime_factory(context.config, context.directory)
            runtime = await produced if inspect.isawaitable(produced) else produced
            context.runtime = runtime
            try:
                if context.provider_handles_live:
                    self._register_recovered_writer_sessions(context, runtime)
                orchestrator = DerivationOrchestrator(
                    config=context.config,
                    task_text=context.command.task_text,
                    runtime=runtime,
                    record=context.record,
                    control=context.control,
                    provider_handles_live=context.provider_handles_live,
                    formula_validator=self._formula_validator(context),
                )
            except Exception:
                try:
                    await self._close_runtime(runtime)
                except BaseException:
                    # Keep the exact runtime reachable so service.close() can
                    # retry without allowing a second backend instance.
                    self._close_failed = True
                    raise
                context.runtime = None
                raise
            context.orchestrator = orchestrator
            return orchestrator

    def _formula_validator(
        self, context: RunContext
    ) -> Callable[[StepContent], FormulaValidationResult] | None:
        # The manifest, not the currently installed product default, decides
        # whether a recovered run participates in the new format policy.
        # formula-v2 is a pure static gate inside the orchestrator; only v1
        # compiles each step with the locked engine.
        if context.config.formula_validation_policy in {None, "formula-v2"}:
            return None
        from .formula_compiler import ProductFormulaValidator

        return ProductFormulaValidator(
            runner=self.report_exporter.runner,
            evidence_root=context.directory / "formula_validation",
        )

    @staticmethod
    def _register_recovered_writer_sessions(
        context: RunContext,
        runtime: ModelRuntime,
    ) -> None:
        register = getattr(runtime, "register_recovered_writer_session", None)
        if not callable(register) or not context.record.events:
            return
        snapshot = context.record.snapshot()
        for branch in sorted(
            snapshot["branches"],
            key=lambda item: item["branch_id"],
        ):
            branch_id = branch["branch_id"]
            bookmark = context.control.branch(context.run_id, branch_id)
            if (
                bookmark is None
                or bookmark.provider_session_id is None
                or bookmark.provider_lineage is None
            ):
                continue
            try:
                session = RuntimeSession(
                    bookmark.provider_session_id,
                    ProviderLineage(bookmark.provider_lineage),
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeInvariantError(
                    f"invalid persisted writer session for Branch {branch_id}"
                ) from exc
            register(session, branch_id)

    def _load_context(self, manifest_path: Path) -> RunContext:
        self._validate_run_directory(manifest_path)
        value, command, _capability_profile, config = _parse_manifest(manifest_path)
        try:
            context = self._new_context(manifest_path.parent, command, config)
            context.last_event_id = self._read_sse_cursor(context.directory)
            events = context.record.events
            context.last_canonical = _last_complete_canonical(events)
            context.hard_interrupt_requested = bool(
                value.get("hard_interrupt_requested", False)
            )
            stored_error = value.get("error_message")
            if stored_error is not None and not isinstance(stored_error, str):
                raise RuntimeError(
                    f"invalid error_message in manifest: {manifest_path}"
                )
            context.error_message = stored_error
            if context.hard_interrupt_requested:
                context.presentation_phase = "interrupted"
            elif context.error_message is not None:
                context.presentation_phase = "error"
            snapshot = context.record.snapshot()
            query_status = ReplayQuery(context.last_canonical).status()
            context.provider_handles_live = (
                self.preserve_provider_handles_on_start
                and query_status.phase
                not in {
                    RunPhase.REVIEW_READY,
                    RunPhase.REVIEW_READY_DUE_TO_CAP,
                }
            )
            try:
                persisted_run = context.control.run(context.run_id)
            except KeyError:
                context.control.rebuild_from_snapshot(
                    snapshot,
                    credential_profile_id=config.credential_profile_id,
                    phase=query_status.phase,
                    stop_reason=query_status.stop_reason,
                )
            else:
                if not self.preserve_provider_handles_on_start:
                    format_paused = (
                        config.formula_validation_policy is not None
                        and persisted_run.stop_reason == "formula_validation_failed"
                    )
                    context.control.rebuild_from_snapshot(
                        snapshot,
                        credential_profile_id=config.credential_profile_id,
                        phase=RunPhase.PAUSED if format_paused else query_status.phase,
                        stop_reason=persisted_run.stop_reason
                        if format_paused
                        else query_status.stop_reason,
                    )
                    if format_paused:
                        context.control.update_phase(
                            context.run_id,
                            RunPhase.PAUSED,
                            stop_reason=persisted_run.stop_reason,
                            last_error=persisted_run.last_error,
                        )
                else:
                    context.control.reconcile_from_snapshot(snapshot)
            return context
        except Exception:
            if "context" in locals():
                context.control.close()
            raise

    def _discover_archives(self) -> None:
        if self.archive_root is None or not self.archive_root.exists():
            return
        if not self.archive_root.is_dir():
            raise RuntimeError("archive_root must be a directory when it exists")

        duplicates: set[str] = set()
        for manifest_path in sorted(self.archive_root.rglob("manifest.json")):
            directory = manifest_path.parent
            try:
                if directory.resolve().is_relative_to(self.run_root):
                    continue
                archive = self._load_archive(manifest_path)
            except (
                ContractError,
                KeyError,
                OSError,
                RuntimeError,
                StopIteration,
                TypeError,
                ValueError,
            ) as exc:
                self._archive_rejections[manifest_path] = str(exc)
                continue
            if archive.run_id in self._runs:
                continue
            if archive.run_id in duplicates:
                continue
            if archive.run_id in self._archives:
                del self._archives[archive.run_id]
                duplicates.add(archive.run_id)
                continue
            self._archives[archive.run_id] = archive

    def _load_archive(self, manifest_path: Path) -> ArchivedRun:
        self._validate_archive_directory(manifest_path)
        value, command, _capability_profile, config = _parse_manifest(manifest_path)
        events = load_events(manifest_path.parent / "events.jsonl")
        canonical = replay_events(events).canonical
        run_record = canonical["run"]
        expected_record = {
            "run_id": config.run_id,
            "task": config.task.to_record(),
            "pack": config.pack.to_record(),
            "code_commit": config.code_commit,
            "configuration": config.record_configuration(),
            "input_policy": config.input_policy.to_record(),
            "record_spec": config.record_spec.to_record(),
            "event_schema": config.event_schema.to_record(),
            "canonical_schema": config.canonical_schema.to_record(),
        }
        if any(
            run_record.get(key) != expected for key, expected in expected_record.items()
        ):
            raise RuntimeError(
                f"archive manifest differs from Record V1: {manifest_path}"
            )
        if sha256_text(command.task_text) != config.task.sha256:
            raise RuntimeError(
                f"archive problem differs from Record V1 task: {manifest_path}"
            )
        hard_interrupt_requested = value.get("hard_interrupt_requested", False)
        if not isinstance(hard_interrupt_requested, bool):
            raise TypeError(
                f"invalid hard_interrupt_requested in manifest: {manifest_path}"
            )
        error_message = value.get("error_message")
        if error_message is not None and not isinstance(error_message, str):
            raise RuntimeError(f"invalid error_message in manifest: {manifest_path}")
        phase: str | None = None
        if hard_interrupt_requested:
            phase = "interrupted"
        elif error_message is not None:
            phase = "error"
        view = build_run_view(
            canonical=canonical,
            question=command.question,
            api_config=command.config,
            runtime_config=command.runtime,
            record_events=events,
            phase=phase,
            pause_requested=False,
            hard_interrupt_requested=hard_interrupt_requested,
            error_message=error_message,
            step_bookmark=lambda _step_id: None,
            call_bookmark=lambda _call_id: None,
        )
        view = self._with_command_capabilities(
            view,
            read_only=True,
            interrupt_ready=False,
        )
        if view.id != config.run_id:
            raise RuntimeError(f"Record run id differs from manifest: {manifest_path}")
        return ArchivedRun(
            run_id=config.run_id,
            directory=manifest_path.parent,
            command=command,
            view=view,
        )

    def _validate_archive_directory(self, manifest_path: Path) -> None:
        if self.archive_root is None:
            raise RuntimeError("archive discovery is disabled")
        directory = manifest_path.parent
        if not RUN_ID_RE.fullmatch(directory.name):
            raise RuntimeError(f"invalid archive run directory name: {directory}")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"archive run directory must be real: {directory}")
        resolved = directory.resolve()
        if not resolved.is_relative_to(self.archive_root):
            raise RuntimeError(
                f"archive run resolves outside archive_root: {directory}"
            )
        relative = directory.relative_to(self.archive_root)
        cursor = self.archive_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RuntimeError(f"archive path must not contain symlinks: {cursor}")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError(
                f"archive manifest must be a regular file: {manifest_path}"
            )
        event_log = directory / "events.jsonl"
        if event_log.is_symlink() or not event_log.is_file():
            raise RuntimeError(f"archive event log must be a regular file: {event_log}")

    def _validate_run_directory(self, manifest_path: Path) -> None:
        directory = manifest_path.parent
        if not RUN_ID_RE.fullmatch(directory.name):
            raise RuntimeError(f"invalid run directory name: {directory}")
        expected = self.run_root / directory.name
        if directory != expected:
            raise RuntimeError(
                f"run directory is not an exact run_root child: {directory}"
            )
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"run directory must be a real directory: {directory}")
        if directory.resolve() != expected:
            raise RuntimeError(f"run directory resolves outside run_root: {directory}")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError(
                f"manifest must be a regular non-symlink file: {manifest_path}"
            )
        event_log = directory / "events.jsonl"
        if event_log.is_symlink() or not event_log.is_file():
            raise RuntimeError(
                f"event log must be a regular non-symlink file: {event_log}"
            )
        for optional_name in (
            "control.sqlite",
            "control.sqlite-shm",
            "control.sqlite-wal",
            "sse_cursor.json",
            "idempotency.jsonl",
        ):
            optional = directory / optional_name
            if optional.is_symlink() or (optional.exists() and not optional.is_file()):
                raise RuntimeError(f"invalid run support file: {optional}")

    def _schedule_recovery(self, context: RunContext) -> None:
        if context.hard_interrupt_requested or context.error_message is not None:
            return
        if self._formula_pause_message(context) is not None:
            return
        snapshot = context.record.snapshot()
        in_flight = any(item["state"] == "started" for item in snapshot["model_calls"])
        query_phase = ReplayQuery(context.last_canonical).status().phase
        if in_flight:

            async def reconcile_until_settled() -> object:
                orchestrator = await self._ensure_orchestrator(context)
                delay = self.recovery_poll_initial_seconds
                while True:
                    result = await orchestrator.reconcile_in_flight()
                    if result is not None:
                        return result
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.recovery_poll_max_seconds)

            self._schedule_driver(
                context,
                reconcile_until_settled(),
                running_phase=RunPhase.RECOVERING,
            )
        elif query_phase is RunPhase.AUTONOMOUS_EXPLORATION:

            async def resume_autonomous() -> object:
                orchestrator = await self._ensure_orchestrator(context)
                return await orchestrator.drive(phase=RunPhase.AUTONOMOUS_EXPLORATION)

            self._schedule_driver(
                context,
                resume_autonomous(),
                running_phase=RunPhase.AUTONOMOUS_EXPLORATION,
            )
        elif self._typeset_pending(context):
            # A restart between a completed route and its typeset layer: build
            # the layer before this run is presented as review-ready again.
            # The phase moves before the task is scheduled, so a client that
            # reads the run in between never sees review-ready without it.
            context.presentation_phase = RunPhase.RECOVERING.value
            self._schedule_driver(
                context,
                self._typeset_completed_routes(context),
                running_phase=RunPhase.RECOVERING,
            )

    def _schedule_driver(
        self,
        context: RunContext,
        operation: Coroutine[Any, Any, object],
        *,
        running_phase: RunPhase,
    ) -> asyncio.Task[None]:
        current = context.driver_task
        if current is not None and not current.done():
            operation.close()
            raise DerivationServiceError(
                ErrorKind.CONFLICT,
                "run_driver_busy",
                "The run already has an active scheduler operation.",
            )

        operation_started = False

        async def drive() -> None:
            nonlocal operation_started
            operation_started = True
            context.presentation_phase = running_phase.value
            self._publish(context, "run.updated")
            try:
                await operation
            except asyncio.CancelledError:
                raise
            # A scheduler operation is an isolation boundary: provider and
            # invariant failures become an explicit non-scientific error phase.
            except Exception as exc:  # noqa: BLE001
                self._log_run_failure(context, exc, "runtime_error")
                context.error_message = str(exc)
                context.presentation_phase = RunPhase.ERROR.value
                try:
                    context.control.reconcile_from_snapshot(context.record.snapshot())
                except Exception as recovery_error:  # noqa: BLE001
                    self._log_run_failure(
                        context, recovery_error, "bookmark_recovery_failed"
                    )
                with suppress(KeyError):
                    context.control.update_phase(
                        context.run_id,
                        RunPhase.ERROR,
                        stop_reason="runtime_error",
                        last_error=str(exc),
                    )
                self._refresh_canonical(context)
                self._write_manifest(context)
                self._publish(context, "run.error")
            else:
                self._refresh_canonical(context)
                await self._typeset_completed_routes(context)
                try:
                    await self._release_review_terminal_runtime(context)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # The scientific Record is already terminal, but the
                    # provider owner is not safely reusable until its exact
                    # runtime closes. Keep both references for service.close()
                    # to retry and block every new command meanwhile. This
                    # transient cleanup failure is intentionally not written
                    # into the Run manifest or Record.
                    self._close_failed = True
                    self._log_run_failure(context, exc, "runtime_release_failed")
                    context.error_message = f"terminal runtime release failed: {exc}"
                    context.presentation_phase = RunPhase.ERROR.value
                    self._publish(context, "run.error")
                else:
                    context.presentation_phase = None
                    self._publish(context, "run.updated")

        task = asyncio.create_task(drive(), name=f"derivation-driver:{context.run_id}")

        def close_unstarted_operation(_task: asyncio.Task[None]) -> None:
            if not operation_started:
                operation.close()

        task.add_done_callback(close_unstarted_operation)
        context.driver_task = task
        return task

    def _log_run_failure(
        self, context: RunContext, error: Exception, failure_code: str
    ) -> None:
        # Exception messages and tracebacks can contain provider/user content.
        # Keep operational logs searchable without copying that content.
        logger.error(
            "Derivation run failure run_id=%s failure_code=%s error_type=%s release_id=%s commit=%s",
            context.run_id,
            failure_code,
            type(error).__name__,
            self.build_identity.release_id,
            self.build_identity.commit,
            extra={
                "run_id": context.run_id,
                "failure_code": failure_code,
                "error_type": type(error).__name__,
                "release_id": self.build_identity.release_id,
                "code_commit": self.build_identity.commit,
            },
        )

    def _typeset_pending(self, context: RunContext) -> bool:
        if context.config.formula_validation_policy != "formula-v2":
            return False
        if context.hard_interrupt_requested or context.error_message is not None:
            return False
        try:
            return bool(
                pending_typeset_routes(context.directory, context.last_canonical)
            )
        except (KeyError, OSError, ValueError):
            return False

    async def _typeset_completed_routes(self, context: RunContext) -> None:
        """Write the typeset layer of every completed route of this run.

        It runs inside the scheduler operation, after the Record is terminal
        for this operation and before the run is presented as review-ready,
        because a consumer that renders the candidate the moment the phase
        turns review-ready has to find the layer already there. It writes no
        Record event, never pauses the run, and turns its own failures into
        flags inside the layer.
        """

        if self._closing or not self._typeset_pending(context):
            return
        try:
            if context.runtime is None:
                # A run recovered after a restart has no live provider yet, and
                # the repair round needs one.
                await self._ensure_orchestrator(context)
            await typeset_completed_routes(
                run_directory=context.directory,
                config=context.config,
                canonical=context.record.verify_complete(),
                runner=self.report_exporter.runner,
                runtime=context.runtime,
                # Steps the per-step gate sealed with unresolved format
                # defects are marked in the layer; nothing else reads the
                # control plane's audit.
                format_audits=context.control.formula_audits(context.run_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - typesetting never fails a run
            self._log_run_failure(context, exc, "route_typeset_failed")

    def _typeset_layers(
        self, directory: Path, view: RunView
    ) -> dict[str, dict[str, Any]] | None:
        """Verified typeset layers for the routes of one run view."""

        return run_view_typeset_layers(directory, view)

    def _displayed(self, directory: Path, view: RunView) -> RunView:
        """One sealed view as the client sees it, with verified typeset math.

        Every client-facing emission of a RunView goes through here - the
        catalog read, the command responses and the SSE payload - so a reader
        never sees the same step repaired in one response and broken in the
        next. A run without a verified layer is returned unchanged.
        """

        return displayed_run_view(directory, view)

    def _displayed_view(self, context: RunContext) -> RunView:
        return self._displayed(context.directory, self._view(context))

    async def _release_review_terminal_runtime(self, context: RunContext) -> None:
        """Release provider ownership only after a reviewable Record terminal."""

        phase = ReplayQuery(context.last_canonical).status().phase
        if phase not in {
            RunPhase.REVIEW_READY,
            RunPhase.REVIEW_READY_DUE_TO_CAP,
        }:
            return
        async with context.runtime_lock:
            runtime = context.runtime
            if runtime is None:
                if context.orchestrator is not None:
                    raise RuntimeInvariantError(
                        "terminal run has an orchestrator without a runtime"
                    )
                context.provider_handles_live = False
                return
            await self._close_runtime(runtime)
            context.provider_handles_live = False
            context.runtime = None
            context.orchestrator = None

    def _on_record_event(self, context: RunContext, event: Mapping[str, Any]) -> None:
        self._refresh_canonical(context)
        if not context.observer_enabled:
            return
        event_type = {
            "run_created": "run.created",
            "branch_created": "branch.created",
            "step_revision_sealed": "step.sealed",
        }.get(event["type"], "run.updated")
        self._publish(context, event_type)
        if event["type"] == "branch_created":
            for waiter in tuple(context.branch_waiters):
                waiter.set()

    def _refresh_canonical(self, context: RunContext) -> None:
        try:
            canonical = context.record.verify_complete()
        except ContractError:
            return
        context.last_canonical = canonical

    def _phase(self, context: RunContext) -> str:
        if context.hard_interrupt_requested:
            return "interrupted"
        if context.error_message is not None:
            return "error"
        if self._formula_pause_message(context) is not None:
            return "paused"
        if context.presentation_phase is not None:
            return context.presentation_phase
        return ReplayQuery(context.last_canonical).status().phase.value

    @staticmethod
    def _formula_pause_message(context: RunContext) -> str | None:
        if context.config.formula_validation_policy is None:
            return None
        try:
            bookmark = context.control.run(context.run_id)
        except KeyError:
            return None
        if bookmark.stop_reason != "formula_validation_failed":
            return None
        details = ["Formula validation paused."]
        audits = context.control.formula_audits(context.run_id)
        if any(
            audit["disposition"] == "infrastructure_failure"
            for audit in audits.values()
        ):
            details.append(
                "The formula compiler is unavailable or failed. Restore it, then resume to recheck the saved response."
            )
        else:
            details.append(
                "The configured format retry budget is exhausted. Resume cannot reset it; start a new run to try again."
            )
        for audit in audits.values():
            if audit["disposition"] == "rejected":
                details.extend(
                    f"{issue.get('field', 'content')} formula {issue.get('formula_index', 0)}: "
                    f"{issue.get('message', issue.get('code', 'format error'))}"
                    for issue in audit["issues"]
                    if issue.get("severity") == "error"
                )
        return "\n".join(details)

    def _view(self, context: RunContext) -> RunView:
        if not context.last_canonical:
            raise RuntimeInvariantError("run has no strict canonical snapshot")
        try:
            pause_requested = context.control.run(context.run_id).pause_requested
        except KeyError:
            pause_requested = False
        view = build_run_view(
            canonical=context.last_canonical,
            question=context.command.question,
            api_config=context.command.config,
            runtime_config=context.command.runtime,
            record_events=context.record.events,
            phase=self._phase(context),
            pause_requested=pause_requested,
            hard_interrupt_requested=context.hard_interrupt_requested,
            error_message=context.error_message or self._formula_pause_message(context),
            step_bookmark=lambda step_id: context.control.step(context.run_id, step_id),
            call_bookmark=lambda call_id: context.control.call(context.run_id, call_id),
        )
        in_flight_ids = [
            item["model_call_id"]
            for item in context.record.snapshot()["model_calls"]
            if item["state"] == "started"
        ]
        view = self._with_command_capabilities(
            view,
            read_only=False,
            interrupt_ready=(
                bool(in_flight_ids)
                and not self._interrupt_unready_call_ids(context, in_flight_ids)
            ),
        )
        if self._formula_pause_message(context) is not None:
            can_recheck = any(
                audit["disposition"] == "infrastructure_failure"
                for audit in context.control.formula_audits(context.run_id).values()
            )
            view = view.model_copy(
                update={
                    "commands": view.commands.model_copy(
                        update={"can_resume": can_recheck}
                    )
                }
            )
        return view

    @staticmethod
    def _interrupt_unready_call_ids(
        context: RunContext,
        in_flight_ids: list[str],
    ) -> list[str]:
        """Return Record-active calls that cannot yet be interrupted safely."""

        bookmarks = {
            item.model_call_id: item
            for item in context.control.in_flight_calls(context.run_id)
        }
        return [
            model_call_id
            for model_call_id in in_flight_ids
            if (
                (bookmark := bookmarks.get(model_call_id)) is None
                or bookmark.provider_session_id is None
                or bookmark.provider_operation_id is None
            )
        ]

    @staticmethod
    def _with_command_capabilities(
        view: RunView,
        *,
        read_only: bool,
        interrupt_ready: bool,
    ) -> RunView:
        active_branches = sum(branch.status == "active" for branch in view.branches)
        branch_capacity = (
            view.config.max_active_branches is None
            or active_branches < view.config.max_active_branches
        )
        branchable = (
            [step.revision_id for step in view.steps if step.status == "sealed"]
            if not read_only
            and view.phase in {"review_ready", "review_ready_due_to_cap"}
            and branch_capacity
            else []
        )
        return view.model_copy(
            update={
                "read_only": read_only,
                "commands": RunCommandCapabilities(
                    can_pause=(
                        not read_only
                        and view.phase
                        in {
                            "submitted",
                            "autonomous_exploration",
                            "human_expansion",
                        }
                        and not view.pause_requested
                    ),
                    can_resume=not read_only and view.phase == "paused",
                    can_interrupt=(
                        not read_only
                        and interrupt_ready
                        and not view.hard_interrupt_requested
                    ),
                    branchable_step_revision_ids=branchable,
                ),
            },
            deep=True,
        )

    def _overlay(self, context: RunContext) -> RuntimeOverlay:
        snapshot = context.record.snapshot()
        branches = {item["branch_id"]: item for item in snapshot["branches"]}
        steps = {item["step_revision_id"]: item for item in snapshot["step_revisions"]}
        checks = {item["check_id"]: item for item in snapshot["checks"]}
        candidates = {item["candidate_id"]: item for item in snapshot["candidates"]}
        judgements = {item["judgement_id"]: item for item in snapshot["judgements"]}
        active: list[ActiveCallOverlay] = []
        for call in snapshot["model_calls"]:
            if call["state"] != "started":
                continue
            bookmark = context.control.call(context.run_id, call["model_call_id"])
            branch_id = call["target"].get("branch_id")
            if branch_id is None and bookmark is not None:
                branch_id = bookmark.branch_id
            from_step_id: str | None = None
            check_id = call["target"].get("check_id")
            if branch_id is None and check_id in checks:
                from_step_id = checks[check_id]["target_step_revision_id"]
                branch_id = steps[from_step_id]["branch_id"]
            judgement_id = call["target"].get("judgement_id")
            if branch_id is None and judgement_id in judgements:
                candidate = candidates[judgements[judgement_id]["candidate_id"]]
                from_step_id = candidate["tip_step_revision_id"]
                branch_id = candidate["branch_id"]
            if branch_id is None or branch_id not in branches:
                continue
            step_ids = branches[branch_id]["step_revision_ids"]
            if from_step_id is None:
                from_step_id = step_ids[-1] if step_ids else f"task_{context.run_id}"
            active.append(
                ActiveCallOverlay(
                    call_id=call["model_call_id"],
                    branch_id=branch_id,
                    from_step_id=from_step_id,
                    label=f"{call['role'].title()} model call",
                )
            )
        return RuntimeOverlay(
            hard_interrupt_requested=context.hard_interrupt_requested,
            active_calls=active,
        )

    def _publish(self, context: RunContext, event_type: str) -> None:
        with context.lock:
            event_id = context.last_event_id + 1
            event = RunEvent(
                event_id=event_id,
                type=cast(Any, event_type),
                run_id=context.run_id,
                occurred_at=utc_now(),
                run=self._displayed_view(context).model_copy(deep=True),
                overlay=self._overlay(context),
            )
            self._write_sse_cursor(context.directory, event_id)
            context.last_event_id = event_id
            context.events.append(event)
            stale: list[asyncio.Queue[RunEvent | None]] = []
            for queue in context.subscribers:
                try:
                    queue.put_nowait(event.model_copy(deep=True))
                except asyncio.QueueFull:
                    queue.get_nowait()
                    queue.put_nowait(None)
                    stale.append(queue)
            for queue in stale:
                context.subscribers.discard(queue)
            previous = context.change_event
            context.change_event = asyncio.Event()
            previous.set()

    def _config_for_command(self, run_id: str, command: CreateRunRequest) -> RunConfig:
        from .problem_sources import validate_problem_sources

        if command.problem.origin == "direct_spec" and (
            command.config.record_version != "1.1"
            or command.runtime.capability_profile != "source_reading_v1"
        ):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "direct_problem_profile_invalid",
                "Direct problem specifications require Record 1.1 and source_reading_v1.",
            )
        try:
            pack = validate_problem_sources(command, self.repo_root)
        except ValueError as exc:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE, "problem_sources_invalid", str(exc)
            ) from exc

        if self.model_catalog_loader is not None:
            for role_name in ("writer", "checker", "judge"):
                role = getattr(command.config, role_name)
                if role.provider != "openai":
                    continue
                try:
                    validate_model_effort(
                        role.model,
                        role.effort,
                        catalog=self.model_catalog,
                    )
                    validate_model_service_tier(
                        role.model,
                        command.runtime.service_tier,
                        catalog=self.model_catalog,
                    )
                except ValueError as exc:
                    raise DerivationServiceError(
                        ErrorKind.INVALID_STATE,
                        "run_model_invalid",
                        f"{role_name}: {exc}",
                    ) from exc

        def artifact(relative: str) -> ArtifactRef:
            path = self.repo_root / relative
            return ArtifactRef(relative, sha256_bytes(path.read_bytes()))

        return RunConfig(
            run_id=run_id,
            task=ContentRef(f"task_{run_id}", sha256_text(command.task_text)),
            pack=pack,
            code_commit=self.code_commit,
            granularity=command.config.granularity,
            max_active_branches=command.config.max_active_branches,
            max_model_calls=command.config.max_model_calls,
            record_version=command.config.record_version,
            checker_enabled=command.config.checker_enabled,
            max_local_repairs=command.config.max_local_repairs,
            concurrency=command.runtime.concurrency,
            retries=command.runtime.retries,
            service_tier=command.runtime.service_tier,
            reading_mode=command.runtime.reading_mode,
            generation_context_sha256=command.runtime.generation_context_sha256,
            intent_ledger_first=command.runtime.intent_ledger_first,
            dimension_check=command.runtime.dimension_check,
            formula_validation_policy=self.formula_validation_policy,
            writer=ModelSpec(**command.config.writer.model_dump()),
            checker=ModelSpec(**command.config.checker.model_dump()),
            judge=ModelSpec(**command.config.judge.model_dump()),
            backend_name=command.config.backend.name,
            backend_version=command.config.backend.version,
            record_spec=artifact(
                "docs/spec/DERIVATION_RUNTIME_RECORD_V1_1_cn.md"
                if command.config.record_version == "1.1"
                else "docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md"
            ),
            event_schema=artifact(
                "src/derivation_agent_record/schemas/event-v1.1.schema.json"
                if command.config.record_version == "1.1"
                else "docs/spec/derivation_agent_event_v1.schema.json"
            ),
            canonical_schema=artifact(
                "src/derivation_agent_record/schemas/canonical-v1.1.schema.json"
                if command.config.record_version == "1.1"
                else "docs/spec/derivation_agent_canonical_v1.schema.json"
            ),
            input_policy=InputPolicy(
                reference_allowed=command.config.reference_allowed,
                allowed_paths=tuple(command.config.allowed_paths),
            ),
            credential_profile_id=self.credential_profile_id,
        )

    def _write_manifest(self, context: RunContext) -> None:
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "question": context.command.question,
            "problem": context.command.problem.model_dump(mode="json"),
            "api_config": context.command.config.model_dump(mode="json"),
            "runtime_config": context.command.runtime.model_dump(mode="json"),
            "capability_profile": context.capability_profile.to_dict(),
            "capability_profile_sha256": context.capability_profile.sha256,
            "run_config": _run_config_dict(context.config),
            "hard_interrupt_requested": context.hard_interrupt_requested,
            "error_message": context.error_message,
        }
        path = context.directory / "manifest.json"
        pending = context.directory / ".manifest.json.pending"
        encoded = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with pending.open("w", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)

    @staticmethod
    async def _close_runtime(runtime: ModelRuntime) -> None:
        close = getattr(runtime, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except BaseException:
            # AuthorizedRuntimeLease deliberately surfaces a late protocol
            # health error after it has closed the child, while retaining the
            # instance lock until one explicit retry confirms that exact exit.
            # Perform that one bounded retry during service shutdown.
            if not bool(getattr(runtime, "close_failed", False)):
                raise
            retry = close()
            if inspect.isawaitable(retry):
                await retry

    @staticmethod
    def _read_sse_cursor(directory: Path) -> int:
        path = directory / "sse_cursor.json"
        if not path.exists():
            return 0
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"invalid SSE cursor file: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["schema_version"] != SSE_CURSOR_SCHEMA:
                raise ValueError("unsupported SSE cursor schema")
            event_id = value["last_event_id"]
            if (
                isinstance(event_id, bool)
                or not isinstance(event_id, int)
                or event_id < 0
            ):
                raise ValueError("last_event_id must be a non-negative integer")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid derivation SSE cursor: {path}") from exc
        return event_id

    @staticmethod
    def _write_sse_cursor(directory: Path, event_id: int) -> None:
        path = directory / "sse_cursor.json"
        pending = directory / ".sse_cursor.json.pending"
        payload = {
            "schema_version": SSE_CURSOR_SCHEMA,
            "last_event_id": event_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with pending.open("w", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)

    def _context(self, run_id: str) -> RunContext:
        self._require_started()
        try:
            return self._runs[run_id]
        except KeyError:
            raise self._not_found(run_id) from None

    def _writable_context(self, run_id: str) -> RunContext:
        self._require_started()
        if run_id in self._archives:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "archive_run_read_only",
                "Archived derivation runs are immutable and cannot accept commands.",
                details={"run_id": run_id, "read_only": True},
            )
        return self._context(run_id)

    @staticmethod
    def _not_found(run_id: str) -> DerivationServiceError:
        return DerivationServiceError(
            ErrorKind.NOT_FOUND,
            "run_not_found",
            f"Derivation run {run_id!r} does not exist.",
        )

    @staticmethod
    def _summary(view: RunView, *, read_only: bool) -> RunSummary:
        return RunSummary(
            id=view.id,
            question=view.question,
            status=view.status,
            phase=view.phase,
            step_count=len(view.steps),
            route_count=len(view.routes),
            read_only=read_only,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    def _require_started(self) -> None:
        if not self._started:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "service_not_started",
                "The derivation runtime service has not started.",
            )
        if self._closing or self._close_failed:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "service_shutdown_incomplete",
                "Service shutdown is in progress or failed; retry close before use.",
            )

    def _idempotency_lookup(
        self,
        scope: str,
        key: str | None,
        payload: object,
    ) -> RunView | None:
        if key is None:
            return None
        entry = self._idempotency.get((scope, key))
        if entry is None:
            return None
        if entry.fingerprint != _fingerprint(payload):
            raise DerivationServiceError(
                ErrorKind.IDEMPOTENCY_CONFLICT,
                "idempotency_key_reused",
                "The Idempotency-Key was already used with a different command.",
                details={"scope": scope},
            )
        if entry.response is None:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "idempotent_command_pending",
                "This command was durably accepted but its outcome was not "
                "durably settled; refusing a potentially duplicate side effect.",
                details={"scope": scope, "run_id": entry.run_id},
            )
        return entry.response.model_copy(deep=True)

    def _idempotency_accept(
        self,
        scope: str,
        key: str | None,
        payload: object,
        *,
        run_id: str,
    ) -> None:
        if key is None:
            return
        cache_key = (scope, key)
        if cache_key in self._idempotency:
            raise RuntimeError("idempotency accept called after lookup found an entry")
        entry = IdempotencyEntry(
            fingerprint=_fingerprint(payload),
            run_id=run_id,
            response=None,
        )
        self._append_idempotency_entry(scope, key, entry)
        self._idempotency[cache_key] = entry

    def _idempotency_store(
        self,
        scope: str,
        key: str | None,
        payload: object,
        response: RunView,
    ) -> None:
        if key is None:
            return
        cache_key = (scope, key)
        pending = self._idempotency.get(cache_key)
        fingerprint = _fingerprint(payload)
        if (
            pending is None
            or pending.fingerprint != fingerprint
            or pending.run_id != response.id
            or pending.response is not None
        ):
            raise RuntimeError("idempotency completion lacks its exact pending intent")
        entry = IdempotencyEntry(
            fingerprint=fingerprint,
            run_id=response.id,
            response=response.model_copy(deep=True),
        )
        if response.id not in self._runs:
            raise RuntimeError(
                f"cannot persist idempotency for unknown run {response.id!r}"
            )
        self._append_idempotency_entry(scope, key, entry)
        self._idempotency[cache_key] = entry

    def _load_idempotency_entries(self) -> None:
        path = self.run_root / ".idempotency.jsonl"
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"invalid idempotency ledger: {path}")
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.endswith("\n"):
                    raise RuntimeError(
                        f"idempotency ledger line {line_number} lacks newline: {path}"
                    )
                try:
                    value = json.loads(raw_line)
                    if set(value) != {
                        "schema_version",
                        "state",
                        "scope",
                        "key",
                        "fingerprint",
                        "run_id",
                        "response",
                    }:
                        raise ValueError("ledger entry keys differ")
                    if value["schema_version"] != IDEMPOTENCY_SCHEMA:
                        raise ValueError("unsupported idempotency schema")
                    scope = value["scope"]
                    key = value["key"]
                    fingerprint = value["fingerprint"]
                    run_id = value["run_id"]
                    state = value["state"]
                    if not all(
                        type(item) is str and bool(item)
                        for item in (scope, key, fingerprint, run_id, state)
                    ):
                        raise ValueError("ledger identity fields must be text")
                    if len(fingerprint) != 64 or any(
                        character not in "0123456789abcdef" for character in fingerprint
                    ):
                        raise ValueError("fingerprint must be sha256")
                    if not RUN_ID_RE.fullmatch(run_id):
                        raise ValueError("ledger run_id is not portable")
                    if state == "pending":
                        if value["response"] is not None:
                            raise ValueError("pending ledger response must be null")
                        response = None
                    elif state == "completed":
                        response = RunView.model_validate(value["response"])
                        if response.id != run_id:
                            raise ValueError(
                                "response run id differs from ledger run_id"
                            )
                    else:
                        raise ValueError("unsupported ledger state")
                    if scope != "create_run" and run_id not in self._runs:
                        raise ValueError("non-create ledger references an unknown run")
                    if response is not None and run_id not in self._runs:
                        raise ValueError("completed ledger references an unknown run")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"invalid idempotency ledger line {line_number}: {path}"
                    ) from exc
                entry = IdempotencyEntry(
                    fingerprint=fingerprint,
                    run_id=run_id,
                    response=response,
                )
                cache_key = (scope, key)
                previous = self._idempotency.get(cache_key)
                if previous is not None:
                    same_intent = (
                        previous.fingerprint == entry.fingerprint
                        and previous.run_id == entry.run_id
                    )
                    valid_transition = (
                        same_intent
                        and previous.response is None
                        and entry.response is not None
                    )
                    exact_repeat = previous == entry
                    if not valid_transition and not exact_repeat:
                        raise RuntimeError(
                            f"conflicting persisted idempotency key {key!r} in {path}"
                        )
                self._idempotency[cache_key] = entry

    def _append_idempotency_entry(
        self,
        scope: str,
        key: str,
        entry: IdempotencyEntry,
    ) -> None:
        path = self.run_root / ".idempotency.jsonl"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f"invalid idempotency ledger: {path}")
        value = {
            "schema_version": IDEMPOTENCY_SCHEMA,
            "state": "completed" if entry.response is not None else "pending",
            "scope": scope,
            "key": key,
            "fingerprint": entry.fingerprint,
            "run_id": entry.run_id,
            "response": None
            if entry.response is None
            else entry.response.model_dump(mode="json"),
        }
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError(f"idempotency ledger is not regular: {path}")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise OSError("short write to idempotency ledger")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name != "nt":
            directory_descriptor = os.open(self.run_root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)


__all__ = ["RuntimeDerivationService", "RuntimeFactory"]
