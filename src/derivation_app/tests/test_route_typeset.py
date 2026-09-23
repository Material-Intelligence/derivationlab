"""Typeset layer of a finished route: quotations, repair, guard, review, cap."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from derivation_agent_record import canonical_json, sha256_text
from derivation_app.reporting import CompileResult
from derivation_app.route_typeset import (
    LAYER_STATUS_COMPILED,
    LAYER_STATUS_INFRASTRUCTURE,
    LAYER_STATUS_WITH_FAILURES,
    MAX_REPAIR_ROUNDS,
    STATUS_FAILED,
    STATUS_NOT_COMPILED,
    STATUS_OK,
    STATUS_QUOTATION_EXPANDED,
    STATUS_QUOTATION_VERBATIM,
    STATUS_REPAIRED,
    STATUS_REPAIRED_REVIEWED,
    TypesetCallBudget,
    error_commands,
    syntax_only_guard,
    typeset_field_text,
    typeset_lookup,
    typeset_route,
    verified_layers,
    verify_typeset_layer,
    write_typeset_layer,
)
from derivation_runtime.formula_validation import EngineWhitelist, load_engine_whitelist
from derivation_runtime.types import (
    ArtifactRef,
    ContentRef,
    FormulaEquivalenceOutput,
    FormulaRepairOutput,
    InputPolicy,
    ModelSpec,
    RunConfig,
    Usage,
)

WHITELIST = load_engine_whitelist()
UNDEFINED = "Undefined control sequence"


class RuleRunner:
    """Fake TectonicRunner: a fragment fails while it matches one of ``rules``.

    The document layout and the line map are the production ones, so the log
    lines this returns are located exactly as the locked engine's would be.
    """

    runtime = SimpleNamespace(
        version="fake-engine",
        target="test",
        binary_sha256="a" * 64,
        bundle_sha256="b" * 64,
    )

    def __init__(
        self,
        rules: Sequence[str] = (),
        *,
        message: str = UNDEFINED,
        infrastructure: bool = False,
    ) -> None:
        self.rules = list(rules)
        self.message = message
        self.infrastructure = infrastructure
        self.documents: list[str] = []

    @staticmethod
    def _bodies(tex: str) -> list[tuple[int, str]]:
        lines = tex.split("\n")
        found: list[tuple[int, str]] = []
        index = 0
        while index < len(lines) - 1:
            if lines[index] == r"\[" and lines[index + 1] == "{":
                cursor = index + 2
                while not (lines[cursor] == "}" and lines[cursor + 1] == r"\]"):
                    found.append((cursor + 1, lines[cursor]))
                    cursor += 1
                index = cursor + 2
                continue
            index += 1
        return found

    def compile(self, tex: str, *, workspace_parent: Path) -> CompileResult:
        assert workspace_parent.is_dir()
        self.documents.append(tex)
        if self.infrastructure:
            return CompileResult("failed", "boom", None, "fake", "tectonic_timeout")
        for number, text in self._bodies(tex):
            if any(rule in text for rule in self.rules):
                log = (
                    f"error: report.tex:{number}: {self.message}\n"
                    f"! {self.message}.\n"
                    f"l.{number} {text}\n"
                )
                return CompileResult(
                    "failed", log, None, "fake", "tectonic_compile_failed"
                )
        return CompileResult("success", "ok", b"%PDF-", "fake")


class ScriptedTypesetRuntime:
    """Runtime that answers typeset calls from a script, and counts them."""

    def __init__(self, repairs=(), reviews=(), *, repair_error: Exception | None = None):
        self.repairs = list(repairs)
        self.reviews = list(reviews)
        self.repair_error = repair_error
        self.repair_requests: list[object] = []
        self.review_requests: list[object] = []
        self._counter = 0

    def _invocation(self, role: str):
        from derivation_runtime.types import (
            ModelRole,
            ProviderLineage,
            RuntimeInvocation,
            RuntimeSession,
        )

        self._counter += 1
        return RuntimeInvocation(
            session=RuntimeSession(f"thread-{self._counter}", ProviderLineage.NATIVE),
            operation_id=f"turn-{self._counter}",
            role=ModelRole(role),
        )

    async def start_formula_repair(self, request):
        if self.repair_error is not None:
            raise self.repair_error
        self.repair_requests.append(request)
        return self._invocation("writer")

    async def collect_formula_repair(self, _invocation):
        answer = self.repairs.pop(0)
        return FormulaRepairOutput(
            corrections=tuple(answer(self.repair_requests[-1])),
            finish_reason="stop",
            usage=Usage({"total_tokens": 1}),
            raw_output=json.dumps({"corrections": "fake"}),
        )

    async def start_formula_review(self, request):
        self.review_requests.append(request)
        return self._invocation("checker")

    async def collect_formula_review(self, _invocation):
        answer = self.reviews.pop(0)
        return FormulaEquivalenceOutput(
            verdicts=tuple(answer(self.review_requests[-1])),
            finish_reason="stop",
            usage=Usage({"total_tokens": 1}),
            raw_output=json.dumps({"reviews": "fake"}),
        )


def config(**overrides) -> RunConfig:
    artifact = ArtifactRef("fixture", "a" * 64)
    values: dict = {
        "run_id": "run-typeset",
        "task": ContentRef("task", "b" * 64),
        "pack": ContentRef("pack", "c" * 64),
        "code_commit": "d" * 40,
        "granularity": "one_task",
        "max_active_branches": 2,
        "max_model_calls": 40,
        "concurrency": 1,
        "retries": 1,
        "writer": ModelSpec("openai", "writer-model", "high"),
        "checker": ModelSpec("openai", "checker-model", "medium"),
        "judge": ModelSpec("openai", "judge-model", "high"),
        "backend_name": "codex-app-server",
        "backend_version": "0.147.0",
        "record_spec": artifact,
        "event_schema": artifact,
        "canonical_schema": artifact,
        "input_policy": InputPolicy(False, ()),
        "credential_profile_id": "test-profile",
        "record_version": "1.1",
        "formula_validation_policy": "formula-v2",
    }
    values.update(overrides)
    return RunConfig(**values)


def step(
    revision_id: str,
    *,
    derivation: str = "Plain derivation text.",
    source: str = "Plain source text.",
) -> dict:
    content = {
        "claim": "Claim text.",
        "why": "Why text.",
        "source": source,
        "derivation": derivation,
        "scope": "Scope text.",
    }
    return {
        "step_revision_id": revision_id,
        "output_sha256": sha256_text(canonical_json(content)),
        "content": content,
    }


def build(
    tmp_path: Path,
    steps,
    *,
    runner: RuleRunner,
    runtime=None,
    sources=None,
    documents=None,
    run_config: RunConfig | None = None,
    whitelist: EngineWhitelist = WHITELIST,
    format_audits=None,
) -> dict:
    run_config = run_config or config()
    budget = TypesetCallBudget(
        journal_path=tmp_path / "typeset" / "model_calls.jsonl",
        max_model_calls=run_config.max_model_calls,
        record_model_calls=2,
    )
    return asyncio.run(
        typeset_route(
            run_directory=tmp_path,
            config=run_config,
            route_id="route_br_0001",
            branch_id="br_0001",
            steps=steps,
            sources=sources or {},
            documents=documents or {},
            whitelist=whitelist,
            runner=runner,
            runtime=runtime,
            budget=budget,
            format_audits=format_audits,
        )
    )


def entries(layer: dict) -> dict:
    return {item["formula_id"]: item for item in layer["formulas"]}


# ---------------------------------------------------------------------------
# Guard


def test_guard_accepts_grouping_and_delimiter_repairs():
    guard = syntax_only_guard(r"x^2^3", r"{x^2}^3", allowed_commands=())
    assert guard["within_guard"]
    spaced = syntax_only_guard(
        r"\int f(x) dx", r"\int f(x)\, \mathrm{d}x", allowed_commands=()
    )
    assert not spaced["within_guard"]
    assert spaced["added_commands"] == ["mathrm"]
    assert syntax_only_guard(
        r"\left( a + b", r"\left( a + b \right)", allowed_commands=()
    )["within_guard"]


def test_guard_refuses_changed_mathematics():
    identifiers = syntax_only_guard(r"E = y^2", r"E = z^2", allowed_commands=())
    assert not identifiers["within_guard"]
    assert not identifiers["identifiers_equal"]
    relations = syntax_only_guard(r"a = b", r"a = b = c", allowed_commands=())
    assert not relations["within_guard"]
    assert relations["relations"] == [1, 2]


def test_guard_allows_only_the_commands_the_compiler_named():
    allowed = error_commands(
        [r"Undefined control sequence (context: l.19 a + \zorp)"], r"a + \zorp", WHITELIST
    )
    assert {"zorp"} <= allowed
    assert syntax_only_guard(r"a + \zorp", r"a + zorp", allowed_commands=allowed)[
        "within_guard"
    ] is False  # the expansion introduces an identifier
    # A licensed command may be exchanged for another name in the same place...
    assert syntax_only_guard(r"a + \zorp", r"a + \psi", allowed_commands=allowed | {"psi"})[
        "within_guard"
    ]
    # ...but deleting it outright removes a symbol from the expression, which
    # is a question for the Checker, not for the host.
    assert not syntax_only_guard(r"a + \zorp b", r"a + b", allowed_commands=allowed)[
        "within_guard"
    ]
    # An unsupported command of the recorded formula is always replaceable.
    assert "zorp" in error_commands([], r"a + \zorp", WHITELIST)
    assert "alpha" not in error_commands([], r"a + \alpha", WHITELIST)


# ---------------------------------------------------------------------------
# Compiling and quotations


def test_a_route_that_compiles_produces_no_repair_and_no_substitution(tmp_path):
    runtime = ScriptedTypesetRuntime()
    layer = build(
        tmp_path,
        [step("step_0001", derivation=r"Result \(E = mc^2\).")],
        runner=RuleRunner(),
        runtime=runtime,
    )
    assert layer["status"] == LAYER_STATUS_COMPILED
    assert [item["status"] for item in layer["formulas"]] == [STATUS_OK]
    assert layer["formulas"][0]["typeset"] == "E = mc^2"
    assert layer["calls"] == [] and not runtime.repair_requests
    assert layer["steps"] == [
        {
            "step_revision_id": "step_0001",
            "output_sha256": sha256_text(
                canonical_json(step("step_0001", derivation=r"Result \(E = mc^2\).")["content"])
            ),
            "format_issues": [],
        }
    ]
    assert layer["accepted_with_format_issues"] is False


def test_quotation_is_expanded_for_typesetting_only(tmp_path):
    sources = {"src_a": r"\newcommand{\rr}{{\bf r}}" + "\nThe position \\rr appears."}
    runtime = ScriptedTypesetRuntime()
    steps = [
        step(
            "step_0001",
            source=r"Quoted from src_a: \(\rr \cdot \rr + 1 = 0\) as printed.",
        )
    ]
    layer = build(
        tmp_path, steps, runner=RuleRunner([r"\rr"]), runtime=runtime, sources=sources
    )
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_QUOTATION_EXPANDED
    assert entry["quotation"] == "source_field"
    assert entry["typeset"] == r"{\bf r} \cdot {\bf r} + 1 = 0"
    assert entry["original"] == r"\rr \cdot \rr + 1 = 0"
    # A quotation is never sent to the Writer, expanded or not.
    assert not runtime.repair_requests and layer["calls"] == []
    assert layer["status"] == LAYER_STATUS_COMPILED


def test_quotation_that_still_fails_is_marked_verbatim(tmp_path):
    runtime = ScriptedTypesetRuntime()
    steps = [step("step_0001", source=r"As printed: \(\k_{1} = 0\).")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"\k"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_QUOTATION_VERBATIM
    assert entry["typeset"] == entry["original"] == r"\k_{1} = 0"
    assert not runtime.repair_requests
    assert any(item["code"] == "quotation_not_typeset" for item in layer["flags"])
    assert layer["status"] == LAYER_STATUS_COMPILED


# ---------------------------------------------------------------------------
# Repair, guard, review


def test_repair_within_the_guard_is_accepted_without_review(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"{x^2}^3")]]
    )
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_REPAIRED
    assert entry["typeset"] == r"{x^2}^3"
    assert entry["guard"]["within_guard"] is True
    assert entry["compiler_errors"][0]["message"].endswith(r"x^2^3)")
    assert "context: l." in entry["compiler_errors"][0]["message"]
    assert [item["kind"] for item in layer["calls"]] == ["formula_repair"]
    assert layer["calls"][0]["model"] == "writer-model"
    assert layer["calls"][0]["effort"] == "high"
    assert not runtime.review_requests
    assert layer["status"] == LAYER_STATUS_COMPILED
    # One call was made and journalled for the run's budget.
    assert (tmp_path / "typeset" / "model_calls.jsonl").read_text().count("\n") == 1


def test_repair_beyond_the_guard_is_accepted_only_by_review(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"z^{2}")]],
        reviews=[
            lambda request: [
                (request.items[0].formula_id, "equivalent", "same quantity renamed")
            ]
        ],
    )
    steps = [step("step_0001", derivation=r"Then \(y^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_REPAIRED_REVIEWED
    assert entry["typeset"] == r"z^{2}"
    assert entry["guard"]["within_guard"] is False
    assert entry["review"] == {
        "verdict": "equivalent",
        "reason": "same quantity renamed",
    }
    assert [item["kind"] for item in layer["calls"]] == [
        "formula_repair",
        "formula_equivalence_review",
    ]
    assert layer["calls"][1]["model"] == "checker-model"
    review = runtime.review_requests[0]
    assert review.items[0].original_latex == r"y^2^3"
    assert review.steps[0].content.derivation == steps[0]["content"]["derivation"]


def test_a_correction_the_review_refuses_keeps_the_recorded_formula(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"z^{2}")]],
        reviews=[
            lambda request: [
                (request.items[0].formula_id, "not_equivalent", "y became z")
            ]
        ],
    )
    steps = [step("step_0001", derivation=r"Then \(y^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_FAILED
    assert entry["typeset"] == entry["original"] == r"y^2^3"
    assert entry["review"]["verdict"] == "not_equivalent"
    assert layer["status"] == LAYER_STATUS_WITH_FAILURES
    assert {item["code"] for item in layer["flags"]} >= {
        "correction_not_equivalent",
        "formula_failed",
    }
    # It is not asked again in a later round.
    assert len(runtime.repair_requests) == 1


def test_a_correction_that_is_not_one_math_body_is_refused(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"\[{x^2}^3\]")]]
    )
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_FAILED
    assert entry["repair_attempts"][0]["accepted"] is False
    assert not runtime.review_requests


def test_repair_rounds_are_capped(tmp_path):
    # Every attempt stays inside the guard and every attempt still fails.
    variants = [r"{x^2}^3", r"{x^{2}}^3", r"{{x^2}}^3", r"{x^{{2}}}^3"]
    runtime = ScriptedTypesetRuntime(
        repairs=[
            lambda request, latex=latex: [(request.formula_ids[0], latex)]
            for latex in variants
        ]
    )
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^3"]), runtime=runtime)
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_FAILED
    assert entry["typeset"] == entry["original"]
    assert len(runtime.repair_requests) == MAX_REPAIR_ROUNDS
    assert len(entry["repair_attempts"]) == MAX_REPAIR_ROUNDS
    assert any(item["code"] == "repair_rounds_exhausted" for item in layer["flags"])
    assert layer["repair_rounds"] == MAX_REPAIR_ROUNDS
    # Every later round repairs the recorded formula, not the failed attempt.
    second = runtime.repair_requests[1].steps[0].formulas[0]
    assert second.record_latex == r"x^2^3"
    assert second.latex == r"{x^2}^3"


def test_infrastructure_failure_leaves_every_formula_unverified(tmp_path):
    runtime = ScriptedTypesetRuntime()
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(
        tmp_path, steps, runner=RuleRunner(infrastructure=True), runtime=runtime
    )
    assert layer["status"] == LAYER_STATUS_INFRASTRUCTURE
    entry = layer["formulas"][0]
    assert entry["status"] == STATUS_NOT_COMPILED
    assert entry["typeset"] == entry["original"]
    assert not runtime.repair_requests
    assert any("infrastructure" in item["code"] for item in layer["flags"])


def test_the_model_call_budget_stops_repairs(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"{x^2}^3")]]
    )
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(
        tmp_path,
        steps,
        runner=RuleRunner([r"^2^3"]),
        runtime=runtime,
        run_config=config(max_model_calls=2),
    )
    assert not runtime.repair_requests
    assert any(item["code"] == "repair_budget_exhausted" for item in layer["flags"])
    assert layer["formulas"][0]["status"] == STATUS_FAILED


def test_a_runtime_without_repair_support_only_flags(tmp_path):
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=None)
    assert any(item["code"] == "repair_unavailable" for item in layer["flags"])
    assert layer["formulas"][0]["status"] == STATUS_FAILED


def test_a_failed_repair_call_does_not_fail_the_layer(tmp_path):
    from derivation_runtime.types import RuntimeInvocationError

    runtime = ScriptedTypesetRuntime(
        repair_error=RuntimeInvocationError(
            "provider_failed", "boom", partial_output="", retryable=True
        )
    )
    steps = [step("step_0001", derivation=r"Then \(x^2^3\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3"]), runtime=runtime)
    assert layer["calls"][0]["status"] == "not_started"
    assert any(item["code"] == "repair_call_failed" for item in layer["flags"])
    assert layer["formulas"][0]["status"] == STATUS_FAILED
    # A call that never started is not charged to the run.
    assert not (tmp_path / "typeset" / "model_calls.jsonl").exists()


# ---------------------------------------------------------------------------
# Consumers


def test_verification_rejects_a_layer_of_another_record(tmp_path):
    steps = [step("step_0001", derivation=r"Then \(x^2\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner())
    write_typeset_layer(tmp_path, layer)
    assert verify_typeset_layer(
        layer, run_id="run-typeset", route_id="route_br_0001", steps=steps
    )
    assert verified_layers(
        tmp_path, run_id="run-typeset", routes={"route_br_0001": steps}
    ) == {"route_br_0001": layer}
    changed = [step("step_0001", derivation=r"Then \(x^3\) follows.")]
    assert not verify_typeset_layer(
        layer, run_id="run-typeset", route_id="route_br_0001", steps=changed
    )
    assert (
        verified_layers(
            tmp_path, run_id="run-typeset", routes={"route_br_0001": changed}
        )
        == {}
    )
    assert not verify_typeset_layer(
        layer, run_id="other-run", route_id="route_br_0001", steps=steps
    )


def test_field_substitution_uses_only_verified_statuses(tmp_path):
    runtime = ScriptedTypesetRuntime(
        repairs=[lambda request: [(request.formula_ids[0], r"{x^2}^3")]]
    )
    steps = [
        step(
            "step_0001",
            derivation=r"First \(x^2^3\) then \(a+b\).",
            source=r"As printed: \(\k_{1} = 0\).",
        )
    ]
    layer = build(tmp_path, steps, runner=RuleRunner([r"^2^3", r"\k"]), runtime=runtime)
    lookup = typeset_lookup(layer)
    derivation = typeset_field_text(
        steps[0]["content"]["derivation"],
        [entry for key, entry in lookup.items() if key[1] == "derivation"],
    )
    assert derivation == r"First \({x^2}^3\) then \(a+b\)."
    # A verbatim quotation stays exactly as recorded; the consumer that wants
    # to mark it reads the status from the layer, not from this function.
    source_entries = [entry for key, entry in lookup.items() if key[1] == "source"]
    assert [entry["status"] for entry in source_entries] == [STATUS_QUOTATION_VERBATIM]
    assert (
        typeset_field_text(steps[0]["content"]["source"], source_entries)
        == steps[0]["content"]["source"]
    )


def test_layer_file_round_trips(tmp_path):
    steps = [step("step_0001", derivation=r"Then \(x^2\) follows.")]
    layer = build(tmp_path, steps, runner=RuleRunner())
    path = write_typeset_layer(tmp_path, layer)
    assert path == tmp_path / "typeset" / "route_br_0001.json"
    assert json.loads(path.read_text(encoding="utf-8")) == layer


def test_a_second_attempt_never_overwrites_compile_evidence(tmp_path):
    steps = [step("step_0001", derivation=r"Then \(x^2\) follows.")]
    first = build(tmp_path, steps, runner=RuleRunner())
    second = build(tmp_path, steps, runner=RuleRunner())
    assert first["evidence_directory"] != second["evidence_directory"]
    assert (tmp_path / first["evidence_directory"] / "round-00").is_dir()
    assert (tmp_path / second["evidence_directory"] / "round-00").is_dir()


def real_runner():
    """The locked Tectonic of this repository, or a clean skip without it."""

    from derivation_app.reporting import TectonicRunner, TectonicRuntimeSpec

    root = Path(__file__).resolve().parents[3]
    try:
        runner = TectonicRunner(TectonicRuntimeSpec.from_lock(root))
    except (OSError, ValueError, KeyError) as exc:
        pytest.skip(f"locked Tectonic runtime unavailable: {exc}")
    error = runner._artifact_error()
    if error is not None:
        pytest.skip(f"locked Tectonic runtime unavailable: {error}")
    return runner


def test_the_locked_engine_expands_a_quotation_and_accepts_a_repair(tmp_path):
    runner = real_runner()
    sources = {
        "src_a": "\\newcommand{\\rr}{{\\bf r}}\n"
        "Quoted from src_a: $\\rr \\cdot \\rr = 1$ exactly as printed.\n"
    }
    steps = [
        step(
            "step_0001",
            source=r"Quoted from src_a: \(\rr \cdot \rr = 1\) exactly as printed.",
            derivation=r"Then \(\sum_\k f(\k) = 1\) closes the route.",
        )
    ]
    runtime = ScriptedTypesetRuntime(
        repairs=[
            lambda request: [
                (request.formula_ids[0], r"\sum_{\mathbf{k}} f(\mathbf{k}) = 1")
            ]
        ],
        reviews=[
            lambda request: [
                (request.items[0].formula_id, "equivalent", "standard notation")
            ]
        ],
    )
    layer = build(tmp_path, steps, runner=runner, runtime=runtime, sources=sources)

    assert layer["status"] == LAYER_STATUS_COMPILED
    quotation = entries(layer)["step_0001:source:1"]
    assert quotation["status"] == STATUS_QUOTATION_EXPANDED
    assert quotation["typeset"] == r"{\bf r} \cdot {\bf r} = 1"
    repaired = entries(layer)["step_0001:derivation:1"]
    assert repaired["status"] == STATUS_REPAIRED_REVIEWED
    assert repaired["typeset"] == r"\sum_{\mathbf{k}} f(\mathbf{k}) = 1"
    assert "context: l." in repaired["compiler_errors"][0]["message"]
    assert repaired["compiler_errors"][0]["kind"] in {"tex_error", "missing_glyph"}
    assert layer["engine"]["binary_sha256"] == runner.runtime.binary_sha256


def test_typeset_route_refuses_an_unsafe_route_id(tmp_path):
    from derivation_app.route_typeset import layer_path

    with pytest.raises(ValueError, match="unsafe route id"):
        layer_path(tmp_path, "../escape")
