"""Pure scientific-text tokenization and conservative TeX diagnostics.

Offsets are zero-based Unicode character offsets in the original field; end is
exclusive. Formula indices are one-based within each field. No input is changed.

Two command vocabularies exist. ``formula-v1`` (archived runs) uses the frozen
hand-written sets below, so archived audits replay unchanged. ``formula-v2``
passes an :class:`EngineWhitelist` loaded from the committed
``formula_engine_whitelist.json``, which the locked TeX engine generated; the
unsafe classification is identical under both vocabularies.
"""

from __future__ import annotations

import functools
import hashlib
import json
import platform
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

_MATH_COMMAND = re.compile(r"\\([A-Za-z@]+|.)")
_MATH_ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")
_SUPPORTED_MATH_ENVIRONMENTS = frozenset(
    {
        "aligned",
        "alignedat",
        "array",
        "bmatrix",
        "cases",
        "gathered",
        "matrix",
        "pmatrix",
        "smallmatrix",
        "split",
        "vmatrix",
        "Vmatrix",
    }
)
_ALLOWED_MATH_COMMANDS = frozenset(
    {
        # Structure, sizing, accents, and presentation.
        "boxed",
        "cdots",
        "bf",
        "rm",
        "cal",
        "begin",
        "end",
        "frac",
        "dfrac",
        "tfrac",
        "sqrt",
        "left",
        "right",
        "middle",
        "big",
        "Big",
        "bigg",
        "Bigg",
        "bigl",
        "bigr",
        "Bigl",
        "Bigr",
        "biggl",
        "biggr",
        "Biggl",
        "Biggr",
        "overline",
        "underline",
        "widehat",
        "widetilde",
        "hat",
        "tilde",
        "bar",
        "vec",
        "dot",
        "ddot",
        "dddot",
        "ddddot",
        "breve",
        "check",
        "acute",
        "grave",
        "mathring",
        "overset",
        "underset",
        "underbrace",
        "overbrace",
        "substack",
        "text",
        # Math alphabets and style controls.
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathtt",
        "mathcal",
        "mathbb",
        "mathfrak",
        "boldsymbol",
        "displaystyle",
        "textstyle",
        "scriptstyle",
        "scriptscriptstyle",
        "operatorname",
        "operatornamewithlimits",
        # Greek letters and common letter-like symbols.
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "varepsilon",
        "zeta",
        "eta",
        "theta",
        "vartheta",
        "iota",
        "kappa",
        "lambda",
        "mu",
        "nu",
        "xi",
        "omicron",
        "pi",
        "varpi",
        "rho",
        "varrho",
        "sigma",
        "varsigma",
        "tau",
        "upsilon",
        "phi",
        "varphi",
        "chi",
        "psi",
        "omega",
        "Gamma",
        "Delta",
        "Theta",
        "Lambda",
        "Xi",
        "Pi",
        "Sigma",
        "Upsilon",
        "Phi",
        "Psi",
        "Omega",
        "ell",
        "hbar",
        "imath",
        "jmath",
        "Re",
        "Im",
        "wp",
        "partial",
        "nabla",
        "infty",
        "emptyset",
        "varnothing",
        # Operators and functions.
        "sum",
        "prod",
        "coprod",
        "int",
        "iint",
        "iiint",
        "iiiint",
        "oint",
        "bigcap",
        "bigcup",
        "bigsqcup",
        "bigvee",
        "bigwedge",
        "bigodot",
        "bigotimes",
        "bigoplus",
        "biguplus",
        "lim",
        "limsup",
        "liminf",
        "sup",
        "inf",
        "max",
        "min",
        "det",
        "gcd",
        "Pr",
        "ker",
        "dim",
        "hom",
        "arg",
        "sin",
        "cos",
        "tan",
        "cot",
        "sec",
        "csc",
        "arcsin",
        "arccos",
        "arctan",
        "sinh",
        "cosh",
        "tanh",
        "coth",
        "exp",
        "log",
        "ln",
        "deg",
        # Binary operations, relations, sets, and logic.
        "pm",
        "mp",
        "times",
        "div",
        "cdot",
        "ast",
        "star",
        "circ",
        "bullet",
        "oplus",
        "ominus",
        "otimes",
        "oslash",
        "odot",
        "dagger",
        "ddagger",
        "cap",
        "cup",
        "uplus",
        "sqcap",
        "sqcup",
        "vee",
        "wedge",
        "setminus",
        "wr",
        "diamond",
        "triangleleft",
        "triangleright",
        "bigtriangleup",
        "bigtriangledown",
        "lhd",
        "rhd",
        "unlhd",
        "unrhd",
        "le",
        "leq",
        "ge",
        "geq",
        "neq",
        "ne",
        "equiv",
        "approx",
        "sim",
        "simeq",
        "cong",
        "propto",
        "prec",
        "succ",
        "preceq",
        "succeq",
        "ll",
        "gg",
        "subset",
        "supset",
        "subseteq",
        "supseteq",
        "sqsubset",
        "sqsupset",
        "sqsubseteq",
        "sqsupseteq",
        "in",
        "ni",
        "notin",
        "vdash",
        "dashv",
        "models",
        "perp",
        "parallel",
        "mid",
        "nmid",
        "smile",
        "frown",
        "asymp",
        "bowtie",
        "not",
        "forall",
        "exists",
        "nexists",
        "neg",
        "land",
        "lor",
        "therefore",
        "because",
        # Arrows and delimiters.
        "to",
        "gets",
        "mapsto",
        "leftarrow",
        "rightarrow",
        "leftrightarrow",
        "Leftarrow",
        "Rightarrow",
        "Leftrightarrow",
        "longleftarrow",
        "longrightarrow",
        "longleftrightarrow",
        "Longleftarrow",
        "Longrightarrow",
        "Longleftrightarrow",
        "uparrow",
        "downarrow",
        "updownarrow",
        "Uparrow",
        "Downarrow",
        "Updownarrow",
        "nearrow",
        "searrow",
        "swarrow",
        "nwarrow",
        "hookleftarrow",
        "hookrightarrow",
        "leftharpoonup",
        "leftharpoondown",
        "rightharpoonup",
        "rightharpoondown",
        "rightleftharpoons",
        "leadsto",
        "langle",
        "rangle",
        "lceil",
        "rceil",
        "lfloor",
        "rfloor",
        "lvert",
        "rvert",
        "lVert",
        "rVert",
        "vert",
        "Vert",
        # Spacing and punctuation commands used in conventional math.
        "quad",
        "qquad",
        "enspace",
        "thinspace",
        "medspace",
        "thickspace",
    }
)
_ALLOWED_MATH_CONTROL_SYMBOLS = frozenset(
    {",", ";", ":", "!", " ", "\\", "{", "}", "_", "^", "$", "&", "|", "/"}
)


