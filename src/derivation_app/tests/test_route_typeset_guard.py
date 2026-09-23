"""The host guard must not let a rewritten equation pass as a syntax repair.

Only a correction that changes presentation is
accepted by the host; anything else goes to the Checker's light review. These
cases are the ones a review of the first implementation showed slipping
through, plus the repairs that must keep passing.
"""

from __future__ import annotations

import pytest

from derivation_app.route_typeset import (
    STATUS_REPAIRED,
    error_commands,
    latest_compiler_messages,
    syntax_only_guard,
)
from derivation_app.tests.test_route_typeset import (
    RuleRunner,
    ScriptedTypesetRuntime,
    build,
    entries,
    step,
)
from derivation_runtime.formula_validation import load_engine_whitelist

WHITELIST = load_engine_whitelist()


# Same tokens, different mathematics: a multiset of commands and identifiers
# cannot tell these apart, which is why the guard compares their order.
SEMANTIC_REWRITES = [
    ("a+b", "a-b", "an operator was replaced"),
    (r"\frac{a}{b}", r"\frac{b}{a}", "numerator and denominator were swapped"),
    (r"\int_0^1", r"\int_1^0", "the integration limits were swapped"),
    ("E=mc^2", "E=mc_2", "an exponent became a subscript"),
    (r"\sqrt{a}+\sqrt{b}", r"\sqrt{a+b}", "two roots became one"),
    ("a_1b_2", "a_2b_1", "the subscripts were swapped"),
]


@pytest.mark.parametrize("original,corrected,reason", SEMANTIC_REWRITES)
def test_a_changed_equation_is_never_accepted_as_syntax(original, corrected, reason):
    guard = syntax_only_guard(original, corrected, allowed_commands=())
    assert not guard["within_guard"], reason
    # Unlimited licence from the compiler does not help: the guard refuses on
    # structure, which no command name can excuse.
    generous = syntax_only_guard(
        original, corrected, allowed_commands={"frac", "sqrt", "int"}
    )
    assert not generous["within_guard"], reason
    assert guard["structure_difference"] is not None


@pytest.mark.parametrize("original,corrected,reason", SEMANTIC_REWRITES)
def test_a_changed_equation_reaches_the_checker(original, corrected, reason):
    """The refusal must be a review, not a silent drop: see typeset_route."""

    guard = syntax_only_guard(original, corrected, allowed_commands=())
    assert guard["within_guard"] is False
    assert not guard["structure_equal"] or not (
        guard["identifiers_equal"] and guard["commands_equal"]
    )


def test_a_missing_brace_is_still_a_syntax_repair():
    assert syntax_only_guard(r"\frac{a}{b", r"\frac{a}{b}", allowed_commands=())[
        "within_guard"
    ]
    assert syntax_only_guard(r"x^2^3", r"{x^2}^3", allowed_commands=())["within_guard"]


def test_an_unsupported_command_may_be_replaced_by_a_named_synonym():
    """``\\bm`` is not in the engine vocabulary; the error names its synonym."""

    messages = [
        r"Undefined control sequence \bm; this engine provides \boldsymbol",
    ]
    allowed = error_commands(messages, r"\bm{q}", WHITELIST)
    assert {"bm", "boldsymbol"} <= allowed
    assert syntax_only_guard(r"\bm{q}", r"\boldsymbol{q}", allowed_commands=allowed)[
        "within_guard"
    ]


def test_sizing_delimiters_may_be_closed_but_bare_ones_may_not_be_added():
    assert syntax_only_guard(
        r"\left( a + b", r"\left( a + b \right)", allowed_commands=()
    )["within_guard"]
    # A bare parenthesis is arithmetic, not sizing.
    assert not syntax_only_guard(r"a + b \times c", r"(a + b) \times c", allowed_commands=())[
        "within_guard"
    ]


def test_a_command_named_in_one_round_is_not_allowed_in_a_later_round():
    """MAJOR 6: the licence comes from the current compile, not from history."""

    errors = [
        {"round": 0, "message": r"Undefined control sequence \bm; use \boldsymbol"},
        {"round": 1, "message": r"Undefined control sequence \mathscr"},
        {"round": 2, "message": r"Missing $ inserted"},
    ]
    assert latest_compiler_messages(errors) == [r"Missing $ inserted"]
    assert latest_compiler_messages(errors[:1]) == [
        r"Undefined control sequence \bm; use \boldsymbol"
    ]
    assert latest_compiler_messages([]) == []

    round_one = error_commands(
        latest_compiler_messages(errors[:1]), r"\bm{q}", WHITELIST
    )
    round_three = error_commands(latest_compiler_messages(errors), r"\bm{q}", WHITELIST)
    assert "boldsymbol" in round_one
    assert "boldsymbol" not in round_three
    # The synonym the round-1 error named is licensed in round 1 and not later.
    assert syntax_only_guard(
        r"\bm{q}", r"\boldsymbol{q}", allowed_commands=round_one
    )["within_guard"]
    assert not syntax_only_guard(
        r"\bm{q}", r"\boldsymbol{q}", allowed_commands=round_three
    )["within_guard"]


