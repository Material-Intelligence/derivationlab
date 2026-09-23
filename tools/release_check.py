#!/usr/bin/env python3
"""Fail-closed release scanner for the public DerivationLab repository.

Every gate below is a thing that must not leave the private repository. The
scanner reports every hit with its path and line, and exits non-zero if there
is any. It uses only the Python standard library so it can run in the same bare
environment as the package.

**Where the needles are.** Three gates work by plain substring: a private path
or hostname (``identifier``), a provenance claim this snapshot does not make
(``provenance``), and an institution or funding line (``affiliation``). Their
needles are, by definition, the strings that must not appear in this repository
— so the list cannot live in this file. Encoding it would not help: an encoding
is reversible by anyone who reads the decoder beside it. The list therefore
lives in ``.release-needles``, which ``.gitignore`` keeps out of the publish set
and which this scanner never scans.

Without that file those three gates are OFF, and the scanner says so on stderr
and again in its last line rather than printing a bare "ok". Every other gate —
secret patterns, high-entropy tokens, third-party email addresses, CJK
characters, oversized files, committed bytecode, non-text files and the identity
check — needs no secret to run and always runs.

**Where those gates are allowed to look away.** The repository ships a bilingual
web UI, tests that exercise its Chinese locale, two normative documents in
Chinese that the runtime hashes into every record, and test fixtures that use
made-up addresses. Those are features, not leaks, so ``PUBLIC_EXEMPTIONS`` below
lists each one: a path glob, the gate, optionally the text the hit must contain,
and the reason. The table is public and pure ASCII on purpose: an exemption
nobody can read is a gate somebody quietly turned off. It can only exempt the
gates that need no secret; the substring gates are exempted, when they must be,
by the ``[allow]`` section of the private list. Addresses at domains reserved
for documentation and testing (RFC 2606, RFC 6761) are never third-party
addresses and need no exemption.

**What it scans.** Publishing means pushing what git would push, so that is
what is scanned: tracked files plus untracked files that are not ignored. Build
output, virtualenvs and ``__pycache__`` are ignored by ``.gitignore`` and so are
skipped — they never reach the remote, and treating them as leaks would mean
the gate cries wolf every time somebody runs the tests. If the tree is not a git
work tree (an unpacked tarball, say), every file is scanned instead;
``--all-files`` forces that mode anywhere. Either way, ``.pyc`` is still a gate:
committing bytecode remains a finding.

Usage::

    python3 tools/release_check.py                 # all gates
    python3 tools/release_check.py --skip-cjk      # every gate except the CJK one
    python3 tools/release_check.py --root DIR      # scan somewhere other than the repo
    python3 tools/release_check.py --needles FILE  # a needle list somewhere else
    python3 tools/release_check.py --all-files     # ignore git; walk the whole tree

The lists are curated, not exhaustive. A clean run means "none of the known
leaks are present", never "this tree is safe".
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024

#: The needle list, relative to the scanned root. Ignored by git; never scanned.
DEFAULT_NEEDLE_FILE = ".release-needles"

#: The gates that cannot run without a needle list, named so the scanner can say
#: which ones it did not run.
NEEDLE_GATES = ("identifier", "provenance", "affiliation")

#: Only consulted when walking the tree directly, which happens when the root is
#: not a git work tree or ``--all-files`` was passed. Inside a git work tree the
#: skip set comes from ``.gitignore``, where it belongs.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

#: The one identity this snapshot is allowed to carry.
AUTHOR_NAME = "Jiahao Xie"
AUTHOR_EMAIL = "xiejh.mail@gmail.com"

#: This file is scanned exactly like every other file, and gets no exemption
#: from any gate. The one thing it may not do is satisfy the identity check —
#: the author has to be named by a file a reader would actually read.
SELF_PATH = "tools/release_check.py"


@dataclass(frozen=True)
class Needles:
    """The three substring lists, plus the exemptions that name their matches.

    An instance with empty lists is not an error: it is the honest state of a
    tree with no needle file, and the caller is expected to say so out loud.
    """

    literals: tuple[str, ...] = ()
    vocabulary: tuple[str, ...] = ()
    affiliation: tuple[str, ...] = ()
    #: ``(path glob, gate, matched text, why)``. Every entry is a decision
    #: someone made in the open; an exemption applies only to files matching the
    #: glob and only to that exact matched text.
    allowlist: tuple[tuple[str, str, str, str], ...] = ()
    #: Where the list came from, or ``None`` when there was none. The scanner
    #: skips this path so that the list never trips its own gates.
    path: Path | None = None

    def __bool__(self) -> bool:
        return bool(self.literals or self.vocabulary or self.affiliation)


NO_NEEDLES = Needles()

_SECTIONS = ("literals", "vocabulary", "affiliation", "allow")


def _entry(raw: str) -> str:
    """One needle, with optional quoting so trailing spaces survive."""

    line = raw.strip()
    if len(line) >= 2 and line.startswith('"') and line.endswith('"'):
        return line[1:-1]
    return line


def load_needles(path: Path) -> Needles | None:
    """Read a needle list, or return ``None`` when there is no file to read.

    Raises ``ValueError`` on a malformed file. A needle list that silently
    parsed to nothing would be the worst outcome available: a scanner that
    prints "ok" while checking none of the things it was asked to check.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    collected: dict[str, list[tuple[int, str]]] = {name: [] for name in _SECTIONS}
    section: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in collected:
                raise ValueError(f"{path}:{number}: unknown section [{section}]; expected one of {_SECTIONS}")
            continue
        if section is None:
            raise ValueError(f"{path}:{number}: entry before any [section] header")
        collected[section].append((number, raw))

    allowlist = []
    for number, raw in collected["allow"]:
        fields = tuple(field.strip() for field in raw.split("|"))
        if len(fields) != 4 or not all(fields):
            raise ValueError(f"{path}:{number}: an [allow] entry is 'glob | gate | matched text | why'")
        if fields[1] not in NEEDLE_GATES:
            raise ValueError(f"{path}:{number}: {fields[1]!r} is not one of {NEEDLE_GATES}")
        allowlist.append(fields)

    if not any(collected[name] for name in ("literals", "vocabulary", "affiliation")):
        raise ValueError(f"{path}: no needles; delete the file rather than leaving an empty one")

    return Needles(
        literals=tuple(_entry(raw) for _number, raw in collected["literals"]),
        vocabulary=tuple(_entry(raw) for _number, raw in collected["vocabulary"]),
        affiliation=tuple(_entry(raw) for _number, raw in collected["affiliation"]),
        allowlist=tuple(allowlist),
        path=path,
    )


