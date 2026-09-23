"""Typed, provider-neutral API models.

The request deliberately separates the frozen Record V1 configuration from
runtime-only control metadata.  In particular, authentication mode,
concurrency, and retry policy must never be copied into Record V1's frozen
``configuration`` object.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import MAX_EVENT_ID

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=32_768)]
ShortText = Annotated[str, Field(min_length=1, max_length=256)]

DEFAULT_MAX_MODEL_CALLS = 100


class StrictModel(BaseModel):
    """Reject undeclared fields so protocol drift fails visibly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class RoleModelConfig(StrictModel):
    provider: ShortText
    model: ShortText
    effort: ShortText


class BackendConfig(StrictModel):
    name: ShortText
    version: ShortText


class FrozenRunConfig(StrictModel):
    """Fields that map exactly into Record V1 run_created payloads."""

    granularity: Literal["one_claim", "one_task"]
    writer: RoleModelConfig
    checker: RoleModelConfig
    judge: RoleModelConfig
    backend: BackendConfig
    max_model_calls: Annotated[int, Field(ge=1, le=10_000)] | None
    max_active_branches: Annotated[int, Field(ge=1, le=1_000)] | None
    reference_allowed: bool
    allowed_paths: Annotated[list[str], Field(max_length=256)]
    checker_enabled: bool = True
    max_local_repairs: Annotated[int, Field(ge=0, le=10)] = 3
    record_version: Literal["1.0", "1.1"] = "1.0"

    @model_validator(mode="after")
    def validate_execution_version(self) -> FrozenRunConfig:
        if self.record_version == "1.0" and (self.max_model_calls is None or not self.checker_enabled):
            raise ValueError("unlimited runs and optional Checker require Record 1.1")
        return self

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, paths: list[str], info) -> list[str]:  # type: ignore[no-untyped-def]
        reference_allowed = bool(info.data.get("reference_allowed", False))
        seen: set[str] = set()
        for raw_path in paths:
            if not raw_path or raw_path != raw_path.strip():
                raise ValueError("allowed_paths must contain non-empty, trimmed paths")
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("allowed_paths must be safe repository-relative paths")
            if not reference_allowed and path.parts and path.parts[0] == "reference":
                raise ValueError("reference paths require reference_allowed=true")
            if raw_path in seen:
                raise ValueError("allowed_paths must not contain duplicates")
            seen.add(raw_path)
        return paths

    def record_configuration(self) -> dict[str, object]:
        """Return the exact Record V1 ``configuration`` shape."""

        result = {
            "granularity": self.granularity,
            "max_active_branches": self.max_active_branches,
            "max_model_calls": self.max_model_calls,
            "models": {
                "writer": self.writer.model_dump(),
                "checker": self.checker.model_dump(),
                "judge": self.judge.model_dump(),
            },
            "backend": self.backend.model_dump(),
        }
        if self.record_version == "1.1":
            result.update(checker_enabled=self.checker_enabled, max_local_repairs=self.max_local_repairs)
        return result

    def record_input_policy(self) -> dict[str, object]:
        """Return the exact Record V1 ``input_policy`` shape."""

        return {
            "reference_allowed": self.reference_allowed,
            "allowed_paths": list(self.allowed_paths),
        }


class RuntimeConfig(StrictModel):
    """Implemented v1 execution profile stored outside frozen Record config.

    V1 exposes ChatGPT subscription authentication, the serial scheduler, and
    one versioned benchmark capability profile. ``max_run_seconds`` remains an
    explicit required null until the service enforces a wall-clock deadline.
    """

    auth_mode: Literal["chatgpt"]
    concurrency: Literal[1]
    retries: Literal[0, 1]
    max_run_seconds: Literal[None]
    capability_profile: Literal["benchmark_symbolic_v1", "source_reading_v1"] = "benchmark_symbolic_v1"
    service_tier: Literal["standard", "fast"] = "standard"
    reading_mode: Literal["on_demand", "direct_full", "reader_assisted"] = "on_demand"
    generation_context_sha256: Sha256 | None = None
    # Frozen per-run setting, not an operational knob: when true the Writer
    # owes an intent ledger as the first step of every route and the Checker
    # reviews that step under its own rule.  The product ships it on.
    intent_ledger_first: bool = True
    # Frozen per-run setting, not an operational knob: when true the Writer
    # owes an explicit dimensional-analysis line for the endpoint formula and
    # the Checker recomputes it at completion intent.  The product ships it on.
    dimension_check: bool = True

    @model_validator(mode="after")
    def validate_generation_context(self) -> RuntimeConfig:
        if (self.reading_mode == "on_demand") != (self.generation_context_sha256 is None):
            raise ValueError(
                "on_demand requires no generation context; explicit reading modes "
                "require a bound generation context"
            )
        return self


