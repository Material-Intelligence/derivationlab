"""Positive controls for the release scanner.

A scanner nobody has ever seen fire is indistinguishable from a scanner that
prints "ok" unconditionally, and this one is the last thing standing between a
private working tree and a public repository. So every gate gets a planted
needle here, and the clean-tree case is asserted too, because a gate that fires
on everything is no more useful than one that fires on nothing.

**This file contains no real needle, in any form.** The needles for three of the
gates are exactly the strings that must not appear in this repository, and a
test file is published like everything else. So the planted needles below are
synthetic — a path nobody has, a DOI in a prefix nobody owns — and the real
list, when the working copy has one, is exercised without ever being written
down: ``test_every_private_needle_is_caught`` reads it from the scanner at run
time, plants each entry into a temporary tree, and reports by index if one fails
to fire. There are no pinned digests of the real entries either: a needle with a
known shape, a DOI above all, is recovered from its digest by guessing, which
would make a digest disclosure with an extra step.

Where the real list is absent — in the public repository, in CI, in an unpacked
sdist — the tests that need it skip, and say so.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from tools.release_check import (
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    DEFAULT_NEEDLE_FILE,
    EXEMPTIBLE_GATES,
    MAX_FILE_BYTES,
    NEEDLE_GATES,
    PUBLIC_EXEMPTIONS,
    Exemption,
    Needles,
    git_publishable_files,
    is_reserved_address,
    load_needles,
    main,
    scan,
)

# --- Synthetic needles. Nothing here is, or resembles, a real one. -----------

SYNTHETIC_PATH = "/Users/nobody"
SYNTHETIC_TAILNET = "example-tailnet"
SYNTHETIC_DOI = "10.9999/aaaa-bbbb"
SYNTHETIC_VENDOR = "acme-model-vendor"
SYNTHETIC_AFFILIATION = "Imaginary Institute of Nowhere"

SYNTHETIC = Needles(
    literals=(SYNTHETIC_PATH, SYNTHETIC_TAILNET, SYNTHETIC_DOI),
    vocabulary=(SYNTHETIC_VENDOR,),
    affiliation=(SYNTHETIC_AFFILIATION,),
)

# `.zz` is not a delegated top-level domain, and it is not one of the domains
# reserved for documentation either, which the scanner deliberately lets pass.
THIRD_PARTY_EMAIL = "someone" + "@" + "mail.zz"
API_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
CJK_CHARACTER = chr(0x4E2D)

#: The working copy's real list, or None where there is none. Loaded once, used
#: only as data: no test prints an entry, and no assertion message contains one.
PRIVATE = load_needles(REPO_ROOT / DEFAULT_NEEDLE_FILE)
NO_PRIVATE_LIST = f"no {DEFAULT_NEEDLE_FILE} in this working copy; the private gates are off here"


def private_entries(needles: Needles) -> list[tuple[str, str]]:
    """Every real needle paired with the gate it is supposed to trip."""

    return [
        *((needle, "identifier") for needle in needles.literals),
        *((needle, "provenance") for needle in needles.vocabulary),
        *((needle, "affiliation") for needle in needles.affiliation),
    ]


def make_clean_tree(root: Path) -> Path:
    """A minimal tree that passes every gate, to plant needles into.

    The identity gate requires the author to appear somewhere, so a tree with
    no author would fail for a reason that has nothing to do with the needle
    under test. Stating that here keeps each test about one thing.
    """

    root.mkdir(parents=True, exist_ok=True)
    (root / "CITATION.cff").write_text(
        f"authors:\n  - family-names: Xie\n    given-names: Jiahao\n    email: {AUTHOR_EMAIL}\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(f"# Example\n\nBy {AUTHOR_NAME}.\n", encoding="utf-8")
    return root


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    return make_clean_tree(tmp_path / "tree")


def gates(findings: list[str]) -> set[str]:
    return {finding.split(":", 1)[0] for finding in findings}


# ---------------------------------------------------------------------------
# The control
# ---------------------------------------------------------------------------


def test_a_clean_tree_passes(clean_tree: Path) -> None:
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC) == []


def test_this_repository_passes() -> None:
    """The gate the owner runs before pushing, run here so it cannot rot.

    With a needle list present this is the real thing: every private string,
    checked against every file git would publish. Without one it still checks
    the gates that need no secret, which is what CI can do.

    Skipped outside a git work tree — from an unpacked sdist, for instance —
    because there the scanner falls back to walking every file, and the walk
    would report the ``__pycache__`` this very test run just created. The
    question "what would git publish" has no answer where there is no git.
    """

    if git_publishable_files(REPO_ROOT) is None:
        pytest.skip("not a git work tree; there is no publish set to check")

    findings = scan(REPO_ROOT, skip_cjk=False, needles=PRIVATE)
    assert findings == [], "\n".join(findings)


def test_every_private_needle_is_caught(tmp_path: Path) -> None:
    """Each real entry, planted and caught — without naming one.

    An entry that no longer fires is reported by its index and its gate, which
    is enough to find it in the private list and does not put it in a log.
    """

    if PRIVATE is None:
        pytest.skip(NO_PRIVATE_LIST)

    entries = private_entries(PRIVATE)
    assert entries, "a needle list that loaded with no entries would gate nothing"
    for index, (needle, gate) in enumerate(entries):
        root = make_clean_tree(tmp_path / f"case_{index}")
        (root / "leak.md").write_text(f"a line that mentions {needle} in passing\n", encoding="utf-8")

        findings = scan(root, skip_cjk=False, needles=PRIVATE)
        assert gate in gates(findings), f"needle #{index} of the {gate} list did not trip its gate"
        assert any("leak.md" in finding for finding in findings)


def test_the_private_needle_list_is_not_published() -> None:
    """The one file whose whole content is needles must never be in the set."""

    if PRIVATE is None:
        pytest.skip(NO_PRIVATE_LIST)

    publishable = git_publishable_files(REPO_ROOT)
    if publishable is None:
        pytest.skip("not a git work tree; there is no publish set to check")

    names = {path.relative_to(REPO_ROOT).as_posix() for path in publishable}
    assert DEFAULT_NEEDLE_FILE not in names, f"{DEFAULT_NEEDLE_FILE} is in git's publish set"


def test_the_shipped_scanner_carries_no_needle(clean_tree: Path) -> None:
    """The published scanner, scanned as an ordinary file, comes up clean.

    This is the enforced form of the rule the scanner's own docstring states: it
    reads the very file that will be published and requires that none of the
    strings it hunts for can be found in it. If someone moves a needle back into
    the source, this fails before a reader can ``grep`` it out of the public
    repository.
    """

    if PRIVATE is None:
        pytest.skip(NO_PRIVATE_LIST)

    tools = clean_tree / "tools"
    tools.mkdir()
    shipped = (REPO_ROOT / "tools" / "release_check.py").read_text(encoding="utf-8")
    (tools / "release_check.py").write_text(shipped, encoding="utf-8")

    findings = [item for item in scan(clean_tree, skip_cjk=False, needles=PRIVATE) if "release_check.py" in item]
    assert findings == [], "\n".join(findings)


# ---------------------------------------------------------------------------
# One planted needle per gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needle", "gate"),
    [
        (SYNTHETIC_PATH, "identifier"),
        (SYNTHETIC_TAILNET, "identifier"),
        (SYNTHETIC_DOI, "identifier"),
        (SYNTHETIC_VENDOR, "provenance"),
        (SYNTHETIC_AFFILIATION, "affiliation"),
        (THIRD_PARTY_EMAIL, "email"),
        (API_KEY, "secret"),
        (CJK_CHARACTER, "cjk"),
    ],
)
def test_each_gate_fires(clean_tree: Path, needle: str, gate: str) -> None:
    (clean_tree / "leak.md").write_text(f"a line that mentions {needle} in passing\n", encoding="utf-8")

    findings = scan(clean_tree, skip_cjk=False, needles=SYNTHETIC)
    assert gate in gates(findings), f"{needle!r} did not trip the {gate} gate: {findings}"
    assert any("leak.md" in finding for finding in findings)


def test_without_a_needle_list_the_three_substring_gates_do_not_run(clean_tree: Path) -> None:
    """The honest failure mode: absent list, gates off, nothing pretended.

    The gates that need no secret keep running, which is the whole reason the
    scanner still ships.
    """

    (clean_tree / "leak.md").write_text(
        f"{SYNTHETIC_PATH} {SYNTHETIC_VENDOR} {SYNTHETIC_AFFILIATION} {API_KEY}\n",
        encoding="utf-8",
    )

    found = gates(scan(clean_tree, skip_cjk=False, needles=None))
    assert not (set(NEEDLE_GATES) & found)
    assert "secret" in found


def test_the_cjk_gate_can_be_skipped(clean_tree: Path) -> None:
    (clean_tree / "notes.md").write_text(f"{CJK_CHARACTER}\n", encoding="utf-8")

    assert "cjk" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))
    assert "cjk" not in gates(scan(clean_tree, skip_cjk=True, needles=SYNTHETIC))


def test_a_high_entropy_token_is_reported(clean_tree: Path) -> None:
    # Deliberately outside the hex alphabet: a 42-character run of hex digits
    # is a digest, and the scanner is right not to call that a key.
    (clean_tree / "config.txt").write_text("token = " + "zQ7" * 14 + "\n", encoding="utf-8")
    assert "secret" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_hashes_and_long_identifiers_are_not_reported_as_secrets(clean_tree: Path) -> None:
    """The counterpart to the test above. A record is made of digests and long
    snake_case names; a scanner that called those secrets would be turned off
    within a day, and a scanner that is turned off gates nothing."""

    (clean_tree / "record.txt").write_text(
        "event_sha256 = " + "0123456789abcdef" * 4 + "\n"
        "field = derivation_agent_record_canonical_state_transcript\n"
        "upper = MACOS_LAUNCH_SECURITY_SCRIPT_RELATIVE_PATH_CONSTANT\n",
        encoding="utf-8",
    )
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC) == []


def test_an_identifier_assigned_a_constant_is_not_one_long_token(clean_tree: Path) -> None:
    """``name=CONSTANT`` is two words. Read as one 40-character token with mixed
    case and a digit, it looked like a key, on every keyword argument that
    passes a constant."""

    (clean_tree / "code.py").write_text(
        "call(expected_schema_sha256=PINNED_V2_SCHEMA_SHA256)\n"
        "call(capability_profile=SOURCE_READING_V1, other_flag=SOME_CONSTANT_2)\n",
        encoding="utf-8",
    )
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC) == []

    # ...but the value of an assignment is still judged on its own.
    (clean_tree / "code.py").write_text("token=" + "zQ7" * 14 + "\n", encoding="utf-8")
    assert "secret" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_subresource_integrity_digests_are_not_secrets(clean_tree: Path) -> None:
    """An npm lock file carries one ``sha512-<base64>`` digest per package."""

    digest = "zQ7a/Xb9" * 11 + "=="
    (clean_tree / "package-lock.json").write_text(f'"integrity": "sha512-{digest}"\n', encoding="utf-8")
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC) == []

    # The same characters without the digest prefix are a token like any other.
    (clean_tree / "package-lock.json").write_text(f'"value": "{digest.replace("/", "")}"\n', encoding="utf-8")
    assert "secret" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


@pytest.mark.parametrize(
    ("domain", "reserved"),
    [
        ("example.test", True),
        ("example.com", True),
        ("mail.example.org", True),
        ("host.invalid", True),
        ("example", True),
        ("example.edu", False),
        ("mail.zz", False),
        ("notexample.com", False),
    ],
)
def test_documentation_domains_are_not_third_party(clean_tree: Path, domain: str, reserved: bool) -> None:
    """RFC 2606 and RFC 6761 reserve a handful of names for exactly this use."""

    address = "alice" + "@" + domain
    assert is_reserved_address(address) is reserved

    (clean_tree / "fixture.py").write_text(f"USER = {address!r}\n", encoding="utf-8")
    hit = "email" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))
    assert hit is not reserved


def test_compiled_python_is_reported(clean_tree: Path) -> None:
    (clean_tree / "module.pyc").write_bytes(b"\x00\x01\x02\x03")
    assert "bytecode" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_an_oversize_file_is_reported(clean_tree: Path) -> None:
    (clean_tree / "big.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    assert "oversize" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_a_non_text_file_is_reported_for_review(clean_tree: Path) -> None:
    (clean_tree / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    assert "binary" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_a_tree_without_the_author_fails_the_identity_gate(tmp_path: Path) -> None:
    root = tmp_path / "anonymous"
    root.mkdir()
    (root / "README.md").write_text("# Nobody in particular\n", encoding="utf-8")
    assert "identity" in gates(scan(root, skip_cjk=False, needles=SYNTHETIC))


def test_an_empty_tree_fails(tmp_path: Path) -> None:
    """Scanning nothing must not look like scanning something clean."""

    root = tmp_path / "nothing"
    root.mkdir()
    findings = scan(root, skip_cjk=False, needles=SYNTHETIC)
    assert "empty" in gates(findings)


# ---------------------------------------------------------------------------
# The public exemption table
# ---------------------------------------------------------------------------


def test_a_public_exemption_applies_only_where_it_says(clean_tree: Path) -> None:
    exemptions = (Exemption("locale/*.ts", "cjk", "a locale catalog"),)
    (clean_tree / "locale").mkdir()
    (clean_tree / "locale" / "zh.ts").write_text(f"title: '{CJK_CHARACTER}'\n", encoding="utf-8")
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC, exemptions=exemptions) == []

    (clean_tree / "elsewhere.ts").write_text(f"title: '{CJK_CHARACTER}'\n", encoding="utf-8")
    findings = scan(clean_tree, skip_cjk=False, needles=SYNTHETIC, exemptions=exemptions)
    assert [finding.split(":")[1].strip() for finding in findings] == ["elsewhere.ts"]


def test_a_public_exemption_with_a_match_covers_only_that_text(clean_tree: Path) -> None:
    exemptions = (Exemption("tests/*.py", "email", "made-up users", match="@example.edu"),)
    (clean_tree / "tests").mkdir()
    fixture = clean_tree / "tests" / "test_users.py"

    fixture.write_text("USER = 'alice" + "@" + "example.edu'\n", encoding="utf-8")
    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC, exemptions=exemptions) == []

    fixture.write_text(f"USER = {THIRD_PARTY_EMAIL!r}\n", encoding="utf-8")
    assert "email" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC, exemptions=exemptions))


def test_a_public_exemption_cannot_silence_a_needle(clean_tree: Path) -> None:
    """The substring gates are exempted only from the private list, where the
    needles themselves are; a public entry naming one of them does nothing."""

    exemptions = (Exemption("*.md", "identifier", "an entry that must have no effect"),)
    (clean_tree / "leak.md").write_text(f"{SYNTHETIC_PATH}\n", encoding="utf-8")
    assert "identifier" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC, exemptions=exemptions))


def test_every_public_exemption_is_well_formed() -> None:
    for entry in PUBLIC_EXEMPTIONS:
        assert entry.gate in EXEMPTIBLE_GATES, entry
        assert len(entry.reason) > 20, f"{entry.glob}: an exemption without a usable reason"
        assert entry.reason.isascii() and entry.glob.isascii() and entry.match.isascii(), entry


def test_every_public_exemption_is_still_needed() -> None:
    """An exemption that suppresses nothing has outlived its reason.

    A file renamed, translated or deleted leaves its entry behind, and a stale
    entry is a hole waiting for the next file to land at that path. So every
    entry must still hide at least one real hit in this repository.
    """

    if git_publishable_files(REPO_ROOT) is None:
        pytest.skip("not a git work tree; there is no publish set to check")

    used: set[Exemption] = set()
    scan(REPO_ROOT, skip_cjk=False, needles=PRIVATE, used=used)
    stale = [entry.glob for entry in PUBLIC_EXEMPTIONS if entry not in used]
    assert stale == [], f"exemptions that no longer suppress anything: {stale}"


# ---------------------------------------------------------------------------
# The needle list itself
# ---------------------------------------------------------------------------

EXAMPLE_LIST = """\
# a comment
[literals]
/Users/nobody
"padded needle "

