"""App Server adapter for one persistent, frontier-based Intake round."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from derivation_runtime.app_server_protocol import JsonObject
from derivation_runtime.app_server_structured_turn import StructuredTurnResult

from .intake_session import (
    REQUIRED_SPEC_SECTIONS,
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
    intake_session_fingerprint,
    intake_session_payload,
    problem_specification_payload,
)

INTAKE_SESSION_PROMPT_VERSION = "intake-grill-v3"
INTAKE_SESSION_SCHEMA_VERSION = "intake-session-v3"

logger = logging.getLogger(__name__)

MAX_ROUND_QUESTIONS = 4
MAX_ROUND_DECLARED_DEFAULTS = 8
MAX_AUDIT_BLOCKING_QUESTIONS = 3
DEFAULT_MAX_FRONTIER_ROUNDS = 3
DEFAULT_MAX_AUDIT_REJECTIONS = 2

# ``intake_session._IDENTIFIER_RE`` accepts at most this many characters, so an
# id derived from another id has to stay inside the same budget.
MAX_IDENTIFIER_LENGTH = 128


class IntakeRoundMode(StrEnum):
    """Whether the round may still ask, or the user has decided to start."""

    ADVANCE = "advance"
    FINALIZE = "finalize"


class IntakeInvariantGrade(StrEnum):
    """How much a violated Intake invariant is allowed to cost the user.

    A round arrives after a minute of model work and after the user answered the
    whole frontier, so refusing it is the expensive outcome. Only the invariants
    that protect scientific meaning are worth that price.
    """

    #: The payload is not an intake round, or accepting it would silently change
    #: what the derivation is asked to do. The round is refused.
    HARD = "hard"
    #: Bookkeeping the host can settle deterministically without deciding
    #: anything on the user's behalf. Repaired, recorded, never raised.
    REPAIRABLE = "repairable"
    #: Worth knowing about, but nothing is changed and nothing is refused.
    ADVISORY = "advisory"


class IntakeInvariant(StrEnum):
    """Every invariant the Intake parse path can trip, by stable code."""

    # -- hard ---------------------------------------------------------------
    PAYLOAD_SHAPE = "payload_shape"
    PAYLOAD_TYPE = "payload_type"
    QUESTION_NOT_PROBLEM_CLASS = "question_not_problem_class"
    SPECIFICATION_SECTION_INVALID = "specification_section_invalid"
    QUESTION_INVALID = "question_invalid"
    QUESTION_OPTION_INVALID = "question_option_invalid"
    DECLARED_DEFAULT_INVALID = "declared_default_invalid"
    LADDER_RUNG_INVALID = "ladder_rung_invalid"
    SPECIFICATION_INVALID = "specification_invalid"
    FINALIZE_NOT_CANDIDATE_READY = "finalize_not_candidate_ready"
    FINALIZE_STILL_ASKING = "finalize_still_asking"
    FINALIZE_WITHOUT_LADDER = "finalize_without_ladder"
    READY_WITH_BLOCKING_QUESTIONS = "ready_with_blocking_questions"

    # -- repairable ---------------------------------------------------------
    PROBLEM_DEFAULT_BECAME_QUESTION = "problem_default_became_question"
    PROBLEM_DEFAULT_DROPPED = "problem_default_dropped"
    FINALIZE_QUESTION_BECAME_DEFAULT = "finalize_question_became_default"
    DUPLICATE_DEFAULT_DROPPED = "duplicate_default_dropped"
    DUPLICATE_QUESTION_DROPPED = "duplicate_question_dropped"
    DUPLICATE_REOPEN_DROPPED = "duplicate_reopen_dropped"
    UNKNOWN_REOPEN_DROPPED = "unknown_reopen_dropped"
    UNANSWERABLE_REOPEN_DROPPED = "unanswerable_reopen_dropped"
    TEXT_ENTRY_DROPPED = "text_entry_dropped"
    UNDECLARED_LADDER_DEFAULT_DROPPED = "undeclared_ladder_default_dropped"
    CANDIDATE_READY_DOWNGRADED = "candidate_ready_downgraded"
    # Only the conversation archive can grade this one.
    UNKNOWN_MESSAGE_REF_DROPPED = "unknown_message_ref_dropped"
    # Only the decision log can grade these, so they are repaired where the
    # round meets the session rather than while the payload is read.
    REPEATED_DECISION_DROPPED = "repeated_decision_dropped"
    RESOLVED_DECISION_REOPENED = "resolved_decision_reopened"
    OPEN_QUESTION_REWORDED = "open_question_reworded"
    REDUNDANT_REOPEN_DROPPED = "redundant_reopen_dropped"
    DECISION_KEY_PRESERVED = "decision_key_preserved"
    REOPEN_PROVENANCE_BACKFILLED = "reopen_provenance_backfilled"

    # -- advisory -----------------------------------------------------------
    ROUND_BUDGET_EXCEEDED = "round_budget_exceeded"
    EMPTY_PUBLIC_SUMMARY = "empty_public_summary"


INTAKE_INVARIANT_GRADES: Mapping[IntakeInvariant, IntakeInvariantGrade] = {
    # A payload that is not shaped like an intake round, or whose scientific
    # content cannot be reconstructed, is refused: papering over it would hand
    # the derivation agent a specification nobody wrote.
    IntakeInvariant.PAYLOAD_SHAPE: IntakeInvariantGrade.HARD,
    IntakeInvariant.PAYLOAD_TYPE: IntakeInvariantGrade.HARD,
    # A convention asked as a question, or a question asked as a convention,
    # moves a decision between the user and the agent. Only the user may.
    IntakeInvariant.QUESTION_NOT_PROBLEM_CLASS: IntakeInvariantGrade.HARD,
    IntakeInvariant.SPECIFICATION_SECTION_INVALID: IntakeInvariantGrade.HARD,
    IntakeInvariant.QUESTION_INVALID: IntakeInvariantGrade.HARD,
    IntakeInvariant.QUESTION_OPTION_INVALID: IntakeInvariantGrade.HARD,
    IntakeInvariant.DECLARED_DEFAULT_INVALID: IntakeInvariantGrade.HARD,
    IntakeInvariant.LADDER_RUNG_INVALID: IntakeInvariantGrade.HARD,
    IntakeInvariant.SPECIFICATION_INVALID: IntakeInvariantGrade.HARD,
    # Finalize is a user-owned stop condition: the round the user started must
    # actually be startable, and a ladder is what makes it executable.
    IntakeInvariant.FINALIZE_NOT_CANDIDATE_READY: IntakeInvariantGrade.HARD,
    IntakeInvariant.FINALIZE_STILL_ASKING: IntakeInvariantGrade.HARD,
    IntakeInvariant.FINALIZE_WITHOUT_LADDER: IntakeInvariantGrade.HARD,
    IntakeInvariant.READY_WITH_BLOCKING_QUESTIONS: IntakeInvariantGrade.HARD,
    IntakeInvariant.PROBLEM_DEFAULT_BECAME_QUESTION: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.PROBLEM_DEFAULT_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.FINALIZE_QUESTION_BECAME_DEFAULT: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.DUPLICATE_DEFAULT_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.DUPLICATE_QUESTION_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.DUPLICATE_REOPEN_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.UNKNOWN_REOPEN_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.UNANSWERABLE_REOPEN_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.TEXT_ENTRY_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.UNDECLARED_LADDER_DEFAULT_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.CANDIDATE_READY_DOWNGRADED: IntakeInvariantGrade.REPAIRABLE,
    # A reference to a Conversation Archive event the host never showed the
    # model points at nothing the user can open, so it is provenance that does
    # not exist rather than science that does. Dropping it costs the user
    # nothing; refusing the round costs them the answers they just typed.
    IntakeInvariant.UNKNOWN_MESSAGE_REF_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    # A round that collides with the decision log arrives after the user has
    # already answered the frontier, so refusing it costs exactly the answers
    # the collision is about. Each of these either drops something the user
    # could never have seen, or hands the decision back to them; none of them
    # settles a decision on their behalf.
    IntakeInvariant.REPEATED_DECISION_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.RESOLVED_DECISION_REOPENED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.OPEN_QUESTION_REWORDED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.REDUNDANT_REOPEN_DROPPED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.DECISION_KEY_PRESERVED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.REOPEN_PROVENANCE_BACKFILLED: IntakeInvariantGrade.REPAIRABLE,
    IntakeInvariant.ROUND_BUDGET_EXCEEDED: IntakeInvariantGrade.ADVISORY,
    IntakeInvariant.EMPTY_PUBLIC_SUMMARY: IntakeInvariantGrade.ADVISORY,
}


@dataclass(frozen=True)
class IntakeContractViolationDetail:
    """One hard invariant the model's payload broke."""

    invariant: IntakeInvariant
    message: str
    subject_id: str | None = None

    def payload(self) -> JsonObject:
        return {
            "code": self.invariant.value,
            "message": self.message,
            "subject_id": self.subject_id,
        }


