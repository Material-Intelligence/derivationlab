from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from derivation_api.models import FrozenProblemInput, RunView
from fastapi.testclient import TestClient

from derivation_app.factory import create_fake_service, create_http_app
from derivation_app.reporting import (
    CompileResult,
    ReportExporter,
    ReportSource,
    TectonicRunner,
    TectonicRuntimeSpec,
    _math_is_safe,
    _render_report,
    _scientific_tokens,
    _sha256_directory,
    _sha256_file,
    compiler_formula_error,
)

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "runs"


def _problem() -> FrozenProblemInput:
    return FrozenProblemInput.model_validate(
        {
            "problem_id": "report-test",
            "version": 1,
            "supersedes_version": None,
            "objective": "Derive $x=1$ without trusting <raw HTML>.",
            "givens": ["A bounded deterministic fixture."],
            "assumptions": [],
            "scope": "Report export only.",
            "deliverable": "An auditable PDF.",
            "allowed_tools": [],
            "allowed_references": [],
            "success_criteria": ["The bundle is immutable."],
            "source_pack": None,
            "confirmed_by_user": True,
        }
    )


def _run() -> RunView:
    return RunView.model_validate(
        {
            "id": "run-report-test",
            "question": "Derive a report fixture.",
            "status": "review_ready",
            "phase": "review_ready",
            "config": {
                "granularity": "one_task",
                "writer": {"provider": "fake", "model": "writer", "effort": "low"},
                "checker": {"provider": "fake", "model": "checker", "effort": "low"},
                "judge": {"provider": "fake", "model": "judge", "effort": "low"},
                "backend": {"name": "fake", "version": "1"},
                "max_model_calls": 4,
                "max_active_branches": 2,
                "reference_allowed": False,
                "allowed_paths": [],
            },
            "runtime": {
                "auth_mode": "chatgpt",
                "concurrency": 1,
                "retries": 0,
                "max_run_seconds": None,
            },
            "root_step_id": "step-1",
            "steps": [
                {
                    "id": "step-1",
                    "revision_id": "revision-1",
                    "branch_id": "branch-1",
                    "order": 1,
                    "title": "Bounded report step",
                    "status": "sealed",
                    "content": {
                        "claim": "The value is \\(x=1\\).",
                        "why": "A block follows: \\[x^2=1\\].",
                        "source": "No external source.",
                        "derivation": "Also $$y=2$$; reject $$\\input{/etc/passwd}$$ safely.",
                        "scope": "Fixture only.",
                    },
                    "output_sha256": "a" * 64,
                    "input": "fixture",
                    "reasoning_summary": "deterministic",
                    "output": "x=1",
                    "checks": {
                        "schema": "passed",
                        "physics": "passed",
                        "provenance": "passed",
                    },
                    "provenance": {
                        "model": "fake",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "created_at": "2026-08-30T20:00:00Z",
                    },
                }
            ],
            "edges": [],
            "branches": [
                {
                    "branch_id": "branch-1",
                    "parent_branch_id": None,
                    "anchor_step_revision_id": None,
                    "kind": "root",
                    "status": "completed",
                    "status_history": [{"seq": 1, "status": "completed"}],
                    "step_revision_ids": ["revision-1"],
                }
            ],
            "routes": [
                {
                    "id": "route-main",
                    "label": "Main route",
                    "node_ids": ["step-1"],
                    "status": "complete",
                    "branch_id": "branch-1",
                    "status_history": [{"seq": 1, "status": "completed"}],
                },
                {
                    "id": "route-failed",
                    "label": "Failed route",
                    "node_ids": ["step-1"],
                    "status": "failed",
                    "branch_id": "branch-1",
                    "status_history": [{"seq": 1, "status": "completed"}],
                },
            ],
            "budget": {"used_steps": 1, "max_steps": 4},
            "canonical_event_id": 7,
            "pause_requested": False,
            "hard_interrupt_requested": False,
            "created_at": "2026-08-30T20:00:00Z",
            "updated_at": "2026-08-30T20:01:00Z",
            "error_message": None,
        }
    )


