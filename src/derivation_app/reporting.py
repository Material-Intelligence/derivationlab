"""Auditable, immutable local ReportBundle export for DerivationLab."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from derivation_api.models import (
    FrozenProblemInput,
    ReportBundleView,
    RouteView,
    RunView,
    StepView,
)

from derivation_runtime.formula_validation import (
    EngineWhitelist,
    FormulaDiagnostic,
    load_engine_whitelist,
    validate_math,
)
from derivation_runtime.formula_validation import (
    scientific_tokens as _scientific_tokens,
)

from .route_typeset import (
    STATUS_FAILED,
    STATUS_QUOTATION_VERBATIM,
    SUBSTITUTED_STATUSES,
    format_issue_warnings,
    typeset_content_sha256,
    typeset_lookup,
)

REPORT_SCHEMA = "derivationlab-report-bundle-v1"
REPORT_TEMPLATE_VERSION = "derivationlab-auditable-report-v2"
RUNTIME_LOCK_SCHEMA = "derivationlab-tectonic-runtime-lock-v1"
REPORT_TEX_PREAMBLE = r"""\documentclass[11pt,letterpaper]{article}
\usepackage[letterpaper,margin=0.8in]{geometry}
\usepackage{libertinus}
\usepackage{xeCJK}
\setCJKmainfont{FandolSong-Regular.otf}[BoldFont=FandolSong-Bold.otf,ItalicFont=FandolKai-Regular.otf]
\usepackage{longtable}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.45em}
\setlength{\emergencystretch}{2em}
"""
MAX_COMPILER_LOG_BYTES = 1_048_576
MAX_REPORT_PDF_BYTES = 67_108_864
_IDENTIFIER_SAFE = re.compile(r"[^A-Za-z0-9._:-]+")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_directory(path: Path) -> str:
    """Hash a local Tectonic DirBundle without trusting a stored checksum."""

    digest = hashlib.sha256()
    for item in sorted(
        path.rglob("*"), key=lambda candidate: candidate.relative_to(path).as_posix()
    ):
        if item.is_symlink():
            raise ValueError("Tectonic DirBundle must not contain symlinks")
        if not item.is_file():
            continue
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(str(item.stat().st_size).encode())
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "<": r"\textless{}",
        ">": r"\textgreater{}",
    }
    return "".join(
        f"[U+{ord(character):04X}]"
        if (ord(character) < 32 and character not in "\n\r\t")
        or 127 <= ord(character) <= 159
        else replacements.get(character, character)
        for character in value
    )


def _escaped_prose(value: str) -> str:
    paragraphs = re.split(r"\n\s*\n", value)
    return "\n\n\\par\n".join(
        "".join(
            r"\allowbreak{}".join(_tex_escape(c) for c in token)
            if len(token) > 36 and not token.isspace()
            else _tex_escape(token)
            for token in re.split(r"(\s+)", re.sub(r"\s*\n\s*", " ", paragraph))
        )
        for paragraph in paragraphs
    )


def _section_title(value: str, *, limit: int = 132) -> str:
    """Keep scientific record titles useful without putting raw TeX in headings."""

    compact = re.sub(r"\\\[(?:.|\n)*?\\\]", " [display formula] ", value)
    compact = re.sub(r"\$\$(?:.|\n)*?\$\$", " [display formula] ", compact)
    compact = re.sub(r"\\\((?:.|\n)*?\\\)", " [formula] ", compact)
    compact = re.sub(r"(?<!\\)\$(?:[^$\n]|\\\$)+(?<!\\)\$", " [formula] ", compact)
    # Legacy generated titles can be truncated after an opener but before its
    # closing delimiter. Headings must still not expose that raw TeX tail.
    openers = (
        (r"\[", " [display formula]"),
        ("$$", " [display formula]"),
        (r"\(", " [formula]"),
        ("$", " [formula]"),
    )
    for opener, replacement in openers:
        if opener in compact:
            compact = compact.split(opener, 1)[0] + replacement
            break
    compact = re.sub(r"\s+", " ", compact).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _math_is_safe(value: str) -> bool:
    """Compatibility predicate; diagnostics are provided by validate_math."""
    return not validate_math(value)


def compiler_formula_error(log: str) -> tuple[int, str] | None:
    """Return an identifiable TeX content error and one-based source line.

    Infrastructure/resource diagnostics and errors lacking a source location
    never qualify for formula fallback. Callers must still map the line into
    their generated formula boundaries before treating it as content failure.
    """
    content_error = re.compile(
        r"(?:Missing .+ inserted|Double (?:subscript|superscript)|"
        r"Undefined control sequence|Extra [},]|Misplaced |"
        r"Bad math environment delimiter|Missing \\right|"
        r"Illegal parameter number|Illegal character in array arg|Runaway argument|"
        r"Argument of .+ has an extra|Paragraph ended before|"
        # A raw control character (for example a JSON-damaged backslash)
        # halts XeTeX on the offending source line; it is content, not runtime.
        r"Text line contains an invalid character)"
    )
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if not content_error.search(line) or _MISSING_GLYPH.search(line):
            continue
        direct = re.search(r"(?:report\.tex):(\d+):\s*(.*)", line)
        if direct:
            return int(direct.group(1)), direct.group(2).strip()
        if line.startswith("! "):
            for following in lines[index + 1 : index + 16]:
                if following.startswith("! "):
                    break
                location = re.match(r"l\.(\d+)\s", following)
                if location:
                    return int(location.group(1)), line[2:].strip()
    return None


_MISSING_GLYPH = re.compile(
    r"report\.tex:(\d+):\s*Missing character: There is no .*? "
    r"\((?:U\+([0-9A-Fa-f]+)|\"([0-9A-Fa-f]+))\) in font (.*?)!?\s*$"
)


def compiler_missing_glyphs(log: str) -> list[tuple[int, str]]:
    """Return located missing-glyph warnings as (one-based line, message).

    XeTeX does not halt on a missing glyph; ``TectonicRunner`` turns any such
    warning into ``tectonic_missing_glyphs``. Each warning names the source line
    where the affected material was typeset (for display math, its closing
    delimiter), so every omitted glyph in one log can be mapped at once.
    Warnings without a ``report.tex:N`` location are not returned. Duplicates
    (the engine repeats warnings in its summary) are collapsed in log order.
    """
    found: list[tuple[int, str]] = []
    seen: set[tuple[int, int, str]] = set()
    for line in log.splitlines():
        match = _MISSING_GLYPH.search(line)
        if match is None:
            continue
        codepoint = int(match.group(2) or match.group(3), 16)
        font = match.group(4).strip()
        bracketed = re.match(r"\[([^\]]+)\]", font)
        font = bracketed.group(1) if bracketed else font.split("/", 1)[0]
        key = (int(match.group(1)), codepoint, font)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            (key[0], f"Missing character U+{codepoint:04X} in font {font[:120]}")
        )
    return found


def _formula_at_line(tex: str, line_number: int) -> int | None:
    current: int | None = None
    for index, line in enumerate(tex.splitlines(), 1):
        begin = re.fullmatch(r"% DL_FORMULA_BEGIN:(\d+)", line)
        if begin:
            current = int(begin.group(1))
        elif line.startswith("% DL_FORMULA_END:"):
            current = None
        if index == line_number:
            return current if begin is None else None
    return None


@dataclass(frozen=True)
class EquationAudit:
    number: int
    step_revision_id: str
    local_formula_index: int
    route_id: str
    field: str


def _engine_whitelist() -> EngineWhitelist | None:
    """The committed engine vocabulary, or None when it is unavailable.

    Only a formula the typeset layer already compiled is validated against it;
    everything else keeps the frozen formula-v1 vocabulary, so a report of a run
    without a layer renders exactly as before.
    """

    try:
        return load_engine_whitelist()
    except (OSError, ValueError):
        return None


class _ScientificTexRenderer:
    def __init__(self, rejected: Mapping[int, str] | None = None) -> None:
        self.equations: list[EquationAudit] = []
        self.warnings: list[dict[str, object]] = []
        self.rejected = rejected or {}
        self.formula_count = 0

    def verbatim_quotation(self, value: str) -> str:
        return (
            r"\par\noindent\textbf{Quotation shown verbatim (not typeset):} "
            + r"\par{\raggedright "
            + r"\allowbreak{}".join(_tex_escape(character) for character in value)
            + r"\par}"
        )

    def marked(self, formula_id: int, tex: str) -> str:
        return (
            f"\n% DL_FORMULA_BEGIN:{formula_id}\n{tex}\n% DL_FORMULA_END:{formula_id}\n"
        )

    def render(
        self,
        value: str,
        *,
        route_id: str | None = None,
        step_revision_id: str | None = None,
        field: str | None = None,
        formula_counter: list[int] | None = None,
        typeset: Mapping[tuple[str, str, int], Mapping[str, object]] | None = None,
    ) -> str:
        output: list[str] = []
        formula_index = 0
        for token in _scientific_tokens(value):
            if token.kind == "prose":
                output.append(_escaped_prose(token.value))
                continue
            if token.kind == "code":
                output.append(r"\texttt{" + _tex_escape(token.value) + "}")
                continue
            # TeX treats a blank line inside display math as a paragraph break.
            # Record text commonly places display delimiters on their own lines,
            # so normalize only the delimiter-adjacent whitespace here.
            math_value = token.value.strip()
            formula_index += 1
            self.formula_count += 1
            formula_id = self.formula_count
            entry = None
            if typeset is not None and step_revision_id is not None and field:
                candidate = typeset.get((step_revision_id, field, formula_index))
                if candidate is not None and candidate.get("original") == token.value:
                    entry = candidate
            if entry is not None and entry["status"] == STATUS_QUOTATION_VERBATIM:
                # A cited author's notation the engine cannot set: show the
                # recorded characters instead of a rendering of them.
                output.append(self.verbatim_quotation(token.value))
                self.warnings.append(
                    {
                        "code": "quotation_verbatim",
                        "formula_index": formula_index,
                        "start": token.start,
                        "end": token.end,
                        "step_revision_id": step_revision_id,
                        "field": field,
                    }
                )
                continue
            whitelist = None
            validated = token.value
            substituted: str | None = None
            if entry is not None and entry["status"] in SUBSTITUTED_STATUSES:
                # The typeset layer compiled this body with the locked engine,
                # so it is the body that is rendered and checked here.
                math_value = str(entry["typeset"]).strip()
                validated = math_value
                whitelist = _engine_whitelist()
                substituted = str(entry["status"])
            diagnostics = validate_math(
                validated,
                field or "",
                formula_index,
                token.start,
                whitelist=whitelist,
            )
            if entry is not None and entry["status"] == STATUS_FAILED:
                diagnostics.append(
                    FormulaDiagnostic(
                        "syntax_error",
                        field or "",
                        formula_index,
                        token.start,
                        token.end,
                        str(
                            entry["compiler_errors"][-1]["message"]
                            if entry.get("compiler_errors")
                            else "The typeset layer could not compile this formula"
                        ),
                    )
                )
            if formula_id in self.rejected:
                diagnostics.append(
                    FormulaDiagnostic(
                        "syntax_error",
                        field or "",
                        formula_index,
                        token.start,
                        token.end,
                        self.rejected[formula_id],
                    )
                )
            if not token.closed:
                diagnostics.append(
                    FormulaDiagnostic(
                        "syntax_error",
                        field or "",
                        formula_index,
                        token.start,
                        token.end,
                        "Unclosed math delimiter",
                    )
                )
            if diagnostics:
                output.append(
                    r"\par\noindent\textbf{Unrendered math:} "
                    + _tex_escape("; ".join(d.message for d in diagnostics))
                    + r"\par{\raggedright "
                    + r"\allowbreak{}".join(_tex_escape(c) for c in token.value)
                    + r"\par}"
                )
                self.warnings.append(
                    {
                        "code": "math_rejected",
                        "formula_index": formula_index,
                        "start": token.start,
                        "end": token.end,
                        "diagnostics": [
                            {
                                "code": d.code,
                                "field": d.field,
                                "formula_index": d.formula_index,
                                "start": d.start,
                                "end": d.end,
                                "message": d.message,
                            }
                            for d in diagnostics
                        ],
                        "step_revision_id": step_revision_id,
                        "field": field,
                    }
                )
                continue
            if substituted is not None:
                # What is printed here is the typeset layer's body, not the
                # recorded one. The reader is told so, per formula, in the
                # manifest: a repaired or expanded formula is delivered marked.
                self.warnings.append(
                    {
                        "code": f"typeset_{substituted}",
                        "formula_index": formula_index,
                        "start": token.start,
                        "end": token.end,
                        "step_revision_id": step_revision_id,
                        "field": field,
                        "recorded": token.value,
                        "typeset": math_value,
                    }
                )
            if token.kind == "inline_math" and len(math_value) < 48:
                output.append(self.marked(formula_id, r"\(" + math_value + r"\)"))
                continue
            if (
                formula_counter is None
                or step_revision_id is None
                or route_id is None
                or field is None
            ):
                output.append(self.marked(formula_id, r"\[" + math_value + r"\]"))
                continue
            formula_counter[0] += 1
            number = len(self.equations) + 1
            self.equations.append(
                EquationAudit(
                    number=number,
                    step_revision_id=step_revision_id,
                    local_formula_index=formula_counter[0],
                    route_id=route_id,
                    field=field,
                )
            )
            output.append(
                self.marked(
                    formula_id,
                    "\\begin{equation}\n"
                    + math_value
                    + f"\n\\label{{eq:derivationlab-{number}}}\n\\end{{equation}}",
                )
            )
        return "".join(output)


@dataclass(frozen=True)
class TectonicRuntimeSpec:
    """Pinned product-managed compiler identity and local resource bundle."""

    version: str
    managed_root: Path
    binary_path: Path
    bundle_path: Path
    binary_sha256: str | None
    bundle_sha256: str | None
    provisioned: bool
    target: str
    evidence: str = ""

    @classmethod
    def from_lock(
        cls,
        repo_root: Path,
        lock_path: Path | None = None,
    ) -> TectonicRuntimeSpec:
        repository = repo_root.resolve()
        path = (
            lock_path
            or repository / "config" / "reporting" / "tectonic_runtime.lock.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != RUNTIME_LOCK_SCHEMA:
            raise ValueError("unsupported Tectonic runtime lock schema")
        machine = platform.machine().casefold()
        machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
        system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(
            platform.system(), platform.system().casefold()
        )
        target = f"{system}-{machine}"
        targets = value.get("targets")
        if not isinstance(targets, Mapping) or target not in targets:
            raise ValueError(f"Tectonic runtime lock has no target {target!r}")
        target_value = targets[target]
        if not isinstance(target_value, Mapping):
            raise TypeError("invalid Tectonic target lock")
        managed_relative = Path(value["managed_root"])
        bundle_relative = Path(value["bundle"]["path"])
        binary_relative = Path(target_value["binary"]["path"])
        if any(
            item.is_absolute() or ".." in item.parts
            for item in (managed_relative, bundle_relative, binary_relative)
        ):
            raise ValueError(
                "Tectonic lock paths must be safe repository-relative paths"
            )
        managed_root = (repository / managed_relative).resolve()
        binary_path = (repository / binary_relative).resolve()
        bundle_path = (repository / bundle_relative).resolve()
        if not binary_path.is_relative_to(
            managed_root
        ) or not bundle_path.is_relative_to(managed_root):
            raise ValueError("Tectonic lock artifacts must resolve inside managed_root")
        status = target_value.get("status")
        binary_sha256 = target_value["binary"].get("sha256")
        bundle_sha256 = value["bundle"].get("sha256")
        provisioned = status == "provisioned"
        if provisioned and (
            not isinstance(binary_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", binary_sha256)
            or not isinstance(bundle_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256)
        ):
            raise ValueError(
                "provisioned Tectonic artifacts require lowercase sha256 pins"
            )
        return cls(
            version=value["tectonic_version"],
            managed_root=managed_root,
            binary_path=binary_path,
            bundle_path=bundle_path,
            binary_sha256=binary_sha256,
            bundle_sha256=bundle_sha256,
            provisioned=provisioned,
            target=target,
            evidence=str(target_value.get("evidence", "runtime is not provisioned")),
        )


@dataclass(frozen=True)
class CompileResult:
    status: Literal["success", "failed"]
    log: str
    pdf_bytes: bytes | None
    observed_version: str | None
    failure_code: str | None = None


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    output: bytes
    timed_out: bool
    output_limited: bool


class TectonicRunner:
    """Run only an explicitly pinned local Tectonic and bundle, fail closed."""

    def __init__(
        self,
        runtime: TectonicRuntimeSpec,
        *,
        timeout_seconds: float = 45.0,
        max_output_bytes: int = MAX_COMPILER_LOG_BYTES,
        max_pdf_bytes: int = MAX_REPORT_PDF_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Tectonic timeout must be positive")
        if max_output_bytes < 1024:
            raise ValueError("Tectonic output limit must be at least 1024 bytes")
        if max_pdf_bytes < 1024:
            raise ValueError("Tectonic PDF limit must be at least 1024 bytes")
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_pdf_bytes = max_pdf_bytes

    def _sandboxed_argv(self, argv: Sequence[str]) -> list[str]:
        if self.runtime.target.startswith("darwin-"):
            return [
                "/usr/bin/sandbox-exec",
                "-p",
                "(version 1)(allow default)(deny network*)",
                *argv,
            ]
        return list(argv)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def _run_limited(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> _ProcessResult:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        chunks: list[bytes] = []
        byte_count = 0
        output_limited = threading.Event()

        def read_output() -> None:
            nonlocal byte_count
            assert process.stdout is not None
            with process.stdout:
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        return
                    if byte_count < self.max_output_bytes:
                        remaining = self.max_output_bytes - byte_count
                        chunks.append(chunk[:remaining])
                    byte_count += len(chunk)
                    if byte_count > self.max_output_bytes:
                        output_limited.set()

        reader = threading.Thread(
            target=read_output, name="tectonic-log-reader", daemon=True
        )
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while process.poll() is None:
            if output_limited.is_set():
                self._terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process)
                break
            time.sleep(0.01)
        returncode = process.wait()
        reader.join(timeout=1.0)
        return _ProcessResult(
            returncode=returncode,
            output=b"".join(chunks),
            timed_out=timed_out,
            output_limited=output_limited.is_set(),
        )

    def _artifact_error(self) -> str | None:
        runtime = self.runtime
        if not runtime.provisioned:
            return f"runtime lock target {runtime.target} is unprovisioned: {runtime.evidence}"
        for label, path, expected_hash in (
            ("binary", runtime.binary_path, runtime.binary_sha256),
            ("bundle", runtime.bundle_path, runtime.bundle_sha256),
        ):
            if not path.is_relative_to(runtime.managed_root):
                return f"pinned {label} resolves outside the managed runtime root"
            valid_kind = (
                path.is_file()
                if label == "binary"
                else (path.is_file() or path.is_dir())
            )
            if path.is_symlink() or not valid_kind:
                return f"pinned {label} is missing or has an unsupported type: {path}"
            try:
                actual_hash = (
                    _sha256_directory(path) if path.is_dir() else _sha256_file(path)
                )
            except ValueError as exc:
                return str(exc)
            if expected_hash is None or actual_hash != expected_hash:
                return f"pinned {label} sha256 validation failed"
        if os.name != "nt" and not os.access(runtime.binary_path, os.X_OK):
            return "pinned Tectonic binary is not executable"
        if runtime.target.startswith("darwin-") and not os.access(
            "/usr/bin/sandbox-exec", os.X_OK
        ):
            return "macOS network-denial sandbox is unavailable"
        return None

    def compile(self, report_tex: str, *, workspace_parent: Path) -> CompileResult:
        artifact_error = self._artifact_error()
        if artifact_error is not None:
            return CompileResult(
                status="failed",
                log=artifact_error + "\n",
                pdf_bytes=None,
                observed_version=None,
                failure_code="tectonic_runtime_unavailable",
            )
        with tempfile.TemporaryDirectory(
            prefix=".report-build-", dir=workspace_parent
        ) as raw:
            workspace = Path(raw)
            source = workspace / "report.tex"
            output = workspace / "out"
            home = workspace / "home"
            temporary = workspace / "tmp"
            output.mkdir()
            home.mkdir()
            temporary.mkdir()
            source.write_text(report_tex, encoding="utf-8", newline="\n")
            environment = {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TECTONIC_UNTRUSTED_MODE": "1",
            }
            version_result = self._run_limited(
                self._sandboxed_argv([str(self.runtime.binary_path), "--version"]),
                cwd=workspace,
                environment=environment,
                timeout=5.0,
            )
            version_text = version_result.output.decode(
                "utf-8", errors="replace"
            ).strip()
            if (
                version_result.returncode != 0
                or version_result.timed_out
                or version_result.output_limited
                or re.search(
                    rf"(?<![0-9.]){re.escape(self.runtime.version)}(?![0-9.])",
                    version_text,
                )
                is None
            ):
                return CompileResult(
                    status="failed",
                    log="Tectonic version validation failed.\n" + version_text + "\n",
                    pdf_bytes=None,
                    observed_version=version_text or None,
                    failure_code="tectonic_version_mismatch",
                )
            compile_result = self._run_limited(
                self._sandboxed_argv(
                    [
                        str(self.runtime.binary_path),
                        "-X",
                        "compile",
                        "--untrusted",
                        "--only-cached",
                        "--bundle",
                        str(self.runtime.bundle_path),
                        "--outdir",
                        str(output),
                        "--keep-logs",
                        str(source),
                    ]
                ),
                cwd=workspace,
                environment=environment,
                timeout=self.timeout_seconds,
            )
            log = f"Pinned Tectonic: {version_text}\n" + compile_result.output.decode(
                "utf-8", errors="replace"
            )
            if compile_result.timed_out:
                return CompileResult(
                    "failed",
                    log + "\nCompilation timed out.\n",
                    None,
                    version_text,
                    "tectonic_timeout",
                )
            if compile_result.output_limited:
                return CompileResult(
                    "failed",
                    log + "\nCompiler output exceeded the configured limit.\n",
                    None,
                    version_text,
                    "tectonic_output_limit",
                )
            pdf = output / "report.pdf"
            if compile_result.returncode != 0 or pdf.is_symlink() or not pdf.is_file():
                return CompileResult(
                    "failed",
                    log + f"\nCompiler exit code: {compile_result.returncode}.\n",
                    None,
                    version_text,
                    "tectonic_compile_failed",
                )
            if pdf.stat().st_size > self.max_pdf_bytes:
                return CompileResult(
                    "failed",
                    log + "\nGenerated PDF exceeded the configured limit.\n",
                    None,
                    version_text,
                    "tectonic_pdf_output_limit",
                )
            # XeTeX may exit successfully while silently omitting source glyphs.
            # A downloadable PDF with missing scientific content is not success.
            if "Missing character:" in log:
                return CompileResult(
                    "failed",
                    log + "\nPDF omitted unsupported characters; export rejected.\n",
                    None,
                    version_text,
                    "tectonic_missing_glyphs",
                )
            return CompileResult("success", log, pdf.read_bytes(), version_text)


@dataclass(frozen=True)
class ReportSource:
    run: RunView
    problem: FrozenProblemInput
    #: Verified typeset layers of this run, keyed by route id. Absent for a run
    #: without them, and then every formula renders exactly as it did before
    #: the typeset layer existed.
    typeset: Mapping[str, Mapping[str, Any]] | None = None

    def typeset_for(self, route: RouteView) -> dict[tuple[str, str, int], Mapping]:
        """Formula entries of one route, or nothing when it has no layer."""

        if not self.typeset:
            return {}
        layer = self.typeset.get(route.id)
        if layer is None:
            return {}
        return typeset_lookup(layer)


def _render_problem(
    problem: FrozenProblemInput, renderer: _ScientificTexRenderer
) -> str:
    def items(values: Sequence[str]) -> str:
        if not values:
            return "\\begin{itemize}\\item None declared.\\end{itemize}"
        return (
            "\\begin{itemize}\n"
            + "\n".join("\\item " + renderer.render(value) for value in values)
            + "\n\\end{itemize}"
        )

    sections = [
        ("Objective", renderer.render(problem.objective)),
        ("Givens", items(problem.givens)),
        ("Accepted assumptions", items(problem.assumptions)),
        ("Scope", renderer.render(problem.scope)),
        ("Required deliverable", renderer.render(problem.deliverable)),
        ("Allowed tools", items(problem.allowed_tools)),
        ("Allowed references", items(problem.allowed_references)),
        ("Success criteria", items(problem.success_criteria)),
    ]
    return "\n".join(
        f"\\subsection*{{{_tex_escape(title)}}}\n{body}" for title, body in sections
    )


def _render_step(
    step: StepView,
    *,
    route_id: str,
    ordinal: int,
    renderer: _ScientificTexRenderer,
    typeset: Mapping[tuple[str, str, int], Mapping[str, Any]] | None = None,
) -> str:
    if step.content is None:
        return (
            f"\\subsection*{{Step {ordinal}: {_tex_escape(_section_title(step.title))}}}\n"
            f"No sealed five-field content is available (status: {_tex_escape(step.status)})."
        )
    formula_counter = [0]
    fields = (
        ("Claim", "claim", step.content.claim),
        ("Why", "why", step.content.why),
        ("Source", "source", step.content.source),
        ("Derivation", "derivation", step.content.derivation),
        ("Scope", "scope", step.content.scope),
    )
    bodies = []
    for title, field, value in fields:
        bodies.append(
            f"\\paragraph{{{title}.}} "
            + renderer.render(
                value,
                route_id=route_id,
                step_revision_id=step.revision_id,
                field=field,
                formula_counter=formula_counter,
                typeset=typeset,
            )
        )
    return (
        f"\\subsection*{{Step {ordinal}: {_tex_escape(_section_title(step.title))}}}\n"
        f"\\noindent\\texttt{{{_tex_escape(step.revision_id)}}}\\par\n"
        + "\n".join(bodies)
    )


def _route_steps(route: RouteView, by_id: Mapping[str, StepView]) -> list[StepView]:
    missing = [node_id for node_id in route.node_ids if node_id not in by_id]
    if missing:
        raise ValueError(f"route {route.id!r} references unknown step ids: {missing}")
    return [by_id[node_id] for node_id in route.node_ids]


def _render_report(
    source: ReportSource,
    selected_route_id: str,
    rejected: Mapping[int, str] | None = None,
) -> tuple[str, _ScientificTexRenderer]:
    run = source.run
    routes = {route.id: route for route in run.routes}
    try:
        selected = routes[selected_route_id]
    except KeyError as exc:
        raise ValueError(
            f"selected route {selected_route_id!r} does not exist"
        ) from exc
    by_id = {step.id: step for step in run.steps}
    renderer = _ScientificTexRenderer(rejected)
    main_steps = _route_steps(selected, by_id)
    main_typeset = source.typeset_for(selected)
    main_body = "\n".join(
        _render_step(
            step,
            route_id=selected.id,
            ordinal=index,
            renderer=renderer,
            typeset=main_typeset or None,
        )
        for index, step in enumerate(main_steps, start=1)
    )
    branch_rows = "\n".join(
        f"{_tex_escape(branch.branch_id)} & {_tex_escape(branch.kind)} & "
        f"{_tex_escape(branch.status)} & {len(branch.step_revision_ids)} \\\\"
        for branch in run.branches
    )
    checks = []
    for step in run.steps:
        provenance = step.provenance
        provenance_text = (
            "not available"
            if provenance is None
            else f"model={provenance.model}; thread={provenance.thread_id}; turn={provenance.turn_id}; created={provenance.created_at}"
        )
        checks.append(
            f"\\subsection*{{{_tex_escape(step.revision_id)}}}\n"
            f"Schema: {_tex_escape(step.checks.schema_status)}; "
            f"physics: {_tex_escape(step.checks.physics)}; "
            f"provenance: {_tex_escape(step.checks.provenance)}.\\par\n"
            + _escaped_prose(provenance_text)
        )
    appendix = []
    for route in run.routes:
        if route.id == selected.id:
            continue
        steps = _route_steps(route, by_id)
        route_typeset = source.typeset_for(route)
        appendix.append(
            f"\\section{{Route {_tex_escape(route.label)} ({_tex_escape(route.status)})}}\n"
            + "\n".join(
                _render_step(
                    step,
                    route_id=route.id,
                    ordinal=index,
                    renderer=renderer,
                    typeset=route_typeset or None,
                )
                for index, step in enumerate(steps, start=1)
            )
        )
    appendix_body = "\n".join(appendix) or "No other routes were recorded."
    tex = (
        REPORT_TEX_PREAMBLE
        + r"""\begin{document}
