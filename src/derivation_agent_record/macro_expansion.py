"""Independent verification of a recorded ``macro_expansion`` replacement.

A ``writer_output_normalized`` event may replace a manuscript macro in the
Writer's sealed text with the expansion of its definition in a registered
source. That is the one normalization kind whose replacement text is not fixed
by its own shape: a control repair can only insert a backslash or delete a
control character, but an expansion can put *anything* there. Without a check,
a host could rewrite sealed scientific content and call it an expansion.

So the record package derives the expansion itself, from the frozen source
text, and requires the recorded replacement to be what the cited definition
produces. Nothing here consults the runtime: the parser and the expander below
are a second implementation of the same rules, which is the point.

What is verified
----------------
* the cited ``source_id``/``source_line`` carry a definition (``\\newcommand``,
  ``\\renewcommand``, ``\\providecommand``, ``\\def``, ``\\gdef`` or
  ``\\DeclareMathOperator``) of exactly the macro name the replacement consumed;
* the arguments the replacement claims to have consumed are the ones TeX would
  read at that position, and the recorded ``original`` ends exactly where that
  reading ends;
* the recorded ``replacement`` is the substitution of those arguments into that
  definition's body - one level, which is exact whenever nothing in the body or
  the arguments is itself a registered macro, and otherwise re-derived with a
  bounded recursive expansion over the registered definitions.

What is not verified: expansion is checked one definition at a time against the
text that was frozen before any model call. It does not model TeX category
codes, ``\\let`` or ``\\expandafter``, delimited parameter text, or definitions
that appear only in a package the source ``\\input``s; definitions the parser
cannot read are skipped rather than guessed at, and a replacement citing one of
them is rejected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MAX_EXPANSION_DEPTH = 8

_LETTERS = re.compile(r"[A-Za-z]+")
_WHITESPACE = re.compile(r"\s+")
_CONTROL_WORD_END = re.compile(r"\\[A-Za-z]+$")
#: Characters an expansion may never introduce: math delimiters, comments,
#: parameter tokens and code spans all change how the body is tokenized.
STRUCTURAL = re.compile(r"(?<!\\)[$%#`]|\\[()\[\]]")
_DEF_START = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator|def|gdef)"
    r"(?![A-Za-z@])(\*?)"
)


class MacroCheckError(Exception):
    """The recorded expansion is not the one its cited definition produces."""


@dataclass(frozen=True)
class Definition:
    name: str
    kind: str
    star: bool
    nargs: int
    default: str | None
    template: str
    source_id: str
    line: int

    @property
    def key(self) -> tuple[int, str | None, str]:
        return (self.nargs, self.default, _WHITESPACE.sub(" ", self.template).strip())


def _odd_backslashes(text: str, index: int) -> bool:
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def _strip_comments(text: str) -> str:
    """Blank TeX comments while keeping every line, so line numbers stay exact."""

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


def parse_definitions(text: str, source_id: str) -> list[Definition]:
    """Every macro definition in one source text; unparseable ones are skipped."""

    text = _strip_comments(text)
    definitions: list[Definition] = []
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
            group = _read_group(text, param_end)
            if group is None:
                pos = match.end()
                continue
            body, cursor = group
            if not re.fullmatch(r"(#[1-9])*", params.strip()) or any(
                item != f"#{index + 1}" for index, item in enumerate(placeholders)
            ):
                # Delimited parameter text: the runtime never expands these.
                pos = cursor
                continue
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
        if re.fullmatch(r"[A-Za-z]+", name) and nargs <= 9:
            definitions.append(
                Definition(
                    name=name,
                    kind=kind,
                    star=star,
                    nargs=nargs,
                    default=default,
                    template=template,
                    source_id=source_id,
                    line=line_of(start),
                )
            )
        pos = cursor
    return definitions


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


def balanced(text: str) -> bool:
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


class DefinitionTable:
    """Definitions of every registered source, indexed for verification."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        self.by_source: dict[str, list[Definition]] = {
            source_id: parse_definitions(text, source_id)
            for source_id, text in sources.items()
        }
        self.names: frozenset[str] = frozenset(
            item.name for items in self.by_source.values() for item in items
        )

    def at_line(self, source_id: str, line: int, name: str) -> Definition | None:
        for item in self.by_source.get(source_id, ()):
            if item.line == line and item.name == name:
                return item
        return None

    def unique(self, name: str) -> Definition | None:
        found = [
            item
            for items in self.by_source.values()
            for item in items
            if item.name == name
        ]
        if not found or len({item.key for item in found}) != 1:
            return None
        return found[0]