[vocabulary]
acme-model-vendor

[affiliation]
imaginary institute of nowhere

[allow]
docs/*.md | provenance | acme-model-vendor | the docs quote the field name and say why
"""


def write_list(root: Path, text: str) -> Path:
    path = root / DEFAULT_NEEDLE_FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_a_needle_list_parses(tmp_path: Path) -> None:
    needles = load_needles(write_list(tmp_path, EXAMPLE_LIST))

    assert needles is not None
    assert needles.literals == ("/Users/nobody", "padded needle ")
    assert needles.vocabulary == ("acme-model-vendor",)
    assert needles.affiliation == ("imaginary institute of nowhere",)
    assert needles.allowlist == (
        ("docs/*.md", "provenance", "acme-model-vendor", "the docs quote the field name and say why"),
    )
    assert bool(needles)


def test_a_missing_needle_list_reads_as_none(tmp_path: Path) -> None:
    assert load_needles(tmp_path / DEFAULT_NEEDLE_FILE) is None
    assert not Needles()


@pytest.mark.parametrize(
    "text",
    [
        "/Users/nobody\n",  # an entry before any section
        "[nonsense]\nx\n",  # a section nobody reads
        "[literals]\nx\n[allow]\ntoo | few | fields\n",  # a malformed exemption
        "[literals]\nx\n[allow]\ndocs/*.md | nonsense | x | why\n",  # an exemption for no gate
        "[allow]\ndocs/*.md | provenance | x | why\n",  # exemptions but nothing to exempt
    ],
    ids=["entry-before-section", "unknown-section", "short-allow", "unknown-gate", "no-needles"],
)
def test_a_malformed_needle_list_is_an_error(tmp_path: Path, text: str) -> None:
    """A list that parsed to nothing would be the worst outcome available."""

    with pytest.raises(ValueError):
        load_needles(write_list(tmp_path, text))


def test_a_quoted_entry_keeps_its_spaces(tmp_path: Path, clean_tree: Path) -> None:
    """Some needles are only needles with their padding — a short institution
    name is a substring of unrelated words without the trailing space."""

    needles = load_needles(write_list(tmp_path, EXAMPLE_LIST))
    assert needles is not None

    (clean_tree / "yes.md").write_text("a padded needle  here\n", encoding="utf-8")
    assert "identifier" in gates(scan(clean_tree, skip_cjk=False, needles=needles))

    (clean_tree / "yes.md").write_text("a padded needless thing\n", encoding="utf-8")
    assert "identifier" not in gates(scan(clean_tree, skip_cjk=False, needles=needles))


def test_an_exemption_applies_only_where_it_says(tmp_path: Path, clean_tree: Path) -> None:
    needles = load_needles(write_list(tmp_path, EXAMPLE_LIST))
    assert needles is not None

    docs = clean_tree / "docs"
    docs.mkdir()
    (docs / "note.md").write_text(f"the sidecar records {SYNTHETIC_VENDOR}\n", encoding="utf-8")
    assert scan(clean_tree, skip_cjk=False, needles=needles) == []

    (clean_tree / "elsewhere.md").write_text(f"the sidecar records {SYNTHETIC_VENDOR}\n", encoding="utf-8")
    assert "provenance" in gates(scan(clean_tree, skip_cjk=False, needles=needles))


def test_the_needle_list_is_never_scanned(tmp_path: Path) -> None:
    """The list lives in the tree it guards, and is not a finding in it."""

    root = make_clean_tree(tmp_path / "tree")
    needles = load_needles(write_list(root, EXAMPLE_LIST))
    assert needles is not None

    assert scan(root, skip_cjk=False, all_files=True, needles=needles) == []


def test_a_missing_list_is_announced_loudly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Gates that did not run must not look like gates that passed."""

    root = make_clean_tree(tmp_path / "tree")
    assert main(["--root", str(root), "--all-files"]) == 0

    captured = capsys.readouterr()
    assert "did NOT run" in captured.err
    assert DEFAULT_NEEDLE_FILE in captured.err
    assert "OFF" in captured.out, "the final line must carry the warning too; stderr is easy to lose"


def test_a_list_named_on_the_command_line_must_exist(tmp_path: Path) -> None:
    """Asked for by name and not there is a mistake, not a mode."""

    root = make_clean_tree(tmp_path / "tree")
    assert main(["--root", str(root), "--all-files", "--needles", str(tmp_path / "absent")]) == 2


def test_a_malformed_list_stops_the_run(tmp_path: Path) -> None:
    root = make_clean_tree(tmp_path / "tree")
    write_list(root, "[nonsense]\nx\n")
    assert main(["--root", str(root), "--all-files"]) == 2


# ---------------------------------------------------------------------------
# What the scanner looks at
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    """Run git with the developer's own configuration out of the way."""

    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def test_ignored_files_are_not_scanned_inside_a_git_tree(clean_tree: Path) -> None:
    """Build output never reaches the remote, so it is not a release gate.

    This is the difference between a gate that is run and one that is worked
    around: without it, every developer who has run the test suite sees the
    scanner fail on ``__pycache__`` and learns to ignore its output.
    """

    _git(clean_tree, "init", "--quiet")
    (clean_tree / ".gitignore").write_text("__pycache__/\ndist/\n", encoding="utf-8")
    cache = clean_tree / "__pycache__"
    cache.mkdir()
    (cache / "model.cpython-312.pyc").write_bytes(b"\x00compiled")
    (clean_tree / "dist").mkdir()
    (clean_tree / "dist" / "notes.txt").write_text(f"built on {SYNTHETIC_PATH}\n", encoding="utf-8")

    assert scan(clean_tree, skip_cjk=False, needles=SYNTHETIC) == []

    # ...and the same tree scanned in full does report them, so the exclusion
    # is a statement about publishing, not a blind spot.
    forced = gates(scan(clean_tree, skip_cjk=False, all_files=True, needles=SYNTHETIC))
    assert {"bytecode", "identifier"} <= forced


def test_tracked_files_are_scanned_inside_a_git_tree(clean_tree: Path) -> None:
    _git(clean_tree, "init", "--quiet")
    (clean_tree / "leak.md").write_text(f"{SYNTHETIC_TAILNET}\n", encoding="utf-8")
    _git(clean_tree, "add", "leak.md")

    assert "identifier" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_untracked_but_publishable_files_are_scanned(clean_tree: Path) -> None:
    """A file nobody has staged yet is still one `git add -A` from the remote."""

    _git(clean_tree, "init", "--quiet")
    (clean_tree / "leak.md").write_text(f"{SYNTHETIC_DOI}\n", encoding="utf-8")

    assert "identifier" in gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))


def test_the_scanner_is_not_exempt_from_itself(clean_tree: Path) -> None:
    """The scanner gets no exemption, so it must not spell a needle out.

    A scanner that carried a private identifier as a literal would publish that
    identifier — the file is in the repository like any other, and a plain
    ``grep`` finds it whatever the scanner prints. That is why the needles live
    outside the tree; this test is the part that would notice them coming back.
    """

    tools = clean_tree / "tools"
    tools.mkdir()
    (tools / "release_check.py").write_text(
        f"# hunting for {SYNTHETIC_PATH} and {SYNTHETIC_TAILNET}\nTOKEN = {API_KEY!r}\n",
        encoding="utf-8",
    )

    found = gates(scan(clean_tree, skip_cjk=False, needles=SYNTHETIC))
    assert "identifier" in found, "a scanner that spells out a needle publishes it"
    assert "secret" in found, "the scanner is not exempt from the secret gate"


def test_the_scanner_cannot_satisfy_the_identity_gate_by_itself(tmp_path: Path) -> None:
    """The author has to be named by a file a reader would actually read."""

    root = tmp_path / "scanner-only"
    tools = root / "tools"
    tools.mkdir(parents=True)
    shipped = (REPO_ROOT / "tools" / "release_check.py").read_text(encoding="utf-8")
    (tools / "release_check.py").write_text(shipped, encoding="utf-8")

    assert "identity" in gates(scan(root, skip_cjk=False, needles=SYNTHETIC))
