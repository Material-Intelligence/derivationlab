"""Allowlisted, hash-bound literature snapshots and bounded reading tools.

No TeX commands are executed and no ``input``/``include`` paths are followed.
Line numbers refer to the original source: comment stripping preserves lines.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .types import EvidenceSource

# The method-pack allowlist: every DOI a pack may hold, mapped to the exact
# files (relative to METHOD_PACK_ROOT) a run may read for it. It lives in code
# so that a pack file can never grant itself more reading authority. This
# repository ships no pack and no allowlist entry, so every pack is refused; a
# deployment that holds a licensed pack fills these two in.
METHOD_PACK_INPUTS: dict[str, tuple[str, ...]] = {}
METHOD_PACK_ROOT = "data/method_pack"
SOURCE_TOOL_NAMES = ("source_catalog", "source_read", "source_search")


class SourceMaterialError(ValueError):
    """The snapshot or requested source fails its declared boundary."""


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pack_projection(raw: bytes) -> dict[str, Any]:
    """Read the frozen pack's tiny supported surface, not arbitrary YAML.

    The fixed member/path allowlist is intentional. New pack formats require a
    reviewed parser change rather than silently gaining file-read authority.
    """
    allowed = METHOD_PACK_INPUTS
    if not allowed:
        raise SourceMaterialError("no method pack is allowlisted in this build")
    text = raw.decode("utf-8")
    if not re.search(r"^pack_id: independent\s*$", text, re.MULTILINE) or not re.search(
        r"^includes_answer_paper: false\s*$", text, re.MULTILINE
    ):
        raise SourceMaterialError("only the independent method pack is supported")
    root_match = re.search(r"^local_root: ([^\s#]+)\s*$", text, re.MULTILINE)
    if root_match is None or root_match[1] != METHOD_PACK_ROOT:
        raise SourceMaterialError("method pack source root differs from the allowlist")
    blocks = re.split(r'^  - doi: "([^"]+)"\s*$', text, flags=re.MULTILINE)
    papers = []
    for index in range(1, len(blocks), 2):
        doi, block = blocks[index : index + 2]
        title = re.search(r'^    title: (".*")\s*$', block, re.MULTILINE)
        paths = re.findall(r"^      - ([^\s#]+)\s*$", block, re.MULTILINE)
        paths += re.findall(r"^      model_input: ([^\s#]+)\s*$", block, re.MULTILINE)
        if doi not in allowed or tuple(paths) != allowed[doi] or title is None:
            raise SourceMaterialError(
                "method pack input paths differ from the allowlist"
            )
        papers.append({"doi": doi, "title": json.loads(title[1]), "model_input": paths})
    if len(papers) != len(allowed) or {p["doi"] for p in papers} != set(allowed):
        raise SourceMaterialError(
            "method pack membership differs from the allowed papers"
        )
    return {
        "pack_id": "independent",
        "includes_answer_paper": False,
        "local_root": root_match[1],
        "papers": papers,
    }


def _safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {"..", "."} for p in path.parts)
    ):
        raise SourceMaterialError("invalid relative source path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise SourceMaterialError("source symlinks are forbidden")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise SourceMaterialError("source is not a regular allowlisted file")
    return current


def _strip_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        for match in re.finditer("%", line):
            prefix = line[: match.start()]
            if (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0:
                line = prefix
                break
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


class SourceLibrary:
    """A run-owned immutable-by-hash snapshot; never a filesystem browser."""

    def __init__(self, snapshot_dir: Path, manifest: Mapping[str, Any]) -> None:
        self.snapshot_dir = snapshot_dir.resolve()
        self._manifest = json.loads(json.dumps(manifest))
        self._by_id = {entry["source_id"]: entry for entry in self._manifest["sources"]}
        if len(self._by_id) != len(self._manifest["sources"]):
            raise SourceMaterialError("duplicate source id")
        for entry in self._by_id.values():
            self._text(entry)

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._manifest))

    @property
    def manifest_sha256(self) -> str:
        return _sha(_encoded(self._manifest))

    @classmethod
    def empty(cls, snapshot_dir: Path) -> SourceLibrary:
        return cls._write(
            snapshot_dir,
            {"version": 1, "mode": "none", "pack_id": None, "sources": []},
            {},
        )

    @classmethod
    def from_pack(
        cls, pack_path: Path, repository_root: Path, snapshot_dir: Path
    ) -> SourceLibrary:
        root = repository_root.resolve()
        candidate = (
            pack_path
            if pack_path.is_absolute()
            else repository_root.absolute() / pack_path
        )
        try:
            relative_pack = str(candidate.relative_to(repository_root.absolute()))
        except ValueError as exc:
            raise SourceMaterialError("pack is outside the repository") from exc
        pack = _safe_file(root, relative_pack)
        raw_pack = pack.read_bytes()
        data = _pack_projection(raw_pack)
        if (
            not isinstance(data, dict)
            or data.get("pack_id") != "independent"
            or data.get("includes_answer_paper") is not False
        ):
            raise SourceMaterialError("only the independent method pack is supported")
        papers = data.get("papers", [])
        if len(papers) != len(METHOD_PACK_INPUTS) or {
            p.get("doi") for p in papers
        } != set(METHOD_PACK_INPUTS):
            raise SourceMaterialError(
                "method pack membership differs from the allowed papers"
            )
        local_root = data["local_root"]
        sources, files = [], {}
        for paper in papers:
            inputs = list(paper["model_input"])
            supplemental = paper.get("supplemental")
            if isinstance(supplemental, dict) and supplemental.get("model_input"):
                inputs.append(supplemental["model_input"])
            for position, relative in enumerate(inputs):
                original = _safe_file(root, str(Path(local_root) / relative))
                raw = original.read_bytes()
                text = raw.decode("utf-8")
                if original.suffix == ".tex":
                    text = _strip_comments(text)
                payload = text.encode()
                source_id = (
                    "src_" + _sha((paper["doi"] + "\n" + relative).encode())[:20]
                )
                filename = source_id + ".txt"
                files[filename] = payload
                sources.append(
                    {
                        "source_id": source_id,
                        "doi": paper["doi"],
                        "title": paper["title"],
                        "source_relpath": str(Path(local_root) / relative),
                        "snapshot_file": filename,
                        "raw_sha256": _sha(raw),
                        "sha256": _sha(payload),
                        "line_count": len(text.splitlines()),
                        "part_order": position,
                        "macros": original.name == "definitions.tex",
                        "preprocess": "strip_tex_comments_preserve_lines"
                        if original.suffix == ".tex"
                        else "verbatim",
                    }
                )
        manifest = {
            "version": 1,
            "mode": "methods",
            "pack_id": "independent",
            "pack_sha256": _sha(raw_pack),
            "sources": sources,
        }
        return cls._write(snapshot_dir, manifest, files)

    @classmethod
    def _write(
        cls, directory: Path, manifest: dict[str, Any], files: Mapping[str, bytes]
    ) -> SourceLibrary:
        if directory.is_symlink() or any(p.is_symlink() for p in directory.parents):
            raise SourceMaterialError("snapshot symlinks are forbidden")
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            existing = cls.load(
                directory, expected_manifest_sha256=_sha(_encoded(manifest))
            )
            return existing
        for name, payload in files.items():
            (directory / name).write_bytes(payload)
        (directory / "manifest.json").write_bytes(_encoded(manifest))
        return cls(directory, manifest)

    @classmethod
    def load(
        cls, snapshot_dir: Path, expected_manifest_sha256: str | None = None
    ) -> SourceLibrary:
        if snapshot_dir.is_symlink() or any(
            p.is_symlink() for p in snapshot_dir.parents
        ):
            raise SourceMaterialError("snapshot symlinks are forbidden")
        manifest = json.loads(_safe_file(snapshot_dir, "manifest.json").read_text())
        if (
            expected_manifest_sha256 is not None
            and _sha(_encoded(manifest)) != expected_manifest_sha256
        ):
            raise SourceMaterialError("source manifest hash mismatch")
        if manifest.get("version") != 1 or manifest.get("mode") not in {
            "none",
            "methods",
        }:
            raise SourceMaterialError("unsupported source manifest")
        if manifest["mode"] == "none" and manifest["sources"]:
            raise SourceMaterialError("empty condition contains sources")
        if manifest["mode"] == "methods" and (
            not METHOD_PACK_INPUTS
            or {x["doi"] for x in manifest["sources"]} != set(METHOD_PACK_INPUTS)
        ):
            raise SourceMaterialError("snapshot membership mismatch")
        expected_files = {
            "manifest.json",
            *(x["snapshot_file"] for x in manifest["sources"]),
        }
        if {p.name for p in snapshot_dir.iterdir()} != expected_files:
            raise SourceMaterialError("unexpected snapshot files")
        return cls(snapshot_dir, manifest)

    def _text(self, entry: Mapping[str, Any]) -> str:
        payload = _safe_file(self.snapshot_dir, entry["snapshot_file"]).read_bytes()
        if _sha(payload) != entry["sha256"]:
            raise SourceMaterialError("source content hash mismatch")
        return payload.decode("utf-8")

    def delivery_bundle(self, source_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return exact selected source text with immutable provenance.

        This is for system-arranged model delivery, not filesystem browsing.
        Callers must select IDs from this run-owned snapshot. Duplicate IDs,
        unknown IDs, and partial multi-part papers fail closed.
        """

        selected = list(source_ids)
        if not selected or len(selected) != len(set(selected)):
            raise SourceMaterialError(
                "source delivery requires unique selected source ids"
            )
        if any(source_id not in self._by_id for source_id in selected):
            raise SourceMaterialError("source delivery contains an unknown source id")
        selected_dois = {self._by_id[source_id]["doi"] for source_id in selected}
        expected = {
            source_id
            for source_id, entry in self._by_id.items()
            if entry["doi"] in selected_dois
        }
        if set(selected) != expected:
            raise SourceMaterialError(
                "source delivery must include every macro/supplement part of a selected paper"
            )
        result = []
        for source_id in selected:
            entry = self._by_id[source_id]
            text = self._text(entry)
            result.append(
                {
                    "source_id": source_id,
                    "doi": entry["doi"],
                    "title": entry["title"],
                    "source_relpath": entry["source_relpath"],
                    "sha256": entry["sha256"],
                    "line_count": entry["line_count"],
                    "part_order": entry["part_order"],
                    "macros": entry["macros"],
                    "character_count": len(text),
                    "text": text,
                }
            )
        return result

    def catalog(self) -> list[dict[str, Any]]:
        result = []
        for entry in self._by_id.values():
            item = {
                key: entry[key]
                for key in (
                    "source_id",
                    "doi",
                    "title",
                    "line_count",
                    "sha256",
                    "part_order",
                    "macros",
                )
            }
            sections = [
                {"line": number, "text": line[:100]}
                for number, line in enumerate(self._text(entry).splitlines(), 1)
                if re.search(r"\\(?:sub)*section\*?\{", line)
            ]
            item["first_sections"] = sections[:4]
            item["section_count"] = len(sections)
            result.append(item)
        return result

    def read(
        self, source_id: str, start_line: int = 1, line_count: int = 35
    ) -> dict[str, Any]:
        if not self._by_id:
            raise SourceMaterialError("literature_disabled")
        if source_id not in self._by_id:
            raise SourceMaterialError("unknown source id")
        if (
            type(start_line) is not int
            or type(line_count) is not int
            or start_line < 1
            or not 1 <= line_count <= 60
        ):
            raise SourceMaterialError("invalid line range")
        entry = self._by_id[source_id]
        lines = self._text(entry).splitlines()
        if start_line > len(lines):
            raise SourceMaterialError("start line exceeds source")
        selected = []
        chars = 0
        for index in range(
            start_line - 1, min(len(lines), start_line - 1 + line_count)
        ):
            line = lines[index]
            if chars + len(line) > 4000:
                if not selected:
                    raise SourceMaterialError(
                        "source line too long; inspect snapshot outside model runtime"
                    )
                break
            selected.append({"line": index + 1, "text": line})
            chars += len(line)
        return {
            "source_id": source_id,
            "sha256": entry["sha256"],
            "lines": selected,
            "next_line": selected[-1]["line"] + 1
            if selected[-1]["line"] < len(lines)
            else None,
        }

    def search(
        self, query: str, source_id: str | None = None, limit: int = 12
    ) -> dict[str, Any]:
        if not self._by_id:
            raise SourceMaterialError("literature_disabled")
        if (
            not isinstance(query, str)
            or not 1 <= len(query) <= 160
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise SourceMaterialError("invalid search request")
        if source_id is not None and source_id not in self._by_id:
            raise SourceMaterialError("unknown source id")
        matches = []
        for entry in self._by_id.values():
            if source_id is not None and entry["source_id"] != source_id:
                continue
            for number, line in enumerate(self._text(entry).splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append(
                        {
                            "source_id": entry["source_id"],
                            "line": number,
                            "text": line[:200],
                            "sha256": entry["sha256"],
                        }
                    )
                    if len(matches) == limit:
                        return {"matches": matches, "limit_reached": True}
        return {"matches": matches, "limit_reached": False}

    def tools(self) -> list[SourceTool]:
        return [SourceTool(name, self) for name in SOURCE_TOOL_NAMES]

    def evidence_sources(self) -> tuple[EvidenceSource, ...]:
        """Full strings for Record substring verification, not prompt injection."""
        return tuple(
            EvidenceSource("literature_quote", entry["source_id"], self._text(entry))
            for entry in self._by_id.values()
        )

    def evidence_source_documents(self) -> dict[str, str]:
        """Source id -> DOI, so the parts of one paper share macro scope."""
        return {entry["source_id"]: entry["doi"] for entry in self._by_id.values()}


def _spec(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


SOURCE_TOOL_SPECS = {
    "source_catalog": _spec(
        "source_catalog",
        "List only this run's allowed literature and macro parts. No external or repository access.",
        {},
        [],
    ),
    "source_read": _spec(
        "source_read",
        "Read numbered original-source lines; use source_id and hash with line numbers for exact citations. TeX includes are never followed. Read definitions parts for macros.",
        {
            "source_id": {"type": "string"},
            "start_line": {"type": "integer"},
            "line_count": {"type": "integer"},
        },
        ["source_id", "start_line", "line_count"],
    ),
    "source_search": _spec(
        "source_search",
        "Literal case-insensitive search over allowed text. Search section names or TeX label strings to locate formulas, then read adjacent lines.",
        {
            "query": {"type": "string"},
            "source_id": {"type": ["string", "null"]},
            "limit": {"type": "integer"},
        },
        ["query", "source_id", "limit"],
    ),
}


def _result(payload: Any, *, success: bool = True) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 7900:
        text, success = '{"error":"result_too_large; request a smaller range"}', False
    return {"contentItems": [{"type": "inputText", "text": text}], "success": success}


class SourceTool:
    def __init__(self, name: str, library: SourceLibrary) -> None:
        self.name, self.spec, self.library = name, SOURCE_TOOL_SPECS[name], library

    async def invoke(self, arguments: object) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict) or set(arguments) != set(
                self.spec["inputSchema"]["required"]
            ):
                raise SourceMaterialError("invalid tool arguments")
            if self.name == "source_catalog":
                payload = {"sources": self.library.catalog()}
            elif self.name == "source_read":
                payload = self.library.read(**arguments)
            else:
                payload = self.library.search(**arguments)
            return _result(payload)
        except (SourceMaterialError, TypeError) as exc:
            return _result({"error": str(exc)}, success=False)


class TranscriptReadTool:
    """Reads only immutable step IDs from the currently executing Writer request."""

    name = "transcript_read"
    spec = _spec(
        name,
        "Read a step from the current allowed derivation transcript by its step_revision_id. No file access.",
        {
            "step_revision_id": {"type": "string"},
            "field": {
                "type": "string",
                "enum": ["claim", "why", "source", "derivation", "scope"],
            },
            "offset": {"type": "integer"},
        },
        ["step_revision_id", "field", "offset"],
    )

    def __init__(self) -> None:
        self._steps: dict[str, Any] = {}

    def bind(self, steps: Sequence[Any]) -> None:
        self._steps = {step.step_revision_id: step for step in steps}

    async def invoke(self, arguments: object) -> dict[str, Any]:
        if not isinstance(arguments, dict) or set(arguments) != {
            "step_revision_id",
            "field",
            "offset",
        }:
            return _result({"error": "invalid transcript request"}, success=False)
        step = (
            self._steps.get(arguments["step_revision_id"])
            if isinstance(arguments["step_revision_id"], str)
            else None
        )
        field, offset = arguments["field"], arguments["offset"]
        if (
            step is None
            or not isinstance(field, str)
            or field not in {"claim", "why", "source", "derivation", "scope"}
            or type(offset) is not int
            or offset < 0
        ):
            return _result(
                {"error": "unknown transcript step or invalid range"}, success=False
            )
        content = getattr(step.content, field)
        chunk = content[offset : offset + 5000]
        return _result(
            {
                "step_revision_id": step.step_revision_id,
                "field": field,
                "sha256": _sha(content.encode()),
                "offset": offset,
                "text": chunk,
                "next_offset": offset + len(chunk)
                if offset + len(chunk) < len(content)
                else None,
            }
        )


class TranscriptCatalogTool:
    name = "transcript_catalog"
    spec = _spec(
        name,
        "Page through the current allowed transcript directory, eight steps per page. Use transcript_read for full fields.",
        {"offset": {"type": "integer"}},
        ["offset"],
    )

    def __init__(self, reader: TranscriptReadTool) -> None:
        self.reader = reader

    async def invoke(self, arguments: object) -> dict[str, Any]:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"offset"}
            or type(arguments["offset"]) is not int
            or arguments["offset"] < 0
        ):
            return _result({"error": "invalid transcript offset"}, success=False)
        offset = arguments["offset"]
        steps = list(self.reader._steps.values())
        selected = steps[offset : offset + 8]
        return _result(
            {
                "steps": [
                    {
                        "step_revision_id": step.step_revision_id,
                        "claim": step.content.claim[:200],
                        "scope": step.content.scope[:200],
                    }
                    for step in selected
                ],
                "next_offset": offset + len(selected)
                if offset + len(selected) < len(steps)
                else None,
            }
        )