class IntakeContractViolation(ValueError):
    """The model's payload broke an invariant the host cannot repair.

    A ``ValueError`` so that callers written against the old fatal behaviour
    keep working; the structured ``violations`` are what the self-correction
    turn sends back to the model.
    """

    def __init__(self, violations: Sequence[IntakeContractViolationDetail]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(item.message for item in self.violations))

    def as_payload(self) -> list[JsonObject]:
        return [item.payload() for item in self.violations]


@dataclass(frozen=True)
class IntakeRoundRepair:
    """One deterministic repair the host applied to a model round."""

    code: IntakeInvariant
    detail: str
    subject_id: str | None = None


def _violation(
    invariant: IntakeInvariant,
    message: str,
    *,
    subject_id: str | None = None,
) -> IntakeContractViolation:
    return IntakeContractViolation(
        (IntakeContractViolationDetail(invariant, message, subject_id),)
    )


@contextmanager
def _domain_invariant(
    invariant: IntakeInvariant,
    label: str,
    *,
    subject_id: str | None = None,
) -> Iterator[None]:
    """Report a domain dataclass rejecting model content as a contract breach.

    ``intake_session`` validates scientific content in ``__post_init__``; from
    here every such rejection is the model's payload failing the contract, not a
    host bug, so it becomes something the self-correction turn can describe.
    """

    try:
        yield
    except IntakeContractViolation:
        raise
    except ValueError as exc:
        raise _violation(invariant, f"{label}: {exc}", subject_id=subject_id) from exc


class IntakeRepairLog:
    """Collects the deterministic repairs applied to one model payload.

    Shared with ``intake_session_service``: the parse path repairs what the
    payload alone can settle, the apply path repairs what only the decision log
    can settle, and both have to land in the same round's repair trail.
    """

    def __init__(self, *, session_id: str | None = None) -> None:
        self._session_id = session_id or "unknown"
        self._entries: list[IntakeRoundRepair] = []

    def record(
        self,
        code: IntakeInvariant,
        detail: str,
        *,
        subject_id: str | None = None,
    ) -> None:
        if INTAKE_INVARIANT_GRADES[code] is not IntakeInvariantGrade.REPAIRABLE:
            raise AssertionError(f"{code.value} is not a repairable invariant")
        self._entries.append(
            IntakeRoundRepair(code=code, detail=detail, subject_id=subject_id)
        )
        logger.warning(
            "Intake round repaired %s: %s (session_id=%s, subject=%s)",
            code.value,
            detail,
            self._session_id,
            subject_id or "-",
        )

    def note(self, code: IntakeInvariant, detail: str) -> None:
        if INTAKE_INVARIANT_GRADES[code] is not IntakeInvariantGrade.ADVISORY:
            raise AssertionError(f"{code.value} is not an advisory invariant")
        logger.warning(
            "Intake round advisory %s: %s (session_id=%s)",
            code.value,
            detail,
            self._session_id,
        )

    def entries(self) -> tuple[IntakeRoundRepair, ...]:
        return tuple(self._entries)


# Shared verbatim by the advisor and the independent auditor: both must apply
# exactly the same blocking test, or the auditor can reopen what the advisor
# correctly declared as a default and the grill never converges.
INTAKE_DECISION_CLASS_RULES = """Decision classes and the only blocking test.

A decision is blocking only if its answer would (a) change the target quantity
or the required deliverable, (b) change which physics is included or excluded,
or (c) touch a constraint the user stated explicitly. Nothing else is blocking.

problem: a decision that passes the blocking test. Only problem-class decisions
may become questions, and every question is blocking.

convention: an interchangeable or translatable choice. Never a question.
Conventions, not questions: the unit system (SI or Gaussian); the gauge and the
form of the matrix element (momentum, velocity or dipole); the line shape
(Lorentzian or Gaussian); the conversion route between response function,
dielectric function and absorption coefficient; the source of the real part of
the dielectric function.

approximation_level: a rung on the refinement ladder. Never a question.
Approximation levels, not questions: parabolic two-band, Kane, or general Bloch
bands; a constant matrix element or a k-dependent one; an ideal delta function,
a constant broadening, or a state-dependent lifetime; zero temperature or
finite-temperature occupation; an isotropic scalar or a full tensor response.

Report every convention and approximation_level decision as a declared default
with an explicit statement, a rationale, and the alternatives it displaces.
Never turn one into a question and never block on one."""

INTAKE_QUESTION_RULES = f"""Question rules.

Return at most {MAX_ROUND_QUESTIONS} questions per round, each an independent
problem-class blocking decision. Every question carries why_it_matters: one
sentence saying how the answer changes the derivation. Questions may be single
choice, multiple choice, choice with supporting text, or free text; every
choice question must allow custom input. Recommend an option when justified and
give a public reason. Do not ask for facts already present in the canonical
session state, and do not reopen a resolved decision without new information and
an explicit reason. Instead of choosing, the user may answer any choice question
with the strategy simplest_first (start at the lowest ladder rung and upgrade
later) or both_routes (carry both routes as parallel branches)."""

INTAKE_LADDER_RULES = """Refinement ladder rules.

Derive the ladder backwards from the required deliverable. Rung 0 is the
textbook-simplest version compatible with every problem-class constraint the
user has already fixed, so that every problem has a comparable baseline. The
final rung equals the required_output section. Each rung says in `relaxes`
exactly what it relaxes relative to the rung below it, and lists the declared
defaults and decisions it changes. Rung numbers are contiguous and start at 0.
A rung's `default_ids` may name only defaults some round declared: earlier
rounds' declared_defaults carry forward, so re-declare a default here if this
round's ladder relies on one your declared_defaults no longer lists.
A decision the user answered with the both_routes strategy becomes a rung with
parallel_branch true."""

# The schema is rebuilt for every round, so these paragraphs describe what the
# schema already enforces rather than asking the model to remember it.
INTAKE_SESSION_DEVELOPER_INSTRUCTIONS = f"""You are DerivationLab's scientific Problem Intake advisor.
Remove only the scientific ambiguities that must be settled before an autonomous
derivation begins, and declare every remaining choice as a default the
derivation agent owns. Maintain a dependency-aware decision tree and return
every currently independent frontier question, not one question at a time.

{INTAKE_DECISION_CLASS_RULES}

{INTAKE_QUESTION_RULES}

{INTAKE_LADDER_RULES}

Modes. Each round's output schema already encodes its mode, and the round
prompt repeats which one applies. In mode advance the schema accepts questions
and admits only convention and approximation_level declared defaults, so a
problem-class decision that is still open is a question, never a default; you
mark candidate_ready only when the specification is scientifically executable
with no problem-class question left. In mode finalize the user has decided to
start: the schema fixes candidate_ready to true and questions must be empty,
and it is there that a problem-class default belongs, because every remaining
ambiguity is written as a default. A question returned by a finalize round is
rewritten into a declared default, so nothing is gained by asking one.

Budget. round_number and max_frontier_rounds are supplied. Ask what matters most
first: once round_number reaches max_frontier_rounds the user decides whether to
start, and anything still unasked has to become a declared default.

A draft specification may be incomplete. A candidate_ready one may not, and it
must carry declared_defaults and a refinement_ladder. Do not use tools, MCP,
apps, skills, web, files, or hidden repository context. Return only the JSON
object required by the output schema."""


