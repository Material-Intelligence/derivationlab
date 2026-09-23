"""Deterministic, reversible normalization of one Writer step (formula-v2).

Pure functions only: no model, compiler, clock or filesystem access. The same
inputs always produce the same edit list, and every edit is recorded so the raw
Writer output can be restored byte for byte.

Two layers, applied in order.

Layer 1 - JSON transport corruption, every field including ``source``:

``control_char_backslash``
    A character in U+0000..U+0008, U+000B, U+000E..U+001F immediately followed
    by a known command name (the maximal ASCII-letter run, as TeX tokenizes)
    becomes a backslash. Known names are the engine whitelist commands and the
    names defined by the run's registered sources. U+0008 is skipped when
    ``b`` + name is itself a known command (``\\beta`` JSON-decoded as BS +
    ``eta``), because then the lost character cannot be decided.
``control_char_removed``
    Such a character immediately followed by an intact ``\\command`` is deleted.
``del_removed``
    U+007F immediately followed by a backslash is deleted.
``ansi_escape_removed``
    A complete ANSI CSI sequence (ESC ``[`` parameters final byte) is deleted.

Anything else (DEL before letters, a character inside a word, a lost letter)
is left in place and reported; static validation still flags it.

Layer 2 - manuscript macro expansion (``macro_expansion``), only in the
non-quotation fields and never inside a prose verbatim quotation. Macro tables
are parsed from the registered source texts, per ``source_id``. A name the
engine compiles is never expanded. Otherwise an occurrence inside a math
fragment is expanded when its definition is unique under exactly one of:

1. the fragment occurs verbatim (whitespace-normalized, at least
   ``QUOTATION_MIN_CHARS`` characters) in exactly one registered source that
   the step does not cite: that source's definitions;
2. the step cites registered sources: exactly one definition among them;
3. the step cites none: one consistent definition across all sources.

A cited source brings the other parts of its document into scope when the
caller supplies the document grouping (the files of one multi-file source).
A fragment that occurs verbatim in a source the step cites is a prose
quotation: nothing in it is expanded and, from ``QUOTATION_MIN_CHARS``
characters on, its non-safety diagnostics are warnings too. The threshold is
about evidence - a short match is not proof that the author is being quoted -
and it governs the severity downgrade only: a shorter fragment found verbatim
in a cited source is left alone as well, with one exception. A fragment that is
*nothing but* one manuscript macro and its arguments (``$\\ee$``) is the
author's own abbreviation rather than a formula, and its own definition
expands it without saying anything different, so that one is still expanded.
An expansion whose result would contain a command outside the engine
whitelist (for example ``\\bm``) is refused and reported.

Replacements form an ordered edit list. ``start``/``end`` refer to the text of
that field at the moment the edit is applied; layer 1 edits come first (field
order, descending offset), then layer 2 edits (field order, descending offset).
:func:`apply_replacements` reproduces the normalized fields from the raw ones
and :func:`revert_replacements` restores the raw fields exactly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .formula_validation import EngineWhitelist, math_fragments

NORMALIZER_VERSION = "formula-normalization-v1"
STEP_FIELDS = ("claim", "why", "source", "derivation", "scope")
QUOTATION_FIELDS = frozenset({"source"})
MAX_EXPANSION_DEPTH = 8
# A whitespace-normalized fragment shorter than this (``\\ee``, ``x_1``) occurs
# verbatim in almost any manuscript; it is not evidence of quotation.
QUOTATION_MIN_CHARS = 16

CONTROL_KINDS = frozenset(
    {
        "control_char_backslash",
        "control_char_removed",
        "del_removed",
        "ansi_escape_removed",
    }
)
REPLACEMENT_KINDS = CONTROL_KINDS | {"macro_expansion"}

RESTORABLE_CONTROL = frozenset(
    [chr(c) for c in range(0x09)] + ["\x0b"] + [chr(c) for c in range(0x0E, 0x20)]
)
DEL = "\x7f"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LETTERS = re.compile(r"[A-Za-z]+")
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Replacement records


@dataclass(frozen=True)
class Replacement:
    field: str
    start: int
    end: int
    original: str
    replacement: str
    kind: str
    source_id: str | None = None
    source_line: int | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "start": self.start,
            "end": self.end,
            "original": self.original,
            "replacement": self.replacement,
            "kind": self.kind,
            "source_id": self.source_id,
            "source_line": self.source_line,
        }


def _as_record(item: Replacement | Mapping[str, Any]) -> Mapping[str, Any]:
    return item.to_record() if isinstance(item, Replacement) else item


def apply_replacements(
    fields: Mapping[str, str], replacements: Sequence[Replacement | Mapping[str, Any]]
) -> dict[str, str]:
    """Apply an edit list in order; each edit must match the current text."""

    out = dict(fields)
    for raw in replacements:
        item = _as_record(raw)
        text = out[item["field"]]
        start, end = item["start"], item["end"]
        if not 0 <= start <= end <= len(text) or text[start:end] != item["original"]:
            raise ValueError(
                f"replacement does not match {item['field']} at {start}:{end}"
            )
        out[item["field"]] = text[:start] + item["replacement"] + text[end:]
    return out


def revert_replacements(
    fields: Mapping[str, str], replacements: Sequence[Replacement | Mapping[str, Any]]
) -> dict[str, str]:
    """Undo an edit list (reverse order) and return the raw fields."""

    out = dict(fields)
    for raw in reversed(list(replacements)):
        item = _as_record(raw)
        text = out[item["field"]]
        start = item["start"]
        stop = start + len(item["replacement"])
        if text[start:stop] != item["replacement"]:
            raise ValueError(
                f"replacement text not found in {item['field']} at {start}"
            )
        out[item["field"]] = text[:start] + item["original"] + text[stop:]
    return out


# ---------------------------------------------------------------------------
# Macro tables


@dataclass(frozen=True)
class MacroDefinition:
    name: str
    kind: str  # newcommand | renewcommand | providecommand | def | gdef | DeclareMathOperator
    star: bool
    nargs: int
    default: str | None
    template: str  # expansion text with #k placeholders
    source_id: str
    line: int
    expandable: bool
    unsupported_reason: str | None = None

    @property
    def key(self) -> tuple[int, str | None, str]:
        return (self.nargs, self.default, _collapse_ws(self.template))


def _collapse_ws(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


_DEF_START = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator|def|gdef)(?![A-Za-z@])(\*?)"
)


def _odd_backslashes(text: str, index: int) -> bool:
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def _strip_comments(text: str) -> str:
    """Blank TeX comments while keeping every line (line numbers stay exact)."""

    lines = []
    for line in text.split("\n"):
        cursor = 0
        while True:
            cursor = line.find("%", cursor)
            if cursor < 0:
                break
            if not _odd_backslashes(line, cursor):
                line = line[:cursor]
                break
            cursor += 1
        lines.append(line)
    return "\n".join(lines)


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _read_group(
    text: str, pos: int, open_: str = "{", close: str = "}"
) -> tuple[str, int] | None:
    """Balanced group at ``text[pos] == open_`` -> (content, index after closer)."""

    if pos >= len(text) or text[pos] != open_:
        return None
    depth = 0
    brace = 0
    cursor = pos
    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if open_ == "[":
            if character == "{":
                brace += 1
            elif character == "}":
                brace -= 1
            elif brace == 0 and character == "[":
                depth += 1
            elif brace == 0 and character == "]":
                depth -= 1
                if depth == 0:
                    return text[pos + 1 : cursor], cursor + 1
        else:
            if character == open_:
                depth += 1
            elif character == close:
                depth -= 1
                if depth == 0:
                    return text[pos + 1 : cursor], cursor + 1
        cursor += 1
    return None


def _read_control_sequence(text: str, pos: int) -> tuple[str, int] | None:
    if pos + 1 >= len(text) or text[pos] != "\\":
        return None
    match = re.match(r"[A-Za-z@]+", text[pos + 1 :])
    if match:
        return match.group(0), pos + 1 + len(match.group(0))
    return text[pos + 1], pos + 2


def parse_macro_definitions(text: str, source_id: str) -> list[MacroDefinition]:
    """Definitions in one source text; unparseable ones are skipped.

    Supports ``\\newcommand``/``\\renewcommand``/``\\providecommand`` (starred,
    ``[n]`` arguments, ``[default]``), ``\\def``/``\\gdef`` with ``#1#2``
    parameters and ``\\DeclareMathOperator``. Bodies are brace-matched.
    Definitions nested inside another body are not separate definitions.
    """

    text = _strip_comments(text)
    definitions: list[MacroDefinition] = []
    pos = 0

    def line_of(index: int) -> int:
        return text.count("\n", 0, index) + 1

    while True:
        match = _DEF_START.search(text, pos)
        if match is None:
            break
        start = match.start()
        if _odd_backslashes(text, start):
            pos = match.end()
            continue
        kind, star = match.group(1), bool(match.group(2))
        cursor = _skip_ws(text, match.end())
        name: str | None = None
        if cursor < len(text) and text[cursor] == "{":
            group = _read_group(text, cursor)
            if group is not None:
                inner = group[0].strip()
                sequence = _read_control_sequence(inner, 0)
                if sequence is not None and sequence[1] == len(inner):
                    name = sequence[0]
                cursor = group[1]
        elif cursor < len(text) and text[cursor] == "\\":
            sequence = _read_control_sequence(text, cursor)
            if sequence is not None:
                name, cursor = sequence
        if name is None:
            pos = match.end()
            continue
        nargs = 0
        default: str | None = None
        unsupported: str | None = None
        if kind == "DeclareMathOperator":
            group = _read_group(text, _skip_ws(text, cursor))
            if group is None:
                pos = match.end()
                continue
            body, cursor = group
            template = ("\\operatorname*{" if star else "\\operatorname{") + body + "}"
        elif kind in {"def", "gdef"}:
            param_end = cursor
            while param_end < len(text) and text[param_end] != "{":
                if text.startswith("\n\n", param_end):
                    break
                param_end += 1
            params = text[cursor:param_end]
            placeholders = re.findall(r"#[1-9]", params)
            nargs = len(placeholders)
            if not re.fullmatch(r"(#[1-9])*", params.strip()) or any(
                item != f"#{index + 1}" for index, item in enumerate(placeholders)
            ):
                unsupported = "delimited_parameter_text"
            group = _read_group(text, param_end)
            if group is None:
                pos = match.end()
                continue
            body, cursor = group
            template = body
        else:
            cursor = _skip_ws(text, cursor)
            if cursor < len(text) and text[cursor] == "[":
                group = _read_group(text, cursor, "[", "]")
                if group is None or not group[0].strip().isdigit():
                    pos = match.end()
                    continue
                nargs = int(group[0].strip())
                cursor = _skip_ws(text, group[1])
                if cursor < len(text) and text[cursor] == "[":
                    group = _read_group(text, cursor, "[", "]")
                    if group is None:
                        pos = match.end()
                        continue
                    default = group[0]
                    cursor = _skip_ws(text, group[1])
            if cursor < len(text) and text[cursor] == "{":
                group = _read_group(text, cursor)
                if group is None:
                    pos = match.end()
                    continue
                body, cursor = group
            elif cursor < len(text) and text[cursor] == "\\":
                sequence = _read_control_sequence(text, cursor)
                assert sequence is not None
                body = text[cursor : sequence[1]]
                cursor = sequence[1]
            else:
                pos = match.end()
                continue
            template = body
        if not re.fullmatch(r"[A-Za-z]+", name) and unsupported is None:
            unsupported = "control_symbol_or_at_name"
        if nargs > 9 and unsupported is None:
            unsupported = "too_many_arguments"
        definitions.append(
            MacroDefinition(
                name=name,
                kind=kind,
                star=star,
                nargs=nargs,
                default=default,
                template=template,
                source_id=source_id,
                line=line_of(start),
                expandable=unsupported is None,
                unsupported_reason=unsupported,
            )
        )
        pos = cursor
    return definitions


class SourceMacroTables:
    """Macro definitions of a run's registered sources, keyed by source_id.

    ``documents`` optionally maps a source_id to its document (for example the
    DOI shared by the parts of one multi-file source). Citing or quoting one
    part then brings every part of that document into scope; without it each
    source_id is its own document.
    """

    def __init__(
        self,
        sources: Mapping[str, str],
        documents: Mapping[str, str] | None = None,
    ) -> None:
        self.source_ids: tuple[str, ...] = tuple(sources)
        documents = documents or {}
        self.document_of: dict[str, str] = {
            source_id: documents.get(source_id) or source_id
            for source_id in self.source_ids
        }
        self.definitions: dict[str, tuple[MacroDefinition, ...]] = {
            source_id: tuple(parse_macro_definitions(text, source_id))
            for source_id, text in sources.items()
        }
        self._normalized_text = {
            source_id: _collapse_ws(text) for source_id, text in sources.items()
        }
        self.names = frozenset(
            item.name for items in self.definitions.values() for item in items
        )

    def document_scope(self, source_ids: Iterable[str]) -> tuple[str, ...]:
        """Registered sources that share a document with any given source."""
        wanted = {
            self.document_of[item] for item in source_ids if item in self.document_of
        }
        return tuple(
            source_id
            for source_id in self.source_ids
            if self.document_of[source_id] in wanted
        )

    def by_name(self, name: str, source_ids: Iterable[str]) -> list[MacroDefinition]:
        return [
            item
            for source_id in source_ids
            for item in self.definitions.get(source_id, ())
            if item.name == name
        ]

    def verbatim_sources(
        self, fragment: str, *, min_chars: int = QUOTATION_MIN_CHARS
    ) -> list[str]:
        """Registered sources containing this fragment verbatim (whitespace-normalized).

        ``min_chars`` is the length below which a match is not *evidence* of
        quotation: ``\\ee`` or ``x_1`` occurs in almost any manuscript. It is a
        rule about severity and attribution, not about ownership - pass
        ``min_chars=1`` where the question is whether the cited author wrote
        these characters at all, which is what decides whether the host may
        rewrite them.
        """

        normalized = _collapse_ws(fragment)
        if not normalized or len(normalized) < min_chars:
            return []
        return [
            source_id
            for source_id in self.source_ids
            if normalized in self._normalized_text[source_id]
        ]


# ---------------------------------------------------------------------------
# Result


@dataclass(frozen=True)
class NormalizationResult:
    fields: dict[str, str]
    replacements: tuple[Replacement, ...]
    #: (field, formula_index) of prose math that quotes a cited source verbatim.
    quotation_fragments: frozenset[tuple[str, int]]
    cited_source_ids: tuple[str, ...]
    unrepaired_control: tuple[dict[str, Any], ...] = ()
    skipped_macros: tuple[dict[str, Any], ...] = ()
    notes: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.replacements)

    def replacement_records(self) -> list[dict[str, Any]]:
        return [item.to_record() for item in self.replacements]

    def report(self) -> dict[str, Any]:
        """Audit-sized summary of what was and was not changed."""
        return {
            "normalizer_version": NORMALIZER_VERSION,
            "replacement_count": len(self.replacements),
            "replacement_kinds": _count(item.kind for item in self.replacements),
            "cited_source_ids": list(self.cited_source_ids),
            "quotation_fragments": [
                list(item) for item in sorted(self.quotation_fragments)
            ],
            "unrepaired_control": list(self.unrepaired_control),
            "skipped_macros": list(self.skipped_macros),
        }


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Layer 1


def _control_record(
    field_name: str, text: str, index: int, reason: str
) -> dict[str, Any]:
    return {
        "field": field_name,
        "offset": index,
        "codepoint": f"U+{ord(text[index]):04X}",
        "reason": reason,
    }


def _restore_control_characters(
    field_name: str, text: str, known_names: frozenset[str]
) -> tuple[list[Replacement], list[dict[str, Any]]]:
    """Edits for one field in descending offset order, plus unrepaired reports."""

    edits: list[Replacement] = []
    unrepaired: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        character = text[index]
        code = ord(character)
        flagged = (code < 32 and character not in "\n\r\t") or 127 <= code <= 159
        if not flagged:
            index += 1
            continue
        if character == "\x1b":
            ansi = ANSI_ESCAPE.match(text, index)
            if ansi is not None:
                edits.append(
                    Replacement(
                        field_name,
                        index,
                        ansi.end(),
                        ansi.group(0),
                        "",
                        "ansi_escape_removed",
                    )
                )
                index = ansi.end()
                continue
        if character in RESTORABLE_CONTROL:
            letters = _LETTERS.match(text, index + 1)
            following = letters.group(0) if letters else ""
            if following and following in known_names:
                if _odd_backslashes(text, index):
                    unrepaired.append(
                        _control_record(
                            field_name, text, index, "preceded_by_backslash"
                        )
                    )
                elif character == "\x08" and "b" + following in known_names:
                    unrepaired.append(
                        _control_record(
                            field_name, text, index, "ambiguous_json_backspace"
                        )
                    )
                else:
                    edits.append(
                        Replacement(
                            field_name,
                            index,
                            index + 1,
                            character,
                            "\\",
                            "control_char_backslash",
                        )
                    )
                index += 1
                continue
            if text[index + 1 : index + 2] == "\\" and not _odd_backslashes(
                text, index
            ):
                command = _LETTERS.match(text, index + 2)
                if command is not None and command.group(0) in known_names:
                    edits.append(
                        Replacement(
                            field_name,
                            index,
                            index + 1,
                            character,
                            "",
                            "control_char_removed",
                        )
                    )
                    index += 1
                    continue
                reason = "followed_by_backslash"
            elif following:
                reason = "unknown_command_name"
            else:
                reason = "not_followed_by_letters"
            unrepaired.append(_control_record(field_name, text, index, reason))
        elif character == DEL:
            if text[index + 1 : index + 2] == "\\":
                edits.append(
                    Replacement(
                        field_name, index, index + 1, character, "", "del_removed"
                    )
                )
                index += 1
                continue
            unrepaired.append(
                _control_record(field_name, text, index, "del_not_before_backslash")
            )
        else:
            unrepaired.append(
                _control_record(field_name, text, index, "outside_restorable_set")
            )
        index += 1
    edits.sort(key=lambda item: item.start, reverse=True)
    return edits, unrepaired


# ---------------------------------------------------------------------------
# Layer 2


class _Skip(Exception):
    def __init__(self, reason: str, command: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = command


_CONTROL_WORD_END = re.compile(r"\\[A-Za-z]+$")
# Characters that would change math tokenization or TeX safety if an expansion
# introduced them: math delimiters, comments, parameters and code spans.
_STRUCTURAL = re.compile(r"(?<!\\)[$%#`]|\\[()\[\]]")
_COMMAND_TOKEN = re.compile(r"\\([A-Za-z]+|.)", re.DOTALL)


def _join(left: str, right: str) -> str:
    if right[:1].isalpha() and _CONTROL_WORD_END.search(left):
        return left + " " + right
    return left + right


def _read_argument(text: str, pos: int) -> tuple[str, int] | None:
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        return None
    character = text[pos]
    if character == "{":
        return _read_group(text, pos)
    if character == "}":
        return None
    if character == "\\":
        sequence = _read_control_sequence(text, pos)
        return None if sequence is None else (text[pos : sequence[1]], sequence[1])
    return character, pos + 1


def _substitute(template: str, arguments: Sequence[str]) -> str:
    out = ""
    boundary = False
    index = 0
    while index < len(template):
        character = template[index]
        if character == "#" and index + 1 < len(template):
            following = template[index + 1]
            if following == "#":
                out += "#"
                index += 2
                boundary = False
                continue
            if following.isdigit() and 1 <= int(following) <= len(arguments):
                out = _join(out, arguments[int(following) - 1])
                index += 2
                boundary = True
                continue
        out = _join(out, character) if boundary else out + character
        boundary = False
        index += 1
    return out


def _balanced(text: str) -> bool:
    depth = 0
    for index, character in enumerate(text):
        if _odd_backslashes(text, index):
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


class _Expander:
    def __init__(
        self,
        tables: SourceMacroTables,
        whitelist: EngineWhitelist,
        max_depth: int,
    ) -> None:
        self.tables = tables
        self.whitelist = whitelist
        self.max_depth = max_depth

    def expandable_name(self, name: str) -> bool:
        return name not in self.whitelist.commands and name in self.tables.names

    def resolve(self, name: str, scope: Sequence[str]) -> MacroDefinition:
        found = self.tables.by_name(name, scope)
        if not found:
            raise _Skip("undefined_in_scope", name)
        if len({item.key for item in found}) > 1:
            raise _Skip("ambiguous", name)
        if not all(item.expandable for item in found):
            raise _Skip(f"unsupported_definition:{found[0].unsupported_reason}", name)
        return found[0]

    def expand_command(
        self,
        text: str,
        pos: int,
        name: str,
        scope: Sequence[str],
        depth: int,
        chain: tuple[str, ...],
    ) -> tuple[int, str, MacroDefinition]:
        if depth > self.max_depth:
            raise _Skip("depth_limit", name)
        if name in chain:
            raise _Skip("recursive_definition", name)
        definition = self.resolve(name, scope)
        cursor = pos + 1 + len(name)
        arguments: list[str] = []
        if definition.default is not None:
            probe = _skip_ws(text, cursor)
            if probe < len(text) and text[probe] == "[":
                group = _read_group(text, probe, "[", "]")
                if group is None:
                    raise _Skip("malformed_optional_argument", name)
                arguments.append(group[0])
                cursor = group[1]
            else:
                arguments.append(definition.default)
        while len(arguments) < definition.nargs:
            argument = _read_argument(text, cursor)
            if argument is None:
                raise _Skip("missing_arguments", name)
            arguments.append(argument[0])
            cursor = argument[1]
        expanded_arguments = [
            self.expand_text(argument, scope, depth + 1, (*chain, name))
            for argument in arguments
        ]
        body = self.expand_text(
            definition.template,
            self.tables.document_scope((definition.source_id,)),
            depth + 1,
            (*chain, name),
        )
        return cursor, _substitute(body, expanded_arguments), definition

    def expand_text(
        self, text: str, scope: Sequence[str], depth: int, chain: tuple[str, ...]
    ) -> str:
        out = ""
        boundary = False
        index = 0
        while index < len(text):
            character = text[index]
            if character != "\\":
                out = _join(out, character) if boundary else out + character
                boundary = False
                index += 1
                continue
            boundary = False
            letters = _LETTERS.match(text, index + 1)
            if letters is None:
                out += text[index : index + 2]
                index += 2
                continue
            name = letters.group(0)
            if not self.expandable_name(name):
                out += text[index : letters.end()]
                index = letters.end()
                continue
            try:
                end, expansion, _definition = self.expand_command(
                    text, index, name, scope, depth, chain
                )
            except _Skip as skip:
                if skip.reason in {"depth_limit", "recursive_definition"}:
                    raise
                # Left in place; the whitelist check on the outer result
                # refuses the whole expansion.
                out += text[index : letters.end()]
                index = letters.end()
                continue
            out += expansion
            boundary = True
            index = end
        return out

    def unsupported_in(self, text: str) -> list[str]:
        names = []
        for match in _COMMAND_TOKEN.finditer(text):
            if _odd_backslashes(text, match.start()):
                continue
            command = match.group(1)
            if (
                not self.whitelist.is_supported_command(command)
                and command not in names
            ):
                names.append(command)
        return names


@dataclass(frozen=True)
class FragmentExpansion:
    """One math fragment expanded for typesetting only (the record is intact)."""

    text: str
    expansions: tuple[dict[str, Any], ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.expansions)


def _is_whole_fragment(edits: Sequence[Replacement], fragment: Any) -> bool:
    """Was this whole math fragment one manuscript macro and its arguments?

    ``$\\ee$`` is notation, not a quoted formula: the source's own definition
    says what it stands for, so writing that out changes no mathematics.
    ``$E=\\hbar\\omegazero$`` is a formula, however short, and is quoted as
    written.
    """

    if len(edits) != 1:
        return False
    text = fragment.value
    start = fragment.start + len(text) - len(text.lstrip())
    return edits[0].start == start and edits[0].end == fragment.start + len(
        text.rstrip()
    )


def typeset_expansion_scope(
    fragment: str,
    *,
    tables: SourceMacroTables,
    cited_source_ids: Sequence[str] = (),
) -> tuple[tuple[str, ...], str]:
    """Which registered sources define the macros of one fragment, and why.

    The same attribution order normalization uses: a fragment that occurs
    verbatim in exactly one registered source is read against that document,
    otherwise against the sources the step cites, otherwise against all of
    them.
    """

    verbatim = tables.verbatim_sources(fragment)
    if len(verbatim) == 1:
        return tables.document_scope(verbatim), "verbatim_attribution"
    if cited_source_ids:
        return tables.document_scope(cited_source_ids), "cited_sources"
    return tables.source_ids, "all_sources"


def expand_math_for_typesetting(
    fragment: str,
    *,
    tables: SourceMacroTables,
    whitelist: EngineWhitelist,
    scope: Sequence[str] | None = None,
    max_depth: int = MAX_EXPANSION_DEPTH,
) -> FragmentExpansion:
    """Expand manuscript macros of one math fragment for typesetting only.

    The expansion rules are the ones normalization applies to non-quotation
    text: a name the engine compiles is never expanded, the definition must be
    unique in ``scope``, and an expansion that would introduce a command
    outside the engine whitelist or change the math structure is refused and
    reported. Quotations keep their recorded notation - this produces a
    separate typeset copy and never a Record edit, which is why it is the one
    place a quotation may be expanded.
    """

    resolved = tuple(tables.source_ids if scope is None else scope)
    expander = _Expander(tables, whitelist, max_depth)
    expansions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    edits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(fragment):
        if fragment[index] != "\\":
            index += 1
            continue
        letters = _LETTERS.match(fragment, index + 1)
        if letters is None:
            index += 2
            continue
        command = letters.group(0)
        if not expander.expandable_name(command):
            index = letters.end()
            continue
        try:
            end, expansion, definition = expander.expand_command(
                fragment, index, command, resolved, 0, ()
            )
            refused = expander.unsupported_in(expansion)
            if refused:
                raise _Skip(
                    "expansion_uses_unsupported_command:" + ",".join(sorted(refused)),
                    command,
                )
            if not _balanced(expansion) or _STRUCTURAL.search(expansion):
                raise _Skip("expansion_changes_math_structure", command)
        except _Skip as skip:
            skipped.append(
                {"start": index, "command": command, "reason": skip.reason}
            )
            index = letters.end()
            continue
        if (
            _CONTROL_WORD_END.search(expansion)
            and fragment[end : end + 1].isalpha()
        ):
            expansion += " "
        edits.append((index, end, expansion))
        expansions.append(
            {
                "start": index,
                "end": end,
                "original": fragment[index:end],
                "replacement": expansion,
                "source_id": definition.source_id,
                "source_line": definition.line,
            }
        )
        index = end
    text = fragment
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return FragmentExpansion(
        text=text, expansions=tuple(expansions), skipped=tuple(skipped)
    )


def restore_control_characters(
    text: str, *, whitelist: EngineWhitelist, tables: SourceMacroTables | None = None
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Layer 1 on one standalone text (see :func:`normalize_step_fields`).

    Used where the text is not a recorded step field - a typeset-layer formula
    correction, for example - so the same JSON transport damage is repaired by
    the same rules without a Record edit list.
    """

    known = whitelist.commands | frozenset(
        name
        for name in (tables.names if tables is not None else frozenset())
        if name.isalpha()
    )
    edits, unrepaired = _restore_control_characters("text", text, known)
    restored = apply_replacements({"text": text}, edits)["text"]
    return restored, tuple(item.to_record() for item in edits), tuple(unrepaired)