class ModelOptionView(StrictModel):
    """One App Server model and its model-specific reasoning levels."""

    model: ShortText
    display_name: ShortText
    is_default: bool
    default_effort: ShortText
    supported_efforts: Annotated[list[ShortText], Field(min_length=1, max_length=16)]
    default_service_tier: Literal["standard", "fast"] = "standard"
    supported_service_tiers: list[Literal["standard", "fast"]] = Field(
        default_factory=lambda: ["standard"], min_length=1, max_length=2
    )

    @field_validator("supported_efforts")
    @classmethod
    def validate_efforts(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("supported_efforts must not contain duplicates")
        return values

    @field_validator("supported_service_tiers")
    @classmethod
    def validate_service_tiers(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("supported_service_tiers must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_default_effort(self) -> ModelOptionView:
        if self.default_effort not in self.supported_efforts:
            raise ValueError("default_effort must be present in supported_efforts")
        if self.default_service_tier not in self.supported_service_tiers:
            raise ValueError("default_service_tier must be present in supported_service_tiers")
        if "standard" not in self.supported_service_tiers:
            raise ValueError("supported_service_tiers must include standard")
        return self


class CreateRunDefaultsView(StrictModel):
    """Server-owned defaults for the product's create-run form."""

    config: FrozenRunConfig
    runtime: RuntimeConfig
    model_options: Annotated[list[ModelOptionView], Field(min_length=1, max_length=64)]
    model_catalog_source: Literal["app_server", "last_known_good", "static_fixture"]
    model_catalog_refreshed_at: datetime | None
    allowed_models: Annotated[list[ShortText], Field(min_length=1, max_length=64)]
    allowed_efforts: Annotated[list[ShortText], Field(min_length=1, max_length=16)]

    @field_validator("allowed_models", "allowed_efforts")
    @classmethod
    def validate_unique_catalog(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("catalog values must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_model_catalog(self) -> CreateRunDefaultsView:
        option_models = [option.model for option in self.model_options]
        if option_models != self.allowed_models:
            raise ValueError("allowed_models must match model_options order")
        option_efforts = list(
            dict.fromkeys(effort for option in self.model_options for effort in option.supported_efforts)
        )
        if option_efforts != self.allowed_efforts:
            raise ValueError("allowed_efforts must match model_options union")
        if sum(option.is_default for option in self.model_options) != 1:
            raise ValueError("model_options must contain exactly one default")
        options = {option.model: option for option in self.model_options}
        for role in (self.config.writer, self.config.checker, self.config.judge):
            option = options.get(role.model)
            if option is None or role.effort not in option.supported_efforts:
                raise ValueError("configured model/effort must be present in model_options")
            if self.runtime.service_tier not in option.supported_service_tiers:
                raise ValueError("configured service tier must be supported by every role model")
        return self


class SourcePackRef(StrictModel):
    """Opaque reference-pack identity; no retrieval authority is implied."""

    pack_id: Identifier
    version: ShortText
    sha256: Sha256


IntakeSessionStatusValue = Literal[
    "active",
    "convergence_required",
    "candidate_ready",
    "confirmed",
    "cancelled",
]
ProblemSpecificationStatusValue = Literal["draft", "candidate_ready", "confirmed", "superseded"]
DecisionStatusValue = Literal["open", "resolved", "superseded"]
AnswerModeValue = Literal["single_choice", "multi_choice", "choice_with_text", "text"]
DecisionClassValue = Literal["problem", "convention", "approximation_level"]
AnswerStrategyValue = Literal["selected", "simplest_first", "both_routes"]
ThreadGenerationStatusValue = Literal["active", "superseded", "closed"]


class CreateIntakeSessionRequest(StrictModel):
    initial_message: NonEmptyText
    model: ShortText
    effort: ShortText
    service_tier: Literal["standard", "fast"] = "standard"


class IntakeAnswerInput(StrictModel):
    """One answer: chosen options, free text, or a ladder strategy.

    ``simplest_first`` and ``both_routes`` are answers in their own right, so an
    input carrying only a strategy is complete. ``both_routes`` without an
    explicit selection means every option the question offered.
    """

    selected_option_ids: Annotated[list[Identifier], Field(max_length=128)] = []
    custom_text: NonEmptyText | None = None
    strategy: AnswerStrategyValue | None = None

    @model_validator(mode="after")
    def validate_non_empty_answer(self) -> IntakeAnswerInput:
        if not self.selected_option_ids and self.custom_text is None and self.strategy is None:
            raise ValueError("an Intake answer cannot be empty")
        if len(self.selected_option_ids) != len(set(self.selected_option_ids)):
            raise ValueError("selected_option_ids cannot contain duplicates")
        return self


class SubmitIntakeRoundRequest(StrictModel):
    base_revision: Annotated[int, Field(ge=0)]
    answers: Annotated[dict[Identifier, IntakeAnswerInput], Field(max_length=128)] = {}
    user_message: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_round_submission(self) -> SubmitIntakeRoundRequest:
        if not self.answers and self.user_message is None:
            raise ValueError("an Intake round requires answers or a correction message")
        return self


class FinalizeIntakeSessionRequest(StrictModel):
    """Start with what is known: answer any pending problem question and go.

    Valid only while the session is ``convergence_required``. Every pending
    problem question must be answered; everything still open becomes a declared
    default the derivation agent owns.
    """

    base_revision: Annotated[int, Field(ge=0)]
    answers: Annotated[dict[Identifier, IntakeAnswerInput], Field(max_length=128)] = {}


class IntakeRevisionRequest(StrictModel):
    base_revision: Annotated[int, Field(ge=0)]


class IntakeQuestionOptionView(StrictModel):
    option_id: Identifier
    label: ShortText
    impact: NonEmptyText


class IntakeQuestionView(StrictModel):
    """One problem-class decision the user must settle before the derivation runs."""

    question_id: Identifier
    decision_id: Identifier
    semantic_key: NonEmptyText
    title: ShortText
    prompt: NonEmptyText
    why_needed: NonEmptyText
    why_it_matters: NonEmptyText
    answer_mode: AnswerModeValue
    decision_class: DecisionClassValue = "problem"
    options: list[IntakeQuestionOptionView]
    recommended_option_ids: list[Identifier]
    recommendation_reason: NonEmptyText | None
    allow_custom: bool
    depends_on: list[Identifier]
    blocking: bool
    grounded_in: NonEmptyText | None = None


class IntakeDeclaredDefaultView(StrictModel):
    """A convention or approximation the agent adopts instead of asking."""

    default_id: Identifier
    decision_class: DecisionClassValue
    title: ShortText
    statement: NonEmptyText
    rationale: NonEmptyText
    alternatives: Annotated[list[NonEmptyText], Field(max_length=32)] = []


class IntakeLadderRungView(StrictModel):
    """One refinement level; rung 0 is the textbook-simplest baseline."""

    rung: Annotated[int, Field(ge=0)]
    name: ShortText
    relaxes: NonEmptyText
    default_ids: Annotated[list[Identifier], Field(max_length=32)] = []
    decision_ids: Annotated[list[Identifier], Field(max_length=32)] = []
    parallel_branch: bool = False


class IntakeProblemSectionView(StrictModel):
    name: NonEmptyText
    content: NonEmptyText | None
    not_applicable_reason: NonEmptyText | None


class IntakeProblemSpecificationView(StrictModel):
    version: Annotated[int, Field(ge=1)]
    status: ProblemSpecificationStatusValue
    sections: list[IntakeProblemSectionView]
    critical_message_refs: list[Identifier]
    supersedes_version: Annotated[int, Field(ge=1)] | None
    declared_defaults: Annotated[list[IntakeDeclaredDefaultView], Field(max_length=64)] = []
    refinement_ladder: Annotated[list[IntakeLadderRungView], Field(max_length=32)] = []


class IntakeDecisionAnswerView(StrictModel):
    selected_option_ids: list[Identifier]
    custom_text: NonEmptyText | None
    source_message_refs: list[Identifier]
    strategy: AnswerStrategyValue | None = None


class IntakeDecisionView(StrictModel):
    decision_id: Identifier
    semantic_key: NonEmptyText
    status: DecisionStatusValue
    question: IntakeQuestionView
    revision: Annotated[int, Field(ge=1)]
    supersedes_revision: Annotated[int, Field(ge=1)] | None
    answer: IntakeDecisionAnswerView | None
    reopen_reason: NonEmptyText | None
    source_message_refs: list[Identifier]


class IntakeThreadGenerationView(StrictModel):
    generation: Annotated[int, Field(ge=1)]
    status: ThreadGenerationStatusValue
    app_server_thread_id: Identifier
    prompt_version: ShortText
    replaced_generation: Annotated[int, Field(ge=1)] | None
    replacement_reason: NonEmptyText | None


class IntakeConversationEventView(StrictModel):
    event_id: Identifier
    kind: ShortText
    payload: dict[str, Any]


class IntakeConvergenceView(StrictModel):
    """Server-side budget that decides when the grill stops asking."""

    rounds: Annotated[int, Field(ge=0)] = 0
    audit_rejections: Annotated[int, Field(ge=0)] = 0
    reason: NonEmptyText | None = None
    finalized_by_user: bool = False


class IntakePendingAnswerView(StrictModel):
    """One answer as the user submitted it, before any round accepted it."""

    selected_option_ids: list[Identifier] = []
    custom_text: NonEmptyText | None = None
    source_message_refs: list[Identifier] = []
    strategy: AnswerStrategyValue | None = None


class IntakePendingSubmissionView(StrictModel):
    """What the user submitted on an attempt that never reached a commit.

    A round writes the user's answers and the model's reply together, so a model
    round that fails takes the answers with it. The server keeps them here so
    the client can offer them back instead of making the user retype them.
    """

    kind: Literal["round", "finalize"]
    base_revision: Annotated[int, Field(ge=0)]
    submitted_at: ShortText
    answers: Annotated[dict[Identifier, IntakePendingAnswerView], Field(max_length=128)] = {}
    user_message: NonEmptyText | None = None
    failure_reason: NonEmptyText | None = None


class IntakeSessionView(StrictModel):
    session_id: Identifier
    revision: Annotated[int, Field(ge=0)]
    status: IntakeSessionStatusValue
    model: ShortText
    effort: ShortText
    service_tier: Literal["standard", "fast"] = "standard"
    problem_specifications: list[IntakeProblemSpecificationView]
    decisions: list[IntakeDecisionView]
    frontier: list[IntakeQuestionView]
    thread_generations: list[IntakeThreadGenerationView]
    pending_problem_questions: Annotated[list[IntakeQuestionView], Field(max_length=32)] = []
    convergence: IntakeConvergenceView = IntakeConvergenceView()
    conversation: list[IntakeConversationEventView]
    frozen_problem: FrozenProblemInput | None = None
    pending_submission: IntakePendingSubmissionView | None = None


class FrozenProblemInput(StrictModel):
    """Versioned scientific input; direct input and Intake confirmation have distinct provenance."""

    problem_id: Identifier
    version: Annotated[int, Field(ge=1)]
    supersedes_version: Annotated[int, Field(ge=1)] | None = None
    objective: NonEmptyText
    givens: Annotated[list[NonEmptyText], Field(max_length=256)]
    assumptions: Annotated[list[NonEmptyText], Field(max_length=256)]
    accepted_decisions: list[NonEmptyText] = Field(default_factory=list, max_length=256)
    declared_defaults: Annotated[list[IntakeDeclaredDefaultView], Field(max_length=64)] = []
    refinement_ladder: Annotated[list[IntakeLadderRungView], Field(max_length=32)] = []
    scope: NonEmptyText
    deliverable: NonEmptyText
    allowed_tools: Annotated[list[ShortText], Field(max_length=64)]
    allowed_references: Annotated[list[NonEmptyText], Field(max_length=256)]
    success_criteria: Annotated[list[NonEmptyText], Field(min_length=1, max_length=256)]
    source_pack: SourcePackRef | None = None
    origin: Literal["confirmed_intake", "direct_spec"] = "confirmed_intake"
    confirmed_by_user: bool

    @model_validator(mode="after")
    def validate_problem_contract(self) -> FrozenProblemInput:
        if self.origin == "confirmed_intake" and not self.confirmed_by_user:
            raise ValueError("confirmed_intake requires actual user confirmation")
        if self.origin == "direct_spec":
            if self.confirmed_by_user:
                raise ValueError("direct_spec must not claim Intake confirmation")
            if self.problem_id.startswith(("intake_", "intake-")):
                raise ValueError("direct_spec must not impersonate an Intake session")
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("an initial problem cannot supersede another version")
        if self.version > 1 and self.supersedes_version != self.version - 1:
            raise ValueError("a revised problem must identify its immediate predecessor")
        unsupported_tools = sorted(set(self.allowed_tools) - {"scientific_compute"})
        if unsupported_tools:
            raise ValueError(
                "allowed_tools contains tools outside benchmark_symbolic_v1: " + ", ".join(unsupported_tools)
            )
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must not contain duplicates")
        return self

    def task_text(self) -> str:
        """Render the stable scientific input shown to writer/checker/judge."""

        def section(title: str, values: list[str]) -> str:
            body = "\n".join(f"- {value}" for value in values) or "- None declared"
            return f"## {title}\n\n{body}"

        parts = [
            f"# Frozen derivation problem {self.problem_id}:v{self.version}",
            f"## Objective\n\n{self.objective}",
            section("Givens", self.givens),
            section("Accepted assumptions", self.assumptions),
        ]
        if self.accepted_decisions:
            heading = (
                "Confirmed intake decisions"
                if self.origin == "confirmed_intake"
                else "Declared problem decisions"
            )
            parts.append(section(heading, self.accepted_decisions))
        if self.refinement_ladder:
            parts.append(
                section(
                    "Refinement ladder",
                    [
                        f"rung {rung.rung} — {rung.name} — {rung.relaxes}"
                        + (" [parallel branch]" if rung.parallel_branch else "")
                        for rung in sorted(self.refinement_ladder, key=lambda item: item.rung)
                    ],
                )
            )
        if self.declared_defaults:
            parts.append(
                section(
                    "Declared defaults",
                    [f"{item.title}: {item.statement}" for item in self.declared_defaults],
                )
            )
        parts.extend(
            [
                f"## Scope\n\n{self.scope}",
                f"## Required deliverable\n\n{self.deliverable}",
                section("Allowed tools", self.allowed_tools),
                section("Allowed references", self.allowed_references),
                section("Success criteria", self.success_criteria),
            ]
        )
        if self.source_pack is not None:
            parts.append(
                "## Source pack binding\n\n"
                f"- id: {self.source_pack.pack_id}\n"
                f"- version: {self.source_pack.version}\n"
                f"- sha256: {self.source_pack.sha256}"
            )
        return "\n\n".join(parts)


IntakeSessionView.model_rebuild()


class ProblemPresetView(StrictModel):
    id: Identifier
    title: NonEmptyText
    description: NonEmptyText
    problem: FrozenProblemInput


class ProblemPresetsView(StrictModel):
    presets: list[ProblemPresetView]
    method_source_pack: SourcePackRef
    method_references: list[NonEmptyText]
    capability_profile: Literal["source_reading_v1"] = "source_reading_v1"


class CreateRunRequest(StrictModel):
    problem: FrozenProblemInput
    config: FrozenRunConfig
    runtime: RuntimeConfig

    @property
    def question(self) -> str:
        """Short display label retained by the HTTP Run view."""

        return self.problem.objective

    @property
    def task_text(self) -> str:
        return self.problem.task_text()


RunPhase = Literal[
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
RunStatus = Literal["running", "review_ready", "paused", "interrupted", "error"]
BranchStatus = Literal["active", "paused", "parked", "completed", "killed"]
StepStatus = Literal["sealed", "running", "proposed", "failed"]


class StatusHistoryItem(StrictModel):
    seq: Annotated[int, Field(ge=1)]
    status: BranchStatus


class StepChecks(StrictModel):
    schema_status: Literal["passed", "failed", "pending"] = Field(
        validation_alias="schema",
        serialization_alias="schema",
    )
    physics: Literal["passed", "failed", "pending", "not_requested"]
    provenance: Literal["passed", "failed", "pending"]


class StepProvenance(StrictModel):
    model: str
    thread_id: str = Field(serialization_alias="threadId")
    turn_id: str = Field(serialization_alias="turnId")
    created_at: str = Field(serialization_alias="createdAt")


class StepContent(StrictModel):
    claim: NonEmptyText
    why: NonEmptyText
    source: NonEmptyText
    derivation: NonEmptyText
    scope: NonEmptyText


class CreateBranchRequest(StrictModel):
    """Discriminated human expansion command.

    A direction is free text.  A revision is direct human-authored scientific
    content, so its instruction must be a JSON object with exactly the five
    StepContent fields.  A runtime must canonicalize it before recording the
    revise_step HumanAction; it must never ask a model to author content and
    label that output as human.
    """

    from_step_revision_id: Identifier
    kind: Literal["human_direction", "human_revision"]
    instruction: Annotated[str, Field(min_length=1, max_length=16_384)]

    @model_validator(mode="after")
    def validate_revision_content(self) -> CreateBranchRequest:
        if self.kind == "human_revision":
            self.revision_content()
        return self

    def revision_content(self) -> StepContent | None:
        if self.kind != "human_revision":
            return None
        try:
            raw = json.loads(self.instruction)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "human_revision instruction must be JSON with exactly "
                "claim, why, source, derivation, and scope"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError("human_revision instruction must be a JSON object")
        return StepContent.model_validate(raw)

    def canonical_instruction(self) -> str:
        content = self.revision_content()
        if content is None:
            return self.instruction
        return json.dumps(
            content.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class ExportReportRequest(StrictModel):
    """Select and explicitly confirm the main route for one immutable export."""

    selected_route_id: Identifier
    confirm_selected_route: Literal[True]


class ReportBundleView(StrictModel):
    """Safe API projection of a versioned ReportBundle.

    Paths are repository-relative artifact locations produced by the service;
    clients cannot supply or influence an output path.
    """

    status: Literal["success", "failed"]
    run_id: Identifier
    selected_route_id: Identifier
    export_id: Identifier
    bundle_path: str
    files: list[str]
    manifest: dict[str, object]


class StepView(StrictModel):
    """One sealed StepRevision as the product shows it.

    ``output_sha256`` always identifies the *recorded* content, even when
    ``typeset`` is true: the sealed text is the Record, and a typeset layer is
    a display artifact derived from it, never a new version of it.
    """

    id: Identifier
    revision_id: Identifier = Field(serialization_alias="revisionId")
    branch_id: Identifier
    order: Annotated[int, Field(ge=0)]
    title: ShortText
    status: StepStatus
    content: StepContent | None
    output_sha256: Sha256 | None
    input: str
    reasoning_summary: str = Field(serialization_alias="reasoningSummary")
    output: str
    checks: StepChecks
    provenance: StepProvenance | None
    #: True when the math fragments shown here come from a verified typeset
    #: layer rather than from the sealed text. False (or absent) means the
    #: fields are byte-identical to the Record.
    typeset: bool = False


class EdgeView(StrictModel):
    id: Identifier
    from_step_id: Identifier = Field(serialization_alias="from")
    to_step_id: Identifier = Field(serialization_alias="to")
    order: Annotated[int, Field(ge=0)]
    kind: Literal[
        "continuation",
        "model_fork",
        "human_direction",
        "human_revision",
        "model_revision",
        "proposed",
    ]


class BranchView(StrictModel):
    branch_id: Identifier
    parent_branch_id: Identifier | None
    anchor_step_revision_id: Identifier | None
    kind: Literal[
        "root", "model_fork", "human_direction", "human_revision", "model_revision", "instrument_retry"
    ]
    status: BranchStatus
    status_history: list[StatusHistoryItem]
    step_revision_ids: list[Identifier]


class RouteView(StrictModel):
    id: Identifier
    label: ShortText
    node_ids: list[Identifier] = Field(serialization_alias="nodeIds")
    status: Literal["complete", "active", "proposed", "failed"]
    branch_id: Identifier
    status_history: list[StatusHistoryItem]


class BudgetView(StrictModel):
    used_steps: Annotated[int, Field(ge=0)] = Field(serialization_alias="usedSteps")
    max_steps: Annotated[int, Field(ge=1)] | None = Field(serialization_alias="maxSteps")


class RunCommandCapabilities(StrictModel):
    """Backend-derived mutation affordances for one concrete Run snapshot."""

    model_config = ConfigDict(
        json_schema_extra=lambda schema: schema.setdefault("required", []).extend(
            [
                "can_pause",
                "can_resume",
                "can_interrupt",
                "branchable_step_revision_ids",
            ]
        )
    )

    can_pause: bool = False
    can_resume: bool = False
    can_interrupt: bool = False
    branchable_step_revision_ids: list[Identifier] = Field(default_factory=list)


class TypesetLayerView(StrictModel):
    """Identity of the typeset layer one route's shown text was taken from.

    ``content_sha256`` is the layer's own content hash, so a client can tell
    two renderings of the same Record apart, and ``status`` is the layer's
    compile verdict (a layer delivered with failures still repairs the
    formulas it could).
    """

    route_id: Identifier
    content_sha256: Sha256
    status: ShortText


class RunView(StrictModel):
    """Canonical complete snapshot plus backend-derived control phase.

    A real service must build this from the last strict-replayable Record V1
    head.  In-flight deltas belong only in ``RunEvent.overlay`` and must not be
    promoted into ``steps`` before a StepRevision is sealed.

    ``typeset_layers`` is the one place where this snapshot is not the sealed
    text: the routes listed there have their math fragments rendered from a
    verified typeset layer, and the steps carrying that text say so with
    ``StepView.typeset``.
    """

    model_config = ConfigDict(
        json_schema_extra=lambda schema: schema.setdefault("required", []).extend(["read_only", "commands"])
    )

    id: Identifier
    question: NonEmptyText
    status: RunStatus
    phase: RunPhase
    config: FrozenRunConfig
    runtime: RuntimeConfig
    root_step_id: Identifier | None = Field(serialization_alias="rootStepId")
    steps: list[StepView]
    edges: list[EdgeView]
    branches: list[BranchView]
    routes: list[RouteView]
    budget: BudgetView
    canonical_event_id: Annotated[int, Field(ge=0, le=MAX_EVENT_ID)]
    pause_requested: bool
    hard_interrupt_requested: bool
    read_only: bool = False
    commands: RunCommandCapabilities = Field(default_factory=RunCommandCapabilities)
    created_at: str
    updated_at: str
    error_message: str | None = Field(default=None, serialization_alias="errorMessage")
    typeset_layers: list[TypesetLayerView] = Field(default_factory=list)


class RunSummary(StrictModel):
    """Small catalog row for active and immutable archived runs."""

    id: Identifier
    question: NonEmptyText
    status: RunStatus
    phase: RunPhase
    step_count: Annotated[int, Field(ge=0)]
    route_count: Annotated[int, Field(ge=0)]
    read_only: bool
    created_at: str
    updated_at: str


class ActiveCallOverlay(StrictModel):
    call_id: Identifier = Field(serialization_alias="callId")
    branch_id: Identifier = Field(serialization_alias="branchId")
    from_step_id: Identifier = Field(serialization_alias="fromStepId")
    label: ShortText


class RuntimeOverlay(StrictModel):
    hard_interrupt_requested: bool
    active_calls: list[ActiveCallOverlay] = Field(serialization_alias="activeCalls")


class RunEvent(StrictModel):
    event_id: Annotated[int, Field(ge=1, le=MAX_EVENT_ID)]
    type: Literal["run.created", "run.updated", "step.sealed", "branch.created", "run.error"]
    run_id: Identifier
    occurred_at: str
    run: RunView | None
    overlay: RuntimeOverlay | None


class HealthView(StrictModel):
    status: Literal["ok", "degraded"]
    service: str


class BuildInfoView(StrictModel):
    """Immutable identity shared by the app bundle and local backend."""

    schema_version: Literal["derivationlab-build-info-v1"]
    version: Annotated[str, Field(pattern=r"^(?:dev|\d{4}\.\d{1,2}\.\d{1,2})$")]
    build_number: Annotated[str, Field(pattern=r"^(?:0|\d{8}\.\d{6})$")]
    release_id: ShortText
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    openapi_sha256: Sha256
    product_mode: Literal["development", "release"]


class QuitReadinessView(StrictModel):
    safe_to_quit: bool
    active_run_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_consistency(self) -> QuitReadinessView:
        if self.safe_to_quit != (self.active_run_count == 0):
            raise ValueError("safe_to_quit must agree with active_run_count")
        return self


AccountStatus = Literal["signed_in", "signed_out", "reauth_required", "unavailable"]
AccountDiagnostic = Literal[
    "ready",
    "product_auth_missing",
    "product_auth_invalid",
    "source_auth_available",
    "source_auth_unavailable",
    "account_check_failed",
]


class AccountView(StrictModel):
    status: AccountStatus
    credential_store: Literal["file"]
    import_available: bool
    diagnostic: AccountDiagnostic


class AccountRateLimitWindowView(StrictModel):
    kind: Literal["five_hour", "weekly", "other"]
    used_percent: Annotated[int, Field(ge=0, le=100)]
    remaining_percent: Annotated[int, Field(ge=0, le=100)]
    window_duration_mins: Annotated[int, Field(gt=0)] | None = None
    resets_at: str | None = None


class AccountRateLimitsView(StrictModel):
    status: Literal["available", "signed_out", "temporarily_unavailable"]
    plan_type: ShortText | None = None
    windows: list[AccountRateLimitWindowView] = Field(max_length=2)
    observed_at: str | None = None
    stale: bool = False
    diagnostic: Literal[
        "ready",
        "product_auth_missing",
        "rate_limits_unavailable",
    ]


class ImportExistingAccountRequest(StrictModel):
    confirm_import: Literal[True]


def _reject_control_characters(value: str, *, field: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{field} cannot contain control characters")
    return value


class DeviceLoginStartView(StrictModel):
    login_id: Identifier
    verification_url: Annotated[str, Field(max_length=2048)]
    user_code: Annotated[str, Field(min_length=1, max_length=128)]
    expires_at: str

    @model_validator(mode="after")
    def validate_login_fields(self) -> DeviceLoginStartView:
        _reject_control_characters(self.login_id, field="login_id")
        _reject_control_characters(self.user_code, field="user_code")
        _reject_control_characters(self.verification_url, field="verification_url")
        parsed = urlsplit(self.verification_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("verification_url is malformed") from exc
        hostname = (parsed.hostname or "").lower()
        allowed = hostname in {"openai.com", "chatgpt.com"} or hostname.endswith(
            (".openai.com", ".chatgpt.com")
        )
        if (
            parsed.scheme != "https"
            or not allowed
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
        ):
            raise ValueError("verification_url is not an allowed HTTPS URL")
        try:
            datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
        return self


class DeviceLoginStatusView(StrictModel):
    status: Literal["pending", "signed_in", "failed", "expired", "canceled"]
    diagnostic: Annotated[str | None, Field(max_length=256)] = None


class DeviceLoginCancelView(StrictModel):
    status: Literal["canceled", "not_found"]


class SiteLoginRequest(BaseModel):
    """Website login input; password whitespace is never silently normalized."""

    model_config = ConfigDict(extra="forbid")

    identifier: Annotated[str, Field(min_length=1, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier cannot be blank")
        return normalized


class SitePasswordChangeRequest(BaseModel):
    """Password rotation input; exact bytes are passed to the identity authority."""

    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=1024)]
    new_password: Annotated[str, Field(min_length=1, max_length=128)]


class SiteModeView(StrictModel):
    mode: Literal["desktop", "server"]
    channel: Literal["development", "preview", "stable", "staging"]


class AdminCreateSiteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=3, max_length=64)]
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=128)]
    role: Literal["user", "admin"] = "user"


class AdminResetSitePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: Annotated[str, Field(min_length=1, max_length=128)]


class AdminSetSiteAccountStatusRequest(StrictModel):
    status: Literal["active", "disabled"]


class SiteAccountView(StrictModel):
    user_id: Identifier
    username: ShortText
    email: Annotated[str, Field(min_length=3, max_length=254)]
    role: Literal["user", "admin"]
    status: Literal["active", "disabled"]
    must_change_password: bool


class SiteSessionView(StrictModel):
    account: SiteAccountView
    idle_expires_at: datetime
    absolute_expires_at: datetime


class CapabilitiesView(StrictModel):
    api_version: Literal["derivation-http-v1"]
    command_transport: Literal["http"]
    event_transport: Literal["sse"]
    branch_kinds: list[Literal["human_direction", "human_revision"]]
    phases: list[RunPhase]
    idempotency_header: Literal["Idempotency-Key"]
    sse_cursor_header: Literal["Last-Event-ID"]
    replay_query: Literal["follow=false"]
    create_run_defaults: CreateRunDefaultsView


class ErrorDetail(StrictModel):
    code: str
    message: str
    details: object | None


class ErrorEnvelope(StrictModel):
    error: ErrorDetail
    request_id: str