def _question_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "question_id": {"type": "string"},
            "decision_id": {"type": "string"},
            "semantic_key": {"type": "string"},
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "why_needed": {"type": "string"},
            "why_it_matters": {"type": "string"},
            "decision_class": {
                "type": "string",
                "enum": [DecisionClass.PROBLEM.value],
            },
            "answer_mode": {
                "type": "string",
                "enum": [mode.value for mode in AnswerMode],
            },
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "option_id": {"type": "string"},
                        "label": {"type": "string"},
                        "impact": {"type": "string"},
                    },
                    "required": ["option_id", "label", "impact"],
                    "additionalProperties": False,
                },
            },
            "recommended_option_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "recommendation_reason": {"anyOf": [{"type": "null"}, {"type": "string"}]},
            "allow_custom": {"type": "boolean", "enum": [True]},
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
            },
            "blocking": {"type": "boolean", "enum": [True]},
            "grounded_in": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        },
        "required": [
            "question_id",
            "decision_id",
            "semantic_key",
            "title",
            "prompt",
            "why_needed",
            "why_it_matters",
            "decision_class",
            "answer_mode",
            "options",
            "recommended_option_ids",
            "recommendation_reason",
            "allow_custom",
            "depends_on",
            "blocking",
            "grounded_in",
        ],
        "additionalProperties": False,
    }


def _declared_default_schema(
    *, decision_classes: Sequence[DecisionClass] = tuple(DecisionClass)
) -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "default_id": {"type": "string"},
            "decision_class": {
                "type": "string",
                "enum": [item.value for item in decision_classes],
            },
            "title": {"type": "string"},
            "statement": {"type": "string"},
            "rationale": {"type": "string"},
            "alternatives": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "default_id",
            "decision_class",
            "title",
            "statement",
            "rationale",
            "alternatives",
        ],
        "additionalProperties": False,
    }


def _ladder_rung_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "rung": {"type": "integer"},
            "name": {"type": "string"},
            "relaxes": {"type": "string"},
            "default_ids": {"type": "array", "items": {"type": "string"}},
            "decision_ids": {"type": "array", "items": {"type": "string"}},
            "parallel_branch": {"type": "boolean"},
        },
        "required": [
            "rung",
            "name",
            "relaxes",
            "default_ids",
            "decision_ids",
            "parallel_branch",
        ],
        "additionalProperties": False,
    }


#: The only decision classes an advance round may settle without asking. A
#: problem-class decision is the user's to make while the interview is open.
ADVANCE_DEFAULT_DECISION_CLASSES = (
    DecisionClass.CONVENTION,
    DecisionClass.APPROXIMATION_LEVEL,
)


def intake_session_output_schema(
    *,
    mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
    known_decision_ids: Sequence[str] = (),
) -> JsonObject:
    """Build this round's output schema from its mode and session state.

    Anything the format can state is stated here rather than in the prompt: a
    constraint the model can only read is a constraint it can violate, and a
    violation costs a whole round of the user's time.
    """

    finalize = mode is IntakeRoundMode.FINALIZE
    decision_ids = tuple(dict.fromkeys(known_decision_ids))
    # An empty enum matches nothing and is not a legal schema, so a session
    # without decisions keeps a plain string and the parse path drops whatever
    # the model names.
    reopen_decision_id: JsonObject = (
        {"type": "string", "enum": list(decision_ids)}
        if decision_ids
        else {"type": "string"}
    )
    return {
        "type": "object",
        "properties": {
            "public_summary": {"type": "string"},
            "specification_sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": list(REQUIRED_SPEC_SECTIONS),
                        },
                        "content": {
                            "anyOf": [
                                {"type": "null"},
                                {"type": "string"},
                            ]
                        },
                        "not_applicable_reason": {
                            "anyOf": [
                                {"type": "null"},
                                {"type": "string"},
                            ]
                        },
                    },
                    "required": ["name", "content", "not_applicable_reason"],
                    "additionalProperties": False,
                },
                "maxItems": len(REQUIRED_SPEC_SECTIONS),
            },
            "critical_message_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "questions": {
                "type": "array",
                "items": _question_schema(),
                # "no questions at all" is deliberately not expressed here. A
                # degenerate ``maxItems: 0`` is the one array bound no provider
                # is known to have accepted from us, and a provider that refuses
                # it would refuse the finalize round itself — the last step
                # before the derivation starts, and the most expensive one to
                # lose. The finalize repair converts whatever questions do
                # arrive into declared defaults, deterministically, so the
                # constraint costs nothing by being enforced after the fact.
                "maxItems": MAX_ROUND_QUESTIONS,
            },
            "declared_defaults": {
                "type": "array",
                "items": _declared_default_schema(
                    decision_classes=(
                        tuple(DecisionClass)
                        if finalize
                        else ADVANCE_DEFAULT_DECISION_CLASSES
                    )
                ),
                "maxItems": MAX_ROUND_DECLARED_DEFAULTS,
            },
            "refinement_ladder": {
                "type": "array",
                "items": _ladder_rung_schema(),
                "maxItems": 12,
            },
            "reopen_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision_id": reopen_decision_id,
                        "reason": {"type": "string"},
                        "source_message_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                    },
                    "required": ["decision_id", "reason", "source_message_refs"],
                    "additionalProperties": False,
                },
            },
            "candidate_ready": (
                {"type": "boolean", "enum": [True]} if finalize else {"type": "boolean"}
            ),
        },
        "required": [
            "public_summary",
            "specification_sections",
            "critical_message_refs",
            "questions",
            "declared_defaults",
            "refinement_ladder",
            "reopen_requests",
            "candidate_ready",
        ],
        "additionalProperties": False,
    }


#: The advance-round shape, kept as a constant for callers that only need to
#: inspect the contract rather than run a round.
INTAKE_SESSION_OUTPUT_SCHEMA: JsonObject = intake_session_output_schema(
    mode=IntakeRoundMode.ADVANCE
)

SPEC_AUDIT_DEVELOPER_INSTRUCTIONS = f"""You are DerivationLab's independent scientific specification auditor.
Review only the supplied initial problem statement, Problem Specification,
decision log summary, and the declared defaults the specification already
carries.

{INTAKE_DECISION_CLASS_RULES}

You may block only on a problem-class decision that is grounded in the initial
problem statement or in the specification text: quote that phrase in
grounded_in. Do not invent a decision that neither implies, do not reopen a
decision recorded in the decision log, and do not reopen a declared default.
Return at most {MAX_AUDIT_BLOCKING_QUESTIONS} blocking questions, each with
why_it_matters. Every convention or approximation-level gap you find is returned
as a declared default, never as a question; anything you would merely like
tightened is a residual risk.

In mode finalize the user has already decided to start, so you may not fail:
return passed true and record what still worries you as declared defaults and
residual risks.

Do not use tools, files, web, MCP, apps, or prior thread state. Return only the
JSON object required by the output schema."""

SPEC_AUDIT_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "public_summary": {"type": "string"},
        "blocking_questions": {
            "type": "array",
            "items": _question_schema(),
            "maxItems": MAX_AUDIT_BLOCKING_QUESTIONS,
        },
        "declared_defaults": {
            "type": "array",
            "items": _declared_default_schema(),
            "maxItems": MAX_ROUND_DECLARED_DEFAULTS,
        },
        "residual_risks": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_ROUND_DECLARED_DEFAULTS,
        },
    },
    "required": [
        "passed",
        "public_summary",
        "blocking_questions",
        "declared_defaults",
        "residual_risks",
    ],
    "additionalProperties": False,
}

