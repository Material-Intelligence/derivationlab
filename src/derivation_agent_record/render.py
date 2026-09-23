"""Static, read-only HTML audit view for a Derivation Agent Record."""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .model import ContractError

if TYPE_CHECKING:
    # For the annotation only. At runtime the two modules would import each
    # other: a result renders itself, and the renderer reads a result.
    from .replay import ReplayResult

#: ``canonical["schema_version"]`` is the only place the page may learn which
#: generation of the contract it is showing. Hardcoding a version here is how
#: the viewer came to label v1.1 records as v1.
_RECORD_VERSION_LABELS = {
    "derivation-agent-canonical-v1": "v1",
    "derivation-agent-canonical-v1.1": "v1.1",
}

#: The five fields of a step revision, in the order the contract states them.
_STEP_FIELDS = ("claim", "why", "source", "derivation", "scope")


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def render_html(
    record: ReplayResult | Mapping[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render one verified record as a self-contained read-only HTML page.

    Call it with a :class:`~derivation_agent_record.replay.ReplayResult` and
    nothing else: a result carries both halves of its own replay, so the two
    cannot be mismatched.

    The two-argument form ``render_html(result.canonical, events)`` is kept for
    callers written against the older signature. It is the form that made the
    mismatch expressible in the first place — the counters at the top of the
    page come from the canonical state and the table at the bottom is the event
    log, so halves from different records produce a page that is internally
    inconsistent and says so nowhere. The two checks below refuse that pair
    rather than render it.
    """

    if events is None:
        try:
            canonical, events = record.canonical, record.events  # type: ignore[union-attr]
        except AttributeError as exc:
            raise ContractError(
                "render: pass a ReplayResult, or a canonical state and the events it was replayed from"
            ) from exc
    else:
        canonical = record  # type: ignore[assignment]

    log = canonical["event_log"]
    if log["event_count"] != len(events):
        raise ContractError(
            f"render: canonical state describes {log['event_count']} events but {len(events)} were given"
        )
    if not events or log["head_event_sha256"] != events[-1]["event_sha256"]:
        raise ContractError("render: canonical head hash is not the last event's hash")

    summary = canonical["summary"]
    branch_rows = []
    for branch in canonical["branches"]:
        provenance = branch["provenance"]
        branch_rows.append(
            "<tr>"
            f"<td><code>{_esc(branch['branch_id'])}</code></td>"
            f"<td>{_esc(branch['status'])}</td>"
            f"<td>{_esc(branch['created_reason'])}</td>"
            f"<td>{_esc(provenance['content_class'])}</td>"
            f"<td>{_esc(len(branch['step_revision_ids']))}</td>"
            "</tr>"
        )

    candidate_cards = []
    for candidate in canonical["candidates"]:
        candidate_cards.append(
            '<article class="card">'
            f"<h3><code>{_esc(candidate['candidate_id'])}</code> "
            f"<span class=\"badge {_esc(candidate['status'])}\">{_esc(candidate['status'])}</span></h3>"
            f"<p>Branch <code>{_esc(candidate['branch_id'])}</code>; tip "
            f"<code>{_esc(candidate['tip_step_revision_id'])}</code>.</p>"
            f"<p>Transcript SHA-256: <code>{_esc(candidate['transcript_sha256'])}</code></p>"
            f"<p>Provenance: <strong>{_esc(candidate['provenance']['content_class'])}</strong>; "
            f"human_touched=<strong>{_esc(candidate['provenance']['human_touched'])}</strong>.</p>"
            f"<p>Required checks: {_esc(', '.join(candidate['required_check_ids']) or 'none')}</p>"
            "</article>"
        )

    steps_by_id = {step["step_revision_id"]: step for step in canonical["step_revisions"]}
    step_sections = []
    for branch in canonical["branches"]:
        cards = []
        for step_id in branch["step_revision_ids"]:
            step = steps_by_id[step_id]
            origin = step["origin"]
            source_id = origin["model_call_id"] or origin["human_action_id"]
            replaces = step["replaces_step_revision_id"]
            fields = "".join(
                f'<p class="label">{name}</p><pre class="field">{_esc(step["content"][name])}</pre>'
                for name in _STEP_FIELDS
            )
            cards.append(
                '<article class="card">'
                f"<h4><code>{_esc(step_id)}</code> "
                f'<span class="small">slot {_esc(step["step_slot"])}, revision {_esc(step["revision"])}'
                + (f", replaces <code>{_esc(replaces)}</code>" if replaces else "")
                + "</span></h4>"
                + fields
                + f'<p class="small">Output SHA-256 <code>{_esc(step["output_sha256"])}</code>; '
                f'sealed by {_esc(origin["kind"])} <code>{_esc(source_id)}</code>.</p>'
                "</article>"
            )
        step_sections.append(
            f"<h3>Branch <code>{_esc(branch['branch_id'])}</code></h3>"
            + ("".join(cards) if cards else "<p>No step revisions on this branch.</p>")
        )
    steps_html = "".join(step_sections) or "<p>No step revisions.</p>"

    check_rows = []
    for check in canonical["checks"]:
        verdict = check["verdict"] or check["state"]
        quotes = "".join(
            f"<li><code>{_esc(item['kind'])}</code> "
            f"<code>{_esc(item['source_id'])}</code>: {_esc(item['quote'])}</li>"
            for item in check["evidence"]
        )
        check_rows.append(
            "<tr>"
            f"<td><code>{_esc(check['check_id'])}</code></td>"
            f"<td><code>{_esc(check['target_step_revision_id'])}</code></td>"
            f"<td><span class=\"badge {_esc(verdict)}\">{_esc(verdict)}</span>"
            + (" <span class=\"small\">required</span>" if check["required_for_candidate"] else "")
            + "</td>"
            f"<td>{_esc(check['completion_reason'] or check['reason'])}</td>"
            f"<td>{f'<ul>{quotes}</ul>' if quotes else ''}</td>"
            "</tr>"
        )

    judgement_cards = []
    for judgement in canonical["judgements"]:
        judgement_cards.append(
            '<article class="card">'
            f"<h3><code>{_esc(judgement['judgement_id'])}</code> "
            f"<span class=\"badge {_esc(judgement['verdict'] or judgement['state'])}\">"
            f"{_esc(judgement['verdict'] or judgement['state'])}</span></h3>"
            f"<p>Candidate <code>{_esc(judgement['candidate_id'])}</code>; "
            f"reason: {_esc(judgement['completion_reason'] or judgement['reason'])}</p>"
            "</article>"
        )

    selection = canonical["selections"][0] if canonical["selections"] else None
    if selection:
        selection_html = (
            '<article class="card selected">'
            f"<h3>Selected submission: <code>{_esc(selection['candidate_id'])}</code></h3>"
            f"<p>Judgement <code>{_esc(selection['judgement_id'])}</code>; transcript "
            f"<code>{_esc(selection['candidate_transcript_sha256'])}</code>.</p>"
            f"<p>human_touched=<strong>{_esc(selection['provenance']['human_touched'])}</strong>; "
            f"reason: {_esc(selection['reason'])}</p>"
            "</article>"
        )
    else:
        selection_html = "<p>No selected submission.</p>"

    event_rows = []
    for event in events:
        event_rows.append(
            "<tr>"
            f"<td>{_esc(event['seq'])}</td>"
            f"<td><code>{_esc(event['event_id'])}</code></td>"
            f"<td>{_esc(event['type'])}</td>"
            f"<td>{_esc(event['actor']['kind'])}:{_esc(event['actor']['id'])}</td>"
            f"<td><code>{_esc(event['event_sha256'][:12])}</code></td>"
            f"<td><details><summary>payload</summary><pre>{_json(event['payload'])}</pre></details></td>"
            "</tr>"
        )

    version = _RECORD_VERSION_LABELS.get(canonical["schema_version"], canonical["schema_version"])
    heading = f"Derivation Agent Record {version} audit"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(heading)}</title>
<style>
:root {{ font-family: ui-sans-serif, system-ui, sans-serif; color: #171717; background: #f5f5f5; }}
body {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
h1, h2 {{ letter-spacing: -0.02em; }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
code {{ overflow-wrap: anywhere; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .75rem; }}
.metric, .card {{ background: white; border: 1px solid #ddd; border-radius: .7rem; padding: 1rem; margin: .7rem 0; }}
.metric strong {{ display: block; font-size: 1.6rem; }}
.selected {{ border-width: 2px; }}
table {{ width: 100%; border-collapse: collapse; background: white; font-size: .9rem; }}
th, td {{ border: 1px solid #ddd; padding: .55rem; text-align: left; vertical-align: top; }}
pre {{ white-space: pre-wrap; max-width: 50rem; }}
.badge {{ display: inline-block; padding: .15rem .45rem; border-radius: 999px; background: #e5e5e5; font-size: .8rem; }}
.eligible, .pass, .ok {{ background: #d1fae5; }}
.rejected, .fail, .hard_defect {{ background: #fee2e2; }}
.provisional, .blocked, .conditional, .near_pass, .objection, .instrument_failure {{ background: #fef3c7; }}
.small {{ color: #555; font-size: .9rem; }}
.label {{ color: #555; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; margin: .6rem 0 .1rem; }}
.field {{ margin: 0; }}
ul {{ margin: 0; padding-left: 1.1rem; }}
</style>
</head>
<body>
<h1>{_esc(heading)}</h1>
<p class="small">Read-only view derived from the append-only event log. It is not a control surface.</p>
<div class="metrics">
<div class="metric"><strong>{summary['branch_count']}</strong>branches</div>
<div class="metric"><strong>{summary['step_revision_count']}</strong>step revisions</div>
<div class="metric"><strong>{summary['model_call_count']}</strong>model calls</div>
<div class="metric"><strong>{summary['check_count']}</strong>checks</div>
<div class="metric"><strong>{summary['candidate_count']}</strong>candidates</div>
<div class="metric"><strong>{summary['judgement_count']}</strong>judgements</div>
</div>
<h2>Selected submission</h2>
{selection_html}
<h2>Branches</h2>
<table><thead><tr><th>Branch</th><th>Status</th><th>Created reason</th><th>Content provenance</th><th>Steps</th></tr></thead>
<tbody>{''.join(branch_rows)}</tbody></table>
<h2>Step revisions</h2>
<p class="small">Every step on every branch route, in transcript order. Inherited steps appear on each route that carries them.</p>
{steps_html}
<h2>Checks</h2>
<table><thead><tr><th>Check</th><th>Target step</th><th>Verdict</th><th>Reason</th><th>Evidence</th></tr></thead>
<tbody>{''.join(check_rows)}</tbody></table>
<h2>Candidates</h2>
{''.join(candidate_cards)}
<h2>Judgements</h2>
{''.join(judgement_cards)}
<h2>Event hash chain</h2>
<p class="small">Head: <code>{_esc(canonical['event_log']['head_event_sha256'])}</code></p>
<table><thead><tr><th>Seq</th><th>Event</th><th>Type</th><th>Actor</th><th>Hash</th><th>Payload</th></tr></thead>
<tbody>{''.join(event_rows)}</tbody></table>
</body>
</html>
"""