def _is_escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _find_unescaped(value: str, delimiter: str, start: int) -> int:
    cursor = start
    while True:
        cursor = value.find(delimiter, cursor)
        if cursor < 0:
            return -1
        if not _is_escaped(value, cursor):
            return cursor
        cursor += len(delimiter)


def _is_currency_dollar(value: str, index: int) -> bool:
    """Recognize an isolated amount such as ``$5`` without stealing ``$5+x$`` math."""

    match = re.match(r"\d+(?:,\d{3})*(?:\.\d{1,2})?", value[index + 1 :])
    if match is None:
        return False
    end = index + 1 + len(match.group(0))
    following = value[end : end + 1]
    if following == "$":
        return False
    # Look beyond the number: juxtaposition and a TeX command are ordinary
    # mathematics too (``$2 x$``, ``$2 \\alpha$``). A dollar introducing the
    # next amount/word after whitespace is not this expression's closer.
    closing = _find_unescaped(value, "$", end)
    if (
        closing >= 0
        and "`" not in value[end:closing]
        and "\n" not in value[end:closing]
    ):
        before = value[closing - 1 : closing]
        after = value[closing + 1 : closing + 2]
        if (
            before
            and not before.isspace()
            or not after
            or after.isspace()
            or after in ",.!?;:"
        ):
            return False
    rest = value[end:]
    if re.match(r"\s*[+\-*/=<>^_]", rest):
        return False
    return not following or following.isspace() or following in ",.!?;:"


