"""Auditable prompts and JSON Schemas for the Codex App Server runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from derivation_agent_record import canonical_json, sha256_text

from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS
from .types import (
    CheckRequest,
    FormulaEquivalenceRequest,
    FormulaRepairRequest,
    JudgeRequest,
    StepSnapshot,
    WriterRequest,
)

STEP_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim", "why", "source", "derivation", "scope"],
    "properties": {
        field: {"type": "string", "minLength": 1}
        for field in ("claim", "why", "source", "derivation", "scope")
    },
}

WRITER_CONTROL_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "alternatives", "revise_step_revision_id", "reason"],
    "properties": {
        "decision": {"enum": ["continue", "fork", "complete", "revise", "blocked"]},
        "revise_step_revision_id": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
        "alternatives": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}

WRITER_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "control"],
    "properties": {
        "content": STEP_OUTPUT_SCHEMA,
        "control": WRITER_CONTROL_SCHEMA,
    },
}

CHECK_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason", "evidence"],
    "properties": {
        "verdict": {"enum": ["ok", "objection", "hard_defect", "instrument_failure"]},
        "reason": {"type": "string", "minLength": 1},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "source_id", "quote"],
                "properties": {
                    "kind": {
                        "enum": [
                            "ancestor_quote",
                            "hypothesis_quote",
                            "scope_quote",
                            "task_constraint_quote",
                        ]
                    },
                    "source_id": {"type": "string", "minLength": 1},
                    "quote": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

JUDGE_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason", "score"],
    "properties": {
        "verdict": {"enum": ["pass", "near_pass", "fail"]},
        "reason": {"type": "string", "minLength": 1},
        "score": {"type": ["number", "null"]},
    },
}

WRITER_DEVELOPER_INSTRUCTIONS = """You are the writer in a scientific derivation tree.
Produce one meaningful scientific segment toward a local goal you choose, retaining
the load-bearing algebra. Respect the requested granularity; one_task may contain
several related algebraic transformations. Your final assistant message must be
only the JSON object constrained by the writer output schema. Put the five scientific
step fields under content and one scheduling recommendation under control. The control
decision is continue when this branch needs another step, fork when one or more
non-equivalent alternative hypotheses should be explored, or complete when this branch
is ready for independent checking and judgement. Use revise to correct ANY earlier
step, whether the Checker is enabled or disabled: set revise_step_revision_id to its
exact ID, explain the reason, and put the entire replacement in content. Revision
invalidates the old suffix; rebuild any affected later results. Use blocked when you
cannot make further progress: explain the actual obstacle in reason and content;
this does not claim completion. Set revise_step_revision_id=null for other decisions.
Fork requires alternatives; the other decisions require an empty alternatives array.
When you would otherwise report blocked because a load-bearing ingredient is not
determined by the declared inputs, first use fork with the non-equivalent closure
routes you can name, including a route that restricts the declared operator or
Hamiltonian class and says so; report blocked only when no such route remains.
Review checker_feedback as fallible scientific criticism. Address objections with
specific algebra, evidence or a correction; never treat the Checker as an oracle.
When the current tip has an unresolved hard_defect and its revision ID remains
available, the output schema requires revise: replace that step even when the
replacement would otherwise be a final answer. Do not pair complete or continue with
a revision ID.
After max_local_repairs attempts on the same step lineage without new evidence,
explicitly retain the unresolved condition and explore another useful subgoal, or
report blocked. Do not repeat the same repair indefinitely. Do not use a result that
has a deterministic confirmed_refutation as a valid premise. An unresolved claim
may support only explicitly conditional exploration and must remain disclosed at
completion. Self-correction remains available without a Checker. You may call scientific_compute when a
bounded symbolic calculation would materially check the step. It accepts mathematical
expressions, not Python code. You may also use the declared source_catalog/source_search/
source_read and transcript_catalog/transcript_read tools within this run's permitted
sources and current route, and feedback_read for earlier recorded checking opinions.
When reading_preparation is present, it is a system-arranged, hash-bound input from
this run's allowed method sources. Use it before deriving. Full documents are actual
delivered source text, while reader notes remain fallible navigation and must retain
their cited source anchors. Material withheld from this run and any final judgement
are never generation sources.
When the request or reading_preparation contains completion_requirements, they are generic closure
obligations that apply to any derivation task; satisfy them with actual algebra,
definitions and the dependency ledger before returning complete.
When reading_preparation delivers full documents, first make a short internal inventory
of which exact source spans support each load-bearing task requirement and which gaps
remain unsupported; then use one coherent declared model. On later turns, use source_read
for any needed exact span. The inventory is navigation, not scientific evidence.
No other tools are allowed. Do not ask for approval or
user input. After receiving feedback on the current tip, if you wish to finish without
changing its scientific content, return complete and repeat that tip's five fields
exactly; this acknowledges its recorded unresolved checks and does not create another
scientific step. Changed content is a new step and receives a new check."""

CHECKER_DEVELOPER_INSTRUCTIONS = """You are an independent checker. Inspect only the
provided task, target step, explicit route transcript, and this run's permitted method
sources through the declared source and transcript reading tools. Return only JSON matching
the checker output schema. A hard_defect requires direct quoted evidence with an
allowlisted evidence kind; preferences and uncertainty are objections, not hard
defects. Copy each evidence quote verbatim from the corresponding tool result as one short
contiguous substring. Preserve every Markdown, LaTeX, Unicode, whitespace, and newline
character exactly; never retype, normalize, join, or paraphrase a quote. When deterministic
completion requirements are supplied, enforce their complete
coverage only when candidate_completion_intent is true. When it is false, treat them as a
route roadmap: check the current algebra and any claimed partial closure, but do not reject
an internally valid intermediate step merely because later required sections are absent.
When candidate_completion_intent is true, the unit under review is the assembled route,
not the target step alone: read the route transcript and decide, requirement by
requirement, which step actually establishes it. A requirement that no step in the route
establishes is not covered, and a step that is internally valid on its own does not make
the route complete.
When candidate_completion_intent is true, also verify the candidate's dependency ledger
entry by entry. For a SOURCED entry, read the cited span with source_read and confirm
that it supports the claim as the candidate uses it. For a DERIVED_HERE entry, confirm
that the referenced step actually establishes it. Record a hard_defect for a ledger
entry whose cited source does not support it, for a load-bearing ingredient of the
endpoint that has no ledger entry, and for a load-bearing UNRESOLVED entry that the
candidate nevertheless treats as closed. The ledger is the candidate's own list; do not
add entries on its behalf.
When candidate_completion_intent is true, also read the candidate's declared scope
against the derivation it rests on. For every operator class, Hamiltonian class,
coupling, or approximation the scope claims to cover, confirm that some step of the
route carries the term, construction, or argument that handles it. A class the scope
claims with nothing in the derivation that handles it is a hard_defect: quote the
scope sentence that makes the claim, and state that the candidate must either narrow
the scope to the class the result rests on or derive the missing part.
When candidate_completion_intent is true, recompute from the candidate's own final
formula the leading-order expansion in every limit its declared scope names. A
divergence in a limit the scope declares finite, a part of the formula the declared
approximation requires but the formula lacks, or a completion rule that is named but
not written is a hard_defect: quote the offending term or name the missing part. You
may use scientific_compute for the expansion.
Route steps are identified by slot. The transcript_catalog gives every step of the current
route its step slot together with the superseded revision ids that slot has already had. A
reference in a ledger entry or in an ancestor citation that names a step slot on the current
route, or that names any superseded revision of such a slot, resolves to that slot's current
revision and is never by itself a defect.
Regardless of completion intent, do not accept a claimed representation that a requirement
explicitly excludes or a load-bearing contribution that disappears without proof. Do not
continue or rewrite the derivation."""

JUDGE_DEVELOPER_INSTRUCTIONS = """You are an independent terminal judge. Evaluate
only the provided candidate transcript against the task. Return only JSON matching
the judgement output schema. The engine declares a candidate only after the writer
has completed the branch and all step checks have run. Tool-call execution, branch
completion, checker acceptance, budgets, and strict replay are platform acceptance
conditions verified outside your scientific verdict; do not require those operational
records to be repeated inside the candidate prose. Judge the scientific claims,
reasoning, scope, and requested deliverable only. Do not extend the derivation or call
tools."""


# Rendered instead of an offer the run cannot honour. A run whose frozen
# configuration caps the active branches at one can never create the child branch
# a fork asks for, so `writer_output_schema` drops `fork` from the decision enum
# for such a run and this sentence says so in the developer text. Both halves key
# off the same fact, so the Writer is never asked to choose an option the schema
# forbids. Wording contract: fixtures/prompt_rule_texts_v1.md, "Fork before
# reporting blocked".
FORK_UNAVAILABLE_WRITER_PARAGRAPH = """Forking is unavailable in this run and the output schema does not offer it, so
where the rule above asks for a fork, choose instead between restricting the
declared operator or Hamiltonian class and saying so, and reporting blocked."""

# What the host tells the Writer on the turn after a fork it could not create.
# The run allowed forks, the Writer named alternatives, and the active-branch cap
# was already reached when the host tried to open the child branch. Saying nothing
# would let the branch continue as though the alternatives were being explored
# elsewhere. Wording contract: the same file, "Trace of a suppressed fork".
FORK_SUPPRESSED_WRITER_NOTE = (
    "The run's active-branch limit was already reached, so no branch was created "
    "for these alternatives and nothing else will explore them. Continue on this "
    "branch: either carry one of them yourself and say which, or restrict the "
    "declared operator or Hamiltonian class and say so. Do not assume another "
    "branch covers them."
)

# The two paragraphs below are the switchable "intent ledger first" obligation.
# They are inserted only for a run whose frozen configuration declares
# intent_ledger_first; with the flag off every rendered text stays byte-identical
# to the baseline, so a run with the flag on differs from one with it off in
# exactly one paragraph. The wording is fixed by the prompt contract
# (fixtures/prompt_rule_texts_v1.md, "Intent ledger first") and is reproduced
# verbatim apart from line wrapping.
INTENT_LEDGER_FIRST_WRITER_PARAGRAPH = """Before any derivation step, the first step of every route must be an intent
ledger. It states: (i) the starting object and the quantity you will compute;
(ii) the endpoint form you will deliver; (iii) every approximation, standard
result and identity you intend to rely on, each with status HYPOTHESIS and,
where you already know it, the allowlisted source span you expect to support it;
(iv) the class of Hamiltonian and operators you intend to assume. The intent
ledger contains no derivation. Later steps may add, drop or replace entries; the
final dependency ledger must reconcile with it: every intent entry is either
carried forward with its final status or explicitly retired with a reason."""

INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH = """When the target step is the first step of its route and the run declares
intent_ledger_first, check only two things: that every cited source span exists
and supports the intended use, and that the entries are mutually consistent (for
example, the declared Hamiltonian class admits the operators the candidate
intends to use). Do not judge the completeness of the plan and do not require
derivation. Record a hard_defect only for a cited source that contradicts the
intended use, or for an internal contradiction between entries. When
candidate_completion_intent is true, also verify that the final dependency ledger
reconciles with the intent ledger: a retired entry carries a reason; an intent
entry that vanished silently is a hard_defect."""

# The two paragraphs below are the switchable "dimension check" obligation.
# They are inserted only for a run whose frozen configuration declares
# dimension_check; with the flag off every rendered text stays byte-identical
# to the baseline. The wording is fixed by the prompt contract
# (fixtures/prompt_rule_texts_v1.md, "Dimensional closure") and is reproduced
# verbatim apart from line wrapping.
DIMENSION_CHECK_WRITER_PARAGRAPH = """Before declaring complete, the final step's derivation field must contain one
explicit dimensional-analysis line for the endpoint formula, written in the unit
system the derivation itself declares. State the dimension of every factor of
that formula, show that the product of those dimensions equals the dimension of
the quantity the task defines, and make every dimensional constant the check
requires (for example hbar, c, e, 2*pi, a volume, or a normalisation) appear
explicitly in the formula rather than leaving it implicit. A formula whose
dimensions do not close is not complete."""

DIMENSION_CHECK_CHECKER_PARAGRAPH = """When candidate_completion_intent is true and the run declares dimension_check,
recompute that dimensional-analysis line independently from the candidate's own
declared definitions and unit system. Assign a dimension to every factor of the
endpoint formula and confirm that the product equals the dimension of the
quantity the task defines. A missing dimensional-analysis line, or a product
whose dimensions do not close, is a hard_defect; quote the offending factor."""

# Where each paragraph joins its baseline text. The Writer obligations follow the
# sentence that introduces the dependency ledger, and the Checker obligations
# follow the ledger-verification block and its slot-resolution rule, so the
# closure rules read as one block in both roles.
_WRITER_LEDGER_ANCHOR = (
    "definitions and the dependency ledger before returning complete.\n"
)
_CHECKER_LEDGER_ANCHOR = "revision and is never by itself a defect.\n"
# Where the cap paragraph joins: directly after the always-on rule that tells the
# Writer to fork before reporting blocked, so the exception reads with the rule it
# qualifies.
_WRITER_FORK_ANCHOR = "report blocked only when no such route remains.\n"


def _with_paragraph(text: str, anchor: str, paragraph: str) -> str:
    """Splice one paragraph in at a unique anchor, or fail at import time.

    A silently missed anchor would append nothing and produce a flag-on run
    that differs from the baseline in no way at all, so the anchor is required
    to occur exactly once.
    """

    if text.count(anchor) != 1:
        raise RuntimeError("developer instruction anchor is not unique")
    return text.replace(anchor, anchor + paragraph + "\n", 1)


INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS = _with_paragraph(
    WRITER_DEVELOPER_INSTRUCTIONS,
    _WRITER_LEDGER_ANCHOR,
    INTENT_LEDGER_FIRST_WRITER_PARAGRAPH,
)

INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS = _with_paragraph(
    CHECKER_DEVELOPER_INSTRUCTIONS,
    _CHECKER_LEDGER_ANCHOR,
    INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH,
)

DIMENSION_CHECK_WRITER_DEVELOPER_INSTRUCTIONS = _with_paragraph(
    WRITER_DEVELOPER_INSTRUCTIONS,
    _WRITER_LEDGER_ANCHOR,
    DIMENSION_CHECK_WRITER_PARAGRAPH,
)

DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS = _with_paragraph(
    CHECKER_DEVELOPER_INSTRUCTIONS,
    _CHECKER_LEDGER_ANCHOR,
    DIMENSION_CHECK_CHECKER_PARAGRAPH,
)


def writer_developer_instructions(
    *,
    intent_ledger_first: bool = False,
    dimension_check: bool = False,
    fork_available: bool = True,
) -> str:
    # The obligations splice at the same anchor, so the dimension paragraph goes
    # in first and the intent paragraph lands ahead of it. The order is fixed
    # here rather than left to the call site, so two runs with the same flags
    # cannot differ by paragraph order alone.
    text = WRITER_DEVELOPER_INSTRUCTIONS
    if not fork_available:
        text = _with_paragraph(
            text, _WRITER_FORK_ANCHOR, FORK_UNAVAILABLE_WRITER_PARAGRAPH
        )
    if dimension_check:
        text = _with_paragraph(
            text, _WRITER_LEDGER_ANCHOR, DIMENSION_CHECK_WRITER_PARAGRAPH
        )
    if intent_ledger_first:
        text = _with_paragraph(
            text, _WRITER_LEDGER_ANCHOR, INTENT_LEDGER_FIRST_WRITER_PARAGRAPH
        )
    return text


def checker_developer_instructions(
    *,
    intent_ledger_first: bool = False,
    dimension_check: bool = False,
) -> str:
    text = CHECKER_DEVELOPER_INSTRUCTIONS
    if dimension_check:
        text = _with_paragraph(
            text, _CHECKER_LEDGER_ANCHOR, DIMENSION_CHECK_CHECKER_PARAGRAPH
        )
    if intent_ledger_first:
        text = _with_paragraph(
            text, _CHECKER_LEDGER_ANCHOR, INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH
        )
    return text


def writer_user_prompt(request: WriterRequest) -> str:
    payload: dict[str, Any] = {
        "role": "writer",
        "run_id": request.run_id,
        "branch_id": request.branch_id,
        "step_slot": request.step_slot,
        "task": request.task_text,
        "hypothesis": request.hypothesis,
        "granularity": request.granularity,
        "checker_enabled": request.checker_enabled,
        "record_version": request.record_version,
        "checker_feedback": list(request.checker_feedback),
        "feedback_catalog": [
            {
                key: item[key]
                for key in (
                    "check_id",
                    "step_revision_id",
                    "verdict",
                    "issue_anchor",
                )
            }
            for item in catalog_transcript(
                request.full_checker_feedback or request.checker_feedback
            )
        ],
        "full_feedback_count": len(
            request.full_checker_feedback or request.checker_feedback
        ),
        "feedback_read_rule": "Use feedback_read(check_id,offset=0) for full recorded opinions; check_id=catalog pages older IDs. Truncated summaries are navigation, not complete evidence.",
        "repair_attempts": request.repair_attempts,
        "max_local_repairs": request.max_local_repairs,
        "exhausted_revision_ids": list(request.exhausted_revision_ids),
        "transcript": [
            {
                "step_revision_id": item.step_revision_id,
                "content": item.content.to_record(),
            }
            for item in request.transcript
        ],
        "transcript_catalog": [
            {
                "step_revision_id": item.step_revision_id,
                "step_slot": item.step_slot,
                "superseded_step_revision_ids": list(item.superseded_step_revision_ids),
                "claim": item.content.claim[:500],
                "scope": item.content.scope[:500],
            }
            for item in catalog_transcript(
                request.full_transcript or request.transcript
            )
        ],
        "full_transcript_length": len(request.full_transcript or request.transcript),
        "history_read_rule": "Omitted content remains immutable in the full record. Use transcript_catalog(offset) for omitted IDs; transcript_read(step_revision_id,field,offset=0) reads claim/why/source/derivation/scope, follow next_offset until complete. Read omitted steps before relying on or revising them; the catalog is navigation, not proof. Each catalog entry carries the step's step_slot on this route and the superseded_step_revision_ids that slot has already had; refer to a step of this route by its slot number, which stays stable when the step is replaced.",
        "reading_preparation": request.preparation_context,
    }
    # Prepared, hash-bound contexts retain their own delivered requirements.
    # Ordinary product runs have no preparation and receive the current gates.
    if request.preparation_context is None:
        payload["completion_requirements"] = list(COMMON_WRITER_CLOSURE_REQUIREMENTS)
    # Rendered only when the run declares the obligation, so a baseline prompt
    # stays byte-identical to the one archived runs were given.
    if request.intent_ledger_first:
        payload["intent_ledger_first"] = True
    if request.dimension_check:
        payload["dimension_check"] = True
    # Host dispositions the Writer could not observe from its own transcript. The
    # key is absent whenever there are none, so an ordinary turn renders exactly
    # the bytes it rendered before this field existed.
    if request.runtime_notes:
        payload["runtime_notes"] = [dict(item) for item in request.runtime_notes]
    if request.formula_validation_policy:
        payload["formula_format_requirements"] = (
            "Use self-contained standard TeX math with balanced delimiters, braces, "
            "and environments. Do not rely on manuscript macros or external files. "
            "JSON-escape backslashes correctly. Preserve source quotations verbatim; "
            "do not rewrite quoted evidence to satisfy formatting. Host format feedback "
            "is separate from scientific Checker feedback: correct the current unsealed "
            "slot without revising a sealed step or changing the scientific claims."
        )
    if request.formula_feedback:
        payload["formula_feedback"] = [dict(item) for item in request.formula_feedback]
    return canonical_json(payload)


def _unique_sources(sources):
    """One catalog entry per kind/source pair.

    Several texts can share a source id - the completion requirements all sit
    under the run's task id, because the record requires task evidence to carry
    exactly that id - and the catalog is a list of what may be quoted, not of
    how the text is stored.
    """

    seen = set()
    unique = []
    for item in sources:
        key = (item.kind, item.source_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def checker_output_schema(request: CheckRequest) -> Mapping[str, Any]:
    schema = deepcopy(CHECK_OUTPUT_SCHEMA)
    source_ids = sorted({item.source_id for item in request.evidence_sources})
    schema["properties"]["evidence"]["items"]["properties"]["kind"]["enum"] = sorted(
        {item.kind for item in request.evidence_sources}
    ) or list(
        CHECK_OUTPUT_SCHEMA["properties"]["evidence"]["items"]["properties"]["kind"][
            "enum"
        ]
    )
    if source_ids:
        schema["properties"]["evidence"]["items"]["properties"]["source_id"] = {
            "type": "string",
            "enum": source_ids,
        }
    else:
        schema["properties"]["evidence"]["maxItems"] = 0
    return schema


def writer_output_schema(request: WriterRequest) -> Mapping[str, Any]:
    schema = deepcopy(WRITER_OUTPUT_SCHEMA)
    allowed = [
        item.step_revision_id
        for item in (request.full_transcript or request.transcript)
        if item.step_revision_id not in request.exhausted_revision_ids
    ]
    control = schema["properties"]["control"]["properties"]
    control["revise_step_revision_id"] = {
        "type": ["string", "null"],
        "enum": [None, *allowed],
    }
    if not allowed:
        control["decision"]["enum"].remove("revise")
    if request.record_version == "1.0":
        control["decision"]["enum"] = ["continue", "fork", "complete"]
        control["revise_step_revision_id"] = {"type": "null"}
    else:
        forced_revision = next(
            (
                item.get("step_revision_id")
                for item in reversed(request.checker_feedback)
                if item.get("verdict") == "hard_defect"
                and item.get("step_revision_id") in allowed
            ),
            None,
        )
        if forced_revision is not None:
            control["decision"]["enum"] = ["revise"]
            control["revise_step_revision_id"] = {
                "type": "string",
                "enum": [forced_revision],
            }
    # A run that may hold only one active branch can never open the child branch a
    # fork asks for, so the option is removed rather than offered and discarded.
    # Applied last so it also covers the narrowed 1.0 and forced-revision enums.
    if not request.fork_available:
        control["decision"]["enum"] = [
            item for item in control["decision"]["enum"] if item != "fork"
        ]
    return schema


def checker_user_prompt(request: CheckRequest) -> str:
    payload: dict[str, Any] = {
        "role": "checker",
        "run_id": request.run_id,
        "check_id": request.check_id,
        "task": request.task_text,
        "target": {
            "step_revision_id": request.target.step_revision_id,
            "content": request.target.content.to_record(),
        },
        "transcript": [
            {
                "step_revision_id": item.step_revision_id,
                "content": item.content.to_record(),
            }
            for item in request.transcript
        ],
        "evidence_sources": [
            item.to_record()
            if item.kind != "literature_quote"
            and (item.kind != "ancestor_quote" or request.record_version == "1.0")
            else {
                "kind": item.kind,
                "source_id": item.source_id,
                "sha256": sha256_text(item.text),
                "text_access": "Read the exact lines through source_read before quoting."
                if item.kind == "literature_quote"
                else "Read exact fields through transcript_read before quoting. The catalog alone is not evidence.",
            }
            for item in _unique_sources(request.evidence_sources)
        ],
        "transcript_catalog": [
            {
                "step_revision_id": item.step_revision_id,
                "step_slot": item.step_slot,
                "superseded_step_revision_ids": list(item.superseded_step_revision_ids),
            }
            for item in catalog_transcript(
                request.full_transcript or request.transcript
            )
        ],
        "evidence_rule": "Use only an exact kind/source_id pair from evidence_sources and copy one short contiguous exact substring of its text. For tool-backed text, copy directly from source_read or transcript_read; never retype, normalize, join lines, or alter Markdown, LaTeX, Unicode, whitespace, or newlines. Never invent aliases such as target.claim or task.scope.",
        "completion_requirements": list(request.completion_requirements),
        "candidate_completion_intent": request.completion_intent,
    }
    # The Checker paragraph keys off both facts, so they are rendered together
    # and only for a run that declares the obligation; a baseline check prompt
    # stays byte-identical.
    if request.intent_ledger_first:
        payload["intent_ledger_first"] = True
        payload["target_is_route_first_step"] = request.target_is_route_first_step
    if request.dimension_check:
        payload["dimension_check"] = True
    return canonical_json(payload)


def judge_user_prompt(request: JudgeRequest) -> str:
    return canonical_json(
        {
            "role": "judge",
            "run_id": request.run_id,
            "judgement_id": request.judgement_id,
            "candidate_id": request.candidate_id,
            "unresolved_checks": list(request.unresolved_checks),
            "task": request.task_text,
            "transcript": [item.content.to_record() for item in request.transcript],
        }
    )


# Typeset layer of a finished route. Neither text names a task, a field of
# physics or a notation: both apply to any derivation whose recorded formulas do
# not compile under the locked report engine. Changing either text changes the
# prompt hashes audited in every later typeset layer.
FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS = """You repair the TeX syntax of formulas
from a finished scientific derivation so that they compile as display math under
standard LaTeX with amsmath. The derivation itself is already recorded and is not
being revised. For each affected step you receive its recorded fields and, for every
formula of that step that failed, the formula id, the LaTeX body that failed and the
compiler error. Return only the JSON object required by the output schema, with
exactly one corrected LaTeX body for every formula id you were given.
Change only what the formula needs in order to compile: balance braces, delimiters and
environments; replace an undefined, package-specific or manuscript-specific command by
standard commands with the same meaning; remove stray control characters. Keep every
symbol, index, operator, relation, sign and factor the formula states; do not simplify,
extend, complete or correct the mathematics. When the failed body came from an earlier
correction, repair the recorded formula again rather than building on that attempt.
Give the formula body only, without $, \\( \\) or \\[ \\] delimiters and without
comments or macro definitions. JSON-escape every backslash. When the intended meaning
of a command is unclear, choose the reading the step text supports and keep the
notation as close to the recorded formula as possible. No tools are available."""

FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS = """You are an independent checker of formula
corrections. Formulas of a finished scientific derivation did not compile, and the
proposed corrections change more than grouping or delimiters. For each item decide only
whether the corrected formula states the same mathematics that the recorded formula
intended, read in the context of the recorded step fields. A difference in presentation
that leaves the mathematical content unchanged is equivalent. A changed or added symbol,
index, operator, relation, sign, factor or term that the recorded step does not support
is not_equivalent, and so is a correction that settles an ambiguity the step leaves
open. Return only the JSON object required by the output schema, with one verdict and a
short reason for every formula id you were given. Do not rewrite formulas and do not
judge the derivation itself. No tools are available."""


def formula_repair_user_prompt(request: FormulaRepairRequest) -> str:
    return canonical_json(
        {
            "role": "formula_repair",
            "run_id": request.run_id,
            "repair_id": request.repair_id,
            "route_id": request.route_id,
            "steps": [
                {
                    "step_revision_id": step.step_revision_id,
                    "content": step.content.to_record(),
                    "failed_formulas": [item.to_record() for item in step.formulas],
                }
                for step in request.steps
            ],
        }
    )


def formula_repair_output_schema(request: FormulaRepairRequest) -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["corrections"],
        "properties": {
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["formula_id", "latex"],
                    "properties": {
                        "formula_id": {
                            "type": "string",
                            "enum": list(request.formula_ids),
                        },
                        "latex": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }


def formula_review_user_prompt(request: FormulaEquivalenceRequest) -> str:
    return canonical_json(
        {
            "role": "formula_equivalence_review",
            "run_id": request.run_id,
            "review_id": request.review_id,
            "route_id": request.route_id,
            "steps": [
                {
                    "step_revision_id": step.step_revision_id,
                    "content": step.content.to_record(),
                }
                for step in request.steps
            ],
            "corrections": [item.to_record() for item in request.items],
        }
    )


def formula_review_output_schema(
    request: FormulaEquivalenceRequest,
) -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviews"],
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["formula_id", "verdict", "reason"],
                    "properties": {
                        "formula_id": {
                            "type": "string",
                            "enum": [item.formula_id for item in request.items],
                        },
                        "verdict": {"enum": ["equivalent", "not_equivalent"]},
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }


def rehydrated_writer_instructions(
    transcript: Sequence[StepSnapshot],
    *,
    intent_ledger_first: bool = False,
    dimension_check: bool = False,
    fork_available: bool = True,
) -> str:
    payload = [
        {
            "step_revision_id": item.step_revision_id,
            "content": item.content.to_record(),
        }
        for item in transcript
    ]
    return (
        writer_developer_instructions(
            intent_ledger_first=intent_ledger_first,
            dimension_check=dimension_check,
            fork_available=fork_available,
        )
        + "\n\nThis new provider thread is rehydrated from the exact sealed product "
        + "transcript below. Treat it as immutable prior branch context:\n"
        + canonical_json(payload)
    )


def bounded_transcript(
    transcript: Sequence[StepSnapshot],
    *,
    max_chars: int = 48000,
) -> tuple[StepSnapshot, ...]:
    """Keep recent exact segments; omitted originals remain available by ID."""
    selected: list[StepSnapshot] = []
    size = 0
    for item in reversed(transcript):
        length = len(canonical_json(item.content.to_record()))
        if length > max_chars:
            continue
        if size + length > max_chars:
            break
        selected.append(item)
        size += length
    return tuple(reversed(selected))


def catalog_transcript(transcript: Sequence[StepSnapshot]) -> Sequence[StepSnapshot]:
    return (
        transcript if len(transcript) <= 104 else (*transcript[:8], *transcript[-96:])
    )


__all__ = [
    "CHECKER_DEVELOPER_INSTRUCTIONS",
    "CHECK_OUTPUT_SCHEMA",
    "DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS",
    "DIMENSION_CHECK_CHECKER_PARAGRAPH",
    "DIMENSION_CHECK_WRITER_DEVELOPER_INSTRUCTIONS",
    "DIMENSION_CHECK_WRITER_PARAGRAPH",
    "FORK_SUPPRESSED_WRITER_NOTE",
    "FORK_UNAVAILABLE_WRITER_PARAGRAPH",
    "FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS",
    "FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS",
    "INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS",
    "INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH",
    "INTENT_LEDGER_FIRST_WRITER_DEVELOPER_INSTRUCTIONS",
    "INTENT_LEDGER_FIRST_WRITER_PARAGRAPH",
    "JUDGE_DEVELOPER_INSTRUCTIONS",
    "JUDGE_OUTPUT_SCHEMA",
    "STEP_OUTPUT_SCHEMA",
    "WRITER_CONTROL_SCHEMA",
    "WRITER_DEVELOPER_INSTRUCTIONS",
    "WRITER_OUTPUT_SCHEMA",
    "checker_developer_instructions",
    "checker_output_schema",
    "checker_user_prompt",
    "formula_repair_output_schema",
    "formula_repair_user_prompt",
    "formula_review_output_schema",
    "formula_review_user_prompt",
    "judge_user_prompt",
    "rehydrated_writer_instructions",
    "writer_developer_instructions",
    "writer_user_prompt",
]
