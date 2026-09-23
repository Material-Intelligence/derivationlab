"""The always-on limit-expansion obligation.

A final formula can look complete while missing part of its dependence on a
declared variable, or while asserting its behaviour in a declared limit rather
than deriving it. Closure obligations 4 and 6 already ask for every required
part of the result and for the limiting checks, but a self-check that is only
asserted is easy to skip and nothing recomputes it. This obligation has the same
shape as the dimensional one: the Writer owes an explicit artefact in the final
step, and the Checker recomputes it independently.

Unlike the dimensional obligation this pair is always on, so there is no
configuration with the flag off; what has to stay true instead is that the two
switchable obligations still splice exactly one paragraph each onto the new
baseline, and that a run declaring neither of them renders the baseline byte for
byte.

The expected wording is pinned below, so these tests depend on nothing outside
the repository.
"""

from __future__ import annotations

import json

from .capabilities import SOURCE_READING_V1
from .prompts import (
    CHECKER_DEVELOPER_INSTRUCTIONS,
    DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS,
    DIMENSION_CHECK_CHECKER_PARAGRAPH,
    INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
    INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH,
    WRITER_DEVELOPER_INSTRUCTIONS,
    checker_developer_instructions,
    writer_developer_instructions,
    writer_user_prompt,
)
from .scientific_runtime import SCIENTIFIC_TOOL_NAME
from .source_material import SOURCE_TOOL_NAMES
from .types import WriterRequest

EXPECTED_GATE = """For every limit the declared scope names (for example a limit in which a declared variable tends to zero or to infinity), write the leading-order expansion of the final formula in that limit explicitly in the final step, naming each part of the formula the scope requires that you retain, or the rule that completes a part you omit; a declared limit whose behaviour is asserted rather than written out is not closed."""
EXPECTED_CHECKER = """When candidate_completion_intent is true, recompute from the candidate's own final formula the leading-order expansion in every limit its declared scope names. A divergence in a limit the scope declares finite, a part of the formula the declared approximation requires but the formula lacks, or a completion rule that is named but not written is a hard_defect: quote the offending term or name the missing part. You may use scientific_compute for the expansion."""
EXPECTED_OBLIGATION_FOUR = """Derive the full result the task's scope demands, including every part of it that your declared approximation requires; keeping only the part that is convenient in one regime and dropping a part the scope covers is incomplete. Audit overall signs and factors against your own stated conventions with an explicit consistency check."""


def _flat(value: str) -> str:
    return " ".join(value.split())


def test_ordinary_product_writer_receives_current_gates_without_preparation() -> None:
    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    request = WriterRequest(
        run_id="product",
        branch_id="root",
        step_slot=1,
        task_text="Derive the invariant.",
        hypothesis="Use symmetry.",
        transcript=(),
    )
    payload = json.loads(writer_user_prompt(request))
    assert payload["reading_preparation"] is None
    assert payload["completion_requirements"] == list(
        COMMON_WRITER_CLOSURE_REQUIREMENTS
    )
    assert EXPECTED_GATE in payload["completion_requirements"][5]
    assert (
        "request or reading_preparation contains completion_requirements"
        in WRITER_DEVELOPER_INSTRUCTIONS
    )


def test_prepared_writer_keeps_its_archived_gates_without_current_duplicates() -> None:
    preparation = {"completion_requirements": ["An archived closure obligation."]}
    request = WriterRequest(
        run_id="archive",
        branch_id="root",
        step_slot=1,
        task_text="Derive the invariant.",
        hypothesis="Use symmetry.",
        transcript=(),
        preparation_context=preparation,
    )
    payload = json.loads(writer_user_prompt(request))
    assert payload["reading_preparation"] == preparation
    assert "completion_requirements" not in payload


def test_limit_texts_match_the_pinned_product_contract() -> None:
    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    assert _flat(EXPECTED_GATE) in _flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[5])
    assert _flat(EXPECTED_CHECKER) in _flat(CHECKER_DEVELOPER_INSTRUCTIONS)