INTAKE_REHYDRATE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "canonical_state_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        }
    },
    "required": ["canonical_state_sha256"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DecisionReopenRequest:
    decision_id: str
    reason: str
    source_message_refs: tuple[str, ...]


@dataclass(frozen=True)
class IntakeRoundProposal:
    public_summary: str
    problem_specification: ProblemSpecification
    questions: tuple[IntakeQuestion, ...]
    reopen_requests: tuple[DecisionReopenRequest, ...]
    candidate_ready: bool
    thread_id: str
    turn_id: str
    created_thread: bool
    repairs: tuple[IntakeRoundRepair, ...] = ()


@dataclass(frozen=True)
class SpecificationAudit:
    passed: bool
    public_summary: str
    blocking_questions: tuple[IntakeQuestion, ...]
    thread_id: str
    turn_id: str
    declared_defaults: tuple[DeclaredDefault, ...] = ()
    residual_risks: tuple[str, ...] = ()
    #: The audit's own deterministic repairs, carried out so the caller can put
    #: them on the same round's repair trail as the advisor's.
    repairs: tuple[IntakeRoundRepair, ...] = ()


@dataclass(frozen=True)
class ThreadReplacementReceipt:
    thread_id: str
    turn_id: str
    canonical_state_sha256: str


class PersistentIntakeRoundRunner(Protocol):
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
    ) -> StructuredTurnResult: ...


class EphemeralSpecificationAuditRunner(Protocol):
    async def run_ephemeral(
        self,
        *,
        developer_instructions: str,
        prompt: str,
        output_schema: Mapping[str, Any],
    ) -> StructuredTurnResult: ...


def _exact(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _violation(
            IntakeInvariant.PAYLOAD_SHAPE,
            f"{label} must contain exactly {sorted(fields)}",
        )
    return value


def _texts(value: object, label: str, *, repairs: IntakeRepairLog) -> tuple[str, ...]:
    """Read a list of non-empty strings, repairing its bookkeeping.

    These lists hold references, labels and ids, so a blank or repeated entry
    says nothing the rest of the list does not already say: it is dropped and
    recorded rather than allowed to cost the round. Anything that is not a
    string is a different matter and is refused.
    """

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE,
            f"{label} must be an array of non-empty text",
        )
    kept: list[str] = []
    for item in (entry.strip() for entry in value):
        if not item:
            repairs.record(
                IntakeInvariant.TEXT_ENTRY_DROPPED,
                f"{label} contained a blank entry",
                subject_id=label,
            )
            continue
        if item in kept:
            repairs.record(
                IntakeInvariant.TEXT_ENTRY_DROPPED,
                f"{label} repeated an entry; kept the first",
                subject_id=label,
            )
            continue
        kept.append(item)
    return tuple(kept)


def _parse_declared_defaults(
    value: object,
    *,
    label: str,
    repairs: IntakeRepairLog,
) -> tuple[DeclaredDefault, ...]:
    """Read declared defaults, keeping the last wording of a repeated id.

    Repetition follows :func:`_merge_declared_defaults`: a later statement of
    the same default is the model revising itself inside one payload, so the
    last one is what the user would have been shown.
    """

    if not isinstance(value, list):
        raise _violation(IntakeInvariant.PAYLOAD_TYPE, f"{label} must be an array")
    merged: dict[str, DeclaredDefault] = {}
    order: list[str] = []
    for entry in value:
        item = _exact(
            entry,
            {
                "default_id",
                "decision_class",
                "title",
                "statement",
                "rationale",
                "alternatives",
            },
            "declared default",
        )
        with _domain_invariant(
            IntakeInvariant.DECLARED_DEFAULT_INVALID,
            label,
            subject_id=(
                item["default_id"] if isinstance(item["default_id"], str) else None
            ),
        ):
            default = DeclaredDefault(
                default_id=item["default_id"],
                decision_class=DecisionClass(item["decision_class"]),
                title=item["title"],
                statement=item["statement"],
                rationale=item["rationale"],
                alternatives=_texts(
                    item["alternatives"],
                    "declared default alternatives",
                    repairs=repairs,
                ),
            )
        if default.default_id in merged:
            repairs.record(
                IntakeInvariant.DUPLICATE_DEFAULT_DROPPED,
                f"{label} declared {default.default_id} more than once; "
                "kept the last wording",
                subject_id=default.default_id,
            )
        else:
            order.append(default.default_id)
        merged[default.default_id] = default
    return tuple(merged[default_id] for default_id in order)


def _merge_declared_defaults(
    existing: Sequence[DeclaredDefault],
    incoming: Sequence[DeclaredDefault],
) -> tuple[DeclaredDefault, ...]:
    """Keep prior defaults when a later model turn returns only its delta."""

    merged = {item.default_id: item for item in existing}
    order = [item.default_id for item in existing]
    for item in incoming:
        if item.default_id not in merged:
            order.append(item.default_id)
        merged[item.default_id] = item
    return tuple(merged[default_id] for default_id in order)


def _parse_refinement_ladder(
    value: object, *, repairs: IntakeRepairLog
) -> tuple[LadderRung, ...]:
    if not isinstance(value, list):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE, "refinement_ladder must be an array"
        )
    rungs: list[LadderRung] = []
    for entry in value:
        item = _exact(
            entry,
            {
                "rung",
                "name",
                "relaxes",
                "default_ids",
                "decision_ids",
                "parallel_branch",
            },
            "refinement ladder rung",
        )
        with _domain_invariant(
            IntakeInvariant.LADDER_RUNG_INVALID, "refinement ladder rung"
        ):
            rungs.append(
                LadderRung(
                    rung=item["rung"],
                    name=item["name"],
                    relaxes=item["relaxes"],
                    default_ids=_texts(
                        item["default_ids"],
                        "ladder rung default IDs",
                        repairs=repairs,
                    ),
                    decision_ids=_texts(
                        item["decision_ids"],
                        "ladder rung decision IDs",
                        repairs=repairs,
                    ),
                    parallel_branch=item["parallel_branch"],
                )
            )
    return tuple(rungs)


def _prune_undeclared_ladder_defaults(
    ladder: tuple[LadderRung, ...],
    declared: Sequence[DeclaredDefault],
    *,
    repairs: IntakeRepairLog,
) -> tuple[LadderRung, ...]:
    """Drop ladder references to defaults no round ever declared.

    ``declared_defaults`` is capped per round, so a long session pushes an older
    default out of the model's list while a rung still names it. The rung's own
    prose survives; only the dangling id goes, because a default the user was
    never shown cannot be rendered next to the rung anyway. Raising here instead
    would throw away the user's answered round *after* the model call, which is
    what the specification invariant used to do.
    """

    known = {item.default_id for item in declared}
    pruned: list[LadderRung] = []
    for rung in ladder:
        kept = tuple(item for item in rung.default_ids if item in known)
        if len(kept) == len(rung.default_ids):
            pruned.append(rung)
            continue
        for dropped in rung.default_ids:
            if dropped not in known:
                repairs.record(
                    IntakeInvariant.UNDECLARED_LADDER_DEFAULT_DROPPED,
                    f"refinement ladder rung {rung.rung} cited undeclared default "
                    f"{dropped}",
                    subject_id=dropped,
                )
        pruned.append(replace(rung, default_ids=kept))
    return tuple(pruned)


