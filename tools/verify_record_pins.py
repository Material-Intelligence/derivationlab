#!/usr/bin/env python3
"""Check every record's `run_created` pins against the files they name.

A record's `run_created` payload pins three artefacts by path and SHA-256: the
contract document, the event schema and the canonical schema. Those three are
the only claims in a record that reach outside its own hash chain. Everything
else a reader wants to check — an event body, a step's output hash, a
transcript, the chain itself — is recomputed by `replay`, which is why tampering
with it is caught. A pin is not: replay accepts any 64 hex characters there,
because the file being pinned is not part of the record.

So the pin is exactly the claim a reader has to check by hand, doing the obvious
thing:

    shasum -a 256 docs/RECORD_SPEC.md

and comparing it with what the record says. This script is that, done for every
pin of every record, so the answer is a gate rather than an exercise.

Every pin must resolve, with one declared exception and no allowance for an
absent file. The Record v1 records cite the Chinese contract document,
`docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md`, which ships untranslated for
exactly this reason: a translation is a different byte sequence, so only the
cited bytes can hash to the pinned digest. The file that ships differs from an
earlier wording in a few clauses. A record written against the shipped file
resolves. The one record written against the earlier wording,
`examples/runs/uniformly_charged_sphere/`, cannot: its
`record_spec` digest is listed in `WITHHELD_ORIGINALS`, reported as withheld
rather than resolved, and any other digest at that path still fails. See
`docs/spec/README.md`.

Usage::

    python3 tools/verify_record_pins.py                      # examples/runs/
    python3 tools/verify_record_pins.py DIR [DIR ...]        # somewhere else
    python3 tools/verify_record_pins.py --root DIR           # resolve pins from DIR

Exit 0 when every pin resolves or names a declared withheld original, 1 when
any other pin does not resolve.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The three `run_created` payload fields that pin a file.
PINNED_FIELDS = ("record_spec", "event_schema", "canonical_schema")

#: Pins of an earlier wording of a document this repository ships, keyed by
#: (field, path, digest). Such a pin cannot resolve here, and
#: saying so is the whole of the exception: it is reported, never counted as
#: resolved, and it covers exactly this field, path and digest.
WITHHELD_ORIGINALS: dict[tuple[str, str, str], str] = {
    (
        "record_spec",
        "docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md",
        "290a674f2f39fcd6509e5a31aa7ec8a7d069722ea4c104530335dc2d09251cfb",
    ): (
        "earlier wording of the shipped document; the two differ in two clauses "
        "of section 15 and one sentence of section 4.2"
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def first_event(events_path: Path) -> dict:
    """The record's first event, read without replaying it.

    Reading one line keeps this script independent of the package: it must be
    able to say that a pin is wrong even in a tree where replay is broken.
    """

    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return json.loads(line)
    raise ValueError(f"{events_path}: no events")


def withheld_pins(events_path: Path) -> list[str]:
    """The pins of one record that name a declared withheld original."""

    payload = first_event(events_path).get("payload", {})
    found: list[str] = []
    for field in PINNED_FIELDS:
        pin = payload.get(field)
        if isinstance(pin, dict) and (field, pin.get("path"), pin.get("sha256")) in WITHHELD_ORIGINALS:
            found.append(field)
    return found


def check_record(events_path: Path, *, root: Path) -> list[str]:
    """Every failure in one record's pins; empty when they all resolve.

    A pin listed in `WITHHELD_ORIGINALS` is not a failure; `withheld_pins`
    names it, and `main` reports it.
    """

    failures: list[str] = []
    event = first_event(events_path)
    where = events_path.relative_to(root) if events_path.is_relative_to(root) else events_path
    if event.get("type") != "run_created":
        return [f"{where}: first event is {event.get('type')!r}, not run_created"]

    payload = event.get("payload", {})
    for field in PINNED_FIELDS:
        pin = payload.get(field)
        if not isinstance(pin, dict) or "path" not in pin or "sha256" not in pin:
            failures.append(f"{where}: {field} is not a path/sha256 pair")
            continue
        pinned_path, pinned_sha = pin["path"], pin["sha256"]
        if (field, pinned_path, pinned_sha) in WITHHELD_ORIGINALS:
            continue
        target = root / pinned_path
        if not target.is_file():
            failures.append(f"{where}: {field} pins {pinned_path}, which is not in the tree")
            continue
        actual = sha256_file(target)
        if actual != pinned_sha:
            failures.append(f"{where}: {field} pins {pinned_path} at {pinned_sha}, but that file hashes to {actual}")
    return failures


def record_paths(directories: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for directory in directories:
        paths.extend(sorted(directory.glob("*/events.jsonl")))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify every record's run_created pins.")
    parser.add_argument(
        "directories",
        nargs="*",
        type=Path,
        default=None,
        help="directories of run directories (default: examples/runs)",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="resolve pinned paths from here")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    directories = [path.resolve() for path in (args.directories or [root / "examples" / "runs"])]
    paths = record_paths(directories)
    if not paths:
        print(f"error: no events.jsonl under {', '.join(str(d) for d in directories)}", file=sys.stderr)
        return 1

    failures: list[str] = []
    withheld: list[str] = []
    for events_path in paths:
        failures.extend(check_record(events_path, root=root))
        where = events_path.relative_to(root) if events_path.is_relative_to(root) else events_path
        for field in withheld_pins(events_path):
            pin = first_event(events_path)["payload"][field]
            reason = WITHHELD_ORIGINALS[(field, pin["path"], pin["sha256"])]
            withheld.append(
                f"{where}: {field} pins {pin['path']} at {pin['sha256'][:12]}..., not resolvable here: {reason}"
            )

    for failure in failures:
        print(failure)
    if failures:
        print(f"\nFAIL: {len(failures)} unresolved pin(s) in {len(paths)} record(s)", file=sys.stderr)
        return 1
    for note in withheld:
        print(f"withheld: {note}")
    if withheld:
        print(
            f"ok: every other run_created pin resolves in {len(paths)} record(s) under {root}; "
            f"{len(withheld)} pin(s) name a declared withheld original"
        )
    else:
        print(f"ok: every run_created pin resolves in {len(paths)} record(s) under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
