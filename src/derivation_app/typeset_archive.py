"""Typeset views of archived runs: repair the formulas, never touch the Record.

An archived run is a hash-chained append-only Record. Its Writer output carries
the JSON-transport damage and the manuscript macros the formula gate did not yet
remove, so the delivered route often does not compile. This module builds a
*view* of such a run beside it: the same content, deterministically normalized
(:mod:`derivation_runtime.formula_normalization`) and compiled with the locked
report engine (:func:`derivation_app.formula_compiler.compile_fragments`), with
every substitution listed and bound to the hashes it was derived from.

Nothing is ever written inside an archived run directory. The archive is opened
read-only, the view is written under a separate output root, and every write is
checked against that root first (:func:`assert_inside`). A view is therefore a
derived artifact: delete it and the archive is exactly what it was.

The route-level result is a :mod:`derivation_app.route_typeset` layer, written
to ``<view>/typeset/<route_id>.json`` so that the report exporter can read it
with the view directory as its run directory. Note that such a layer is built on
*normalized* content, so :func:`derivation_app.route_typeset.verify_typeset_layer`
against the raw archived Record deliberately fails: an archived run was produced
under ``formula-v1``, the Record content was never normalized, and the view does
not pretend otherwise. The view's own ``steps`` bind it to the Record instead -
run id, ``events.jsonl`` digest and every step's ``output_sha256``.

Repair rounds are optional and off by default: without a runtime the
loop reports ``repair_unavailable`` and the view is the deterministic pass alone.
``--repair`` needs a model runtime, which this tool does not build - pass one
with ``--runtime-factory module:function`` (the function is called with the
:class:`ArchiveRun` and returns a runtime, or ``None`` to skip that run).

CLI::

    python -m derivation_app.typeset_archive --runs RUNS --out OUT [options]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from derivation_agent_record.replay import replay_events
from derivation_runtime.formula_normalization import (
    CONTROL_KINDS,
    NORMALIZER_VERSION,
    SourceMacroTables,
    normalize_step_fields,
)
from derivation_runtime.formula_validation import (
    EngineWhitelist,
    formula_v2_issues,
    load_engine_whitelist,
    math_fragments,
)
from derivation_runtime.types import ModelSpec

from .route_typeset import (
    STATUS_FAILED,
    STATUS_NOT_COMPILED,
    STATUS_QUOTATION_VERBATIM,
    TypesetCallBudget,
    _compiler_message,
    layer_path,
    typeset_directory,
    typeset_route,
    write_typeset_layer,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .reporting import TectonicRunner

ARCHIVE_VIEW_SCHEMA_VERSION = "derivationlab-archive-typeset-view-v1"
ARCHIVE_INDEX_SCHEMA_VERSION = "derivationlab-archive-typeset-index-v1"
VIEW_FILENAME = "typeset_view.json"
INDEX_FILENAME = "INDEX.json"
#: Never descend into the output root when it lives under the archive root.
SKIPPED_DIRECTORY_NAMES = frozenset({"typeset_views"})

STEP_FIELDS = ("claim", "why", "source", "derivation", "scope")
#: Compiler evidence kept after a run finishes.
EVIDENCE_RETENTION = ("full", "logs", "summary", "none")
_RETAINED_SUFFIXES = {
    "full": None,
    "logs": (".compiler.json", ".log"),
    "summary": (".compiler.json",),
    "none": (),
}

_UNDEFINED = re.compile(r"Undefined control sequence")
_CONTROL_WORD = re.compile(r"\\([A-Za-z@]+)")


#: Stand-in spec for an archive whose manifest does not name its models.
_UNKNOWN_MODEL = ModelSpec("openai", "unknown", "high")


class ArchiveError(RuntimeError):
    """An archived run could not be read or replayed."""


# ---------------------------------------------------------------------------
# Write guard


def assert_inside(path: Path, root: Path) -> Path:
    """Refuse any path that is not inside ``root``; return it resolved."""

    resolved = Path(path).resolve()
    base = Path(root).resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise ArchiveError(f"refusing to write outside the output root: {resolved}")
    return resolved


# ---------------------------------------------------------------------------
# Reading an archive, read-only


@dataclass(frozen=True)
class ArchiveConfig:
    """What :func:`~derivation_app.route_typeset.typeset_route` reads off a run.

    An archived run's ``RunConfig`` cannot be rebuilt offline (it carries the
    launch gate's pins and content refs), and the typeset layer only reads these
    fields, so the view passes this instead. Model specs matter only when a
    repair runtime is supplied.
    """

    run_id: str
    max_model_calls: int | None = None
    formula_validation_policy: str | None = None
    writer: ModelSpec = field(default_factory=lambda: _UNKNOWN_MODEL)
    checker: ModelSpec = field(default_factory=lambda: _UNKNOWN_MODEL)
    service_tier: str = "standard"


@dataclass(frozen=True)
class ArchiveRun:
    directory: Path
    relative_path: str
    events_sha256: str
    event_count: int
    canonical: dict[str, Any]
    config: ArchiveConfig
    #: source_id -> text, from the Record's frozen source evidence.
    sources: dict[str, str]
    #: source_id -> DOI, so parts of one paper share one macro scope.
    documents: dict[str, str]

    @property
    def run_id(self) -> str:
        return str(self.canonical["run"]["run_id"])


def discover_run_directories(runs_root: Path) -> list[Path]:
    """Every archived run directory under ``runs_root``, sorted.

    A run directory is one holding an ``events.jsonl`` whose first event is
    ``run_created``. Directories named in :data:`SKIPPED_DIRECTORY_NAMES` are not
    descended into, so an output root placed inside the archive is never read
    back as an archive.
    """

    root = Path(runs_root).resolve()
    found: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            name for name in directories if name not in SKIPPED_DIRECTORY_NAMES
        )
        if "events.jsonl" not in files:
            continue
        path = Path(current) / "events.jsonl"
        try:
            with path.open(encoding="utf-8") as handle:
                first = handle.readline()
            if json.loads(first).get("type") == "run_created":
                found.append(Path(current))
        except (OSError, ValueError):
            continue
    return sorted(found)


def _read_events(path: Path) -> tuple[list[dict[str, Any]], str, int]:
    digest = hashlib.sha256()
    events: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            text = line.decode("utf-8").strip()
            if text:
                events.append(json.loads(text))
    return events, digest.hexdigest(), len(events)


def _source_documents(directory: Path) -> dict[str, str]:
    """source_id -> DOI from the run's frozen source manifest, when it has one.

    Without it every registered source is its own document, which is the
    conservative reading: a macro defined in one part does not reach another.
    """

    path = directory / "sources" / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = value.get("sources")
    if not isinstance(entries, list):
        return {}
    documents: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        source_id = entry.get("source_id")
        doi = entry.get("doi")
        if isinstance(source_id, str) and isinstance(doi, str) and doi:
            documents[source_id] = doi
    return documents


def _archive_config(directory: Path, run_id: str) -> ArchiveConfig:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ArchiveConfig(run_id=run_id)
    api = manifest.get("api_config")
    if not isinstance(api, Mapping):
        return ArchiveConfig(run_id=run_id)

    def spec(name: str) -> ModelSpec:
        value = api.get(name)
        if not isinstance(value, Mapping):
            return ModelSpec("openai", "unknown", "high")
        return ModelSpec(
            str(value.get("provider") or "openai"),
            str(value.get("model") or "unknown"),
            str(value.get("effort") or "high"),
        )

    calls = api.get("max_model_calls")
    return ArchiveConfig(
        run_id=run_id,
        max_model_calls=calls if isinstance(calls, int) else None,
        formula_validation_policy=(
            api.get("formula_validation_policy")
            if isinstance(api.get("formula_validation_policy"), str)
            else None
        ),
        writer=spec("writer"),
        checker=spec("checker"),
        service_tier=str(api.get("service_tier") or "standard"),
    )


def load_archive_run(directory: Path, *, runs_root: Path) -> ArchiveRun:
    """Replay one archived run into canonical state without writing anything."""

    directory = Path(directory).resolve()
    events_path = directory / "events.jsonl"
    try:
        events, digest, count = _read_events(events_path)
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"{type(exc).__name__}: {exc}") from exc
    try:
        canonical = replay_events(events).canonical
    except Exception as exc:  # replay raises its own contract errors
        raise ArchiveError(f"{type(exc).__name__}: {exc}") from exc
    sources = {
        item["source_id"]: item["text"]
        for item in canonical.get("source_evidence", ()) or ()
    }
    documents = {
        key: value
        for key, value in _source_documents(directory).items()
        if key in sources
    }
    return ArchiveRun(
        directory=directory,
        relative_path=directory.relative_to(Path(runs_root).resolve()).as_posix(),
        events_sha256=digest,
        event_count=count,
        canonical=canonical,
        config=_archive_config(directory, str(canonical["run"]["run_id"])),
        sources=sources,
        documents=documents,
    )


# ---------------------------------------------------------------------------
# The delivered route


@dataclass(frozen=True)
class RouteSelection:
    route_id: str
    branch_id: str
    #: "completed", "active" or "latest" - why this branch was taken.
    selection: str


def _last_status_seq(branch: Mapping[str, Any], status: str | None = None) -> int:
    """Sequence number at which a branch last reached ``status`` (-1: never).

    A canonical status history entry names the status it moved *to*.
    """

    history = branch.get("status_history") or ()
    seqs = [
        int(item["seq"])
        for item in history
        if status is None or item.get("to") == status
    ]
    return max(seqs) if seqs else -1


def select_route(canonical: Mapping[str, Any]) -> RouteSelection | None:
    """The delivered route: the latest completed branch, else the latest active.

    "Latest" is the sequence number at which the branch reached that status, so
    the route a reader would be shown is the route the view typesets.
    """

    branches = [
        item for item in canonical.get("branches", ()) if item.get("step_revision_ids")
    ]
    if not branches:
        return None
    for status in ("completed", "active"):
        matching = [item for item in branches if item.get("status") == status]
        if matching:
            chosen = max(
                matching, key=lambda item: (_last_status_seq(item, status), item["branch_id"])
            )
            return RouteSelection(
                f"route_{chosen['branch_id']}", chosen["branch_id"], status
            )
    chosen = max(branches, key=lambda item: (_last_status_seq(item), item["branch_id"]))
    return RouteSelection(f"route_{chosen['branch_id']}", chosen["branch_id"], "latest")


def record_route_steps(
    canonical: Mapping[str, Any], branch_id: str
) -> list[dict[str, Any]]:
    """The recorded steps of one branch, in route order, content unchanged."""

    branch = next(
        item for item in canonical["branches"] if item["branch_id"] == branch_id
    )
    revisions = {item["step_revision_id"]: item for item in canonical["step_revisions"]}
    return [
        {
            "step_revision_id": step_id,
            "output_sha256": revisions[step_id]["output_sha256"],
            "content": {
                name: revisions[step_id]["content"][name] for name in STEP_FIELDS
            },
        }
        for step_id in branch["step_revision_ids"]
    ]


# ---------------------------------------------------------------------------
# Deterministic normalization of every sealed step


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_sha256(content: Mapping[str, str]) -> str:
    payload = json.dumps(
        {name: content[name] for name in STEP_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


def normalize_step(
    content: Mapping[str, str],
    *,
    sources: Mapping[str, str],
    tables: SourceMacroTables,
    whitelist: EngineWhitelist,
) -> dict[str, Any]:
    """Normalize one sealed step and describe what changed and what did not."""

    fields = {name: content[name] for name in STEP_FIELDS}
    before = formula_v2_issues(fields, whitelist=whitelist)
    try:
        analysis = normalize_step_fields(
            fields, sources=sources, whitelist=whitelist, tables=tables
        )
    except (ValueError, KeyError) as exc:
        return {
            "normalized": dict(fields),
            "error": f"{type(exc).__name__}: {exc}",
            "issues_before": before,
            "issues_after": before,
            "replacements": [],
            "replacement_kinds": {},
        }
    after = formula_v2_issues(
        analysis.fields,
        whitelist=whitelist,
        quotation_fragments=analysis.quotation_fragments,
    )
    report = analysis.report()
    return {
        "normalized": dict(analysis.fields),
        "normalized_content_sha256": _content_sha256(analysis.fields),
        "changed": analysis.changed,
        "replacements": analysis.replacement_records(),
        "replacement_kinds": report["replacement_kinds"],
        "cited_source_ids": report["cited_source_ids"],
        "quotation_fragments": report["quotation_fragments"],
        "unrepaired_control": report["unrepaired_control"],
        "skipped_macros": report["skipped_macros"],
        "issues_before": before,
        "issues_after": after,
    }


def normalize_sealed_steps(
    canonical: Mapping[str, Any],
    *,
    sources: Mapping[str, str],
    tables: SourceMacroTables,
    whitelist: EngineWhitelist,
    route_step_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Normalize every sealed step revision of a run, route or not."""

    on_route = set(route_step_ids)
    steps: list[dict[str, Any]] = []
    for revision in canonical.get("step_revisions", ()):
        content = revision.get("content")
        if not isinstance(content, Mapping) or not all(
            isinstance(content.get(name), str) for name in STEP_FIELDS
        ):
            steps.append(
                {
                    "step_revision_id": revision.get("step_revision_id"),
                    "output_sha256": revision.get("output_sha256"),
                    "error": "step content is not the five text fields",
                }
            )
            continue
        result = normalize_step(
            content, sources=sources, tables=tables, whitelist=whitelist
        )
        normalized = result.pop("normalized")
        steps.append(
            {
                "step_revision_id": revision["step_revision_id"],
                "branch_id": revision.get("branch_id"),
                "step_slot": revision.get("step_slot"),
                "revision": revision.get("revision"),
                "output_sha256": revision["output_sha256"],
                "on_delivered_route": revision["step_revision_id"] in on_route,
                "recorded_content_sha256": _content_sha256(content),
                "normalization": result,
                "_normalized": normalized,
            }
        )
    return steps


