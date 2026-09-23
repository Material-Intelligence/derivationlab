"""Provider-neutral runtime contracts for the derivation agent.

The scientific domain deliberately sees opaque runtime session and operation
identifiers.  Provider-specific thread/turn fields belong in an adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeAlias

FORMULA_VALIDATION_POLICIES = frozenset({None, "formula-v1", "formula-v2"})


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


class ModelRole(str, Enum):
    WRITER = "writer"
    CHECKER = "checker"
    JUDGE = "judge"


class WriterDecision(str, Enum):
    CONTINUE = "continue"
    FORK = "fork"
    COMPLETE = "complete"
    REVISE = "revise"
    BLOCKED = "blocked"


class RunPhase(str, Enum):
    SUBMITTED = "submitted"
    AUTONOMOUS_EXPLORATION = "autonomous_exploration"
    HUMAN_EXPANSION = "human_expansion"
    PAUSED = "paused"
    RECOVERING = "recovering"
    REVIEW_READY = "review_ready"
    REVIEW_READY_DUE_TO_CAP = "review_ready_due_to_cap"
    ERROR = "error"


class ProviderLineage(str, Enum):
    NATIVE = "native"
    FORKED = "forked"
    REHYDRATED = "rehydrated"


class ReconcileStatus(str, Enum):
    COMPLETED = "completed"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        _non_empty(self.provider, "provider")
        _non_empty(self.model, "model")
        _non_empty(self.effort, "effort")

    def to_record(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class ContentRef:
    id: str
    sha256: str

    def __post_init__(self) -> None:
        _non_empty(self.id, "content ref id")
        if len(self.sha256) != 64:
            raise ValueError("content ref sha256 must contain 64 hex characters")

    def to_record(self) -> dict[str, str]:
        return {"id": self.id, "sha256": self.sha256}


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _non_empty(self.path, "artifact path")
        if len(self.sha256) != 64:
            raise ValueError("artifact sha256 must contain 64 hex characters")

    def to_record(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class InputPolicy:
    reference_allowed: bool
    allowed_paths: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "reference_allowed": self.reference_allowed,
            "allowed_paths": list(self.allowed_paths),
        }


@dataclass(frozen=True)
class RunConfig:
    """All runtime choices are explicit; model and effort have no defaults."""

    run_id: str
    task: ContentRef
    pack: ContentRef
    code_commit: str
    granularity: str
    max_active_branches: int | None
    max_model_calls: int | None
    concurrency: int
    retries: int
    writer: ModelSpec
    checker: ModelSpec
    judge: ModelSpec
    backend_name: str
    backend_version: str
    record_spec: ArtifactRef
    event_schema: ArtifactRef
    canonical_schema: ArtifactRef
    input_policy: InputPolicy
    credential_profile_id: str
    service_tier: str = "standard"
    checker_enabled: bool = True
    # Three repairs per step lineage, so that a one-token bookkeeping
    # correction does not exhaust the budget for the scientific repair the same
    # slot may still need.
    max_local_repairs: int = 3
    record_version: str = "1.0"
    reading_mode: str = "on_demand"
    generation_context_sha256: str | None = None
    # Switchable generic obligation: the first step of every route is an intent
    # ledger, and the Checker reviews that step under its own rule. It is a
    # frozen run input rather than a runtime choice so that a run's prompts
    # are fixed by its configuration. Archived runs carry no such field and
    # default to the baseline.
    intent_ledger_first: bool = False
    # Switchable generic obligation: the endpoint formula carries an explicit
    # dimensional-analysis line and the Checker recomputes it. Like the
    # obligation above it is a frozen run input; archived runs carry no such
    # field.
    dimension_check: bool = False
    # None: archived runs without a format gate. "formula-v1": per-step static
    # check plus locked-compiler probe, pausing when the retry budget is spent.
    # "formula-v2": deterministic normalization plus engine-whitelist static
    # check, at most two format rewrites per slot, never pausing for format.
    formula_validation_policy: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.run_id, "run_id")
        if self.formula_validation_policy not in FORMULA_VALIDATION_POLICIES:
            raise ValueError("unsupported formula_validation_policy")
        if len(self.code_commit) != 40:
            raise ValueError("code_commit must contain 40 hex characters")
        if self.granularity not in {"one_claim", "one_task"}:
            raise ValueError("granularity must be one_claim or one_task")
        if self.max_active_branches is not None and self.max_active_branches <= 0:
            raise ValueError("max_active_branches must be positive or None")
        if self.max_model_calls is not None and self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive or None")
        if type(self.checker_enabled) is not bool:
            raise ValueError("checker_enabled must be boolean")
        if type(self.intent_ledger_first) is not bool:
            raise ValueError("intent_ledger_first must be boolean")
        if type(self.dimension_check) is not bool:
            raise ValueError("dimension_check must be boolean")
        if type(self.max_local_repairs) is not int or self.max_local_repairs < 0:
            raise ValueError("max_local_repairs must be a non-negative integer")
        if self.record_version not in {"1.0", "1.1"}:
            raise ValueError("unsupported record_version")
        if self.record_version == "1.0" and (
            self.max_model_calls is None or not self.checker_enabled
        ):
            raise ValueError("unlimited calls and Checker-off require Record 1.1")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.retries not in {0, 1}:
            raise ValueError("v1 retries must be explicitly 0 or 1")
        if self.service_tier not in {"standard", "fast"}:
            raise ValueError("service_tier must be standard or fast")
        if self.reading_mode not in {
            "on_demand",
            "direct_full",
            "reader_assisted",
        }:
            raise ValueError("unsupported reading_mode")
        if (self.reading_mode == "on_demand") != (
            self.generation_context_sha256 is None
        ):
            raise ValueError(
                "on_demand requires no generation context; explicit reading modes "
                "require a bound generation context"
            )
        if self.generation_context_sha256 is not None and (
            len(self.generation_context_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.generation_context_sha256
            )
        ):
            raise ValueError("generation_context_sha256 must be lowercase sha256")
        _non_empty(self.backend_name, "backend_name")
        _non_empty(self.backend_version, "backend_version")
        _non_empty(self.credential_profile_id, "credential_profile_id")

    def model_for(self, role: ModelRole) -> ModelSpec:
        return {
            ModelRole.WRITER: self.writer,
            ModelRole.CHECKER: self.checker,
            ModelRole.JUDGE: self.judge,
        }[role]

    def record_configuration(self) -> dict[str, Any]:
        return {
            "granularity": self.granularity,
            "max_active_branches": self.max_active_branches,
            "max_model_calls": self.max_model_calls,
            **(
                {
                    "checker_enabled": self.checker_enabled,
                    "max_local_repairs": self.max_local_repairs,
                }
                if self.record_version == "1.1"
                else {}
            ),
            "models": {
                "writer": self.writer.to_record(),
                "checker": self.checker.to_record(),
                "judge": self.judge.to_record(),
            },
            "backend": {"name": self.backend_name, "version": self.backend_version},
        }


@dataclass(frozen=True)
class StepContent:
    claim: str
    why: str
    source: str
    derivation: str
    scope: str

    def __post_init__(self) -> None:
        for name in ("claim", "why", "source", "derivation", "scope"):
            _non_empty(getattr(self, name), f"step.{name}")

    def to_record(self) -> dict[str, str]:
        return {
            "claim": self.claim,
            "why": self.why,
            "source": self.source,
            "derivation": self.derivation,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class StepSnapshot:
    """One sealed step plus the route lineage the host can resolve for it.

    ``step_slot`` is the step's position on the route it belongs to, and
    ``superseded_step_revision_ids`` lists the revisions that slot has already
    had, newest first.  A model cannot know either fact reliably - a step
    cannot cite an identifier that is assigned only when it is sealed - so the
    host supplies both and every reference to a slot resolves without the
    model guessing.  Both default to the empty case so a snapshot built
    outside an orchestrated route stays valid.
    """

    step_revision_id: str
    content: StepContent
    step_slot: int | None = None
    superseded_step_revision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BranchAlternative:
    hypothesis: str

    def __post_init__(self) -> None:
        _non_empty(self.hypothesis, "alternative hypothesis")


@dataclass(frozen=True)
class WriterControl:
    decision: WriterDecision
    alternatives: tuple[BranchAlternative, ...]
    revise_step_revision_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision is WriterDecision.REVISE:
            _non_empty(self.revise_step_revision_id, "revision target")
            _non_empty(self.reason, "revision reason")
        elif self.revise_step_revision_id is not None:
            raise ValueError("revision target requires revise decision")
        if self.decision is WriterDecision.BLOCKED:
            _non_empty(self.reason, "blocked reason")
        if self.decision is WriterDecision.FORK and not self.alternatives:
            raise ValueError("fork decision requires at least one alternative")
        if self.decision is not WriterDecision.FORK and self.alternatives:
            raise ValueError("alternatives are valid only for a fork decision")

    def to_record(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "alternatives": [item.hypothesis for item in self.alternatives],
            "revise_step_revision_id": self.revise_step_revision_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Usage:
    values: Mapping[str, Any]

    def to_record(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class WriterRequest:
    run_id: str
    branch_id: str
    step_slot: int
    task_text: str
    hypothesis: str
    transcript: tuple[StepSnapshot, ...]
    granularity: str = "one_task"
    checker_enabled: bool = True
    checker_feedback: tuple[Mapping[str, Any], ...] = ()
    repair_attempts: int = 0
    max_local_repairs: int = 3
    full_transcript: tuple[StepSnapshot, ...] = ()
    exhausted_revision_ids: tuple[str, ...] = ()
    full_checker_feedback: tuple[Mapping[str, Any], ...] = ()
    record_version: str = "1.0"
    preparation_context: Mapping[str, Any] | None = None
    intent_ledger_first: bool = False
    dimension_check: bool = False
    # Whether this run may hold more than one active branch. A run capped at one
    # can never open the child branch a fork asks for, so the decision is removed
    # from the output schema instead of being offered and then discarded.
    fork_available: bool = True
    # Host dispositions this turn should know about that the Writer cannot read
    # off its own transcript. Empty on an ordinary turn, and then the rendered
    # prompt carries no such key at all.
    runtime_notes: tuple[Mapping[str, Any], ...] = ()
    formula_feedback: tuple[Mapping[str, Any], ...] = ()
    formula_validation_policy: str | None = None


@dataclass(frozen=True)
class FormulaValidationResult:
    """Host formatting diagnostics, independent of scientific checking."""

    issues: tuple[Mapping[str, Any], ...] = ()
    infrastructure_error: str | None = None


@dataclass(frozen=True)
class EvidenceSource:
    kind: str
    source_id: str
    text: str

    def to_record(self) -> dict[str, str]:
        return {"kind": self.kind, "source_id": self.source_id, "text": self.text}


@dataclass(frozen=True)
class CheckRequest:
    run_id: str
    check_id: str
    task_text: str
    target: StepSnapshot
    transcript: tuple[StepSnapshot, ...]
    evidence_sources: tuple[EvidenceSource, ...] = ()
    full_transcript: tuple[StepSnapshot, ...] = ()
    record_version: str = "1.0"
    completion_requirements: tuple[str, ...] = ()
    completion_intent: bool = False
    intent_ledger_first: bool = False
    dimension_check: bool = False
    # Whether the step under review opens its branch's route. A revise replaces
    # the route prefix, and a fork inherits it, so this is decided from the
    # branch's current step list rather than from the step's own slot.
    target_is_route_first_step: bool = False


@dataclass(frozen=True)
class JudgeRequest:
    run_id: str
    judgement_id: str
    candidate_id: str
    task_text: str
    transcript: tuple[StepSnapshot, ...]
    unresolved_checks: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class WriterOutput:
    content: StepContent
    control: WriterControl
    finish_reason: str
    usage: Usage


@dataclass(frozen=True)
class CheckEvidence:
    kind: str
    source_id: str
    quote: str

    def to_record(self) -> dict[str, str]:
        return {"kind": self.kind, "source_id": self.source_id, "quote": self.quote}


@dataclass(frozen=True)
class CheckOutput:
    verdict: str
    reason: str
    evidence: tuple[CheckEvidence, ...]
    finish_reason: str
    usage: Usage
    raw_output: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class JudgeOutput:
    verdict: str
    reason: str
    score: float | None
    finish_reason: str
    usage: Usage


@dataclass(frozen=True)
class FormulaRepairItem:
    """One formula of a finished route that did not compile.

    ``latex`` is the body that failed (no math delimiters); ``record_latex`` is
    the recorded body when an earlier correction is what failed.
    """

    formula_id: str
    field: str
    latex: str
    compiler_error: str
    record_latex: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.formula_id, "formula id")
        _non_empty(self.field, "formula field")
        _non_empty(self.compiler_error, "compiler error")

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "formula_id": self.formula_id,
            "field": self.field,
            "latex": self.latex,
            "compiler_error": self.compiler_error,
        }
        if self.record_latex is not None:
            record["record_latex"] = self.record_latex
        return record


@dataclass(frozen=True)
class FormulaRepairStep:
    step_revision_id: str
    content: StepContent
    formulas: tuple[FormulaRepairItem, ...]

    def __post_init__(self) -> None:
        _non_empty(self.step_revision_id, "step revision id")
        if not self.formulas:
            raise ValueError("a formula repair step needs at least one formula")


@dataclass(frozen=True)
class FormulaRepairRequest:
    """Typeset-layer repair turn: corrected LaTeX per failing formula id.

    Not a scientific call: it is never written to Record. It runs on a fresh
    Writer thread without literature or transcript tools.
    """

    run_id: str
    repair_id: str
    route_id: str
    steps: tuple[FormulaRepairStep, ...]

    def __post_init__(self) -> None:
        _non_empty(self.repair_id, "repair id")
        _non_empty(self.route_id, "route id")
        ids = [item.formula_id for step in self.steps for item in step.formulas]
        if not ids:
            raise ValueError("formula repair needs at least one formula")
        if len(ids) != len(set(ids)):
            raise ValueError("formula repair ids must be unique")

    @property
    def formula_ids(self) -> tuple[str, ...]:
        return tuple(item.formula_id for step in self.steps for item in step.formulas)


@dataclass(frozen=True)
class FormulaRepairOutput:
    #: (formula_id, corrected LaTeX body), one per requested id.
    corrections: tuple[tuple[str, str], ...]
    finish_reason: str
    usage: Usage
    raw_output: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FormulaReviewItem:
    formula_id: str
    step_revision_id: str
    field: str
    original_latex: str
    corrected_latex: str
    compiler_error: str

    def __post_init__(self) -> None:
        _non_empty(self.formula_id, "formula id")
        _non_empty(self.step_revision_id, "step revision id")

    def to_record(self) -> dict[str, str]:
        return {
            "formula_id": self.formula_id,
            "step_revision_id": self.step_revision_id,
            "field": self.field,
            "original_latex": self.original_latex,
            "corrected_latex": self.corrected_latex,
            "compiler_error": self.compiler_error,
        }


@dataclass(frozen=True)
class FormulaEquivalenceRequest:
    """Typeset-layer light review of corrections beyond syntax-only edits."""

    run_id: str
    review_id: str
    route_id: str
    steps: tuple[StepSnapshot, ...]
    items: tuple[FormulaReviewItem, ...]

    def __post_init__(self) -> None:
        _non_empty(self.review_id, "review id")
        _non_empty(self.route_id, "route id")
        ids = [item.formula_id for item in self.items]
        if not ids:
            raise ValueError("formula review needs at least one item")
        if len(ids) != len(set(ids)):
            raise ValueError("formula review ids must be unique")
        known = {step.step_revision_id for step in self.steps}
        if any(item.step_revision_id not in known for item in self.items):
            raise ValueError("formula review item refers to a step not supplied")


@dataclass(frozen=True)
class FormulaEquivalenceOutput:
    #: (formula_id, "equivalent" | "not_equivalent", reason), one per item.
    verdicts: tuple[tuple[str, str, str], ...]
    finish_reason: str
    usage: Usage
    raw_output: str | None = field(default=None, compare=False)


RuntimeOutput: TypeAlias = (
    WriterOutput
    | CheckOutput
    | JudgeOutput
    | FormulaRepairOutput
    | FormulaEquivalenceOutput
)


@dataclass(frozen=True)
class RuntimeSession:
    session_id: str
    lineage: ProviderLineage

    def __post_init__(self) -> None:
        _non_empty(self.session_id, "runtime session id")


@dataclass(frozen=True)
class RuntimeInvocation:
    session: RuntimeSession
    operation_id: str
    role: ModelRole

    def __post_init__(self) -> None:
        _non_empty(self.operation_id, "runtime operation id")


@dataclass(frozen=True)
class RuntimeInterruption:
    partial_output: str


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    output: RuntimeOutput | None
    partial_output: str
    failure_kind: str | None
    message: str | None
    retryable: bool


class RuntimeInvocationError(RuntimeError):
    def __init__(
        self,
        failure_kind: str,
        message: str,
        *,
        partial_output: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.failure_kind = _non_empty(failure_kind, "failure_kind")
        self.partial_output = partial_output
        self.retryable = retryable


class RuntimeInvariantError(RuntimeError):
    """Raised when an orchestration transition would violate accepted semantics."""


class ProviderForkError(RuntimeError):
    """One provider fork attempt failed, and nothing else did.

    The provider session the fork was taken from, the run that asked for it and
    any other run sharing the same child are all still usable.  The orchestrator
    answers this by recording the refused alternative and continuing the parent
    branch; it must never end a run.
    """


class ModelRuntime(Protocol):
    """Opaque, provider-neutral execution interface used by the orchestrator."""

    async def start_writer(
        self, request: WriterRequest, session: RuntimeSession | None
    ) -> RuntimeInvocation: ...

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput: ...

    async def start_checker(self, request: CheckRequest) -> RuntimeInvocation: ...

    async def collect_checker(self, invocation: RuntimeInvocation) -> CheckOutput: ...

    async def start_judge(self, request: JudgeRequest) -> RuntimeInvocation: ...

    async def collect_judge(self, invocation: RuntimeInvocation) -> JudgeOutput: ...

    async def fork(
        self, session: RuntimeSession, completed_operation_id: str
    ) -> RuntimeSession: ...

    async def rehydrate(self, transcript: Sequence[StepSnapshot]) -> RuntimeSession: ...

    def rebind_branch_writer_session(
        self, branch_id: str, session: RuntimeSession
    ) -> None:
        """Bind a Branch to the writer session it was just given.

        A Branch is advanced by exactly one session at a time.  When the
        orchestrator rotates a Branch's provider context, or rehydrates it
        after losing its bookmark, the Branch is handed a session it is not
        bound to yet, and the runtime has no other way to tell that apart from
        a turn about to land on the wrong thread.
        """

    async def interrupt(self, invocation: RuntimeInvocation) -> RuntimeInterruption: ...

    async def reconcile(self, invocation: RuntimeInvocation) -> ReconcileResult: ...


class FormulaTypesetRuntime(Protocol):
    """Optional runtime support for the typeset layer of a finished route.

    Both kinds run on fresh threads with the run's configured Writer (repair)
    or Checker (review) model, effort and service tier, and without tools.
    They are not scientific calls: they are never written to Record and never
    reconciled after a restart. The typeset layer audits them and counts them
    against ``max_model_calls``.
    """

    async def start_formula_repair(
        self, request: FormulaRepairRequest
    ) -> RuntimeInvocation: ...

    async def collect_formula_repair(
        self, invocation: RuntimeInvocation
    ) -> FormulaRepairOutput: ...

    async def start_formula_review(
        self, request: FormulaEquivalenceRequest
    ) -> RuntimeInvocation: ...

    async def collect_formula_review(
        self, invocation: RuntimeInvocation
    ) -> FormulaEquivalenceOutput: ...


__all__ = [
    "ArtifactRef",
    "BranchAlternative",
    "CheckEvidence",
    "CheckOutput",
    "CheckRequest",
    "ContentRef",
    "EvidenceSource",
    "FormulaEquivalenceOutput",
    "FormulaEquivalenceRequest",
    "FormulaRepairItem",
    "FormulaRepairOutput",
    "FormulaRepairRequest",
    "FormulaRepairStep",
    "FormulaReviewItem",
    "FormulaTypesetRuntime",
    "InputPolicy",
    "JudgeOutput",
    "JudgeRequest",
    "ModelRole",
    "ModelRuntime",
    "ModelSpec",
    "ProviderForkError",
    "ProviderLineage",
    "ReconcileResult",
    "ReconcileStatus",
    "RunConfig",
    "RunPhase",
    "RuntimeInterruption",
    "RuntimeInvariantError",
    "RuntimeInvocation",
    "RuntimeInvocationError",
    "RuntimeOutput",
    "RuntimeSession",
    "StepContent",
    "StepSnapshot",
    "Usage",
    "WriterControl",
    "WriterDecision",
    "WriterOutput",
    "WriterRequest",
]
