"""The one normalisation whose replacement text is not fixed by its own shape.

A control repair can only insert a backslash or delete a control character, so
its own kind pins what it may have done. A macro expansion can put *anything*
where the macro was — which is exactly the shape of an attack: rewrite sealed
scientific text and call it an expansion. ``macro_expansion.py`` exists to make
that claim checkable, by re-deriving the expansion from the frozen source and
requiring the recorded replacement to be what the cited definition produces.

So the tests that matter here are the negative ones: for every definition form,
a replacement that is *not* what the definition yields must be refused. The
positive cases are the control — a checker that refuses everything would pass a
file of rejections.

Nothing here is reachable from a shipped record, because a macro expansion has
to cite registered source text and no published record may carry any (see
``tests/test_examples.py``). These are unit tests against the module's own API.
"""

from __future__ import annotations

import pytest

from derivation_agent_record.macro_expansion import (
    MAX_EXPANSION_DEPTH,
    DefinitionTable,
    MacroCheckError,
    parse_definitions,
    verify_macro_expansion,
)

SOURCE_ID = "src-1"


def table_of(text: str) -> DefinitionTable:
    return DefinitionTable({SOURCE_ID: text})


def check(
    source: str,
    line: int,
    field_text: str,
    original: str,
    replacement: str,
    *,
    start: int | None = None,
) -> None:
    """Verify one expansion the way the replay engine does."""

    where = field_text.index(original) if start is None else start
    verify_macro_expansion(
        field_text=field_text,
        start=where,
        end=where + len(original),
        original=original,
        replacement=replacement,
        source_id=SOURCE_ID,
        source_line=line,
        table=table_of(source),
    )


# ---------------------------------------------------------------------------
# One definition form per row, each with the expansion it does produce
# ---------------------------------------------------------------------------

#: (label, source text, line the definition is on, original, replacement)
GOOD = [
    (
        "newcommand-no-args",
        "\\newcommand{\\eps}{\\varepsilon}\n",
        1,
        "\\eps",
        "\\varepsilon",
    ),
    (
        "newcommand-one-arg",
        "\\newcommand{\\vv}[1]{\\mathbf{#1}}\n",
        1,
        "\\vv{E}",
        "\\mathbf{E}",
    ),
    (
        "newcommand-two-args",
        "\\newcommand{\\pd}[2]{\\frac{\\partial #1}{\\partial #2}}\n",
        1,
        "\\pd{f}{x}",
        "\\frac{\\partial f}{\\partial x}",
    ),
    (
        "newcommand-optional-arg-defaulted",
        "\\newcommand{\\norm}[1][2]{\\lVert x \\rVert_{#1}}\n",
        1,
        "\\norm",
        "\\lVert x \\rVert_{2}",
    ),
    (
        "newcommand-optional-arg-given",
        "\\newcommand{\\norm}[1][2]{\\lVert x \\rVert_{#1}}\n",
        1,
        "\\norm[1]",
        "\\lVert x \\rVert_{1}",
    ),
    (
        "renewcommand",
        "% a note\n\\renewcommand{\\vec}[1]{\\mathbf{#1}}\n",
        2,
        "\\vec{B}",
        "\\mathbf{B}",
    ),
    (
        "providecommand",
        "\\providecommand{\\half}{\\tfrac{1}{2}}\n",
        1,
        "\\half",
        "\\tfrac{1}{2}",
    ),
    (
        "def",
        "\\def\\Efield{\\mathbf{E}}\n",
        1,
        "\\Efield",
        "\\mathbf{E}",
    ),
    (
        "gdef-with-parameters",
        "\\gdef\\pair#1#2{(#1, #2)}\n",
        1,
        "\\pair{a}{b}",
        "(a, b)",
    ),
    (
        "declaremathoperator",
        "\\DeclareMathOperator{\\Tr}{Tr}\n",
        1,
        "\\Tr",
        "\\operatorname{Tr}",
    ),
    (
        "declaremathoperator-star",
        "\\DeclareMathOperator*{\\argmin}{arg\\,min}\n",
        1,
        "\\argmin",
        "\\operatorname*{arg\\,min}",
    ),
]


@pytest.mark.parametrize(("label", "source", "line", "original", "replacement"), GOOD, ids=[row[0] for row in GOOD])
def test_a_faithful_expansion_is_accepted(label: str, source: str, line: int, original: str, replacement: str) -> None:
    check(source, line, f"the term {original} appears here", original, replacement)


@pytest.mark.parametrize(("label", "source", "line", "original", "replacement"), GOOD, ids=[row[0] for row in GOOD])
def test_an_unfaithful_expansion_is_refused(
    label: str, source: str, line: int, original: str, replacement: str
) -> None:
    """The whole point: the recorded text must be the one the definition gives.

    Each row is retried with a plausible-looking substitution that the cited
    definition does not produce. A host that rewrote sealed text and labelled it
    an expansion would look exactly like this.
    """

    forged = replacement.replace("\\", "\\wrong", 1) if "\\" in replacement else replacement + "x"
    if forged == replacement:  # pragma: no cover - defensive
        pytest.skip("no distinct forgery for this row")

    with pytest.raises(MacroCheckError):
        check(source, line, f"the term {original} appears here", original, forged)