@dataclass(frozen=True)
class Exemption:
    """One reasoned, path-scoped exemption from a gate that needs no secret.

    ``match``, when not empty, narrows the exemption to hits whose matched text
    contains it (case-insensitively): a fixture's made-up address, say, rather
    than every address the file might ever carry.
    """

    glob: str
    gate: str
    reason: str
    match: str = ""


#: The gates ``PUBLIC_EXEMPTIONS`` may name. The substring gates are not among
#: them: their exemptions live next to their needles, in the private list.
EXEMPTIBLE_GATES = ("cjk", "email", "secret")

_ZH_LOCALE = "zh-CN locale strings; the web UI is bilingual and English is the default locale"
_ZH_TEST = "a test of the zh-CN locale; it asserts on the Chinese strings it renders"
_NORMATIVE = (
    "normative original of the Record contract; the runtime hashes these exact bytes into every record "
    "it writes, so it ships untranslated (English translation: docs/RECORD_SPEC.md; see docs/spec/README.md)"
)

PUBLIC_EXEMPTIONS: tuple[Exemption, ...] = (
    # --- cjk: the two normative originals (docs/spec/README.md) ----------------
    Exemption("docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md", "cjk", _NORMATIVE),
    Exemption(
        "docs/spec/DERIVATION_RUNTIME_RECORD_V1_1_cn.md",
        "cjk",
        "normative original of the Record 1.1 contract; the runtime hashes these bytes "
        "into every Record 1.1 it writes (English translation: docs/RECORD_SPEC.md; see docs/spec/README.md)",
    ),
    # --- cjk: locale resources of the web UI ----------------------------------
    Exemption("src/derivation_web/src/i18n.tsx", "cjk", _ZH_LOCALE),
    Exemption("src/derivation_web/src/directProblemMessages.ts", "cjk", _ZH_LOCALE),
    Exemption("src/derivation_web/src/components/readerLiveMessages.ts", "cjk", _ZH_LOCALE),
    Exemption("src/derivation_web/src/components/treeCanvasMessages.ts", "cjk", _ZH_LOCALE),
    Exemption("src/derivation_web/src/components/SiteAccessGate.tsx", "cjk", _ZH_LOCALE + " (inline table)"),
    Exemption("src/derivation_web/src/components/SiteAdminPanel.tsx", "cjk", _ZH_LOCALE + " (inline table)"),
    Exemption("src/derivation_web/src/components/ReportExportDialog.tsx", "cjk", _ZH_LOCALE + " (inline table)"),
    # --- cjk: tests of the zh-CN locale and of CJK handling ---------------------
    Exemption("src/derivation_web/src/i18n.test.tsx", "cjk", _ZH_TEST),
    Exemption("src/derivation_web/src/components/RunSidebar.test.tsx", "cjk", _ZH_TEST),
    Exemption(
        "src/derivation_app/tests/reporting_real_smoke.py",
        "cjk",
        "manual smoke test that Chinese text typesets in the PDF report; the input has to be Chinese",
    ),
    # --- email: made-up addresses in tests at domains that are not reserved -----
    Exemption(
        "src/derivation_app/tests/test_site_identity.py",
        "email",
        "made-up user addresses in site-identity tests",
        match="@example.edu",
    ),
    Exemption(
        "src/derivation_runtime/test_app_server_login.py",
        "email",
        "made-up account address in a fake Codex login response; the provider domain is what the code parses",
        match="@auth.openai.com",
    ),
    Exemption(
        "src/derivation_web/src/host.test.ts",
        "email",
        "made-up account address in a test of the desktop-host bridge",
        match="@openai.com",
    ),
)

