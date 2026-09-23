"""Deterministic hermetic service for transport tests and local UI demos only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .models import (
    DEFAULT_MAX_MODEL_CALLS,
    AccountRateLimitsView,
    AccountRateLimitWindowView,
    AccountView,
    ActiveCallOverlay,
    BackendConfig,
    BranchView,
    BudgetView,
    BuildInfoView,
    CapabilitiesView,
    CreateBranchRequest,
    CreateIntakeSessionRequest,
    CreateRunDefaultsView,
    CreateRunRequest,
    DeviceLoginCancelView,
    DeviceLoginStartView,
    DeviceLoginStatusView,
    EdgeView,
    ExportReportRequest,
    FinalizeIntakeSessionRequest,
    FrozenProblemInput,
    FrozenRunConfig,
    HealthView,
    ImportExistingAccountRequest,
    IntakeAnswerInput,
    IntakeConvergenceView,
    IntakeConversationEventView,
    IntakeDecisionAnswerView,
    IntakeDecisionView,
    IntakeDeclaredDefaultView,
    IntakeLadderRungView,
    IntakePendingAnswerView,
    IntakePendingSubmissionView,
    IntakeProblemSectionView,
    IntakeProblemSpecificationView,
    IntakeQuestionOptionView,
    IntakeQuestionView,
    IntakeRevisionRequest,
    IntakeSessionStatusValue,
    IntakeSessionView,
    IntakeThreadGenerationView,
    ProblemPresetsView,
    ProblemSpecificationStatusValue,
    QuitReadinessView,
    ReportBundleView,
    RoleModelConfig,
    RouteView,
    RunCommandCapabilities,
    RunEvent,
    RunPhase,
    RunSummary,
    RuntimeConfig,
    RuntimeOverlay,
    RunView,
    StatusHistoryItem,
    StepChecks,
    StepContent,
    StepProvenance,
    StepView,
    SubmitIntakeRoundRequest,
)
from .service import DerivationServiceError, ErrorKind

BASE_TIME = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)
"""Answer text that makes the fixture fail a round the way a model round can."""
FIXTURE_ROUND_FAILURE = "fixture: fail this round"
PHASES: list[RunPhase] = [
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


def _fixture_round_failure(
    answers: Mapping[str, IntakeAnswerInput],
    user_message: str | None,
) -> bool:
    return user_message == FIXTURE_ROUND_FAILURE or any(
        answer.custom_text == FIXTURE_ROUND_FAILURE for answer in answers.values()
    )


def _fixture_frozen_problem(session: IntakeSessionView) -> FrozenProblemInput:
    specification = session.problem_specifications[-1]
    sections = {section.name: section for section in specification.sections}

    def content(name: str) -> str:
        section = sections[name]
        if section.content is not None:
            return section.content
        return f"Not applicable: {section.not_applicable_reason}"

    accepted_decisions = []
    latest = {decision.decision_id: decision for decision in session.decisions}
    for decision_id in sorted(latest):
        decision = latest[decision_id]
        if decision.status != "resolved" or decision.answer is None:
            continue
        options = {option.option_id: option for option in decision.question.options}
        answer_parts = [
            (
                f"{options[option_id].label} — {options[option_id].impact}"
                if option_id in options
                else option_id
            )
            for option_id in decision.answer.selected_option_ids
        ]
        if decision.answer.custom_text is not None:
            answer_parts.append(decision.answer.custom_text)
        accepted_decisions.append(
            f"{decision.question.title} [{decision.decision_id}]: "
            f"{decision.question.prompt} Accepted answer: " + " | ".join(answer_parts)
        )
    return FrozenProblemInput(
        problem_id=session.session_id,
        version=specification.version,
        supersedes_version=specification.supersedes_version,
        objective=(f"Purpose:\n{content('purpose')}\n\nScientific target:\n{content('scientific_target')}"),
        givens=[
            f"Givens and starting point:\n{content('givens_and_starting_point')}",
            f"Notation and conventions:\n{content('notation_and_conventions')}",
        ],
        assumptions=[
            f"Assumptions and regime:\n{content('assumptions_and_regime')}",
            f"Agent discretion:\n{content('agent_discretion')}",
        ],
        accepted_decisions=accepted_decisions,
        declared_defaults=list(specification.declared_defaults),
        refinement_ladder=list(specification.refinement_ladder),
        scope=content("scope_and_non_goals"),
        deliverable=content("required_output"),
        allowed_tools=["scientific_compute"],
        allowed_references=[],
        success_criteria=[content("validation_criteria")],
        source_pack=None,
        confirmed_by_user=True,
    )


@dataclass(frozen=True)
class IdempotencyEntry:
    fingerprint: str
    response: RunView


@dataclass(frozen=True)
class PausedState:
    previous_phase: RunPhase
    active_calls: tuple[ActiveCallOverlay, ...]
    branch_ids: tuple[str, ...]


@dataclass(frozen=True)
class IntakeIdempotencyEntry:
    fingerprint: str
    response: IntakeSessionView


def _copy_run(run: RunView) -> RunView:
    return run.model_copy(deep=True)


def _fingerprint(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _fake_create_run_defaults() -> CreateRunDefaultsView:
    from derivation_app.model_catalog import fake_catalog

    catalog = fake_catalog()
    role = RoleModelConfig(provider="fake", model="deterministic", effort="none")
    return CreateRunDefaultsView(
        config=FrozenRunConfig(
            granularity="one_task",
            writer=role,
            checker=role,
            judge=role,
            backend=BackendConfig(name="deterministic-fake-runtime", version="1"),
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
            service_tier="standard",
        ),
        model_options=[
            {
                "model": option.model,
                "display_name": option.display_name,
                "is_default": option.is_default,
                "default_effort": option.default_effort,
                "supported_efforts": list(option.supported_efforts),
                "default_service_tier": option.default_service_tier,
                "supported_service_tiers": list(option.supported_service_tiers),
            }
            for option in catalog.options
        ],
        model_catalog_source=catalog.source,
        model_catalog_refreshed_at=catalog.refreshed_at,
        allowed_models=list(catalog.models),
        allowed_efforts=list(catalog.efforts),
    )


class FakeDerivationService:
    """A deterministic service double; never imported by the application factory."""

    def __init__(
        self,
        *,
        auto_complete_on_submit: bool = True,
        subscriber_queue_size: int = 16,
        create_run_defaults: CreateRunDefaultsView | None = None,
        build_info: BuildInfoView | None = None,
    ) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        self.auto_complete_on_submit = auto_complete_on_submit
        self.subscriber_queue_size = subscriber_queue_size
        self.create_run_defaults = (create_run_defaults or _fake_create_run_defaults()).model_copy(deep=True)
        self._build_info = (
            build_info
            or BuildInfoView(
                schema_version="derivationlab-build-info-v1",
                version="dev",
                build_number="0",
                release_id="development",
                commit="0" * 40,
                openapi_sha256="0" * 64,
                product_mode="development",
            )
        ).model_copy(deep=True)
        self._account = AccountView(
            status="signed_in",
            credential_store="file",
            import_available=False,
            diagnostic="ready",
        )
        self._account_imports: dict[str, AccountView] = {}
        self._device_logins: dict[str, DeviceLoginStatusView] = {}
        self._intake_sessions: dict[str, IntakeSessionView] = {}
        self._intake_idempotency: dict[tuple[str, str], IntakeIdempotencyEntry] = {}
        self._intake_counter = 0
        self._intake_event_counter = 0
        self._started = False
        self._lock = asyncio.Lock()
        self._runs: dict[str, RunView] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent | None]]] = {}
        self._idempotency: dict[tuple[str, str], IdempotencyEntry] = {}
        self._pending_revision_ids: dict[str, str] = {}
        self._active_calls: dict[str, list[ActiveCallOverlay]] = {}
        self._paused_states: dict[str, PausedState] = {}
        self._run_counter = 0

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        async with self._lock:
            self._started = False
            for queues in self._subscribers.values():
                for queue in queues:
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)
            self._subscribers.clear()
            self._active_calls.clear()
            self._paused_states.clear()

    async def health(self) -> HealthView:
        return HealthView(status="ok" if self._started else "degraded", service="deterministic-fake")

    async def build_info(self) -> BuildInfoView:
        return self._build_info.model_copy(deep=True)

    async def quit_readiness(self) -> QuitReadinessView:
        active = sum(
            1
            for run in self._runs.values()
            if run.phase in {"submitted", "autonomous_exploration", "human_expansion", "recovering"}
        )
        return QuitReadinessView(safe_to_quit=active == 0, active_run_count=active)

    async def account(self) -> AccountView:
        return self._account.model_copy(deep=True)

    async def account_rate_limits(self) -> AccountRateLimitsView:
        if self._account.status != "signed_in":
            return AccountRateLimitsView(
                status="signed_out",
                windows=[],
                diagnostic="product_auth_missing",
            )
        return AccountRateLimitsView(
            status="available",
            plan_type="plus",
            windows=[
                AccountRateLimitWindowView(
                    kind="five_hour",
                    used_percent=18,
                    remaining_percent=82,
                    window_duration_mins=300,
                    resets_at="2026-09-04T20:00:00Z",
                ),
                AccountRateLimitWindowView(
                    kind="weekly",
                    used_percent=31,
                    remaining_percent=69,
                    window_duration_mins=10_080,
                    resets_at="2026-09-07T19:21:00Z",
                ),
            ],
            observed_at="2026-09-04T16:00:00Z",
            diagnostic="ready",
        )

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView:
        del command
        if idempotency_key is not None and idempotency_key in self._account_imports:
            return self._account_imports[idempotency_key].model_copy(deep=True)
        if self._account.status == "signed_in":
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "account_already_signed_in",
                "The product account is already signed in.",
            )
        self._account = AccountView(
            status="signed_in",
            credential_store="file",
            import_available=False,
            diagnostic="ready",
        )
        if idempotency_key is not None:
            self._account_imports[idempotency_key] = self._account.model_copy(deep=True)
        return self._account.model_copy(deep=True)

    async def start_device_login(self) -> DeviceLoginStartView:
        login_id = f"login-{len(self._device_logins) + 1:04d}"
        self._device_logins[login_id] = DeviceLoginStatusView(status="pending")
        return DeviceLoginStartView(
            login_id=login_id,
            verification_url="https://auth.openai.com/device",
            user_code="FAKE-CODE",
            expires_at="2026-08-29T16:15:00Z",
        )

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView:
        status = self._device_logins.get(login_id)
        if status is None:
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "device_login_not_found",
                "The device login does not exist.",
            )
        return status.model_copy(deep=True)

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView:
        status = self._device_logins.get(login_id)
        if status is None:
            return DeviceLoginCancelView(status="not_found")
        self._device_logins[login_id] = DeviceLoginStatusView(status="canceled")
        return DeviceLoginCancelView(status="canceled")

    async def get_problem_presets(self) -> ProblemPresetsView:
        from derivation_app.problem_sources import build_problem_presets

        return await asyncio.to_thread(build_problem_presets, Path(__file__).resolve().parents[3])

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

    def _next_intake_event(self, kind: str, payload: dict[str, object]) -> IntakeConversationEventView:
        self._intake_event_counter += 1
        return IntakeConversationEventView(
            event_id=f"event-intake-{self._intake_event_counter}",
            kind=kind,
            payload=payload,
        )

    @staticmethod
    def _intake_questions() -> list[IntakeQuestionView]:
        return [
            IntakeQuestionView(
                question_id="question-convention",
                decision_id="convention",
                semantic_key="response-convention",
                title="Response convention",
                prompt="Which sign and Fourier convention should the derivation use?",
                why_needed="Different choices change intermediate signs.",
                why_it_matters="The reported expression carries the opposite sign.",
                answer_mode="single_choice",
                decision_class="problem",
                options=[
                    IntakeQuestionOptionView(
                        option_id="standard-minus",
                        label="exp(-iωt)",
                        impact="Use the common condensed-matter time convention.",
                    ),
                    IntakeQuestionOptionView(
                        option_id="standard-plus",
                        label="exp(+iωt)",
                        impact="Use the opposite time convention consistently.",
                    ),
                ],
                recommended_option_ids=["standard-minus"],
                recommendation_reason="It is the fixture's documented default.",
                allow_custom=True,
                depends_on=[],
                blocking=True,
            ),
            IntakeQuestionView(
                question_id="question-output",
                decision_id="output-form",
                semantic_key="required-output-form",
                title="Required output",
                prompt="What final form and intermediate detail do you need?",
                why_needed="The Writer needs a concrete stopping condition.",
                why_it_matters="It changes which quantity the derivation must deliver.",
                answer_mode="choice_with_text",
                decision_class="problem",
                options=[
                    IntakeQuestionOptionView(
                        option_id="symbolic",
                        label="Symbolic expression",
                        impact="Produce a directly reusable closed-form result.",
                    ),
                    IntakeQuestionOptionView(
                        option_id="derivation-chain",
                        label="Full derivation chain",
                        impact="Show all scientifically meaningful intermediate steps.",
                    ),
                ],
                recommended_option_ids=["derivation-chain"],
                recommendation_reason="The product is designed to expose derivation routes.",
                allow_custom=True,
                depends_on=[],
                blocking=True,
            ),
        ]

    @staticmethod
    def _pending_question() -> IntakeQuestionView:
        """The problem-class decision the stalled fixture parks for the user."""

        return IntakeQuestionView(
            question_id="question-regime",
            decision_id="regime",
            semantic_key="included-physics",
            title="Included physics",
            prompt="Should phonon-assisted transitions be inside the target?",
            why_needed="It decides which physics the derivation must contain.",
            why_it_matters="Including phonons changes the target quantity itself.",
            answer_mode="choice_with_text",
            decision_class="problem",
            options=[
                IntakeQuestionOptionView(
                    option_id="direct-only",
                    label="Direct transitions only",
                    impact="Keep the vertical-transition target.",
                ),
                IntakeQuestionOptionView(
                    option_id="with-phonons",
                    label="Include phonon-assisted transitions",
                    impact="Widen the target to indirect absorption.",
                ),
            ],
            recommended_option_ids=["direct-only"],
            recommendation_reason="It matches the fixture's stated scope.",
            allow_custom=True,
            depends_on=[],
            blocking=True,
            grounded_in="absorption coefficient versus photon energy",
        )

    @staticmethod
    def _declared_defaults() -> list[IntakeDeclaredDefaultView]:
        return [
            IntakeDeclaredDefaultView(
                default_id="unit-system",
                decision_class="convention",
                title="Unit system",
                statement="Work in SI units throughout.",
                rationale="Interchangeable with Gaussian units; declared to stay explicit.",
                alternatives=["Gaussian units"],
            ),
            IntakeDeclaredDefaultView(
                default_id="line-shape",
                decision_class="approximation_level",
                title="Line shape",
                statement="Start from an ideal delta-function line shape.",
                rationale="The simplest baseline; broadening is a later ladder rung.",
                alternatives=["Constant broadening", "State-dependent lifetime"],
            ),
        ]

    @staticmethod
    def _refinement_ladder() -> list[IntakeLadderRungView]:
        return [
            IntakeLadderRungView(
                rung=0,
                name="Textbook-simplest baseline",
                relaxes="Nothing; this is the comparable baseline.",
                default_ids=["unit-system", "line-shape"],
                decision_ids=[],
                parallel_branch=False,
            ),
            IntakeLadderRungView(
                rung=1,
                name="Required deliverable",
                relaxes="Replaces the ideal line shape with a finite broadening.",
                default_ids=["line-shape"],
                decision_ids=["output-form"],
                parallel_branch=False,
            ),
        ]

    @staticmethod
    def _draft_specification(message: str) -> IntakeProblemSpecificationView:
        return IntakeProblemSpecificationView(
            version=1,
            status="draft",
            sections=[
                IntakeProblemSectionView(
                    name="scientific_target",
                    content=message,
                    not_applicable_reason=None,
                )
            ],
            critical_message_refs=[],
            supersedes_version=None,
        )

    @staticmethod
    def _candidate_specification(
        session: IntakeSessionView,
        answers: dict[str, IntakeAnswerInput],
        *,
        status: ProblemSpecificationStatusValue = "candidate_ready",
    ) -> IntakeProblemSpecificationView:
        target = session.problem_specifications[-1].sections[0].content or "Derive the requested result."
        answer_text = {}
        for key, value in answers.items():
            parts = [*value.selected_option_ids]
            if value.custom_text:
                parts.append(value.custom_text)
            if value.strategy is not None:
                parts.append(f"strategy: {value.strategy}")
            answer_text[key] = "; ".join(parts)
        values = {
            "purpose": "Produce an auditable scientific derivation for the user.",
            "scientific_target": target,
            "givens_and_starting_point": "Use the definitions stated in the initial request.",
            "notation_and_conventions": answer_text["convention"],
            "assumptions_and_regime": "Do not add unconfirmed physical approximations.",
            "scope_and_non_goals": "Stay within the scientific target supplied by the user.",
            "required_output": answer_text["output-form"],
            "validation_criteria": "Check dimensions, signs, limits, and internal consistency.",
            "agent_discretion": "The agent may choose non-critical algebraic organization.",
        }
        return IntakeProblemSpecificationView(
            version=2,
            status=status,
            sections=[
                IntakeProblemSectionView(
                    name=name,
                    content=content,
                    not_applicable_reason=None,
                )
                for name, content in values.items()
            ],
            critical_message_refs=[],
            supersedes_version=1,
            declared_defaults=FakeDerivationService._declared_defaults(),
            refinement_ladder=FakeDerivationService._refinement_ladder(),
        )

    def _hold_intake_submission(
        self,
        session: IntakeSessionView,
        answers: Mapping[str, IntakeAnswerInput],
        user_message: str | None,
        kind: Literal["round", "finalize"],
    ) -> None:
        """Deterministic fixture switch onto the failed-round path.

        Submitting FIXTURE_ROUND_FAILURE as the correction message or as any
        answer's custom text fails the round the way a model contract violation
        does: nothing is committed, and the submission is held so a client can
        exercise the pre-filled retry without a live model.
        """

        held = IntakePendingSubmissionView(
            kind=kind,
            base_revision=session.revision,
            submitted_at="2026-01-01T00:00:00+00:00",
            answers={
                decision_id: IntakePendingAnswerView(
                    selected_option_ids=list(answer.selected_option_ids),
                    custom_text=answer.custom_text,
                    strategy=answer.strategy,
                )
                for decision_id, answer in answers.items()
            },
            user_message=user_message,
            failure_reason="ValueError: the fixture round was asked to fail.",
        )
        self._intake_sessions[session.session_id] = session.model_copy(
            update={"pending_submission": held}
        )
        raise DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "intake_command_failed",
            "The persistent AI Intake command failed closed.",
        )

    def _intake_replay(
        self,
        scope: str,
        key: str,
        payload: object,
    ) -> IntakeSessionView | None:
        entry = self._intake_idempotency.get((scope, key))
        if entry is None:
            return None
        if entry.fingerprint != _fingerprint(payload):
            raise DerivationServiceError(
                ErrorKind.IDEMPOTENCY_CONFLICT,
                "intake_idempotency_conflict",
                "The Intake idempotency key was reused with a different command.",
            )
        return entry.response.model_copy(deep=True)

    def _store_intake_receipt(
        self,
        scope: str,
        key: str,
        payload: object,
        response: IntakeSessionView,
    ) -> None:
        self._intake_idempotency[(scope, key)] = IntakeIdempotencyEntry(
            fingerprint=_fingerprint(payload),
            response=response.model_copy(deep=True),
        )

    async def create_intake_session(
        self,
        command: CreateIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        from derivation_app.model_catalog import (
            fake_catalog,
            validate_model_effort,
            validate_model_service_tier,
        )

        try:
            validate_model_effort(
                command.model,
                command.effort,
                catalog=fake_catalog(),
            )
            validate_model_service_tier(
                command.model,
                command.service_tier,
                catalog=fake_catalog(),
            )
        except ValueError as exc:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_model_invalid",
                str(exc),
            ) from exc
        replay = self._intake_replay("create", idempotency_key, command)
        if replay is not None:
            return replay
        self._intake_counter += 1
        session_id = f"intake-{self._intake_counter}"
        user_event = self._next_intake_event("user_message", {"text": command.initial_message})
        questions = self._intake_questions()
        session = IntakeSessionView(
            session_id=session_id,
            revision=1,
            status="active",
            model=command.model,
            effort=command.effort,
            service_tier=command.service_tier,
            problem_specifications=[self._draft_specification(command.initial_message)],
            decisions=[
                IntakeDecisionView(
                    decision_id=question.decision_id,
                    semantic_key=question.semantic_key,
                    status="open",
                    question=question,
                    revision=1,
                    supersedes_revision=None,
                    answer=None,
                    reopen_reason=None,
                    source_message_refs=[user_event.event_id],
                )
                for question in questions
            ],
            frontier=questions,
            thread_generations=[
                IntakeThreadGenerationView(
                    generation=1,
                    status="active",
                    app_server_thread_id=f"thread-{session_id}",
                    prompt_version="intake-grill-v2-fixture",
                    replaced_generation=None,
                    replacement_reason=None,
                )
            ],
            conversation=[
                user_event,
                self._next_intake_event(
                    "assistant_round",
                    {
                        "summary": "Two independent decisions are ready to answer.",
                        "question_ids": [item.question_id for item in questions],
                    },
                ),
            ],
        )
        self._intake_sessions[session_id] = session
        self._store_intake_receipt("create", idempotency_key, command, session)
        return session.model_copy(deep=True)

    async def get_intake_session(self, session_id: str) -> IntakeSessionView:
        session = self._intake_sessions.get(session_id)
        if session is None:
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "intake_session_not_found",
                f"IntakeSession {session_id!r} does not exist.",
            )
        return session.model_copy(deep=True)

    async def list_intake_sessions(self, *, status: IntakeSessionStatusValue) -> list[IntakeSessionView]:
        return [
            session.model_copy(deep=True)
            for session in self._intake_sessions.values()
            if session.status == status
        ]

    async def submit_intake_round(
        self,
        session_id: str,
        command: SubmitIntakeRoundRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        scope = f"round:{session_id}"
        replay = self._intake_replay(scope, idempotency_key, command)
        if replay is not None:
            return replay
        session = await self.get_intake_session(session_id)
        if session.revision != command.base_revision:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_stale_revision",
                "The IntakeSession changed before this round was submitted.",
            )
        if _fixture_round_failure(command.answers, command.user_message):
            self._hold_intake_submission(
                session,
                command.answers,
                command.user_message,
                "round",
            )
        if command.user_message is not None and not command.answers:
            latest = session.problem_specifications[-1]
            revised = latest.model_copy(
                update={
                    "version": latest.version + 1,
                    "status": "candidate_ready",
                    "supersedes_version": latest.version,
                    "sections": [
                        section.model_copy(
                            update={
                                "content": (f"{section.content}\n\nUser correction: {command.user_message}")
                            }
                        )
                        if section.name == "scientific_target"
                        else section
                        for section in latest.sections
                    ],
                }
            )
            corrected = session.model_copy(
                update={
                    "revision": session.revision + 1,
                    "pending_submission": None,
                    "status": "candidate_ready",
                    "problem_specifications": [
                        *[item for item in session.problem_specifications[:-1]],
                        latest.model_copy(update={"status": "superseded"}),
                        revised,
                    ],
                    "conversation": [
                        *session.conversation,
                        self._next_intake_event("user_message", {"text": command.user_message}),
                        self._next_intake_event(
                            "specification_audit",
                            {"passed": True, "summary": "Fixture audit passed."},
                        ),
                    ],
                }
            )
            self._intake_sessions[session_id] = corrected
            self._store_intake_receipt(scope, idempotency_key, command, corrected)
            return corrected.model_copy(deep=True)
        blocking = {item.decision_id for item in session.frontier if item.blocking}
        if not blocking.issubset(command.answers):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_answers_incomplete",
                "Every blocking frontier question requires an answer.",
            )
        event = self._next_intake_event(
            "user_answers",
            {"answers": {key: value.model_dump(mode="json") for key, value in command.answers.items()}},
        )
        if any(value.strategy is not None for value in command.answers.values()):
            return self._stall_intake_locked(session, command, event, scope, idempotency_key)
        decisions = [
            item.model_copy(
                update={
                    "status": "resolved",
                    "revision": item.revision + 1,
                    "supersedes_revision": item.revision,
                    "answer": IntakeDecisionAnswerView(
                        selected_option_ids=command.answers[item.decision_id].selected_option_ids,
                        custom_text=command.answers[item.decision_id].custom_text,
                        source_message_refs=[event.event_id],
                    ),
                    "source_message_refs": [event.event_id],
                }
            )
            for item in session.decisions
        ]
        candidate = session.model_copy(
            update={
                "revision": session.revision + 1,
                "pending_submission": None,
                "status": "candidate_ready",
                "problem_specifications": [
                    session.problem_specifications[0].model_copy(update={"status": "superseded"}),
                    self._candidate_specification(session, command.answers),
                ],
                "decisions": decisions,
                "frontier": [],
                "conversation": [
                    *session.conversation,
                    event,
                    self._next_intake_event(
                        "assistant_round",
                        {"summary": "The specification is ready for confirmation."},
                    ),
                    self._next_intake_event(
                        "specification_audit",
                        {"passed": True, "summary": "Fixture audit passed."},
                    ),
                ],
            }
        )
        self._intake_sessions[session_id] = candidate
        self._store_intake_receipt(scope, idempotency_key, command, candidate)
        return candidate.model_copy(deep=True)

    def _stall_intake_locked(
        self,
        session: IntakeSessionView,
        command: SubmitIntakeRoundRequest,
        event: IntakeConversationEventView,
        scope: str,
        idempotency_key: str,
    ) -> IntakeSessionView:
        """Deterministic fixture switch onto the stalled path.

        Answering any question with a ladder strategy drives this fixture into
        convergence_required so a client can exercise the pending-question page
        and the finalize command without a live model.
        """

        pending = self._pending_question()
        decisions = [
            item.model_copy(
                update={
                    "status": "resolved",
                    "revision": item.revision + 1,
                    "supersedes_revision": item.revision,
                    "answer": IntakeDecisionAnswerView(
                        selected_option_ids=command.answers[item.decision_id].selected_option_ids,
                        custom_text=command.answers[item.decision_id].custom_text,
                        source_message_refs=[event.event_id],
                        strategy=command.answers[item.decision_id].strategy,
                    ),
                    "source_message_refs": [event.event_id],
                }
            )
            for item in session.decisions
        ]
        decisions.append(
            IntakeDecisionView(
                decision_id=pending.decision_id,
                semantic_key=pending.semantic_key,
                status="open",
                question=pending,
                revision=1,
                supersedes_revision=None,
                answer=None,
                reopen_reason=None,
                source_message_refs=[event.event_id],
            )
        )
        latest = session.problem_specifications[-1]
        stalled = session.model_copy(
            update={
                "revision": session.revision + 1,
                "pending_submission": None,
                "status": "convergence_required",
                "problem_specifications": [
                    latest.model_copy(update={"status": "superseded"}),
                    self._candidate_specification(
                        session,
                        command.answers,
                        status="draft",
                    ),
                ],
                "decisions": decisions,
                "frontier": [],
                "pending_problem_questions": [pending],
                "convergence": IntakeConvergenceView(
                    rounds=1,
                    audit_rejections=2,
                    reason="max_audit_rejections",
                    finalized_by_user=False,
                ),
                "conversation": [
                    *session.conversation,
                    event,
                    self._next_intake_event(
                        "specification_audit",
                        {"passed": False, "summary": "Fixture audit rejected twice."},
                    ),
                    self._next_intake_event(
                        "convergence_required",
                        {
                            "reason": "max_audit_rejections",
                            "pending_question_ids": [pending.question_id],
                        },
                    ),
                ],
            }
        )
        self._intake_sessions[session.session_id] = stalled
        self._store_intake_receipt(scope, idempotency_key, command, stalled)
        return stalled.model_copy(deep=True)

    async def finalize_intake_session(
        self,
        session_id: str,
        command: FinalizeIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        scope = f"finalize:{session_id}"
        replay = self._intake_replay(scope, idempotency_key, command)
        if replay is not None:
            return replay
        session = await self.get_intake_session(session_id)
        if session.status != "convergence_required":
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_not_convergence_required",
                "Only a convergence-required IntakeSession can be finalized.",
            )
        if session.revision != command.base_revision:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_stale_revision",
                "The IntakeSession changed before this finalize was submitted.",
            )
        pending = {item.decision_id for item in session.pending_problem_questions}
        if not pending.issubset(command.answers):
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_answers_incomplete",
                "Every pending problem question requires an answer.",
            )
        if _fixture_round_failure(command.answers, None):
            self._hold_intake_submission(session, command.answers, None, "finalize")
        event = self._next_intake_event(
            "finalize_answers",
            {"answers": {key: value.model_dump(mode="json") for key, value in command.answers.items()}},
        )
        decisions = [
            item.model_copy(
                update={
                    "status": "resolved",
                    "revision": item.revision + 1,
                    "supersedes_revision": item.revision,
                    "answer": IntakeDecisionAnswerView(
                        selected_option_ids=command.answers[item.decision_id].selected_option_ids,
                        custom_text=command.answers[item.decision_id].custom_text,
                        source_message_refs=[event.event_id],
                        strategy=command.answers[item.decision_id].strategy,
                    ),
                    "source_message_refs": [event.event_id],
                }
            )
            if item.decision_id in command.answers and item.status == "open"
            else item
            for item in session.decisions
        ]
        latest = session.problem_specifications[-1]
        finalized = session.model_copy(
            update={
                "revision": session.revision + 1,
                "pending_submission": None,
                "status": "candidate_ready",
                "problem_specifications": [
                    *[item for item in session.problem_specifications[:-1]],
                    latest.model_copy(update={"status": "superseded"}),
                    latest.model_copy(
                        update={
                            "version": latest.version + 1,
                            "status": "candidate_ready",
                            "supersedes_version": latest.version,
                        }
                    ),
                ],
                "decisions": decisions,
                "frontier": [],
                "pending_problem_questions": [],
                "convergence": session.convergence.model_copy(update={"finalized_by_user": True}),
                "conversation": [
                    *session.conversation,
                    event,
                    self._next_intake_event(
                        "assistant_round",
                        {"summary": "Finalized with the declared defaults."},
                    ),
                    self._next_intake_event(
                        "specification_audit",
                        {"passed": True, "summary": "Fixture finalize audit passed."},
                    ),
                    self._next_intake_event(
                        "finalize_round",
                        {
                            "rounds": session.convergence.rounds,
                            "audit_rejections": session.convergence.audit_rejections,
                            "finalized_by_user": True,
                        },
                    ),
                ],
            }
        )
        self._intake_sessions[session_id] = finalized
        self._store_intake_receipt(scope, idempotency_key, command, finalized)
        return finalized.model_copy(deep=True)

    async def confirm_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        return self._terminal_intake_command(
            session_id,
            command,
            idempotency_key=idempotency_key,
            action="confirm",
        )

    async def cancel_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView:
        return self._terminal_intake_command(
            session_id,
            command,
            idempotency_key=idempotency_key,
            action="cancel",
        )

    def _terminal_intake_command(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
        action: str,
    ) -> IntakeSessionView:
        scope = f"{action}:{session_id}"
        replay = self._intake_replay(scope, idempotency_key, command)
        if replay is not None:
            return replay
        session = self._intake_sessions.get(session_id)
        if session is None:
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "intake_session_not_found",
                f"IntakeSession {session_id!r} does not exist.",
            )
        if session.revision != command.base_revision:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_stale_revision",
                "The IntakeSession changed before this command was submitted.",
            )
        if action == "confirm" and session.status != "candidate_ready":
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_not_ready",
                "Only a candidate-ready IntakeSession can be confirmed.",
            )
        if action == "cancel" and session.status not in {
            "active",
            "candidate_ready",
        }:
            raise DerivationServiceError(
                ErrorKind.INVALID_STATE,
                "intake_not_resumable",
                "Only a resumable IntakeSession can be cancelled.",
            )
        terminal_status = "confirmed" if action == "confirm" else "cancelled"
        specifications = list(session.problem_specifications)
        if action == "confirm":
            latest = specifications[-1]
            specifications[-1] = latest.model_copy(update={"status": "superseded"})
            specifications.append(
                latest.model_copy(
                    update={
                        "version": latest.version + 1,
                        "status": "confirmed",
                        "supersedes_version": latest.version,
                    }
                )
            )
        terminal = session.model_copy(
            update={
                "revision": session.revision + 1,
                "pending_submission": None,
                "status": terminal_status,
                "problem_specifications": specifications,
                "frontier": [],
                "thread_generations": [
                    item.model_copy(update={"status": "closed"}) if item.status == "active" else item
                    for item in session.thread_generations
                ],
                "conversation": [
                    *session.conversation,
                    self._next_intake_event(
                        f"session_{terminal_status}",
                        (
                            {"problem_specification_version": (specifications[-1].version)}
                            if action == "confirm"
                            else {}
                        ),
                    ),
                ],
            }
        )
        if action == "confirm":
            terminal = terminal.model_copy(update={"frozen_problem": _fixture_frozen_problem(terminal)})
        self._intake_sessions[session_id] = terminal
        self._store_intake_receipt(scope, idempotency_key, command, terminal)
        return terminal.model_copy(deep=True)

    def _timestamp(self, ordinal: int) -> str:
        return (BASE_TIME + timedelta(seconds=ordinal)).isoformat().replace("+00:00", "Z")

    def _lookup_run_locked(self, run_id: str) -> RunView:
        run = self._runs.get(run_id)
        if run is None:
            raise DerivationServiceError(
                ErrorKind.NOT_FOUND,
                "run_not_found",
                f"Derivation run {run_id!r} does not exist.",
            )
        return run

    def _idempotency_lookup_locked(
        self,
        scope: str,
        key: str | None,
        payload: object,
    ) -> RunView | None:
        if key is None:
            return None
        fingerprint = _fingerprint(payload)
        entry = self._idempotency.get((scope, key))
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            raise DerivationServiceError(
                ErrorKind.IDEMPOTENCY_CONFLICT,
                "idempotency_key_reused",
                "The Idempotency-Key was already used with a different command.",
                details={"scope": scope},
            )
        return _copy_run(entry.response)

    def _idempotency_store_locked(
        self,
        scope: str,
        key: str | None,
        payload: object,
        response: RunView,
    ) -> None:
        if key is None:
            return
        self._idempotency[(scope, key)] = IdempotencyEntry(_fingerprint(payload), _copy_run(response))

    def _emit_locked(
        self,
        run: RunView,
        event_type: str,
        *,
        overlay: RuntimeOverlay | None = None,
    ) -> None:
        self._sync_commands_locked(run)
        events = self._events[run.id]
        event_id = len(events) + 1
        run.canonical_event_id = event_id
        run.updated_at = self._timestamp(event_id)
        event = RunEvent(
            event_id=event_id,
            type=event_type,
            run_id=run.id,
            occurred_at=run.updated_at,
            run=_copy_run(run),
            overlay=overlay,
        )
        events.append(event)
        stale: list[asyncio.Queue[RunEvent | None]] = []
        for queue in self._subscribers.get(run.id, set()):
            try:
                queue.put_nowait(event.model_copy(deep=True))
            except asyncio.QueueFull:
                # Force the slow subscriber to reconnect from its last event id.
                queue.get_nowait()
                queue.put_nowait(None)
                stale.append(queue)
        for queue in stale:
            self._subscribers[run.id].discard(queue)

    def _sync_commands_locked(self, run: RunView) -> None:
        active_calls = self._active_calls.get(run.id, [])
        active_branches = sum(branch.status == "active" for branch in run.branches)
        branch_capacity = (
            run.config.max_active_branches is None or active_branches < run.config.max_active_branches
        )
        branchable = (
            [step.revision_id for step in run.steps if step.status == "sealed"]
            if run.phase in {"review_ready", "review_ready_due_to_cap"} and branch_capacity
            else []
        )
        run.read_only = False
        run.commands = RunCommandCapabilities(
            can_pause=(
                run.phase in {"submitted", "autonomous_exploration", "human_expansion"}
                and not run.pause_requested
            ),
            can_resume=run.phase == "paused",
            can_interrupt=bool(active_calls) and not run.hard_interrupt_requested,
            branchable_step_revision_ids=branchable,
        )

    @staticmethod
    def _route_status(branch_status: str) -> str:
        if branch_status == "completed":
            return "complete"
        if branch_status in {"killed", "parked"}:
            return "failed"
        return "active"

    @staticmethod
    def _compatibility_status(phase: RunPhase) -> str:
        if phase in {"review_ready", "review_ready_due_to_cap"}:
            return "review_ready"
        if phase == "paused":
            return "paused"
        if phase == "interrupted":
            return "interrupted"
        if phase == "error":
            return "error"
        return "running"

    def _sync_routes_locked(self, run: RunView) -> None:
        revision_to_node = {step.revision_id: step.id for step in run.steps}
        routes: list[RouteView] = []
        for index, branch in enumerate(run.branches, start=1):
            node_ids = [
                revision_to_node[item] for item in branch.step_revision_ids if item in revision_to_node
            ]
            routes.append(
                RouteView(
                    id=f"route-{branch.branch_id}",
                    label=f"Route {index}",
                    node_ids=node_ids,
                    status=self._route_status(branch.status),
                    branch_id=branch.branch_id,
                    status_history=[item.model_copy(deep=True) for item in branch.status_history],
                )
            )
        run.routes = sorted(routes, key=lambda route: (route.status_history[0].seq, route.branch_id))

    def _seal_fake_step_locked(
        self,
        run: RunView,
        *,
        branch: BranchView,
        instruction: str | None = None,
        anchor: StepView | None = None,
        revision_content: StepContent | None = None,
    ) -> StepView:
        step_number = len(run.steps) + 1
        step_id = f"step-{run.id}-{step_number:03d}"
        revision_id = f"revision-{run.id}-{step_number:03d}"
        if instruction is None:
            claim = "The deterministic fake completed one sealed derivation step."
            why = "This hermetic result verifies autonomous API orchestration without a model call."
            source = "DeterministicFakeDerivationService fixture."
            derivation = "The fixture validates, seals, and publishes a fixed five-field result in one pass."
            scope = "Transport and state-machine verification only; this is not a scientific conclusion."
            title = "Hermetic sealed result"
            input_text = run.question
            content = StepContent(claim=claim, why=why, source=source, derivation=derivation, scope=scope)
            provenance = StepProvenance(
                model=run.config.writer.model,
                thread_id=f"fake-thread-{branch.branch_id}",
                turn_id=f"fake-turn-{run.id}-{step_number:03d}",
                created_at=self._timestamp(len(self._events[run.id]) + 1),
            )
        elif revision_content is None:
            claim = f"A human-requested branch was sealed for instruction: {instruction}"
            why = "The review-ready branch command requested autonomous expansion from a sealed anchor."
            source = f"Human instruction anchored at {anchor.revision_id if anchor else 'unknown'}."
            derivation = "The fake service creates a new immutable child step without modifying its parent."
            scope = "Hermetic branch-control verification only; this is not a scientific conclusion."
            title = "Human expansion result"
            input_text = instruction
            content = StepContent(claim=claim, why=why, source=source, derivation=derivation, scope=scope)
            provenance = StepProvenance(
                model=run.config.writer.model,
                thread_id=f"fake-thread-{branch.branch_id}",
                turn_id=f"fake-turn-{run.id}-{step_number:03d}",
                created_at=self._timestamp(len(self._events[run.id]) + 1),
            )
        else:
            content = revision_content
            title = "Human-authored replacement"
            input_text = instruction
            provenance = None
        content_json = json.dumps(
            content.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output_sha256 = hashlib.sha256(content_json.encode()).hexdigest()
        step = StepView(
            id=step_id,
            revision_id=revision_id,
            branch_id=branch.branch_id,
            order=step_number - 1,
            title=title,
            status="sealed",
            content=content,
            output_sha256=output_sha256,
            input=input_text,
            reasoning_summary=content.derivation,
            output=content.claim,
            checks=StepChecks(
                schema="passed",
                physics="passed" if run.config.checker_enabled else "not_requested",
                provenance="passed",
            ),
            provenance=provenance,
        )
        run.steps.append(step)
        run.budget.used_steps = len(run.steps)
        return step

    async def create_run(
        self,
        command: CreateRunRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView:
        async with self._lock:
            cached = self._idempotency_lookup_locked("create_run", idempotency_key, command)
            if cached is not None:
                return cached
            self._run_counter += 1
            run_id = f"run-{self._run_counter:04d}"
            created_at = self._timestamp(0)
            root_branch = BranchView(
                branch_id=f"branch-{run_id}-001",
                parent_branch_id=None,
                anchor_step_revision_id=None,
                kind="root",
                status="active",
                status_history=[StatusHistoryItem(seq=1, status="active")],
                step_revision_ids=[],
            )
            run = RunView(
                id=run_id,
                question=command.question,
                status="running",
                phase="submitted",
                config=command.config.model_copy(deep=True),
                runtime=command.runtime.model_copy(deep=True),
                root_step_id=None,
                steps=[],
                edges=[],
                branches=[root_branch],
                routes=[],
                budget=BudgetView(used_steps=0, max_steps=command.config.max_model_calls),
                canonical_event_id=0,
                pause_requested=False,
                hard_interrupt_requested=False,
                created_at=created_at,
                updated_at=created_at,
            )
            self._runs[run_id] = run
            self._events[run_id] = []
            self._subscribers[run_id] = set()
            self._active_calls[run_id] = []
            self._emit_locked(run, "run.created")

            run.phase = "autonomous_exploration"
            run.status = "running"
            self._pending_revision_ids[run_id] = f"pending-{run_id}-001"
            root_call = ActiveCallOverlay(
                call_id=f"call-{run_id}-pending",
                branch_id=root_branch.branch_id,
                from_step_id=f"task-{run_id}",
                label="Deterministic fake writer",
            )
            self._active_calls[run_id] = [root_call]
            self._sync_routes_locked(run)
            self._emit_locked(
                run,
                "run.updated",
                overlay=RuntimeOverlay(
                    hard_interrupt_requested=False,
                    active_calls=[root_call.model_copy(deep=True)],
                ),
            )

            if self.auto_complete_on_submit:
                step = self._seal_fake_step_locked(run, branch=root_branch)
                self._pending_revision_ids.pop(run_id, None)
                self._active_calls[run_id] = []
                root_branch.step_revision_ids.append(step.revision_id)
                root_branch.status = "completed"
                root_branch.status_history.append(
                    StatusHistoryItem(seq=len(self._events[run_id]) + 1, status="completed")
                )
                run.root_step_id = step.id
                run.phase = "review_ready"
                run.status = "review_ready"
                self._sync_routes_locked(run)
                empty_overlay = RuntimeOverlay(hard_interrupt_requested=False, active_calls=[])
                self._emit_locked(run, "step.sealed", overlay=empty_overlay)
                self._emit_locked(run, "run.updated", overlay=empty_overlay)

            response = _copy_run(run)
            self._idempotency_store_locked("create_run", idempotency_key, command, response)
            return response

    async def get_run(self, run_id: str) -> RunView:
        async with self._lock:
            run = self._lookup_run_locked(run_id)
            self._sync_commands_locked(run)
            return _copy_run(run)

    async def list_runs(self) -> list[RunSummary]:
        async with self._lock:
            summaries = [
                RunSummary(
                    id=run.id,
                    question=run.question,
                    status=run.status,
                    phase=run.phase,
                    step_count=len(run.steps),
                    route_count=len(run.routes),
                    read_only=False,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
                for run in self._runs.values()
            ]
            return sorted(summaries, key=lambda item: (item.updated_at, item.id), reverse=True)

    async def export_report(
        self,
        run_id: str,
        command: ExportReportRequest,
    ) -> ReportBundleView:
        async with self._lock:
            self._lookup_run_locked(run_id)
        raise DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "report_export_unavailable",
            "The transport-only fake service has no product-managed Tectonic runtime.",
            details={"selected_route_id": command.selected_route_id},
        )

    async def read_report_pdf(self, run_id: str, export_id: str) -> bytes:
        async with self._lock:
            self._lookup_run_locked(run_id)
        raise DerivationServiceError(
            ErrorKind.UNAVAILABLE,
            "report_download_unavailable",
            "The transport-only fake service has no ReportBundle artifacts.",
            details={"export_id": export_id},
        )

    async def pending_step_revision_id(self, run_id: str) -> str | None:
        """Testing hook for proving an in-flight revision cannot be forked."""

        async with self._lock:
            self._lookup_run_locked(run_id)
            return self._pending_revision_ids.get(run_id)

    async def subscriber_count(self, run_id: str) -> int:
        async with self._lock:
            self._lookup_run_locked(run_id)
            return len(self._subscribers[run_id])

    async def pause_run(self, run_id: str, *, idempotency_key: str | None) -> RunView:
        async with self._lock:
            scope = f"pause:{run_id}"
            cached = self._idempotency_lookup_locked(scope, idempotency_key, {})
            if cached is not None:
                return cached
            run = self._lookup_run_locked(run_id)
            if run.phase not in {"submitted", "autonomous_exploration", "human_expansion"}:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_pausable",
                    "Soft pause is only valid while a run is actively scheduling work.",
                    details={"phase": run.phase},
                )
            paused_branch_ids = tuple(
                branch.branch_id for branch in run.branches if branch.status == "active"
            )
            self._paused_states[run_id] = PausedState(
                previous_phase=run.phase,
                active_calls=tuple(call.model_copy(deep=True) for call in self._active_calls.get(run_id, [])),
                branch_ids=paused_branch_ids,
            )
            self._active_calls[run_id] = []
            run.pause_requested = True
            run.phase = "paused"
            run.status = "paused"
            pause_seq = len(self._events[run.id]) + 1
            for branch in run.branches:
                if branch.status == "active":
                    branch.status = "paused"
                    branch.status_history.append(StatusHistoryItem(seq=pause_seq, status="paused"))
            self._sync_routes_locked(run)
            self._emit_locked(
                run,
                "run.updated",
                overlay=RuntimeOverlay(hard_interrupt_requested=False, active_calls=[]),
            )
            response = _copy_run(run)
            self._idempotency_store_locked(scope, idempotency_key, {}, response)
            return response

    async def resume_run(self, run_id: str, *, idempotency_key: str | None) -> RunView:
        async with self._lock:
            scope = f"resume:{run_id}"
            cached = self._idempotency_lookup_locked(scope, idempotency_key, {})
            if cached is not None:
                return cached
            run = self._lookup_run_locked(run_id)
            if run.phase != "paused":
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_resumable",
                    "Resume is only valid for a soft-paused run.",
                    details={"phase": run.phase},
                )
            paused_state = self._paused_states.get(run_id)
            if paused_state is None:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_resumable",
                    "The paused run has no resumable scheduler state.",
                    details={"phase": run.phase},
                )

            run.pause_requested = False
            run.phase = paused_state.previous_phase
            run.status = "running"
            resume_seq = len(self._events[run.id]) + 1
            resumed_branch_ids = set(paused_state.branch_ids)
            for branch in run.branches:
                if branch.branch_id in resumed_branch_ids and branch.status == "paused":
                    branch.status = "active"
                    branch.status_history.append(StatusHistoryItem(seq=resume_seq, status="active"))
            self._active_calls[run_id] = [call.model_copy(deep=True) for call in paused_state.active_calls]
            del self._paused_states[run_id]
            self._sync_routes_locked(run)
            self._emit_locked(
                run,
                "run.updated",
                overlay=RuntimeOverlay(
                    hard_interrupt_requested=False,
                    active_calls=[call.model_copy(deep=True) for call in self._active_calls[run_id]],
                ),
            )
            response = _copy_run(run)
            self._idempotency_store_locked(scope, idempotency_key, {}, response)
            return response

    async def interrupt_run(self, run_id: str, *, idempotency_key: str | None) -> RunView:
        async with self._lock:
            scope = f"interrupt:{run_id}"
            cached = self._idempotency_lookup_locked(scope, idempotency_key, {})
            if cached is not None:
                return cached
            run = self._lookup_run_locked(run_id)
            active_calls = self._active_calls.get(run_id, [])
            if not active_calls:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_interruptible",
                    "Hard interrupt requires at least one current in-flight call.",
                    details={"phase": run.phase, "active_calls": 0},
                )
            run.hard_interrupt_requested = True
            run.phase = "interrupted"
            run.status = "interrupted"
            self._active_calls[run_id] = []
            self._pending_revision_ids.pop(run_id, None)
            self._paused_states.pop(run_id, None)
            self._emit_locked(
                run,
                "run.updated",
                overlay=RuntimeOverlay(hard_interrupt_requested=True, active_calls=[]),
            )
            response = _copy_run(run)
            self._idempotency_store_locked(scope, idempotency_key, {}, response)
            return response

    async def create_branch(
        self,
        run_id: str,
        command: CreateBranchRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView:
        async with self._lock:
            scope = f"create_branch:{run_id}"
            cached = self._idempotency_lookup_locked(scope, idempotency_key, command)
            if cached is not None:
                return cached
            run = self._lookup_run_locked(run_id)
            if command.from_step_revision_id == self._pending_revision_ids.get(run_id):
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "step_not_sealed",
                    "Branches can only start from a sealed StepRevision.",
                )
            anchor = next(
                (step for step in run.steps if step.revision_id == command.from_step_revision_id),
                None,
            )
            if anchor is None:
                raise DerivationServiceError(
                    ErrorKind.NOT_FOUND,
                    "step_revision_not_found",
                    f"StepRevision {command.from_step_revision_id!r} does not exist in this run.",
                )
            if anchor.status != "sealed" or anchor.content is None:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "step_not_sealed",
                    "Branches can only start from a sealed StepRevision.",
                )
            if run.phase not in {"review_ready", "review_ready_due_to_cap"}:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "run_not_review_ready",
                    "Human expansion is only allowed after autonomous exploration reaches review_ready.",
                    details={"phase": run.phase},
                )
            active_count = sum(branch.status == "active" for branch in run.branches)
            active_cap = run.config.max_active_branches
            if active_cap is not None and active_count >= active_cap:
                raise DerivationServiceError(
                    ErrorKind.INVALID_STATE,
                    "max_active_branches_reached",
                    "The run has no active-branch capacity for another human expansion.",
                    details={"max_active_branches": active_cap},
                )
            parent = next(branch for branch in run.branches if branch.branch_id == anchor.branch_id)
            anchor_index = parent.step_revision_ids.index(anchor.revision_id)
            if command.kind == "human_revision":
                inherited = parent.step_revision_ids[:anchor_index]
            else:
                inherited = parent.step_revision_ids[: anchor_index + 1]
            branch_number = len(run.branches) + 1
            branch = BranchView(
                branch_id=f"branch-{run.id}-{branch_number:03d}",
                parent_branch_id=parent.branch_id,
                anchor_step_revision_id=anchor.revision_id,
                kind=command.kind,
                status="active",
                status_history=[StatusHistoryItem(seq=len(self._events[run.id]) + 1, status="active")],
                step_revision_ids=list(inherited),
            )
            run.branches.append(branch)
            has_call_capacity = (
                run.config.max_model_calls is None or run.budget.used_steps < run.config.max_model_calls
            )
            run.phase = "human_expansion" if has_call_capacity else "review_ready_due_to_cap"
            run.status = "running" if has_call_capacity else "review_ready"
            self._sync_routes_locked(run)
            branch_call = ActiveCallOverlay(
                call_id=f"call-{run.id}-{branch_number:03d}",
                branch_id=branch.branch_id,
                from_step_id=anchor.id,
                label="Deterministic fake branch writer",
            )
            self._active_calls[run.id] = [branch_call] if has_call_capacity else []
            self._emit_locked(
                run,
                "branch.created",
                overlay=RuntimeOverlay(
                    hard_interrupt_requested=False,
                    active_calls=([branch_call.model_copy(deep=True)] if has_call_capacity else []),
                ),
            )

            if not has_call_capacity:
                empty_overlay = RuntimeOverlay(hard_interrupt_requested=False, active_calls=[])
                self._emit_locked(run, "run.updated", overlay=empty_overlay)
                response = _copy_run(run)
                self._idempotency_store_locked(scope, idempotency_key, command, response)
                return response

            step = self._seal_fake_step_locked(
                run,
                branch=branch,
                instruction=command.instruction,
                anchor=anchor,
                revision_content=command.revision_content(),
            )
            self._active_calls[run.id] = []
            branch.step_revision_ids.append(step.revision_id)
            branch.status = "completed"
            branch.status_history.append(
                StatusHistoryItem(seq=len(self._events[run.id]) + 1, status="completed")
            )
            run.edges.append(
                EdgeView(
                    id=f"edge-{run.id}-{len(run.edges) + 1:03d}",
                    from_step_id=anchor.id,
                    to_step_id=step.id,
                    order=branch_number - 1,
                    kind=command.kind,
                )
            )
            run.phase = "review_ready"
            run.status = "review_ready"
            self._sync_routes_locked(run)
            empty_overlay = RuntimeOverlay(hard_interrupt_requested=False, active_calls=[])
            self._emit_locked(run, "step.sealed", overlay=empty_overlay)
            self._emit_locked(run, "run.updated", overlay=empty_overlay)
            response = _copy_run(run)
            self._idempotency_store_locked(scope, idempotency_key, command, response)
            return response

    async def publish_test_event(self, run_id: str) -> None:
        """Testing hook used to exercise bounded slow-subscriber cleanup."""

        async with self._lock:
            run = self._lookup_run_locked(run_id)
            self._emit_locked(
                run,
                "run.updated",
                overlay=RuntimeOverlay(hard_interrupt_requested=False, active_calls=[]),
            )

    async def _remove_subscriber(self, run_id: str, queue: asyncio.Queue[RunEvent | None]) -> None:
        async with self._lock:
            self._subscribers.get(run_id, set()).discard(queue)

    async def _event_stream(
        self,
        run_id: str,
        *,
        after_event_id: int,
        follow: bool,
    ) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent | None] | None = None
        async with self._lock:
            self._lookup_run_locked(run_id)
            backlog = [
                event.model_copy(deep=True)
                for event in self._events[run_id]
                if event.event_id > after_event_id
            ]
            if follow:
                queue = asyncio.Queue(maxsize=self.subscriber_queue_size)
                self._subscribers[run_id].add(queue)
        try:
            for event in backlog:
                yield event
            if queue is None:
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            if queue is not None:
                await self._remove_subscriber(run_id, queue)

    def stream_events(
        self,
        run_id: str,
        *,
        after_event_id: int,
        follow: bool,
    ) -> AsyncIterator[RunEvent]:
        return self._event_stream(run_id, after_event_id=after_event_id, follow=follow)
