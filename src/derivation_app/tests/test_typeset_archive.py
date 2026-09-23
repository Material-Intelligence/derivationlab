"""Typeset views of archived runs: read-only archives, normalization, compile."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from derivation_agent_record import canonical_json, sha256_text
from derivation_app.reporting import CompileResult
from derivation_app.route_typeset import (
    LAYER_STATUS_COMPILED,
    LAYER_STATUS_WITH_FAILURES,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_QUOTATION_EXPANDED,
    STATUS_REPAIRED,
    STATUS_REPAIRED_REVIEWED,
)
from derivation_app.typeset_archive import (
    ARCHIVE_INDEX_SCHEMA_VERSION,
    ARCHIVE_VIEW_SCHEMA_VERSION,
    ArchiveConfig,
    ArchiveError,
    ArchiveRun,
    assert_inside,
    build_archive_views,
    build_view,
    discover_run_directories,
    failure_class,
    load_archive_run,
    main,
    normalize_step,
    select_route,
    view_summary,
)
from derivation_runtime.formula_normalization import SourceMacroTables
from derivation_runtime.formula_validation import load_engine_whitelist
from derivation_runtime.types import (
    FormulaEquivalenceOutput,
    FormulaRepairOutput,
    ModelRole,
    ModelSpec,
    ProviderLineage,
    RuntimeInvocation,
    RuntimeSession,
    Usage,
)

WHITELIST = load_engine_whitelist()
UNDEFINED = "Undefined control sequence"
GOLDEN = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden_events.jsonl"
)


# ---------------------------------------------------------------------------
# Fakes


class RuleRunner:
    """Fake TectonicRunner: a fragment fails while it contains one of ``rules``.

    The document layout and line map are the production ones, so the log this
    returns is located exactly as the locked engine's would be.
    """

    runtime = SimpleNamespace(
        version="fake-engine",
        target="test",
        binary_sha256="a" * 64,
        bundle_sha256="b" * 64,
    )

    def __init__(self, rules: Sequence[str] = (), *, message: str = UNDEFINED) -> None:
        self.rules = list(rules)
        self.message = message
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


class ScriptedRuntime:
    """Runtime answering the typeset repair and review calls from a script."""

    def __init__(self, repairs=(), reviews=()) -> None:
        self.repairs = list(repairs)
        self.reviews = list(reviews)
        self.repair_requests: list[object] = []
        self.review_requests: list[object] = []
        self._counter = 0

    def _invocation(self, role: str) -> RuntimeInvocation:
        self._counter += 1
        return RuntimeInvocation(
            session=RuntimeSession(f"thread-{self._counter}", ProviderLineage.NATIVE),
            operation_id=f"turn-{self._counter}",
            role=ModelRole(role),
        )

    async def start_formula_repair(self, request):
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


# ---------------------------------------------------------------------------
# Fixtures


def content(**overrides: str) -> dict[str, str]:
    value = {
        "claim": "Claim text.",
        "why": "Why text.",
        "source": "Plain source text.",
        "derivation": "Plain derivation text.",
        "scope": "Scope text.",
    }
    value.update(overrides)
    return value


def revision(revision_id: str, branch_id: str, fields: dict[str, str]) -> dict:
    return {
        "step_revision_id": revision_id,
        "branch_id": branch_id,
        "step_slot": "s1",
        "revision": 1,
        "content": fields,
        "output_sha256": sha256_text(canonical_json(fields)),
    }


def canonical(
    revisions: Sequence[dict],
    *,
    branches: Sequence[dict] | None = None,
    sources: Sequence[dict] = (),
    run_id: str = "run_fixture",
) -> dict:
    by_branch: dict[str, list[str]] = {}
    for item in revisions:
        by_branch.setdefault(item["branch_id"], []).append(item["step_revision_id"])
    resolved = list(branches) if branches is not None else [
        {
            "branch_id": branch_id,
            "status": "completed",
            "status_history": [{"seq": 10, "from": None, "to": "completed"}],
            "step_revision_ids": step_ids,
        }
        for branch_id, step_ids in by_branch.items()
    ]
    return {
        "schema_version": "derivation-agent-canonical-v1.1",
        "run": {"run_id": run_id},
        "branches": resolved,
        "step_revisions": list(revisions),
        "source_evidence": list(sources),
        "summary": {"model_call_count": 4},
    }


def archive_run(
    canonical_value: dict,
    *,
    directory: Path,
    sources: dict[str, str] | None = None,
    documents: dict[str, str] | None = None,
    max_model_calls: int | None = 40,
) -> ArchiveRun:
    return ArchiveRun(
        directory=directory,
        relative_path="fixture/run",
        events_sha256="c" * 64,
        event_count=7,
        canonical=canonical_value,
        config=ArchiveConfig(
            run_id=canonical_value["run"]["run_id"],
            max_model_calls=max_model_calls,
            formula_validation_policy="formula-v1",
            writer=ModelSpec("openai", "writer-model", "high"),
            checker=ModelSpec("openai", "checker-model", "medium"),
        ),
        sources=dict(sources or {}),
        documents=dict(documents or {}),
    )


def build(
    run: ArchiveRun,
    tmp_path: Path,
    *,
    runner: RuleRunner,
    runtime=None,
    evidence: str = "full",
    compile_recorded: bool = True,
) -> dict:
    out = tmp_path / "views"
    return asyncio.run(
        build_view(
            run,
            output_directory=out / run.relative_path,
            output_root=out,
            runner=runner,
            whitelist=WHITELIST,
            runtime=runtime,
            evidence=evidence,
            compile_recorded=compile_recorded,
        )
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def golden_archive(tmp_path: Path, name: str = "run_golden") -> Path:
    if not GOLDEN.is_file():
        pytest.skip("golden record fixture is not available")
    directory = tmp_path / "runs" / name
    directory.mkdir(parents=True)
    shutil.copyfile(GOLDEN, directory / "events.jsonl")
    return directory


# ---------------------------------------------------------------------------
# The write guard and discovery


def test_assert_inside_refuses_a_path_outside_the_output_root(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    assert assert_inside(root / "a" / "b.json", root) == (root / "a" / "b.json")
    assert assert_inside(root, root) == root.resolve()
    with pytest.raises(ArchiveError):
        assert_inside(tmp_path / "elsewhere.json", root)
    with pytest.raises(ArchiveError):
        assert_inside(root / ".." / "escape.json", root)


def test_discovery_takes_records_and_skips_the_view_directory(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "a").mkdir(parents=True)
    (runs / "a" / "events.jsonl").write_text(
        json.dumps({"type": "run_created"}) + "\n", encoding="utf-8"
    )
    (runs / "b").mkdir()
    (runs / "b" / "events.jsonl").write_text(
        json.dumps({"type": "writer_call_started"}) + "\n", encoding="utf-8"
    )
    (runs / "c").mkdir()
    (runs / "c" / "events.jsonl").write_text("not json\n", encoding="utf-8")
    (runs / "typeset_views" / "a").mkdir(parents=True)
    (runs / "typeset_views" / "a" / "events.jsonl").write_text(
        json.dumps({"type": "run_created"}) + "\n", encoding="utf-8"
    )
    assert discover_run_directories(runs) == [(runs / "a").resolve()]


def test_loading_a_record_leaves_the_archive_byte_for_byte(tmp_path: Path) -> None:
    directory = golden_archive(tmp_path)
    before = tree_digest(directory)
    run = load_archive_run(directory, runs_root=tmp_path / "runs")
    assert run.run_id
    assert run.relative_path == "run_golden"
    assert run.events_sha256 == hashlib.sha256(
        (directory / "events.jsonl").read_bytes()
    ).hexdigest()
    assert tree_digest(directory) == before


def test_a_broken_record_raises_instead_of_writing(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / "broken"
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        json.dumps({"type": "run_created"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ArchiveError):
        load_archive_run(directory, runs_root=tmp_path / "runs")
    assert sorted(path.name for path in directory.iterdir()) == ["events.jsonl"]


# ---------------------------------------------------------------------------
# Never writing into an archived run


def test_a_view_is_refused_inside_the_archived_run_directory(tmp_path: Path) -> None:
    archive = tmp_path / "runs" / "run_x"
    archive.mkdir(parents=True)
    run = archive_run(
        canonical([revision("step_0001", "br_0001", content())]), directory=archive
    )
    with pytest.raises(ArchiveError):
        asyncio.run(
            build_view(
                run,
                output_directory=archive / "typeset_views",
                output_root=archive,
                runner=RuleRunner(),
                whitelist=WHITELIST,
            )
        )
    assert list(archive.iterdir()) == []


def test_building_a_view_never_writes_into_a_read_only_archive(tmp_path: Path) -> None:
    archive = golden_archive(tmp_path)
    before = tree_digest(archive)
    mode = archive.stat().st_mode
    archive.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        run = archive_run(
            canonical(
                [
                    revision(
                        "step_0001",
                        "br_0001",
                        content(derivation="Take \\(E = mc^2\\) here."),
                    )
                ]
            ),
            directory=archive,
        )
        view = build(run, tmp_path, runner=RuleRunner())
    finally:
        archive.chmod(mode)
    assert view["schema_version"] == ARCHIVE_VIEW_SCHEMA_VERSION
    assert tree_digest(archive) == before
    assert (tmp_path / "views" / "fixture" / "run" / "typeset_view.json").is_file()


# ---------------------------------------------------------------------------
# Route selection


def test_the_delivered_route_is_the_latest_completed_branch() -> None:
    value = canonical(
        [
            revision("step_0001", "br_0001", content()),
            revision("step_0002", "br_0002", content()),
            revision("step_0003", "br_0003", content()),
        ],
        branches=[
            {
                "branch_id": "br_0001",
                "status": "completed",
                "status_history": [{"seq": 5, "to": "completed"}],
                "step_revision_ids": ["step_0001"],
            },
            {
                "branch_id": "br_0002",
                "status": "completed",
                "status_history": [{"seq": 40, "to": "completed"}],
                "step_revision_ids": ["step_0002"],
            },
            {
                "branch_id": "br_0003",
                "status": "active",
                "status_history": [{"seq": 90, "to": "active"}],
                "step_revision_ids": ["step_0003"],
            },
        ],
    )
    chosen = select_route(value)
    assert chosen is not None
    assert (chosen.branch_id, chosen.selection) == ("br_0002", "completed")


def test_without_a_completed_branch_the_latest_active_one_is_used() -> None:
    value = canonical(
        [
            revision("step_0001", "br_0001", content()),
            revision("step_0002", "br_0002", content()),
        ],
        branches=[
            {
                "branch_id": "br_0001",
                "status": "active",
                "status_history": [{"seq": 7, "to": "active"}],
                "step_revision_ids": ["step_0001"],
            },
            {
                "branch_id": "br_0002",
                "status": "active",
                "status_history": [{"seq": 11, "to": "active"}],
                "step_revision_ids": ["step_0002"],
            },
        ],
    )
    chosen = select_route(value)
    assert chosen is not None
    assert (chosen.branch_id, chosen.selection) == ("br_0002", "active")


def test_without_an_active_branch_the_latest_branch_is_used() -> None:
    value = canonical(
        [revision("step_0001", "br_0001", content())],
        branches=[
            {
                "branch_id": "br_0001",
                "status": "paused",
                "status_history": [{"seq": 3, "to": "paused"}],
                "step_revision_ids": ["step_0001"],
            }
        ],
    )
    chosen = select_route(value)
    assert chosen is not None
    assert chosen.selection == "latest"


def test_a_run_without_recorded_steps_has_no_route(tmp_path: Path) -> None:
    value = canonical(
        [],
        branches=[
            {
                "branch_id": "br_0001",
                "status": "active",
                "status_history": [{"seq": 1, "to": "active"}],
                "step_revision_ids": [],
            }
        ],
    )
    assert select_route(value) is None
    view = build(
        archive_run(value, directory=tmp_path / "archive"), tmp_path, runner=RuleRunner()
    )
    assert view["route"] is None
    assert view_summary(view)["route_status"] == "no_route"


# ---------------------------------------------------------------------------
# Deterministic normalization


def test_normalization_restores_control_characters_and_expands_macros() -> None:
    source_text = "\\newcommand{\\rr}{{\\bf r}}\nBody of the cited paper.\n"
    sources = {"src_a": source_text}
    tables = SourceMacroTables(sources, {"src_a": "10.0/a"})
    result = normalize_step(
        content(
            derivation="Then \\(\x0bomega \\rr\\) follows.",
            source="src_a line 1",
        ),
        sources=sources,
        tables=tables,
        whitelist=WHITELIST,
    )
    assert result["replacement_kinds"]["control_char_backslash"] == 1
    assert result["replacement_kinds"]["macro_expansion"] == 1
    assert "\\omega {\\bf r}" in result["normalized"]["derivation"]
    assert not [item for item in result["issues_after"] if item["severity"] == "error"]
    assert [item for item in result["issues_before"] if item["severity"] == "error"]


def test_every_sealed_step_is_normalized_not_only_the_route(tmp_path: Path) -> None:
    on_route = revision(
        "step_0001", "br_0001", content(derivation="Route \\(\x0bomega\\).")
    )
    off_route = revision(
        "step_0002", "br_0002", content(derivation="Parked \\(\x0blambda\\).")
    )
    value = canonical(
        [on_route, off_route],
        branches=[
            {
                "branch_id": "br_0001",
                "status": "completed",
                "status_history": [{"seq": 10, "to": "completed"}],
                "step_revision_ids": ["step_0001"],
            },
            {
                "branch_id": "br_0002",
                "status": "parked",
                "status_history": [{"seq": 4, "to": "parked"}],
                "step_revision_ids": ["step_0002"],
            },
        ],
    )
    view = build(
        archive_run(value, directory=tmp_path / "archive"), tmp_path, runner=RuleRunner()
    )
    by_id = {item["step_revision_id"]: item for item in view["steps"]}
    assert by_id["step_0001"]["on_delivered_route"] is True
    assert by_id["step_0002"]["on_delivered_route"] is False
    for step in by_id.values():
        assert step["normalization"]["replacement_kinds"]["control_char_backslash"] == 1
    assert view["route"]["step_revision_ids"] == ["step_0001"]


# ---------------------------------------------------------------------------
# Compiling the route, before and after


def test_the_view_compiles_what_the_record_could_not(tmp_path: Path) -> None:
    value = canonical(
        [revision("step_0001", "br_0001", content(derivation="Take \\(\x0bomega\\)."))]
    )
    # The control character is what the recorded formula fails on.
    runner = RuleRunner(rules=["\x0b"])
    view = build(archive_run(value, directory=tmp_path / "archive"), tmp_path, runner=runner)
    route = view["route"]
    assert route["recorded_compile"]["passed"] is False
    assert route["recorded_compile"]["failures"][0]["formula_id"] == (
        "step_0001:derivation:1"
    )
    assert route["typeset_layer"]["status"] == LAYER_STATUS_COMPILED
    assert [item["status"] for item in route["typeset_layer"]["formulas"]] == [STATUS_OK]
    assert route["residual_failures"] == []
    summary = view_summary(view)
    assert summary["recorded_compile_passed"] is False
    assert summary["route_status"] == LAYER_STATUS_COMPILED


def test_a_formula_no_rule_can_fix_is_delivered_flagged_and_classified(
    tmp_path: Path,
) -> None:
    value = canonical(
        [revision("step_0001", "br_0001", content(derivation="Then \\(\\zorp\\) is."))]
    )
    view = build(
        archive_run(value, directory=tmp_path / "archive"),
        tmp_path,
        runner=RuleRunner(rules=["\\zorp"]),
    )
    route = view["route"]
    assert route["typeset_layer"]["status"] == LAYER_STATUS_WITH_FAILURES
    assert [item["status"] for item in route["typeset_layer"]["formulas"]] == [
        STATUS_FAILED
    ]
    residual = route["residual_failures"]
    assert len(residual) == 1
    assert residual[0]["failure_class"] == "undefined_control_sequence:\\zorp"
    # The delivered body is still exactly what the Record holds.
    assert route["typeset_layer"]["formulas"][0]["typeset"] == "\\zorp"
    assert view_summary(view)["residual_failure_classes"] == {
        "undefined_control_sequence:\\zorp": 1
    }


def test_a_quotation_is_expanded_only_for_typesetting(tmp_path: Path) -> None:
    source_text = "\\newcommand{\\Ef}{{\\bf E}}\nThe drift velocity v_{d}(\\Ef) follows.\n"
    value = canonical(
        [
            revision(
                "step_0001",
                "br_0001",
                content(source="Quoting src_a: \\(v_{d}(\\Ef)\\)."),
            )
        ],
        sources=[{"source_id": "src_a", "text": source_text}],
    )
    run = archive_run(
        value,
        directory=tmp_path / "archive",
        sources={"src_a": source_text},
        documents={"src_a": "10.0/a"},
    )
    view = build(run, tmp_path, runner=RuleRunner())
    entry = view["route"]["typeset_layer"]["formulas"][0]
    assert entry["status"] == STATUS_QUOTATION_EXPANDED
    assert entry["original"] == "v_{d}(\\Ef)"
    assert entry["typeset"] == "v_{d}({\\bf E})"
    assert entry["record_original"] == "v_{d}(\\Ef)"
    assert entry["normalized_equals_record"] is True


def test_the_view_binds_itself_to_the_record_it_was_built_from(
    tmp_path: Path,
) -> None:
    step = revision("step_0001", "br_0001", content(derivation="Take \\(x\\)."))
    view = build(
        archive_run(canonical([step]), directory=tmp_path / "archive"),
        tmp_path,
        runner=RuleRunner(),
    )
    assert view["run"]["events_sha256"] == "c" * 64
    assert view["steps"][0]["output_sha256"] == step["output_sha256"]
    layer = json.loads(
        (
            tmp_path / "views" / "fixture" / "run" / "typeset" / "route_br_0001.json"
        ).read_text(encoding="utf-8")
    )
    assert layer["run_id"] == "run_fixture"
    assert layer["steps"][0]["output_sha256"] == step["output_sha256"]
    assert layer["archive_view"]["events_sha256"] == "c" * 64


def test_evidence_retention_prunes_what_it_is_told_to(tmp_path: Path) -> None:
    value = canonical(
        [revision("step_0001", "br_0001", content(derivation="Take \\(x\\)."))]
    )
    run = archive_run(value, directory=tmp_path / "archive")
    build(run, tmp_path / "full", runner=RuleRunner(), evidence="full")
    build(run, tmp_path / "lean", runner=RuleRunner(), evidence="summary")
    build(run, tmp_path / "bare", runner=RuleRunner(), evidence="none")
    suffixes = {
        name: sorted(
            {
                path.name.split(".", 1)[1]
                for path in (tmp_path / name / "views").rglob("compile-*")
            }
        )
        for name in ("full", "lean", "bare")
    }
    assert suffixes["full"] == ["compiler.json", "log", "map.json", "tex"]
    assert suffixes["lean"] == ["compiler.json"]
    assert suffixes["bare"] == []


# ---------------------------------------------------------------------------
# Repair mode


def test_without_a_runtime_the_repair_loop_is_reported_unavailable(
    tmp_path: Path,
) -> None:
    value = canonical(
        [revision("step_0001", "br_0001", content(derivation="Then \\(\\zorp\\)."))]
    )
    view = build(
        archive_run(value, directory=tmp_path / "archive"),
        tmp_path,
        runner=RuleRunner(rules=["\\zorp"]),
    )
    layer = view["route"]["typeset_layer"]
    assert {item["code"] for item in layer["flags"]} >= {"repair_unavailable"}
    assert layer["calls"] == []
    assert view_summary(view)["model_calls"] == 0


def test_a_syntax_only_correction_is_accepted_without_a_review(
    tmp_path: Path,
) -> None:
    value = canonical(
        [
            revision(
                "step_0001", "br_0001", content(derivation="Then \\(\\frac{a{b}\\).")
            )
        ]
    )
    runtime = ScriptedRuntime(
        repairs=[
            lambda request: [
                (item.formula_id, "\\frac{a}{b}")
                for step in request.steps
                for item in step.formulas
            ]
        ]
    )
    view = build(
        archive_run(value, directory=tmp_path / "archive"),
        tmp_path,
        runner=RuleRunner(rules=["\\frac{a{b}"]),
        runtime=runtime,
    )
    layer = view["route"]["typeset_layer"]
    assert len(runtime.repair_requests) == 1
    assert runtime.review_requests == []
    assert layer["formulas"][0]["status"] == STATUS_REPAIRED
    assert layer["formulas"][0]["typeset"] == "\\frac{a}{b}"
    assert layer["formulas"][0]["original"] == "\\frac{a{b}"
    assert layer["status"] == LAYER_STATUS_COMPILED
    assert view["route"]["residual_failures"] == []
    assert view_summary(view)["model_calls"] == 1


def test_a_correction_beyond_syntax_is_put_to_the_checker(tmp_path: Path) -> None:
    value = canonical(
        [
            revision(
                "step_0001", "br_0001", content(derivation="Then \\(\\zorp x\\) is.")
            )
        ]
    )
    runtime = ScriptedRuntime(
        repairs=[
            lambda request: [
                (item.formula_id, "\\mu x")
                for step in request.steps
                for item in step.formulas
            ]
        ],
        reviews=[
            lambda request: [
                (item.formula_id, "equivalent", "same quantity")
                for item in request.items
            ]
        ],
    )
    view = build(
        archive_run(value, directory=tmp_path / "archive"),
        tmp_path,
        runner=RuleRunner(rules=["\\zorp"]),
        runtime=runtime,
    )
    layer = view["route"]["typeset_layer"]
    assert len(runtime.repair_requests) == 1
    assert len(runtime.review_requests) == 1
    assert layer["formulas"][0]["status"] == STATUS_REPAIRED_REVIEWED
    assert layer["formulas"][0]["typeset"] == "\\mu x"
    assert layer["status"] == LAYER_STATUS_COMPILED
    assert view_summary(view)["model_calls"] == 2


# ---------------------------------------------------------------------------
# The whole archive


def test_the_index_totals_every_view_and_names_what_it_skipped(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    golden_archive(tmp_path, "run_golden")
    broken = runs / "run_broken"
    broken.mkdir(parents=True)
    (broken / "events.jsonl").write_text(
        json.dumps({"type": "run_created"}) + "\n", encoding="utf-8"
    )
    index = asyncio.run(
        build_archive_views(
            runs_root=runs,
            output_root=runs / "typeset_views",
            runner=RuleRunner(),
            whitelist=WHITELIST,
            evidence="summary",
            concurrency=2,
        )
    )
    assert index["schema_version"] == ARCHIVE_INDEX_SCHEMA_VERSION
    assert index["repair"] is False
    assert [row["run_path"] for row in index["runs"]] == ["run_golden"]
    assert [row["run_path"] for row in index["skipped"]] == ["run_broken"]
    assert index["totals"]["runs"] == 1
    assert index["totals"]["steps"] == index["runs"][0]["steps"]
    written = json.loads((runs / "typeset_views" / "INDEX.json").read_text("utf-8"))
    assert written["totals"] == index["totals"]
    assert (runs / "typeset_views" / "run_golden" / "typeset_view.json").is_file()
    # The archive itself gained nothing.
    assert sorted(path.name for path in (runs / "run_golden").iterdir()) == [
        "events.jsonl"
    ]


def test_failure_classes_group_by_what_a_repair_would_face() -> None:
    assert failure_class("tex_error", "Undefined control sequence") == (
        "undefined_control_sequence"
    )
    assert failure_class(
        "tex_error", "Undefined control sequence (context: l.9 a\\zorp b)"
    ) == "undefined_control_sequence:\\zorp"
    assert failure_class("missing_glyph", "U+1D706 in font") == "missing_glyph"
    assert failure_class("unsafe_not_compiled", "never compiled") == (
        "unsafe_not_compiled"
    )
    assert failure_class("tex_error", "Missing $ inserted (context: l.4 x)") == (
        "Missing $ inserted"
    )


def test_the_cli_refuses_repair_without_a_runtime_factory() -> None:
    with pytest.raises(SystemExit):
        main(["--repair"])
