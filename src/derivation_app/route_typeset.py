"""Typeset layer of a finished route: compile it, repair it, never touch Record.

When a route completes, its recorded formulas are compiled once, as one
document, with the locked report engine. Formulas that do not compile are
handled in two different ways, because they are two different things.

*Quotations* - the ``source`` field and prose math that occurs verbatim in a
cited registered source - carry the cited author's notation and are never a
repair target: no model is ever asked to correct one. (They are not invisible
to the model: a repair or review request carries the whole recorded step, so a
quotation can appear there as context for a formula that is being repaired.)
They are typeset with a typeset-only macro expansion (the same attribution
rules normalization uses, see
:func:`derivation_runtime.formula_normalization.expand_math_for_typesetting`),
and a quotation that still fails is shown verbatim with a marker.

*Everything else* goes to one repair round: a fresh Writer thread with no
literature, the step's recorded fields and each failing formula with its
compiler error, answering with corrected LaTeX per formula id. A correction
that only changes syntax - the same ordered sequence of identifiers, operators
and commands, except the ones the compiler named, with braces ignored and
sizing delimiters compared separately so a missing one may be added but none
may change shape - is accepted by the host guard. A correction beyond that is
put to a fresh Checker thread, which decides only
whether it states the same mathematics; a correction it refuses is dropped and
the recorded formula is kept and flagged. The whole route is recompiled and the
loop repeats at most :data:`MAX_REPAIR_ROUNDS` times, after which the layer is
delivered with flags for the formulas that still fail.

The result is a hash-bound file under ``<run>/typeset/``. It is *not* the
scientific Record: no Record event is written, the recorded content never
changes, and a consumer uses the layer only while its hashes still match the
Record it was built from.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from derivation_runtime.formula_normalization import (
    NORMALIZER_VERSION,
    SourceMacroTables,
    expand_math_for_typesetting,
    normalize_step_fields,
    restore_control_characters,
    typeset_expansion_scope,
)
from derivation_runtime.formula_validation import (
    EngineWhitelist,
    load_engine_whitelist,
    math_fragments,
)
from derivation_runtime.prompts import (
    formula_repair_user_prompt,
    formula_review_user_prompt,
)
from derivation_runtime.types import (
    FormulaEquivalenceRequest,
    FormulaRepairItem,
    FormulaRepairRequest,
    FormulaRepairStep,
    FormulaReviewItem,
    RunConfig,
    RuntimeInvariantError,
    RuntimeInvocationError,
    StepContent,
    StepSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .reporting import TectonicRunner

# ``.formula_compiler`` and ``.reporting`` are imported where they are used:
# the report exporter imports this module for the consumer side, and a compiler
# import at module scope would close that circle.

TYPESET_SCHEMA_VERSION = "derivationlab-route-typeset-v1"
TYPESET_DIRECTORY = "typeset"
CALL_JOURNAL_NAME = "model_calls.jsonl"
#: Whole-route recompiles after a repair round.
MAX_REPAIR_ROUNDS = 3

STATUS_OK = "ok"
STATUS_REPAIRED = "repaired"
STATUS_REPAIRED_REVIEWED = "repaired_reviewed"
STATUS_QUOTATION_EXPANDED = "quotation_expanded"
STATUS_QUOTATION_VERBATIM = "quotation_verbatim"
STATUS_FAILED = "failed"
#: No verdict: the compiler itself was unavailable, so nothing was verified.
STATUS_NOT_COMPILED = "not_compiled"
#: The recorded body needed nothing but deterministic normalization, and the
#: normalized body is what compiled. A run sealed under ``formula-v2`` never
#: produces it - there the Record content *is* the normalized content, so the
#: two are the same text and the status is :data:`STATUS_OK`. It exists for a
#: layer bound to a ``formula-v1`` Record, whose sealed body still carries the
#: JSON-transport damage the normalizer undoes.
STATUS_NORMALIZED = "normalized"

#: Statuses whose ``typeset`` text a consumer renders instead of the recorded one.
SUBSTITUTED_STATUSES = frozenset(
    {
        STATUS_REPAIRED,
        STATUS_REPAIRED_REVIEWED,
        STATUS_QUOTATION_EXPANDED,
        STATUS_NORMALIZED,
    }
)

LAYER_STATUS_COMPILED = "compiled"
LAYER_STATUS_WITH_FAILURES = "compiled_with_failures"
LAYER_STATUS_INFRASTRUCTURE = "infrastructure_error"
LAYER_STATUS_ERROR = "typeset_error"

_STEP_FIELDS = ("claim", "why", "source", "derivation", "scope")
_MATH_DELIMITERS = re.compile(r"(?<!\\)\$|\\\(|\\\)|\\\[|\\\]")
_COMMAND_IN_TEXT = re.compile(r"\\([A-Za-z@]+)")
_CONTEXT_LINE = re.compile(r"^l\.(\d+)(\s.*)?$")
_TOKEN = re.compile(r"\\([A-Za-z@]+)|\\(.)|([A-Za-z0-9])|(\S)", re.DOTALL)
#: How :func:`_compiler_message` attaches TeX's echoed source line, and how
#: :func:`error_commands` takes it apart again. The echo is source text, so it
#: is never a source of licensed command names (see ``_licensed_from_message``).
_ECHOED_CONTEXT = re.compile(r"\s*\(context:\s*(?P<echo>.*)\)\s*\Z", re.DOTALL)
_ECHOED_SOURCE = re.compile(r"\Al\.\d+[ \t](?P<consumed>.*)\Z", re.DOTALL)
_TRAILING_COMMAND = re.compile(r"\\([A-Za-z@]+)[ \t]*\Z")
_UNDEFINED_CONTROL_SEQUENCE = re.compile(r"Undefined control sequence")
#: ``compiler_missing_glyphs`` reformats a missing glyph as this; it names a
#: character and a font, never a command, so nothing in it is ever licensed.
_MISSING_GLYPH_MESSAGE = re.compile(r"\AMissing character U\+")

# Letter-like commands: identifiers, not structure.
_LETTER_COMMANDS = frozenset(
    {
        "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
        "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron",
        "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau", "upsilon", "phi",
        "varphi", "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi",
        "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega", "ell", "hbar", "imath",
        "jmath", "wp", "partial", "nabla", "infty", "emptyset", "varnothing",
    }
)
# Presentation only: adding or removing them cannot change the mathematics.
_LAYOUT_COMMANDS = frozenset(
    {
        "left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr",
        "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr", "displaystyle",
        "textstyle", "scriptstyle", "scriptscriptstyle", "limits", "nolimits",
        "quad", "qquad", "enspace", "thinspace", "medspace", "thickspace",
        ",", ";", ":", "!", " ", "\\",
    }
)
_RELATION_COMMANDS = frozenset(
    {
        "le", "leq", "ge", "geq", "ne", "neq", "equiv", "approx", "sim", "simeq",
        "cong", "propto", "to", "gets", "mapsto", "rightarrow", "leftarrow",
        "Rightarrow", "Leftarrow", "leftrightarrow", "Leftrightarrow", "longmapsto",
        "longrightarrow", "longleftarrow", "ll", "gg", "in", "ni", "notin", "subset",
        "supset", "subseteq", "supseteq", "prec", "succ", "preceq", "succeq",
        "asymp", "doteq", "models", "perp", "parallel", "vdash", "dashv",
    }
)
_RELATION_SYMBOLS = frozenset({"=", "<", ">"})
# Braces carry no mathematics of their own: a missing or surplus brace is the
# archetypal syntax-only repair, and every grouping they express is already
# visible in the order of the tokens they group. They are the one class of
# token the profile drops; every other non-layout character is significant.
_GROUPING_SYMBOLS = frozenset({"{", "}"})
# A delimiter that ``\left``/``\right``/``\middle`` governs is sizing: an
# unbalanced one is what stops the engine, and closing it is the archetypal
# syntax repair, so it is taken out of the ordered structure - otherwise adding
# the missing ``\right)`` would read as an inserted token. It is *not*
# forgotten: the shapes are compared separately (see ``_is_subsequence``), so a
# repair may add a missing delimiter but may not change ``(`` into ``[``. A
# bare ``(`` is neither: adding one around ``a + b`` changes what it means.
_SIZING_COMMANDS = frozenset({"left", "right", "middle"})
_SIZED_DELIMITERS = frozenset("()[]|./<>{}")


# ---------------------------------------------------------------------------
# Layer files


def typeset_directory(run_directory: Path) -> Path:
    return Path(run_directory) / TYPESET_DIRECTORY


def layer_path(run_directory: Path, route_id: str) -> Path:
    """The layer file of one route; a route id is a safe path component."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", route_id) or (
        ".." in route_id
    ):
        raise ValueError(f"unsafe route id for a typeset layer: {route_id!r}")
    return typeset_directory(run_directory) / f"{route_id}.json"


