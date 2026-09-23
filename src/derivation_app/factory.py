"""Composition roots for deterministic development and future web mounting."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from derivation_api.application import ApiSettings
from derivation_api.application import create_app as create_api_app
from derivation_api.models import (
    DEFAULT_MAX_MODEL_CALLS,
    BackendConfig,
    BuildInfoView,
    CreateRunDefaultsView,
    FrozenRunConfig,
    ModelOptionView,
    RoleModelConfig,
    RuntimeConfig,
)
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from derivation_runtime.app_server_client import AppServerClient, ClientTimeouts
from derivation_runtime.app_server_structured_turn import (
    StructuredTurnResult,
    StructuredTurnSpec,
    run_app_server_structured_turn,
)
from derivation_runtime.capabilities import (
    BENCHMARK_SYMBOLIC_V1,
    INTAKE_V1,
    SOURCE_READING_V1,
    CapabilityProfile,
)
from derivation_runtime.fake import DeterministicFakeRuntime
from derivation_runtime.platform_policy import PINNED_CODEX_VERSION, PlatformFamily
from derivation_runtime.scientific_runtime import provision_scientific_runtime
from derivation_runtime.types import (
    BranchAlternative,
    CheckOutput,
    JudgeOutput,
    ModelRuntime,
    RunConfig,
    RuntimeInvariantError,
    StepContent,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
)

from .account import CodexAccountBackend, ProductAccountManager
from .account_rate_limits import AccountRateLimitStore
from .app_server_intake_session import (
    AppServerIntakeSessionAdvisor,
    AppServerProblemSpecificationAuditor,
    IntakeRoundMode,
    IntakeRoundProposal,
    SpecificationAudit,
    ThreadReplacementReceipt,
)
from .host_run_lease import HostLeasedRuntime, HostScientificRunLease
from .intake_session import (
    AnswerMode,
    DecisionClass,
    DeclaredDefault,
    IntakeQuestion,
    IntakeSession,
    LadderRung,
    ProblemSection,
    ProblemSpecification,
    ProblemSpecificationStatus,
    QuestionOption,
    SQLiteIntakeStore,
    intake_session_fingerprint,
)
from .intake_session_service import PersistentIntakeSessionService
from .model_catalog import (
    DEFAULT_PRODUCT_EFFORT,
    DEFAULT_PRODUCT_MODEL,
    DEFAULT_PRODUCT_SERVICE_TIER,
    ModelCatalog,
    ModelCatalogCache,
    catalog_from_app_server_rows,
    fake_catalog,
    fixture_catalog,
    product_default_selection,
)
from .product_profile import (
    IntakeWorkspaceMapping,
    ProductProfile,
    ProfileInstanceLock,
    collect_and_authorize_intake_turn,
    create_product_runtime,
    create_shared_product_runtime,
    prepare_intake_workspace,
    prepare_product_launch,
    prepare_run_workspace,
    provision_product_profile,
)
from .reporting import ReportExporter
from .service import DeterministicAccountService, RunIdFactory, RuntimeDerivationService
from .shared_client import SharedProfileClientHolder
from .site_identity import SiteIdentityStore
from .tenant_content import FilesystemTenantContentReader
from .tenant_runtime import (
    TenantPaths,
    TenantRequestServiceResolver,
    TenantRuntimeRegistry,
)


def _create_run_defaults(
    *,
    provider: str,
    model: str,
    effort: str,
    service_tier: str,
    backend_name: str,
    backend_version: str,
    catalog: ModelCatalog,
) -> CreateRunDefaultsView:
    role = RoleModelConfig(provider=provider, model=model, effort=effort)
    return CreateRunDefaultsView(
        config=FrozenRunConfig(
            granularity="one_task",
            writer=role,
            checker=role,
            judge=role,
            backend=BackendConfig(name=backend_name, version=backend_version),
            max_model_calls=DEFAULT_MAX_MODEL_CALLS,
            max_active_branches=1,
            reference_allowed=False,
            allowed_paths=[],
        ),
        runtime=RuntimeConfig(
            auth_mode="chatgpt",
            concurrency=1,
            retries=0,
            max_run_seconds=None,
            capability_profile="benchmark_symbolic_v1",
            service_tier=service_tier,
        ),
        model_options=[
            ModelOptionView(
                model=option.model,
                display_name=option.display_name,
                is_default=option.is_default,
                default_effort=option.default_effort,
                supported_efforts=list(option.supported_efforts),
                default_service_tier=option.default_service_tier,
                supported_service_tiers=list(option.supported_service_tiers),
            )
            for option in catalog.options
        ],
        model_catalog_source=catalog.source,
        model_catalog_refreshed_at=catalog.refreshed_at,
        allowed_models=list(catalog.models),
        allowed_efforts=list(catalog.efforts),
    )


def fake_create_run_defaults() -> CreateRunDefaultsView:
    return _create_run_defaults(
        provider="fake",
        model="deterministic",
        effort="none",
        service_tier="standard",
        backend_name="deterministic-fake-runtime",
        backend_version="1",
        catalog=fake_catalog(),
    )


def _product_create_run_defaults(catalog: ModelCatalog) -> CreateRunDefaultsView:
    default, effort, service_tier = product_default_selection(catalog)
    return _create_run_defaults(
        provider="openai",
        model=default.model,
        effort=effort,
        service_tier=service_tier,
        backend_name="codex-app-server",
        backend_version=PINNED_CODEX_VERSION,
        catalog=catalog,
    )


def _step(label: str) -> StepContent:
    return StepContent(
        claim=f"Deterministic claim for {label}",
        why=f"The hermetic fixture advances {label} without a network or model dependency.",
        source=f"Deterministic runtime script entry {label}.",
        derivation=f"Apply the fixed transition for {label}, seal it, and verify its hash.",
        scope="Application integration verification only; this is not a scientific conclusion.",
    )


def _writer(
    label: str,
    decision: WriterDecision,
    alternatives: tuple[BranchAlternative, ...] = (),
) -> WriterOutput:
    return WriterOutput(
        content=_step(label),
        control=WriterControl(decision=decision, alternatives=alternatives),
        finish_reason="stop",
        usage=Usage({"input_tokens": 1, "output_tokens": 1}),
    )


def deterministic_runtime(config: RunConfig) -> DeterministicFakeRuntime:
    """Create the multi-route fake used by the real service adapter.

    It deliberately rejects configurations claiming a non-fake provider so a
    hermetic UI run cannot masquerade as a ChatGPT/App Server result.
    """

    providers = {config.writer.provider, config.checker.provider, config.judge.provider}
    if providers != {"fake"} or config.backend_name != "deterministic-fake-runtime":
        raise RuntimeInvariantError(
            "deterministic_runtime requires provider='fake' for all roles and "
            "backend.name='deterministic-fake-runtime'"
        )

    outputs: dict[tuple[str, int], WriterOutput] = {
        ("br_0001", 1): _writer("root step 1", WriterDecision.CONTINUE),
        ("br_0001", 2): _writer(
            "root step 2",
            WriterDecision.FORK,
            (
                BranchAlternative(
                    "Evaluate the same invariant through a symmetry route."
                ),
            ),
        ),
        ("br_0001", 3): _writer("root step 3", WriterDecision.COMPLETE),
        ("br_0002", 3): _writer("model alternative step 3", WriterDecision.COMPLETE),
    }
    for branch_number in range(3, 65):
        for step_slot in range(1, 65):
            outputs[(f"br_{branch_number:04d}", step_slot)] = _writer(
                f"human branch {branch_number} step {step_slot}",
                WriterDecision.COMPLETE,
            )

    def check(_: Any) -> CheckOutput:
        return CheckOutput(
            verdict="ok",
            reason="The deterministic five-field step satisfies the integration fixture.",
            evidence=(),
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )

    def judge(_: Any) -> JudgeOutput:
        return JudgeOutput(
            verdict="pass",
            reason="The deterministic candidate reached its bounded fixture objective.",
            score=1.0,
            finish_reason="stop",
            usage=Usage({"input_tokens": 1, "output_tokens": 1}),
        )

    return DeterministicFakeRuntime(
        writer_outputs=outputs,
        check_factory=check,
        judge_factory=judge,
    )


def deterministic_runtime_factory(
    config: RunConfig,
    _evidence_directory: Path,
) -> DeterministicFakeRuntime:
    """Async-compatible service factory without allocating external resources."""

    return deterministic_runtime(config)


async def _prime_rate_limit_cache(client: AppServerClient) -> None:
    """Best-effort quota bootstrap; usage display cannot block scientific work."""

    try:
        await client.account_rate_limits_read()
    except (OSError, RuntimeError):
        pass


def _schedule_rate_limit_cache_prime(client: AppServerClient) -> None:
    asyncio.create_task(
        _prime_rate_limit_cache(client),
        name="derivationlab-rate-limit-prime",
    )


@dataclass(frozen=True)
class ProductRuntimeFactory:
    """Create one fully authorized App Server lease for a concrete run."""

    profile: ProductProfile
    repo_root: Path
    platform: PlatformFamily
    architecture: str
    app_server_executable: Path
    user_home: Path
    evidence_root: Path | None = None
    capability_profile: CapabilityProfile = BENCHMARK_SYMBOLIC_V1
    client_timeouts: ClientTimeouts = field(default_factory=ClientTimeouts)
    rate_limit_observer: Callable[[dict[str, Any], bool], None] | None = None
    rate_limit_refresh_claim: Callable[[], bool] | None = None
    host_run_lease_path: Path | None = None
    channel: str = "development"
    # When set, every run borrows one isolated session from this already-locked
    # holder instead of starting (and locking) its own App Server child.
    holder: SharedProfileClientHolder | None = None

    def __post_init__(self) -> None:
        if not self.repo_root.is_absolute():
            raise ValueError("product repo_root must be absolute")
        if not self.app_server_executable.is_absolute():
            raise ValueError("product App Server executable must be absolute")
        if not self.user_home.is_absolute():
            raise ValueError("product user_home must be absolute")
        if (
            self.host_run_lease_path is not None
            and not self.host_run_lease_path.is_absolute()
        ):
            raise ValueError("host_run_lease_path must be absolute")
        if self.evidence_root is None:
            object.__setattr__(self, "evidence_root", self.repo_root / "runs")
        assert self.evidence_root is not None
        if not self.evidence_root.is_absolute():
            raise ValueError("product evidence_root must be absolute")
        if self.holder is not None and self.host_run_lease_path is not None:
            raise ValueError(
                "a host-exclusive scientific run lease cannot share an App Server child"
            )
        provision_product_profile(self.profile, repo_root=self.repo_root)

    async def __call__(
        self, config: RunConfig, evidence_directory: Path
    ) -> ModelRuntime:
        mapping = prepare_run_workspace(
            self.profile,
            run_id=config.run_id,
            evidence_directory=evidence_directory,
            repo_root=self.repo_root,
            evidence_root=self.evidence_root,
        )
        source_library = None
        writer_preparation = None
        capability_profile = self.capability_profile
        if config.record_version == "1.1":
            from .problem_sources import prepare_run_sources

            if config.generation_context_sha256 is not None:
                raise RuntimeInvariantError(
                    "reader-assisted writer preparation is not available in this build"
                )
            capability_profile = SOURCE_READING_V1
            source_library = prepare_run_sources(
                config,
                self.repo_root,
                evidence_directory,
                mapping.sandbox_workspace,
            )
        if self.holder is not None:
            if capability_profile != self.holder.capability_profile:
                raise RuntimeInvariantError(
                    "run capability profile differs from the shared App Server holder"
                )
            prepared, session = await self.holder.acquire_run(
                run_mapping=mapping,
                dynamic_tool_audit_path=evidence_directory / "dynamic_tool_calls.jsonl",
            )
            return await create_shared_product_runtime(
                config=config,
                prepared=prepared,
                client=self.holder.client,
                session=session,
                **(
                    {"source_library": source_library}
                    if source_library is not None
                    else {}
                ),
                **(
                    {"writer_preparation": writer_preparation}
                    if writer_preparation is not None
                    else {}
                ),
            )
        lock = ProfileInstanceLock(self.profile).acquire()
        client: AppServerClient | None = None
        host_lease: HostScientificRunLease | None = None
        try:
            prepared = prepare_product_launch(
                self.profile,
                lock=lock,
                repo_root=self.repo_root,
                platform=self.platform,
                architecture=self.architecture,
                app_server_executable=self.app_server_executable,
                workspace_mapping=mapping,
                repository=self.repo_root,
                user_home=self.user_home,
                capability_profile=capability_profile,
                evidence_root=self.evidence_root,
            )
            client = AppServerClient(
                prepared.command.argv,
                cwd=prepared.command.cwd,
                env=prepared.command.environment,
                timeouts=self.client_timeouts,
                dynamic_tool_audit_path=evidence_directory / "dynamic_tool_calls.jsonl",
                **(
                    {"rate_limit_observer": self.rate_limit_observer}
                    if self.rate_limit_observer is not None
                    else {}
                ),
            )
            await client.start()
            if self.host_run_lease_path is not None:
                host_lease = HostScientificRunLease.acquire(
                    self.host_run_lease_path,
                    channel=self.channel,
                    run_id=config.run_id,
                )
            if self.rate_limit_observer is not None and (
                self.rate_limit_refresh_claim is None or self.rate_limit_refresh_claim()
            ):
                _schedule_rate_limit_cache_prime(client)
            runtime = await create_product_runtime(
                config=config,
                prepared=prepared,
                client=client,
                **(
                    {"source_library": source_library}
                    if source_library is not None
                    else {}
                ),
                **(
                    {"writer_preparation": writer_preparation}
                    if writer_preparation is not None
                    else {}
                ),
            )
            return (
                HostLeasedRuntime(runtime, host_lease)
                if host_lease is not None
                else runtime
            )
        except BaseException:
            if lock.runtime_owned:
                # create_product_runtime transferred cleanup authority to either
                # an AuthorizedRuntimeLease or RejectedClientCleanup. That owner
                # must retain the lock until its exact child exit is confirmed.
                if host_lease is not None:
                    host_lease.retain_after_cleanup_failure()
                raise
            if client is not None and client.is_running:
                await client.close()
            if lock.held:
                lock.release()
            if host_lease is not None:
                host_lease.release()
            raise


@dataclass(frozen=True)
class ProductIntakeTurnRunner:
    """Run one authorized, tool-free Intake turn with a short-lived client."""

    profile: ProductProfile
    repo_root: Path
    platform: PlatformFamily
    architecture: str
    app_server_executable: Path
    user_home: Path
    model_provider: str = "openai"
    model: str = DEFAULT_PRODUCT_MODEL
    effort: str = DEFAULT_PRODUCT_EFFORT
    service_tier: str = DEFAULT_PRODUCT_SERVICE_TIER
    client_timeouts: ClientTimeouts = field(default_factory=ClientTimeouts)
    rate_limit_observer: Callable[[dict[str, Any], bool], None] | None = None
    rate_limit_refresh_claim: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if not self.repo_root.is_absolute():
            raise ValueError("product repo_root must be absolute")
        if not self.app_server_executable.is_absolute():
            raise ValueError("product App Server executable must be absolute")
        if not self.user_home.is_absolute():
            raise ValueError("product user_home must be absolute")
        provision_product_profile(self.profile, repo_root=self.repo_root)

    @staticmethod
    def _remove_workspace(
        profile: ProductProfile, mapping: IntakeWorkspaceMapping
    ) -> None:
        workspace = mapping.sandbox_workspace
        if (
            workspace.parent != profile.workspaces
            or workspace.name != mapping.intake_id
            or not mapping.intake_id.startswith("intake_")
        ):
            raise RuntimeInvariantError(
                "refusing to remove an unbound Intake workspace"
            )
        if workspace.exists():
            shutil.rmtree(workspace)

    async def __call__(
        self,
        developer_instructions: str,
        prompt: str,
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = await self.run_ephemeral(
            developer_instructions=developer_instructions,
            prompt=prompt,
            output_schema=output_schema,
        )
        return result.payload

    async def run_ephemeral(
        self,
        *,
        developer_instructions: str,
        prompt: str,
        output_schema: Mapping[str, Any],
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> StructuredTurnResult:
        """Run an independent tool-free checker turn and return its receipt."""

        return await self._run(
            intake_id=f"intake_{uuid.uuid4().hex}",
            developer_instructions=developer_instructions,
            prompt=prompt,
            output_schema=output_schema,
            persistent_thread=False,
            resume_thread_id=None,
            remove_workspace=True,
            model=model,
            effort=effort,
            service_tier=service_tier,
        )

    async def run_round(
        self,
        *,
        intake_id: str,
        developer_instructions: str,
        prompt: str,
        output_schema: Mapping[str, Any],
        resume_thread_id: str | None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> StructuredTurnResult:
        """Run one persistent Intake round without holding resources while idle."""

        return await self._run(
            intake_id=intake_id,
            developer_instructions=developer_instructions,
            prompt=prompt,
            output_schema=output_schema,
            persistent_thread=True,
            resume_thread_id=resume_thread_id,
            remove_workspace=False,
            model=model,
            effort=effort,
            service_tier=service_tier,
        )

    def remove_session_workspace(self, intake_id: str) -> None:
        """Remove one terminal Intake workspace after the store is safely frozen."""

        workspace = self.profile.workspaces / intake_id
        mapping = IntakeWorkspaceMapping(
            intake_id=intake_id,
            sandbox_workspace=workspace,
            runtime_temp=workspace / ".runtime_tmp",
        )
        self._remove_workspace(self.profile, mapping)

    async def list_models(self) -> tuple[Mapping[str, Any], ...]:
        """Read the live model catalog from a short-lived App Server child."""

        intake_id = f"intake_catalog_{uuid.uuid4().hex}"
        lock = ProfileInstanceLock(self.profile).acquire()
        mapping: IntakeWorkspaceMapping | None = None
        client: AppServerClient | None = None
        start_completed = False
        try:
            mapping = prepare_intake_workspace(
                self.profile, intake_id=intake_id, repo_root=self.repo_root
            )
            prepared = prepare_product_launch(
                self.profile,
                lock=lock,
                repo_root=self.repo_root,
                platform=self.platform,
                architecture=self.architecture,
                app_server_executable=self.app_server_executable,
                workspace_mapping=mapping,
                repository=self.repo_root,
                user_home=self.user_home,
                capability_profile=INTAKE_V1,
            )
            client = AppServerClient(
                prepared.command.argv,
                cwd=prepared.command.cwd,
                env=prepared.command.environment,
                timeouts=self.client_timeouts,
                **(
                    {"rate_limit_observer": self.rate_limit_observer}
                    if self.rate_limit_observer is not None
                    else {}
                ),
            )
            await client.start()
            start_completed = True
            return await client.model_list()
        finally:
            if client is not None and client.is_running:
                await client.close()
            if (
                start_completed
                and client is not None
                and (client.is_running or client.returncode is None)
            ):
                raise RuntimeInvariantError(
                    "model catalog App Server process exit is unconfirmed"
                )
            if lock.held:
                lock.release()
            if mapping is not None:
                self._remove_workspace(self.profile, mapping)

    async def _run(
        self,
        *,
        intake_id: str,
        developer_instructions: str,
        prompt: str,
        output_schema: Mapping[str, Any],
        persistent_thread: bool,
        resume_thread_id: str | None,
        remove_workspace: bool,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> StructuredTurnResult:
        selected_model = self.model if model is None else model
        selected_effort = self.effort if effort is None else effort
        selected_service_tier = (
            self.service_tier if service_tier is None else service_tier
        )
        lock = ProfileInstanceLock(self.profile).acquire()
        mapping: IntakeWorkspaceMapping | None = None
        client: AppServerClient | None = None
        start_completed = False
        try:
            mapping = prepare_intake_workspace(
                self.profile,
                intake_id=intake_id,
                repo_root=self.repo_root,
            )
            prepared = prepare_product_launch(
                self.profile,
                lock=lock,
                repo_root=self.repo_root,
                platform=self.platform,
                architecture=self.architecture,
                app_server_executable=self.app_server_executable,
                workspace_mapping=mapping,
                repository=self.repo_root,
                user_home=self.user_home,
                capability_profile=INTAKE_V1,
            )
            client = AppServerClient(
                prepared.command.argv,
                cwd=prepared.command.cwd,
                env=prepared.command.environment,
                timeouts=self.client_timeouts,
                **(
                    {"rate_limit_observer": self.rate_limit_observer}
                    if self.rate_limit_observer is not None
                    else {}
                ),
            )
            await client.start()
            start_completed = True
            if self.rate_limit_observer is not None and (
                self.rate_limit_refresh_claim is None or self.rate_limit_refresh_claim()
            ):
                _schedule_rate_limit_cache_prime(client)
            authorization = await collect_and_authorize_intake_turn(
                prepared,
                model_provider=self.model_provider,
                model=selected_model,
                effort=selected_effort,
                client=client,
            )
            result = await run_app_server_structured_turn(
                client,
                StructuredTurnSpec(
                    workspace=str(authorization.settings.workspace),
                    permission_profile=authorization.settings.permission_profile,
                    model_provider=self.model_provider,
                    model=selected_model,
                    effort=selected_effort,
                    service_tier=selected_service_tier,
                    developer_instructions=developer_instructions,
                    prompt=prompt,
                    output_schema=output_schema,
                    thread_config=authorization.settings.thread_config,
                    persistent_thread=persistent_thread,
                    resume_thread_id=resume_thread_id,
                    timeout_seconds=authorization.settings.turn_timeout,
                ),
            )
            return result
        finally:
            if client is not None and client.is_running:
                await client.close()
            if (
                start_completed
                and client is not None
                and (client.is_running or client.returncode is None)
            ):
                # An unconfirmed child exit must retain the profile lock and
                # workspace for manual diagnosis, matching Run cleanup policy.
                raise RuntimeInvariantError(
                    "Intake App Server process exit is unconfirmed"
                )
            if lock.held:
                lock.release()
            if mapping is not None and remove_workspace:
                self._remove_workspace(self.profile, mapping)


@dataclass(frozen=True)
class ProductModelCatalogLoader:
    """Refresh from App Server, falling back only to a validated private cache."""

    runner: ProductIntakeTurnRunner
    cache: ModelCatalogCache

    async def __call__(self) -> ModelCatalog:
        try:
            catalog = catalog_from_app_server_rows(await self.runner.list_models())
            self.cache.store(catalog)
            return catalog
        except (OSError, RuntimeError, ValueError) as live_error:
            try:
                return self.cache.load()
            except (OSError, RuntimeError, ValueError) as cache_error:
                raise RuntimeError(
                    "App Server model catalog and last-known-good cache are unavailable; "
                    f"cache_error={type(cache_error).__name__}"
                ) from live_error


def create_http_app(
    service: RuntimeDerivationService | None,
    *,
    settings: ApiSettings | None = None,
    web_dist: str | Path | None = None,
    site_identity: SiteIdentityStore | None = None,
    service_resolver: TenantRequestServiceResolver | None = None,
    admin_content_reader: FilesystemTenantContentReader | None = None,
) -> FastAPI:
    """Create the local API and optionally mount an already-built web bundle."""

    app = create_api_app(
        service,
        settings=settings,
        site_identity=site_identity,
        service_resolver=service_resolver,
        admin_content_reader=admin_content_reader,
    )
    if web_dist is not None:
        directory = Path(web_dist).resolve()
        if not directory.is_dir():
            raise ValueError(f"web_dist is not a directory: {directory}")
        app.mount(
            "/", StaticFiles(directory=directory, html=True), name="derivation-web"
        )
    return app


def _deterministic_intake_question(
    decision_id: str,
    *,
    title: str,
    prompt: str,
) -> IntakeQuestion:
    return IntakeQuestion(
        question_id=f"question-{decision_id}",
        decision_id=decision_id,
        semantic_key=f"semantic-{decision_id}",
        title=title,
        prompt=prompt,
        why_needed="The fixture treats this as an independent scientific blocker.",
        why_it_matters="The fixture derivation changes shape with this answer.",
        answer_mode=AnswerMode.CHOICE_WITH_TEXT,
        decision_class=DecisionClass.PROBLEM,
        options=(
            QuestionOption(
                option_id="recommended",
                label="Use the documented default",
                impact="Keep the fixture deterministic and reproducible.",
            ),
        ),
        recommended_option_ids=("recommended",),
        recommendation_reason="This option is stable across fixture runs.",
        allow_custom=True,
    )


_DETERMINISTIC_DEFAULTS = (
    DeclaredDefault(
        default_id="unit-system",
        decision_class=DecisionClass.CONVENTION,
        title="Unit system",
        statement="Work in SI units throughout.",
        rationale="Interchangeable with Gaussian units; declared to stay explicit.",
        alternatives=("Gaussian units",),
    ),
    DeclaredDefault(
        default_id="broadening",
        decision_class=DecisionClass.APPROXIMATION_LEVEL,
        title="Line shape",
        statement="Start from an ideal delta-function line shape.",
        rationale="The simplest baseline; broadening is a later ladder rung.",
        alternatives=("Constant broadening", "State-dependent lifetime"),
    ),
)

_DETERMINISTIC_LADDER = (
    LadderRung(
        rung=0,
        name="Textbook-simplest baseline",
        relaxes="Nothing; this is the comparable baseline.",
        default_ids=("unit-system", "broadening"),
    ),
    LadderRung(
        rung=1,
        name="Required deliverable",
        relaxes="Replaces the ideal line shape with a finite broadening.",
        default_ids=("broadening",),
    ),
)


class _DeterministicIntakeSessionAdvisor:
    """Hermetic multi-question fixture behind the real persistent state machine."""

    async def advance(
        self,
        session: IntakeSession,
        *,
        user_submission: Mapping[str, Any],
        visible_event_ids: tuple[str, ...],
        resume_thread_id: str | None,
        mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
        round_number: int = 0,
        max_frontier_rounds: int = 0,
        audit_rejections: int = 0,
    ) -> IntakeRoundProposal:
        del user_submission, round_number, max_frontier_rounds, audit_rejections
        ready = mode is IntakeRoundMode.FINALIZE
        version = len(session.problem_specifications) + 1
        ready = ready or bool(session.decisions)
        sections = tuple(
            ProblemSection(
                name=name,
                content=(
                    "Resolved by the deterministic persistent Intake fixture."
                    if ready
                    else "Drafted from the user's initial scientific request."
                ),
            )
            for name in (
                "purpose",
                "scientific_target",
                "givens_and_starting_point",
                "notation_and_conventions",
                "assumptions_and_regime",
                "scope_and_non_goals",
                "required_output",
                "validation_criteria",
                "agent_discretion",
            )
        )
        questions = (
            ()
            if ready
            else (
                _deterministic_intake_question(
                    "convention",
                    title="Convention",
                    prompt="Which sign and Fourier convention should be used?",
                ),
                _deterministic_intake_question(
                    "output-form",
                    title="Output form",
                    prompt="Which final form and intermediate detail are required?",
                ),
            )
        )
        return IntakeRoundProposal(
            public_summary=(
                "The deterministic specification is ready."
                if ready
                else "Two independent decisions remain."
            ),
            problem_specification=ProblemSpecification(
                version=version,
                supersedes_version=version - 1 or None,
                status=(
                    ProblemSpecificationStatus.CANDIDATE_READY
                    if ready
                    else ProblemSpecificationStatus.DRAFT
                ),
                sections=sections,
                critical_message_refs=(visible_event_ids[-1],),
                declared_defaults=_DETERMINISTIC_DEFAULTS if ready else (),
                refinement_ladder=_DETERMINISTIC_LADDER if ready else (),
            ),
            questions=questions,
            reopen_requests=(),
            candidate_ready=ready,
            thread_id=resume_thread_id or f"thread-{session.session_id}",
            turn_id=f"turn-{version}",
            created_thread=resume_thread_id is None,
        )

    async def rehydrate(
        self,
        session: IntakeSession,
        *,
        visible_event_ids: tuple[str, ...],
    ) -> ThreadReplacementReceipt:
        del visible_event_ids
        return ThreadReplacementReceipt(
            thread_id=f"thread-{session.session_id}-replacement",
            turn_id="turn-rehydrate",
            canonical_state_sha256=intake_session_fingerprint(session),
        )


class _DeterministicSpecificationAuditor:
    async def audit(
        self,
        specification: ProblemSpecification,
        *,
        initial_problem: str = "",
        decision_log: Sequence[Mapping[str, Any]] = (),
        mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
        session_id: str | None = None,
    ) -> SpecificationAudit:
        del initial_problem, decision_log, mode, session_id
        return SpecificationAudit(
            passed=True,
            public_summary="The deterministic fixture audit passed.",
            blocking_questions=(),
            thread_id="thread-fixture-audit",
            turn_id=f"turn-audit-{specification.version}",
        )


def _fake_intake_service(run_root: str | Path) -> PersistentIntakeSessionService:
    root = Path(run_root).resolve().parent / "intakes"
    return PersistentIntakeSessionService(
        store=SQLiteIntakeStore(root / "control.sqlite"),
        advisor=_DeterministicIntakeSessionAdvisor(),  # type: ignore[arg-type]
        auditor=_DeterministicSpecificationAuditor(),
    )


def create_fake_service(
    *,
    run_root: str | Path,
    storage_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    run_id_factory: RunIdFactory | None = None,
    code_commit: str = "0" * 40,
    report_exporter: ReportExporter | None = None,
    build_info: BuildInfoView | None = None,
) -> RuntimeDerivationService:
    """Build the deterministic service while exercising the real integration."""

    return RuntimeDerivationService(
        run_root=run_root,
        storage_root=storage_root,
        archive_root=archive_root,
        repo_root=repo_root,
        runtime_factory=deterministic_runtime_factory,
        intake_session_service=_fake_intake_service(run_root),
        code_commit=code_commit,
        credential_profile_id="fake-profile",
        run_id_factory=run_id_factory,
        service_identity="derivation-runtime-adapter-fake",
        preserve_provider_handles_on_start=False,
        report_exporter=report_exporter,
        build_info=build_info,
        account_service=DeterministicAccountService(),
        create_run_defaults=fake_create_run_defaults(),
    )


def create_product_service(
    *,
    run_root: str | Path,
    storage_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    repo_root: str | Path,
    profile: ProductProfile,
    platform: PlatformFamily,
    architecture: str,
    app_server_executable: str | Path,
    user_home: str | Path,
    credential_profile_id: str,
    code_commit: str,
    build_info: BuildInfoView | None = None,
    run_id_factory: RunIdFactory | None = None,
    report_exporter: ReportExporter | None = None,
    intake_root: str | Path | None = None,
    allow_existing_account_import: bool = True,
    enable_default_archive: bool = True,
    host_run_lease_path: str | Path | None = None,
    channel: str = "development",
    shared_client_holder: SharedProfileClientHolder | None = None,
) -> RuntimeDerivationService:
    """Build the real FastAPI service over the reviewed App Server boundary."""

    repository = Path(repo_root).resolve()
    evidence_root = Path(run_root).resolve()
    product_storage_root = Path(storage_root or (repository / "runs")).resolve()
    product_archive_root = (
        Path(archive_root).resolve()
        if archive_root is not None
        else (repository / "runs").resolve()
        if enable_default_archive
        else None
    )
    rate_limit_store = AccountRateLimitStore()
    runtime_factory = ProductRuntimeFactory(
        profile=profile,
        repo_root=repository,
        platform=platform,
        architecture=architecture,
        # Preserve the configured launcher path; the client resolves it once
        # for a portable child-process consistency check.
        app_server_executable=Path(app_server_executable).absolute(),
        user_home=Path(user_home).resolve(),
        evidence_root=evidence_root,
        rate_limit_observer=rate_limit_store.observe,
        rate_limit_refresh_claim=rate_limit_store.claim_provider_refresh,
        host_run_lease_path=(
            Path(host_run_lease_path).resolve()
            if host_run_lease_path is not None
            else None
        ),
        channel=channel,
        holder=shared_client_holder,
    )
    intake_runner = ProductIntakeTurnRunner(
        profile=profile,
        repo_root=repository,
        platform=platform,
        architecture=architecture,
        app_server_executable=Path(app_server_executable).absolute(),
        user_home=Path(user_home).resolve(),
        rate_limit_observer=rate_limit_store.observe,
        rate_limit_refresh_claim=rate_limit_store.claim_provider_refresh,
    )
    catalog_loader = ProductModelCatalogLoader(
        runner=intake_runner,
        cache=ModelCatalogCache(profile.runtime / "model-catalog-v1.json"),
    )
    intake_session_service = PersistentIntakeSessionService(
        store=SQLiteIntakeStore(
            Path(intake_root or (profile.root / "intakes")).resolve() / "control.sqlite"
        ),
        advisor=AppServerIntakeSessionAdvisor(intake_runner),
        auditor=AppServerProblemSpecificationAuditor(intake_runner),
    )
    account_backend = CodexAccountBackend(
        repo_root=repository,
        platform=platform,
        architecture=architecture,
        app_server_executable=Path(app_server_executable).absolute(),
        user_home=Path(user_home).resolve(),
        rate_limit_observer=rate_limit_store.observe,
        rate_limit_refresh_claim=rate_limit_store.claim_provider_refresh,
    )
    account_service = ProductAccountManager(
        profile=profile,
        repo_root=repository,
        source_auth=(
            Path(user_home).resolve() / ".codex" / "auth.json"
            if allow_existing_account_import
            else None
        ),
        backend=account_backend,
        rate_limit_store=rate_limit_store,
    )
    return RuntimeDerivationService(
        run_root=run_root,
        storage_root=product_storage_root,
        archive_root=product_archive_root,
        repo_root=repository,
        runtime_factory=runtime_factory,
        intake_session_service=intake_session_service,
        intake_workspace_cleanup=intake_runner.remove_session_workspace,
        code_commit=code_commit,
        build_info=build_info,
        account_service=account_service,
        credential_profile_id=credential_profile_id,
        run_id_factory=run_id_factory,
        service_identity="derivation-runtime-adapter",
        preserve_provider_handles_on_start=True,
        report_exporter=report_exporter,
        create_run_defaults=_product_create_run_defaults(fixture_catalog()),
        formula_validation_policy="formula-v2",
        model_catalog_loader=catalog_loader,
    )


@dataclass(frozen=True)
class ProductTenantServiceFactory:
    """Lazily assemble one credential and data boundary per website user."""

    repo_root: Path
    platform: PlatformFamily
    architecture: str
    app_server_executable: Path
    user_home: Path
    code_commit: str
    build_info: BuildInfoView
    uv_executable: str = "uv"
    host_run_lease_path: Path | None = None
    channel: str = "development"

    async def __call__(self, paths: TenantPaths) -> RuntimeDerivationService:
        profile = paths.product_profile

        def prepare_profile() -> None:
            validation = provision_product_profile(profile, repo_root=self.repo_root)
            if validation.scientific_runtime is None:
                provision_scientific_runtime(
                    profile.runtime,
                    uv_executable=self.uv_executable,
                )

        await asyncio.to_thread(prepare_profile)
        return create_product_service(
            run_root=paths.run_root,
            storage_root=paths.root,
            archive_root=paths.archive_root,
            repo_root=self.repo_root,
            profile=profile,
            platform=self.platform,
            architecture=self.architecture,
            app_server_executable=self.app_server_executable,
            user_home=self.user_home,
            credential_profile_id=f"derivationlab-site-{paths.user_id}",
            code_commit=self.code_commit,
            build_info=self.build_info,
            intake_root=paths.intake_root,
            allow_existing_account_import=False,
            enable_default_archive=False,
            host_run_lease_path=self.host_run_lease_path,
            channel=self.channel,
        )


def _require_disjoint_server_roots(*roots: Path) -> None:
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise ValueError(
            "server state, profile, and host control roots must not overlap"
        )


def create_product_server_app(
    *,
    server_root: str | Path,
    server_profile_root: str | Path,
    host_control_root: str | Path,
    channel: str,
    repo_root: str | Path,
    platform: PlatformFamily,
    architecture: str,
    app_server_executable: str | Path,
    user_home: str | Path,
    code_commit: str,
    build_info: BuildInfoView,
    settings: ApiSettings,
    web_dist: str | Path | None = None,
    uv_executable: str = "uv",
) -> FastAPI:
    """Create explicit multi-user Server mode without a shared product profile."""

    root = Path(server_root).expanduser().resolve()
    profile_root = Path(server_profile_root).expanduser().resolve()
    control_root = Path(host_control_root).expanduser().resolve()
    _require_disjoint_server_roots(root, profile_root, control_root)
    identity = SiteIdentityStore(root / "identity" / "site-identity.sqlite")
    tenant_factory = ProductTenantServiceFactory(
        repo_root=Path(repo_root).resolve(),
        platform=platform,
        architecture=architecture,
        app_server_executable=Path(app_server_executable).absolute(),
        user_home=Path(user_home).resolve(),
        code_commit=code_commit,
        build_info=build_info,
        host_run_lease_path=control_root / "scientific-run.lock",
        channel=channel,
        uv_executable=uv_executable,
    )
    registry = TenantRuntimeRegistry(
        root / "tenants",
        tenant_factory,
        profiles_root=profile_root,
    )
    resolver = TenantRequestServiceResolver(registry, build_info=build_info)
    content_reader = FilesystemTenantContentReader(root / "tenants")
    return create_http_app(
        None,
        settings=settings,
        web_dist=web_dist,
        site_identity=identity,
        service_resolver=resolver,
        admin_content_reader=content_reader,
    )


def create_product_app(
    *,
    run_root: str | Path,
    storage_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    repo_root: str | Path,
    profile: ProductProfile,
    platform: PlatformFamily,
    architecture: str,
    app_server_executable: str | Path,
    user_home: str | Path,
    credential_profile_id: str,
    code_commit: str,
    build_info: BuildInfoView | None = None,
    run_id_factory: RunIdFactory | None = None,
    settings: ApiSettings | None = None,
    web_dist: str | Path | None = None,
    report_exporter: ReportExporter | None = None,
) -> FastAPI:
    service = create_product_service(
        run_root=run_root,
        storage_root=storage_root,
        archive_root=archive_root,
        repo_root=repo_root,
        profile=profile,
        platform=platform,
        architecture=architecture,
        app_server_executable=app_server_executable,
        user_home=user_home,
        credential_profile_id=credential_profile_id,
        code_commit=code_commit,
        build_info=build_info,
        run_id_factory=run_id_factory,
        report_exporter=report_exporter,
    )
    return create_http_app(service, settings=settings, web_dist=web_dist)


def create_fake_app(
    *,
    run_root: str | Path,
    storage_root: str | Path | None = None,
    archive_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    run_id_factory: RunIdFactory | None = None,
    code_commit: str = "0" * 40,
    settings: ApiSettings | None = None,
    web_dist: str | Path | None = None,
    report_exporter: ReportExporter | None = None,
    build_info: BuildInfoView | None = None,
) -> FastAPI:
    service = create_fake_service(
        run_root=run_root,
        storage_root=storage_root,
        archive_root=archive_root,
        repo_root=repo_root,
        run_id_factory=run_id_factory,
        code_commit=code_commit,
        report_exporter=report_exporter,
        build_info=build_info,
    )
    return create_http_app(service, settings=settings, web_dist=web_dist)


__all__ = [
    "ProductRuntimeFactory",
    "ProductTenantServiceFactory",
    "create_fake_app",
    "create_fake_service",
    "create_http_app",
    "create_product_app",
    "create_product_server_app",
    "create_product_service",
    "deterministic_runtime",
    "deterministic_runtime_factory",
]
