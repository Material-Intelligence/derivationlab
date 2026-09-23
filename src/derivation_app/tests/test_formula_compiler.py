from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from derivation_app.formula_compiler import ProductFormulaValidator
from derivation_app.reporting import CompileResult, TectonicRunner, TectonicRuntimeSpec
from derivation_app.service import _run_config_dict, _run_config_from_dict
from derivation_runtime.selftest import _config
from derivation_runtime.types import StepContent


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


def content(derivation=r"$x=1$", **updates):
    return replace(
        StepContent("Claim", "Reason", "Source", derivation, "Scope"), **updates
    )


class Compiler:
    runtime = SimpleNamespace(
        version="test", target="test", binary_sha256="a", bundle_sha256="b"
    )

    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def compile(self, tex, *, workspace_parent):
        self.calls.append(tex)
        if self.failure == "syntax":
            line = tex.splitlines().index("x=1") + 1
            return CompileResult(
                "failed",
                f"error: report.tex:{line}: Missing {{ inserted.\n",
                None,
                "test",
                "tectonic_compile_failed",
            )
        if self.failure:
            return CompileResult("failed", "runtime failed", None, "test", self.failure)
        return CompileResult("success", "compiled", b"%PDF-", "test")


def test_bad_control_character_never_reaches_compiler(tmp_path):
    runner = Compiler()
    result = ProductFormulaValidator(runner=runner, evidence_root=tmp_path)(
        content("$x\x00$")
    )
    assert result.issues[0]["code"] == "control_character"
    assert result.issues[0]["severity"] == "error"
    assert not runner.calls


def test_source_macro_is_preserved_as_warning_not_expanded(tmp_path):
    runner = Compiler()
    step = content(source=r"The paper quotes $\zorp=\blip$.")
    result = ProductFormulaValidator(runner=runner, evidence_root=tmp_path)(step)
    assert result.issues and all(i["severity"] == "warning" for i in result.issues)
    assert not result.infrastructure_error
    assert step.source == r"The paper quotes $\zorp=\blip$."
    assert r"\zorp" not in runner.calls[0]


def test_engine_error_maps_to_original_formula(tmp_path):
    result = ProductFormulaValidator(runner=Compiler("syntax"), evidence_root=tmp_path)(
        content()
    )
    assert result.infrastructure_error is None
    assert result.issues[-1]["field"] == "derivation"
    assert result.issues[-1]["formula_index"] == 1
    assert result.issues[-1]["code"] == "syntax_error"
    assert list(tmp_path.glob("*/attempt-*/compile-*.log"))


def test_environment_error_is_not_returned_as_writer_repair(tmp_path):
    for failure in (
        "tectonic_timeout",
        "tectonic_runtime_unavailable",
        "tectonic_compile_failed",
    ):
        result = ProductFormulaValidator(
            runner=Compiler(failure), evidence_root=tmp_path / failure
        )(content())
        assert result.infrastructure_error
        assert not result.issues


def test_manifest_policy_roundtrip_and_legacy_default():
    config = replace(
        _config(max_active_branches=1, max_model_calls=4, retries=1),
        formula_validation_policy="formula-v1",
    )
    serialized = _run_config_dict(config)
    assert _run_config_from_dict(serialized) == config
    serialized.pop("formula_validation_policy")
    assert _run_config_from_dict(serialized).formula_validation_policy is None


def test_real_locked_engine_accepts_all_five_previously_rejected_commands(tmp_path):
    runner = real_runner()
    result = ProductFormulaValidator(runner=runner, evidence_root=tmp_path)(
        content(r"$\boxed{x}$; $x+\cdots+y$; ${\bf q}$; ${\rm d}x$; ${\cal H}$")
    )
    assert not result.infrastructure_error
    assert not result.issues


def test_real_engine_syntax_error_feedback_and_source_warning(tmp_path):
    gate = ProductFormulaValidator(runner=real_runner(), evidence_root=tmp_path)
    result = gate(content(r"$x^$"))
    assert result.infrastructure_error is None
    assert result.issues[-1]["severity"] == "error"
    assert result.issues[-1]["field"] == "derivation"
    source = content(source=r"The exact quotation is $x_ $. No silent correction.")
    result = gate(source)
    assert result.infrastructure_error is None
    assert result.issues[-1]["severity"] == "warning"
    assert result.issues[-1]["field"] == "source"
    assert source.source == r"The exact quotation is $x_ $. No silent correction."


def test_rechecks_append_compiler_evidence(tmp_path):
    gate = ProductFormulaValidator(runner=Compiler(), evidence_root=tmp_path)
    assert not gate(content()).infrastructure_error
    assert not gate(content()).infrastructure_error
    assert len(list(tmp_path.glob("*/attempt-*/compile-001.log"))) == 2


def test_real_invalid_array_column_is_format_error_not_infrastructure(tmp_path):
    gate = ProductFormulaValidator(runner=real_runner(), evidence_root=tmp_path)
    result = gate(content(r"$\begin{array}{Q}x\end{array}$"))
    assert result.infrastructure_error is None
    assert result.issues[-1]["code"] == "syntax_error"
    assert "Illegal character in array arg" in result.issues[-1]["message"]
