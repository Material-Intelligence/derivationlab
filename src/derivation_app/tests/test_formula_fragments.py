"""Document layout, log locator and whole-list fragment compilation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from derivation_app.formula_compiler import (
    FORMULA_DOCUMENT_LAYOUT,
    ProductFormulaValidator,
    compile_fragments,
    formula_document,
)
from derivation_app.reporting import (
    CompileResult,
    TectonicRunner,
    TectonicRuntimeSpec,
    compiler_formula_error,
    compiler_missing_glyphs,
)
from derivation_runtime.types import StepContent

GLYPH = (
    "warning: report.tex:{line}: Missing character: There is no \ufffd\ufffd "
    "(U+1D706) in font [LibertinusSerif-Regular.otf]/OT:script=latn;language=dflt;!"
)


class RuleCompiler:
    """Fake TectonicRunner applying per-fragment rules to the real layout.

    ``BAD`` halts with a located TeX error, ``GLYPH`` emits a located
    missing-glyph warning at the fragment's closing ``\\]``, a VT control
    character halts with XeTeX's invalid-character error, ``HIDDEN`` halts
    without any line, ``INFRA`` is a runtime failure.
    """

    runtime = SimpleNamespace(
        version="test", target="test", binary_sha256="a", bundle_sha256="b"
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def regions(tex: str) -> list[tuple[int, list[tuple[int, str]]]]:
        lines = tex.split("\n")
        found = []
        i = 0
        while i < len(lines):
            if lines[i] == r"\[" and lines[i + 1] == "{":
                body = []
                j = i + 2
                while not (lines[j] == "}" and lines[j + 1] == r"\]"):
                    body.append((j + 1, lines[j]))
                    j += 1
                found.append((j + 2, body))  # (closing \] line, body lines)
                i = j + 2
            else:
                i += 1
        return found

    def compile(self, tex: str, *, workspace_parent: Path) -> CompileResult:
        assert workspace_parent.is_dir()
        self.calls.append(tex)
        log: list[str] = []
        for closing, body in self.regions(tex):
            for number, text in body:
                if "INFRA" in text:
                    return CompileResult(
                        "failed", "boom", None, "test", "tectonic_timeout"
                    )
                if "HIDDEN" in text:
                    return CompileResult(
                        "failed",
                        "\n".join([*log, "! Emergency stop."]),
                        None,
                        "test",
                        "tectonic_compile_failed",
                    )
                if "\x0b" in text:
                    log.append(
                        f"error: report.tex:{number}: Text line contains an invalid character"
                    )
                    return CompileResult(
                        "failed", "\n".join(log), None, "test", "tectonic_compile_failed"
                    )
                if "BAD" in text:
                    log.append(f"error: report.tex:{number}: Undefined control sequence")
                    return CompileResult(
                        "failed", "\n".join(log), None, "test", "tectonic_compile_failed"
                    )
            if any("GLYPH" in text for _, text in body):
                log.append(GLYPH.format(line=closing))
                log.append(GLYPH.format(line=closing))  # engine repeats warnings
        if log:
            return CompileResult(
                "failed", "\n".join(log), None, "test", "tectonic_missing_glyphs"
            )
        return CompileResult("success", "ok", b"%PDF-", "test")


def real_runner() -> TectonicRunner:
    root = Path(__file__).resolve().parents[3]
    try:
        runner = TectonicRunner(TectonicRuntimeSpec.from_lock(root))
    except (OSError, ValueError, KeyError) as exc:
        pytest.skip(f"locked Tectonic runtime unavailable: {exc}")
    error = runner._artifact_error()
    if error is not None:
        pytest.skip(f"locked Tectonic runtime unavailable: {error}")
    return runner


def test_document_braces_each_fragment_and_keeps_exact_line_map():
    values = ["x=1", "a\\k", "p\x0bq\nr", "  \\alpha \r\n\\beta  ", "\u00a0\x0bx\u2028"]
    tex, regions = formula_document(values)
    lines = tex.split("\n")
    for _value, (first, last) in zip(values, regions, strict=True):
        assert lines[first - 1] == r"\["
        assert lines[first] == "{"
        assert lines[last - 2] == "}"
        assert lines[last - 1] == r"\]"
    # Only LF/CR end TeX lines: VT stays inside its line, CRLF becomes LF.
    assert lines[regions[2][0] + 1 : regions[2][1] - 2] == ["p\x0bq", "r"]
    assert lines[regions[3][0] + 1 : regions[3][1] - 2] == ["\\alpha ", "\\beta"]
    assert lines[regions[1][0] + 1] == "a\\k"
    # Ordinary Unicode spaces are trimmed as before; control characters stay.
    assert lines[regions[4][0] + 1 : regions[4][1] - 2] == ["\x0bx"]


def test_locator_recognizes_invalid_character_and_missing_glyphs():
    assert compiler_formula_error(
        "error: report.tex:19: Text line contains an invalid character\n"
    ) == (19, "Text line contains an invalid character")
    assert compiler_formula_error(
        "! Text line contains an invalid character.\nl.19 a^^E\n"
    ) == (19, "Text line contains an invalid character.")
    log = "\n".join(
        [
            GLYPH.format(line=21),
            (
                "warning: report.tex:31: Missing character: There is no \ufffd "
                '("105) in font cmmi10!'
            ),
            GLYPH.format(line=21),
            "warning: Missing character: U+4E2D",
        ]
    )
    assert compiler_missing_glyphs(log) == [
        (21, "Missing character U+1D706 in font LibertinusSerif-Regular.otf"),
        (31, "Missing character U+0105 in font cmmi10"),
    ]
    # A glyph warning is never mistaken for a halting TeX error.
    assert compiler_formula_error(log) is None


def test_compile_fragments_drops_error_and_glyphs_together_then_recompiles(tmp_path):
    runner = RuleCompiler()
    fragments = ["a", "GLYPH x", "b", "BAD", "GLYPH y", "c"]
    result = compile_fragments(fragments, runner, tmp_path / "evidence")
    # First log: glyph for fragment 1 (typeset before) + halting error at 3.
    assert [(f.index, f.kind) for f in result.failures] == [
        (1, "missing_glyph"),
        (3, "tex_error"),
        (4, "missing_glyph"),
    ]
    assert result.accepted == (0, 2, 5)
    assert not result.passed and result.infrastructure_error is None
    assert [c.fragments for c in result.compiles] == [
        (0, 1, 2, 3, 4, 5),
        (0, 2, 4, 5),
        (0, 2, 5),
    ]
    assert [f.compile_number for f in result.failures] == [1, 1, 2]
    assert len(result.durations) == 3
    bad = result.failures[1]
    tex = runner.calls[0].split("\n")
    assert tex[bad.line - 1] == "BAD"
    evidence = tmp_path / "evidence"
    for number in (1, 2, 3):
        for suffix in (".tex", ".map.json", ".log", ".compiler.json"):
            assert (evidence / f"compile-{number:03d}{suffix}").is_file()
    mapping = json.loads((evidence / "compile-002.map.json").read_text())
    assert mapping["layout"] == FORMULA_DOCUMENT_LAYOUT
    assert [r["index"] for r in mapping["regions"]] == [0, 2, 4, 5]
    compiler = json.loads((evidence / "compile-003.compiler.json").read_text())
    assert compiler["status"] == "success" and compiler["binary_sha256"] == "a"
    json.dumps(result.to_dict())
    with pytest.raises(FileExistsError):
        compile_fragments(fragments, runner, evidence)


def test_compile_fragments_invalid_character_is_a_formula_failure(tmp_path):
    result = compile_fragments(["a", "\x0bomega", "b"], RuleCompiler(), tmp_path)
    assert [(f.index, f.kind) for f in result.failures] == [(1, "tex_error")]
    assert "invalid character" in result.failures[0].message
    assert result.accepted == (0, 2)


def test_compile_fragments_bisects_an_unlocated_failure(tmp_path):
    fragments = ["a", "b", "c", "HIDDEN", "d", "GLYPH"]
    result = compile_fragments(fragments, RuleCompiler(), tmp_path)
    assert result.infrastructure_error is None
    assert {(f.index, f.kind) for f in result.failures} == {
        (3, "unattributed"),
        (5, "missing_glyph"),
    }
    assert "bisection" in {c.purpose for c in result.compiles}
    assert result.accepted == (0, 1, 2, 4)


def test_compile_fragments_infrastructure_and_unsafe(tmp_path):
    runner = RuleCompiler()
    result = compile_fragments(
        [r"\input{secret}", r"x \end{document}", "y\\stop", "INFRA"],
        runner,
        tmp_path / "one",
    )
    assert [f.kind for f in result.failures] == ["unsafe_not_compiled"] * 3
    assert result.infrastructure_error and "tectonic_timeout" in result.infrastructure_error
    assert not result.accepted
    assert all(r"\input" not in tex and "stop" not in tex for tex in runner.calls)
    empty = compile_fragments([], runner, tmp_path / "two")
    assert empty.passed and not empty.compiles


def test_validator_maps_glyph_and_invalid_character_instead_of_infrastructure(tmp_path):
    gate = ProductFormulaValidator(runner=RuleCompiler(), evidence_root=tmp_path)
    step = StepContent("Claim", "Reason", "Quoted $GLYPH q$", "$x=1$ and $GLYPH$", "S")
    result = gate(step)
    assert result.infrastructure_error is None
    by_field = {(i["field"], i["severity"], i["code"]) for i in result.issues}
    assert ("derivation", "error", "missing_glyph") in by_field
    # The quotation's glyph is warned in the same pass as the derivation error.
    assert ("source", "warning", "missing_glyph") in by_field
    source_only = replace(step, derivation="$x=1$")
    result = ProductFormulaValidator(
        runner=RuleCompiler(), evidence_root=tmp_path / "source"
    )(source_only)
    assert result.infrastructure_error is None
    assert [(i["field"], i["severity"]) for i in result.issues] == [
        ("source", "warning")
    ]


def test_real_engine_trailing_argument_command_no_longer_swallows_delimiter(tmp_path):
    runner = real_runner()
    result = compile_fragments(
        [r"\alpha", r"\k", r"{\rm \lambda}", "a\x05omega", r"\frac{a}{b}"],
        runner,
        tmp_path / "evidence",
    )
    assert result.infrastructure_error is None
    assert {(f.index, f.kind) for f in result.failures} == {
        (1, "tex_error"),
        (2, "missing_glyph"),
        (3, "tex_error"),
    }
    assert result.accepted == (0, 4)
