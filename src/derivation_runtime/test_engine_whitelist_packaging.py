"""The engine whitelist must ship, and must describe the engine that is running.

``formula-v2`` is the factory default and the runtime fails closed when the
whitelist is missing, so a release that does not carry
``formula_engine_whitelist.json`` produces a build that cannot run at all. The
Server release exports its sources with ``git archive``, which only sees
tracked files - hence the tracked check below, which is the one a future
deletion or a forgotten ``git add`` trips over.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from .formula_validation import (
    ENGINE_WHITELIST_PATH,
    EngineWhitelist,
    host_target,
    load_engine_whitelist,
    verify_engine_whitelist,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGED_PATH = "src/derivation_runtime/formula_engine_whitelist.json"


def test_the_whitelist_is_where_the_runtime_looks_for_it():
    # Compare resolved paths: a release snapshot builds under /var/folders, which
    # is a symlink to /private/var/folders, so the two spellings of the same file
    # differ as strings.
    assert ENGINE_WHITELIST_PATH.resolve() == (ROOT / PACKAGED_PATH).resolve()
    assert ENGINE_WHITELIST_PATH.is_file()
    whitelist = load_engine_whitelist()
    assert whitelist.commands and whitelist.environments


def test_the_whitelist_is_tracked_by_git():
    """``git archive HEAD`` exports tracked files only."""

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", PACKAGED_PATH],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "not a git repository" in result.stderr:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, (
        f"{PACKAGED_PATH} is not tracked by git, so the release export would "
        f"ship a build whose formula-v2 gate cannot start: {result.stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# The whitelist must describe this engine


def _record() -> dict:
    return json.loads(ENGINE_WHITELIST_PATH.read_text(encoding="utf-8"))


def test_a_whitelist_from_another_preamble_is_refused(tmp_path):
    value = _record()
    value["preamble_sha256"] = "0" * 64
    path = tmp_path / "whitelist.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="different report preamble"):
        load_engine_whitelist(path)


def test_a_whitelist_from_another_platform_is_refused(tmp_path):
    value = _record()
    value["engine"] = {**value["engine"], "target": "plan9-vax"}
    path = tmp_path / "whitelist.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="running platform"):
        load_engine_whitelist(path)


def test_the_committed_whitelist_matches_this_preamble_and_platform():
    from derivation_app.reporting import REPORT_TEX_PREAMBLE

    whitelist = load_engine_whitelist()
    assert whitelist.preamble_sha256 == hashlib.sha256(
        REPORT_TEX_PREAMBLE.encode("utf-8")
    ).hexdigest()
    assert dict(whitelist.engine)["target"] == host_target()
    verify_engine_whitelist(whitelist)


def test_a_whitelist_that_cannot_be_compared_is_not_refused():
    """A fixture with no provenance is loadable; it just proves nothing."""

    bare = EngineWhitelist.from_record(
        {
            "schema_version": "formula-engine-whitelist-v1",
            "supported": {"alpha": ["{c}"]},
            "control_symbols": ["_"],
            "environments": ["aligned"],
        }
    )
    assert bare.preamble_sha256 is None and bare.engine == ()
    verify_engine_whitelist(bare)