def test_layout_and_spacing_stay_invisible_to_the_guard():
    assert syntax_only_guard(r"a+b", r"a \, + \; b", allowed_commands=())[
        "within_guard"
    ]
    assert syntax_only_guard(r"\displaystyle a+b", r"a+b", allowed_commands=())[
        "within_guard"
    ]


def test_a_later_round_does_not_inherit_the_verdict_of_an_earlier_one(tmp_path):
    """R2 #7: guard and review describe the correction that is on the table."""

    runtime = ScriptedTypesetRuntime(
        # Round 1 proposes a different expression - beyond the guard, so the
        # Checker decides, and calls it equivalent. It still does not compile.
        repairs=[
            lambda request: [(request.formula_ids[0], r"\alpha^2")],
            lambda request: [(request.formula_ids[0], r"{x^2}^3")],
        ],
        reviews=[
            lambda request: [(request.items[0].formula_id, "equivalent", "same")]
        ],
    )
    layer = build(
        tmp_path,
        [step("step_0001", derivation=r"First \(x^2^3\).")],
        runner=RuleRunner([r"^2^3", r"\alpha"]),
        runtime=runtime,
    )
    entry = entries(layer)["step_0001:derivation:1"]
    assert entry["status"] == STATUS_REPAIRED
    assert entry["typeset"] == r"{x^2}^3"
    # The round-2 correction is within the guard, so no verdict applies to it.
    assert entry["guard"]["within_guard"] is True
    assert entry["review"] is None
    # The history is still there; only the summary fields moved on.
    assert [item["round"] for item in entry["repair_attempts"]] == [1, 2]
    assert entry["repair_attempts"][0]["reason"] == "reviewed as equivalent"


# ---------------------------------------------------------------------------
# The licensing leak: TeX's echoed source line is context, not a licence.
#
# ``_compiler_message`` appends the line the engine echoed back ("l.42 $\alpha
# \otimes \beta = \gamma \bm") so the Writer can see where the error is. The
# first implementation harvested every ``\name`` of the whole message, so every
# command standing on the failing line was licensed - and because a licensed
# command is masked to one interchangeable token, the verifier could rewrite
# the equation and still pass as "syntax only".

#: The message the fixed pipeline produces for a real ``\bm`` failure: TeX
#: breaks the echoed line after the token it stopped on.
ECHOED_UNDEFINED = (
    r"Undefined control sequence "
    r"(context: l.42 $\alpha \otimes \beta = \gamma \bm)"
)
#: The same error when the engine echoed the whole line instead of its halves.
ECHOED_WHOLE_LINE = (
    r"Undefined control sequence "
    r"(context: l.42 $\alpha \otimes \beta = \gamma \bm{q}$)"
)

VERIFIER_COUNTEREXAMPLES = [
    (r"\beta \otimes \alpha", "the two factors were swapped"),
    (r"\gamma \otimes \beta", "a factor was replaced by another symbol"),
]


@pytest.mark.parametrize("message", [ECHOED_UNDEFINED, ECHOED_WHOLE_LINE])
@pytest.mark.parametrize("corrected,reason", VERIFIER_COUNTEREXAMPLES)
def test_the_echoed_source_line_does_not_license_a_rewrite(
    message, corrected, reason
):
    """BLOCKER: both verifier probes passed the guard as "syntax only"."""

    original = r"\alpha \otimes \beta"
    allowed = error_commands([message], original, WHITELIST)
    assert not allowed & {"alpha", "otimes", "beta", "gamma"}, reason
    assert not syntax_only_guard(original, corrected, allowed_commands=allowed)[
        "within_guard"
    ], reason


def test_only_the_offending_command_of_an_echoed_line_is_licensed():
    """The last token of the consumed half is the error; the rest is source."""

    allowed = error_commands(
        [ECHOED_UNDEFINED], r"\alpha \otimes \beta = \gamma \bm{q}", WHITELIST
    )
    # ``bm`` is the command TeX stopped on (and the one the locked engine does
    # not provide); nothing else on that line is evidence about anything.
    assert allowed == {"bm"}