# ---------------------------------------------------------------------------
# Failure classes: what a repair round would be asked to fix


def failure_class(kind: str, message: str) -> str:
    """A short label grouping compiler failures by what a repair would face."""

    if kind != "tex_error":
        return kind
    if _UNDEFINED.search(message):
        context = message.split("(context:", 1)
        names = _CONTROL_WORD.findall(context[-1] if len(context) > 1 else message)
        if not names:
            return "undefined_control_sequence"
        return f"undefined_control_sequence:\\{names[-1]}"
    head = message.split("(context:", 1)[0].strip()
    head = re.sub(r"\s+", " ", head)
    head = re.sub(r"[`'\"][^`'\"]*['\"`]", "...", head)
    return head[:80] or "tex_error"


def _residual_failures(layer: Mapping[str, Any]) -> list[dict[str, Any]]:
    residual: list[dict[str, Any]] = []
    for entry in layer.get("formulas", ()):
        status = entry.get("status")
        if status not in {STATUS_FAILED, STATUS_QUOTATION_VERBATIM, STATUS_NOT_COMPILED}:
            continue
        errors = entry.get("compiler_errors") or []
        last = errors[-1] if errors else {}
        residual.append(
            {
                "formula_id": entry["formula_id"],
                "step_revision_id": entry["step_revision_id"],
                "field": entry["field"],
                "index": entry["index"],
                "status": status,
                "quotation": entry.get("quotation"),
                "kind": last.get("kind"),
                "message": last.get("message"),
                "failure_class": failure_class(
                    str(last.get("kind") or "unknown"), str(last.get("message") or "")
                ),
                "latex": entry.get("original"),
            }
        )
    return residual


