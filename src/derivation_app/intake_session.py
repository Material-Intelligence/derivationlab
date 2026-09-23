"""Persistent domain state for multi-round scientific Problem Intake."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _identifier(value: str, name: str) -> str:
    identifier = _text(value, name)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(f"{name} must be a portable identifier")
    return identifier


def _unique(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


class IntakeSessionStatus(StrEnum):
    ACTIVE = "active"
    CONVERGENCE_REQUIRED = "convergence_required"
    CANDIDATE_READY = "candidate_ready"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class ProblemSpecificationStatus(StrEnum):
    DRAFT = "draft"
    CANDIDATE_READY = "candidate_ready"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


class DecisionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class AnswerMode(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    CHOICE_WITH_TEXT = "choice_with_text"
    TEXT = "text"


class DecisionClass(StrEnum):
    """The three kinds of scientific decision an Intake round can produce.

    Only ``PROBLEM`` decisions may become questions: their answer changes the
    target quantity, the physics that is included or excluded, or touches an
    explicit user constraint. ``CONVENTION`` and ``APPROXIMATION_LEVEL``
    decisions are interchangeable or laddered, so they become declared defaults
    that the derivation agent owns.
    """

    PROBLEM = "problem"
    CONVENTION = "convention"
    APPROXIMATION_LEVEL = "approximation_level"


class AnswerStrategy(StrEnum):
    """How the user wants a problem-class decision to be carried forward."""

    SELECTED = "selected"
    SIMPLEST_FIRST = "simplest_first"
    BOTH_ROUTES = "both_routes"


class ThreadGenerationStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class PendingSubmissionKind(StrEnum):
    """Which user command a held submission would replay."""

    ROUND = "round"
    FINALIZE = "finalize"


REQUIRED_SPEC_SECTIONS = (
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


@dataclass(frozen=True)
class ProblemSection:
    name: str
    content: str | None = None
    not_applicable_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "section name"))
        if (self.content is None) == (self.not_applicable_reason is None):
            raise ValueError(
                "a problem section requires exactly one of content or "
                "not_applicable_reason"
            )
        if self.content is not None:
            object.__setattr__(self, "content", _text(self.content, "section content"))
        if self.not_applicable_reason is not None:
            object.__setattr__(
                self,
                "not_applicable_reason",
                _text(self.not_applicable_reason, "not-applicable reason"),
            )


@dataclass(frozen=True)
class DeclaredDefault:
    """A convention or approximation the agent adopts without asking the user."""

    default_id: str
    decision_class: DecisionClass
    title: str
    statement: str
    rationale: str
    alternatives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "default_id", _identifier(self.default_id, "default_id")
        )
        object.__setattr__(self, "decision_class", DecisionClass(self.decision_class))
        for name in ("title", "statement", "rationale"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "alternatives",
            _unique(
                tuple(
                    _text(value, "declared default alternative")
                    for value in self.alternatives
                ),
                "declared default alternatives",
            ),
        )


@dataclass(frozen=True)
class LadderRung:
    """One level of the refinement ladder from the simplest baseline upward."""

    rung: int
    name: str
    relaxes: str
    default_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    parallel_branch: bool = False

    def __post_init__(self) -> None:
        if self.rung < 0:
            raise ValueError("refinement ladder rungs are numbered from zero")
        for name in ("name", "relaxes"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "default_ids",
            _unique(
                tuple(
                    _identifier(value, "ladder rung default_id")
                    for value in self.default_ids
                ),
                "ladder rung default IDs",
            ),
        )
        object.__setattr__(
            self,
            "decision_ids",
            _unique(
                tuple(
                    _identifier(value, "ladder rung decision_id")
                    for value in self.decision_ids
                ),
                "ladder rung decision IDs",
            ),
        )


@dataclass(frozen=True)
class ProblemSpecification:
    """One versioned specification, optionally carrying a refinement ladder.

    The ladder is structurally validated here, but "a ready specification must
    have one" is enforced where a ready specification is produced — model
    parsing (:func:`parse_intake_round_proposal`) and confirmation — so that
    snapshots written before the ladder existed still load.
    """

    version: int
    status: ProblemSpecificationStatus
    sections: tuple[ProblemSection, ...]
    critical_message_refs: tuple[str, ...] = ()
    supersedes_version: int | None = None
    declared_defaults: tuple[DeclaredDefault, ...] = ()
    refinement_ladder: tuple[LadderRung, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("problem specification version must be positive")
        names = tuple(section.name for section in self.sections)
        _unique(names, "problem specification section names")
        missing = tuple(name for name in REQUIRED_SPEC_SECTIONS if name not in names)
        ready = self.status in {
            ProblemSpecificationStatus.CANDIDATE_READY,
            ProblemSpecificationStatus.CONFIRMED,
        }
        if missing and ready:
            raise ValueError(
                "ready problem specification is missing required sections: "
                + ", ".join(missing)
            )
        default_ids = _unique(
            tuple(item.default_id for item in self.declared_defaults),
            "declared default IDs",
        )
        rungs = tuple(item.rung for item in self.refinement_ladder)
        if self.refinement_ladder:
            if rungs != tuple(range(len(rungs))):
                raise ValueError(
                    "refinement ladder rungs must be contiguous and start at zero"
                )
            unknown = tuple(
                default_id
                for item in self.refinement_ladder
                for default_id in item.default_ids
                if default_id not in default_ids
            )
            if unknown:
                raise ValueError(
                    "refinement ladder references undeclared defaults: "
                    + ", ".join(sorted(set(unknown)))
                )
        object.__setattr__(
            self,
            "critical_message_refs",
            _unique(
                tuple(
                    _text(ref, "critical message reference")
                    for ref in self.critical_message_refs
                ),
                "critical message references",
            ),
        )
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("initial problem specification cannot supersede a version")
        if self.version > 1 and self.supersedes_version != self.version - 1:
            raise ValueError(
                "problem specification must immediately supersede its predecessor"
            )


@dataclass(frozen=True)
class QuestionOption:
    option_id: str
    label: str
    impact: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "option_id", _identifier(self.option_id, "option_id"))
        object.__setattr__(self, "label", _text(self.label, "option label"))
        object.__setattr__(self, "impact", _text(self.impact, "option impact"))


@dataclass(frozen=True)
class IntakeQuestion:
    question_id: str
    decision_id: str
    semantic_key: str
    title: str
    prompt: str
    why_needed: str
    why_it_matters: str
    answer_mode: AnswerMode
    decision_class: DecisionClass = DecisionClass.PROBLEM
    options: tuple[QuestionOption, ...] = ()
    recommended_option_ids: tuple[str, ...] = ()
    recommendation_reason: str | None = None
    allow_custom: bool = False
    depends_on: tuple[str, ...] = ()
    blocking: bool = True
    grounded_in: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "question_id",
            "decision_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in ("semantic_key", "title", "prompt", "why_needed", "why_it_matters"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "decision_class", DecisionClass(self.decision_class))
        if self.decision_class is not DecisionClass.PROBLEM:
            raise ValueError(
                "only problem-class decisions may become Intake questions; "
                f"{self.decision_class.value} decisions are declared defaults"
            )
        if not self.blocking:
            raise ValueError("problem-class Intake questions are always blocking")
        if self.grounded_in is not None:
            object.__setattr__(
                self, "grounded_in", _text(self.grounded_in, "grounded_in")
            )
        option_ids = _unique(
            tuple(option.option_id for option in self.options), "question option IDs"
        )
        recommended = _unique(
            tuple(
                _text(value, "recommended option ID")
                for value in self.recommended_option_ids
            ),
            "recommended option IDs",
        )
        if any(value not in option_ids for value in recommended):
            raise ValueError("recommended options must belong to the question")
        choice_mode = self.answer_mode is not AnswerMode.TEXT
        if choice_mode and not self.options:
            raise ValueError("choice questions require options")
        if not choice_mode and self.options:
            raise ValueError("text questions cannot declare options")
        if choice_mode and not self.allow_custom:
            raise ValueError("choice questions must allow custom input")
        if recommended and self.recommendation_reason is None:
            raise ValueError("recommended options require a public reason")
        if self.recommendation_reason is not None:
            object.__setattr__(
                self,
                "recommendation_reason",
                _text(self.recommendation_reason, "recommendation reason"),
            )
        dependencies = _unique(
            tuple(_text(value, "decision dependency") for value in self.depends_on),
            "decision dependencies",
        )
        if self.decision_id in dependencies:
            raise ValueError("a decision cannot depend on itself")
        object.__setattr__(self, "depends_on", dependencies)


@dataclass(frozen=True)
class DecisionAnswer:
    selected_option_ids: tuple[str, ...] = ()
    custom_text: str | None = None
    source_message_refs: tuple[str, ...] = ()
    strategy: AnswerStrategy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_option_ids",
            _unique(
                tuple(
                    _text(value, "selected option ID")
                    for value in self.selected_option_ids
                ),
                "selected option IDs",
            ),
        )
        if self.custom_text is not None:
            object.__setattr__(
                self, "custom_text", _text(self.custom_text, "custom answer")
            )
        if self.strategy is not None:
            object.__setattr__(self, "strategy", AnswerStrategy(self.strategy))
        if (
            not self.selected_option_ids
            and self.custom_text is None
            and self.strategy in {None, AnswerStrategy.SELECTED}
        ):
            raise ValueError("a decision answer cannot be empty")
        object.__setattr__(
            self,
            "source_message_refs",
            _unique(
                tuple(
                    _text(value, "source message reference")
                    for value in self.source_message_refs
                ),
                "source message references",
            ),
        )


@dataclass(frozen=True)
class DecisionEntry:
    decision_id: str
    semantic_key: str
    status: DecisionStatus
    question: IntakeQuestion
    revision: int = 1
    supersedes_revision: int | None = None
    answer: DecisionAnswer | None = None
    reopen_reason: str | None = None
    source_message_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_id", _identifier(self.decision_id, "decision_id")
        )
        object.__setattr__(
            self, "semantic_key", _text(self.semantic_key, "semantic_key")
        )
        if self.question.decision_id != self.decision_id:
            raise ValueError("decision and question IDs must match")
        if self.question.semantic_key != self.semantic_key:
            raise ValueError("decision and question semantic keys must match")
        if self.revision < 1:
            raise ValueError("decision revision must be positive")
        if self.revision == 1 and self.supersedes_revision is not None:
            raise ValueError("initial decision cannot supersede a revision")
        if self.revision > 1 and self.supersedes_revision != self.revision - 1:
            raise ValueError("decision must immediately supersede its predecessor")
        if self.status is DecisionStatus.RESOLVED and self.answer is None:
            raise ValueError("a resolved decision requires an answer")
        if self.status is DecisionStatus.OPEN and self.answer is not None:
            raise ValueError("an open decision cannot already have an answer")
        option_ids = {option.option_id for option in self.question.options}
        if self.answer is not None:
            answer = self._normalized_answer(self.answer)
            object.__setattr__(self, "answer", answer)
            selected = answer.selected_option_ids
            if any(option_id not in option_ids for option_id in selected):
                raise ValueError("decision answer selected an unknown option")
            if (
                self.question.answer_mode
                in {AnswerMode.SINGLE_CHOICE, AnswerMode.CHOICE_WITH_TEXT}
                and len(selected) > 1
                and answer.strategy is not AnswerStrategy.BOTH_ROUTES
            ):
                raise ValueError("single-choice decisions accept at most one option")
            if self.question.answer_mode is AnswerMode.TEXT and selected:
                raise ValueError("text decisions cannot select options")
        if (
            self.revision > 1
            and self.status is DecisionStatus.OPEN
            and self.reopen_reason is None
        ):
            raise ValueError("a reopened decision requires a reason")
        if self.reopen_reason is not None:
            object.__setattr__(
                self, "reopen_reason", _text(self.reopen_reason, "reopen reason")
            )
        object.__setattr__(
            self,
            "source_message_refs",
            _unique(
                tuple(
                    _text(value, "decision source message reference")
                    for value in self.source_message_refs
                ),
                "decision source message references",
            ),
        )

    def _normalized_answer(self, answer: DecisionAnswer) -> DecisionAnswer:
        """Validate the answer strategy and expand an unselected both-routes fork.

        ``both_routes`` without an explicit selection means "run every offered
        route", so the stored decision names the options instead of leaving the
        downstream Writer to guess which fork was intended.
        """

        if (
            answer.strategy
            in {
                AnswerStrategy.SIMPLEST_FIRST,
                AnswerStrategy.BOTH_ROUTES,
            }
            and self.question.answer_mode is AnswerMode.TEXT
        ):
            raise ValueError(
                "simplest-first and both-routes strategies require a choice question"
            )
        if answer.strategy is not AnswerStrategy.BOTH_ROUTES:
            return answer
        if len(self.question.options) < 2:
            raise ValueError("both-routes strategy requires at least two options")
        if not answer.selected_option_ids:
            return replace(
                answer,
                selected_option_ids=tuple(
                    option.option_id for option in self.question.options
                ),
            )
        if len(answer.selected_option_ids) < 2:
            raise ValueError("both-routes strategy requires at least two options")
        return answer


@dataclass(frozen=True)
class ThreadGeneration:
    generation: int
    status: ThreadGenerationStatus
    app_server_thread_id: str
    prompt_version: str
    replaced_generation: int | None = None
    replacement_reason: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("thread generation must be positive")
        object.__setattr__(
            self,
            "app_server_thread_id",
            _identifier(self.app_server_thread_id, "App Server thread ID"),
        )
        object.__setattr__(
            self, "prompt_version", _text(self.prompt_version, "prompt version")
        )
        if self.generation == 1 and self.replaced_generation is not None:
            raise ValueError("initial thread generation cannot replace another")
        if self.generation > 1 and self.replaced_generation != self.generation - 1:
            raise ValueError("a replacement must follow the prior generation")
        if self.replacement_reason is not None:
            object.__setattr__(
                self,
                "replacement_reason",
                _text(self.replacement_reason, "replacement reason"),
            )


@dataclass(frozen=True)
class ConversationEvent:
    event_id: str
    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        object.__setattr__(self, "kind", _text(self.kind, "event kind"))
        json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ConvergenceState:
    """Server-side budget that ends the grill instead of trusting the model."""

    rounds: int = 0
    audit_rejections: int = 0
    reason: str | None = None
    finalized_by_user: bool = False

    def __post_init__(self) -> None:
        if self.rounds < 0:
            raise ValueError("convergence round count cannot be negative")
        if self.audit_rejections < 0:
            raise ValueError("convergence audit rejection count cannot be negative")
        if self.reason is not None:
            object.__setattr__(self, "reason", _text(self.reason, "convergence reason"))


@dataclass(frozen=True)
class IntakeSession:
    session_id: str
    revision: int
    status: IntakeSessionStatus
    problem_specifications: tuple[ProblemSpecification, ...]
    decisions: tuple[DecisionEntry, ...]
    frontier: tuple[IntakeQuestion, ...]
    thread_generations: tuple[ThreadGeneration, ...]
    pending_problem_questions: tuple[IntakeQuestion, ...] = ()
    convergence: ConvergenceState = ConvergenceState()
    model: str = "gpt-5.4"
    effort: str = "low"
    service_tier: str = "standard"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", _identifier(self.session_id, "session_id")
        )
        object.__setattr__(self, "model", _text(self.model, "model"))
        object.__setattr__(self, "effort", _text(self.effort, "effort"))
        object.__setattr__(
            self, "service_tier", _text(self.service_tier, "service_tier")
        )
        if self.service_tier not in {"standard", "fast"}:
            raise ValueError("service_tier must be standard or fast")
        if self.revision < 0:
            raise ValueError("session revision cannot be negative")
        spec_versions = tuple(item.version for item in self.problem_specifications)
        if spec_versions != tuple(range(1, len(spec_versions) + 1)):
            raise ValueError("problem specification versions must be contiguous")
        if any(
            item.status is not ProblemSpecificationStatus.SUPERSEDED
            for item in self.problem_specifications[:-1]
        ):
            raise ValueError("older problem specifications must be superseded")
        histories: dict[str, list[DecisionEntry]] = {}
        for entry in self.decisions:
            histories.setdefault(entry.decision_id, []).append(entry)
        latest_decisions: dict[str, DecisionEntry] = {}
        for decision_id, entries in histories.items():
            revisions = tuple(entry.revision for entry in entries)
            if revisions != tuple(range(1, len(entries) + 1)):
                raise ValueError("decision revisions must be contiguous and ordered")
            if any(
                entry.status is not DecisionStatus.SUPERSEDED for entry in entries[:-1]
            ):
                raise ValueError("older decision revisions must be superseded")
            latest_decisions[decision_id] = entries[-1]
        _unique(
            tuple(item.semantic_key for item in latest_decisions.values()),
            "active decision semantic keys",
        )
        resolved = {
            item.decision_id
            for item in latest_decisions.values()
            if item.status is DecisionStatus.RESOLVED
        }
        frontier_ids = _unique(
            tuple(item.decision_id for item in self.frontier), "frontier decision IDs"
        )
        for question in self.frontier:
            entry = latest_decisions.get(question.decision_id)
            if entry is None or entry.status is not DecisionStatus.OPEN:
                raise ValueError("frontier questions must point to open decisions")
            if not set(question.depends_on).issubset(resolved):
                raise ValueError("frontier dependencies must already be resolved")
        if any(decision_id in resolved for decision_id in frontier_ids):
            raise ValueError("resolved decisions cannot reappear in the frontier")
        pending_ids = _unique(
            tuple(item.decision_id for item in self.pending_problem_questions),
            "pending problem decision IDs",
        )
        if set(pending_ids) & set(frontier_ids):
            raise ValueError(
                "a decision cannot be both on the frontier and pending user judgement"
            )
        for question in self.pending_problem_questions:
            entry = latest_decisions.get(question.decision_id)
            if entry is None or entry.status is not DecisionStatus.OPEN:
                raise ValueError("pending questions must point to open decisions")
        generation_numbers = tuple(item.generation for item in self.thread_generations)
        if generation_numbers != tuple(range(1, len(generation_numbers) + 1)):
            raise ValueError("thread generations must be contiguous")
        if any(
            item.status is not ThreadGenerationStatus.SUPERSEDED
            for item in self.thread_generations[:-1]
        ):
            raise ValueError("older thread generations must be superseded")
        active_generations = tuple(
            item
            for item in self.thread_generations
            if item.status is ThreadGenerationStatus.ACTIVE
        )
        if len(active_generations) > 1:
            raise ValueError("an IntakeSession can have only one active thread")
        if self.status in {
            IntakeSessionStatus.CANDIDATE_READY,
            IntakeSessionStatus.CONFIRMED,
        }:
            if any(
                entry.status is DecisionStatus.OPEN and entry.question.blocking
                for entry in latest_decisions.values()
            ):
                raise ValueError("ready sessions cannot have open blocking decisions")
            if any(question.blocking for question in self.frontier):
                raise ValueError("ready sessions cannot expose a blocking frontier")
            if self.pending_problem_questions:
                raise ValueError("ready sessions cannot hold pending problem questions")
        if self.status is IntakeSessionStatus.CONVERGENCE_REQUIRED:
            if not self.problem_specifications:
                raise ValueError(
                    "convergence-required sessions require a specification"
                )
            if self.frontier:
                raise ValueError(
                    "convergence-required sessions replace the frontier with "
                    "pending problem questions"
                )
            if self.convergence.reason is None:
                raise ValueError("convergence-required sessions must record a reason")
        if self.status is IntakeSessionStatus.CONFIRMED:
            if not self.problem_specifications:
                raise ValueError("confirmed sessions require a problem specification")
            if (
                self.problem_specifications[-1].status
                is not ProblemSpecificationStatus.CONFIRMED
            ):
                raise ValueError("confirmed sessions require a confirmed latest spec")
        if self.status is IntakeSessionStatus.CANDIDATE_READY:
            if not self.problem_specifications:
                raise ValueError("candidate-ready sessions require a specification")
            if (
                self.problem_specifications[-1].status
                is not ProblemSpecificationStatus.CANDIDATE_READY
            ):
                raise ValueError(
                    "candidate-ready sessions require a candidate-ready latest spec"
                )

    def next_revision(self, **changes: Any) -> IntakeSession:
        return replace(self, revision=self.revision + 1, **changes)


class IntakeStoreError(RuntimeError):
    pass


class IntakeSessionNotFound(IntakeStoreError):
    pass


class IntakeStaleRevision(IntakeStoreError):
    pass


class IntakeIdempotencyConflict(IntakeStoreError):
    pass


class IntakeCommandRejected(IntakeStoreError):
    """Expected command refusal with a stable API-facing error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = _identifier(code, "intake error code")


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def intake_session_payload(session: IntakeSession) -> dict[str, Any]:
    """Return the canonical JSON-compatible snapshot used at process boundaries."""

    payload = _jsonable(session)
    if not isinstance(payload, dict):
        raise TypeError("IntakeSession payload must be an object")
    return payload


