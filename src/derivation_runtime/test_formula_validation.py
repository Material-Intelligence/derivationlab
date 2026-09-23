"""Regression coverage for shared scientific-text diagnostics."""

import pytest

from derivation_runtime.formula_validation import (
    math_fragments,
    validate_fields,
    validate_math,
)


@pytest.mark.parametrize(
    "command", [r"\boxed{x}", r"x\cdots y", r"{\bf x}", r"{\rm x}", r"{\cal H}"]
)
def test_conventional_commands_are_supported(command):
    assert validate_math(command) == []


@pytest.mark.parametrize(
    "value,code",
    [
        (r"\input{secret}", "unsafe_command"),
        (r"\def\x{y}", "unsafe_command"),
        (r"\unknown{x}", "unsupported_command"),
        ("x\x0crac{1}{2}", "control_character"),
        (r"\frac{a}{b", "syntax_error"),
        (r"\begin{matrix}x\end{cases}", "syntax_error"),
        (r"\begin{matrix}x", "syntax_error"),
    ],
)
def test_diagnostic_categories(value, code):
    assert code in {d.code for d in validate_math(value)}


def test_code_currency_escapes_and_offsets():
    text = r"Price $5 and $10. `\input{x}` ```$\unknown$``` escaped \$ then $\boxed{x}$"
    assert validate_fields({"claim": text}) == []
    fragments = list(math_fragments({"claim": text}))
    assert len(fragments) == 1
    fragment = fragments[0]
    assert text[fragment.start : fragment.end] == r"\boxed{x}"
    assert fragment.field == "claim" and fragment.formula_index == 1


def test_all_fields_preserve_precise_error_positions():
    fields = {
        name: r"Before $\unknown{x}$ after"
        for name in ("claim", "derivation", "assumptions", "checks", "source")
    }
    diagnostics = validate_fields(fields)
    assert len(diagnostics) == 5
    for diagnostic in diagnostics:
        assert (
            fields[diagnostic.field][diagnostic.start : diagnostic.end] == r"\unknown"
        )
        assert diagnostic.formula_index == 1


@pytest.mark.parametrize(
    "value", [r"Text \[x", r"Text \(x", "Text $$x", r"Text \]", r"Text \)"]
)
def test_unmatched_delimiters(value):
    assert any(d.code == "syntax_error" for d in validate_fields({"claim": value}))


def test_control_in_prose_and_no_input_mutation():
    fields = {"source": "Original\x08 text"}
    assert validate_fields(fields)[0].code == "control_character"
    assert fields == {"source": "Original\x08 text"}


@pytest.mark.parametrize("text", [r"$2 x$", r"$2 \alpha$", "$2 x $", "$2$"])
def test_numeric_formulas_are_paired_before_currency_detection(text):
    fields = {"derivation": text}
    assert validate_fields(fields) == []
    fragments = list(math_fragments(fields))
    assert len(fragments) == 1
    assert text[fragments[0].start : fragments[0].end] == text[1:-1]
    assert fields == {"derivation": text}


@pytest.mark.parametrize(
    "text", ["$5", "$10", "Price $5 and $10.", r"Pay $5 and $10, then use $2 \alpha$."]
)
def test_currency_amounts_do_not_form_accidental_formulas(text):
    assert validate_fields({"claim": text}) == []
    fragments = list(math_fragments({"claim": text}))
    assert len(fragments) == int("alpha" in text)


@pytest.mark.parametrize("length", [1, 2, 3, 4, 7])
def test_arbitrary_backtick_runs_protect_code(length):
    delimiter = "`" * length
    text = delimiter + r"$\unknown$" + delimiter + r" then $2 x$"
    assert validate_fields({"claim": text}) == []
    assert [f.value for f in math_fragments({"claim": text})] == ["2 x"]


def test_shorter_runs_inside_fence_do_not_end_code():
    text = "````tex\n```\n" + r"$\unknown$" + "\n```\n````"
    assert validate_fields({"claim": text}) == []
    assert list(math_fragments({"claim": text})) == []


@pytest.mark.parametrize("text", ["$$", "$$$$", "$ $", r"\(\)", r"\[\]"])
def test_empty_math_is_not_silently_discarded(text):
    assert any(d.code == "syntax_error" for d in validate_fields({"claim": text}))


@pytest.mark.parametrize("text", [r"$x \(y\)$", r"\[x $y$\]", r"$$x \[y\]$$"])
def test_nested_math_delimiters_are_rejected(text):
    fields = {"claim": text}
    assert any(d.code == "syntax_error" for d in validate_fields(fields))
    assert fields == {"claim": text}