def _find_backtick_run(value: str, length: int, start: int) -> int:
    """Match a complete run, never a prefix of a longer code delimiter."""
    for match in re.finditer(r"`+", value[start:]):
        if len(match.group()) == length:
            return start + match.start()
    return -1


@dataclass(frozen=True)
class _TextToken:
    kind: Literal["prose", "code", "inline_math", "display_math"]
    value: str
    start: int = 0
    end: int = 0
    closed: bool = True


def _scientific_tokens(value: str) -> list[_TextToken]:
    tokens: list[_TextToken] = []
    prose_start = 0
    cursor = 0

    def append_prose(end: int) -> None:
        nonlocal prose_start
        if end > prose_start:
            tokens.append(_TextToken("prose", value[prose_start:end], prose_start, end))

    while cursor < len(value):
        opener = ""
        closer = ""
        kind: Literal["code", "inline_math", "display_math"] | None = None
        if value[cursor] == "`" and not _is_escaped(value, cursor):
            run_end = cursor + 1
            while run_end < len(value) and value[run_end] == "`":
                run_end += 1
            opener = closer = value[cursor:run_end]
            kind = "code"
        elif value.startswith("$$", cursor) and not _is_escaped(value, cursor):
            opener = closer = "$$"
            kind = "display_math"
        elif value.startswith(r"\[", cursor) and not _is_escaped(value, cursor):
            opener, closer = r"\[", r"\]"
            kind = "display_math"
        elif value.startswith(r"\(", cursor) and not _is_escaped(value, cursor):
            opener, closer = r"\(", r"\)"
            kind = "inline_math"
        elif (
            value[cursor] == "$"
            and not _is_escaped(value, cursor)
            and not _is_currency_dollar(value, cursor)
        ):
            opener = closer = "$"
            kind = "inline_math"

        if kind is None:
            cursor += 1
            continue
        end = (
            _find_backtick_run(value, len(opener), cursor + len(opener))
            if kind == "code"
            else _find_unescaped(value, closer, cursor + len(opener))
        )
        if end < 0:
            append_prose(cursor)
            tokens.append(
                _TextToken(
                    kind,
                    value[cursor + len(opener) :],
                    cursor + len(opener),
                    len(value),
                    False,
                )
            )
            prose_start = len(value)
            break
        body = value[cursor + len(opener) : end]
        append_prose(cursor)
        tokens.append(_TextToken(kind, body, cursor + len(opener), end))
        cursor = end + len(closer)
        prose_start = cursor
    append_prose(len(value))
    return tokens


def _balanced_braces(value: str) -> bool:
    depth = 0
    for index, character in enumerate(value):
        if _is_escaped(value, index):
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


scientific_tokens = _scientific_tokens


@dataclass(frozen=True)
class FormulaDiagnostic:
    code: str
    field: str
    formula_index: int
    start: int
    end: int
    message: str


@dataclass(frozen=True)
class FormulaFragment:
    field: str
    formula_index: int
    start: int
    end: int
    value: str
    kind: str


def math_fragments(fields: Mapping[str, str]) -> Iterator[FormulaFragment]:
    for field, value in fields.items():
        index = 0
        for token in scientific_tokens(value):
            if token.kind.endswith("math"):
                index += 1
                yield FormulaFragment(
                    field, index, token.start, token.end, token.value, token.kind
                )


