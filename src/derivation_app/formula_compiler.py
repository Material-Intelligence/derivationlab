"""Offline product formula compilation; source evidence is never rewritten.

Two entry points share one document layout and one log locator:

* ``ProductFormulaValidator`` is the per-step formula-v1 gate (static checks,
  then one compiled document for the step's math).
* ``compile_fragments`` compiles an arbitrary list of math fragments (for
  example every formula of a finished route) into one document and returns
  every failing fragment, dropping located failures and recompiling until the
  remaining fragments compile.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from derivation_runtime.formula_validation import (
    math_fragments,
    validate_fields,
    validate_math,
)
from derivation_runtime.types import FormulaValidationResult, StepContent

from .reporting import (
    REPORT_TEX_PREAMBLE,
    CompileResult,
    TectonicRunner,
    compiler_formula_error,
    compiler_missing_glyphs,
)

# Each fragment is typeset as ``\[`` / ``{`` / fragment lines / ``}`` / ``\]``.
# The group stops a trailing argument-taking command (``\k``, ``\hat``) from
# absorbing the closing ``\]`` and compiling by accident; the delimiters stay
# on their own lines so fragment lines keep their text and the line map stays
# exact. The same wrapping is used to derive the engine whitelist.
FORMULA_DOCUMENT_LAYOUT = "display-braced-v2"
CONTENT_FAILURE_CODES = frozenset({"tectonic_compile_failed", "tectonic_missing_glyphs"})
# Commands that could end the document early and make later fragments look
# compiled. They are never sent to the compiler by ``compile_fragments``.
_DOCUMENT_CONTROL = re.compile(
    r"\\(?:begin|end)\s*\{\s*document\s*\}|"
    r"\\(?:stop|endinput|enddocument|dump|batchmode|nonstopmode|scrollmode|"
    r"errorstopmode)(?![A-Za-z@])"
)

FailureKind = Literal[
    "tex_error", "missing_glyph", "unsafe_not_compiled", "unattributed"
]


def _strippable(character: str) -> bool:
    code = ord(character)
    control = (code < 32 and character not in "\t\n\r") or 127 <= code <= 159
    return character.isspace() and not control


def _fragment_lines(value: str) -> list[str]:
    # TeX ends input lines only at LF and CR, while str.splitlines() and
    # str.strip() also act on VT, FF, FS..US and NEL; those must reach the
    # compiler (as XeTeX errors) instead of silently disappearing, and the
    # line map must count lines exactly as TeX does.
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    start, end = 0, len(text)
    while start < end and _strippable(text[start]):
        start += 1
    while end > start and _strippable(text[end - 1]):
        end -= 1
    text = text[start:end]
    return text.split("\n") if text else []


def formula_document(values: Sequence[str]) -> tuple[str, list[tuple[int, int]]]:
    """Return TeX for ``values`` and each fragment's one-based line region.

    A region spans the fragment's ``\\[`` line through its ``\\]`` line
    inclusive: TeX errors are reported on a fragment line or on the closing
    brace, missing-glyph warnings on the closing ``\\]``.
    """
    lines = (REPORT_TEX_PREAMBLE + "\n\\begin{document}\n").splitlines()
    regions: list[tuple[int, int]] = []
    for value in values:
        first = len(lines) + 1
        lines.append(r"\[")
        lines.append("{")
        lines.extend(_fragment_lines(value))
        lines.append("}")
        lines.append(r"\]")
        regions.append((first, len(lines)))
    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n", regions


@dataclass(frozen=True)
class LocatedFailure:
    position: int  # index into the compiled document's fragment list
    kind: FailureKind
    line: int
    message: str


def locate_compile_failures(
    result: CompileResult, regions: Sequence[tuple[int, int]]
) -> list[LocatedFailure]:
    """Map a content failure's log lines to document fragments.

    Returns the halting TeX error (if located) first, then every located
    missing glyph, at most one entry per fragment. Infrastructure failures and
    lines outside every region yield nothing.
    """
    if result.failure_code not in CONTENT_FAILURE_CODES:
        return []

    def position(line: int) -> int | None:
        return next(
            (i for i, (first, last) in enumerate(regions) if first <= line <= last),
            None,
        )

    located: list[LocatedFailure] = []
    seen: set[int] = set()
    if result.failure_code == "tectonic_compile_failed":
        error = compiler_formula_error(result.log)
        index = position(error[0]) if error else None
        if error and index is not None:
            located.append(LocatedFailure(index, "tex_error", error[0], error[1]))
            seen.add(index)
    for line, message in compiler_missing_glyphs(result.log):
        index = position(line)
        if index is None or index in seen:
            continue
        seen.add(index)
        located.append(LocatedFailure(index, "missing_glyph", line, message))
    return located


def _tex_error_located(located: Sequence[LocatedFailure]) -> bool:
    return any(item.kind == "tex_error" for item in located)


@dataclass(frozen=True)
class FragmentFailure:
    index: int  # index into the caller's ``fragments``
    kind: FailureKind
    message: str
    line: int | None
    compile_number: int | None


@dataclass(frozen=True)
class FragmentCompile:
    number: int
    purpose: Literal["document", "bisection"]
    fragments: tuple[int, ...]
    status: Literal["success", "failed"]
    failure_code: str | None
    seconds: float
    evidence: str  # file prefix relative to evidence_dir


@dataclass(frozen=True)
class FragmentCompileResult:
    fragment_count: int
    failures: tuple[FragmentFailure, ...]
    compiles: tuple[FragmentCompile, ...]
    accepted: tuple[int, ...]
    infrastructure_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.infrastructure_error is None and not self.failures

    @property
    def durations(self) -> tuple[float, ...]:
        return tuple(item.seconds for item in self.compiles)

    @property
    def failed_indices(self) -> tuple[int, ...]:
        return tuple(sorted(item.index for item in self.failures))

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": FORMULA_DOCUMENT_LAYOUT,
            **asdict(self),
        }


def _runtime_identity(runner: TectonicRunner) -> dict[str, Any]:
    runtime = runner.runtime
    return {
        "version": runtime.version,
        "target": runtime.target,
        "binary_sha256": runtime.binary_sha256,
        "bundle_sha256": runtime.bundle_sha256,
    }


def compile_fragments(
    fragments: Sequence[str],
    runner: TectonicRunner,
    evidence_dir: Path,
    *,
    label: str = "formula-fragments",
    clock: Callable[[], float] = time.monotonic,
) -> FragmentCompileResult:
    """Compile ``fragments`` into one document and list every failing one.

    Loop: compile the remaining fragments; on a content failure drop the
    fragment holding the halting TeX error together with every fragment named
    by a missing-glyph warning, then recompile. A failure the log does not
    locate is bisected over the remaining fragments. Fragments with a static
    ``unsafe_command`` diagnostic or document-control commands are never
    compiled. Any other runner failure stops with ``infrastructure_error``
    (no retry; callers decide).

    Every compile appends ``compile-NNN.{tex,map.json,log,compiler.json}`` under
    ``evidence_dir``; existing evidence is never overwritten (FileExistsError),
    so pass a fresh directory per call.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    identity = _runtime_identity(runner)
    compiles: list[FragmentCompile] = []
    failures: dict[int, FragmentFailure] = {}

    def run(
        indices: Sequence[int], purpose: str
    ) -> tuple[CompileResult, list[tuple[LocatedFailure, int]]]:
        number = len(compiles) + 1
        tex, regions = formula_document([fragments[i] for i in indices])
        prefix = f"compile-{number:03d}"

        def write(suffix: str, text: str) -> None:
            with (evidence_dir / (prefix + suffix)).open(
                "x", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(text)

        write(".tex", tex)
        write(
            ".map.json",
            json.dumps(
                {
                    "layout": FORMULA_DOCUMENT_LAYOUT,
                    "purpose": purpose,
                    "regions": [
                        {"index": index, "first_line": first, "last_line": last}
                        for index, (first, last) in zip(indices, regions, strict=True)
                    ],
                },
                indent=2,
            )
            + "\n",
        )
        started = clock()
        result = runner.compile(tex, workspace_parent=evidence_dir)
        seconds = max(0.0, clock() - started)
        write(".log", result.log)
        write(
            ".compiler.json",
            json.dumps(
                {
                    "label": label,
                    "layout": FORMULA_DOCUMENT_LAYOUT,
                    "status": result.status,
                    "failure_code": result.failure_code,
                    **identity,
                },
                indent=2,
            )
            + "\n",
        )
        compiles.append(
            FragmentCompile(
                number=number,
                purpose=purpose,  # type: ignore[arg-type]
                fragments=tuple(indices),
                status=result.status,
                failure_code=result.failure_code,
                seconds=round(seconds, 6),
                evidence=prefix,
            )
        )
        located = locate_compile_failures(result, regions)
        return result, [
            (
                LocatedFailure(
                    indices[item.position], item.kind, item.line, item.message
                ),
                number,
            )
            for item in located
        ]

    def record(item: LocatedFailure, number: int | None) -> None:
        failures.setdefault(
            item.position,
            FragmentFailure(
                item.position,
                item.kind,
                item.message,
                item.line or None,
                number,
            ),
        )

    def infrastructure(result: CompileResult) -> str:
        return (
            f"Formula compiler {result.failure_code}; inspect "
            f"{evidence_dir / compiles[-1].evidence}.log"
        )

    def bisect(
        candidates: list[int],
    ) -> tuple[tuple[LocatedFailure, int] | None, str | None]:
        items = list(candidates)
        while len(items) > 1:
            half = items[: len(items) // 2]
            result, _ = run(half, "bisection")
            if result.status == "failed" and result.failure_code not in CONTENT_FAILURE_CODES:
                return None, infrastructure(result)
            items = half if result.status == "failed" else items[len(items) // 2 :]
        if not items:
            return None, None
        result, located = run(items, "bisection")
        if result.status == "success":
            return None, None
        if result.failure_code not in CONTENT_FAILURE_CODES:
            return None, infrastructure(result)
        if located:
            return located[0], None
        return (
            (
                LocatedFailure(
                    items[0],
                    "unattributed",
                    0,
                    f"Compiler {result.failure_code} without a located line when "
                    "compiled alone",
                ),
                len(compiles),
            ),
            None,
        )

    active: list[int] = []
    for index, value in enumerate(fragments):
        unsafe = [d for d in validate_math(value) if d.code == "unsafe_command"]
        control = _DOCUMENT_CONTROL.search(value)
        if unsafe or control:
            failures[index] = FragmentFailure(
                index,
                "unsafe_not_compiled",
                unsafe[0].message if unsafe else f"Disabled TeX command: {control.group(0)}",
                None,
                None,
            )
        else:
            active.append(index)

    accepted: tuple[int, ...] = ()
    infrastructure_error: str | None = None
    # Each iteration removes at least one fragment, so this always terminates.
    while active:
        result, located = run(active, "document")
        if result.status == "success":
            accepted = tuple(active)
            break
        if result.failure_code not in CONTENT_FAILURE_CODES:
            infrastructure_error = infrastructure(result)
            break
        halted = result.failure_code == "tectonic_compile_failed"
        if not located or (
            halted and not _tex_error_located([item for item, _ in located])
        ):
            known = {item.position for item, _ in located}
            culprit, infrastructure_error = bisect([i for i in active if i not in known])
            if infrastructure_error:
                break
            if culprit is not None:
                located.append(culprit)
            elif not located:
                infrastructure_error = (
                    "Formula compiler failure could not be attributed to a formula; "
                    f"inspect {evidence_dir / compiles[-1].evidence}.log"
                )
                break
        for item, number in located:
            record(item, number)
        dropped = {item.position for item, _ in located}
        active = [i for i in active if i not in dropped]
    return FragmentCompileResult(
        fragment_count=len(fragments),
        failures=tuple(failures[i] for i in sorted(failures)),
        compiles=tuple(compiles),
        accepted=accepted,
        infrastructure_error=infrastructure_error,
    )


class ProductFormulaValidator:
    """Validate one candidate using the same locked compiler as PDF export.

    One document covers all generated formulas. Its line map permits precise
    feedback without starting a compiler process for every individual formula.
    The raw Writer response remains in Record; only derived TeX and compiler
    evidence are written here, under that run's content-addressed directory.
    Audit policy is formula-v1; the document layout is ``FORMULA_DOCUMENT_LAYOUT``.
    """

    def __init__(self, *, runner: TectonicRunner, evidence_root: Path) -> None:
        self.runner = runner
        self.evidence_root = evidence_root

    def __call__(self, content: StepContent) -> FormulaValidationResult:
        fields = content.to_record()
        issues: list[dict[str, Any]] = []
        for diagnostic in validate_fields(fields):
            item = asdict(diagnostic)
            # A source quotation is evidence, not a request to silently fix the
            # cited author's notation. Unsafe characters still block globally.
            item["severity"] = (
                "warning"
                if diagnostic.field == "source"
                and diagnostic.code not in {"control_character", "unsafe_command"}
                else "error"
            )
            item["excerpt"] = (
                fields[diagnostic.field][diagnostic.start : diagnostic.end][:300]
                .encode("unicode_escape")
                .decode("ascii")
            )
            issues.append(item)
        if any(item["severity"] == "error" for item in issues):
            return FormulaValidationResult(issues=tuple(issues))

        warned = {(item["field"], item["formula_index"]) for item in issues}
        fragments = [
            f
            for f in math_fragments(fields)
            if (f.field, f.formula_index) not in warned
        ]
        if not fragments:
            return FormulaValidationResult(issues=tuple(issues))
        digest = hashlib.sha256(
            json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        try:
            parent = self.evidence_root / digest
            parent.mkdir(parents=True, exist_ok=True)
            # Explicit infrastructure rechecks append evidence, preserving the
            # original failed compiler log instead of overwriting it.
            directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
            for attempt in range(len(fragments) + 1):
                tex, regions = formula_document([f.value for f in fragments])
                line_map = [
                    {**asdict(f), "first_line": first, "last_line": last}
                    for f, (first, last) in zip(fragments, regions, strict=True)
                ]
                prefix = directory / f"compile-{attempt + 1:03d}"
                prefix.with_suffix(".tex").write_text(tex, encoding="utf-8")
                prefix.with_suffix(".map.json").write_text(
                    json.dumps(line_map, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                result = self.runner.compile(tex, workspace_parent=directory)
                prefix.with_suffix(".log").write_text(result.log, encoding="utf-8")
                prefix.with_suffix(".compiler.json").write_text(
                    json.dumps(
                        {
                            "policy": "formula-v1",
                            "layout": FORMULA_DOCUMENT_LAYOUT,
                            "status": result.status,
                            "failure_code": result.failure_code,
                            **_runtime_identity(self.runner),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if result.status == "success":
                    return FormulaValidationResult(issues=tuple(issues))
                located = locate_compile_failures(result, regions)
                halted = result.failure_code == "tectonic_compile_failed"
                if not located or (halted and not _tex_error_located(located)):
                    return FormulaValidationResult(
                        issues=tuple(issues),
                        infrastructure_error=(
                            f"Formula compiler {result.failure_code}; inspect "
                            f"{prefix.with_suffix('.log').relative_to(self.evidence_root.parent)}"
                        ),
                    )
                blocking = False
                for failure in located:
                    fragment = fragments[failure.position]
                    source_warning = fragment.field == "source"
                    blocking = blocking or not source_warning
                    issues.append(
                        {
                            "code": "syntax_error"
                            if failure.kind == "tex_error"
                            else "missing_glyph",
                            "severity": "warning" if source_warning else "error",
                            "field": fragment.field,
                            "formula_index": fragment.formula_index,
                            "start": fragment.start,
                            "end": fragment.end,
                            "message": failure.message,
                            "excerpt": fragment.value[:300],
                        }
                    )
                if blocking:
                    return FormulaValidationResult(issues=tuple(issues))
                # Source is immutable. Warn and omit only those quotations from
                # this derived compiler probe, then validate remaining math.
                dropped = {failure.position for failure in located}
                fragments = [f for i, f in enumerate(fragments) if i not in dropped]
                if not fragments:
                    return FormulaValidationResult(issues=tuple(issues))
        except (OSError, ValueError) as exc:
            return FormulaValidationResult(
                issues=tuple(issues),
                infrastructure_error=f"Formula compiler evidence/runtime unavailable: {exc}",
            )
        return FormulaValidationResult(
            issues=tuple(issues),
            infrastructure_error="Formula compiler did not converge",
        )
