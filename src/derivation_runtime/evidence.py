"""Frozen checker sources and provider-independent semantic validation."""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from derivation_agent_record import canonical_json
from derivation_agent_record.model import ancestor_evidence_text

from .types import CheckOutput, EvidenceSource, RuntimeInvocationError


def evidence_catalog(
    snapshot: Mapping[str, Any],
    target_step_id: str,
    task_text: str,
    completion_requirements: Sequence[str] = (),
) -> tuple[EvidenceSource, ...]:
    """Every text the checker may quote.

    The completion requirements belong here because the checker prompt already
    renders them: leaving them out made the visible material a strict superset
    of the quotable sources, so a checker that quoted a requirement verbatim was
    failed as invalid output and its whole verdict was discarded.  Each
    requirement is its own source so a quote still has to sit inside one of
    them, with no match spanning a boundary.
    """
    steps = {item["step_revision_id"]: item for item in snapshot["step_revisions"]}
    branches = {item["branch_id"]: item for item in snapshot["branches"]}
    branch = branches[steps[target_step_id]["branch_id"]]
    ids = branch["step_revision_ids"]
    sources = []
    record_version = (
        "1.1"
        if snapshot["schema_version"] == "derivation-agent-canonical-v1.1"
        else "1.0"
    )
    for step_id in ids[: ids.index(target_step_id) + 1]:
        content = steps[step_id]["content"]
        sources.extend(
            (
                EvidenceSource(
                    "ancestor_quote",
                    step_id,
                    ancestor_evidence_text(content, record_version=record_version),
                ),
                EvidenceSource("scope_quote", step_id, content["scope"]),
            )
        )
    while branch is not None:
        sources.append(
            EvidenceSource(
                "hypothesis_quote", branch["branch_id"], branch["hypothesis"]["text"]
            )
        )
        branch = branches.get(branch["parent_branch_id"])
    task_id = snapshot["run"]["task"]["id"]
    # The record requires task evidence to carry the run's task id, so the
    # requirements share that id and appear as separate texts under it.  They
    # are kept apart rather than concatenated so no quote can match by spanning
    # the boundary between two of them.
    sources.append(EvidenceSource("task_constraint_quote", task_id, task_text))
    sources.extend(
        EvidenceSource("task_constraint_quote", task_id, text)
        for text in completion_requirements
    )
    sources.extend(
        EvidenceSource(item["kind"], item["source_id"], item["text"])
        for item in snapshot.get("source_evidence", [])
    )
    return tuple(sources)


def _quotes_source(text: str, quote: str) -> bool:
    """Whether the quote is a contiguous copy of the source.

    Byte-exact containment is what the checker is asked for and what is tried
    first.  The one tolerated difference is whitespace reflow - line breaks and
    runs of spaces - because that alone was discarding whole checks that were
    otherwise substantive: a checker draft rejected here loses its verdict and
    its reasoning, which live only in the failed call's partial output.  Every
    other character, including all Markdown, LaTeX and Unicode, must still match
    contiguously and in order, so a paraphrase, a rewritten formula or an
    invented span is still rejected.
    """

    if quote in text:
        return True
    return " ".join(quote.split()) in " ".join(text.split())


def _source_span(text: str, quote: str) -> str | None:
    """The contiguous span of ``text`` that ``quote`` copies, or None.

    An exact copy is its own span.  A quote that matches only after whitespace
    reflow (see ``_quotes_source``) maps back to the source's own characters:
    every non-whitespace character of the quote is the same character of the
    source, in order, and every whitespace run between them is the source's
    whitespace run at that place.  The span therefore is a byte-exact substring
    of the source whose whitespace-normalized form equals the quote's, which is
    what Record replay requires of recorded evidence.  The first occurrence is
    used, so the result is deterministic.
    """

    if quote in text:
        return quote
    normalized_quote = " ".join(quote.split())
    if not normalized_quote:
        return None
    tokens: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(text):
        if character.isspace():
            if start is not None:
                tokens.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        tokens.append((start, len(text)))
    # normalized_starts[k] is where token k begins in " ".join(tokens).
    normalized_starts: list[int] = []
    offset = 0
    for token_start, token_end in tokens:
        normalized_starts.append(offset)
        offset += token_end - token_start + 1
    normalized_text = " ".join(text[a:b] for a, b in tokens)
    found = normalized_text.find(normalized_quote)
    if found < 0:
        return None

    def source_index(normalized_index: int) -> int:
        token = bisect_right(normalized_starts, normalized_index) - 1
        return tokens[token][0] + normalized_index - normalized_starts[token]

    # The normalized quote begins and ends with a non-whitespace character, so
    # both ends of the match land inside source tokens.
    first = source_index(found)
    last = source_index(found + len(normalized_quote) - 1)
    return text[first : last + 1]


def validate_check_output(
    output: CheckOutput, sources: tuple[EvidenceSource, ...]
) -> CheckOutput:
    """Validate a Checker output and return it with recordable evidence.

    The returned output is ``output`` itself when every quote is an exact copy,
    so exact evidence is recorded byte-identically.  A quote accepted only for
    whitespace reflow is replaced by the exact source span it copies (see
    ``_source_span``), so the guard and Record replay, which requires exact
    containment, agree on every evidence kind.  The host records the returned
    output in ``check_completed``; the finished Checker call keeps the body the
    provider returned.
    """

    body = canonical_json(
        {
            "verdict": output.verdict,
            "reason": output.reason,
            "evidence": [item.to_record() for item in output.evidence],
        }
    )

    def invalid(message: str) -> None:
        raise RuntimeInvocationError(
            "invalid_model_output",
            message,
            partial_output=output.raw_output if output.raw_output is not None else body,
            retryable=True,
        )

    if output.verdict not in {"ok", "objection", "hard_defect"}:
        invalid("Checker did not return a scientific verdict.")
    if not isinstance(output.reason, str) or not output.reason.strip():
        invalid("Checker reason must be non-empty text.")
    if output.verdict == "hard_defect" and not output.evidence:
        invalid("hard_defect requires quoted evidence.")
    catalog: dict[tuple[str, str], list[str]] = {}
    for source in sources:
        catalog.setdefault((source.kind, source.source_id), []).append(source.text)
    recorded = []
    for item in output.evidence:
        texts = catalog.get((item.kind, item.source_id))
        if texts is None:
            invalid("Checker evidence kind/source is outside the frozen catalog.")
        if not isinstance(item.quote, str) or not item.quote.strip():
            invalid("Checker evidence quote is not an exact substring of its source.")
        if not any(_quotes_source(text, item.quote) for text in texts):
            invalid("Checker evidence quote is not an exact substring of its source.")
        if any(item.quote in text for text in texts):
            recorded.append(item)
            continue
        span = next(
            span
            for span in (_source_span(text, item.quote) for text in texts)
            if span is not None
        )
        recorded.append(replace(item, quote=span))
    if all(new is old for new, old in zip(recorded, output.evidence)):
        return output
    return replace(output, evidence=tuple(recorded))