_UNSAFE_COMMANDS = frozenset(
    [
        "input",
        "include",
        "openin",
        "openout",
        "read",
        "write",
        "immediate",
        "special",
        "catcode",
        "csname",
        "endcsname",
        "def",
        "edef",
        "gdef",
        "xdef",
        "let",
        "futurelet",
        "newcommand",
        "renewcommand",
        "providecommand",
        "newenvironment",
        "renewenvironment",
        "usepackage",
        "documentclass",
        "font",
        "hbox",
        "vbox",
        "shipout",
        "loop",
        "repeat",
        "directlua",
        "luaexec",
        "scantokens",
        "everyjob",
        "everymath",
        "everydisplay",
        "global",
        "expandafter",
        "noexpand",
    ]
)


ENGINE_WHITELIST_SCHEMA_VERSION = "formula-engine-whitelist-v1"
ENGINE_WHITELIST_PATH = Path(__file__).with_name("formula_engine_whitelist.json")


@dataclass(frozen=True)
class EngineWhitelist:
    """Math vocabulary the locked TeX engine compiles under the report preamble.

    ``commands`` are control words (letters), ``control_symbols`` are
    single-character control sequences, ``environments`` are math environment
    names. Unsafe names are never part of the vocabulary: validation classifies
    them before consulting this object.
    """

    commands: frozenset[str]
    control_symbols: frozenset[str]
    environments: frozenset[str]
    unsafe_excluded: frozenset[str] = frozenset()
    schema_version: str = ENGINE_WHITELIST_SCHEMA_VERSION
    sha256: str | None = None
    engine: tuple[tuple[str, str], ...] = ()
    preamble_sha256: str | None = None

    @classmethod
    def from_record(
        cls, value: Mapping[str, Any], *, sha256: str | None = None
    ) -> EngineWhitelist:
        if not isinstance(value, Mapping):
            # One error type for every malformed whitelist file.
            raise ValueError("engine whitelist must be a JSON object")  # noqa: TRY004
        if value.get("schema_version") != ENGINE_WHITELIST_SCHEMA_VERSION:
            raise ValueError("unsupported engine whitelist schema_version")
        supported = value.get("supported")
        if not isinstance(supported, Mapping) or not supported:
            raise ValueError("engine whitelist needs a non-empty supported mapping")
        commands: set[str] = set()
        symbols: set[str] = set()
        for name, templates in supported.items():
            if not isinstance(name, str) or not name:
                raise ValueError("engine whitelist command names must be text")
            if not isinstance(templates, list) or not templates:
                raise ValueError(f"engine whitelist entry {name!r} has no template")
            (commands if re.fullmatch(r"[A-Za-z@]+", name) else symbols).add(name)
        raw_symbols = value.get("control_symbols", [])
        raw_environments = value.get("environments")
        raw_unsafe = value.get("unsafe_excluded", [])
        for label, items in (
            ("control_symbols", raw_symbols),
            ("environments", raw_environments),
            ("unsafe_excluded", raw_unsafe),
        ):
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item for item in items
            ):
                raise ValueError(f"engine whitelist {label} must be a list of names")
        if any(len(item) != 1 for item in raw_symbols):
            raise ValueError("engine whitelist control symbols are single characters")
        engine = value.get("engine", {})
        if not isinstance(engine, Mapping):
            raise ValueError("engine whitelist engine must be an object")  # noqa: TRY004
        preamble = value.get("preamble_sha256")
        return cls(
            commands=frozenset(commands),
            control_symbols=frozenset(symbols | set(raw_symbols)),
            environments=frozenset(raw_environments),
            unsafe_excluded=frozenset(raw_unsafe),
            schema_version=ENGINE_WHITELIST_SCHEMA_VERSION,
            sha256=sha256,
            engine=tuple(sorted((str(k), str(v)) for k, v in engine.items())),
            preamble_sha256=preamble if isinstance(preamble, str) else None,
        )

    def is_supported_command(self, name: str) -> bool:
        return name in self.commands or name in self.control_symbols

    def identity(self) -> dict[str, Any]:
        """Small, hash-bound provenance suitable for an audit payload."""
        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "engine": dict(self.engine),
            "preamble_sha256": self.preamble_sha256,
        }


REGENERATE_WHITELIST = (
    "regenerate it with tools/formula_whitelist/generate.py on this platform "
    "(see tools/formula_whitelist/README.md)"
)


