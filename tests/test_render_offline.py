"""The rendered view must be openable from ``file://`` with the network off.

An audit view that phones out to a CDN for a font or a script is not an audit
view: whoever serves that asset can change what the reader sees, and a reader
without a network sees nothing at all. This module is the machine check behind
that claim, applied both to the HTML committed under ``examples/`` and to HTML
rendered fresh from every record the suite can build.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import EVENT_V1, EVENT_V1_1, minimal_events, read_events, upgrade_to_v1_1

from derivation_agent_record import ContractError, load_events, render_html, replay_events

#: Anything that would make the browser fetch a second file: an absolute URL, a
#: protocol-relative URL, a CSS import, or a CSS ``url()`` reference.
EXTERNAL_REFERENCE = re.compile(r"""(?ix)
    (?: [a-z][a-z0-9+.-]* : / / )   # scheme://host
    | (?: (?<= ["'(] ) // )          # protocol-relative //host
    | @import
    | url \s* \(
    """)

#: Elements whose presence means the document is not self-contained. ``<style>``
#: is fine — it is inline. ``<link>``, ``<script>`` and ``<img>`` are not.
FETCHING_TAGS = frozenset({"link", "script", "img", "iframe", "object", "embed", "audio", "video", "source"})

#: Attributes that name something to fetch.
FETCHING_ATTRIBUTES = frozenset({"src", "href", "srcset", "data", "poster", "background"})

#: A file opened from ``file://`` gets no encoding header, so the document has
#: to declare its own or a reader on a non-UTF-8 default sees mojibake.
CHARSET = re.compile(r"""<meta\s+charset=["']?utf-8["']?""", re.IGNORECASE)


class _TagCollector(HTMLParser):
    """Collect start tags and attributes, and prove the document parses."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.fetching: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for name, value in attrs:
            if name.lower() in FETCHING_ATTRIBUTES and value:
                self.fetching.append((tag, name.lower(), value))


def _parse(html: str) -> _TagCollector:
    collector = _TagCollector()
    collector.feed(html)
    collector.close()
    return collector


def assert_self_contained(html: str, *, label: str) -> None:
    """One place that states, in full, what 'self-contained' means here."""

    assert html.startswith("<!doctype html>"), f"{label}: not a complete document"
    assert html.rstrip().endswith("</html>"), f"{label}: document is truncated"

    offenders = [match.group(0) for match in EXTERNAL_REFERENCE.finditer(html)]
    assert not offenders, f"{label}: external reference(s) {sorted(set(offenders))}"

    collector = _parse(html)
    fetching_tags = sorted({tag for tag in collector.tags if tag in FETCHING_TAGS})
    assert not fetching_tags, f"{label}: fetching element(s) {fetching_tags}"
    assert not collector.fetching, f"{label}: fetching attribute(s) {collector.fetching}"
    assert "meta" in collector.tags, f"{label}: no meta element at all"
    assert CHARSET.search(html), f"{label}: no declared encoding, so the bytes are at the reader's mercy"


def test_committed_viewers_are_self_contained(example_run: Path) -> None:
    html = (example_run / "viewer.html").read_text(encoding="utf-8")
    assert_self_contained(html, label=f"examples/runs/{example_run.name}/viewer.html")


def test_rendered_views_are_self_contained(example_run: Path) -> None:
    events = load_events(example_run / "events.jsonl")
    html = render_html(replay_events(events).canonical, events)
    assert_self_contained(html, label=f"render({example_run.name})")


@pytest.mark.parametrize("version", [EVENT_V1, EVENT_V1_1], ids=["v1", "v1_1"])
def test_minimal_records_render_to_a_self_contained_view(version: str) -> None:
    events = minimal_events(version=version)
    html = render_html(replay_events(events).canonical, events)
    assert_self_contained(html, label=f"render(minimal {version})")


def test_upgraded_v1_1_record_renders_to_a_self_contained_view(example_run: Path) -> None:
    events = upgrade_to_v1_1(read_events(example_run / "events.jsonl"))
    html = render_html(replay_events(events).canonical, events)
    assert_self_contained(html, label=f"render(v1.1 upgrade of {example_run.name})")


def test_the_view_shows_the_chain_head(example_run: Path) -> None:
    """A reader must be able to compare what they see against the record."""

    events = load_events(example_run / "events.jsonl")
    canonical = replay_events(events).canonical
    html = render_html(canonical, events)
    assert canonical["event_log"]["head_event_sha256"] in html


def test_payload_text_is_escaped_not_injected() -> None:
    """A record is untrusted input. Rendering it must not execute it.

    The renderer is handed a verified record here and the event payload is then
    replaced with hostile markup, because that is the situation that matters: a
    record can be verified and still contain whatever its author typed.
    """

    hostile = '</pre><script src="//example.invalid/x.js"></script>'
    events = minimal_events()
    canonical = replay_events(events).canonical
    events[0]["payload"] = {"note": hostile}

    html = render_html(canonical, events)
    assert "<script" not in html
    assert hostile not in html
    assert "&lt;script" in html, "the hostile text should be visible as text, not dropped"
    assert_self_contained(html, label="render(hostile payload)")


#: Every candidate status the replay engine can derive, v1 and v1.1 together.
CANDIDATE_STATUSES = ("eligible", "provisional", "blocked", "rejected", "conditional")

#: A class selector in the inline stylesheet: `.name` or `.a, .b {`.
STYLE_BLOCK = re.compile(r"<style>(.*?)</style>", re.DOTALL)
CLASS_SELECTOR = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)")


def defined_classes(html: str) -> set[str]:
    block = STYLE_BLOCK.search(html)
    assert block, "the page has no inline stylesheet"
    return set(CLASS_SELECTOR.findall(block.group(1)))


def test_every_candidate_status_has_a_colour(example_run: Path) -> None:
    """README promises a coloured badge per status; the stylesheet must have one.

    A status with no rule renders in the default grey, which is the colour of
    "this page does not colour that" — indistinguishable, to a reader, from a
    status the page has nothing to say about. `conditional` was exactly that.
    """

    html = (example_run / "viewer.html").read_text(encoding="utf-8")
    classes = defined_classes(html)

    missing = [status for status in CANDIDATE_STATUSES if status not in classes]
    assert not missing, f"no badge colour for {missing}"


def test_the_page_says_which_generation_of_the_contract_it_shows(example_run: Path) -> None:
    """A v1.1 record labelled `V1` tells the reader something that is not true."""

    events = load_events(example_run / "events.jsonl")
    result = replay_events(events)
    expected = {"derivation-agent-canonical-v1": "v1", "derivation-agent-canonical-v1.1": "v1.1"}[
        result.canonical["schema_version"]
    ]

    html = result.render()

    assert f"<title>Derivation Agent Record {expected} audit</title>" in html
    assert f"<h1>Derivation Agent Record {expected} audit</h1>" in html


def test_the_page_shows_the_derivation_and_the_check_results(example_run: Path) -> None:
    """The artifact a human reads has to contain the science, not only counters.

    Every step field and every check verdict used to exist on the page only
    inside the raw JSON of the event table, which is the record rather than a
    reading of it.
    """

    result = replay_events(load_events(example_run / "events.jsonl"))
    page = result.render()

    for step in result.canonical["step_revisions"]:
        assert step["step_revision_id"] in page
        for field in ("claim", "why", "source", "derivation", "scope"):
            assert escape(step["content"][field], quote=True) in page, f"{step['step_revision_id']}.{field}"
    for check in result.canonical["checks"]:
        assert check["check_id"] in page
        assert escape(check["completion_reason"] or check["reason"], quote=True) in page


def test_a_result_renders_itself(example_run: Path) -> None:
    """The one-argument form cannot be handed halves of two different replays."""

    events = load_events(example_run / "events.jsonl")
    result = replay_events(events)

    assert result.render() == render_html(result) == render_html(result.canonical, events)


def test_a_canonical_state_on_its_own_is_refused_with_a_usable_message(example_run: Path) -> None:
    """Half a replay is not a record, and the page may not be drawn from it."""

    canonical = replay_events(load_events(example_run / "events.jsonl")).canonical

    with pytest.raises(ContractError, match="ReplayResult"):
        render_html(canonical)


def test_a_mismatched_canonical_and_event_list_is_refused(example_run: Path) -> None:
    """The viewer is the artifact humans read; it may not be quietly incoherent.

    ``render_html`` takes two halves of one replay. Handed halves from different
    records it used to render them anyway, producing a page whose counters said
    one thing and whose event table showed another — with nothing on the page to
    say so. A page that lies silently is the worst failure this package has.
    """

    events = load_events(example_run / "events.jsonl")
    canonical = replay_events(events).canonical

    with pytest.raises(ContractError, match="events"):
        render_html(canonical, events[:5])

    with pytest.raises(ContractError):
        render_html(canonical, [])