#: Domains reserved for documentation and testing (RFC 2606, RFC 6761). An
#: address there belongs to nobody, so it is not a third-party address.
RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


def is_reserved_address(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].lower().rstrip(".")
    if domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return True
    return any(domain == reserved or domain.endswith("." + reserved) for reserved in RESERVED_DOMAINS)


SECRET_PATTERNS = {
    "sk_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "sk_ant_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

#: Generic high-entropy candidate: 32+ characters of token alphabet. Hashes are
#: everywhere in a record, so anything that is pure hex, pure digits, or single
#: case is not reported; a real API key mixes case with digits. ``=`` is allowed
#: only as trailing base64 padding, so ``name=CONSTANT`` is two words rather
#: than one long token, and the value of ``KEY=value`` is judged on its own.
GENERIC_TOKEN = re.compile(r"[A-Za-z0-9+_-]{32,}={0,2}")
#: Subresource-integrity digests (``sha512-<base64>``), as npm lock files carry
#: one per package. They are hashes of public artefacts, in a fixed and
#: recognisable format, and are removed from a line before the token scan.
SRI_DIGEST = re.compile(r"\bsha(?:256|384|512)-[A-Za-z0-9+/]{40,}={0,2}")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
#: CJK ranges, built from code points so this file stays pure ASCII: a
#: scanner that carries its own needle as a literal trips its own gate.
_CJK_RANGES = ((0x3000, 0x303F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))
CJK = re.compile("[" + "".join(chr(lo) + "-" + chr(hi) for lo, hi in _CJK_RANGES) + "]")


class Finding(str):
    """One gate hit, rendered as ``gate: path:line: detail``."""


def _is_identifier_shaped(token: str) -> bool:
    """A long name, not a key: every underscore-separated part is one case."""
    parts = token.split("_")
    if len(parts) < 2:
        return False
    return all(part.isupper() or part.islower() or part.isdigit() or not part for part in parts)


def _looks_like_secret(token: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]+", token):  # sha256 and friends
        return False
    if token.isdigit() or token.isalpha():
        return False
    if _is_identifier_shaped(token):
        return False
    has_upper = any(character.isupper() for character in token)
    has_lower = any(character.islower() for character in token)
    has_digit = any(character.isdigit() for character in token)
    return has_upper and has_lower and has_digit


def git_publishable_files(root: Path) -> list[Path] | None:
    """Every file git would push: tracked, plus untracked and not ignored.

    Returns ``None`` when ``root`` is not a git work tree, or when git is not
    installed — the caller then walks the tree instead. A repository with no
    commits yet answers correctly: everything is untracked and nothing is
    tracked, which is exactly the set a first commit would publish.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None

    paths = []
    for entry in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not entry:
            continue
        path = root / entry
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return sorted(paths)


def walk_files(root: Path) -> list[Path]:
    """Every file under ``root``, minus the directories nothing publishes."""

    paths = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return sorted(paths)


def iter_files(root: Path, *, all_files: bool = False):
    """The files to scan, and a note saying which way they were chosen."""

    if not all_files:
        tracked = git_publishable_files(root)
        if tracked is not None:
            return tracked, "git"
    return walk_files(root), "tree"


def scan(
    root: Path,
    *,
    skip_cjk: bool,
    all_files: bool = False,
    needles: Needles | None = None,
    exemptions: tuple[Exemption, ...] = PUBLIC_EXEMPTIONS,
    used: set[Exemption] | None = None,
) -> list[Finding]:
    """Every gate hit under ``root``.

    ``needles`` is the list loaded from ``.release-needles``; ``None`` means
    there was none, and the three substring gates do not run. That is a real
    state, not a degenerate one — the public repository has no such file — so it
    is represented rather than faked, and ``main`` is the place that says so.

    ``exemptions`` defaults to the public table; pass ``()`` to see every hit
    the table hides. ``used``, when given, collects every exemption that
    suppressed at least one hit, so a caller can find the ones that no longer
    suppress anything.
    """

    findings: list[Finding] = []
    needles = needles or NO_NEEDLES
    needle_file = needles.path.resolve() if needles.path is not None else None

    def exempt(gate: str, path: Path, matched: str) -> bool:
        relative = path.relative_to(root).as_posix()
        if gate in EXEMPTIBLE_GATES:
            for entry in exemptions:
                if (
                    fnmatch(relative, entry.glob)
                    and gate == entry.gate
                    and (not entry.match or entry.match.lower() in matched.lower())
                ):
                    if used is not None:
                        used.add(entry)
                    return True
            return False
        return any(
            fnmatch(relative, pattern) and gate == allowed_gate and matched.lower() == text.lower()
            for pattern, allowed_gate, text, _reason in needles.allowlist
        )

    def report(gate: str, path: Path, line: int | None, detail: str, matched: str = "") -> None:
        if exempt(gate, path, matched):
            return
        where = f"{path.relative_to(root)}" + (f":{line}" if line else "")
        findings.append(Finding(f"{gate}: {where}: {detail}"))

    saw_author_name = False
    saw_author_email = False

    paths, _source = iter_files(root, all_files=all_files)
    if not paths:
        findings.append(Finding(f"empty: <tree>: nothing to scan under {root}"))
    for path in paths:
        # The needle list is the one file whose whole content is needles. It is
        # never published (see .gitignore), and scanning it would report every
        # entry as a leak.
        if needle_file is not None and path.resolve() == needle_file:
            continue
        is_self = path.relative_to(root).as_posix() == SELF_PATH
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            report("oversize", path, None, f"{size} bytes exceeds {MAX_FILE_BYTES}")
        if path.suffix in {".pyc", ".pyo"}:
            report("bytecode", path, None, "compiled Python must not ship")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            report("binary", path, None, "not UTF-8 text; review by hand")
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            for literal in needles.literals:
                if literal.lower() in lowered:
                    report("identifier", path, number, f"contains {literal!r}", matched=literal)
            for word in needles.vocabulary:
                if word.lower() in lowered:
                    report("provenance", path, number, f"contains {word!r}", matched=word)
            for word in needles.affiliation:
                if word.lower() in lowered:
                    report("affiliation", path, number, f"contains {word!r}", matched=word)
            for name, pattern in SECRET_PATTERNS.items():
                found = pattern.search(line)
                if found:
                    report("secret", path, number, f"matches {name}", matched=found.group(0))
            for token in GENERIC_TOKEN.findall(SRI_DIGEST.sub(" ", line)):
                if _looks_like_secret(token):
                    report("secret", path, number, f"high-entropy token {token[:8]}...", matched=token)
            for address in EMAIL.findall(line):
                if address.lower() == AUTHOR_EMAIL:
                    if not is_self:
                        saw_author_email = True
                elif not is_reserved_address(address):
                    report("email", path, number, f"third-party address {address}", matched=address)
            if not skip_cjk:
                characters = "".join(CJK.findall(line))
                if characters:
                    report("cjk", path, number, "contains CJK characters", matched=characters)
            if AUTHOR_NAME in line and not is_self:
                saw_author_name = True

    if not saw_author_name:
        findings.append(Finding(f"identity: <tree>: no file names the author {AUTHOR_NAME!r}"))
    if not saw_author_email:
        findings.append(Finding(f"identity: <tree>: no file carries {AUTHOR_EMAIL}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--skip-cjk", action="store_true", help="run every gate except the CJK one")
    parser.add_argument(
        "--needles",
        type=Path,
        default=None,
        help=f"needle list to load (default: {DEFAULT_NEEDLE_FILE} under the root)",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="scan every file under the root instead of asking git what would be published",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    needle_path = args.needles if args.needles is not None else root / DEFAULT_NEEDLE_FILE
    try:
        needles = load_needles(needle_path)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if needles is None and args.needles is not None:
        # Asked for by name and not there: that is a mistake, not a mode.
        print(f"error: no needle list at {needle_path}", file=sys.stderr)
        return 2

    off = ""
    if needles is None:
        off = f" ({'/'.join(NEEDLE_GATES)} gates OFF: no {DEFAULT_NEEDLE_FILE})"
        print(f"warning: no needle list at {needle_path}", file=sys.stderr)
        print(f"warning: the {', '.join(NEEDLE_GATES)} gates did NOT run.", file=sys.stderr)
        print(
            f"warning: copy {DEFAULT_NEEDLE_FILE}.example to {DEFAULT_NEEDLE_FILE}, fill it in,"
            " and keep it out of git.",
            file=sys.stderr,
        )

    paths, source = iter_files(root, all_files=args.all_files)
    if needles is not None and needles.path is not None:
        # Counted the way scan() counts, so the summary line is not one out
        # under --all-files, where the needle list is on the walk.
        skipped = needles.path.resolve()
        paths = [path for path in paths if path.resolve() != skipped]
    findings = scan(root, skip_cjk=args.skip_cjk, all_files=args.all_files, needles=needles)
    for finding in findings:
        print(finding)
    scanned = f"{len(paths)} file(s) ({'git publish set' if source == 'git' else 'whole tree'})"
    if findings:
        print(f"\nFAIL: {len(findings)} release gate hit(s) in {scanned} under {root}{off}", file=sys.stderr)
        return 1
    print(f"ok: all release gates clean in {scanned} under {root}{off}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