def _parse_question(
    value: object, index: int, *, repairs: IntakeRepairLog
) -> IntakeQuestion:
    raw = _exact(
        value,
        {
            "question_id",
            "decision_id",
            "semantic_key",
            "title",
            "prompt",
            "why_needed",
            "why_it_matters",
            "decision_class",
            "answer_mode",
            "options",
            "recommended_option_ids",
            "recommendation_reason",
            "allow_custom",
            "depends_on",
            "blocking",
            "grounded_in",
        },
        f"question {index}",
    )
    options_raw = raw["options"]
    if not isinstance(options_raw, list):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE, f"question {index} options must be an array"
        )
    with _domain_invariant(
        IntakeInvariant.QUESTION_OPTION_INVALID, f"question {index} option"
    ):
        options = tuple(
            QuestionOption(
                **_exact(item, {"option_id", "label", "impact"}, "question option")
            )
            for item in options_raw
        )
    if not isinstance(raw["allow_custom"], bool) or not isinstance(
        raw["blocking"], bool
    ):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE,
            f"question {index} boolean fields are invalid",
        )
    with _domain_invariant(
        IntakeInvariant.QUESTION_NOT_PROBLEM_CLASS, f"question {index}"
    ):
        decision_class = DecisionClass(raw["decision_class"])
    if decision_class is not DecisionClass.PROBLEM:
        # Moving a decision between the user and the agent is the one thing the
        # host must never do quietly, in either direction.
        raise _violation(
            IntakeInvariant.QUESTION_NOT_PROBLEM_CLASS,
            f"question {index} is a {decision_class.value} decision; "
            "conventions and approximation levels must be declared defaults",
        )
    with _domain_invariant(IntakeInvariant.QUESTION_INVALID, f"question {index}"):
        return IntakeQuestion(
            question_id=raw["question_id"],
            decision_id=raw["decision_id"],
            semantic_key=raw["semantic_key"],
            title=raw["title"],
            prompt=raw["prompt"],
            why_needed=raw["why_needed"],
            why_it_matters=raw["why_it_matters"],
            answer_mode=AnswerMode(raw["answer_mode"]),
            decision_class=decision_class,
            options=options,
            recommended_option_ids=_texts(
                raw["recommended_option_ids"],
                "recommended option IDs",
                repairs=repairs,
            ),
            recommendation_reason=raw["recommendation_reason"],
            allow_custom=raw["allow_custom"],
            depends_on=_texts(
                raw["depends_on"], "question dependencies", repairs=repairs
            ),
            blocking=raw["blocking"],
            grounded_in=raw["grounded_in"],
        )


def _derived_identifier(prefix: str, source: str) -> str:
    """Derive one id from another, identically on every retry.

    Retrying a round must not renumber the frontier, so the derivation uses only
    the source id: no counter, no clock, no randomness.
    """

    candidate = f"{prefix}{source}"
    if len(candidate) <= MAX_IDENTIFIER_LENGTH:
        return candidate
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    keep = max(MAX_IDENTIFIER_LENGTH - len(prefix) - len(digest) - 1, 1)
    return f"{prefix}{source[:keep]}.{digest}"


# Every sentence below is written by the host, not by the advisor. The advisor's
# own words are always quoted and attributed, because a generated question mixes
# the two in fields the web UI renders as one paragraph: a reader who cannot tell
# them apart reads the host's framing as the advisor's scientific argument.
#
# Known limitation: the host has no locale here. The advisor writes in whatever
# language the user used, so these sentences sit in English beside Chinese model
# text. They are therefore kept short, factual, and few.
_PROBLEM_DEFAULT_WHY_NEEDED = (
    "This is a problem-class decision, which is yours to settle; the advisor "
    "proposed to apply a default here instead of asking, so it is being put "
    "back to you."
)
_PROBLEM_DEFAULT_RECOMMENDED_IMPACT = (
    "The advisor's proposed default — the option it would have applied without "
    "asking you."
)
_PROBLEM_DEFAULT_ALTERNATIVE_IMPACT = (
    "An alternative the advisor listed as displaced by that proposal."
)


def _question_from_problem_default(default: DeclaredDefault) -> IntakeQuestion:
    """Hand a problem-class decision back to the user as an open question.

    An advance round may not settle a problem-class decision on its own, but the
    content is still good science: the model's statement becomes the recommended
    option and its alternatives become the others, so nothing it worked out is
    lost and the choice stays the user's.

    The default's ``rationale`` is the advisor's argument for *not* asking, so it
    is carried as the recommendation's reason, attributed and quoted. It is
    never reused as the answer to "why are you being asked this", which is a
    question only the host can answer.
    """

    options = [
        QuestionOption(
            option_id="option_recommended",
            label=default.statement,
            impact=_PROBLEM_DEFAULT_RECOMMENDED_IMPACT,
        )
    ]
    options.extend(
        QuestionOption(
            option_id=f"option_alternative_{position}",
            label=alternative,
            impact=_PROBLEM_DEFAULT_ALTERNATIVE_IMPACT,
        )
        for position, alternative in enumerate(default.alternatives, start=1)
    )
    return IntakeQuestion(
        question_id=_derived_identifier("question_default_", default.default_id),
        decision_id=_derived_identifier("decision_default_", default.default_id),
        semantic_key=f"declared_default:{default.default_id}",
        title=default.title,
        prompt=(
            "This choice changes the problem itself, so it stays yours. "
            f"The advisor proposed: “{default.statement}”"
        ),
        why_needed=_PROBLEM_DEFAULT_WHY_NEEDED,
        why_it_matters=(
            "The derivation runs on whichever option you pick for "
            f"“{default.title}”, and on the advisor's proposal only if "
            "you choose it."
        ),
        answer_mode=AnswerMode.SINGLE_CHOICE,
        decision_class=DecisionClass.PROBLEM,
        options=tuple(options),
        recommended_option_ids=("option_recommended",),
        recommendation_reason=(
            "The advisor's reason for proposing to settle this without asking, "
            f"in its words: “{default.rationale}”"
        ),
        allow_custom=True,
        blocking=True,
    )


def _default_from_finalize_question(question: IntakeQuestion) -> DeclaredDefault:
    """Write a still-open question as the default the finalize contract wants.

    Finalize means the user chose to start with this decision unresolved, so the
    recommended answer becomes the statement and the rest of the question is
    preserved next to it: the user can still read what was asked and what was
    displaced.
    """

    chosen: QuestionOption | None = None
    if question.recommended_option_ids:
        chosen = next(
            (
                option
                for option in question.options
                if option.option_id == question.recommended_option_ids[0]
            ),
            None,
        )
    # Nothing was recommended, so the host takes the first option the round
    # listed. That is a host choice by position, not the advisor's judgement,
    # and the rationale has to say so or a reader will credit the advisor with
    # an argument it never made.
    by_position = chosen is None and bool(question.options)
    if by_position:
        chosen = question.options[0]
    statement = (
        chosen.label
        if chosen is not None
        else (
            "The derivation agent settles this at its own discretion and records "
            "the choice it made."
        )
    )
    alternatives = tuple(
        dict.fromkeys(
            option.label
            for option in question.options
            if chosen is None or option.option_id != chosen.option_id
        )
    )
    return DeclaredDefault(
        default_id=_derived_identifier("default_question_", question.decision_id),
        decision_class=DecisionClass.PROBLEM,
        title=question.title,
        statement=statement,
        rationale=(
            "The user started the derivation with this decision open. "
            f"The round asked: {question.prompt}"
            + (
                " The round recommended no option, so this statement is the "
                "first one it listed, taken by position rather than on the "
                "advisor's recommendation."
                if by_position
                else ""
            )
        ),
        alternatives=alternatives,
    )


