"""Content-neutrality and scope tests for the shared role prompts.

The checker prompt is generation-side text, so it is bound by the same
provenance rule as the writer gates: it may state process obligations that the
fixed task already imposes, and it may never carry a physical route, identity
or formula.
"""

from __future__ import annotations

from .prompts import (
    CHECKER_DEVELOPER_INSTRUCTIONS,
    DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS,
    FORK_SUPPRESSED_WRITER_NOTE,
    FORK_UNAVAILABLE_WRITER_PARAGRAPH,
    INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
    WRITER_DEVELOPER_INSTRUCTIONS,
)


def test_completion_intent_reviews_the_assembled_route() -> None:
    text = " ".join(CHECKER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        (
            "when candidate_completion_intent is true, the unit under review is the "
            "assembled route, not the target step alone"
        ),
        "requirement by requirement, which step actually establishes it",
        "a requirement that no step in the route establishes is not covered",
        "a step that is internally valid on its own does not make the route complete",
    ):
        assert required in text
    # The roadmap reading for intermediate steps must survive unchanged.
    assert "treat them as a route roadmap" in text


def test_checker_prompt_carries_no_physical_route() -> None:
    # Every switchable form is generation-side text and bound by the same rule,
    # and so are the two Writer texts a run renders only under its own branch
    # cap.  The terms are plain substrings, so each one is chosen not to occur
    # inside an ordinary word of the instructions.
    instructions = (
        CHECKER_DEVELOPER_INSTRUCTIONS,
        INTENT_LEDGER_FIRST_CHECKER_DEVELOPER_INSTRUCTIONS,
        DIMENSION_CHECK_CHECKER_DEVELOPER_INSTRUCTIONS,
        FORK_UNAVAILABLE_WRITER_PARAGRAPH,
        FORK_SUPPRESSED_WRITER_NOTE,
    )
    text = " ".join(" ".join(instructions).lower().split())
    for forbidden_physics in (
        "commutator",
        "sum rule",
        "gauge",
        "kramers",
        "green's function",
        "perturbation theory",
        "partition function",
        "harmonic oscillator",
        "fourier",
        "maxwell",
    ):
        assert forbidden_physics not in text


def test_completion_intent_verifies_the_dependency_ledger() -> None:
    text = " ".join(CHECKER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        "also verify the candidate's dependency ledger entry by entry",
        "for a sourced entry, read the cited span with source_read",
        (
            "for a derived_here entry, confirm that the referenced step actually "
            "establishes it"
        ),
        (
            "record a hard_defect for a ledger entry whose cited source does not "
            "support it"
        ),
        "a load-bearing ingredient of the endpoint that has no ledger entry",
        (
            "a load-bearing unresolved entry that the candidate nevertheless treats "
            "as closed"
        ),
        "the ledger is the candidate's own list; do not add entries on its behalf",
    ):
        assert required in text


def test_completion_intent_checks_the_declared_scope_against_the_derivation() -> None:
    # A declared scope can claim a wider class than the derivation actually
    # handles. The Writer already carries that obligation; this checks that
    # the Checker verifies it.
    text = " ".join(CHECKER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        "also read the candidate's declared scope against the derivation it rests on",
        (
            "for every operator class, hamiltonian class, coupling, or approximation "
            "the scope claims to cover"
        ),
        (
            "confirm that some step of the route carries the term, construction, or "
            "argument that handles it"
        ),
        (
            "a class the scope claims with nothing in the derivation that handles it "
            "is a hard_defect"
        ),
        "quote the scope sentence that makes the claim",
        (
            "either narrow the scope to the class the result rests on or derive the "
            "missing part"
        ),
    ):
        assert required in text


def test_writer_is_told_to_fork_before_reporting_blocked() -> None:
    # A fork belongs at a genuinely underdetermined ingredient, so the rule is
    # stated as the step before blocked rather than as an independent
    # invitation to branch.
    text = " ".join(WRITER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        (
            "when you would otherwise report blocked because a load-bearing "
            "ingredient is not determined by the declared inputs"
        ),
        "first use fork with the non-equivalent closure routes you can name",
        (
            "including a route that restricts the declared operator or hamiltonian "
            "class and says so"
        ),
        "report blocked only when no such route remains",
    ):
        assert required in text
    # The cap-driven exception names the same two remaining choices.
    exception = " ".join(FORK_UNAVAILABLE_WRITER_PARAGRAPH.lower().split())
    assert "forking is unavailable in this run" in exception
    assert "the output schema does not offer it" in exception
    assert "restricting the declared operator or hamiltonian class" in exception
    assert "reporting blocked" in exception


def test_a_slot_reference_on_the_current_route_is_never_a_defect() -> None:
    # A step cannot cite the identifier it will only be given when it is
    # sealed, so a reference to a route position has to resolve on the host
    # side rather than count against the candidate.
    text = " ".join(CHECKER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        "route steps are identified by slot",
        (
            "the transcript_catalog gives every step of the current route its step "
            "slot together with the superseded revision ids that slot has already had"
        ),
        (
            "that names a step slot on the current route, or that names any "
            "superseded revision of such a slot"
        ),
        "resolves to that slot's current revision and is never by itself a defect",
    ):
        assert required in text


def test_completion_intent_recomputes_every_declared_limit() -> None:
    # A final formula can miss part of its dependence on a declared variable,
    # or assert a declared limit rather than derive it. The Writer already owes
    # the limiting checks; this checks that the Checker recomputes them.
    text = " ".join(CHECKER_DEVELOPER_INSTRUCTIONS.lower().split())
    for required in (
        (
            "recompute from the candidate's own final formula the leading-order "
            "expansion in every limit its declared scope names"
        ),
        "a divergence in a limit the scope declares finite",
        (
            "a part of the formula the declared approximation requires but the "
            "formula lacks"
        ),
        "a completion rule that is named but not written is a hard_defect",
        "quote the offending term or name the missing part",
        "you may use scientific_compute for the expansion",
    ):
        assert required in text
