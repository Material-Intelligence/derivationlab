"""The public command surface.

This snapshot promises a reader two commands and an importable module. These
tests hold that promise to its word: every command is run as a subprocess, and
what is asserted is what the reader sees — the exit code, stdout, and stderr.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import EXAMPLE_RUNS, REPO_ROOT, minimal_events, run_cli, write_events

SUBCOMMANDS = ("verify", "replay", "render", "build")


def test_module_help_exits_zero() -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "verify" in result.stdout
    assert "render" in result.stdout


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_subcommand_help_exits_zero(subcommand: str) -> None:
    result = run_cli(subcommand, "--help")
    assert result.returncode == 0, result.stderr
    assert subcommand in result.stdout


def test_no_subcommand_is_a_usage_error() -> None:
    """Exit 2 with usage on stderr is argparse's contract; pin it."""

    result = run_cli()
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_verify_reports_the_shape_of_the_record(example_run: Path) -> None:
    result = run_cli("verify", example_run / "events.jsonl")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok: ")
    for word in ("events", "branches", "candidates", "judgements"):
        assert word in result.stdout


def test_verify_accepts_a_minimal_record_of_either_version(tmp_path: Path) -> None:
    for version, name in (
        ("derivation-agent-event-v1", "v1"),
        ("derivation-agent-event-v1.1", "v1_1"),
    ):
        path = write_events(tmp_path / name, minimal_events(version=version))
        result = run_cli("verify", path)
        assert result.returncode == 0, f"{version}: {result.stderr}"
        assert result.stdout.startswith("ok: 1 events")


def test_replay_writes_canonical_json(tmp_path: Path, example_run: Path) -> None:
    output = tmp_path / "canonical.json"
    result = run_cli("replay", example_run / "events.jsonl", "--output", output)
    assert result.returncode == 0, result.stderr

    canonical = json.loads(output.read_text(encoding="utf-8"))
    assert canonical["schema_version"].startswith("derivation-agent-canonical-v1")
    assert canonical["event_log"]["event_count"] > 0
    assert len(canonical["event_log"]["head_event_sha256"]) == 64


def test_replay_is_deterministic(tmp_path: Path, example_run: Path) -> None:
    """Two replays of one record must be byte-identical, or nothing downstream
    of the canonical form can be compared across machines."""

    first, second = tmp_path / "a.json", tmp_path / "b.json"
    for output in (first, second):
        assert run_cli("replay", example_run / "events.jsonl", "--output", output).returncode == 0
    assert first.read_bytes() == second.read_bytes()


def test_render_writes_html(tmp_path: Path, example_run: Path) -> None:
    output = tmp_path / "viewer.html"
    result = run_cli("render", example_run / "events.jsonl", "--output", output)
    assert result.returncode == 0, result.stderr

    html = output.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")


def test_build_writes_both_artifacts(tmp_path: Path, example_run: Path) -> None:
    canonical, html = tmp_path / "canonical.json", tmp_path / "viewer.html"
    result = run_cli(
        "build",
        example_run / "events.jsonl",
        "--canonical",
        canonical,
        "--html",
        html,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(canonical.read_text(encoding="utf-8"))["summary"]
    assert "<!doctype html>" in html.read_text(encoding="utf-8")


def test_output_directories_are_created(tmp_path: Path, example_run: Path) -> None:
    output = tmp_path / "deep" / "nested" / "viewer.html"
    assert run_cli("render", example_run / "events.jsonl", "--output", output).returncode == 0
    assert output.is_file()


def test_an_empty_log_is_a_contract_error(tmp_path: Path) -> None:
    empty = tmp_path / "events.jsonl"
    empty.write_text("", encoding="utf-8")
    result = run_cli("verify", empty)
    assert result.returncode == 2
    assert "contract error" in result.stderr
    assert "empty" in result.stderr


def test_a_missing_file_is_not_a_contract_violation(tmp_path: Path) -> None:
    """Exit 3, not 2 and not a traceback.

    A pipeline that gates on 2 is asking "is this record sound?". A path that
    does not exist is no answer to that question, so it gets its own code — and
    a reader who mistypes a path should see one line, not a stack trace.
    """

    result = run_cli("verify", tmp_path / "nowhere.jsonl")

    assert result.returncode == 3
    assert "Traceback" not in result.stderr
    assert "nowhere.jsonl" in result.stderr
    assert result.stderr.startswith("error:")


def test_an_unwritable_output_is_not_a_contract_violation(tmp_path: Path, example_run: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory\n", encoding="utf-8")

    result = run_cli("render", example_run / "events.jsonl", "--output", blocker / "viewer.html")

    assert result.returncode == 3
    assert "Traceback" not in result.stderr


def test_malformed_json_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"not": "closed"\n', encoding="utf-8")
    result = run_cli("verify", path)
    assert result.returncode == 2
    assert "line 1" in result.stderr


def test_the_package_is_importable_and_exports_the_api() -> None:
    """The other half of the public surface: one import, four names."""

    import derivation_agent_record as package

    for name in ("load_events", "replay_events", "render_html", "ContractError"):
        assert name in package.__all__, f"{name} is used in the README but is not exported"
        assert hasattr(package, name)


def test_examples_exist() -> None:
    """A snapshot whose whole demo is "replay this record" must ship one."""

    assert EXAMPLE_RUNS, f"no example run found under {REPO_ROOT / 'examples' / 'runs'}"