def host_target() -> str:
    """The ``system-machine`` target string of the running platform.

    The same spelling the Tectonic runtime lock uses; kept here so the
    whitelist can be checked without importing the report layer.
    """

    machine = platform.machine().casefold()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().casefold()
    )
    return f"{system}-{machine}"


def _report_preamble() -> str | None:
    """The report preamble the whitelist was generated against, if reachable."""

    try:  # A lazy, guarded import: derivation_runtime does not depend on the
        # report layer, and the record package must stay usable without it.
        from derivation_app.reporting import REPORT_TEX_PREAMBLE
    except ImportError:  # the report layer is absent: nothing to check against
        return None
    return REPORT_TEX_PREAMBLE


def verify_engine_whitelist(
    whitelist: EngineWhitelist,
    *,
    preamble: str | None = None,
    target: str | None = None,
) -> None:
    """Fail closed when the whitelist does not describe *this* engine.

    The vocabulary is the engine's answer, not a rule: it is only meaningful
    for the preamble it was compiled under and the platform whose binary
    produced it. A whitelist that has drifted from either would silently accept
    or reject commands the running engine treats the other way, so it is
    refused instead of used.
    """

    expected_preamble = _report_preamble() if preamble is None else preamble
    if expected_preamble is not None and whitelist.preamble_sha256 is not None:
        current = hashlib.sha256(expected_preamble.encode("utf-8")).hexdigest()
        if current != whitelist.preamble_sha256:
            raise ValueError(
                "engine whitelist was generated for a different report preamble "
                f"({whitelist.preamble_sha256} != {current}): {REGENERATE_WHITELIST}"
            )
    engine = dict(whitelist.engine)
    recorded_target = engine.get("target")
    running = host_target() if target is None else target
    if recorded_target is not None and recorded_target != running:
        raise ValueError(
            f"engine whitelist was generated on {recorded_target!r}, not on the "
            f"running platform {running!r}: {REGENERATE_WHITELIST}"
        )


def load_engine_whitelist(
    path: str | Path | None = None, *, verify: bool = True
) -> EngineWhitelist:
    """Load and validate an engine whitelist file (default: the committed one).

    The default file is read lazily and cached for the process; an explicit
    path is always read fresh. A missing or malformed file raises, and so does
    one that does not match the running preamble and platform (see
    :func:`verify_engine_whitelist`).

    ``verify=False`` exists for synthetic fixtures, which describe no real
    engine and are never the vocabulary a run is validated against. No
    production caller passes it; every one of them takes the committed file.
    """

    if path is None:
        return _default_engine_whitelist()
    raw = Path(path).read_bytes()
    whitelist = EngineWhitelist.from_record(
        json.loads(raw.decode("utf-8")), sha256=hashlib.sha256(raw).hexdigest()
    )
    if verify:
        verify_engine_whitelist(whitelist)
    return whitelist


@functools.lru_cache(maxsize=1)
def _default_engine_whitelist() -> EngineWhitelist:
    raw = ENGINE_WHITELIST_PATH.read_bytes()
    whitelist = EngineWhitelist.from_record(
        json.loads(raw.decode("utf-8")), sha256=hashlib.sha256(raw).hexdigest()
    )
    verify_engine_whitelist(whitelist)
    return whitelist


def _is_unsafe_command(command: str) -> bool:
    return command in _UNSAFE_COMMANDS or command.lower().startswith(
        ("pdf", "xetex", "luatex")
    )