def test_the_gate_sentence_qualifies_the_limiting_checks_it_belongs_to() -> None:
    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    gate = _flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[5])
    # It lands with the sentence it sharpens rather than after the closing
    # unresolved-issues sentence, and the rest of the gate is untouched.
    assert gate.index("Show the symmetry and limiting checks") < gate.index(
        "For every limit the declared scope names"
    )
    assert gate.index("For every limit the declared scope names") < gate.index(
        "List only genuinely non-load-bearing unresolved issues"
    )
    assert gate.endswith(
        "any missing ingredient needed to compute the endpoint means the "
        "derivation is incomplete."
    )
    # The example names the shape of a limit, never a particular one: naming a
    # specific limit would steer every task towards that limit and bias the
    # Writer's own check.
    lowered = gate.lower()
    assert "a declared variable tends to zero or to infinity" in lowered
    for named_limit in ("high-temperature", "thermodynamic limit", "weak-coupling"):
        assert named_limit not in lowered


def test_the_neutralised_wording_matches_the_pinned_product_contract() -> None:
    """The two texts must read as generic to any derivation task.

    The Writer and Checker texts carry no domain-flavoured vocabulary, even
    where it would name no particular quantity, so the obligation that carries
    the expansion and the paragraph that recomputes it speak of parts of the
    formula rather than of named physical regimes.
    """

    from .closure_requirements import COMMON_WRITER_CLOSURE_REQUIREMENTS

    assert _flat(EXPECTED_OBLIGATION_FOUR) == _flat(
        COMMON_WRITER_CLOSURE_REQUIREMENTS[3]
    )

    gate = _flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[5])
    assert "the conversion to the quantities the task names" in gate
    assert (
        "naming each part of the formula the scope requires that you retain, or "
        "the rule that completes a part you omit" in gate
    )
    checker = _flat(CHECKER_DEVELOPER_INSTRUCTIONS)
    assert (
        "a part of the formula the declared approximation requires but the "
        "formula lacks" in checker
    )
    assert "quote the offending term or name the missing part" in checker
    # The obligation still forbids keeping only the convenient piece; only the
    # vocabulary changed.
    obligation = _flat(COMMON_WRITER_CLOSURE_REQUIREMENTS[3])
    assert obligation.startswith("Derive the full result the task's scope demands")
    assert "dropping a part the scope covers is incomplete" in obligation
    assert obligation.endswith(
        "Audit overall signs and factors against your own stated conventions "
        "with an explicit consistency check."
    )


def test_the_checker_rule_sits_with_the_other_completion_intent_paragraphs() -> None:
    flat = _flat(CHECKER_DEVELOPER_INSTRUCTIONS)
    # After the scope check it extends, before the slot-resolution rule that
    # anchors the two switchable paragraphs, and before the closing always-on
    # sentence.
    assert flat.index("either narrow the scope to the class") < flat.index(
        "recompute from the candidate's own final formula"
    )
    assert flat.index("recompute from the candidate's own final formula") < flat.index(
        "Route steps are identified by slot"
    )
    assert flat.index("Route steps are identified by slot") < flat.index(
        "Regardless of completion intent"
    )


def test_the_switchable_obligations_still_add_exactly_one_paragraph_each() -> None:
    # The always-on paragraph joins the baseline, so the splice anchor and both
    # switchable paragraphs have to keep meaning what they meant: one paragraph added,
    # nothing else moved.
    assert writer_developer_instructions() == WRITER_DEVELOPER_INSTRUCTIONS
    assert checker_developer_instructions() == CHECKER_DEVELOPER_INSTRUCTIONS
    for rendered, paragraph in (
        (
            DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS,
            DIMENSION_CHECK_CHECKER_PARAGRAPH,
        ),
        (
            INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
            INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH,
        ),
    ):
        assert rendered != CHECKER_DEVELOPER_INSTRUCTIONS
        assert (
            rendered.replace(paragraph + "\n", "", 1) == CHECKER_DEVELOPER_INSTRUCTIONS
        )
    assert (
        checker_developer_instructions(intent_ledger_first=True, dimension_check=True)
        .replace(INTENT_LEDGER_FIRST_CHECKER_PARAGRAPH + "\n", "", 1)
        .replace(DIMENSION_CHECK_CHECKER_PARAGRAPH + "\n", "", 1)
        == CHECKER_DEVELOPER_INSTRUCTIONS
    )


def test_the_checker_really_has_the_tool_the_rule_offers() -> None:
    # The paragraph tells the Checker it may use scientific_compute. The role
    # filter in _dynamic_tools_for_role keeps that tool for a non-Writer role
    # only when the profile also declares the source tools, so the offer is
    # honest exactly for the tool-enabled Record 1.1 profile.
    allowed = set(SOURCE_READING_V1.allowed_tools)
    assert SCIENTIFIC_TOOL_NAME in allowed
    assert set(SOURCE_TOOL_NAMES) & allowed