def test_a_command_only_in_the_echo_is_never_licensed():
    """A supported command that merely stands on the failing line stays fixed."""

    message = r"Missing $ inserted (context: l.42 $\alpha \otimes \beta = \gamma)"
    allowed = error_commands([message], r"\alpha \otimes \beta", WHITELIST)
    assert allowed == set()
    # Not even the trailing one: only ``Undefined control sequence`` says the
    # token it stopped on is a command at all.
    assert "gamma" not in allowed


def test_a_missing_glyph_message_licenses_nothing():
    message = (
        "Missing character U+03B1 in font lmroman10-regular "
        r"(context: l.42 $\alpha + \beta$)"
    )
    assert error_commands([message], r"\alpha + \beta", WHITELIST) == set()


def test_an_undefined_command_named_by_the_error_stays_within_guard():
    """The repair the guard exists to wave through must still be waved through."""

    allowed = error_commands(
        [r"Undefined control sequence \bm; this engine provides \boldsymbol"],
        r"\gamma \bm{q}",
        WHITELIST,
    )
    assert {"bm", "boldsymbol"} <= allowed
    assert syntax_only_guard(
        r"\gamma \bm{q}", r"\gamma \boldsymbol{q}", allowed_commands=allowed
    )["within_guard"]


def test_a_long_echoed_line_keeps_the_token_the_engine_stopped_on():
    """A cut from the end would put an unrelated command in the licence."""

    from derivation_app.route_typeset import CONTEXT_LIMIT, _shorten_context

    body = " ".join([r"\gamma_{" + str(index) + "}" for index in range(60)])
    echoed = rf"l.42 $\alpha \otimes {body} \bm"
    assert len(echoed) > CONTEXT_LIMIT
    shortened = _shorten_context(echoed)
    assert len(shortened) <= CONTEXT_LIMIT
    assert shortened.endswith(r"\bm")
    allowed = error_commands(
        [f"Undefined control sequence (context: {shortened})"],
        r"\alpha \otimes \bm{q}",
        WHITELIST,
    )
    assert allowed == {"bm"}


def test_a_rewritten_equation_reaches_the_checker_through_the_layer(tmp_path):
    """End to end: the probe is reviewed, not accepted by the host."""

    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"\beta \otimes \alpha")]],
        reviews=[
            lambda request: [
                (request.items[0].formula_id, "not_equivalent", "the factors differ")
            ]
        ],
    )
    layer = build(
        tmp_path,
        [step("step_0001", derivation=r"Then \(\alpha \otimes \beta\).")],
        runner=RuleRunner([r"\otimes"]),
        runtime=runtime,
    )
    entry = entries(layer)["step_0001:derivation:1"]
    assert len(runtime.review_requests) == 1
    assert entry["guard"]["within_guard"] is False
    assert entry["repair_attempts"][0]["accepted"] is False
    assert entry["repair_attempts"][0]["reason"] == "reviewed as not equivalent"
    assert entry["review"] == {
        "verdict": "not_equivalent",
        "reason": "the factors differ",
    }
    assert entry["typeset"] == entry["original"]
    assert {"correction_not_equivalent", "formula_failed"} <= {
        flag["code"] for flag in layer["flags"]
    }


# ---------------------------------------------------------------------------
# Sizing delimiters: a missing one may be added, none may change shape.


def test_a_sizing_delimiter_may_not_change_shape():
    guard = syntax_only_guard(r"\left( a \right)", r"\left[ a \right]", allowed_commands=())
    assert not guard["within_guard"]
    assert guard["delimiters_preserved"] is False
    assert guard["delimiters"] == [["(", ")"], ["[", "]"]]
    # The half-open interval this protects.
    assert not syntax_only_guard(
        r"\left[ a , b \right)", r"\left[ a , b \right]", allowed_commands=()
    )["within_guard"]


def test_a_missing_sizing_delimiter_may_still_be_closed():
    guard = syntax_only_guard(r"\left( a + b", r"\left( a + b \right)", allowed_commands=())
    assert guard["within_guard"]
    assert guard["delimiters_preserved"] is True
    assert guard["delimiters"] == [["("], ["(", ")"]]
    # ...and the opening one, which is what an orphaned ``\right)`` needs.
    assert syntax_only_guard(r"a + b \right)", r"\left( a + b \right)", allowed_commands=())[
        "within_guard"
    ]