def problem_specification_payload(
    specification: ProblemSpecification,
) -> dict[str, Any]:
    """Return a JSON-compatible specification for an independent audit."""

    payload = _jsonable(specification)
    if not isinstance(payload, dict):
        raise TypeError("ProblemSpecification payload must be an object")
    return payload


def intake_question_payload(question: IntakeQuestion) -> dict[str, Any]:
    """Return the complete user-visible question recorded in the archive."""

    payload = _jsonable(question)
    if not isinstance(payload, dict):
        raise TypeError("IntakeQuestion payload must be an object")
    return payload


def intake_session_fingerprint(session: IntakeSession) -> str:
    """Hash the canonical state used to prove lossless thread rehydration."""

    encoded = json.dumps(
        intake_session_payload(session),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _question(raw: Mapping[str, Any]) -> IntakeQuestion:
    # Snapshots written before prompt version intake-grill-v3 carry neither a
    # decision class nor why_it_matters, and could carry a non-blocking
    # question. Under v3 every persisted question is a blocking problem-class
    # decision, so a legacy snapshot loads as one instead of failing closed.
    return IntakeQuestion(
        question_id=raw["question_id"],
        decision_id=raw["decision_id"],
        semantic_key=raw["semantic_key"],
        title=raw["title"],
        prompt=raw["prompt"],
        why_needed=raw["why_needed"],
        why_it_matters=raw.get("why_it_matters") or raw["why_needed"],
        answer_mode=AnswerMode(raw["answer_mode"]),
        decision_class=DecisionClass(raw.get("decision_class", DecisionClass.PROBLEM)),
        options=tuple(QuestionOption(**item) for item in raw["options"]),
        recommended_option_ids=tuple(raw["recommended_option_ids"]),
        recommendation_reason=raw["recommendation_reason"],
        allow_custom=raw["allow_custom"],
        depends_on=tuple(raw["depends_on"]),
        blocking=True,
        grounded_in=raw.get("grounded_in"),
    )


def _specification(raw: Mapping[str, Any]) -> ProblemSpecification:
    return ProblemSpecification(
        version=raw["version"],
        status=ProblemSpecificationStatus(raw["status"]),
        sections=tuple(ProblemSection(**section) for section in raw["sections"]),
        critical_message_refs=tuple(raw["critical_message_refs"]),
        supersedes_version=raw["supersedes_version"],
        declared_defaults=tuple(
            DeclaredDefault(
                default_id=item["default_id"],
                decision_class=DecisionClass(item["decision_class"]),
                title=item["title"],
                statement=item["statement"],
                rationale=item["rationale"],
                alternatives=tuple(item["alternatives"]),
            )
            for item in raw.get("declared_defaults", ())
        ),
        refinement_ladder=tuple(
            LadderRung(
                rung=item["rung"],
                name=item["name"],
                relaxes=item["relaxes"],
                default_ids=tuple(item["default_ids"]),
                decision_ids=tuple(item["decision_ids"]),
                parallel_branch=item["parallel_branch"],
            )
            for item in raw.get("refinement_ladder", ())
        ),
    )


def _convergence(raw: Mapping[str, Any] | None) -> ConvergenceState:
    # Snapshots written before the server-side convergence gate carry no budget.
    if raw is None:
        return ConvergenceState()
    return ConvergenceState(
        rounds=raw["rounds"],
        audit_rejections=raw["audit_rejections"],
        reason=raw["reason"],
        finalized_by_user=raw["finalized_by_user"],
    )


def _is_open_legacy_optional_decision(raw: Mapping[str, Any]) -> bool:
    return (
        raw.get("status") == DecisionStatus.OPEN.value
        and raw["question"].get("blocking") is False
    )


def _session_from_json(payload: str) -> IntakeSession:
    raw = json.loads(payload)
    specs = tuple(_specification(item) for item in raw["problem_specifications"])
    legacy_optional_ids = {
        item["decision_id"]
        for item in raw["decisions"]
        if _is_open_legacy_optional_decision(item)
    }
    decisions = []
    for item in raw["decisions"]:
        answer_raw = item["answer"]
        status = DecisionStatus(item["status"])
        if item["decision_id"] in legacy_optional_ids:
            status = DecisionStatus.SUPERSEDED
        decisions.append(
            DecisionEntry(
                decision_id=item["decision_id"],
                semantic_key=item["semantic_key"],
                status=status,
                question=_question(item["question"]),
                revision=item["revision"],
                supersedes_revision=item["supersedes_revision"],
                answer=(
                    None
                    if answer_raw is None
                    else DecisionAnswer(
                        selected_option_ids=tuple(answer_raw["selected_option_ids"]),
                        custom_text=answer_raw["custom_text"],
                        source_message_refs=tuple(answer_raw["source_message_refs"]),
                        strategy=(
                            None
                            if answer_raw.get("strategy") is None
                            else AnswerStrategy(answer_raw["strategy"])
                        ),
                    )
                ),
                reopen_reason=item["reopen_reason"],
                source_message_refs=tuple(item.get("source_message_refs", ())),
            )
        )
    if legacy_optional_ids:
        logger.info(
            "withdrew legacy optional Intake decisions from session %s: %s",
            raw["session_id"],
            ", ".join(sorted(legacy_optional_ids)),
        )
    return IntakeSession(
        session_id=raw["session_id"],
        revision=raw["revision"],
        status=IntakeSessionStatus(raw["status"]),
        problem_specifications=specs,
        decisions=tuple(decisions),
        frontier=tuple(
            _question(item)
            for item in raw["frontier"]
            if item["decision_id"] not in legacy_optional_ids
        ),
        thread_generations=tuple(
            ThreadGeneration(
                generation=item["generation"],
                status=ThreadGenerationStatus(item["status"]),
                app_server_thread_id=item["app_server_thread_id"],
                prompt_version=item["prompt_version"],
                replaced_generation=item["replaced_generation"],
                replacement_reason=item["replacement_reason"],
            )
            for item in raw["thread_generations"]
        ),
        pending_problem_questions=tuple(
            _question(item)
            for item in raw.get("pending_problem_questions", ())
            if item["decision_id"] not in legacy_optional_ids
        ),
        convergence=_convergence(raw.get("convergence")),
        model=raw.get("model") or "gpt-5.4",
        effort=raw.get("effort") or "low",
        service_tier=raw.get("service_tier") or "standard",
    )


@dataclass(frozen=True)
class PendingSubmission:
    """One submission the user has pressed send on, held beside the event chain.

    A round commits the user's answers and the model's reply in a single
    event-chain write, so a model call that dies after the user pressed submit
    used to leave the server with no trace of what they typed. The journal is
    the side channel that keeps it: written before the model call, stamped with
    a failure reason if the round dies, dropped once the round commits. It is
    never part of the append-only chain and never changes a revision.
    """

    session_id: str
    kind: PendingSubmissionKind
    base_revision: int
    idempotency_key: str
    submitted_at: str
    answers: Mapping[str, DecisionAnswer] = field(default_factory=dict)
    user_message: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _text(self.session_id, "session_id"))
        object.__setattr__(self, "kind", PendingSubmissionKind(self.kind))
        if self.base_revision < 0:
            raise ValueError("a held submission must cite a non-negative revision")
        object.__setattr__(
            self, "idempotency_key", _text(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(
            self, "submitted_at", _text(self.submitted_at, "submitted_at")
        )
        object.__setattr__(
            self,
            "answers",
            {
                _text(key, "decision ID"): value
                for key, value in dict(self.answers).items()
            },
        )
        if self.user_message is not None:
            object.__setattr__(
                self, "user_message", _text(self.user_message, "correction message")
            )
        if self.failure_reason is not None:
            object.__setattr__(
                self, "failure_reason", _text(self.failure_reason, "failure reason")
            )
        if not self.answers and self.user_message is None:
            raise ValueError("a held submission carries answers or a correction")


def _pending_answers_json(answers: Mapping[str, DecisionAnswer]) -> str:
    return json.dumps(
        {key: _jsonable(value) for key, value in answers.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pending_answers(payload: str) -> dict[str, DecisionAnswer]:
    raw = json.loads(payload)
    return {
        key: DecisionAnswer(
            selected_option_ids=tuple(item["selected_option_ids"]),
            custom_text=item["custom_text"],
            source_message_refs=tuple(item.get("source_message_refs", ())),
            strategy=(
                None
                if item.get("strategy") is None
                else AnswerStrategy(item["strategy"])
            ),
        )
        for key, item in raw.items()
    }


class SQLiteIntakeStore:
    """One local authoritative store with optimistic revision and idempotency."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._managed_connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS intake_sessions (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intake_events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    UNIQUE (session_id, event_id),
                    FOREIGN KEY (session_id)
                        REFERENCES intake_sessions(session_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS intake_idempotency (
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, idempotency_key),
                    FOREIGN KEY (session_id)
                        REFERENCES intake_sessions(session_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS intake_create_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    command_hash TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    FOREIGN KEY (session_id)
                        REFERENCES intake_sessions(session_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS intake_pending_submissions (
                    session_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    user_message TEXT,
                    failure_reason TEXT,
                    FOREIGN KEY (session_id)
                        REFERENCES intake_sessions(session_id) ON DELETE RESTRICT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection, connection:
            yield connection

    def create(
        self,
        session: IntakeSession,
        *,
        events: Iterable[ConversationEvent] = (),
        idempotency_key: str | None = None,
        command_hash: str | None = None,
    ) -> None:
        if session.revision != 0:
            raise ValueError("a new IntakeSession must start at revision zero")
        snapshot = json.dumps(
            _jsonable(session),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_items = tuple(events)
        if (idempotency_key is None) != (command_hash is None):
            raise ValueError(
                "create idempotency key and command hash must be supplied together"
            )
        with self._managed_connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO intake_sessions VALUES (?, ?, ?, ?)",
                    (
                        session.session_id,
                        session.revision,
                        session.status.value,
                        snapshot,
                    ),
                )
                self._append_events(connection, session.session_id, event_items)
                if idempotency_key is not None and command_hash is not None:
                    connection.execute(
                        "INSERT INTO intake_create_idempotency VALUES (?, ?, ?)",
                        (
                            _text(idempotency_key, "idempotency_key"),
                            _text(command_hash, "command_hash"),
                            session.session_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise IntakeStoreError("IntakeSession already exists") from exc

    def replay_create(
        self,
        *,
        idempotency_key: str,
        command_hash: str,
    ) -> IntakeSession | None:
        key = _text(idempotency_key, "idempotency_key")
        digest = _text(command_hash, "command_hash")
        with self._managed_connection() as connection:
            row = connection.execute(
                """
                SELECT command_hash, session_id
                FROM intake_create_idempotency
                WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        if row["command_hash"] != digest:
            raise IntakeIdempotencyConflict(key)
        return self.get(row["session_id"])

    def get(self, session_id: str) -> IntakeSession:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM intake_sessions WHERE session_id = ?",
                (_text(session_id, "session_id"),),
            ).fetchone()
        if row is None:
            raise IntakeSessionNotFound(session_id)
        return _session_from_json(row["snapshot_json"])

    def list_active(self) -> tuple[IntakeSession, ...]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json FROM intake_sessions
                WHERE status IN (?, ?, ?)
                ORDER BY session_id
                """,
                (
                    IntakeSessionStatus.ACTIVE.value,
                    IntakeSessionStatus.CONVERGENCE_REQUIRED.value,
                    IntakeSessionStatus.CANDIDATE_READY.value,
                ),
            ).fetchall()
        return tuple(_session_from_json(row["snapshot_json"]) for row in rows)

    def commit(
        self,
        session: IntakeSession,
        *,
        base_revision: int,
        idempotency_key: str,
        command_hash: str,
        events: Iterable[ConversationEvent] = (),
    ) -> IntakeSession:
        key = _text(idempotency_key, "idempotency_key")
        digest = _text(command_hash, "command_hash")
        event_items = tuple(events)
        with self._managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(connection, session.session_id, key)
            if receipt is not None:
                if receipt["command_hash"] != digest:
                    raise IntakeIdempotencyConflict(key)
                return _session_from_json(receipt["snapshot_json"])
            row = connection.execute(
                "SELECT revision FROM intake_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is None:
                raise IntakeSessionNotFound(session.session_id)
            if (
                row["revision"] != base_revision
                or session.revision != base_revision + 1
            ):
                raise IntakeStaleRevision(
                    f"expected revision {row['revision']}, received base {base_revision}"
                )
            snapshot = json.dumps(
                _jsonable(session),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE intake_sessions
                SET revision = ?, status = ?, snapshot_json = ?
                WHERE session_id = ?
                """,
                (
                    session.revision,
                    session.status.value,
                    snapshot,
                    session.session_id,
                ),
            )
            self._append_events(connection, session.session_id, event_items)
            connection.execute(
                "INSERT INTO intake_idempotency VALUES (?, ?, ?, ?, ?)",
                (session.session_id, key, digest, session.revision, snapshot),
            )
            # A held submission is worth keeping only while it still fits the
            # revision it was typed at, which is what `pending_submission`
            # checks before serving it. Once the session has moved past that
            # revision the row is spent: its answers have either landed or been
            # superseded, and nobody will ever be offered them again. The 409
            # retry mints a fresh idempotency key, so the row the retry replaced
            # is not the one `release_submission` deletes, and the user's
            # answers would otherwise sit in the file forever.
            #
            # Strictly behind, never equal: a submission the user has since
            # replaced was held against the revision this commit just produced,
            # so a late failure still cannot delete or stamp it.
            connection.execute(
                """
                DELETE FROM intake_pending_submissions
                WHERE session_id = ? AND base_revision < ?
                """,
                (session.session_id, session.revision),
            )
        return session

    def replay(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        command_hash: str,
    ) -> IntakeSession | None:
        """Return a prior command result before allocating a model call."""

        key = _text(idempotency_key, "idempotency_key")
        digest = _text(command_hash, "command_hash")
        with self._managed_connection() as connection:
            receipt = self._receipt(connection, _text(session_id, "session_id"), key)
        if receipt is None:
            return None
        if receipt["command_hash"] != digest:
            raise IntakeIdempotencyConflict(key)
        return _session_from_json(receipt["snapshot_json"])

    def events(self, session_id: str) -> tuple[ConversationEvent, ...]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, kind, payload_json
                FROM intake_events
                WHERE session_id = ?
                ORDER BY sequence
                """,
                (_text(session_id, "session_id"),),
            ).fetchall()
        return tuple(
            ConversationEvent(
                event_id=row["event_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def hold_submission(self, submission: PendingSubmission) -> None:
        """Keep what the user submitted before the round that may lose it.

        One row per session: a newer submission replaces the older one, because
        only the latest attempt is worth offering back to the user.
        """

        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO intake_pending_submissions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission.session_id,
                    submission.kind.value,
                    submission.base_revision,
                    submission.idempotency_key,
                    submission.submitted_at,
                    _pending_answers_json(submission.answers),
                    submission.user_message,
                    submission.failure_reason,
                ),
            )

    def pending_submission(self, session_id: str) -> PendingSubmission | None:
        """Return the submission still held for this session, if any."""

        with self._managed_connection() as connection:
            row = connection.execute(
                """
                SELECT kind, base_revision, idempotency_key, submitted_at,
                       answers_json, user_message, failure_reason
                FROM intake_pending_submissions
                WHERE session_id = ?
                """,
                (_text(session_id, "session_id"),),
            ).fetchone()
        if row is None:
            return None
        return PendingSubmission(
            session_id=session_id,
            kind=PendingSubmissionKind(row["kind"]),
            base_revision=row["base_revision"],
            idempotency_key=row["idempotency_key"],
            submitted_at=row["submitted_at"],
            answers=_pending_answers(row["answers_json"]),
            user_message=row["user_message"],
            failure_reason=row["failure_reason"],
        )

    def fail_submission(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        failure_reason: str,
    ) -> None:
        """Say why the round died on the submission that is still held for it.

        Keyed by idempotency key as well as session, so a round that fails late
        cannot stamp itself over a submission the user has since replaced.
        """

        with self._managed_connection() as connection:
            connection.execute(
                """
                UPDATE intake_pending_submissions
                SET failure_reason = ?
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (
                    _text(failure_reason, "failure reason"),
                    _text(session_id, "session_id"),
                    _text(idempotency_key, "idempotency_key"),
                ),
            )

    def release_submission(self, session_id: str, *, idempotency_key: str) -> None:
        """Drop the held submission once its round is committed."""

        with self._managed_connection() as connection:
            connection.execute(
                """
                DELETE FROM intake_pending_submissions
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (
                    _text(session_id, "session_id"),
                    _text(idempotency_key, "idempotency_key"),
                ),
            )

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        session_id: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT command_hash, snapshot_json
            FROM intake_idempotency
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, idempotency_key),
        ).fetchone()

    @staticmethod
    def _append_events(
        connection: sqlite3.Connection,
        session_id: str,
        events: tuple[ConversationEvent, ...],
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM intake_events "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        sequence = row["last"]
        for event in events:
            sequence += 1
            connection.execute(
                "INSERT INTO intake_events VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    sequence,
                    event.event_id,
                    event.kind,
                    json.dumps(
                        event.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