class FeedbackReadTool:
    """Read only the current Writer's frozen unresolved-check feedback."""

    name = "feedback_read"
    spec = _spec(
        name,
        "Read frozen Checker feedback. Use check_id='catalog' with an item offset for eight directory rows; otherwise offset is a character position in the check's JSON. Follow next_offset.",
        {"check_id": {"type": "string"}, "offset": {"type": "integer"}},
        ["check_id", "offset"],
    )

    def __init__(self) -> None:
        self._feedback: dict[str, dict[str, Any]] = {}

    def bind(self, feedback: Sequence[Mapping[str, Any]]) -> None:
        self._feedback = {
            item["check_id"]: json.loads(json.dumps(item)) for item in feedback
        }

    async def invoke(self, arguments: object) -> dict[str, Any]:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"check_id", "offset"}
            or not isinstance(arguments["check_id"], str)
            or type(arguments["offset"]) is not int
            or arguments["offset"] < 0
        ):
            return _result({"error": "invalid feedback request"}, success=False)
        check_id, offset = arguments["check_id"], arguments["offset"]
        if check_id == "catalog":
            rows = list(self._feedback.values())
            selected = rows[offset : offset + 8]
            return _result(
                {
                    "checks": [
                        {
                            key: row.get(key)
                            for key in ("check_id", "step_revision_id", "verdict")
                        }
                        for row in selected
                    ],
                    "next_offset": offset + len(selected)
                    if offset + len(selected) < len(rows)
                    else None,
                }
            )
        if check_id not in self._feedback:
            return _result({"error": "unknown current feedback check"}, success=False)
        text = _encoded(self._feedback[check_id]).decode()
        chunk = text[offset : offset + 3000]
        return _result(
            {
                "check_id": check_id,
                "sha256": _sha(text.encode()),
                "offset": offset,
                "text": chunk,
                "next_offset": offset + len(chunk)
                if offset + len(chunk) < len(text)
                else None,
            }
        )