def validate_math(
    value: str,
    field: str = "",
    formula_index: int = 1,
    start: int = 0,
    *,
    whitelist: EngineWhitelist | None = None,
) -> list[FormulaDiagnostic]:
    diagnostics: list[FormulaDiagnostic] = []
    allowed_commands = (
        _ALLOWED_MATH_COMMANDS if whitelist is None else whitelist.commands
    )
    allowed_symbols = (
        _ALLOWED_MATH_CONTROL_SYMBOLS
        if whitelist is None
        else whitelist.control_symbols
    )
    environments_allowed = (
        _SUPPORTED_MATH_ENVIRONMENTS if whitelist is None else whitelist.environments
    )

    def add(code: str, offset: int, end: int, message: str) -> None:
        diagnostics.append(
            FormulaDiagnostic(
                code, field, formula_index, start + offset, start + end, message
            )
        )

    if not value.strip():
        add("syntax_error", 0, len(value), "Empty mathematical expression")
    for i, c in enumerate(value):
        if c == "$" and not _is_escaped(value, i):
            add("syntax_error", i, i + 1, "Nested math delimiter")
        if (ord(c) < 32 and c not in "\n\r\t") or 127 <= ord(c) <= 159:
            add("control_character", i, i + 1, f"Control character U+{ord(c):04X}")
        if c in "%#" and not _is_escaped(value, i):
            add(
                "unsafe_command",
                i,
                i + 1,
                "TeX comment or parameter syntax is disabled",
            )
    if "^^" in value:
        i = value.index("^^")
        add("unsafe_command", i, i + 2, "TeX character-code expansion is disabled")
    for match in _MATH_COMMAND.finditer(value):
        command = match.group(1)
        if command in {"(", ")", "[", "]"}:
            add("syntax_error", match.start(), match.end(), "Nested math delimiter")
        # The frozen v1 vocabulary never contains an unsafe name, so checking
        # unsafe first changes nothing there and keeps a generated vocabulary
        # from ever admitting one.
        unsafe = _is_unsafe_command(command)
        if unsafe or (
            command not in allowed_commands and command not in allowed_symbols
        ):
            add(
                "unsafe_command" if unsafe else "unsupported_command",
                match.start(),
                match.end(),
                f"Disabled TeX command: \\{command}"
                if unsafe
                else f"Unsupported TeX command: \\{command}; supply a standard self-contained expansion from its source definition",
            )
    if not _balanced_braces(value):
        add("syntax_error", 0, len(value), "Unbalanced braces")
    environments: list[str] = []
    for match in _MATH_ENVIRONMENT.finditer(value):
        action, name = match.groups()
        if name not in environments_allowed:
            add(
                "unsupported_command",
                match.start(),
                match.end(),
                f"Unsupported math environment: {name}",
            )
        if action == "begin":
            environments.append(name)
        elif not environments or environments.pop() != name:
            add(
                "syntax_error",
                match.start(),
                match.end(),
                "Mismatched math environment",
            )
    if environments:
        add("syntax_error", 0, len(value), "Unclosed math environment")
    return diagnostics


def validate_fields(
    fields: Mapping[str, str], *, whitelist: EngineWhitelist | None = None
) -> list[FormulaDiagnostic]:
    diagnostics: list[FormulaDiagnostic] = []
    for field, value in fields.items():
        index = 0
        for token in scientific_tokens(value):
            if token.kind.endswith("math"):
                index += 1
                if not token.closed:
                    diagnostics.append(
                        FormulaDiagnostic(
                            "syntax_error",
                            field,
                            index,
                            token.start,
                            token.end,
                            "Unclosed math delimiter",
                        )
                    )
                diagnostics.extend(
                    validate_math(
                        token.value, field, index, token.start, whitelist=whitelist
                    )
                )
            elif token.kind == "prose":
                for i, c in enumerate(token.value):
                    if (ord(c) < 32 and c not in "\n\r\t") or 127 <= ord(c) <= 159:
                        diagnostics.append(
                            FormulaDiagnostic(
                                "control_character",
                                field,
                                0,
                                token.start + i,
                                token.start + i + 1,
                                f"Control character U+{ord(c):04X}",
                            )
                        )
                for match in re.finditer(r"\\[\]\)]", token.value):
                    if not _is_escaped(token.value, match.start()):
                        diagnostics.append(
                            FormulaDiagnostic(
                                "syntax_error",
                                field,
                                0,
                                token.start + match.start(),
                                token.start + match.end(),
                                "Unmatched closing math delimiter",
                            )
                        )
    return diagnostics