def _cited_source_ids(
    fields: Mapping[str, str], source_ids: Sequence[str]
) -> tuple[str, ...]:
    cited = []
    for source_id in source_ids:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(source_id) + r"(?![A-Za-z0-9_])"
        )
        if any(pattern.search(value) for value in fields.values()):
            cited.append(source_id)
    return tuple(cited)


def normalize_step_fields(
    fields: Mapping[str, str],
    *,
    sources: Mapping[str, str],
    whitelist: EngineWhitelist,
    source_documents: Mapping[str, str] | None = None,
    tables: SourceMacroTables | None = None,
    expand_macros: bool = True,
    max_depth: int = MAX_EXPANSION_DEPTH,
) -> NormalizationResult:
    """Normalize the five Writer fields. See the module docstring for the rules.

    ``sources`` maps each registered ``source_id`` to its full text and
    ``source_documents`` optionally groups source ids into documents (see
    :class:`SourceMacroTables`). Pass a prebuilt ``tables`` for the same sources
    and documents to avoid reparsing them.
    ``expand_macros=False`` applies layer 1 only (used where normalized content
    cannot be recorded).
    """

    if set(fields) != set(STEP_FIELDS) or not all(
        isinstance(fields[name], str) for name in STEP_FIELDS
    ):
        raise ValueError("normalization requires the five step text fields")
    tables = (
        tables if tables is not None else SourceMacroTables(sources, source_documents)
    )
    raw = {name: fields[name] for name in STEP_FIELDS}
    known_names = whitelist.commands | frozenset(
        name for name in tables.names if name.isalpha()
    )

    layer_one: list[Replacement] = []
    unrepaired: list[dict[str, Any]] = []
    text_one: dict[str, str] = {}
    for name in STEP_FIELDS:
        edits, problems = _restore_control_characters(name, raw[name], known_names)
        layer_one.extend(edits)
        unrepaired.extend(problems)
        text_one[name] = apply_replacements({name: raw[name]}, edits)[name]

    cited = _cited_source_ids(text_one, tables.source_ids)
    cited_scope = tables.document_scope(cited)
    expander = _Expander(tables, whitelist, max_depth)
    layer_two: list[Replacement] = []
    skipped: list[dict[str, Any]] = []
    quotations: set[tuple[str, int]] = set()
    for name in STEP_FIELDS:
        if name in QUOTATION_FIELDS:
            continue
        value = text_one[name]
        field_edits: list[Replacement] = []
        for fragment in math_fragments({name: value}):
            verbatim = tables.verbatim_sources(fragment.value)
            if any(source_id in cited_scope for source_id in verbatim):
                quotations.add((name, fragment.formula_index))
                continue
            if not expand_macros:
                continue
            if len(verbatim) == 1:
                scope, rule = tables.document_scope(verbatim), "verbatim_attribution"
            elif cited:
                scope, rule = cited_scope, "cited_sources"
            else:
                scope, rule = tables.source_ids, "all_sources"
            fragment_edits: list[Replacement] = []
            fragment_text = fragment.value
            index = 0
            while index < len(fragment_text):
                if fragment_text[index] != "\\":
                    index += 1
                    continue
                letters = _LETTERS.match(fragment_text, index + 1)
                if letters is None:
                    index += 2
                    continue
                command = letters.group(0)
                if not expander.expandable_name(command):
                    index = letters.end()
                    continue
                absolute = fragment.start + index
                try:
                    end, expansion, definition = expander.expand_command(
                        fragment_text, index, command, scope, 0, ()
                    )
                    refused = expander.unsupported_in(expansion)
                    if refused:
                        raise _Skip(
                            "expansion_uses_unsupported_command:"
                            + ",".join(sorted(refused)),
                            command,
                        )
                    if not _balanced(expansion) or _STRUCTURAL.search(expansion):
                        raise _Skip("expansion_changes_math_structure", command)
                except _Skip as skip:
                    skipped.append(
                        {
                            "field": name,
                            "formula_index": fragment.formula_index,
                            "start": absolute,
                            "command": command,
                            "rule": rule,
                            "reason": skip.reason,
                        }
                    )
                    index = letters.end()
                    continue
                absolute_end = fragment.start + end
                if (
                    _CONTROL_WORD_END.search(expansion)
                    and value[absolute_end : absolute_end + 1].isalpha()
                ):
                    expansion += " "
                fragment_edits.append(
                    Replacement(
                        name,
                        absolute,
                        absolute_end,
                        value[absolute:absolute_end],
                        expansion,
                        "macro_expansion",
                        definition.source_id,
                        definition.line,
                    )
                )
                index = end
            # ``QUOTATION_MIN_CHARS`` decides whether a verbatim match is
            # evidence of quotation (above), and nothing else. A shorter
            # fragment found verbatim in a cited source is still that author's
            # text, so the host does not rewrite it either - unless the whole
            # fragment is one of the author's own abbreviations, which its own
            # definition expands without saying anything different.
            protected = any(
                source_id in cited_scope
                for source_id in tables.verbatim_sources(fragment.value, min_chars=1)
            )
            if (
                fragment_edits
                and protected
                and not _is_whole_fragment(fragment_edits, fragment)
            ):
                skipped.append(
                    {
                        "field": name,
                        "formula_index": fragment.formula_index,
                        "start": fragment.start,
                        "command": None,
                        "rule": rule,
                        "reason": "short_verbatim_quotation_of_cited_source",
                    }
                )
                continue
            field_edits.extend(fragment_edits)
        field_edits.sort(key=lambda item: item.start, reverse=True)
        layer_two.extend(field_edits)

    replacements = tuple(layer_one + layer_two)
    normalized = apply_replacements(raw, replacements)
    return NormalizationResult(
        fields=normalized,
        replacements=replacements,
        quotation_fragments=frozenset(quotations),
        cited_source_ids=cited,
        unrepaired_control=tuple(unrepaired),
        skipped_macros=tuple(skipped),
    )


__all__ = [
    "CONTROL_KINDS",
    "MAX_EXPANSION_DEPTH",
    "NORMALIZER_VERSION",
    "QUOTATION_MIN_CHARS",
    "REPLACEMENT_KINDS",
    "FragmentExpansion",
    "MacroDefinition",
    "NormalizationResult",
    "Replacement",
    "SourceMacroTables",
    "apply_replacements",
    "expand_math_for_typesetting",
    "normalize_step_fields",
    "parse_macro_definitions",
    "restore_control_characters",
    "revert_replacements",
    "typeset_expansion_scope",
]