def deduplicate_intake_questions(
    questions: Sequence[IntakeQuestion], *, repairs: IntakeRepairLog
) -> tuple[IntakeQuestion, ...]:
    """Keep the first question per decision and per semantic key.

    Shared with ``intake_session_service``: the advisor's questions are deduped
    while the round is read, and the auditor's are merged in afterwards, so the
    same rule has to run once more before the round reaches the decision log.
    """

    seen_decisions: set[str] = set()
    seen_keys: set[str] = set()
    kept: list[IntakeQuestion] = []
    for question in questions:
        if question.decision_id in seen_decisions:
            repairs.record(
                IntakeInvariant.DUPLICATE_QUESTION_DROPPED,
                f"the frontier asked decision {question.decision_id} twice; "
                "kept the first",
                subject_id=question.decision_id,
            )
            continue
        if question.semantic_key in seen_keys:
            repairs.record(
                IntakeInvariant.DUPLICATE_QUESTION_DROPPED,
                f"the frontier asked semantic key {question.semantic_key} twice; "
                "kept the first",
                subject_id=question.decision_id,
            )
            continue
        seen_decisions.add(question.decision_id)
        seen_keys.add(question.semantic_key)
        kept.append(question)
    return tuple(kept)


def _questions_from_problem_defaults(
    defaults: Sequence[DeclaredDefault],
    questions: Sequence[IntakeQuestion],
    *,
    repairs: IntakeRepairLog,
) -> tuple[tuple[DeclaredDefault, ...], tuple[IntakeQuestion, ...]]:
    """Split an advance round's problem-class defaults back onto the frontier."""

    taken_decisions = {item.decision_id for item in questions}
    taken_keys = {item.semantic_key for item in questions}
    kept: list[DeclaredDefault] = []
    generated: list[IntakeQuestion] = []
    for default in defaults:
        if default.decision_class is not DecisionClass.PROBLEM:
            kept.append(default)
            continue
        try:
            question = _question_from_problem_default(default)
        except ValueError as exc:
            repairs.record(
                IntakeInvariant.PROBLEM_DEFAULT_DROPPED,
                f"problem-class default {default.default_id} could not be asked "
                f"as a question ({exc})",
                subject_id=default.default_id,
            )
            continue
        if question.decision_id in taken_decisions or question.semantic_key in (
            taken_keys
        ):
            # The model already asked this; its own question is the better one.
            repairs.record(
                IntakeInvariant.PROBLEM_DEFAULT_DROPPED,
                f"problem-class default {default.default_id} duplicates a "
                "question this round already asked",
                subject_id=default.default_id,
            )
            continue
        taken_decisions.add(question.decision_id)
        taken_keys.add(question.semantic_key)
        generated.append(question)
        repairs.record(
            IntakeInvariant.PROBLEM_DEFAULT_BECAME_QUESTION,
            f"an advance round declared problem-class default "
            f"{default.default_id}; it is now open question "
            f"{question.decision_id}",
            subject_id=default.default_id,
        )
    return tuple(kept), tuple(generated)


def _defaults_from_finalize_questions(
    defaults: Sequence[DeclaredDefault],
    questions: Sequence[IntakeQuestion],
    *,
    repairs: IntakeRepairLog,
) -> tuple[DeclaredDefault, ...]:
    """Fold a finalize round's remaining questions into its declared defaults."""

    converted: list[DeclaredDefault] = []
    taken = {item.default_id for item in defaults}
    for question in questions:
        try:
            default = _default_from_finalize_question(question)
        except ValueError as exc:
            raise _violation(
                IntakeInvariant.FINALIZE_STILL_ASKING,
                f"a finalize round asked {question.decision_id}, which cannot be "
                f"written as a declared default ({exc})",
                subject_id=question.decision_id,
            ) from exc
        if default.default_id in taken:
            repairs.record(
                IntakeInvariant.FINALIZE_QUESTION_BECAME_DEFAULT,
                f"a finalize round asked {question.decision_id}, which this "
                f"round already declared as default {default.default_id}",
                subject_id=question.decision_id,
            )
            continue
        taken.add(default.default_id)
        converted.append(default)
        repairs.record(
            IntakeInvariant.FINALIZE_QUESTION_BECAME_DEFAULT,
            f"a finalize round asked {question.decision_id}; it is now declared "
            f"default {default.default_id}",
            subject_id=question.decision_id,
        )
    return (*defaults, *converted)


def _parse_reopen_requests(
    value: object,
    *,
    questions: Sequence[IntakeQuestion],
    known_decision_ids: Sequence[str] | None,
    repairs: IntakeRepairLog,
) -> tuple[DecisionReopenRequest, ...]:
    if not isinstance(value, list):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE, "reopen_requests must be an array"
        )
    replacements = {item.decision_id for item in questions}
    known = None if known_decision_ids is None else set(known_decision_ids)
    kept: list[DecisionReopenRequest] = []
    seen: set[str] = set()
    for entry in value:
        item = _exact(
            entry,
            {"decision_id", "reason", "source_message_refs"},
            "decision reopen request",
        )
        request = DecisionReopenRequest(
            decision_id=item["decision_id"],
            reason=item["reason"],
            source_message_refs=_texts(
                item["source_message_refs"],
                "reopen source message refs",
                repairs=repairs,
            ),
        )
        if request.decision_id in seen:
            repairs.record(
                IntakeInvariant.DUPLICATE_REOPEN_DROPPED,
                f"decision {request.decision_id} was reopened twice; kept the first",
                subject_id=request.decision_id,
            )
            continue
        if known is not None and request.decision_id not in known:
            # Reopening is an edit to the decision log; a decision that is not
            # in it has nothing to edit, and the user never saw it.
            repairs.record(
                IntakeInvariant.UNKNOWN_REOPEN_DROPPED,
                f"decision {request.decision_id} is not in this session's decision log",
                subject_id=request.decision_id,
            )
            continue
        if request.decision_id not in replacements:
            # A reopened decision is re-asked; without a replacement question
            # the user would be left with a decision nobody can answer.
            repairs.record(
                IntakeInvariant.UNANSWERABLE_REOPEN_DROPPED,
                f"decision {request.decision_id} was reopened without a "
                "replacement question",
                subject_id=request.decision_id,
            )
            continue
        seen.add(request.decision_id)
        kept.append(request)
    return tuple(kept)