_ESCAPE_SWALLOW_LETTER = {"\t": "t", "\r": "r", "\n": "n"}
_ASCII_LETTERS = re.compile(r"[A-Za-z]+")


def escape_swallow_diagnostics(
    fields: Mapping[str, str], *, whitelist: EngineWhitelist
) -> list[FormulaDiagnostic]:
    """Math where TAB/CR/LF plus the following letters spell a known command.

    A JSON string ``"\\tau"`` written with one backslash decodes to TAB + ``au``;
    the command letter is gone, so no deterministic repair exists. The output
    usually still compiles, with a different meaning, which is why this is only
    a non-blocking warning for the author to check.
    """

    diagnostics: list[FormulaDiagnostic] = []
    for fragment in math_fragments(fields):
        value = fragment.value
        for index, character in enumerate(value):
            letter = _ESCAPE_SWALLOW_LETTER.get(character)
            if letter is None:
                continue
            # A line break that opens the display or follows spacing or a TeX
            # row break is layout, not a swallowed ``\n`` escape.
            if character == "\n" and (
                index == 0
                or value[index - 1].isspace()
                or value[:index].endswith("\\\\")
            ):
                continue
            match = _ASCII_LETTERS.match(value, index + 1)
            if match is None:
                continue
            following = match.group(0)
            command = letter + following
            if command not in whitelist.commands or following in whitelist.commands:
                continue
            diagnostics.append(
                FormulaDiagnostic(
                    "possible_escape_swallow",
                    fragment.field,
                    fragment.formula_index,
                    fragment.start + index,
                    fragment.start + match.end(),
                    f"Control character U+{ord(character):04X} followed by "
                    f"'{following}' may be a JSON-escaped \\{command} that lost "
                    "its command letter; check the intended symbol",
                )
            )
    return diagnostics


#: Diagnostics that stay errors wherever they occur, quotations included.
ALWAYS_ERROR_CODES = frozenset({"control_character", "unsafe_command"})
WARNING_ONLY_CODES = frozenset({"possible_escape_swallow"})


def formula_v2_issues(
    fields: Mapping[str, str],
    *,
    whitelist: EngineWhitelist,
    quotation_fragments: Sequence[tuple[str, int]] | frozenset[tuple[str, int]] = (),
    quotation_fields: frozenset[str] = frozenset({"source"}),
) -> list[dict[str, Any]]:
    """Static ``formula-v2`` diagnostics with their severity.

    Quotations (the ``source`` field, and prose math that occurs verbatim in a
    cited registered source) keep the cited author's notation, so their
    diagnostics are warnings, except control characters and unsafe syntax,
    which stay errors everywhere. Possible escape swallows are warnings.
    """

    quoted_fragments = frozenset(quotation_fragments)
    issues: list[dict[str, Any]] = []
    for diagnostic in validate_fields(fields, whitelist=whitelist):
        quotation = (
            "source_field"
            if diagnostic.field in quotation_fields
            else "prose_verbatim"
            if (diagnostic.field, diagnostic.formula_index) in quoted_fragments
            and diagnostic.formula_index > 0
            else None
        )
        item = _issue_record(diagnostic, fields)
        item["severity"] = (
            "warning"
            if quotation is not None and diagnostic.code not in ALWAYS_ERROR_CODES
            else "error"
        )
        if quotation is not None:
            item["quotation"] = quotation
        issues.append(item)
    for diagnostic in escape_swallow_diagnostics(fields, whitelist=whitelist):
        item = _issue_record(diagnostic, fields)
        item["severity"] = "warning"
        issues.append(item)
    return issues


def _issue_record(
    diagnostic: FormulaDiagnostic, fields: Mapping[str, str]
) -> dict[str, Any]:
    item = asdict(diagnostic)
    item["excerpt"] = (
        fields[diagnostic.field][diagnostic.start : diagnostic.end][:300]
        .encode("unicode_escape")
        .decode("ascii")
    )
    return item
