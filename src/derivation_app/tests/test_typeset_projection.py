"""What the run view substitutes, what it leaves sealed, and what it declares."""

from __future__ import annotations

from derivation_app.projection import typeset_run_view
from derivation_app.route_typeset import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_QUOTATION_VERBATIM,
    STATUS_REPAIRED,
    typeset_content_sha256,
)
from derivation_app.tests.test_report_bundle import _run
from derivation_app.tests.test_route_typeset_consumers import _formula, _layer


def _claim_layer(typeset: str, status: str = STATUS_REPAIRED, **overrides) -> dict:
    layer = _layer(_formula("claim", 1, "x=1", typeset, status))
    layer.update(overrides)
    return layer


def test_a_verified_layer_replaces_the_math_and_nothing_else() -> None:
    view = _run()
    layer = _claim_layer("x = 1")

    shown = typeset_run_view(view, {"route-main": layer})

    step = shown.steps[0]
    sealed = view.steps[0]
    assert step.content is not None and sealed.content is not None
    assert step.content.claim == "The value is \\(x = 1\\)."
    assert step.typeset is True
    # The other four fields, the ids and the Record's hash are untouched.
    assert step.content.why == sealed.content.why
    assert step.content.derivation == sealed.content.derivation
    assert step.output_sha256 == sealed.output_sha256
    assert step.revision_id == sealed.revision_id
    # Title and output are projections of the claim, so they follow it.
    assert step.title == "The value is \\(x = 1\\)."
    assert step.output == step.content.claim
    assert step.reasoning_summary == step.content.derivation
    assert [
        (item.route_id, item.content_sha256, item.status)
        for item in shown.typeset_layers
    ] == [("route-main", typeset_content_sha256(layer), "compiled")]
    # The input view is not mutated in place.
    assert view.steps[0].content.claim == "The value is \\(x=1\\)."
    assert view.typeset_layers == []


def test_no_layer_is_the_same_view() -> None:
    view = _run()

    assert typeset_run_view(view, {}) is view
    # A layer for a route this view does not carry changes nothing either.
    assert typeset_run_view(view, {"route-elsewhere": _claim_layer("x = 1")}) is view


def test_statuses_that_are_not_repairs_stay_sealed() -> None:
    view = _run()
    for status in (STATUS_OK, STATUS_FAILED, STATUS_QUOTATION_VERBATIM):
        shown = typeset_run_view(
            view, {"route-main": _claim_layer("x = 1", status=status)}
        )
        step = shown.steps[0]
        assert step.content is not None
        assert step.content.claim == "The value is \\(x=1\\).", status
        # The layer is still declared - it produced this rendering - but no
        # step claims text it did not receive.
        assert step.typeset is False, status
        assert [item.route_id for item in shown.typeset_layers] == ["route-main"]


def test_a_step_on_two_routes_takes_the_first_route_that_has_a_layer() -> None:
    """The same Record and the same layer files always produce the same text."""

    view = _run()
    assert [route.id for route in view.routes] == ["route-main", "route-failed"]
    layers = {
        "route-main": _claim_layer("x = 1"),
        "route-failed": _claim_layer("x \\equiv 1", route_id="route-failed"),
    }

    shown = typeset_run_view(view, layers)
    reordered = typeset_run_view(view, dict(reversed(list(layers.items()))))

    assert shown.steps[0].content is not None
    assert shown.steps[0].content.claim == "The value is \\(x = 1\\)."
    assert reordered.steps[0].content == shown.steps[0].content
    # Both layers are named: they are part of what produced this view.
    assert [item.route_id for item in shown.typeset_layers] == [
        "route-main",
        "route-failed",
    ]