def _read_arguments(
    text: str, pos: int, definition: Definition
) -> tuple[list[str], int]:
    """The arguments TeX reads for ``definition`` at ``text[pos:]``."""

    cursor = pos
    arguments: list[str] = []
    if definition.default is not None:
        probe = _skip_ws(text, cursor)
        if probe < len(text) and text[probe] == "[":
            group = _read_group(text, probe, "[", "]")
            if group is None:
                raise MacroCheckError("malformed optional argument")
            arguments.append(group[0])
            cursor = group[1]
        else:
            arguments.append(definition.default)
    while len(arguments) < definition.nargs:
        argument = _read_argument(text, cursor)
        if argument is None:
            raise MacroCheckError("the recorded original has too few arguments")
        arguments.append(argument[0])
        cursor = argument[1]
    return arguments, cursor


def _expand_text(
    text: str, table: DefinitionTable, depth: int, chain: tuple[str, ...]
) -> str:
    """Best-effort recursion over registered macros, mirroring the runtime."""

    if depth > MAX_EXPANSION_DEPTH:
        raise MacroCheckError("expansion depth limit")
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
        definition = None if name in chain else table.unique(name)
        if definition is None:
            out += text[index : letters.end()]
            index = letters.end()
            continue
        try:
            arguments, cursor = _read_arguments(text, letters.end(), definition)
        except MacroCheckError:
            out += text[index : letters.end()]
            index = letters.end()
            continue
        out += _substitute(
            _expand_text(definition.template, table, depth + 1, (*chain, name)),
            [
                _expand_text(argument, table, depth + 1, (*chain, name))
                for argument in arguments
            ],
        )
        boundary = True
        index = cursor
    return out


def verify_macro_expansion(
    *,
    field_text: str,
    start: int,
    end: int,
    original: str,
    replacement: str,
    source_id: str,
    source_line: int,
    sources: Mapping[str, str] | None = None,
    table: DefinitionTable | None = None,
) -> Definition:
    """Check one recorded expansion against its cited definition.

    ``field_text`` is the field as it stands when this edit is applied, so the
    characters after ``end`` are available: the runtime appends a separating
    space when the expansion ends in a control word and a letter follows.
    Raises :class:`MacroCheckError` with a specific reason; returns the cited
    definition when the replacement is the one it produces.
    """

    table = DefinitionTable(sources or {}) if table is None else table
    letters = _LETTERS.match(original, 1)
    if not original.startswith("\\") or letters is None:
        raise MacroCheckError("an expansion must replace a control word")
    name = letters.group(0)
    definition = table.at_line(source_id, source_line, name)
    if definition is None:
        raise MacroCheckError(
            f"{source_id} line {source_line} does not define \\{name}"
        )
    arguments, cursor = _read_arguments(original, letters.end(), definition)
    if cursor != len(original):
        raise MacroCheckError(
            "the recorded original is not exactly the macro and its arguments"
        )
    if not balanced(replacement) or STRUCTURAL.search(replacement):
        raise MacroCheckError("the replacement changes the math structure")
    expected = _substitute(definition.template, arguments)
    trailing = field_text[end : end + 1]
    candidates = {expected}
    if _CONTROL_WORD_END.search(expected) and trailing.isalpha():
        candidates.add(expected + " ")
    if replacement in candidates:
        return definition
    # The body or an argument names another registered macro, so the runtime
    # expanded further. Re-derive it the same way and require that instead.
    nested = _substitute(
        _expand_text(definition.template, table, 1, (name,)),
        [_expand_text(argument, table, 1, (name,)) for argument in arguments],
    )
    if replacement == nested or (
        _CONTROL_WORD_END.search(nested)
        and trailing.isalpha()
        and replacement == nested + " "
    ):
        return definition
    raise MacroCheckError(
        f"\\{name} expands to {expected!r} under its cited definition, "
        f"not to {replacement!r}"
    )


__all__ = [
    "MAX_EXPANSION_DEPTH",
    "Definition",
    "DefinitionTable",
    "MacroCheckError",
    "parse_definitions",
    "verify_macro_expansion",
]
