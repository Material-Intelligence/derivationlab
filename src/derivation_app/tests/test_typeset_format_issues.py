"""A sealed step with unresolved format defects must leave the control plane.

The per-step gate spends at most two
format rewrites and then seals the output anyway, marked. ``control.sqlite``
is not a deliverable, so the mark has to reach the typeset layer, the report
manifest and the candidate text a reviewer reads.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from derivation_app import route_typeset
from derivation_app.reporting import (
    ReportExporter,
    ReportSource,
    TectonicRunner,
    TectonicRuntimeSpec,
)
from derivation_app.route_typeset import (
    FORMAT_ISSUE_FLAG,
    STATUS_OK,
    format_issue_warnings,
    layer_format_issue_flags,
    pending_typeset_routes,
    step_format_issues,
    typeset_content_sha256,
    typeset_layer_identity,
    write_typeset_layer,
)
from derivation_app.tests.test_report_bundle import (
    ROOT,
    RUNS,
    _compiler_script,
    _problem,
    _run,
    _sha256_directory,
    _sha256_file,
)
from derivation_app.tests.test_route_typeset import RuleRunner, build, step
from derivation_app.tests.test_route_typeset_consumers import _formula, _layer

AUDIT = {
    "call_writer_1": {
        "disposition": "accepted_with_format_issues",
        "issues": [
            {
                "code": "unsupported_command",
                "field": "derivation",
                "formula_index": 1,
                "severity": "error",
                "message": r"Unsupported TeX command: \bm",
            },
            {
                "code": "possible_escape_swallow",
                "field": "derivation",
                "formula_index": 1,
                "severity": "warning",
                "message": "advisory",
            },
        ],
    }
}


def _sealed(revision_id: str, call_id: str | None) -> dict:
    value = step(revision_id, derivation=r"Result \(E = mc^2\).")
    value["model_call_id"] = call_id
    return value


# ---------------------------------------------------------------------------
# The layer


def test_step_format_issues_selects_only_the_marked_disposition():
    steps = [_sealed("step_0001", "call_writer_1"), _sealed("step_0002", "call_other")]
    found = step_format_issues(steps, AUDIT)
    assert list(found) == ["step_0001"]
    # Only the blocking defects are carried; advisories are not defects.
    assert [item["code"] for item in found["step_0001"]] == ["unsupported_command"]
    assert step_format_issues(steps, None) == {}
    assert step_format_issues(steps, {"call_writer_1": {"disposition": "accepted"}}) == {}


def test_the_layer_carries_the_mark_for_the_step_that_still_has_defects(tmp_path):
    layer = build(
        tmp_path,
        [_sealed("step_0001", "call_writer_1"), _sealed("step_0002", "call_other")],
        runner=RuleRunner(),
        format_audits=AUDIT,
    )
    assert layer["accepted_with_format_issues"] is True
    assert [item["status"] for item in layer["formulas"]] == [STATUS_OK, STATUS_OK]
    marked = {
        item["step_revision_id"]: item["format_issues"] for item in layer["steps"]
    }
    assert [item["code"] for item in marked["step_0001"]] == ["unsupported_command"]
    assert marked["step_0002"] == []
    flags = layer_format_issue_flags(layer)
    assert [item["code"] for item in flags] == [FORMAT_ISSUE_FLAG]
    assert flags[0]["step_revision_id"] == "step_0001"
    assert flags[0]["issue_count"] == 1


def test_a_clean_run_says_so_rather_than_saying_nothing(tmp_path):
    layer = build(tmp_path, [_sealed("step_0001", "call_other")], runner=RuleRunner())
    assert layer["accepted_with_format_issues"] is False
    assert layer_format_issue_flags(layer) == []
    assert format_issue_warnings({"route_br_0001": layer}) == []


# ---------------------------------------------------------------------------
# The report


def _source(typeset=None) -> ReportSource:
    return ReportSource(run=_run(), problem=_problem(), typeset=typeset)


def _marked_layer() -> dict:
    layer = _layer(_formula("claim", 1, "x=1", "x=1", STATUS_OK))
    layer["accepted_with_format_issues"] = True
    layer["steps"][0]["format_issues"] = [
        {"code": "unsupported_command", "field": "claim", "message": r"\bm"}
    ]
    layer["flags"] = [
        {
            "code": FORMAT_ISSUE_FLAG,
            "step_revision_id": "revision-1",
            "issue_count": 1,
            "issues": layer["steps"][0]["format_issues"],
        }
    ]
    return layer


def test_format_issue_warnings_name_the_route_and_the_step():
    warnings = format_issue_warnings({"route-main": _marked_layer()})
    assert [item["code"] for item in warnings] == [FORMAT_ISSUE_FLAG]
    assert warnings[0]["route_id"] == "route-main"
    assert warnings[0]["step_revision_id"] == "revision-1"


class ReportIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNS.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="typeset-identity-test-", dir=RUNS
        )
        self.root = Path(self.temporary.name)
        self.managed = self.root / "runtime"
        self.bundle = self.managed / "bundle"
        self.bundle.mkdir(parents=True)
        (self.bundle / "bundle.txt").write_text("fixture", encoding="utf-8")
        self.binary = self.managed / "tectonic"
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 deterministic" > "$outdir/report.pdf"\n'
            'printf "offline compile ok\\n"\n',
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _exporter(self) -> ReportExporter:
        runtime = TectonicRuntimeSpec(
            version="0.17.0",
            managed_root=self.managed,
            binary_path=self.binary,
            bundle_path=self.bundle,
            binary_sha256=_sha256_file(self.binary),
            bundle_sha256=_sha256_directory(self.bundle),
            provisioned=True,
            target="test-host",
        )
        return ReportExporter(
            repo_root=ROOT,
            report_root=self.root / "reports",
            runner=TectonicRunner(runtime, timeout_seconds=2.0, max_output_bytes=4096),
            clock=lambda: datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        )

    def test_a_run_without_a_layer_keeps_its_content_hash(self) -> None:
        exporter = self._exporter()
        bare = exporter.export(_source(), selected_route_id="route-main")
        empty = exporter.export(_source({}), selected_route_id="route-main")
        self.assertEqual(
            bare.manifest["content_sha256"], empty.manifest["content_sha256"]
        )

    def test_a_layer_changes_the_content_hash_and_shows_the_mark(self) -> None:
        exporter = self._exporter()
        bare = exporter.export(_source(), selected_route_id="route-main")
        marked = exporter.export(
            _source({"route-main": _marked_layer()}), selected_route_id="route-main"
        )
        self.assertNotEqual(
            bare.manifest["content_sha256"], marked.manifest["content_sha256"]
        )
        codes = [item["code"] for item in marked.manifest["warnings"]]
        self.assertIn(FORMAT_ISSUE_FLAG, codes)

    def test_two_layers_that_deliver_different_text_hash_differently(self) -> None:
        exporter = self._exporter()
        first = _layer(_formula("claim", 1, "x=1", "x = 1", "repaired"))
        second = _layer(_formula("claim", 1, "x=1", r"x = 1 \quad", "repaired"))
        self.assertNotEqual(
            typeset_content_sha256(first), typeset_content_sha256(second)
        )
        left = exporter.export(
            _source({"route-main": first}), selected_route_id="route-main"
        )
        right = exporter.export(
            _source({"route-main": second}), selected_route_id="route-main"
        )
        self.assertNotEqual(
            left.manifest["content_sha256"], right.manifest["content_sha256"]
        )

    def test_the_identity_ignores_timings_and_evidence_paths(self) -> None:
        layer = _layer(_formula("claim", 1, "x=1", "x = 1", "repaired"))
        noisy = {
            **layer,
            "seconds": 12.5,
            "compile_seconds": 3.25,
            "evidence_directory": "typeset/route-main.evidence/attempt-07",
            "calls": [{"call_id": "typeset_route-main_r01"}],
        }
        self.assertEqual(typeset_content_sha256(layer), typeset_content_sha256(noisy))
        self.assertNotIn("seconds", typeset_layer_identity(noisy))


# ---------------------------------------------------------------------------
# A stale layer is rebuilt, not fallen back on for ever (R2 #6)


def _canonical(step_ids, output_hashes, contents) -> dict:
    return {
        "run": {"run_id": "run-typeset"},
        "branches": [
            {
                "branch_id": "br_0001",
                "status": "completed",
                "step_revision_ids": list(step_ids),
                "status_history": [{"seq": 1}],
            }
        ],
        "step_revisions": [
            {
                "step_revision_id": step_id,
                "output_sha256": output_hash,
                "content": content,
                "origin": {"model_call_id": None},
            }
            for step_id, output_hash, content in zip(
                step_ids, output_hashes, contents, strict=True
            )
        ],
    }


def test_a_stale_layer_leaves_its_route_pending(tmp_path):
    first = _sealed("step_0001", None)
    canonical = _canonical(["step_0001"], [first["output_sha256"]], [first["content"]])
    assert pending_typeset_routes(tmp_path, canonical) == [("route_br_0001", "br_0001")]

    layer = build(tmp_path, [first], runner=RuleRunner())
    write_typeset_layer(tmp_path, layer)
    assert pending_typeset_routes(tmp_path, canonical) == []

    # The route grew a step: the file on disk no longer describes this Record,
    # every consumer ignores it, so it must be rebuilt rather than kept.
    second = _sealed("step_0002", None)
    grown = _canonical(
        ["step_0001", "step_0002"],
        [first["output_sha256"], second["output_sha256"]],
        [first["content"], second["content"]],
    )
    assert pending_typeset_routes(tmp_path, grown) == [("route_br_0001", "br_0001")]


# ---------------------------------------------------------------------------
# A failed quotation analysis is conservative (R2 #4)


def test_a_failed_quotation_analysis_protects_the_whole_step(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise ValueError("macro tables are unreadable")

    # Without the analysis the host cannot tell a quotation from the Writer's
    # own math, and sending a cited author's notation to a repair round is the
    # one thing that must not happen.
    monkeypatch.setattr(route_typeset, "normalize_step_fields", unavailable)
    layer = build(
        tmp_path,
        [_sealed("step_0001", None)],
        runner=RuleRunner([r"E = mc"]),
        runtime=None,
        sources={"src_a": "irrelevant"},
    )
    assert [item["code"] for item in layer["flags"]][:1] == [
        "quotation_analysis_unavailable"
    ]
    entry = layer["formulas"][0]
    assert entry["quotation"] == "analysis_unavailable"
    # Nothing was sent to a repair round; the recorded body is shown verbatim.
    assert entry["status"] == "quotation_verbatim"
    assert entry["typeset"] == entry["original"]
    assert layer["calls"] == []
