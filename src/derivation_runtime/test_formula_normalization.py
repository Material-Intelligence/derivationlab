"""formula-normalization-v1: control-character restoration, macro expansion,
prose quotations, reversibility, and the formula-v2 static severity rules.

The macro tables and the control-character strings are synthetic, written for
these tests on textbook material (a driven harmonic oscillator, matrix
elements, perturbation theory, a Gaussian integral, a Fourier series). They exercise the shapes the normalizer has to handle:
which control character replaced which backslash, which macro names collide
across sources, and the definition forms of the TeX and LaTeX macro commands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from derivation_agent_record import canonical_json

from .formula_normalization import (
    NORMALIZER_VERSION,
    QUOTATION_MIN_CHARS,
    REPLACEMENT_KINDS,
    SourceMacroTables,
    apply_replacements,
    normalize_step_fields,
    parse_macro_definitions,
    revert_replacements,
)
from .formula_validation import (
    ENGINE_WHITELIST_PATH,
    EngineWhitelist,
    escape_swallow_diagnostics,
    formula_v2_issues,
    load_engine_whitelist,
    validate_fields,
)

FIXTURE = Path(__file__).with_name("fixtures") / "formula_engine_whitelist_fixture.json"
# Synthetic vocabulary: no real engine, so no preamble/platform check.
WHITELIST = load_engine_whitelist(FIXTURE, verify=False)

SRC_A = "src_synthetic_a"
SRC_B_DEFS = "src_synthetic_b_defs"
SRC_B_MAIN = "src_synthetic_b_main"
SRC_C = "src_synthetic_c"

# Synthetic source tables, written for these tests from scratch: shorthands for
# plane waves, Poisson brackets and a free particle. What the tests exercise is
# the line of each definition and the collisions between the tables: \rv means
# different things in A and B, \om is identical in all three, \k is identical in
# A and C, \Rp differs between A and C, and \tick/\tock form a cycle in C.
SRC_A_TEXT = r"""
\def\om{\omega}
\def\Ezero{E_{0}}
\def\rv{\mathbf{r}}
\def\sq#1{{#1}^{2}}
\def\k{\mathbf{k}}
\def\conj#1{#1^{*}}
\def\Om{\Omega}
\def\g2{g_2}
\def\norm#1{\Vert#1\Vert}
\def\Rp{\mathrm{Re}}
\def\tri#1#2#3{#1\cdot(#2\times#3)}
\def\Om#1{\Omega_{#1}}
Separating time from space gives \nabla^2 u(\rv,\om) + \frac{\om^2}{c^2}u(\rv,\om) = 0 at each frequency.
"""
SRC_B_DEFS_TEXT = r"""
\DeclareMathOperator{\sgn}{sgn}
\def\om{\omega}
\providecommand{\ihbar}{\frac{i}{\hbar}}
\newcommand*{\ddt}[1]{\frac{d#1}{dt}}
\def\rv{\vec{r}}
\newcommand\pb[2]{
  \{#1,#2\}}
\renewcommand{\|}{\Vert}
\newcommand{\deriv}[2][x]{\frac{d#2}{d#1}}
\def\Bvec{\mathbf{B}}
%\def\oldB{B_0}
\DeclareMathOperator*{\esssup}{ess\,sup}
"""
SRC_C_TEXT = r"""
\def\tick{\tock}
\def\pv{\mathbf{p}}
\def\k{\mathbf{k}}
\def\Rp{R_{\mathrm{p}}}
\def\Hfree{\frac{\pv^2}{2m}}
\def\sig{\bm\sigma}
\renewcommand\exp{\mathrm{Exp}}
\def\om{\omega}
\def\kk{\k\cdot\k}
\def\tock{\tick}
"""

SOURCES = {
    SRC_A: SRC_A_TEXT,
    SRC_B_DEFS: SRC_B_DEFS_TEXT,
    SRC_B_MAIN: "The main part has no definitions.",
    SRC_C: SRC_C_TEXT,
}


def step(**overrides: str) -> dict[str, str]:
    fields = {
        "claim": "c",
        "why": "w",
        "source": "used_input_refs: task",
        "derivation": "d",
        "scope": "s",
    }
    fields.update(overrides)
    return fields


def run(fields, sources=None, **kwargs):
    sources = SOURCES if sources is None else sources
    result = normalize_step_fields(
        fields, sources=sources, whitelist=WHITELIST, **kwargs
    )
    # Every result must be exactly reversible, via objects and via JSON records.
    records = result.replacement_records()
    assert apply_replacements(fields, result.replacements) == result.fields
    assert revert_replacements(result.fields, result.replacements) == dict(fields)
    decoded = json.loads(canonical_json(records))
    assert apply_replacements(fields, decoded) == result.fields
    assert revert_replacements(result.fields, decoded) == dict(fields)
    for record in records:
        assert set(record) == {
            "field",
            "start",
            "end",
            "original",
            "replacement",
            "kind",
            "source_id",
            "source_line",
        }
        assert record["kind"] in REPLACEMENT_KINDS
    return result


def kinds(result):
    return [item.kind for item in result.replacements]


def cite(*source_ids: str) -> str:
    return "support_refs: " + ", ".join(f"{item}:1-2" for item in source_ids)


# --------------------------------------------------------------------------- whitelist


def test_whitelist_loader_reads_coordinated_schema(tmp_path):
    assert NORMALIZER_VERSION == "formula-normalization-v1"
    assert "mathscr" in WHITELIST.commands and "lhd" not in WHITELIST.commands
    assert "\\" in WHITELIST.control_symbols and "^" not in WHITELIST.control_symbols
    assert "aligned" in WHITELIST.environments
    assert len(WHITELIST.sha256) == 64
    record = json.loads(FIXTURE.read_text())
    record["supported"][","] = ["bare"]
    record["control_symbols"] = [";"]
    loaded = EngineWhitelist.from_record(record)
    assert {",", ";"} <= loaded.control_symbols and "," not in loaded.commands
    for broken in (
        {**record, "schema_version": "other"},
        {**record, "supported": {}},
        {**record, "supported": {"alpha": []}},
        {**record, "environments": "aligned"},
        {**record, "control_symbols": ["ab"]},
    ):
        with pytest.raises(ValueError):
            EngineWhitelist.from_record(broken)
    with pytest.raises(FileNotFoundError):
        load_engine_whitelist(tmp_path / "missing.json")


@pytest.mark.skipif(
    not ENGINE_WHITELIST_PATH.exists(), reason="committed engine whitelist not present"
)
def test_committed_engine_whitelist_loads_and_is_safe():
    committed = load_engine_whitelist()
    assert committed.commands and committed.environments
    from .formula_validation import _UNSAFE_COMMANDS

    assert not committed.commands & _UNSAFE_COMMANDS


def test_v1_vocabulary_unchanged_and_v2_vocabulary_is_engine_derived():
    fields = step(derivation=r"$\mathscr{L} \lhd x$")
    v1 = {d.message.split(";")[0] for d in validate_fields(fields)}
    v2 = {d.message.split(";")[0] for d in validate_fields(fields, whitelist=WHITELIST)}
    assert v1 == {"Unsupported TeX command: \\mathscr"}
    assert v2 == {"Unsupported TeX command: \\lhd"}
    unsafe = EngineWhitelist.from_record(
        {**json.loads(FIXTURE.read_text()), "supported": {"input": ["bare"]}}
    )
    assert [
        d.code for d in validate_fields({"claim": r"$\input$"}, whitelist=unsafe)
    ] == ["unsafe_command"]


# --------------------------------------------------------------------------- parser


def test_parser_forms_arguments_defaults_and_lines():
    defs = {d.name: d for d in parse_macro_definitions(SRC_B_DEFS_TEXT, SRC_B_DEFS)}
    assert defs["pb"].nargs == 2 and defs["pb"].line == 7
    assert defs["ddt"].star and defs["ddt"].nargs == 1
    assert defs["deriv"].nargs == 2 and defs["deriv"].default == "x"
    assert defs["sgn"].template == r"\operatorname{sgn}"
    assert defs["esssup"].template == r"\operatorname*{ess\,sup}"
    assert defs["ihbar"].kind == "providecommand"
    assert not defs["|"].expandable
    assert "oldB" not in defs
    src_a = parse_macro_definitions(SRC_A_TEXT, SRC_A)
    assert {d.name: d.nargs for d in src_a}["tri"] == 3
    g2 = next(d for d in src_a if d.name == "g")
    assert not g2.expandable and g2.unsupported_reason == "delimited_parameter_text"


def test_nested_definition_bodies_are_not_parsed_as_definitions():
    defs = parse_macro_definitions(r"\newcommand{\outer}{\def\inner{x}}", "s")
    assert [d.name for d in defs] == ["outer"]


# --------------------------------------------------------------------------- layer 1


@pytest.mark.parametrize("character", ["\x00", "\x05", "\x07", "\x0b", "\x1d"])
def test_control_char_before_known_command_becomes_backslash(character):
    fields = step(derivation=f"\\[{character}omega+{character}mathbf k\\]")
    result = run(fields)
    assert result.fields["derivation"] == r"\[\omega+\mathbf k\]"
    assert kinds(result) == ["control_char_backslash", "control_char_backslash"]
    assert not result.unrepaired_control


def test_control_restoration_applies_to_source_field_without_macro_expansion():
    fields = step(source=f"quote: $\\om(\x0bmu)$ from {SRC_A}")
    result = run(fields)
    assert result.fields["source"] == f"quote: $\\om(\\mu)$ from {SRC_A}"
    assert kinds(result) == ["control_char_backslash"]


def test_control_char_before_source_macro_name_is_restored_then_expanded():
    result = run(step(derivation="$\x0bEzero$", scope=cite(SRC_A)))
    assert result.fields["derivation"] == r"$E_{0}$"
    assert kinds(result) == ["control_char_backslash", "macro_expansion"]
    assert result.replacements[1].source_id == SRC_A
    assert result.replacements[1].source_line == 3


def test_del_before_backslash_removed_other_del_reported():
    fields = step(why="the even-\x7f\\(n\\) terms", derivation="$a,\x7fzz'$")
    result = run(fields)
    assert result.fields["why"] == r"the even-\(n\) terms"
    assert kinds(result) == ["del_removed"]
    assert [u["reason"] for u in result.unrepaired_control] == [
        "del_not_before_backslash"
    ]


def test_control_char_before_intact_command_is_removed():
    result = run(step(claim="\\[\nE_n=\x00\\hbar\\omega(n+1/2).\n\\]"))
    assert result.fields["claim"] == "\\[\nE_n=\\hbar\\omega(n+1/2).\n\\]"
    assert kinds(result) == ["control_char_removed"]
    # A doubled backslash is not an intact command: the lost character is unknown.
    kept = run(step(derivation="$c_\x00\\\\nu$"))
    assert kept.replacements == ()
    assert kept.unrepaired_control[0]["reason"] == "followed_by_backslash"


def test_ansi_escape_sequence_is_removed():
    result = run(step(derivation="$x$\x1b[0m and \x1b[1;31m$y$"))
    assert result.fields["derivation"] == "$x$ and $y$"
    assert kinds(result) == ["ansi_escape_removed", "ansi_escape_removed"]


def test_unrepairable_controls_are_left_and_reported():
    fields = step(
        derivation="\\(\x05appa\\) and zero-\x00point and n_\x131 and \x7ftilde u"
    )
    result = run(fields)
    assert result.fields == fields
    assert [u["reason"] for u in result.unrepaired_control] == [
        "unknown_command_name",
        "unknown_command_name",
        "not_followed_by_letters",
        "del_not_before_backslash",
    ]


def test_maximal_letter_run_and_escaped_backslash_are_respected():
    assert run(step(derivation="$\x07omegat$")).replacements == ()
    escaped = run(step(derivation="$a\\\x0bmu$"))
    assert escaped.replacements == ()
    assert escaped.unrepaired_control[0]["reason"] == "preceded_by_backslash"


def test_json_backspace_that_may_have_swallowed_b_is_not_guessed():
    # "\beta" decoded as BS + "eta": restoring would silently produce \eta.
    result = run(step(derivation="$\x08eta + \x08mu$"))
    assert result.fields["derivation"] == "$\x08eta + \\mu$"
    assert result.unrepaired_control[0]["reason"] == "ambiguous_json_backspace"


# --------------------------------------------------------------------------- layer 2


def test_expansion_uses_cited_sources_only():
    result = run(step(derivation=r"$x(\rv,\om)$", scope=cite(SRC_A)))
    assert result.fields["derivation"] == r"$x(\mathbf{r},\omega)$"
    assert {(r.source_id, r.original) for r in result.replacements} == {
        (SRC_A, r"\rv"),
        (SRC_A, r"\om"),
    }
    assert result.cited_source_ids == (SRC_A,)


def test_ambiguous_when_cited_sources_disagree():
    result = run(step(derivation=r"$\rv$", scope=cite(SRC_A, SRC_B_DEFS)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "ambiguous"


def test_undefined_in_cited_even_if_defined_elsewhere():
    result = run(step(derivation=r"$\Bvec$", scope=cite(SRC_A)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "undefined_in_scope"
    # Each source_id is its own table: citing a main part does not pull in a
    # separately registered definitions part.
    main = run(step(derivation=r"$\Bvec$", scope=cite(SRC_B_MAIN)))
    assert main.replacements == ()


def test_document_grouping_brings_definitions_part_into_scope():
    documents = {SRC_B_DEFS: "doc_b", SRC_B_MAIN: "doc_b"}
    result = run(
        step(derivation=r"$\Bvec$", scope=cite(SRC_B_MAIN)), source_documents=documents
    )
    assert result.fields["derivation"] == r"$\mathbf{B}$"
    assert result.replacements[0].source_id == SRC_B_DEFS
    # Citing SRC_A and the main part of B: \rv means \mathbf{r} in one source
    # and \vec{r} in the other (through its definitions part), so it stays.
    ambiguous = run(
        step(derivation=r"$\rv$", scope=cite(SRC_A, SRC_B_MAIN)),
        source_documents=documents,
    )
    assert ambiguous.replacements == ()
    assert ambiguous.skipped_macros[0]["reason"] == "ambiguous"


def test_uncited_step_requires_one_consistent_definition_across_run():
    result = run(step(derivation=r"$\om+\rv+\zorp$"))
    assert result.fields["derivation"] == r"$\omega+\rv+\zorp$"
    assert [(s["command"], s["reason"]) for s in result.skipped_macros] == [
        ("rv", "ambiguous")
    ]


def test_argument_macros_braced_unbraced_and_token_boundaries():
    fields = step(
        derivation=r"$\sq{c_n}+\norm v\,t+\tri{a}{\mathbf b}{c}\conj z$",
        scope=cite(SRC_A),
    )
    result = run(fields)
    # \norm v and #2\times#3 need a space after the control word they end on.
    assert result.fields["derivation"] == (
        r"${c_n}^{2}+\Vert v\Vert\,t+a\cdot(\mathbf b\times c)z^{*}$"
    )


def test_expansion_followed_by_letter_gets_separator():
    sources = {"src_a": r"\def\om{\omega}"}
    result = run(step(derivation=r"$\om{}x \om t$"), sources=sources)
    assert result.fields["derivation"] == r"$\omega{}x \omega t$"


def test_optional_default_argument_and_operators():
    result = run(
        step(
            derivation=r"$\deriv{f}+\deriv[t]{g}+\sgn\phi+\ddt{y}$",
            scope=cite(SRC_B_DEFS),
        )
    )
    assert result.fields["derivation"] == (
        r"$\frac{df}{dx}+\frac{dg}{dt}+\operatorname{sgn}\phi+\frac{dy}{dt}$"
    )


def test_missing_arguments_not_expanded():
    result = run(step(derivation=r"$x\norm$", scope=cite(SRC_A)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "missing_arguments"


def test_nested_macros_resolve_in_defining_source():
    result = run(step(derivation=r"$\Hfree+\kk$", scope=cite(SRC_C)))
    assert result.fields["derivation"] == (
        r"$\frac{\mathbf{p}^2}{2m}+\mathbf{k}\cdot\mathbf{k}$"
    )


def test_recursive_and_depth_limited_definitions_are_refused():
    result = run(step(derivation=r"$\tick$", scope=cite(SRC_C)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "recursive_definition"
    chain = (
        "\n".join(rf"\def\m{chr(97 + i)}{{\m{chr(98 + i)}}}" for i in range(12))
        + "\n\\def\\mm{x}"
    )
    sources = {"src_chain": chain}
    shallow = run(step(derivation=r"$\ma$"), sources=sources, max_depth=5)
    assert shallow.replacements == ()
    assert shallow.skipped_macros[0]["reason"] == "depth_limit"
    deep = run(step(derivation=r"$\ma$"), sources=sources, max_depth=20)
    assert deep.fields["derivation"] == "$x$"


def test_engine_supported_standard_name_is_never_expanded():
    result = run(step(derivation=r"$\exp x$", scope=cite(SRC_C)))
    assert result.replacements == () and result.skipped_macros == ()


def test_standard_name_the_engine_cannot_compile_expands_only_when_unique():
    # \k is an ogonek accent the engine cannot typeset in math; the source's
    # meaning applies under a unique attribution.
    result = run(step(derivation=r"$\k+\Rp$", scope=cite(SRC_A)))
    assert result.fields["derivation"] == r"$\mathbf{k}+\mathrm{Re}$"
    # Identical definitions in two cited sources are one meaning.
    same = run(step(derivation=r"$\k$", scope=cite(SRC_A, SRC_C)))
    assert same.fields["derivation"] == r"$\mathbf{k}$"
    sources = {**SOURCES, "wavenumber_source": r"\def\k{k_0}"}
    ambiguous = run(
        step(derivation=r"$\k$", scope=cite(SRC_A, "wavenumber_source")),
        sources=sources,
    )
    assert ambiguous.replacements == ()
    assert ambiguous.skipped_macros[0]["reason"] == "ambiguous"


def test_expansion_introducing_unsupported_command_is_refused():
    result = run(step(derivation=r"$\sig_x$", scope=cite(SRC_C)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "expansion_uses_unsupported_command:bm"


def test_expansion_changing_math_structure_is_refused():
    sources = {"src_a": r"\def\dollar{a$b$}\def\unbal{\left(}"}
    result = run(step(derivation=r"$\dollar$"), sources=sources)
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "expansion_changes_math_structure"


def test_within_source_redefinition_is_ambiguous():
    result = run(step(derivation=r"$\Om{a}$", scope=cite(SRC_A)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"] == "ambiguous"


def test_delimited_definition_not_expanded():
    result = run(step(derivation=r"$\g2$", scope=cite(SRC_A)))
    assert result.replacements == ()
    assert result.skipped_macros[0]["reason"].startswith("unsupported_definition")


def test_math_delimiters_and_prose_are_untouched():
    fields = step(
        derivation=r"\[ a \] and \(\|b\|\) and \om in prose", scope=cite(SRC_B_DEFS)
    )
    assert run(fields).fields == fields


def test_escaped_backslash_is_not_a_macro():
    fields = step(derivation=r"$\begin{aligned}a\\om\end{aligned}$", scope=cite(SRC_A))
    assert run(fields).fields == fields


# --------------------------------------------------------------------------- quotations


QUOTED = r"\nabla^2 u(\rv,\om) + \frac{\om^2}{c^2}u(\rv,\om) = 0"


def test_prose_verbatim_quotation_of_cited_source_is_not_expanded():
    fields = step(scope=f"Source A reads ${QUOTED}$ and ours is $\\om$; {cite(SRC_A)}")
    result = run(fields)
    assert result.quotation_fragments == frozenset({("scope", 1)})
    assert [r.original for r in result.replacements] == [r"\om"]
    issues = formula_v2_issues(
        result.fields,
        whitelist=WHITELIST,
        quotation_fragments=result.quotation_fragments,
    )
    quoted = [i for i in issues if i["formula_index"] == 1 and i["field"] == "scope"]
    assert quoted and all(i["severity"] == "warning" for i in quoted)
    assert all(i["quotation"] == "prose_verbatim" for i in quoted)


def test_verbatim_fragment_in_exactly_one_uncited_source_uses_that_source():
    # The step cites nothing, \rv is ambiguous across the run, but the fragment
    # occurs verbatim only in SRC_A.
    result = run(step(derivation=f"${QUOTED}$"))
    assert result.quotation_fragments == frozenset()
    assert result.fields["derivation"] == (
        r"$\nabla^2 u(\mathbf{r},\omega) + \frac{\omega^2}{c^2}u(\mathbf{r},\omega) = 0$"
    )
    assert {r.source_id for r in result.replacements} == {SRC_A}


def test_short_fragments_are_not_treated_as_quotations():
    result = run(step(derivation=r"$\rv$", scope=cite(SRC_A)))
    assert result.quotation_fragments == frozenset()
    # An abbreviation of the cited author, written out by the author's own
    # definition: the same mathematics, so it is still expanded.
    assert result.fields["derivation"] == r"$\mathbf{r}$"


def test_a_short_formula_quoted_verbatim_is_not_expanded_either():
    """The 16-character rule governs severity, never ownership."""

    # This is not the author's abbreviation, it is a piece of the author's
    # formula - shorter than QUOTATION_MIN_CHARS, so not a quotation for
    # severity, but still text the host must not rewrite.
    short = r"(\rv,\om)"
    assert len(short) < QUOTATION_MIN_CHARS
    assert SRC_A in SOURCES and short in SOURCES[SRC_A]
    result = run(step(derivation=f"${short}$", scope=cite(SRC_A)))
    assert result.quotation_fragments == frozenset()
    assert result.replacements == ()
    assert result.fields["derivation"] == f"${short}$"
    assert [item["reason"] for item in result.skipped_macros] == [
        "short_verbatim_quotation_of_cited_source"
    ]
    # Not a quotation for severity: its diagnostics stay errors.
    issues = formula_v2_issues(
        result.fields,
        whitelist=WHITELIST,
        quotation_fragments=result.quotation_fragments,
    )
    unsupported = [item for item in issues if item["code"] == "unsupported_command"]
    assert unsupported and all(item["severity"] == "error" for item in unsupported)


def test_a_short_quotation_of_an_uncited_source_is_still_expanded():
    """Only the sources this step cites own its text."""

    result = run(step(derivation=r"$(\rv,\om)$", scope="no citation here"))
    # ``\om`` means the same in every source and is written out; ``\rv`` stays
    # because two sources disagree about it, which is the ordinary rule - the
    # quotation protection never enters into it.
    assert result.fields["derivation"] == r"$(\rv,\omega)$"
    assert [item["reason"] for item in result.skipped_macros] == ["ambiguous"]


# --------------------------------------------------------------------------- severity


def test_v2_severity_rules():
    fields = step(
        source=r"quote $\unknownmacro$ and $a\x0cb$".replace("\\x0c", "\x0c"),
        derivation=r"$\unknownmacro + \input$",
        claim="$\\[\n\rho$",
    )
    issues = formula_v2_issues(fields, whitelist=WHITELIST)
    by = {(i["field"], i["code"]): i["severity"] for i in issues}
    assert by[("source", "unsupported_command")] == "warning"
    assert by[("source", "control_character")] == "error"
    assert by[("derivation", "unsupported_command")] == "error"
    assert by[("derivation", "unsafe_command")] == "error"
    assert by[("claim", "possible_escape_swallow")] == "warning"


@pytest.mark.parametrize(
    "value,command",
    [
        ("$\tau$", "tau"),
        ("$\theta$", "theta"),
        ("$\\left(x\right)$", "right"),
        ("$x-\nu$", "nu"),
        ("$x \tag{1}$", "tag"),
    ],
)
def test_possible_escape_swallow_warning(value, command):
    whitelist = EngineWhitelist.from_record(
        {
            **json.loads(FIXTURE.read_text()),
            "supported": {
                **json.loads(FIXTURE.read_text())["supported"],
                "tag": ["bare"],
            },
        }
    )
    found = escape_swallow_diagnostics({"derivation": value}, whitelist=whitelist)
    assert [d.code for d in found] == ["possible_escape_swallow"]
    assert command in found[0].message


def test_escape_swallow_ignores_prose_and_layout_newlines():
    fields = {
        "derivation": "Line one\nrho is prose.\n$$\na+b\n$$ \\[\ni\\gamma\\] "
        "\\[a \\\\\nu_k\\] $x+ \nu$"
    }
    assert escape_swallow_diagnostics(fields, whitelist=WHITELIST) == []
    real = {"derivation": "\\(x=\\omega^2-\nu^2\\)"}
    assert [d.code for d in escape_swallow_diagnostics(real, whitelist=WHITELIST)] == [
        "possible_escape_swallow"
    ]


# --------------------------------------------------------------------------- control-character shapes
# Each string carries one typical damage shape (which control character stood
# in for which backslash, and where) around synthetic mathematics.


CONTROL_SHAPES = [
    (  # ENQ for two backslashes in one fraction
        "\\int_0^{\\infty}e^{-ax^2}dx=\\frac{\u0005sqrt{\u0005pi}}{2\\sqrt{a}}",
        "\\int_0^{\\infty}e^{-ax^2}dx=\\frac{\\sqrt{\\pi}}{2\\sqrt{a}}",
        ["control_char_backslash"] * 2,
    ),
    (  # BEL for the backslash of an argument after a bold vector
        "V(\\mathbf r,\u0007lambda)=\\frac{1}{2}\\lambda\\,|\\mathbf r|^2",
        "V(\\mathbf r,\\lambda)=\\frac{1}{2}\\lambda\\,|\\mathbf r|^2",
        ["control_char_backslash"],
    ),
    (  # NUL for the backslash of a spacing command
        "a_n=\\frac{1}{\\pi}\\int_{-\\pi}^{\\pi}f(x)\\cos nx\\,dx,\u0000qquad n\\ge 0",
        "a_n=\\frac{1}{\\pi}\\int_{-\\pi}^{\\pi}f(x)\\cos nx\\,dx,\\qquad n\\ge 0",
        ["control_char_backslash"],
    ),
    (  # DEL before an inline-math opener in prose
        "the series converges for every \u007f\\(x\\in(-\\pi,\\pi)\\) in the interval",
        "the series converges for every \\(x\\in(-\\pi,\\pi)\\) in the interval",
        ["del_removed"],
    ),
]


@pytest.mark.parametrize("raw,expected,expected_kinds", CONTROL_SHAPES)
def test_control_shape_strings(raw, expected, expected_kinds):
    fields = step(derivation=raw)
    assert "control_character" in {d.code for d in validate_fields(fields)}
    result = run(fields)
    assert result.fields["derivation"] == expected
    assert kinds(result) == expected_kinds
    assert "control_character" not in {
        d.code for d in validate_fields(result.fields, whitelist=WHITELIST)
    }


def test_control_shape_undefined_macro_restores_controls_and_leaves_undefined_name():
    raw = (
        "\\[V(x)=\\frac{1}{2}m\u000bomega^2x^2+\u0007lambda\\zorp x^4,"
        "\u000bquad x>0\\]"
    )
    result = run(step(claim=raw, scope=cite(SRC_A)))
    assert result.fields["claim"] == (
        "\\[V(x)=\\frac{1}{2}m\\omega^2x^2+\\lambda\\zorp x^4,\\quad x>0\\]"
    )
    assert kinds(result) == ["control_char_backslash"] * 3
    errors = [
        i
        for i in formula_v2_issues(result.fields, whitelist=WHITELIST)
        if i["severity"] == "error"
    ]
    assert [i["excerpt"] for i in errors] == ["\\\\zorp"]


def test_control_shape_unrepaired_cases():
    fields = step(
        derivation="c_{n,\u007fm}\\,\\delta_{nm}",
        why="spring constant; \\(\u0005appa\\): stiffness",
    )
    result = run(fields)
    assert result.fields == fields
    assert sorted(u["reason"] for u in result.unrepaired_control) == [
        "del_not_before_backslash",
        "unknown_command_name",
    ]


def test_multi_field_record_offsets_reverse_exactly():
    fields = step(
        claim="$\x0blambda+\\om$",
        derivation="$\x07omega\\rv$ and \x7f\\(\\Ezero\\)",
        source="\x00frac{1}{2}",
        scope=cite(SRC_A),
    )
    result = run(fields)
    assert result.fields["claim"] == r"$\lambda+\omega$"
    assert result.fields["derivation"] == r"$\omega\mathbf{r}$ and \(E_{0}\)"
    assert result.fields["source"] == r"\frac{1}{2}"
    layer_one = [r.kind for r in result.replacements if r.kind != "macro_expansion"]
    assert layer_one == [
        "control_char_backslash",  # claim
        "control_char_backslash",  # source
        "del_removed",  # derivation, descending offsets
        "control_char_backslash",
    ]


def test_tables_can_be_shared_between_calls():
    tables = SourceMacroTables(SOURCES)
    first = normalize_step_fields(
        step(derivation=r"$\om$"), sources=SOURCES, whitelist=WHITELIST, tables=tables
    )
    assert first.fields["derivation"] == r"$\omega$"
    layer_one_only = normalize_step_fields(
        step(derivation="$\x05omega \\om$"),
        sources=SOURCES,
        whitelist=WHITELIST,
        tables=tables,
        expand_macros=False,
    )
    assert layer_one_only.fields["derivation"] == r"$\omega \om$"
