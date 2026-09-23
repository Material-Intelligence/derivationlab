"""The record package derives a macro expansion instead of believing it.

Lives beside the runtime tests because that is where this suite runs; the
subject is ``derivation_agent_record.macro_expansion``, which imports nothing
from the runtime on purpose - it is the independent second implementation the
replay check is worth anything for.
"""

from __future__ import annotations

import pytest

from derivation_agent_record.macro_expansion import (
    DefinitionTable,
    MacroCheckError,
    parse_definitions,
    verify_macro_expansion,
)

SOURCE = "\n".join(  # noqa: FLY002 - the comments are the line numbers
    [
        r"\def\la{\lambda}",  # line 1
        r"\def\xv{{\bf x}}",  # line 2
        r"\newcommand{\abs}[1]{\vert #1\vert}",  # line 3
        r"\newcommand{\pder}[2][t]{\partial_{#1}#2}",  # line 4
        r"\DeclareMathOperator{\sgn}{sgn}",  # line 5
        r"\newcommand{\ak}{\abs{k}}",  # line 6
        r"The Green function of the string reads G(\xv,\la).",  # line 7
    ]
)
SOURCES = {"src_method": SOURCE}
TABLE = DefinitionTable(SOURCES)


def check(original, replacement, line, *, field_text="", end=0, name="src_method"):
    return verify_macro_expansion(
        field_text=field_text,
        start=0,
        end=end,
        original=original,
        replacement=replacement,
        source_id=name,
        source_line=line,
        table=TABLE,
    )


def test_the_parser_finds_every_kind_and_its_line():
    found = {item.name: item for item in parse_definitions(SOURCE, "src_method")}
    assert set(found) == {"la", "xv", "abs", "pder", "sgn", "ak"}
    assert found["xv"].line == 2 and found["xv"].template == r"{\bf x}"
    assert found["abs"].nargs == 1
    assert found["pder"].nargs == 2 and found["pder"].default == "t"
    assert found["sgn"].template == r"\operatorname{sgn}"


def test_a_true_expansion_is_accepted():
    assert check(r"\xv", r"{\bf x}", 2).name == "xv"
    assert check(r"\la", r"\lambda", 1).name == "la"
    assert check(r"\abs{k}", r"\vert k\vert", 3).name == "abs"
    assert check(r"\pder{u}", r"\partial_{t}u", 4).name == "pder"
    assert check(r"\pder[x]{u}", r"\partial_{x}u", 4).name == "pder"
    assert check(r"\sgn", r"\operatorname{sgn}", 5).name == "sgn"


def test_a_nested_definition_is_re_derived_one_more_level():
    # ``\ak`` expands to ``\abs{k}``, which the runtime expands in turn.
    assert check(r"\ak", r"\vert k\vert", 6).name == "ak"
    with pytest.raises(MacroCheckError, match="not to"):
        check(r"\ak", r"\vert j\vert", 6)


def test_a_forged_replacement_is_refused():
    """The cited line really defines \\xv; the replacement is not what it says."""

    with pytest.raises(MacroCheckError, match="not to"):
        check(r"\xv", r"{\bf p}_{\rm forged}", 2)
    with pytest.raises(MacroCheckError, match="not to"):
        check(r"\abs{k}", r"\vert k\vert + \epsilon", 3)


def test_the_cited_line_must_define_this_macro():
    with pytest.raises(MacroCheckError, match=r"does not define \\xv"):
        check(r"\xv", r"{\bf x}", 1)
    with pytest.raises(MacroCheckError, match="does not define"):
        check(r"\xv", r"{\bf x}", 7)
    with pytest.raises(MacroCheckError, match="does not define"):
        check(r"\unknown", "x", 2)


def test_the_original_must_be_the_macro_and_exactly_its_arguments():
    with pytest.raises(MacroCheckError, match="not exactly the macro"):
        check(r"\xv x", r"{\bf x} x", 2)
    with pytest.raises(MacroCheckError, match="too few arguments"):
        check(r"\abs", r"\vert \vert", 3)
    with pytest.raises(MacroCheckError, match="control word"):
        check("xv", r"{\bf x}", 2)


def test_an_expansion_may_not_change_the_math_structure():
    with pytest.raises(MacroCheckError, match="math structure"):
        check(r"\xv", "{\\bf x", 2)
    with pytest.raises(MacroCheckError, match="math structure"):
        check(r"\xv", r"$x$", 2)


def test_a_separating_space_is_accepted_only_where_the_text_needs_one():
    # ``\la`` before a letter: the runtime appends a space so ``\lambda`` keeps
    # its own name. ``field_text[end]`` is what decides.
    assert check(r"\la", r"\lambda ", 1, field_text=r"\lax", end=3).name == "la"
    with pytest.raises(MacroCheckError, match="not to"):
        check(r"\la", r"\lambda ", 1, field_text=r"\la+1", end=3)


def test_an_unregistered_source_has_no_definitions():
    with pytest.raises(MacroCheckError, match="does not define"):
        check(r"\xv", r"{\bf x}", 2, name="src_other")