def _compiler_script(path: Path, body: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf "Tectonic 0.17.0\\n"; exit 0; fi\n'
        "outdir=\nprevious=\n"
        'for argument in "$@"; do\n'
        '  if [ "$previous" = "--outdir" ]; then outdir="$argument"; fi\n'
        '  previous="$argument"\n'
        "done\n"
        'case " $* " in *" --untrusted "*) ;; *) exit 91 ;; esac\n'
        'case " $* " in *" --only-cached "*) ;; *) exit 92 ;; esac\n'
        '[ "$TECTONIC_UNTRUSTED_MODE" = "1" ] || exit 93\n' + body,
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_request() -> dict[str, object]:
    return {
        "problem": {
            "problem_id": "report-api-test",
            "version": 1,
            "supersedes_version": None,
            "objective": "Construct a deterministic ReportBundle route.",
            "givens": ["The deterministic runtime is available."],
            "assumptions": [],
            "scope": "Report API integration only.",
            "deliverable": "A checked deterministic route.",
            "allowed_tools": [],
            "allowed_references": [],
            "success_criteria": ["One route reaches review-ready."],
            "source_pack": None,
            "confirmed_by_user": True,
        },
        "config": {
            "granularity": "one_task",
            "writer": {
                "provider": "fake",
                "model": "fake:writer",
                "effort": "deterministic",
            },
            "checker": {
                "provider": "fake",
                "model": "fake:checker",
                "effort": "deterministic",
            },
            "judge": {
                "provider": "fake",
                "model": "fake:judge",
                "effort": "deterministic",
            },
            "backend": {"name": "deterministic-fake-runtime", "version": "1"},
            "max_model_calls": 64,
            "max_active_branches": 2,
            "reference_allowed": False,
            "allowed_paths": [],
        },
        "runtime": {
            "auth_mode": "chatgpt",
            "concurrency": 1,
            "retries": 0,
            "max_run_seconds": None,
        },
    }


class ReportBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNS.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="report-bundle-test-", dir=RUNS
        )
        self.root = Path(self.temporary.name)
        self.managed = self.root / "runtime"
        self.managed.mkdir()
        self.bundle = self.managed / "bundle"
        self.bundle.mkdir()
        (self.bundle / "fixture").write_text("offline bundle", encoding="utf-8")
        self.binary = self.managed / "tectonic"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runner(self, *, timeout: float = 2.0) -> TectonicRunner:
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
        return TectonicRunner(runtime, timeout_seconds=timeout, max_output_bytes=4096)

    def _exporter(self, runner: TectonicRunner) -> ReportExporter:
        return ReportExporter(
            repo_root=ROOT,
            report_root=self.root / "reports",
            runner=runner,
            clock=lambda: datetime(2026, 8, 30, 20, 30, tzinfo=UTC),
        )

    def test_record_control_bytes_are_visible_without_breaking_tex(self) -> None:
        run = _run()
        run.steps[0].title = "second-\x00order"
        run.steps[0].content.derivation = "Before\x00after; $x,\x00y$ and $x\x01y$"
        tex, renderer = _render_report(
            ReportSource(run=run, problem=_problem()), "route-main"
        )
        self.assertNotIn("\x00", tex)
        self.assertNotIn("\x01", tex)
        self.assertIn("second-[U+0000]order", tex)
        self.assertIn("Before[U+0000]after", tex)
        self.assertIn("x[U+0001]y", tex.replace(r"\allowbreak{}", ""))
        self.assertNotIn(r"\fbox{\parbox", tex)
        self.assertTrue(renderer.warnings)

    def test_successful_compiler_with_missing_glyphs_is_rejected(self) -> None:
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 incomplete" > "$outdir/report.pdf"\n'
            'printf "warning: Missing character: U+4E2D\\n"\n',
        )
        result = self._exporter(self._runner()).export(
            ReportSource(run=_run(), problem=_problem()), selected_route_id="route-main"
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.manifest["failure"]["code"], "tectonic_missing_glyphs")
        self.assertNotIn("report.pdf", result.files)

    def test_success_bundle_is_versioned_auditable_and_never_overwritten(self) -> None:
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 deterministic" > "$outdir/report.pdf"\n'
            'printf "offline compile ok\\n"\n',
        )
        exporter = self._exporter(self._runner())
        source = ReportSource(run=_run(), problem=_problem())

        first = exporter.export(source, selected_route_id="route-main")
        second = exporter.export(source, selected_route_id="route-main")

        self.assertEqual(first.status, second.status, "success")
        self.assertNotEqual(first.export_id, second.export_id)
        self.assertTrue(second.export_id.endswith("-01"))
        first_dir = ROOT / first.bundle_path
        self.assertEqual(
            sorted(path.name for path in first_dir.iterdir()),
            ["assets", "compile.log", "manifest.json", "report.pdf", "report.tex"],
        )
        tex = (first_dir / "report.tex").read_text(encoding="utf-8")
        self.assertIn(r"\documentclass[11pt,letterpaper]{article}", tex)
        self.assertIn(r"\usepackage{libertinus}", tex)
        self.assertIn(r"\section{Frozen Problem}", tex)
        self.assertIn(r"\section{Checks and Provenance}", tex)
        self.assertIn(r"\section{Other and Failed Routes}", tex)
        self.assertNotIn(r"\linebreak", tex)
        self.assertNotIn(r"\input{/etc/passwd}", tex)
        equations = first.manifest["equations"]
        self.assertEqual([item["number"] for item in equations], [1, 2, 3, 4])
        self.assertEqual(
            [item["local_formula_index"] for item in equations], [1, 2, 1, 2]
        )
        self.assertTrue(
            all(item["step_revision_id"] == "revision-1" for item in equations)
        )
        self.assertEqual(first.manifest["warnings"][0]["code"], "math_rejected")
        warning = first.manifest["warnings"][0]
        self.assertGreaterEqual(warning["formula_index"], 1)
        self.assertLess(warning["start"], warning["end"])
        self.assertEqual(warning["diagnostics"][0]["code"], "unsafe_command")
        self.assertIn("Disabled TeX command", tex)
        self.assertTrue((first_dir / "report.pdf").exists())

    def test_product_external_data_returns_data_relative_bundle_path(self) -> None:
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 deterministic" > "$outdir/report.pdf"\n',
        )
        with tempfile.TemporaryDirectory(prefix="derivationlab-data-") as directory:
            data_root = Path(directory)
            exporter = ReportExporter(
                repo_root=ROOT,
                report_root=data_root / "runs" / "_report_bundles",
                allowed_root=data_root,
                runner=self._runner(),
                clock=lambda: datetime(2026, 8, 30, 20, 30, tzinfo=UTC),
            )

            result = exporter.export(
                ReportSource(run=_run(), problem=_problem()),
                selected_route_id="route-main",
            )

            self.assertTrue(result.bundle_path.startswith("runs/_report_bundles/"))
            self.assertTrue((data_root / result.bundle_path / "report.pdf").is_file())

    def test_failed_compile_retains_evidence_without_a_pdf(self) -> None:
        _compiler_script(self.binary, 'printf "intentional failure\\n"\nexit 7\n')
        result = self._exporter(self._runner()).export(
            ReportSource(run=_run(), problem=_problem()),
            selected_route_id="route-main",
        )

        self.assertEqual(result.status, "failed")
        directory = ROOT / result.bundle_path
        self.assertFalse((directory / "report.pdf").exists())
        self.assertTrue((directory / "report.tex").exists())
        self.assertIn(
            "intentional failure",
            (directory / "compile.log").read_text(encoding="utf-8"),
        )
        self.assertEqual(result.manifest["failure"]["code"], "tectonic_compile_failed")

    def test_compiler_error_location_supports_native_and_file_line_logs(self) -> None:
        self.assertEqual(
            compiler_formula_error(
                "! Missing { inserted.\n<to be read again>\n$\nl.14 \\]\n"
            ),
            (14, "Missing { inserted."),
        )
        self.assertEqual(
            compiler_formula_error("error: report.tex:19: Double subscript."),
            (19, "Double subscript."),
        )
        self.assertIsNone(
            compiler_formula_error("error: report.tex:19: font file unavailable")
        )

    def test_located_content_failure_falls_back_and_retains_attempts(self) -> None:
        _compiler_script(self.binary, "exit 1\n")
        runner = self._runner()
        run = _run()
        run.steps[0].content.claim = "$x_ $"
        source = ReportSource(run=run, problem=_problem())
        snapshot = run.model_dump_json()

        def compile_candidate(tex, *, workspace_parent):
            for index, line in enumerate(tex.splitlines(), 1):
                if r"\(x_\)" in line:
                    return CompileResult(
                        "failed",
                        f"! Missing {{ inserted.\nl.{index} \\)\n",
                        None,
                        "0.17.0",
                        "tectonic_compile_failed",
                    )
            return CompileResult("success", "finished", b"%PDF-1.7 fixture", "0.17.0")

        with patch.object(
            runner, "compile", side_effect=compile_candidate
        ) as compile_mock:
            result = self._exporter(runner).export(
                source, selected_route_id="route-main"
            )
        self.assertEqual(result.status, "success")
        self.assertGreater(compile_mock.call_count, 1)
        directory = ROOT / result.bundle_path
        self.assertEqual(
            len(list(directory.glob("compile-attempt-*.tex"))),
            compile_mock.call_count - 1,
        )
        self.assertEqual(
            len(list(directory.glob("compile-attempt-*.log"))),
            compile_mock.call_count - 1,
        )
        self.assertIn("Tectonic: Missing", (directory / "report.tex").read_text())
        self.assertEqual(snapshot, run.model_dump_json())
        warnings = [w for w in result.manifest["warnings"] if w["field"] == "claim"]
        self.assertTrue(warnings)
        self.assertEqual(warnings[0]["diagnostics"][0]["code"], "syntax_error")
        self.assertEqual((warnings[0]["start"], warnings[0]["end"]), (1, 4))

    def test_infrastructure_failure_never_triggers_formula_fallback(self) -> None:
        _compiler_script(self.binary, "exit 1\n")
        runner = self._runner()
        failure = CompileResult(
            "failed",
            "! Missing { inserted.\nl.14 \\]\n",
            None,
            "0.17.0",
            "tectonic_timeout",
        )
        with patch.object(runner, "compile", return_value=failure) as compile_mock:
            result = self._exporter(runner).export(
                ReportSource(run=_run(), problem=_problem()),
                selected_route_id="route-main",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(compile_mock.call_count, 1)

    def test_real_compiler_content_failure_is_readable_fallback(self) -> None:
        runtime = TectonicRuntimeSpec.from_lock(ROOT)
        runner = TectonicRunner(runtime)
        if runner._artifact_error():
            self.skipTest("Pinned product compiler is not provisioned")
        run = _run()
        run.steps[0].content.claim = "$x_ $"
        result = self._exporter(runner).export(
            ReportSource(run=run, problem=_problem()), selected_route_id="route-main"
        )
        self.assertEqual(
            result.status,
            "success",
            (ROOT / result.bundle_path / "compile.log").read_text(),
        )
        self.assertTrue(list((ROOT / result.bundle_path).glob("compile-attempt-*.log")))

    def test_display_math_accepts_delimiter_adjacent_blank_lines(self) -> None:
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 deterministic" > "$outdir/report.pdf"\n',
        )
        run = _run().model_copy(deep=True)
        assert run.steps[0].content is not None
        run.steps[0].title = r"Derivative is \[\partial_{x_0}\mathcal F"
        run.steps[0].content.derivation = "Before. \\[\n\n x^2 + y^2 = 1 \n\n\\] After."
        result = self._exporter(self._runner()).export(
            ReportSource(run=run, problem=_problem()),
            selected_route_id="route-main",
        )

        self.assertEqual(result.status, "success")
        tex = (ROOT / result.bundle_path / "report.tex").read_text(encoding="utf-8")
        self.assertIn("\\begin{equation}\nx^2 + y^2 = 1\n", tex)
        self.assertNotIn("\\begin{equation}\n\n", tex)
        self.assertIn("Step 1: Derivative is [display formula]", tex)
        self.assertNotIn(r"\textbackslash{}[", tex)

    def test_currency_dollars_stay_prose_while_numeric_math_stays_math(self) -> None:
        tokens = _scientific_tokens(
            "Price is $5 and cost $10. Formula $x=5$ and $5 + x$ remain math."
        )

        self.assertEqual(
            [token.value for token in tokens if token.kind == "inline_math"],
            ["x=5", "5 + x"],
        )
        prose = "".join(token.value for token in tokens if token.kind == "prose")
        self.assertIn("$5", prose)
        self.assertIn("$10", prose)

    def test_math_command_allowlist_rejects_file_and_engine_primitives(self) -> None:
        self.assertTrue(_math_is_safe(r"\frac{\partial x}{\partial B}=\sum_n A_n"))
        for expression in (
            r"\boxed{x}",
            r"x\cdots y",
            r"{\bf x}",
            r"{\rm x}",
            r"{\cal H}",
        ):
            self.assertTrue(_math_is_safe(expression))
        for payload in (
            r'\hbox{\XeTeXpdffile "/abs/path/report.pdf" page 1}',
            r'\XeTeXpicfile "/abs/path/image.png"',
            r"\pdfximage{/abs/path/report.pdf}",
            r'\font\evil="/abs/path/font.otf"',
            r"\input{/etc/passwd}",
            r"\unknownmacro{x}",
        ):
            with self.subTest(payload=payload):
                self.assertFalse(_math_is_safe(payload))

    def test_timeout_is_bounded_and_retains_a_failure_bundle(self) -> None:
        _compiler_script(self.binary, "/bin/sleep 2\n")
        result = self._exporter(self._runner(timeout=0.1)).export(
            ReportSource(run=_run(), problem=_problem()),
            selected_route_id="route-main",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.manifest["failure"]["code"], "tectonic_timeout")
        self.assertFalse((ROOT / result.bundle_path / "report.pdf").exists())

    def test_hash_mismatch_fails_closed_before_execution(self) -> None:
        _compiler_script(self.binary, 'printf "%s" "%PDF" > "$outdir/report.pdf"\n')
        runtime = TectonicRuntimeSpec(
            version="0.17.0",
            managed_root=self.managed,
            binary_path=self.binary,
            bundle_path=self.bundle,
            binary_sha256="0" * 64,
            bundle_sha256=_sha256_directory(self.bundle),
            provisioned=True,
            target="test-host",
        )
        result = self._exporter(TectonicRunner(runtime)).export(
            ReportSource(run=_run(), problem=_problem()),
            selected_route_id="route-main",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.manifest["failure"]["code"], "tectonic_runtime_unavailable"
        )

    def test_runtime_service_exposes_the_same_pathless_report_api(self) -> None:
        _compiler_script(
            self.binary,
            'printf "%s" "%PDF-1.7 deterministic" > "$outdir/report.pdf"\n',
        )
        exporter = self._exporter(self._runner())
        active_root = self.root / "active"
        active_root.mkdir()
        service = create_fake_service(
            run_root=active_root,
            repo_root=ROOT,
            run_id_factory=lambda: "run-report-api",
            report_exporter=exporter,
        )
        with TestClient(create_http_app(service)) as client:
            created = client.post("/api/runs", json=_run_request())
            self.assertEqual(created.status_code, 201, created.text)
            final = created.json()
            for _ in range(100):
                final_response = client.get("/api/runs/run-report-api")
                self.assertEqual(final_response.status_code, 200, final_response.text)
                final = final_response.json()
                if final["phase"] == "review_ready":
                    break
                threading.Event().wait(0.01)
            self.assertEqual(final["phase"], "review_ready")
            route_id = final["routes"][0]["id"]
            response = client.post(
                "/api/runs/run-report-api/reports",
                json={"selected_route_id": route_id, "confirm_selected_route": True},
            )
            self.assertEqual(response.status_code, 201, response.text)
            report = response.json()
            self.assertEqual(report["status"], "success")
            self.assertNotIn("output_path", report)
            download = client.get(
                f"/api/runs/run-report-api/reports/{report['export_id']}/report.pdf"
            )
            self.assertEqual(download.status_code, 200, download.text)
            self.assertEqual(download.content, b"%PDF-1.7 deterministic")
            self.assertEqual(download.headers["content-type"], "application/pdf")
            self.assertIn("attachment", download.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