\begin{center}
{\LARGE\bfseries DerivationLab Auditable Run Report}\par
\vspace{0.4em}
{\small Fixed Report Template V2}
\end{center}

\section{Frozen Problem}
"""
        + _render_problem(source.problem, renderer)
        + r"""

\section{Run Summary}
\begin{tabular}{@{}lp{0.72\linewidth}@{}}
Run & """
        + _tex_escape(run.id)
        + r""" \\
Status & """
        + _tex_escape(run.status)
        + r""" \\
Phase & """
        + _tex_escape(run.phase)
        + r""" \\
Selected main route & """
        + _tex_escape(selected.id)
        + r""" ("""
        + _tex_escape(selected.label)
        + r""") \\
Canonical event & """
        + str(run.canonical_event_id)
        + r""" \\
Updated & """
        + _tex_escape(run.updated_at)
        + r""" \\
\end{tabular}

\section{Confirmed Main Route}
"""
        + main_body
        + r"""

\section{Branch Overview}
\begin{longtable}{@{}llll@{}}
\textbf{Branch} & \textbf{Kind} & \textbf{Status} & \textbf{Steps} \\
\hline
"""
        + branch_rows
        + r"""
\end{longtable}

\section{Checks and Provenance}
"""
        + "\n".join(checks)
        + r"""