# ---------------------------------------------------------------------------
# The boundaries the module's own docstring names
# ---------------------------------------------------------------------------


def test_the_cited_line_must_carry_the_definition() -> None:
    source = "\\newcommand{\\eps}{\\varepsilon}\n\\newcommand{\\del}{\\partial}\n"

    check(source, 1, "a \\eps b", "\\eps", "\\varepsilon")

    with pytest.raises(MacroCheckError, match="does not define"):
        check(source, 2, "a \\eps b", "\\eps", "\\varepsilon")


def test_a_definition_the_parser_cannot_read_is_not_guessed_at() -> None:
    """Skipped, not assumed: an unreadable definition may not authorise anything."""

    source = "\\newcommand\n"

    assert parse_definitions(source, SOURCE_ID) == []
    with pytest.raises(MacroCheckError, match="does not define"):
        check(source, 1, "a \\eps b", "\\eps", "\\varepsilon")


def test_the_original_must_end_where_the_argument_reading_ends() -> None:
    """``original`` may not swallow text the macro did not consume."""

    source = "\\newcommand{\\vv}[1]{\\mathbf{#1}}\n"

    with pytest.raises(MacroCheckError, match="not exactly the macro and its arguments"):
        check(source, 1, "see \\vv{E} now", "\\vv{E} now", "\\mathbf{E} now")


def test_an_expansion_must_replace_a_control_word() -> None:
    source = "\\newcommand{\\eps}{\\varepsilon}\n"

    with pytest.raises(MacroCheckError, match="control word"):
        check(source, 1, "plain text here", "text", "\\varepsilon")


def test_too_few_arguments_is_an_error_not_a_silent_pass() -> None:
    source = "\\newcommand{\\pd}[2]{\\frac{\\partial #1}{\\partial #2}}\n"

    with pytest.raises(MacroCheckError):
        check(source, 1, "see \\pd{f} here", "\\pd{f}", "\\frac{\\partial f}{\\partial }")


@pytest.mark.parametrize("hostile", ["$x$", "100%", "#1", "`x`", "\\(x\\)"])
def test_a_replacement_may_not_change_the_math_structure(hostile: str) -> None:
    """Math delimiters, comments, parameter tokens and code spans re-tokenise."""

    source = "\\newcommand{\\eps}{\\varepsilon}\n"

    with pytest.raises(MacroCheckError, match="math structure"):
        check(source, 1, "a \\eps b", "\\eps", hostile)


def test_an_unbalanced_replacement_is_refused() -> None:
    source = "\\newcommand{\\eps}{\\varepsilon}\n"

    with pytest.raises(MacroCheckError, match="math structure"):
        check(source, 1, "a \\eps b", "\\eps", "{\\varepsilon")


def test_a_separating_space_is_allowed_only_before_a_letter() -> None:
    """The runtime appends one space when a control word would run into a letter."""

    source = "\\newcommand{\\eps}{\\varepsilon}\n"

    check(source, 1, "a \\epsx", "\\eps", "\\varepsilon ")
    with pytest.raises(MacroCheckError):
        check(source, 1, "a \\eps b", "\\eps", "\\varepsilon ")


def test_a_nested_definition_is_re_derived_rather_than_trusted() -> None:
    source = "\\newcommand{\\eps}{\\varepsilon}\n\\newcommand{\\epsr}{\\eps_{r}}\n"

    check(source, 2, "a \\epsr b", "\\epsr", "\\varepsilon_{r}")
    with pytest.raises(MacroCheckError):
        check(source, 2, "a \\epsr b", "\\epsr", "\\varepsilon_{s}")


def test_a_self_referential_definition_stops_instead_of_recursing_forever() -> None:
    """``MAX_EXPANSION_DEPTH`` is a real bound, and a cycle is not an exception."""

    assert MAX_EXPANSION_DEPTH > 0
    source = "\\newcommand{\\loop}{\\loop}\n"

    check(source, 1, "a \\loop b", "\\loop", "\\loop")


def test_a_commented_out_definition_does_not_count() -> None:
    source = "% \\newcommand{\\eps}{\\varepsilon}\n"

    assert parse_definitions(source, SOURCE_ID) == []
    with pytest.raises(MacroCheckError, match="does not define"):
        check(source, 1, "a \\eps b", "\\eps", "\\varepsilon")


def test_line_numbers_survive_comment_stripping() -> None:
    """Comments are blanked, not removed, so a cited line number stays exact."""

    source = "% first\n% second\n\\newcommand{\\eps}{\\varepsilon}\n"

    definitions = parse_definitions(source, SOURCE_ID)

    assert [(item.name, item.line) for item in definitions] == [("eps", 3)]


def test_the_table_reports_a_name_defined_two_incompatible_ways_as_unusable() -> None:
    """Ambiguity is not resolved by picking one; nested expansion just stops."""

    table = DefinitionTable(
        {
            "a": "\\newcommand{\\x}{1}\n",
            "b": "\\newcommand{\\x}[1]{#1}\n",
        }
    )

    assert table.unique("x") is None
    assert table.at_line("a", 1, "x") is not None
