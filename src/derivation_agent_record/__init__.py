"""Derivation Agent Record offline verification and replay package."""

from .model import ContractError, load_events
from .render import render_html
from .replay import replay_events

#: The stable Python surface, and nothing else. README.md ("Stability") promises
#: these four names and no more, so this list says the same thing rather than
#: implying a wider contract than the prose.
__all__ = [
    "ContractError",
    "load_events",
    "render_html",
    "replay_events",
]

# Internal, and importable by name for callers that already depend on them. None
# of it is covered by the stability promise above and any of it may change
# without notice. The two version pairs are re-exported together, so a caller
# comparing a record's `schema_version` against them never gets half the
# constants.
from .model import (  # noqa: F401
    CANONICAL_SCHEMA_VERSION,
    CANONICAL_SCHEMA_VERSION_1_1,
    EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION_1_1,
    EVENT_TYPES,
    canonical_json,
    compute_event_sha256,
    sha256_bytes,
    sha256_json,
    sha256_text,
    transcript_sha256,
)
from .replay import ReplayEngine, ReplayResult  # noqa: F401