# ---------------------------------------------------------------------------
# Building one view


def _prune_evidence(root: Path, retention: str, *, output_root: Path) -> None:
    keep = _RETAINED_SUFFIXES[retention]
    if keep is None or not root.exists():
        return
    assert_inside(root, output_root)
    for path in sorted(root.rglob("*"), key=lambda item: -len(item.parts)):
        if path.is_file() and not path.name.endswith(tuple(keep) or ("\0",)):
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    if not any(root.iterdir()):
        root.rmdir()


async def _compile_recorded_route(
    steps: Sequence[Mapping[str, Any]],
    *,
    runner: TectonicRunner,
    evidence_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Compile the route exactly as recorded: the "before" of the view."""

    from .formula_compiler import compile_fragments

    fragments: list[tuple[str, str]] = []
    for step in steps:
        content = {name: step["content"][name] for name in STEP_FIELDS}
        for fragment in math_fragments(content):
            formula_id = (
                f"{step['step_revision_id']}:{fragment.field}:"
                f"{fragment.formula_index}"
            )
            fragments.append((formula_id, fragment.value))
    if not fragments:
        return {"fragments": 0, "compiles": 0, "seconds": 0.0, "passed": True,
                "failures": [], "infrastructure_error": None}
    result = await asyncio.to_thread(
        compile_fragments,
        [value for _, value in fragments],
        runner,
        evidence_dir,
        label=label,
    )
    failures = []
    for failure in sorted(result.failures, key=lambda item: item.index):
        message = _compiler_message(failure, result.compiles, evidence_dir)
        failures.append(
            {
                "formula_id": fragments[failure.index][0],
                "kind": failure.kind,
                "message": message,
                "failure_class": failure_class(failure.kind, message),
                "latex": fragments[failure.index][1],
            }
        )
    return {
        "fragments": len(fragments),
        "compiles": len(result.compiles),
        "seconds": round(sum(result.durations), 3),
        "passed": result.passed,
        "failures": failures,
        "infrastructure_error": result.infrastructure_error,
    }


async def build_view(
    run: ArchiveRun,
    *,
    output_directory: Path,
    output_root: Path,
    runner: TectonicRunner,
    whitelist: EngineWhitelist,
    runtime: Any | None = None,
    evidence: str = "summary",
    compile_recorded: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build the typeset view of one archived run and write it out.

    ``output_directory`` must be inside ``output_root`` and outside the archive:
    the whole point of a view is that the run directory stays byte for byte what
    it was.
    """

    started = clock()
    output_directory = assert_inside(output_directory, output_root)
    if output_directory.is_relative_to(run.directory) or run.directory.is_relative_to(
        output_directory
    ):
        raise ArchiveError(
            "a typeset view must not be written inside the archived run directory"
        )
    output_directory.mkdir(parents=True, exist_ok=True)

    tables = SourceMacroTables(run.sources, run.documents)
    route = select_route(run.canonical)
    recorded_steps = (
        record_route_steps(run.canonical, route.branch_id) if route is not None else []
    )
    steps = normalize_sealed_steps(
        run.canonical,
        sources=run.sources,
        tables=tables,
        whitelist=whitelist,
        route_step_ids=[item["step_revision_id"] for item in recorded_steps],
    )
    normalized_by_id = {
        item["step_revision_id"]: item.pop("_normalized")
        for item in steps
        if "_normalized" in item
    }

    view: dict[str, Any] = {
        "schema_version": ARCHIVE_VIEW_SCHEMA_VERSION,
        "generated_by": "derivation_app.typeset_archive",
        "normalizer_version": NORMALIZER_VERSION,
        "engine_whitelist": whitelist.identity(),
        "run": {
            "run_id": run.run_id,
            "run_path": run.relative_path,
            "events_sha256": run.events_sha256,
            "event_count": run.event_count,
            "record_schema_version": run.canonical.get("schema_version"),
            "formula_validation_policy": run.config.formula_validation_policy,
        },
        "sources": [
            {
                "source_id": source_id,
                "sha256": _sha256_text(text),
                "document": run.documents.get(source_id, source_id),
            }
            for source_id, text in sorted(run.sources.items())
        ],
        "steps": steps,
        "evidence_retention": evidence,
    }

    if route is None:
        view["route"] = None
        view["seconds"] = round(max(0.0, clock() - started), 3)
        write_view(output_directory, view, output_root=output_root)
        return view

    evidence_root = assert_inside(
        output_directory / "recorded.evidence", output_root
    )
    before = (
        await _compile_recorded_route(
            recorded_steps,
            runner=runner,
            evidence_dir=evidence_root / "compile",
            label=f"archive-recorded:{route.route_id}",
        )
        if compile_recorded
        else None
    )

    typeset_steps = [
        {
            "step_revision_id": step["step_revision_id"],
            "output_sha256": step["output_sha256"],
            "content": normalized_by_id.get(
                step["step_revision_id"], dict(step["content"])
            ),
        }
        for step in recorded_steps
    ]
    budget = TypesetCallBudget(
        journal_path=typeset_directory(output_directory) / "model_calls.jsonl",
        max_model_calls=run.config.max_model_calls,
        record_model_calls=int(run.canonical["summary"]["model_call_count"]),
    )
    layer = await typeset_route(
        run_directory=output_directory,
        config=run.config,  # type: ignore[arg-type]  # see ArchiveConfig
        route_id=route.route_id,
        branch_id=route.branch_id,
        steps=typeset_steps,
        sources=run.sources,
        documents=run.documents,
        whitelist=whitelist,
        runner=runner,
        runtime=runtime,
        budget=budget,
        clock=clock,
    )
    layer["archive_view"] = {
        "built_from": "normalized_record_content",
        "run_path": run.relative_path,
        "events_sha256": run.events_sha256,
    }
    assert_inside(layer_path(output_directory, route.route_id), output_root)
    write_typeset_layer(output_directory, layer)

    recorded_fragments = {
        (step["step_revision_id"], fragment.field, fragment.formula_index): (
            fragment.value
        )
        for step in recorded_steps
        for fragment in math_fragments(
            {name: step["content"][name] for name in STEP_FIELDS}
        )
    }
    for entry in layer["formulas"]:
        key = (entry["step_revision_id"], entry["field"], entry["index"])
        recorded = recorded_fragments.get(key)
        entry["record_original"] = recorded
        entry["normalized_equals_record"] = recorded == entry["original"]

    view["route"] = {
        "route_id": route.route_id,
        "branch_id": route.branch_id,
        "selection": route.selection,
        "step_revision_ids": [item["step_revision_id"] for item in recorded_steps],
        "recorded_compile": before,
        "typeset_layer": layer,
        "residual_failures": _residual_failures(layer),
    }
    view["seconds"] = round(max(0.0, clock() - started), 3)
    _prune_evidence(evidence_root, evidence, output_root=output_root)
    _prune_evidence(
        typeset_directory(output_directory) / f"{route.route_id}.evidence",
        evidence,
        output_root=output_root,
    )
    write_view(output_directory, view, output_root=output_root)
    return view


def write_view(
    output_directory: Path, view: Mapping[str, Any], *, output_root: Path
) -> Path:
    path = assert_inside(Path(output_directory) / VIEW_FILENAME, output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.pending")
    encoded = json.dumps(dict(view), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with pending.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)
    return path


# ---------------------------------------------------------------------------
# The whole archive


def view_summary(view: Mapping[str, Any]) -> dict[str, Any]:
    """One index row: what happened to this run, without the detail."""

    steps = view.get("steps") or []
    control = 0
    macros = 0
    replacements = 0
    residual_issue_codes: Counter[str] = Counter()
    for step in steps:
        kinds = ((step.get("normalization") or {}).get("replacement_kinds")) or {}
        for kind, count in kinds.items():
            replacements += count
            if kind in CONTROL_KINDS:
                control += count
            elif kind == "macro_expansion":
                macros += count
        for issue in ((step.get("normalization") or {}).get("issues_after")) or []:
            if issue.get("severity") == "error":
                residual_issue_codes[str(issue.get("code"))] += 1
    route = view.get("route")
    row: dict[str, Any] = {
        "run_path": view["run"]["run_path"],
        "run_id": view["run"]["run_id"],
        "events_sha256": view["run"]["events_sha256"],
        "record_schema_version": view["run"]["record_schema_version"],
        "steps": len(steps),
        "replacements": replacements,
        "control_characters_repaired": control,
        "macros_expanded": macros,
        "residual_static_error_codes": dict(sorted(residual_issue_codes.items())),
        "seconds": view.get("seconds"),
    }
    if route is None:
        row.update({"route_id": None, "formulas": 0, "route_status": "no_route"})
        return row
    layer = route["typeset_layer"]
    statuses = Counter(entry["status"] for entry in layer["formulas"])
    before = route.get("recorded_compile") or {}
    row.update(
        {
            "route_id": route["route_id"],
            "branch_id": route["branch_id"],
            "route_selection": route["selection"],
            "formulas": len(layer["formulas"]),
            "formula_statuses": dict(sorted(statuses.items())),
            "route_status": layer["status"],
            "recorded_compile_passed": before.get("passed"),
            "recorded_compile_failures": len(before.get("failures") or []),
            "recorded_compile_seconds": before.get("seconds"),
            "typeset_compile_seconds": layer.get("compile_seconds"),
            "residual_failures": len(route["residual_failures"]),
            "residual_failure_classes": dict(
                sorted(
                    Counter(
                        item["failure_class"] for item in route["residual_failures"]
                    ).items()
                )
            ),
            "repair_rounds": layer.get("repair_rounds", 0),
            "model_calls": len(layer.get("calls") or []),
        }
    )
    return row


def _index_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    classes: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for row in rows:
        classes.update(row.get("residual_failure_classes") or {})
        statuses.update(row.get("formula_statuses") or {})
    with_route = [row for row in rows if row.get("route_id")]
    compiled_before = [
        row for row in with_route if row.get("recorded_compile_passed") is True
    ]
    compiled_after = [
        row for row in with_route if row.get("route_status") == "compiled"
    ]
    return {
        "runs": len(rows),
        "runs_with_route": len(with_route),
        "steps": sum(int(row.get("steps") or 0) for row in rows),
        "formulas": sum(int(row.get("formulas") or 0) for row in rows),
        "control_characters_repaired": sum(
            int(row.get("control_characters_repaired") or 0) for row in rows
        ),
        "macros_expanded": sum(int(row.get("macros_expanded") or 0) for row in rows),
        "routes_compiling_before": len(compiled_before),
        "routes_compiling_after": len(compiled_after),
        "residual_failures": sum(
            int(row.get("residual_failures") or 0) for row in rows
        ),
        "residual_failure_classes": dict(classes.most_common()),
        "formula_statuses": dict(sorted(statuses.items())),
    }


async def build_archive_views(
    *,
    runs_root: Path,
    output_root: Path,
    runner: TectonicRunner,
    whitelist: EngineWhitelist,
    directories: Sequence[Path] | None = None,
    runtime_factory: Callable[[ArchiveRun], Any | None] | None = None,
    evidence: str = "summary",
    compile_recorded: bool = True,
    concurrency: int = 4,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    """Build every view under ``output_root`` and return the index."""

    runs_root = Path(runs_root).resolve()
    output_root = Path(output_root).resolve()
    found = (
        [Path(item).resolve() for item in directories]
        if directories is not None
        else discover_run_directories(runs_root)
    )
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def one(directory: Path) -> None:
        relative = directory.relative_to(runs_root).as_posix()
        try:
            run = await asyncio.to_thread(
                load_archive_run, directory, runs_root=runs_root
            )
        except ArchiveError as exc:
            async with lock:
                skipped.append({"run_path": relative, "reason": str(exc)})
                log(f"skip {relative}: {exc}")
            return
        runtime = runtime_factory(run) if runtime_factory is not None else None
        async with semaphore:
            try:
                view = await build_view(
                    run,
                    output_directory=output_root / relative,
                    output_root=output_root,
                    runner=runner,
                    whitelist=whitelist,
                    runtime=runtime,
                    evidence=evidence,
                    compile_recorded=compile_recorded,
                )
            except ArchiveError as exc:
                async with lock:
                    skipped.append({"run_path": relative, "reason": str(exc)})
                    log(f"skip {relative}: {exc}")
                return
        async with lock:
            rows.append(view_summary(view))
            log(f"done {relative}")

    await asyncio.gather(*(one(directory) for directory in found))
    rows.sort(key=lambda row: row["run_path"])
    skipped.sort(key=lambda row: row["run_path"])
    index = {
        "schema_version": ARCHIVE_INDEX_SCHEMA_VERSION,
        "generated_by": "derivation_app.typeset_archive",
        "normalizer_version": NORMALIZER_VERSION,
        "engine_whitelist": whitelist.identity(),
        "engine": {
            "version": runner.runtime.version,
            "target": runner.runtime.target,
            "binary_sha256": runner.runtime.binary_sha256,
            "bundle_sha256": runner.runtime.bundle_sha256,
        },
        "runs_root": runs_root.as_posix(),
        "repair": runtime_factory is not None,
        "evidence_retention": evidence,
        "totals": _index_totals(rows),
        "runs": rows,
        "skipped": skipped,
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    path = assert_inside(output_root / INDEX_FILENAME, output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


# ---------------------------------------------------------------------------
# CLI


def _load_runtime_factory(spec: str) -> Callable[[ArchiveRun], Any | None]:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ArchiveError("a runtime factory is written module:function")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ArchiveError(f"runtime factory {spec!r} is not callable")
    return factory


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m derivation_app.typeset_archive",
        description=(
            "Build typeset views of archived runs. The archive is only read; "
            "every view is written under --out."
        ),
    )
    repository = _repository_root()
    parser.add_argument("--runs", type=Path, default=repository / "runs")
    parser.add_argument("--out", type=Path, default=repository / "runs" / "typeset_views")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=repository,
        help=(
            "repository holding the provisioned Tectonic resources "
            "(a worktree without them can point at one that has them)"
        ),
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=repository / "config" / "reporting" / "tectonic_runtime.lock.json",
    )
    parser.add_argument("--whitelist", type=Path, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--evidence", choices=EVIDENCE_RETENTION, default="summary",
        help="how much compiler evidence to keep (default: the .compiler.json)",
    )
    parser.add_argument(
        "--no-recorded-compile",
        action="store_true",
        help="skip compiling the route as recorded (the before/after comparison)",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="run model repair rounds; needs --runtime-factory and makes model calls",
    )
    parser.add_argument(
        "--runtime-factory",
        default=None,
        help="module:function returning a model runtime for one ArchiveRun",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    from .reporting import TectonicRunner, TectonicRuntimeSpec

    if args.repair and not args.runtime_factory:
        parser.error("--repair needs --runtime-factory module:function")
    factory = (
        _load_runtime_factory(args.runtime_factory)
        if args.repair and args.runtime_factory
        else None
    )
    spec = TectonicRuntimeSpec.from_lock(args.runtime_root, lock_path=args.lock)
    if not spec.provisioned:
        parser.error(
            f"the Tectonic runtime under {args.runtime_root} is not provisioned"
        )
    whitelist = (
        load_engine_whitelist()
        if args.whitelist is None
        else EngineWhitelist.from_record(
            json.loads(args.whitelist.read_text(encoding="utf-8"))
        )
    )
    directories = discover_run_directories(args.runs)
    if args.limit is not None:
        directories = directories[: args.limit]

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    index = asyncio.run(
        build_archive_views(
            runs_root=args.runs,
            output_root=args.out,
            runner=TectonicRunner(spec),
            whitelist=whitelist,
            directories=directories,
            runtime_factory=factory,
            evidence=args.evidence,
            compile_recorded=not args.no_recorded_compile,
            concurrency=args.concurrency,
            log=log,
        )
    )
    print(json.dumps(index["totals"], ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())


__all__ = [
    "ARCHIVE_INDEX_SCHEMA_VERSION",
    "ARCHIVE_VIEW_SCHEMA_VERSION",
    "ArchiveConfig",
    "ArchiveError",
    "ArchiveRun",
    "RouteSelection",
    "assert_inside",
    "build_archive_views",
    "build_view",
    "discover_run_directories",
    "failure_class",
    "load_archive_run",
    "main",
    "normalize_sealed_steps",
    "normalize_step",
    "record_route_steps",
    "select_route",
    "view_summary",
    "write_view",
]
