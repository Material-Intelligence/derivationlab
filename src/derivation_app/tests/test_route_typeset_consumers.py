"""Report export reads the typeset layer, and renders identically without one."""

from __future__ import annotations

from derivation_app.reporting import ReportSource, _render_report
from derivation_app.route_typeset import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_QUOTATION_VERBATIM,
    STATUS_REPAIRED,
    TYPESET_SCHEMA_VERSION,
)
from derivation_app.tests.test_report_bundle import _problem, _run


def _layer(*formulas: dict) -> dict:
    return {
        "schema_version": TYPESET_SCHEMA_VERSION,
        "run_id": "run-report-test",
        "route_id": "route-main",
        "branch_id": "branch-1",
        "status": "compiled",
        "steps": [{"step_revision_id": "revision-1", "output_sha256": "a" * 64}],
        "formulas": list(formulas),
    }


def _formula(
    field: str,
    index: int,
    original: str,
    typeset: str,
    status: str,
    **extra,
) -> dict:
    return {
        "formula_id": f"revision-1:{field}:{index}",
        "step_revision_id": "revision-1",
        "field": field,
        "index": index,
        "start": 0,
        "end": 0,
        "kind": "inline_math",
        "quotation": None,
        "original": original,
        "typeset": typeset,
        "status": status,
        **extra,
    }


def _source(typeset=None) -> ReportSource:
    return ReportSource(run=_run(), problem=_problem(), typeset=typeset)


def _claim_warnings(renderer) -> list[dict]:
    """Only this fixture's claim field; its derivation has its own warnings."""

    return [item for item in renderer.warnings if item.get("field") == "claim"]


def test_a_run_without_a_layer_renders_exactly_as_before():
    baseline, _ = _render_report(_source(), "route-main")
    assert _render_report(_source({}), "route-main")[0] == baseline
    # A layer of another route leaves this route untouched.
    assert (
        _render_report(_source({"route-other": _layer()}), "route-main")[0] == baseline
    )
    # An entry whose recorded formula no longer matches is ignored.
    stale = _layer(_formula("claim", 1, "x=2", "x=3", STATUS_REPAIRED))
    assert _render_report(_source({"route-main": stale}), "route-main")[0] == baseline


def test_a_repaired_formula_is_typeset_from_the_layer():
    layer = _layer(_formula("claim", 1, "x=1", r"x = 1 \quad", STATUS_REPAIRED))
    tex, renderer = _render_report(_source({"route-main": layer}), "route-main")
    # The confirmed route uses the layer. The frozen problem statement and the
    # appendix route have none, so both keep the recorded body.
    assert tex.count(r"\(x = 1 \quad\)") == 1
    assert tex.count(r"\(x=1\)") == 2
    # The substitution is not silent: the manifest says which body was printed.
    assert [item["code"] for item in _claim_warnings(renderer)] == ["typeset_repaired"]
    assert _claim_warnings(renderer)[0]["recorded"] == "x=1"
    assert _claim_warnings(renderer)[0]["typeset"] == r"x = 1 \quad"


def test_a_layer_verified_command_is_no_longer_rejected_by_the_v1_vocabulary():
    # ``\mathscr`` is outside the frozen formula-v1 set but the locked engine
    # compiles it, so a body the layer compiled is not downgraded to text.
    layer = _layer(
        _formula("claim", 1, "x=1", r"\mathscr{H}x=1", STATUS_REPAIRED)
    )
    tex, renderer = _render_report(_source({"route-main": layer}), "route-main")
    assert r"\mathscr{H}x=1" in tex
    assert [item["code"] for item in _claim_warnings(renderer)] == ["typeset_repaired"]


def test_a_quotation_that_was_not_typeset_is_shown_verbatim():
    layer = _layer(_formula("claim", 1, "x=1", "x=1", STATUS_QUOTATION_VERBATIM))
    tex, renderer = _render_report(_source({"route-main": layer}), "route-main")
    assert "Quotation shown verbatim (not typeset)" in tex
    assert [item["code"] for item in _claim_warnings(renderer)] == [
        "quotation_verbatim"
    ]


def test_a_failed_formula_carries_its_compiler_error_into_the_report():
    layer = _layer(
        _formula(
            "claim",
            1,
            "x=1",
            "x=1",
            STATUS_FAILED,
            compiler_errors=[{"round": 0, "message": "Undefined control sequence"}],
        )
    )
    tex, renderer = _render_report(_source({"route-main": layer}), "route-main")
    assert "Unrendered math" in tex
    assert "Undefined control sequence" in tex
    assert [item["code"] for item in _claim_warnings(renderer)] == ["math_rejected"]


def test_an_unrepaired_formula_keeps_the_ordinary_rendering():
    layer = _layer(_formula("claim", 1, "x=1", "x=1", STATUS_OK))
    baseline, _ = _render_report(_source(), "route-main")
    assert _render_report(_source({"route-main": layer}), "route-main")[0] == baseline


def test_the_service_loads_a_layer_by_route_node_ids(tmp_path):
    from derivation_app.factory import create_fake_service
    from derivation_app.route_typeset import write_typeset_layer
    from derivation_runtime.formula_validation import math_fragments

    view = _run()
    step = view.steps[0]
    content = step.content.model_dump(mode="json")
    layer = {
        **_layer(
            *[
                {
                    "formula_id": f"{step.revision_id}:{fragment.field}:{fragment.formula_index}",
                    "step_revision_id": step.revision_id,
                    "field": fragment.field,
                    "index": fragment.formula_index,
                    "start": fragment.start,
                    "end": fragment.end,
                    "kind": fragment.kind,
                    "quotation": None,
                    "original": fragment.value,
                    "typeset": fragment.value,
                    "status": STATUS_OK,
                }
                for fragment in math_fragments(content)
            ]
        ),
        "run_id": view.id,
        "route_id": "route-main",
        "steps": [
            {
                "step_revision_id": step.revision_id,
                "output_sha256": step.output_sha256,
            }
        ],
    }
    directory = tmp_path / "run"
    directory.mkdir()
    write_typeset_layer(directory, layer)
    service = create_fake_service(run_root=tmp_path / "active", storage_root=tmp_path)
    assert service._typeset_layers(directory, view) == {"route-main": layer}