def load_typeset_layer(run_directory: Path, route_id: str) -> dict[str, Any] | None:
    """Read one layer file, or return None when it is absent or unreadable."""

    try:
        path = layer_path(run_directory, route_id)
    except ValueError:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != (
        TYPESET_SCHEMA_VERSION
    ):
        return None
    return value


def write_typeset_layer(run_directory: Path, layer: Mapping[str, Any]) -> Path:
    path = layer_path(run_directory, layer["route_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.pending")
    encoded = (
        json.dumps(dict(layer), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    with pending.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)
    return path


def _step_fragments(content: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "field": fragment.field,
            "index": fragment.formula_index,
            "start": fragment.start,
            "end": fragment.end,
            "kind": fragment.kind,
            "value": fragment.value,
        }
        for fragment in math_fragments({name: content[name] for name in _STEP_FIELDS})
    ]


def verify_typeset_layer(
    layer: Mapping[str, Any],
    *,
    run_id: str,
    route_id: str,
    steps: Sequence[Mapping[str, Any]],
) -> bool:
    """Is this layer still the one the current Record content produces?

    ``steps`` are the route's steps in order, each with ``step_revision_id``,
    ``output_sha256`` and the five recorded ``content`` fields. Identity, step
    lineage, content hashes and every recorded formula occurrence must agree;
    anything else means the layer belongs to an earlier Record and is ignored.
    """

    if (
        layer.get("schema_version") != TYPESET_SCHEMA_VERSION
        or layer.get("run_id") != run_id
        or layer.get("route_id") != route_id
    ):
        return False
    recorded = layer.get("steps")
    if not isinstance(recorded, list) or len(recorded) != len(steps):
        return False
    for item, step in zip(recorded, steps, strict=True):
        if (
            not isinstance(item, Mapping)
            or item.get("step_revision_id") != step["step_revision_id"]
            or item.get("output_sha256") != step["output_sha256"]
        ):
            return False
    entries = layer.get("formulas")
    if not isinstance(entries, list):
        return False
    expected = [
        (step["step_revision_id"], fragment)
        for step in steps
        for fragment in _step_fragments(step["content"])
    ]
    if len(entries) != len(expected):
        return False
    for entry, (step_revision_id, fragment) in zip(entries, expected, strict=True):
        if not isinstance(entry, Mapping):
            return False
        if (
            entry.get("step_revision_id") != step_revision_id
            or entry.get("field") != fragment["field"]
            or entry.get("index") != fragment["index"]
            or entry.get("start") != fragment["start"]
            or entry.get("end") != fragment["end"]
            or entry.get("original") != fragment["value"]
            or not isinstance(entry.get("typeset"), str)
            or not isinstance(entry.get("status"), str)
        ):
            return False
    return True


def verified_layers(
    run_directory: Path,
    *,
    run_id: str,
    routes: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Load the layers of ``routes`` that still match the Record they cite.

    ``routes`` maps a route id to that route's steps, each with
    ``step_revision_id``, ``output_sha256`` and the recorded ``content``. A
    layer that fails :func:`verify_typeset_layer` is left out, so a consumer
    that finds nothing renders exactly what it rendered before.
    """

    found: dict[str, dict[str, Any]] = {}
    for route_id, steps in routes.items():
        layer = load_typeset_layer(run_directory, route_id)
        if layer is not None and verify_typeset_layer(
            layer, run_id=run_id, route_id=route_id, steps=steps
        ):
            found[route_id] = layer
    return found


def typeset_layer_identity(layer: Mapping[str, Any]) -> dict[str, Any]:
    """Everything about a layer that can change what a consumer renders.

    Timings, evidence paths and model-call audits are deliberately left out:
    they differ between two builds of the same result and say nothing about the
    delivered text.
    """

    return {
        "schema_version": layer.get("schema_version"),
        "run_id": layer.get("run_id"),
        "route_id": layer.get("route_id"),
        "branch_id": layer.get("branch_id"),
        "status": layer.get("status"),
        "normalizer_version": layer.get("normalizer_version"),
        "engine": layer.get("engine"),
        "engine_whitelist": layer.get("engine_whitelist"),
        "accepted_with_format_issues": layer.get("accepted_with_format_issues"),
        "steps": [
            {
                "step_revision_id": step.get("step_revision_id"),
                "output_sha256": step.get("output_sha256"),
                "format_issues": step.get("format_issues", []),
            }
            for step in layer.get("steps", ())
        ],
        "formulas": [
            {
                "formula_id": entry.get("formula_id"),
                "status": entry.get("status"),
                "original": entry.get("original"),
                "typeset": entry.get("typeset"),
            }
            for entry in layer.get("formulas", ())
        ],
    }


def typeset_content_sha256(layer: Mapping[str, Any]) -> str:
    """Content hash of one typeset layer, for a consumer's own identity."""

    return _sha256_text(
        json.dumps(
            typeset_layer_identity(layer),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def typeset_lookup(layer: Mapping[str, Any]) -> dict[tuple[str, str, int], Mapping]:
    """Index one verified layer by (step_revision_id, field, formula index)."""

    return {
        (entry["step_revision_id"], entry["field"], entry["index"]): entry
        for entry in layer["formulas"]
    }


def typeset_field_text(
    value: str,
    entries: Sequence[Mapping[str, Any]],
) -> str:
    """Replace math fragments of one field with their typeset bodies.

    Only statuses in :data:`SUBSTITUTED_STATUSES` change the text; every other
    fragment, a quotation shown verbatim included, is returned unchanged. A
    consumer that wants to mark the untouched ones reads their status from the
    layer itself - this function returns text, not markup.
    """

    by_index = {entry["index"]: entry for entry in entries}
    out = value
    for fragment in sorted(
        math_fragments({"field": value}),
        key=lambda item: item.start,
        reverse=True,
    ):
        entry = by_index.get(fragment.formula_index)
        if entry is None or entry.get("original") != fragment.value:
            continue
        if entry.get("status") not in SUBSTITUTED_STATUSES:
            continue
        out = out[: fragment.start] + entry["typeset"] + out[fragment.end :]
    return out


# ---------------------------------------------------------------------------
# Host guard: is a correction syntax-only?


def _tokenize(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split one math body into its ordered structure and its sizing delimiters.

    The first sequence is everything that means something: identifiers,
    operators, relations, structure characters (``^ _ + - / | ( ) [ ] ,`` ...)
    and non-layout commands, in the order they are written. Order is what
    separates ``\\frac{a}{b}`` from ``\\frac{b}{a}``, ``\\int_0^1`` from
    ``\\int_1^0`` and ``a_1b_2`` from ``a_2b_1`` - none of which a multiset of
    the same tokens can see. Braces and layout commands are dropped outright:
    they are presentation.

    The second sequence is the delimiters ``\\left``/``\\right``/``\\middle``
    govern, in order, kept out of the structure so that closing an unbalanced
    pair does not read as an inserted token, and compared on their own so that
    their *shape* stays visible.
    """

    tokens: list[str] = []
    delimiters: list[str] = []
    sizing = False
    for match in _TOKEN.finditer(text):
        word, symbol, alphanumeric, other = match.groups()
        governed, sizing = sizing, False
        if word is not None:
            if word in _SIZING_COMMANDS:
                sizing = True
                continue
            if word in _LAYOUT_COMMANDS:
                continue
            tokens.append(f"\\{word}")
        elif symbol is not None:
            if governed and symbol in _SIZED_DELIMITERS:
                delimiters.append(f"\\{symbol}")
                continue
            if symbol in _LAYOUT_COMMANDS:
                continue
            tokens.append(f"\\{symbol}")
        elif alphanumeric is not None:
            tokens.append(alphanumeric)
        elif other is not None:
            if governed and other in _SIZED_DELIMITERS:
                delimiters.append(other)
                continue
            if other in _GROUPING_SYMBOLS:
                continue
            tokens.append(other)
    return tuple(tokens), tuple(delimiters)


def _profile(text: str) -> tuple[str, ...]:
    """The ordered structure of one math body; see :func:`_tokenize`."""

    return _tokenize(text)[0]


def _is_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    """Does ``haystack`` contain ``needle`` in order, additions allowed?

    This is how sizing delimiters are compared: a repair may *add* the
    delimiter the engine stopped on, and every delimiter the recorded formula
    already wrote must still be there, in the same order and with the same
    shape. ``\\left( a`` -> ``\\left( a \\right)`` passes; ``\\left( a
    \\right)`` -> ``\\left[ a \\right]`` does not.
    """

    remaining = iter(haystack)
    return all(item in remaining for item in needle)


def _counts(tokens: Sequence[str]) -> tuple[Counter[str], Counter[str], int]:
    """Identifier and command multisets and the relation count of a token list.

    Derived from the same tokens the structural comparison uses, so a licensed
    command is masked here too and cannot make the two disagree.
    """

    identifiers: Counter[str] = Counter()
    commands: Counter[str] = Counter()
    relations = 0
    for token in tokens:
        if token == LICENSED_TOKEN:
            continue
        if token.startswith("\\"):
            name = token[1:]
            if name in _LETTER_COMMANDS:
                identifiers[token] += 1
                continue
            commands[name] += 1
            if name in _RELATION_COMMANDS:
                relations += 1
        elif token.isalnum():
            identifiers[token] += 1
        elif token in _RELATION_SYMBOLS:
            relations += 1
    return identifiers, commands, relations


#: Stands in for a command the compiler error licensed this repair to change.
LICENSED_TOKEN = "\\<licensed>"


def _without_allowed(
    tokens: Sequence[str], allowed: frozenset[str] | set[str]
) -> tuple[str, ...]:
    """Mask the commands the compiler licensed, without deleting their place.

    A licensed command may be replaced by another name or dropped in favour of
    one - that is what the licence is for - but it still occupied a position in
    the expression. Masking rather than removing keeps ``\\sqrt{a}+\\sqrt{b}``
    distinguishable from ``\\sqrt{a+b}`` even when the compiler happened to
    name ``\\sqrt``, at the price of sending an outright deletion to review.
    """

    return tuple(
        LICENSED_TOKEN if (token.startswith("\\") and token[1:] in allowed) else token
        for token in tokens
    )


def _first_difference(
    left: Sequence[str], right: Sequence[str]
) -> dict[str, Any] | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return {"index": index, "original": a, "corrected": b}
    if len(left) != len(right):
        index = min(len(left), len(right))
        return {
            "index": index,
            "original": left[index] if index < len(left) else None,
            "corrected": right[index] if index < len(right) else None,
        }
    return None


def split_compiler_message(message: str) -> tuple[str, str | None]:
    """Separate TeX's diagnosis from the source line the engine echoed back.

    :func:`_compiler_message` appends ``(context: l.NN ...)`` because the
    Writer needs to see where the error is. That echo is *source text*: every
    command standing on the failing line appears in it, none of which the
    engine complained about. Splitting it off is what keeps the licence in
    :func:`error_commands` down to the one offending command.
    """

    match = _ECHOED_CONTEXT.search(message)
    if match is None:
        return message, None
    return message[: match.start()], match.group("echo")


def _offending_control_sequence(diagnostic: str, echo: str | None) -> str | None:
    """The single command an ``Undefined control sequence`` stopped on.

    TeX echoes the failing line in two halves: what it had already read, ending
    with the offending control sequence, and the rest. Only that last token is
    evidence, and only for this one error - a missing glyph, a missing ``$`` or
    an extra brace says nothing about any command name on the line. Anything
    else in the echo is ordinary source text and is never licensed.
    """

    if echo is None or _UNDEFINED_CONTROL_SEQUENCE.search(diagnostic) is None:
        return None
    consumed = _ECHOED_SOURCE.match(echo.strip())
    if consumed is None:
        return None
    trailing = _TRAILING_COMMAND.search(consumed.group("consumed"))
    return trailing.group(1) if trailing is not None else None


def _licensed_from_message(message: str) -> set[str]:
    """Command names one compiler message is evidence about, and no others."""

    diagnostic, echo = split_compiler_message(message)
    if _MISSING_GLYPH_MESSAGE.match(diagnostic.strip()):
        # "Missing character U+03B1 in font ...": a character and a font name.
        return set()
    names = set(_COMMAND_IN_TEXT.findall(diagnostic))
    offending = _offending_control_sequence(diagnostic, echo)
    if offending is not None:
        names.add(offending)
    return names


def error_commands(
    messages: Iterable[str], original: str, whitelist: EngineWhitelist
) -> set[str]:
    """Commands the compiler complained about, plus the ones it cannot know.

    A correction may add or drop exactly these: the names the compiler
    *diagnosis* mentions, the one control sequence an ``Undefined control
    sequence`` error stopped on, and the names of the recorded formula the
    locked engine does not support (an undefined manuscript macro is what the
    repair must remove).

    Names are never harvested from the source line TeX echoes back, even though
    that line is part of the message the Writer sees: licensing every command
    that merely stands on the failing line licenses most of the formula, and a
    licensed command is masked, so licensed tokens become interchangeable -
    ``\\alpha \\otimes \\beta`` -> ``\\beta \\otimes \\alpha`` would pass as a
    syntax repair.

    ``messages`` are the errors of the *current* round only - see
    :func:`latest_compiler_messages` - so a name the engine complained about
    once does not stay licensed for every later round.
    """

    names: set[str] = set()
    for message in messages:
        names.update(_licensed_from_message(message))
    names.update(
        name
        for name in _COMMAND_IN_TEXT.findall(original)
        if not whitelist.is_supported_command(name)
    )
    return names


def latest_compiler_messages(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """The compiler messages of the most recent round in ``errors``.

    The guard widens by exactly the commands the compiler named, so the list
    must not accumulate: a command the engine complained about in round 1 is no
    longer evidence about the body that round 3 is repairing.
    """

    if not errors:
        return []
    latest = max(int(item.get("round", 0)) for item in errors)
    return [
        str(item.get("message", ""))
        for item in errors
        if int(item.get("round", 0)) == latest
    ]


def syntax_only_guard(
    original: str, corrected: str, *, allowed_commands: Iterable[str]
) -> dict[str, Any]:
    """Host check that a correction changed presentation, not mathematics.

    A correction passes only when, after dropping the commands the compiler
    error licensed, the two bodies produce the *same ordered token sequence*
    and the same identifier and command multisets and relation count, and every
    sizing delimiter of the recorded body survives, in order and in shape.
    Anything that reorders, substitutes or drops a token - a sign, an exponent,
    an integration limit, the numerator and denominator of a fraction, the
    brackets of a half-open interval - fails and goes to the Checker's light
    review instead of being accepted silently.
    """

    allowed = set(allowed_commands)
    before_profile, before_delimiters = _tokenize(original)
    after_profile, after_delimiters = _tokenize(corrected)
    before_tokens = _without_allowed(before_profile, allowed)
    after_tokens = _without_allowed(after_profile, allowed)
    before_ids, before_commands, before_relations = _counts(before_tokens)
    after_ids, after_commands, after_relations = _counts(after_tokens)
    identifiers_equal = before_ids == after_ids
    commands_equal = before_commands == after_commands
    relations_equal = before_relations == after_relations
    structure_equal = before_tokens == after_tokens
    delimiters_preserved = _is_subsequence(before_delimiters, after_delimiters)
    return {
        "within_guard": (
            identifiers_equal
            and commands_equal
            and relations_equal
            and structure_equal
            and delimiters_preserved
        ),
        "identifiers_equal": identifiers_equal,
        "commands_equal": commands_equal,
        "relations_equal": relations_equal,
        "structure_equal": structure_equal,
        "delimiters_preserved": delimiters_preserved,
        "delimiters": [list(before_delimiters), list(after_delimiters)],
        "allowed_commands": sorted(allowed),
        "added_commands": sorted(set(after_commands - before_commands)),
        "removed_commands": sorted(set(before_commands - after_commands)),
        "identifier_difference": sorted(
            set(after_ids - before_ids) | set(before_ids - after_ids)
        ),
        "structure_difference": _first_difference(before_tokens, after_tokens),
        "relations": [before_relations, after_relations],
    }


# ---------------------------------------------------------------------------
# Model-call budget


@dataclass
class TypesetCallBudget:
    """Typeset calls count against ``max_model_calls`` together with Record calls.

    The Record's own call count is the scientific budget; the append-only
    journal beside the layers is what a restart reads back, so calls that were
    already spent on an earlier attempt are not spent twice.
    """

    journal_path: Path
    max_model_calls: int | None
    record_model_calls: int

    def used(self) -> int:
        try:
            text = self.journal_path.read_text(encoding="utf-8")
        except OSError:
            return 0
        return sum(1 for line in text.splitlines() if line.strip())

    def available(self) -> bool:
        if self.max_model_calls is None:
            return True
        return self.record_model_calls + self.used() < self.max_model_calls

    def note(self, entry: Mapping[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.journal_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("typeset call journal append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


# ---------------------------------------------------------------------------
# Building one layer


@dataclass
class _Formula:
    formula_id: str
    step_revision_id: str
    field: str
    index: int
    start: int
    end: int
    kind: str
    original: str
    quotation: str | None
    text: str
    status: str = STATUS_OK
    #: The body that produced the most recent compiler error, when a repair
    #: attempt is what failed rather than the recorded formula.
    failing_latex: str | None = None
    final: bool = False
    expansion: tuple[dict[str, Any], ...] = ()
    expansion_skipped: tuple[dict[str, Any], ...] = ()
    guard: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    compiler_errors: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "formula_id": self.formula_id,
            "step_revision_id": self.step_revision_id,
            "field": self.field,
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "quotation": self.quotation,
            "original": self.original,
            "typeset": self.text,
            "status": self.status,
            "expansion": list(self.expansion),
            "expansion_skipped": list(self.expansion_skipped),
            "guard": self.guard,
            "review": self.review,
            "compiler_errors": self.compiler_errors,
            "repair_attempts": self.attempts,
        }


CONTEXT_LIMIT = 200


def _shorten_context(text: str, limit: int = CONTEXT_LIMIT) -> str:
    """Shorten an echoed source line from the middle, never from its end.

    TeX breaks the echoed line exactly after the token it stopped on, so the
    *end* of that line is the part that carries the error and the part
    :func:`_offending_control_sequence` reads. A plain ``text[:limit]`` would
    cut it off and leave some unrelated command of the source line sitting at
    the end, which is precisely what must not become a licence.
    """

    if len(text) <= limit:
        return text
    keep = limit - 5
    return text[: keep // 2] + " ... " + text[-(keep - keep // 2) :]


def _error_context(log: str, line: int | None) -> str | None:
    """The engine's own context line for a halting error, when it has one."""

    if line is None:
        return None
    for text in log.splitlines():
        match = _CONTEXT_LINE.match(text.strip())
        if match is not None and int(match.group(1)) == line:
            return _shorten_context(text.strip())
    return None


def _compiler_message(
    failure: Any, compiles: Sequence[Any], evidence_dir: Path
) -> str:
    message = failure.message
    if failure.compile_number is None:
        return message
    evidence = next(
        (item.evidence for item in compiles if item.number == failure.compile_number),
        None,
    )
    if evidence is None:
        return message
    try:
        log = (evidence_dir / f"{evidence}.log").read_text(encoding="utf-8")
    except OSError:
        return message
    context = _error_context(log, failure.line)
    return message if context is None else f"{message} (context: {context})"


def _attempt_directory(run_directory: Path, route_id: str) -> Path:
    root = typeset_directory(run_directory) / f"{route_id}.evidence"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, 1000):
        candidate = root / f"attempt-{number:02d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a typeset evidence directory")


def _collect_formulas(
    steps: Sequence[Mapping[str, Any]],
    *,
    sources: Mapping[str, str],
    tables: SourceMacroTables,
    whitelist: EngineWhitelist,
) -> tuple[list[_Formula], list[dict[str, Any]]]:
    formulas: list[_Formula] = []
    notes: list[dict[str, Any]] = []
    for step in steps:
        content = {name: step["content"][name] for name in _STEP_FIELDS}
        quotations: frozenset[tuple[str, int]] = frozenset()
        cited: tuple[str, ...] = ()
        analysed = True
        # The recorded content is already normalized; what is needed here is
        # the same attribution normalization used to decide what a quotation
        # is and which sources the step cites.
        try:
            analysis = normalize_step_fields(
                content, sources=sources, whitelist=whitelist, tables=tables
            )
        except (ValueError, KeyError) as exc:
            # Without the analysis the host cannot tell a quotation from the
            # Writer's own math, and sending a cited author's notation to a
            # repair round is the one thing that must not happen. Treat the
            # whole step as quotation: nothing here is repaired or expanded.
            analysed = False
            notes.append(
                {
                    "code": "quotation_analysis_unavailable",
                    "step_revision_id": step["step_revision_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            quotations = analysis.quotation_fragments
            cited = analysis.cited_source_ids
        for fragment in _step_fragments(content):
            quotation = (
                "analysis_unavailable"
                if not analysed
                else "source_field"
                if fragment["field"] == "source"
                else "prose_verbatim"
                if (fragment["field"], fragment["index"]) in quotations
                else None
            )
            formula = _Formula(
                formula_id=(
                    f"{step['step_revision_id']}:{fragment['field']}:{fragment['index']}"
                ),
                step_revision_id=step["step_revision_id"],
                field=fragment["field"],
                index=fragment["index"],
                start=fragment["start"],
                end=fragment["end"],
                kind=fragment["kind"],
                original=fragment["value"],
                quotation=quotation,
                text=fragment["value"],
            )
            if quotation is not None and analysed:
                scope, rule = typeset_expansion_scope(
                    fragment["value"], tables=tables, cited_source_ids=cited
                )
                expanded = expand_math_for_typesetting(
                    fragment["value"],
                    tables=tables,
                    whitelist=whitelist,
                    scope=scope,
                )
                formula.expansion_skipped = tuple(
                    {**item, "rule": rule} for item in expanded.skipped
                )
                if expanded.changed:
                    formula.expansion = tuple(
                        {**item, "rule": rule} for item in expanded.expansions
                    )
                    formula.text = expanded.text
                    formula.status = STATUS_QUOTATION_EXPANDED
            formulas.append(formula)
    return formulas, notes


def _repair_request(
    *,
    run_id: str,
    route_id: str,
    repair_id: str,
    steps: Sequence[Mapping[str, Any]],
    targets: Sequence[_Formula],
) -> FormulaRepairRequest:
    by_step: dict[str, list[_Formula]] = {}
    for formula in targets:
        by_step.setdefault(formula.step_revision_id, []).append(formula)
    repair_steps = []
    for step in steps:
        matching = by_step.get(step["step_revision_id"])
        if not matching:
            continue
        repair_steps.append(
            FormulaRepairStep(
                step_revision_id=step["step_revision_id"],
                content=StepContent(
                    **{name: step["content"][name] for name in _STEP_FIELDS}
                ),
                formulas=tuple(
                    FormulaRepairItem(
                        formula_id=formula.formula_id,
                        field=formula.field,
                        latex=formula.failing_latex or formula.original,
                        compiler_error=(
                            formula.compiler_errors[-1]["message"]
                            if formula.compiler_errors
                            else "The formula did not compile."
                        ),
                        record_latex=(
                            None
                            if (formula.failing_latex or formula.original)
                            == formula.original
                            else formula.original
                        ),
                    )
                    for formula in matching
                ),
            )
        )
    return FormulaRepairRequest(
        run_id=run_id,
        repair_id=repair_id,
        route_id=route_id,
        steps=tuple(repair_steps),
    )


def _review_request(
    *,
    run_id: str,
    route_id: str,
    review_id: str,
    steps: Sequence[Mapping[str, Any]],
    items: Sequence[tuple[_Formula, str]],
) -> FormulaEquivalenceRequest:
    needed = {formula.step_revision_id for formula, _ in items}
    return FormulaEquivalenceRequest(
        run_id=run_id,
        review_id=review_id,
        route_id=route_id,
        steps=tuple(
            StepSnapshot(
                step_revision_id=step["step_revision_id"],
                content=StepContent(
                    **{name: step["content"][name] for name in _STEP_FIELDS}
                ),
            )
            for step in steps
            if step["step_revision_id"] in needed
        ),
        items=tuple(
            FormulaReviewItem(
                formula_id=formula.formula_id,
                step_revision_id=formula.step_revision_id,
                field=formula.field,
                original_latex=formula.original,
                corrected_latex=corrected,
                compiler_error=(
                    formula.compiler_errors[-1]["message"]
                    if formula.compiler_errors
                    else "The formula did not compile."
                ),
            )
            for formula, corrected in items
        ),
    )


FORMAT_ISSUE_DISPOSITION = "accepted_with_format_issues"
FORMAT_ISSUE_FLAG = "step_accepted_with_format_issues"


def step_format_issues(
    steps: Sequence[Mapping[str, Any]],
    format_audits: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Unresolved format defects each sealed step still carries, by step id.

    The per-step gate spends at most two format rewrites and then seals the
    output anyway, with the disposition ``accepted_with_format_issues``. That
    disposition lives in the control plane, which no exported artifact reads,
    so the layer carries it out: a sealed step with unresolved format defects
    is delivered marked, never silently.
    """

    if not format_audits:
        return {}
    found: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        audit = format_audits.get(step.get("model_call_id") or "")
        if audit is None or audit.get("disposition") != FORMAT_ISSUE_DISPOSITION:
            continue
        issues = [
            {
                "code": issue.get("code"),
                "field": issue.get("field"),
                "formula_index": issue.get("formula_index"),
                "severity": issue.get("severity"),
                "message": issue.get("message"),
            }
            for issue in audit.get("issues", ())
            if issue.get("severity") == "error"
        ]
        found[step["step_revision_id"]] = issues
    return found


def layer_format_issue_flags(
    layer: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The ``step_accepted_with_format_issues`` flags of one layer."""

    return [
        dict(flag)
        for flag in layer.get("flags", ())
        if isinstance(flag, Mapping) and flag.get("code") == FORMAT_ISSUE_FLAG
    ]


def format_issue_warnings(
    layers: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Exportable warnings for every step a layer marks as format-defective."""

    warnings: list[dict[str, Any]] = []
    for route_id, layer in sorted((layers or {}).items()):
        for flag in layer_format_issue_flags(layer):
            warnings.append({**flag, "route_id": route_id})
    return warnings


async def typeset_route(
    *,
    run_directory: Path,
    config: RunConfig,
    route_id: str,
    branch_id: str,
    steps: Sequence[Mapping[str, Any]],
    sources: Mapping[str, str],
    documents: Mapping[str, str],
    whitelist: EngineWhitelist,
    runner: TectonicRunner,
    runtime: Any | None,
    budget: TypesetCallBudget,
    format_audits: Mapping[str, Mapping[str, Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Compile, repair and record the typeset layer of one completed route.

    ``format_audits`` are the control plane's formula audits keyed by model
    call id; the steps they mark ``accepted_with_format_issues`` are carried
    into the layer as flags so an export can say so.
    """

    from .formula_compiler import FORMULA_DOCUMENT_LAYOUT, compile_fragments
    from .reporting import REPORT_TEX_PREAMBLE

    started = clock()
    tables = SourceMacroTables(sources, documents)
    formulas, notes = _collect_formulas(
        steps, sources=sources, tables=tables, whitelist=whitelist
    )
    format_issues = step_format_issues(steps, format_audits)
    flags: list[dict[str, Any]] = list(notes)
    flags.extend(
        {
            "code": FORMAT_ISSUE_FLAG,
            "step_revision_id": step["step_revision_id"],
            "issue_count": len(format_issues[step["step_revision_id"]]),
            "issues": format_issues[step["step_revision_id"]],
        }
        for step in steps
        if step["step_revision_id"] in format_issues
    )
    calls: list[dict[str, Any]] = []
    iterations: list[dict[str, Any]] = []
    evidence_root = _attempt_directory(run_directory, route_id)
    layer_status = LAYER_STATUS_COMPILED
    compile_seconds = 0.0
    repair_rounds = 0

    def pending() -> list[_Formula]:
        return [item for item in formulas if not item.final]

    for round_number in range(MAX_REPAIR_ROUNDS + 1):
        # The whole route is compiled again after every repair round, not only
        # the bodies that changed: what has to hold is that the delivered
        # document compiles. Formulas shown verbatim are not typeset as math,
        # and a formula whose only correction was refused is known to fail.
        compiled = [
            item
            for item in formulas
            if item.status not in {STATUS_FAILED, STATUS_QUOTATION_VERBATIM}
        ]
        if not compiled:
            break
        evidence_dir = evidence_root / f"round-{round_number:02d}"
        try:
            result = await asyncio.to_thread(
                compile_fragments,
                [item.text for item in compiled],
                runner,
                evidence_dir,
                label=f"route-typeset:{route_id}",
            )
        except (OSError, ValueError) as exc:
            layer_status = LAYER_STATUS_INFRASTRUCTURE
            flags.append(
                {
                    "code": "compiler_unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break
        compile_seconds += sum(result.durations)
        failures = {failure.index: failure for failure in result.failures}
        iterations.append(
            {
                "round": round_number,
                "evidence": str(evidence_dir.relative_to(run_directory)),
                "fragments": len(compiled),
                "compiles": len(result.compiles),
                "seconds": round(sum(result.durations), 3),
                "infrastructure_error": result.infrastructure_error,
                "failed_formula_ids": [
                    compiled[position].formula_id for position in sorted(failures)
                ],
            }
        )
        if result.infrastructure_error is not None:
            layer_status = LAYER_STATUS_INFRASTRUCTURE
            flags.append(
                {
                    "code": "compiler_infrastructure_error",
                    "round": round_number,
                    "error": result.infrastructure_error,
                }
            )
            break
        for position, formula in enumerate(compiled):
            failure = failures.get(position)
            if failure is None:
                # Verified by this compile: its current body is what is typeset.
                formula.final = True
                continue
            formula.final = False
            message = _compiler_message(failure, result.compiles, evidence_dir)
            formula.failing_latex = formula.text
            formula.compiler_errors.append(
                {
                    "round": round_number,
                    "kind": failure.kind,
                    "message": message,
                    "latex": formula.text,
                }
            )
            # Nothing unverified is ever left in the typeset copy.
            formula.text = formula.original
            if formula.quotation is not None:
                # A quotation is never sent to a model: show it verbatim.
                formula.status = STATUS_QUOTATION_VERBATIM
                formula.final = True
                flags.append(
                    {
                        "code": "quotation_not_typeset",
                        "formula_id": formula.formula_id,
                        "message": message,
                    }
                )
                continue
            formula.status = STATUS_OK
        targets = [
            item
            for item in pending()
            if item.quotation is None and item.status != STATUS_FAILED
        ]
        if not targets:
            break
        if round_number == MAX_REPAIR_ROUNDS:
            flags.append(
                {"code": "repair_rounds_exhausted", "rounds": MAX_REPAIR_ROUNDS}
            )
            break
        proposals, call_records, stop = await _repair_round(
            config=config,
            route_id=route_id,
            round_number=round_number + 1,
            steps=steps,
            targets=targets,
            runtime=runtime,
            budget=budget,
            whitelist=whitelist,
            tables=tables,
            flags=flags,
        )
        calls.extend(call_records)
        repair_rounds += 1 if call_records else 0
        if stop:
            break
        if not proposals:
            # Nothing changed, so recompiling would ask the same question again.
            flags.append({"code": "no_accepted_correction", "round": round_number + 1})
            break

    for formula in formulas:
        verified = formula.final and formula.status != STATUS_FAILED
        if verified:
            continue
        formula.text = formula.original
        if not formula.final and layer_status == LAYER_STATUS_INFRASTRUCTURE:
            # The compiler never gave a verdict, so neither does the layer.
            formula.status = STATUS_NOT_COMPILED
            continue
        formula.status = STATUS_FAILED
        formula.final = True
        flags.append(
            {
                "code": "formula_failed",
                "formula_id": formula.formula_id,
                "step_revision_id": formula.step_revision_id,
                "field": formula.field,
                "message": (
                    formula.compiler_errors[-1]["message"]
                    if formula.compiler_errors
                    else "The formula did not compile."
                ),
            }
        )
    if layer_status == LAYER_STATUS_COMPILED and any(
        item.status == STATUS_FAILED for item in formulas
    ):
        layer_status = LAYER_STATUS_WITH_FAILURES
    return {
        "schema_version": TYPESET_SCHEMA_VERSION,
        "run_id": config.run_id,
        "route_id": route_id,
        "branch_id": branch_id,
        "status": layer_status,
        "formula_validation_policy": config.formula_validation_policy,
        "normalizer_version": NORMALIZER_VERSION,
        "engine": {
            "version": runner.runtime.version,
            "target": runner.runtime.target,
            "binary_sha256": runner.runtime.binary_sha256,
            "bundle_sha256": runner.runtime.bundle_sha256,
            "layout": FORMULA_DOCUMENT_LAYOUT,
            "preamble_sha256": _sha256_text(REPORT_TEX_PREAMBLE),
        },
        "engine_whitelist": whitelist.identity(),
        "accepted_with_format_issues": bool(format_issues),
        "steps": [
            {
                "step_revision_id": step["step_revision_id"],
                "output_sha256": step["output_sha256"],
                "format_issues": format_issues.get(step["step_revision_id"], []),
            }
            for step in steps
        ],
        "formulas": [item.to_record() for item in formulas],
        "iterations": iterations,
        "repair_rounds": repair_rounds,
        "calls": calls,
        "flags": flags,
        "budget": {
            "max_model_calls": config.max_model_calls,
            "record_model_calls": budget.record_model_calls,
            "typeset_calls_after": budget.used(),
        },
        "evidence_directory": str(evidence_root.relative_to(run_directory)),
        "compile_seconds": round(compile_seconds, 3),
        "seconds": round(max(0.0, clock() - started), 3),
    }


def route_steps(canonical: Mapping[str, Any], branch_id: str) -> list[dict[str, Any]]:
    """The recorded steps of one route, in route order, with their hashes."""

    branch = next(
        item for item in canonical["branches"] if item["branch_id"] == branch_id
    )
    steps = {
        item["step_revision_id"]: item for item in canonical["step_revisions"]
    }
    return [
        {
            "step_revision_id": step_id,
            "output_sha256": steps[step_id]["output_sha256"],
            "content": dict(steps[step_id]["content"]),
            # The formula audit of the control plane is keyed by model call;
            # this is how a sealed step is matched to the format defects it
            # still carries (see ``format_audits`` of :func:`typeset_route`).
            "model_call_id": steps[step_id].get("origin", {}).get("model_call_id"),
        }
        for step_id in branch["step_revision_ids"]
    ]


def completed_routes(canonical: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(route_id, branch_id) of every completed branch, oldest first."""

    return [
        (f"route_{item['branch_id']}", item["branch_id"])
        for item in sorted(
            (
                branch
                for branch in canonical["branches"]
                if branch["status"] == "completed"
            ),
            key=lambda item: (int(item["status_history"][0]["seq"]), item["branch_id"]),
        )
    ]


def pending_typeset_routes(
    run_directory: Path, canonical: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """Completed routes of this Record with no layer a consumer would accept.

    A layer file is not enough: the consumers ignore a layer that no longer
    matches the Record it cites, so a stale one (a route that grew a step after
    its layer was written, a file from an earlier Record) leaves the route
    pending and is rebuilt. Otherwise a mismatched file would sit there for
    ever while every reader silently fell back to the unrepaired text.
    """

    run_id = str(canonical["run"]["run_id"])
    pending: list[tuple[str, str]] = []
    for route_id, branch_id in completed_routes(canonical):
        layer = load_typeset_layer(run_directory, route_id)
        if layer is None or not verify_typeset_layer(
            layer,
            run_id=run_id,
            route_id=route_id,
            steps=route_steps(canonical, branch_id),
        ):
            pending.append((route_id, branch_id))
    return pending


async def typeset_completed_routes(
    *,
    run_directory: Path,
    config: RunConfig,
    canonical: Mapping[str, Any],
    runner: TectonicRunner,
    runtime: Any | None = None,
    whitelist: EngineWhitelist | None = None,
    format_audits: Mapping[str, Mapping[str, Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Path]:
    """Write the missing typeset layers of a run's completed routes.

    One layer per completed route, built once and never rebuilt: a later call
    finds the file and leaves it alone. Failures are recorded in the layer, so
    the caller's run phase is never affected by what the compiler or the
    provider did here.
    """

    pending = pending_typeset_routes(run_directory, canonical)
    if not pending:
        return []
    resolved = load_engine_whitelist() if whitelist is None else whitelist
    sources = {
        item["source_id"]: item["text"]
        for item in canonical.get("source_evidence", ())
    }
    provider = getattr(runtime, "evidence_source_documents", None)
    documents = dict(provider()) if callable(provider) else {}
    documents = {key: value for key, value in documents.items() if key in sources}
    budget = TypesetCallBudget(
        journal_path=typeset_directory(run_directory) / CALL_JOURNAL_NAME,
        max_model_calls=config.max_model_calls,
        record_model_calls=int(canonical["summary"]["model_call_count"]),
    )
    written: list[Path] = []
    for route_id, branch_id in pending:
        layer = await typeset_route(
            run_directory=run_directory,
            config=config,
            route_id=route_id,
            branch_id=branch_id,
            steps=route_steps(canonical, branch_id),
            sources=sources,
            documents=documents,
            whitelist=resolved,
            runner=runner,
            runtime=runtime,
            budget=budget,
            format_audits=format_audits,
            clock=clock,
        )
        written.append(write_typeset_layer(run_directory, layer))
    return written


async def _repair_round(
    *,
    config: RunConfig,
    route_id: str,
    round_number: int,
    steps: Sequence[Mapping[str, Any]],
    targets: Sequence[_Formula],
    runtime: Any | None,
    budget: TypesetCallBudget,
    whitelist: EngineWhitelist,
    tables: SourceMacroTables,
    flags: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]], bool]:
    """One repair round: ask, guard, review. Returns (accepted, calls, stop)."""

    calls: list[dict[str, Any]] = []
    if runtime is None or not hasattr(runtime, "start_formula_repair"):
        flags.append({"code": "repair_unavailable", "round": round_number})
        return 0, calls, True
    if not budget.available():
        flags.append(
            {
                "code": "repair_budget_exhausted",
                "round": round_number,
                "max_model_calls": config.max_model_calls,
            }
        )
        return 0, calls, True
    repair_id = f"typeset_{route_id}_r{round_number:02d}"
    request = _repair_request(
        run_id=config.run_id,
        route_id=route_id,
        repair_id=repair_id,
        steps=steps,
        targets=targets,
    )
    audit = {
        "call_id": repair_id,
        "kind": "formula_repair",
        "role": "writer",
        "round": round_number,
        "model": config.writer.model,
        "effort": config.writer.effort,
        "service_tier": config.service_tier,
        "prompt_sha256": _sha256_text(formula_repair_user_prompt(request)),
        "formula_ids": list(request.formula_ids),
    }
    output = await _run_typeset_call(
        runtime.start_formula_repair,
        runtime.collect_formula_repair,
        request,
        audit=audit,
        budget=budget,
        route_id=route_id,
    )
    calls.append(audit)
    if output is None:
        flags.append(
            {
                "code": "repair_call_failed",
                "round": round_number,
                "error": audit.get("error"),
            }
        )
        return 0, calls, True
    by_id = {item.formula_id: item for item in targets}
    accepted = 0
    review_items: list[tuple[_Formula, str]] = []
    for formula_id, latex in output.corrections:
        formula = by_id.get(formula_id)
        if formula is None:
            # A runtime that answers something it was not asked about.
            flags.append(
                {
                    "code": "unrequested_correction",
                    "round": round_number,
                    "formula_id": formula_id,
                }
            )
            continue
        # A later round judges a new correction on its own: the guard and the
        # review verdict of an earlier round describe a body that has already
        # been compiled and rejected.
        formula.guard = None
        formula.review = None
        corrected, control_edits, _unrepaired = restore_control_characters(
            latex, whitelist=whitelist, tables=tables
        )
        corrected = corrected.strip()
        attempt: dict[str, Any] = {
            "round": round_number,
            "latex": corrected,
            "control_edits": list(control_edits),
        }
        if not corrected or _MATH_DELIMITERS.search(corrected):
            attempt["accepted"] = False
            attempt["reason"] = "correction is not a single math body"
            formula.attempts.append(attempt)
            continue
        if corrected in {formula.original, formula.failing_latex}:
            attempt["accepted"] = False
            attempt["reason"] = "correction repeats a body that already failed"
            formula.attempts.append(attempt)
            continue
        guard = syntax_only_guard(
            formula.original,
            corrected,
            allowed_commands=error_commands(
                latest_compiler_messages(formula.compiler_errors),
                formula.original,
                whitelist,
            ),
        )
        formula.guard = guard
        attempt["guard"] = guard
        if guard["within_guard"]:
            formula.text = corrected
            formula.status = STATUS_REPAIRED
            attempt["accepted"] = True
            accepted += 1
        else:
            review_items.append((formula, corrected))
            attempt["accepted"] = None
            attempt["reason"] = "beyond the syntax-only guard; sent to review"
        formula.attempts.append(attempt)
    if review_items:
        accepted += await _review_round(
            config=config,
            route_id=route_id,
            round_number=round_number,
            steps=steps,
            items=review_items,
            runtime=runtime,
            budget=budget,
            flags=flags,
            calls=calls,
            prompt=formula_review_user_prompt,
        )
    return accepted, calls, False


async def _review_round(
    *,
    config: RunConfig,
    route_id: str,
    round_number: int,
    steps: Sequence[Mapping[str, Any]],
    items: Sequence[tuple[_Formula, str]],
    runtime: Any,
    budget: TypesetCallBudget,
    flags: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    prompt: Callable[[FormulaEquivalenceRequest], str],
) -> int:
    def refuse(reason: str) -> None:
        for formula, corrected in items:
            formula.status = STATUS_FAILED
            formula.final = True
            formula.review = {"verdict": "unavailable", "reason": reason}
            formula.attempts[-1]["accepted"] = False
            formula.attempts[-1]["reason"] = reason
            flags.append(
                {
                    "code": "correction_not_reviewed",
                    "formula_id": formula.formula_id,
                    "reason": reason,
                    "correction": corrected,
                }
            )

    if not hasattr(runtime, "start_formula_review"):
        refuse("the runtime cannot review formula corrections")
        return 0
    if not budget.available():
        refuse("the model-call budget is exhausted")
        return 0
    review_id = f"typeset_{route_id}_v{round_number:02d}"
    request = _review_request(
        run_id=config.run_id,
        route_id=route_id,
        review_id=review_id,
        steps=steps,
        items=items,
    )
    audit = {
        "call_id": review_id,
        "kind": "formula_equivalence_review",
        "role": "checker",
        "round": round_number,
        "model": config.checker.model,
        "effort": config.checker.effort,
        "service_tier": config.service_tier,
        "prompt_sha256": _sha256_text(prompt(request)),
        "formula_ids": [item.formula_id for item in request.items],
    }
    output = await _run_typeset_call(
        runtime.start_formula_review,
        runtime.collect_formula_review,
        request,
        audit=audit,
        budget=budget,
        route_id=route_id,
    )
    calls.append(audit)
    if output is None:
        refuse(f"the review call failed: {audit.get('error')}")
        return 0
    corrections = {formula.formula_id: corrected for formula, corrected in items}
    by_id = {formula.formula_id: formula for formula, _ in items}
    accepted = 0
    for formula_id, verdict, reason in output.verdicts:
        formula = by_id.get(formula_id)
        if formula is None:
            flags.append(
                {
                    "code": "unrequested_review",
                    "round": round_number,
                    "formula_id": formula_id,
                }
            )
            continue
        formula.review = {"verdict": verdict, "reason": reason}
        if verdict == "equivalent":
            formula.text = corrections[formula_id]
            formula.status = STATUS_REPAIRED_REVIEWED
            formula.attempts[-1]["accepted"] = True
            formula.attempts[-1]["reason"] = "reviewed as equivalent"
            accepted += 1
            continue
        # Keep the recorded formula and flag it.
        formula.status = STATUS_FAILED
        formula.final = True
        formula.attempts[-1]["accepted"] = False
        formula.attempts[-1]["reason"] = "reviewed as not equivalent"
        flags.append(
            {
                "code": "correction_not_equivalent",
                "formula_id": formula_id,
                "reason": reason,
                "correction": corrections[formula_id],
            }
        )
    answered = {formula_id for formula_id, _verdict, _reason in output.verdicts}
    for formula, corrected in items:
        if formula.formula_id in answered:
            continue
        formula.status = STATUS_FAILED
        formula.final = True
        formula.review = {"verdict": "unanswered", "reason": "no verdict was returned"}
        flags.append(
            {
                "code": "correction_not_reviewed",
                "formula_id": formula.formula_id,
                "reason": "the review returned no verdict for this correction",
                "correction": corrected,
            }
        )
    return accepted


async def _run_typeset_call(
    start: Callable[[Any], Any],
    collect: Callable[[Any], Any],
    request: Any,
    *,
    audit: dict[str, Any],
    budget: TypesetCallBudget,
    route_id: str,
) -> Any | None:
    """Start one typeset call, journal it, and collect it; None on failure."""

    try:
        invocation = await start(request)
    except (RuntimeInvocationError, RuntimeInvariantError) as exc:
        # A typeset call is not scientific work: a provider that refuses it
        # leaves the route unrepaired and flagged, never an unusable run.
        audit["status"] = "not_started"
        audit["error"] = f"{type(exc).__name__}: {exc}"
        return None
    try:
        budget.note(
            {
                "route_id": route_id,
                "call_id": audit["call_id"],
                "kind": audit["kind"],
                "model": audit["model"],
                "effort": audit["effort"],
                "prompt_sha256": audit["prompt_sha256"],
            }
        )
    except OSError as exc:
        # The call is already running; losing its journal line would let a
        # later attempt spend it a second time, so say so in the audit.
        audit["journal_error"] = f"{type(exc).__name__}: {exc}"
    audit["session_id"] = invocation.session.session_id
    audit["operation_id"] = invocation.operation_id
    try:
        output = await collect(invocation)
    except (RuntimeInvocationError, RuntimeInvariantError) as exc:
        audit["status"] = "failed"
        audit["error"] = f"{type(exc).__name__}: {exc}"
        return None
    except ValueError as exc:
        audit["status"] = "failed"
        audit["error"] = f"invalid typeset output: {exc}"
        return None
    audit["status"] = "completed"
    audit["finish_reason"] = output.finish_reason
    audit["usage"] = dict(output.usage.values)
    audit["raw_output"] = output.raw_output
    return output


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CALL_JOURNAL_NAME",
    "FORMAT_ISSUE_DISPOSITION",
    "FORMAT_ISSUE_FLAG",
    "LAYER_STATUS_COMPILED",
    "LAYER_STATUS_ERROR",
    "LAYER_STATUS_INFRASTRUCTURE",
    "LAYER_STATUS_WITH_FAILURES",
    "MAX_REPAIR_ROUNDS",
    "STATUS_FAILED",
    "STATUS_NORMALIZED",
    "STATUS_NOT_COMPILED",
    "STATUS_OK",
    "STATUS_QUOTATION_EXPANDED",
    "STATUS_QUOTATION_VERBATIM",
    "STATUS_REPAIRED",
    "STATUS_REPAIRED_REVIEWED",
    "SUBSTITUTED_STATUSES",
    "TYPESET_DIRECTORY",
    "TYPESET_SCHEMA_VERSION",
    "TypesetCallBudget",
    "completed_routes",
    "error_commands",
    "format_issue_warnings",
    "latest_compiler_messages",
    "layer_format_issue_flags",
    "layer_path",
    "load_typeset_layer",
    "pending_typeset_routes",
    "route_steps",
    "split_compiler_message",
    "step_format_issues",
    "syntax_only_guard",
    "typeset_completed_routes",
    "typeset_content_sha256",
    "typeset_directory",
    "typeset_field_text",
    "typeset_layer_identity",
    "typeset_lookup",
    "typeset_route",
    "verified_layers",
    "verify_typeset_layer",
    "write_typeset_layer",
]
