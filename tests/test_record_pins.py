"""The one claim in a record that reaches outside its own hash chain.

`run_created` pins three artefacts by path and SHA-256: the contract and both
schemas. Everything else a reader might want to check is recomputed by replay,
which is why tampering with it fails. A pin is not — replay accepts any 64 hex
characters there, because the pinned file is not part of the record. So the pin
is the single claim with nothing but this module behind it, which is exactly how
the shipped Record v1.1 record came to cite three placeholder digests at three
paths that ship in this repository and hash to something else.

The tests below are the check a reader would do by hand — `shasum -a 256` on the
cited path, compared with the cited digest — done for every pin of every record,
plus the positive control that says the check can fail, and the one declared
exception: the real model run under `examples/runs/uniformly_charged_sphere/`
pins an earlier wording of the Record v1 contract than the one this repository
ships, so that one pin is reported as withheld.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLE_RUNS_DIR, REPO_ROOT, minimal_events, write_events

from tools.verify_record_pins import (
    PINNED_FIELDS,
    WITHHELD_ORIGINALS,
    check_record,
    main,
    sha256_file,
    withheld_pins,
)

TOOL = REPO_ROOT / "tools" / "verify_record_pins.py"


def test_every_shipped_record_resolves_every_pin(example_run: Path) -> None:
    """The check itself: three paths, three digests, one declared exception."""

    assert check_record(example_run / "events.jsonl", root=REPO_ROOT) == []
    expected = ["record_spec"] if example_run.name == "uniformly_charged_sphere" else []
    assert withheld_pins(example_run / "events.jsonl") == expected


def test_the_tool_passes_on_the_shipped_examples(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "every other run_created pin resolves in 3 record(s)" in out
    assert "1 pin(s) name a declared withheld original" in out
    assert "withheld: examples/runs/uniformly_charged_sphere/events.jsonl: record_spec" in out


def test_the_withheld_original_is_exactly_one_field_path_and_digest(tmp_path: Path) -> None:
    """The exception is not a hole: it is not the file's digest, and nothing near it passes."""

    ((field, path, digest),) = WITHHELD_ORIGINALS
    assert field == "record_spec"
    assert sha256_file(REPO_ROOT / path) != digest

    events = minimal_events()
    events[0]["payload"]["record_spec"] = {"path": path, "sha256": digest}
    write_events(tmp_path / "exact", events)
    assert check_record(tmp_path / "exact" / "events.jsonl", root=REPO_ROOT) == []
    assert withheld_pins(tmp_path / "exact" / "events.jsonl") == ["record_spec"]

    # Another digest at the same path, the same digest at another path, or the
    # same path and digest in another field: each is an ordinary failure.
    near_misses = {
        "digest": ("record_spec", {"path": path, "sha256": "1" * 64}),
        "path": ("record_spec", {"path": "docs/RECORD_SPEC.md", "sha256": digest}),
        "field": ("event_schema", {"path": path, "sha256": digest}),
    }
    for name, (pinned_field, pin) in near_misses.items():
        events = minimal_events()
        events[0]["payload"][pinned_field] = pin
        write_events(tmp_path / name, events)
        failures = check_record(tmp_path / name / "events.jsonl", root=REPO_ROOT)
        assert len(failures) == 1 and "hashes to" in failures[0], name
        assert withheld_pins(tmp_path / name / "events.jsonl") == [], name


def test_the_tool_runs_with_a_bare_interpreter() -> None:
    """CI runs this with nothing installed, so it may not import the package."""

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_a_wrong_digest_is_caught(tmp_path: Path) -> None:
    """The positive control. A check nobody has seen fire proves nothing."""

    events = minimal_events()
    events[0]["payload"]["record_spec"]["sha256"] = "0" * 64
    write_events(tmp_path / "run", events)

    failures = check_record(tmp_path / "run" / "events.jsonl", root=REPO_ROOT)

    assert len(failures) == 1
    assert "record_spec" in failures[0] and "hashes to" in failures[0]


def test_a_pin_naming_a_file_that_is_not_here_is_caught(tmp_path: Path) -> None:
    """An absent file is a finding; there is no allowance for one."""

    events = minimal_events()
    events[0]["payload"]["event_schema"]["path"] = "docs/spec/no_such_schema.json"
    write_events(tmp_path / "run", events)

    failures = check_record(tmp_path / "run" / "events.jsonl", root=REPO_ROOT)

    assert len(failures) == 1
    assert "is not in the tree" in failures[0]


def test_every_test_built_record_pins_files_that_are_really_here(tmp_path: Path) -> None:
    """The suite's own records cite this repository, at this repository's digests.

    A test record citing a path nothing resolves is the same blind spot as a
    shipped one citing it: nobody notices until someone tries the obvious thing.
    """

    write_events(tmp_path / "run", minimal_events())

    assert check_record(tmp_path / "run" / "events.jsonl", root=REPO_ROOT) == []


# ---------------------------------------------------------------------------
# The normative originals
# ---------------------------------------------------------------------------


def _pinned_paths() -> set[str]:
    paths: set[str] = set()
    for events_path in sorted(EXAMPLE_RUNS_DIR.glob("*/events.jsonl")):
        first = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
        paths.update(first["payload"][field]["path"] for field in PINNED_FIELDS)
    return paths


def test_the_v1_records_pin_the_chinese_original_which_ships() -> None:
    """The two Record v1 records cite the contract in its original language.

    That file ships untranslated, next to the English translation, because only
    the cited bytes can hash to the pinned digest. If it were ever dropped for
    being untranslated, the deterministic fixture's pin would stop resolving,
    and this is where that would be noticed first.
    """

    original = "docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md"
    assert original in _pinned_paths()
    assert (REPO_ROOT / original).is_file()


def test_the_v1_1_record_pins_this_repository_s_own_files() -> None:
    """The synthetic record was written here, so all three pins must resolve.

    It is the only shipped record for which that is possible, and the reason the
    defect this module exists for was invisible: the paths were right, so
    nothing looked wrong.
    """

    first = json.loads((EXAMPLE_RUNS_DIR / "worked_v1_1" / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])

    for field in PINNED_FIELDS:
        pin = first["payload"][field]
        assert sha256_file(REPO_ROOT / pin["path"]) == pin["sha256"], field
