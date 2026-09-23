"""Opt-in real offline compiler smoke; no service, user data, or model calls.

Run with the derivation_api environment and PYTHONPATH=src:src/derivation_api.
Pass an evidence directory under runs as the first argument.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from derivation_app.reporting import ReportExporter, ReportSource
from derivation_app.tests.test_report_bundle import _problem, _run


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    evidence = (root / sys.argv[1]).resolve()
    if not evidence.is_relative_to(root / "runs"):
        raise ValueError("evidence directory must be under runs")
    run = _run()
    problem = _problem()
    problem.objective = "中文推导测试：计算能量 $E=mc^2$。"
    run.steps[0].title = "主路线能量推导"
    run.steps[0].content.derivation = "\n\n".join(
        f"第 {i} 段：中文长文检验。能量守恒要求 $E=mc^2$，这里保留所有中文和公式。"
        for i in range(1, 81)
    )
    alternate = run.steps[0].model_copy(deep=True)
    alternate.id = "step-alternate"
    alternate.revision_id = "revision-alternate"
    alternate.title = "备用路线独有标题"
    alternate.content.derivation = (
        "ALTERNATE-ONLY：备用路线内容。\\[E=\\frac{p^2}{2m}\\]"
    )
    run.steps.append(alternate)
    run.routes[1].node_ids = [alternate.id]
    run.routes[1].label = "备用路线"
    exporter = ReportExporter.from_repository_lock(repo_root=root, report_root=evidence)
    results = []
    for route in run.routes:
        result = exporter.export(
            ReportSource(run=run, problem=problem), selected_route_id=route.id
        )
        directory = root / result.bundle_path
        if result.status != "success":
            raise RuntimeError(
                f"{route.id}: {result.manifest['failure']}; see {directory}"
            )
        subprocess.run(
            [
                "pdftotext",
                "-layout",
                str(directory / "report.pdf"),
                str(directory / "extracted.txt"),
            ],
            check=True,
        )
        text = (directory / "extracted.txt").read_text()
        assert "中文推导测试" in text, "Chinese objective was lost"
        main = text.split("Confirmed Main Route", 1)[1].split("Branch Overview", 1)[0]
        assert ("ALTERNATE-ONLY" in main) == (route.id == "route-failed")
        assert "Missing character:" not in (directory / "compile.log").read_text()
        results.append(
            {
                "route": route.id,
                "bundle": result.bundle_path,
                "pages": text.count("\f"),
                "pdf_bytes": (directory / "report.pdf").stat().st_size,
            }
        )
    (evidence / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