def parse_intake_round_proposal(
    payload: Mapping[str, Any],
    *,
    specification_version: int,
    supersedes_version: int | None,
    result: StructuredTurnResult,
    mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
    inherited_declared_defaults: Sequence[DeclaredDefault] = (),
    session_id: str | None = None,
    known_decision_ids: Sequence[str] | None = None,
) -> IntakeRoundProposal:
    """Read one model round, repairing its bookkeeping and refusing the rest.

    ``known_decision_ids`` is the session's decision log; ``None`` means the
    caller does not know it, and reopen requests are then left alone.
    """

    repairs = IntakeRepairLog(session_id=session_id)
    raw = _exact(
        payload,
        {
            "public_summary",
            "specification_sections",
            "critical_message_refs",
            "questions",
            "declared_defaults",
            "refinement_ladder",
            "reopen_requests",
            "candidate_ready",
        },
        "Intake round proposal",
    )
    if not isinstance(raw["candidate_ready"], bool):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE, "candidate_ready must be boolean"
        )
    if not isinstance(raw["public_summary"], str):
        raise _violation(IntakeInvariant.PAYLOAD_TYPE, "public_summary must be text")
    if not raw["public_summary"].strip():
        repairs.note(
            IntakeInvariant.EMPTY_PUBLIC_SUMMARY,
            "the round returned no public summary",
        )
    candidate_ready = raw["candidate_ready"]
    section_values = raw["specification_sections"]
    if not isinstance(section_values, list):
        raise _violation(
            IntakeInvariant.PAYLOAD_TYPE, "specification_sections must be an array"
        )
    with _domain_invariant(
        IntakeInvariant.SPECIFICATION_SECTION_INVALID, "problem specification section"
    ):
        sections = tuple(
            ProblemSection(
                **_exact(
                    item,
                    {"name", "content", "not_applicable_reason"},
                    "problem specification section",
                )
            )
            for item in section_values
        )
    question_values = raw["questions"]
    if not isinstance(question_values, list):
        raise _violation(IntakeInvariant.PAYLOAD_TYPE, "questions must be an array")
    if len(question_values) > MAX_ROUND_QUESTIONS:
        repairs.note(
            IntakeInvariant.ROUND_BUDGET_EXCEEDED,
            f"the round returned {len(question_values)} questions, over the "
            f"budget of {MAX_ROUND_QUESTIONS}",
        )
    questions = deduplicate_intake_questions(
        tuple(
            _parse_question(item, index, repairs=repairs)
            for index, item in enumerate(question_values)
        ),
        repairs=repairs,
    )
    declared = _parse_declared_defaults(
        raw["declared_defaults"], label="declared_defaults", repairs=repairs
    )
    if len(declared) > MAX_ROUND_DECLARED_DEFAULTS:
        repairs.note(
            IntakeInvariant.ROUND_BUDGET_EXCEEDED,
            f"the round declared {len(declared)} defaults, over the budget of "
            f"{MAX_ROUND_DECLARED_DEFAULTS}",
        )
    if mode is IntakeRoundMode.ADVANCE:
        declared, handed_back = _questions_from_problem_defaults(
            declared, questions, repairs=repairs
        )
        if handed_back:
            questions = (*questions, *handed_back)
            # The round settled something it had no right to settle, so it is
            # not a candidate: the user still has to answer.
            candidate_ready = False
    else:
        declared = _defaults_from_finalize_questions(
            declared, questions, repairs=repairs
        )
        questions = ()
    reopen_requests = _parse_reopen_requests(
        raw["reopen_requests"],
        questions=questions,
        known_decision_ids=known_decision_ids,
        repairs=repairs,
    )
    declared_defaults = _merge_declared_defaults(inherited_declared_defaults, declared)
    refinement_ladder = _prune_undeclared_ladder_defaults(
        _parse_refinement_ladder(raw["refinement_ladder"], repairs=repairs),
        declared_defaults,
        repairs=repairs,
    )
    if candidate_ready and questions and mode is IntakeRoundMode.ADVANCE:
        repairs.record(
            IntakeInvariant.CANDIDATE_READY_DOWNGRADED,
            "the round called itself candidate-ready while still asking "
            f"{len(questions)} blocking question(s)",
        )
        candidate_ready = False
    if candidate_ready and not refinement_ladder and mode is IntakeRoundMode.ADVANCE:
        repairs.record(
            IntakeInvariant.CANDIDATE_READY_DOWNGRADED,
            "the round called itself candidate-ready without a refinement ladder",
        )
        candidate_ready = False
    violations: list[IntakeContractViolationDetail] = []
    if mode is IntakeRoundMode.FINALIZE:
        if not candidate_ready:
            violations.append(
                IntakeContractViolationDetail(
                    IntakeInvariant.FINALIZE_NOT_CANDIDATE_READY,
                    "a finalize round must return a candidate-ready specification",
                )
            )
        if questions:
            violations.append(
                IntakeContractViolationDetail(
                    IntakeInvariant.FINALIZE_STILL_ASKING,
                    "a finalize round cannot ask the user another question; "
                    "remaining ambiguities are declared defaults",
                )
            )
        if candidate_ready and not refinement_ladder:
            violations.append(
                IntakeContractViolationDetail(
                    IntakeInvariant.FINALIZE_WITHOUT_LADDER,
                    "a candidate-ready specification requires a refinement ladder "
                    "whose rung 0 is the textbook-simplest baseline",
                )
            )
    if candidate_ready and any(item.blocking for item in questions):
        violations.append(
            IntakeContractViolationDetail(
                IntakeInvariant.READY_WITH_BLOCKING_QUESTIONS,
                "candidate-ready proposal cannot contain blocking questions",
            )
        )
    if violations:
        raise IntakeContractViolation(violations)
    with _domain_invariant(
        IntakeInvariant.SPECIFICATION_INVALID, "problem specification"
    ):
        specification = ProblemSpecification(
            version=specification_version,
            supersedes_version=supersedes_version,
            status=(
                ProblemSpecificationStatus.CANDIDATE_READY
                if candidate_ready
                else ProblemSpecificationStatus.DRAFT
            ),
            sections=sections,
            critical_message_refs=_texts(
                raw["critical_message_refs"], "critical message refs", repairs=repairs
            ),
            declared_defaults=declared_defaults,
            refinement_ladder=refinement_ladder,
        )
    return IntakeRoundProposal(
        public_summary=raw["public_summary"].strip(),
        problem_specification=specification,
        questions=questions,
        reopen_requests=reopen_requests,
        candidate_ready=candidate_ready,
        thread_id=result.thread_id,
        turn_id=result.turn_id,
        created_thread=result.created_thread,
        repairs=repairs.entries(),
    )


