"""Generator logic with a fake compiler (no Tectonic runtime needed)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate as gen

from derivation_app.reporting import CompileResult


class FakeEngine:
    """Knows a handful of commands; mimics XeTeX's located error lines."""

    runtime = SimpleNamespace(
        version="0.17.0", target="test-arch", binary_sha256="b" * 64, bundle_sha256="c" * 64
    )
    defined: ClassVar[set[str]] = {
        "alpha", "frac", "left", "right", "sum", "begin", "end", "hat", ",", "|", "\\",
    }
    environments: ClassVar[set[str]] = {"matrix", "array"}

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def ok(self, body: str) -> str | None:
        for name in re.findall(r"\\([A-Za-z]+|.)", body):
            if name not in self.defined:
                return "Undefined control sequence"
        for env in re.findall(r"\\begin\{([^}]*)\}", body):
            if env not in self.environments:
                return "Environment undefined"
        if "\\hat" in body and not re.search(r"\\hat\{x\}", body):
            return "Missing { inserted"
        if "\\left" in body and "\\right" not in body:
            return "Missing \\right. inserted"
        if re.search(r"\\right\)", body) and "\\left" not in body:
            return "Extra \\right"
        if "\\begin{array}" in body and "{cc}" not in body:
            return "Illegal character in array arg"
        return None

    def compile(self, tex: str, *, workspace_parent: Path) -> CompileResult:
        lines = tex.split("\n")
        for number, line in enumerate(lines, 1):
            if number > 1 and lines[number - 2] == "{" and lines[number - 3] == r"\[":
                self.bodies.append(line)
                message = self.ok(line)
                if message:
                    return CompileResult(
                        "failed",
                        f"error: report.tex:{number}: {message}\n",
                        None,
                        "0.17.0",
                        "tectonic_compile_failed",
                    )
        return CompileResult("success", "", b"%PDF", "0.17.0")


def build(tmp_path, commands, environments=("matrix", "array", "nope"), group_size=3):
    engine = FakeEngine()
    prober = gen.Prober(engine, tmp_path, workers=2, group_size=group_size)
    candidates = {
        "schema_version": gen.CANDIDATES_SCHEMA_VERSION,
        "commands": commands,
        "environments": list(environments),
        "unsafe": ["input", "stop"],
    }
    spec = SimpleNamespace(**vars(engine.runtime))
    output, report = gen.build_whitelist(
        candidates=candidates, spec=spec, lock_bytes=b"lock", prober=prober
    )
    return output, report, engine


def test_templates_and_classification():
    assert gen.render(gen.BASIC_TEMPLATES["arg2"], "frac") == r"\frac{x}{y}"
    assert gen.render(gen.EXTRA_TEMPLATES["left_pair"], "left") == r"\left( x \right)"
    assert gen.render(gen.ENV_TEMPLATES["env_cols"], "array") == (
        r"\begin{array}{cc} x & y \\ z & w \end{array}"
    )
    assert gen.is_control_symbol(",") and gen.is_control_symbol("\\")
    assert not gen.is_control_symbol("k") and not gen.is_control_symbol("alpha")
    unsafe = gen.unsafe_names(["alpha", "pdfoutput", "XeTeXinterchartoks", "input"], ["stop"])
    assert {"pdfoutput", "XeTeXinterchartoks", "input", "stop", "write"} <= unsafe
    assert "alpha" not in unsafe


def test_build_whitelist_schema_templates_and_unsafe_exclusion(tmp_path):
    output, report, engine = build(
        tmp_path,
        ["alpha", "frac", "left", "right", "hat", "qq", "input", "pdfliteral", ",", "|", "("],
    )
    assert list(output) == [
        "schema_version",
        "engine",
        "preamble_sha256",
        "supported",
        "control_symbols",
        "environments",
        "unsafe_excluded",
    ]
    assert output["schema_version"] == "formula-engine-whitelist-v1"
    assert output["engine"] == {
        "version": "0.17.0",
        "target": "test-arch",
        "binary_sha256": "b" * 64,
        "bundle_sha256": "c" * 64,
        "lock_sha256": gen.sha256_bytes(b"lock"),
    }
    assert output["preamble_sha256"] == gen.preamble_sha256()
    supported = output["supported"]
    assert supported["alpha"] == ["bare", "arg1", "arg2", "space_arg", "sub"]
    assert supported["hat"] == ["arg1", "arg2"]
    assert supported["left"] == ["left_pair", "middle_pair"]  # extra-only
    assert "qq" not in supported and report["unsupported"]["qq"] == "undefined_control_sequence"
    assert report["unsupported"]["("] == "undefined_control_sequence"
    assert output["control_symbols"] == [",", "|"]
    assert output["environments"] == ["array", "matrix"]
    assert {"input", "pdfliteral", "stop"} <= set(output["unsafe_excluded"])
    assert not set(supported) & set(output["unsafe_excluded"])
    # Unsafe names never reach the compiler; undefined names skip extras.
    assert not any("input" in body or "pdfliteral" in body for body in engine.bodies)
    assert not any("\\qq(" in body for body in engine.bodies)


def test_generation_is_deterministic_across_grouping(tmp_path):
    commands = ["alpha", "frac", "left", "right", "hat", "qq", ","]
    first, _, _ = build(tmp_path / "a", commands, group_size=2)
    second, _, _ = build(tmp_path / "b", list(reversed(commands)), group_size=7)
    assert gen.serialize(first) == gen.serialize(second)


def test_committed_candidates_are_valid(tmp_path):
    candidates = gen.load_candidates()
    assert len(candidates["commands"]) == len(set(candidates["commands"]))
    assert not set(candidates["commands"]) & set(candidates["unsafe"])
    assert {"input", "write", "stop", "endinput"} <= set(candidates["unsafe"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**candidates, "schema_version": "x"}))
    with pytest.raises(ValueError):
        gen.load_candidates(bad)


def test_infrastructure_failure_is_raised_after_retries(tmp_path):
    class Down(FakeEngine):
        def compile(self, tex, *, workspace_parent):
            return CompileResult("failed", "gone", None, None, "tectonic_runtime_unavailable")

    prober = gen.Prober(Down(), tmp_path, workers=1, group_size=4, retries=1)
    with pytest.raises(gen.InfrastructureError):
        prober.compile(["x"])