\appendix
\section{Other and Failed Routes}
"""
        + appendix_body
        + r"""

\end{document}
"""
    )
    return tex, renderer


class ReportExporter:
    """Create one immutable versioned ReportBundle for every export attempt."""

    def __init__(
        self,
        *,
        repo_root: Path,
        report_root: Path,
        runner: TectonicRunner,
        allowed_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.report_root = report_root.resolve()
        allowed = (allowed_root or (self.repo_root / "runs")).resolve()
        self.bundle_path_root = self.repo_root if allowed_root is None else allowed
        if not self.report_root.is_relative_to(allowed):
            raise ValueError(
                "ReportBundle root must be inside the configured data root"
            )
        if self.report_root.exists() and (
            self.report_root.is_symlink() or not self.report_root.is_dir()
        ):
            raise ValueError("ReportBundle root must be a real directory")
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    @classmethod
    def from_repository_lock(
        cls,
        *,
        repo_root: Path,
        report_root: Path,
        allowed_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ReportExporter:
        try:
            runtime = TectonicRuntimeSpec.from_lock(repo_root)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            managed_root = (
                repo_root
                / "src"
                / "derivation_app"
                / "resources"
                / "reporting"
                / "runtime"
            ).resolve()
            runtime = TectonicRuntimeSpec(
                version="0.17.0",
                managed_root=managed_root,
                binary_path=managed_root / "unprovisioned" / "tectonic",
                bundle_path=managed_root / "unprovisioned" / "bundle",
                binary_sha256=None,
                bundle_sha256=None,
                provisioned=False,
                target="unresolved-host",
                evidence=f"runtime lock could not be loaded: {type(exc).__name__}",
            )
        return cls(
            repo_root=repo_root,
            report_root=report_root,
            allowed_root=allowed_root,
            runner=TectonicRunner(runtime),
            clock=clock,
        )

    def _new_directory(
        self, run_id: str, timestamp: str, content_hash: str
    ) -> tuple[str, Path]:
        parent = self.report_root / run_id
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.resolve().is_relative_to(self.report_root):
            raise RuntimeError("ReportBundle run directory escaped the configured root")
        stem = f"{timestamp}-{content_hash[:12]}"
        for counter in range(1000):
            export_id = stem if counter == 0 else f"{stem}-{counter:02d}"
            directory = parent / export_id
            try:
                directory.mkdir()
            except FileExistsError:
                continue
            return export_id, directory
        raise RuntimeError("could not allocate a unique ReportBundle directory")

    def export(
        self, source: ReportSource, *, selected_route_id: str
    ) -> ReportBundleView:
        if source.run.id != _IDENTIFIER_SAFE.sub("", source.run.id):
            raise ValueError("run id is not a safe artifact path component")
        route_ids = {route.id for route in source.run.routes}
        if selected_route_id not in route_ids:
            raise ValueError(f"selected route {selected_route_id!r} does not exist")
        snapshot = {
            "schema_version": REPORT_SCHEMA,
            "template_version": REPORT_TEMPLATE_VERSION,
            "run": source.run.model_dump(mode="json", by_alias=True),
            "problem": source.problem.model_dump(mode="json"),
            "selected_route_id": selected_route_id,
            "confirmed": True,
            "compiler": {
                "version": self.runner.runtime.version,
                "target": self.runner.runtime.target,
                "binary_sha256": self.runner.runtime.binary_sha256,
                "bundle_sha256": self.runner.runtime.bundle_sha256,
            },
        }
        if source.typeset:
            # A layer changes what the PDF says, so it is part of what the
            # content hash identifies. A run without a layer keeps the key out
            # of the snapshot entirely and hashes exactly as it did before.
            snapshot["typeset"] = {
                route_id: typeset_content_sha256(layer)
                for route_id, layer in sorted(source.typeset.items())
            }
        content_hash = hashlib.sha256(_canonical_json(snapshot).encode()).hexdigest()
        now = self.clock().astimezone(UTC)
        timestamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        with self._lock:
            export_id, directory = self._new_directory(
                source.run.id, timestamp, content_hash
            )
        assets = directory / "assets"
        assets.mkdir()
        tex, renderer = _render_report(source, selected_route_id)
        tex_path = directory / "report.tex"
        rejected: dict[int, str] = {}
        # At most one fallback per original formula occurrence. Every failed
        # compilation is retained before a new derived rendering is attempted.
        for attempt in range(renderer.formula_count + 1):
            tex_path.write_text(tex, encoding="utf-8", newline="\n")
            result = self.runner.compile(tex, workspace_parent=directory.parent)
            error = (
                compiler_formula_error(result.log)
                if result.failure_code == "tectonic_compile_failed"
                else None
            )
            formula_id = _formula_at_line(tex, error[0]) if error else None
            if formula_id is None or formula_id in rejected:
                break
            (directory / f"compile-attempt-{attempt + 1:03d}.tex").write_text(
                tex, encoding="utf-8", newline="\n"
            )
            (directory / f"compile-attempt-{attempt + 1:03d}.log").write_text(
                result.log, encoding="utf-8", newline="\n"
            )
            rejected[formula_id] = f"Tectonic: {error[1]}"
            tex, renderer = _render_report(source, selected_route_id, rejected)
        log_path = directory / "compile.log"
        log_path.write_text(result.log, encoding="utf-8", newline="\n")
        if result.status == "success":
            assert result.pdf_bytes is not None
            (directory / "report.pdf").write_bytes(result.pdf_bytes)
        file_rows: dict[str, dict[str, object]] = {}
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.name == "manifest.json":
                continue
            if path.is_dir():
                file_rows[path.name + "/"] = {"kind": "directory"}
            else:
                file_rows[path.name] = {
                    "kind": "file",
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
        manifest: dict[str, object] = {
            "schema_version": REPORT_SCHEMA,
            "template_version": REPORT_TEMPLATE_VERSION,
            "export_id": export_id,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "status": result.status,
            "run_id": source.run.id,
            "canonical_event_id": source.run.canonical_event_id,
            "selected_route_id": selected_route_id,
            "selected_route_confirmed": True,
            "content_sha256": content_hash,
            "paper": "US Letter",
            "fonts": [
                "Libertinus Serif",
                "Libertinus Math",
                "Fandol Song",
                "Fandol Kai",
            ],
            "compiler": {
                "name": "Tectonic",
                "pinned_version": self.runner.runtime.version,
                "observed_version": result.observed_version,
                "target": self.runner.runtime.target,
                "binary_sha256": self.runner.runtime.binary_sha256,
                "bundle_sha256": self.runner.runtime.bundle_sha256,
                "untrusted": True,
                "only_cached": True,
                "network_isolation": (
                    "macOS sandbox-exec deny network plus Tectonic only-cached"
                    if self.runner.runtime.target.startswith("darwin-")
                    else "Tectonic only-cached resource isolation"
                ),
                "timeout_seconds": self.runner.timeout_seconds,
                "max_log_bytes": self.runner.max_output_bytes,
                "max_pdf_bytes": self.runner.max_pdf_bytes,
            },
            "equations": [
                {
                    "number": equation.number,
                    "step_revision_id": equation.step_revision_id,
                    "local_formula_index": equation.local_formula_index,
                    "route_id": equation.route_id,
                    "field": equation.field,
                }
                for equation in renderer.equations
            ],
            # Sealed steps that still carry unresolved format defects are a
            # property of the run, not of any one formula occurrence, so they
            # are appended once per route from the layer's own flags.
            "warnings": renderer.warnings + format_issue_warnings(source.typeset),
            "failure": (
                None
                if result.failure_code is None
                else {
                    "code": result.failure_code,
                    "message": "See compile.log for bounded evidence.",
                }
            ),
            "files": file_rows,
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        bundle_relative = directory.relative_to(self.bundle_path_root).as_posix()
        return ReportBundleView(
            status=result.status,
            run_id=source.run.id,
            selected_route_id=selected_route_id,
            export_id=export_id,
            bundle_path=bundle_relative,
            files=sorted(
                path.name + ("/" if path.is_dir() else "")
                for path in directory.iterdir()
            ),
            manifest=manifest,
        )

    def read_pdf(self, run_id: str, export_id: str) -> bytes:
        """Read one immutable PDF through validated artifact identifiers."""

        if any(
            not value or value != _IDENTIFIER_SAFE.sub("", value)
            for value in (run_id, export_id)
        ):
            raise ValueError("report artifact identifier is unsafe")
        directory = self.report_root / run_id / export_id
        pdf = directory / "report.pdf"
        if (
            directory.is_symlink()
            or pdf.is_symlink()
            or not pdf.is_file()
            or not pdf.resolve().is_relative_to(self.report_root)
        ):
            raise FileNotFoundError("ReportBundle PDF does not exist")
        if pdf.stat().st_size > MAX_REPORT_PDF_BYTES:
            raise ValueError("ReportBundle PDF exceeds the download limit")
        return pdf.read_bytes()


__all__ = [
    "CompileResult",
    "ReportExporter",
    "ReportSource",
    "TectonicRunner",
    "TectonicRuntimeSpec",
]