def _prompt(
    session: IntakeSession,
    *,
    user_submission: Mapping[str, Any],
    visible_event_ids: Sequence[str],
    mode: IntakeRoundMode,
    round_number: int,
    max_frontier_rounds: int,
    audit_rejections: int,
) -> str:
    payload = {
        "prompt_version": INTAKE_SESSION_PROMPT_VERSION,
        "schema_version": INTAKE_SESSION_SCHEMA_VERSION,
        "mode": mode.value,
        "round_number": round_number,
        "max_frontier_rounds": max_frontier_rounds,
        "audit_rejections": audit_rejections,
        "canonical_session": intake_session_payload(session),
        "visible_event_ids": list(visible_event_ids),
        "latest_user_submission": dict(user_submission),
    }
    # Developer instructions only reach a thread when it is created, so the
    # mode's own rules travel with the turn that is actually in that mode.
    instruction = (
        "Advance this IntakeSession by exactly one frontier round. The canonical "
        "state is authoritative. This round's schema accepts questions and "
        "admits only convention and approximation_level declared defaults: a "
        "problem-class decision that is still open is a question, not a default."
        if mode is IntakeRoundMode.ADVANCE
        else (
            "The user has decided to start. Finalize this IntakeSession: this "
            "round's schema fixes candidate_ready to true and this round may "
            "ask no questions, so write every remaining ambiguity as a declared "
            "default, including the problem-class ones. Any question you do "
            "return is rewritten into a declared default."
        )
    )
    return (
        instruction + "\n\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _correction_prompt(
    violation: IntakeContractViolation, *, mode: IntakeRoundMode
) -> str:
    return (
        "Your previous response was rejected: it broke this round's output "
        "contract. Return one corrected payload for the same round under the "
        "same schema, repeating every field rather than only the corrected "
        "ones. Do not ask about this rejection and do not explain it.\n\n"
        + json.dumps(
            {
                "mode": mode.value,
                "contract_violations": violation.as_payload(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


class AppServerIntakeSessionAdvisor:
    def __init__(self, runner: PersistentIntakeRoundRunner) -> None:
        self._runner = runner
        #: Corrective turns spent over this advisor's lifetime, for logging and
        #: for tests that need to see the retry happen exactly once.
        self.correction_attempts = 0

    async def advance(
        self,
        session: IntakeSession,
        *,
        user_submission: Mapping[str, Any],
        visible_event_ids: Sequence[str],
        resume_thread_id: str | None,
        mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
        round_number: int = 0,
        max_frontier_rounds: int = DEFAULT_MAX_FRONTIER_ROUNDS,
        audit_rejections: int = 0,
    ) -> IntakeRoundProposal:
        latest_version = (
            session.problem_specifications[-1].version
            if session.problem_specifications
            else 0
        )
        known_decision_ids = tuple(
            dict.fromkeys(item.decision_id for item in session.decisions)
        )
        output_schema = intake_session_output_schema(
            mode=mode, known_decision_ids=known_decision_ids
        )
        inherited = (
            session.problem_specifications[-1].declared_defaults
            if session.problem_specifications
            else ()
        )

        def parse(turn: StructuredTurnResult) -> IntakeRoundProposal:
            return parse_intake_round_proposal(
                turn.payload,
                specification_version=latest_version + 1,
                supersedes_version=latest_version or None,
                result=turn,
                mode=mode,
                inherited_declared_defaults=inherited,
                session_id=session.session_id,
                known_decision_ids=known_decision_ids,
            )

        result = await self._runner.run_round(
            intake_id=session.session_id,
            developer_instructions=INTAKE_SESSION_DEVELOPER_INSTRUCTIONS,
            prompt=_prompt(
                session,
                user_submission=user_submission,
                visible_event_ids=visible_event_ids,
                mode=mode,
                round_number=round_number,
                max_frontier_rounds=max_frontier_rounds,
                audit_rejections=audit_rejections,
            ),
            output_schema=output_schema,
            resume_thread_id=resume_thread_id,
            model=session.model,
            effort=session.effort,
            service_tier=session.service_tier,
        )
        try:
            return parse(result)
        except IntakeContractViolation as violation:
            # One correction, on the same thread, and only for a payload that
            # broke the contract: a host bug raises something else and must not
            # be answered by asking the model again.
            self.correction_attempts += 1
            logger.warning(
                "Intake round broke its output contract; asking for one "
                "correction (session_id=%s, attempt=%s, codes=%s)",
                session.session_id,
                self.correction_attempts,
                ", ".join(item.invariant.value for item in violation.violations),
            )
            corrected = await self._runner.run_round(
                intake_id=session.session_id,
                developer_instructions=INTAKE_SESSION_DEVELOPER_INSTRUCTIONS,
                prompt=_correction_prompt(violation, mode=mode),
                output_schema=output_schema,
                resume_thread_id=result.thread_id,
                model=session.model,
                effort=session.effort,
                service_tier=session.service_tier,
            )
            # The accepted payload is the corrective turn's, but the thread was
            # created by the first one and the caller still has to record that.
            proposal = parse(
                replace(
                    corrected,
                    created_thread=corrected.created_thread or result.created_thread,
                )
            )
            logger.warning(
                "Intake round accepted after one correction (session_id=%s)",
                session.session_id,
            )
            return proposal

    async def rehydrate(
        self,
        session: IntakeSession,
        *,
        visible_event_ids: Sequence[str],
    ) -> ThreadReplacementReceipt:
        fingerprint = intake_session_fingerprint(session)
        prompt = {
            "mode": "rehydrate",
            "prompt_version": INTAKE_SESSION_PROMPT_VERSION,
            "canonical_state_sha256": fingerprint,
            "canonical_session": intake_session_payload(session),
            "visible_event_ids": list(visible_event_ids),
        }
        # No corrective turn here: rehydration exists to replace a thread whose
        # context is already suspect, and a second turn on it proves nothing.
        result = await self._runner.run_round(
            intake_id=session.session_id,
            developer_instructions=INTAKE_SESSION_DEVELOPER_INSTRUCTIONS,
            prompt=(
                "Rehydrate a replacement Intake thread from this canonical state. "
                "Do not advance or revise it; return its exact supplied hash.\n\n"
                + json.dumps(prompt, ensure_ascii=False, sort_keys=True)
            ),
            output_schema=INTAKE_REHYDRATE_OUTPUT_SCHEMA,
            resume_thread_id=None,
            model=session.model,
            effort=session.effort,
            service_tier=session.service_tier,
        )
        raw = _exact(
            result.payload,
            {"canonical_state_sha256"},
            "Intake rehydration receipt",
        )
        observed = raw["canonical_state_sha256"]
        if observed != fingerprint:
            raise ValueError("replacement thread rehydration changed canonical state")
        if not result.created_thread:
            raise ValueError("thread rehydration must create a new persistent thread")
        return ThreadReplacementReceipt(
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            canonical_state_sha256=observed,
        )


class AppServerProblemSpecificationAuditor:
    def __init__(self, runner: EphemeralSpecificationAuditRunner) -> None:
        self._runner = runner

    async def audit(
        self,
        specification: ProblemSpecification,
        *,
        initial_problem: str = "",
        decision_log: Sequence[Mapping[str, Any]] = (),
        mode: IntakeRoundMode = IntakeRoundMode.ADVANCE,
        session_id: str | None = None,
    ) -> SpecificationAudit:
        request = {
            "prompt_version": INTAKE_SESSION_PROMPT_VERSION,
            "schema_version": INTAKE_SESSION_SCHEMA_VERSION,
            "mode": mode.value,
            "initial_problem": initial_problem,
            "decision_log": [dict(item) for item in decision_log],
            "problem_specification": problem_specification_payload(specification),
        }
        result = await self._runner.run_ephemeral(
            developer_instructions=SPEC_AUDIT_DEVELOPER_INSTRUCTIONS,
            prompt=(
                "Audit this candidate Problem Specification.\n\n"
                + json.dumps(request, ensure_ascii=False, sort_keys=True)
            ),
            output_schema=SPEC_AUDIT_OUTPUT_SCHEMA,
        )
        repairs = IntakeRepairLog(session_id=session_id)
        raw = _exact(
            result.payload,
            {
                "passed",
                "public_summary",
                "blocking_questions",
                "declared_defaults",
                "residual_risks",
            },
            "specification audit",
        )
        if not isinstance(raw["passed"], bool):
            raise _violation(
                IntakeInvariant.PAYLOAD_TYPE,
                "specification audit passed must be boolean",
            )
        question_values = raw["blocking_questions"]
        if not isinstance(question_values, list):
            raise _violation(
                IntakeInvariant.PAYLOAD_TYPE, "blocking_questions must be an array"
            )
        questions = tuple(
            _parse_question(item, index, repairs=repairs)
            for index, item in enumerate(question_values)
        )
        if any(not question.blocking for question in questions):
            raise ValueError("specification audit questions must be blocking")
        ungrounded = tuple(
            question.question_id
            for question in questions
            if question.grounded_in is None
        )
        if ungrounded:
            raise ValueError(
                "audit questions must quote the initial problem or specification "
                "text in grounded_in: " + ", ".join(sorted(ungrounded))
            )
        if (
            not isinstance(raw["public_summary"], str)
            or not raw["public_summary"].strip()
        ):
            raise ValueError("specification audit summary must be non-empty text")
        public_summary = raw["public_summary"].strip()
        residual_risks = _texts(
            raw["residual_risks"], "audit residual risks", repairs=repairs
        )
        declared_defaults = _parse_declared_defaults(
            raw["declared_defaults"], label="audit declared_defaults", repairs=repairs
        )
        passed = raw["passed"]
        if mode is IntakeRoundMode.FINALIZE:
            # Finalize is a user-owned stop condition. A noncompliant auditor
            # must not reopen the interview; retain its concerns as risks.
            if not passed:
                residual_risks = (*residual_risks, f"Finalize audit: {public_summary}")
            residual_risks = (
                *residual_risks,
                *(
                    f"{question.title}: {question.why_it_matters}"
                    for question in questions
                ),
            )
            passed = True
            questions = ()
        else:
            if passed == bool(questions):
                raise ValueError(
                    "specification audit must either pass or return blocking questions"
                )
            # The auditor only appends to an open specification, so a
            # problem-class gap it found cannot be settled here; it is carried
            # as a risk the user can read instead of being declared behind
            # their back.
            misclassified = tuple(
                item
                for item in declared_defaults
                if item.decision_class is DecisionClass.PROBLEM
            )
            if misclassified:
                logger.warning(
                    "Specification audit declared %s problem-class default(s); "
                    "carried as residual risks instead",
                    len(misclassified),
                )
                residual_risks = (
                    *residual_risks,
                    *(f"{item.title}: {item.statement}" for item in misclassified),
                )
                declared_defaults = tuple(
                    item
                    for item in declared_defaults
                    if item.decision_class is not DecisionClass.PROBLEM
                )
        return SpecificationAudit(
            passed=passed,
            public_summary=public_summary,
            blocking_questions=questions,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            declared_defaults=declared_defaults,
            residual_risks=residual_risks,
            repairs=repairs.entries(),
        )
