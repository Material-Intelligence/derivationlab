"""Literature isolation and real client-owned reading-tool regression tests."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from . import source_material
from .app_server_client import AppServerClient, ClientTimeouts
from .app_server_runtime import CodexAppServerRuntime
from .capabilities import SOURCE_READING_V1
from .evidence import validate_check_output
from .source_material import (
    FeedbackReadTool,
    SourceLibrary,
    SourceMaterialError,
    TranscriptReadTool,
)
from .test_app_server_runtime import (
    ScriptedAppServerClient,
    TurnScript,
    config,
    settings,
)
from .types import (
    CheckRequest,
    EvidenceSource,
    ModelRole,
    RuntimeInvocationError,
    StepContent,
    StepSnapshot,
)

ROOT = Path(__file__).resolve().parents[2]

# A self-written stand-in for a licensed pack: two short notes, one of them in
# two parts (definitions + manuscript), so every rule the real pack exercises
# (allowlisted paths, multi-part papers, macro parts, comment stripping) has
# something to act on. Installed as the allowlist only inside these tests.
SYNTHETIC_ROOT = "data/method_pack"
SYNTHETIC_INPUTS = {
    "10.0000/synthetic.a": ("notes/a/definitions.tex", "notes/a/manuscript.tex"),
    "10.0000/synthetic.b": ("notes/b/manuscript.tex",),
}
SYNTHETIC_FILES = {
    "notes/a/definitions.tex": "\\def\\om{\\omega}\n\\def\\hb{\\hbar}\n",
    "notes/a/manuscript.tex": (
        "\\section{The harmonic oscillator}\n"
        "\\label{sec:oscillator}\n"
        "The levels are $E_n = \\hb\\om (n + 1/2)$. % a comment that is stripped\n"
    ),
    "notes/b/manuscript.tex": (
        "\\section{Gauss's law}\n"
        "\\label{sec:gauss}\n"
        "Outside a uniformly charged sphere the field is that of a point charge.\n"
    ),
}


def write_synthetic_pack(root: Path) -> Path:
    for relative, text in SYNTHETIC_FILES.items():
        path = root / SYNTHETIC_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    lines = [
        "pack_id: independent",
        "includes_answer_paper: false",
        f"local_root: {SYNTHETIC_ROOT}",
        "papers:",
    ]
    for doi, inputs in SYNTHETIC_INPUTS.items():
        lines += [f'  - doi: "{doi}"', f'    title: "Synthetic note {doi[-1]}"']
        lines += ["    model_input:", *(f"      - {item}" for item in inputs)]
    pack = root / "config" / "pack_synthetic.yaml"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text("\n".join(lines) + "\n")
    return pack


class SourceMaterialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()

    def methods(self) -> SourceLibrary:
        """A snapshot of the synthetic pack, with the allowlist naming it."""

        for name, value in (
            ("METHOD_PACK_INPUTS", SYNTHETIC_INPUTS),
            ("METHOD_PACK_ROOT", SYNTHETIC_ROOT),
        ):
            patcher = mock.patch.object(source_material, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = self.directory / "repo"
        self.pack = write_synthetic_pack(self.repo)
        return SourceLibrary.from_pack(self.pack, self.repo, self.directory / "sources")

    def test_this_build_allowlists_no_pack(self) -> None:
        pack = write_synthetic_pack(self.directory / "repo")
        with self.assertRaisesRegex(SourceMaterialError, "no method pack"):
            SourceLibrary.from_pack(pack, self.directory / "repo", self.directory / "s")
        snapshot = self.directory / "methods_snapshot"
        snapshot.mkdir()
        (snapshot / "manifest.json").write_text(
            json.dumps({"version": 1, "mode": "methods", "sources": []})
        )
        with self.assertRaisesRegex(SourceMaterialError, "membership"):
            SourceLibrary.load(snapshot)

    async def test_checker_payload_collects_only_with_registered_v11_sources(
        self,
    ) -> None:
        # A structured Checker payload of the shape a tool-enabled run returns,
        # citing a line of the synthetic pack. No paid model is called.
        library = self.methods()
        section = "\\section{Gauss's law}"
        found = library.search(section, limit=1)["matches"]
        self.assertTrue(found)
        literature_id = found[0]["source_id"]
        payload = {
            "verdict": "ok",
            "reason": "Part one is done: the step quotes a line of the note with its source id, digest and line number, factors y**2 - 9 with scientific_compute, and stops before solving.",
            "evidence": [
                {
                    "kind": "task_constraint_quote",
                    "source_id": "task_run_synthetic_0001",
                    "quote": "Part one: quote one line of an allowed note with source_read, giving its source_id, digest and line number; factor y**2 - 9 with scientific_compute; answer continue.",
                },
                {
                    "kind": "ancestor_quote",
                    "source_id": "step_0001",
                    "quote": "scientific_compute factor(y**2 - 9) gave (y - 3)*(y + 3)",
                },
                {
                    "kind": "literature_quote",
                    "source_id": literature_id,
                    "quote": section,
                },
                {
                    "kind": "scope_quote",
                    "source_id": "step_0001",
                    "quote": "Only part one is covered: the factorisation and one read of the note. Solving y**2 = 9 is left to part two.",
                },
            ],
        }
        sources = (
            tuple(
                EvidenceSource(item["kind"], item["source_id"], item["quote"])
                for item in payload["evidence"]
                if item["kind"] != "literature_quote"
            )
            + library.evidence_sources()
        )
        # The decoded quote has one backslash; never repair/normalize it.
        original_quote = payload["evidence"][2]["quote"]
        source = next(
            item
            for item in sources
            if item.source_id == payload["evidence"][2]["source_id"]
        )
        self.assertEqual(
            original_quote, source.text.splitlines()[found[0]["line"] - 1]
        )
        for version, include_literature in (
            ("1.1", True),
            ("1.0", True),
            ("1.1", False),
        ):
            with self.subTest(version=version, include_literature=include_literature):
                selected = (
                    sources
                    if include_literature
                    else tuple(s for s in sources if s.kind != "literature_quote")
                )
                runtime = CodexAppServerRuntime._from_test_client(
                    config=replace(config(), record_version=version),
                    client=ScriptedAppServerClient([TurnScript(json.dumps(payload))]),
                    settings=settings(capability_profile=SOURCE_READING_V1),
                    source_library=library,
                )
                self.addAsyncCleanup(runtime.close)
                request = CheckRequest(
                    run_id=config().run_id,
                    check_id="check-live",
                    task_text=payload["evidence"][0]["quote"],
                    target=StepSnapshot(
                        "step_0001",
                        StepContent(
                            "claim",
                            "why",
                            "source",
                            payload["evidence"][1]["quote"],
                            payload["evidence"][3]["quote"],
                        ),
                    ),
                    transcript=(),
                    evidence_sources=selected,
                )
                invocation = await runtime.start_checker(request)
                if version == "1.1" and include_literature:
                    output = await runtime.collect_checker(invocation)
                    self.assertEqual(output.verdict, "ok")
                    self.assertEqual(output.evidence[2].quote, original_quote)
                    validate_check_output(output, selected)
                    self.assertEqual(json.loads(output.raw_output), payload)
                else:
                    with self.assertRaisesRegex(
                        RuntimeInvocationError, "kind is not allowlisted"
                    ):
                        await runtime.collect_checker(invocation)

    async def test_pack_search_read_macros_and_relocation(self) -> None:
        library = self.methods()
        self.assertEqual(len(library.catalog()), 3)
        self.assertEqual(len({x["doi"] for x in library.catalog()}), 2)
        macros = next(x for x in library.catalog() if x["macros"])
        self.assertTrue(library.read(macros["source_id"], 1, 10)["lines"])
        found = library.search("\\label", limit=2)["matches"]
        self.assertTrue(found)
        read = library.read(found[0]["source_id"], found[0]["line"], 2)
        self.assertIn("\\label", read["lines"][0]["text"])
        self.assertEqual(found[0]["sha256"], read["sha256"])
        shutil.copytree(library.snapshot_dir, self.directory / "copied")
        copy = SourceLibrary.load(self.directory / "copied", library.manifest_sha256)
        self.assertEqual(copy.manifest, library.manifest)
        for tool in library.tools():
            if tool.name == "source_read":
                result = await tool.invoke(
                    {
                        "source_id": found[0]["source_id"],
                        "start_line": found[0]["line"],
                        "line_count": 2,
                    }
                )
                self.assertTrue(result["success"])
                self.assertIn(
                    "\\label",
                    json.loads(result["contentItems"][0]["text"])["lines"][0]["text"],
                )

    async def test_empty_condition_has_no_metadata_and_denies_reads(self) -> None:
        library = SourceLibrary.empty(self.directory / "empty")
        self.assertEqual(library.catalog(), [])
        with self.assertRaisesRegex(SourceMaterialError, "literature_disabled"):
            library.search("optical")
        with self.assertRaisesRegex(SourceMaterialError, "literature_disabled"):
            library.read("anything")
        for tool in library.tools():
            if tool.name == "source_catalog":
                result = await tool.invoke({})
                self.assertEqual(
                    json.loads(result["contentItems"][0]["text"]), {"sources": []}
                )

    def test_tampering_path_traversal_and_symlink_fail_closed(self) -> None:
        library = self.methods()
        first = library.manifest["sources"][0]
        with self.assertRaisesRegex(SourceMaterialError, "unknown source"):
            library.read("../../config/pack_synthetic.yaml")
        with self.assertRaisesRegex(SourceMaterialError, "manifest hash"):
            SourceLibrary.load(library.snapshot_dir, "0" * 64)
        path = library.snapshot_dir / first["snapshot_file"]
        original = path.read_bytes()
        path.write_bytes(original + b"tamper")
        with self.assertRaisesRegex(SourceMaterialError, "content hash"):
            library.read(first["source_id"])
        path.unlink()
        elsewhere = self.directory / "outside.txt"
        elsewhere.write_bytes(original)
        path.symlink_to(elsewhere)
        with self.assertRaisesRegex(SourceMaterialError, "symlinks"):
            library.read(first["source_id"])

    def test_pack_cannot_expand_source_authority(self) -> None:
        self.methods()
        text = self.pack.read_text()
        cases = {
            "input paths": text.replace("notes/b/manuscript.tex", "../../target.tex"),
            "membership": text.split('  - doi: "10.0000/synthetic.b"')[0],
            "source root": text.replace(SYNTHETIC_ROOT, "data/elsewhere"),
            "independent": text.replace("includes_answer_paper: false", ""),
        }
        for message, edited in cases.items():
            with self.subTest(message=message):
                self.pack.write_text(edited)
                with self.assertRaisesRegex(SourceMaterialError, message):
                    SourceLibrary.from_pack(
                        self.pack, self.repo, self.directory / "bad"
                    )

    async def test_transcript_is_current_request_only_and_paginates(self) -> None:
        tool = TranscriptReadTool()
        content = StepContent("claim", "why", "source", "x" * 6000, "scope")
        tool.bind((StepSnapshot("step-1", content),))
        first = await tool.invoke(
            {"step_revision_id": "step-1", "field": "derivation", "offset": 0}
        )
        self.assertEqual(
            json.loads(first["contentItems"][0]["text"])["next_offset"], 5000
        )
        tool.bind(())
        second = await tool.invoke(
            {"step_revision_id": "step-1", "field": "derivation", "offset": 0}
        )
        self.assertFalse(second["success"])

    def test_runtime_same_source_tools_for_writer_and_checker(self) -> None:
        library = self.methods()
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=ScriptedAppServerClient([]),
            settings=settings(capability_profile=SOURCE_READING_V1),
            source_library=library,
        )
        writer = {x["name"] for x in runtime._dynamic_tools_for_role(ModelRole.WRITER)}
        checker = {
            x["name"] for x in runtime._dynamic_tools_for_role(ModelRole.CHECKER)
        }
        self.assertEqual(writer - checker, {"feedback_read"})
        self.assertIn("source_read", checker)
        self.assertIn("scientific_compute", checker)

    async def test_checker_refreshes_transcript_with_ancestor_and_current_target(
        self,
    ) -> None:
        runtime = CodexAppServerRuntime._from_test_client(
            config=config(),
            client=ScriptedAppServerClient(
                [TurnScript({"verdict": "ok", "reason": "verified", "evidence": []})]
            ),
            settings=settings(capability_profile=SOURCE_READING_V1),
            source_library=self.methods(),
        )
        self.addAsyncCleanup(runtime.close)
        content = StepContent("claim", "why", "source", "derivation", "scope")
        ancestor, target = (
            StepSnapshot("ancestor", content),
            StepSnapshot("target", content),
        )
        runtime._transcript_tool.bind((StepSnapshot("stale", content),))
        invocation = await runtime.start_checker(
            CheckRequest(
                run_id=config().run_id,
                check_id="check-new",
                task_text="check target",
                target=target,
                transcript=(),
                full_transcript=(ancestor,),
            )
        )
        for step_id in ("ancestor", "target", "stale"):
            result = await runtime._transcript_tool.invoke(
                {"step_revision_id": step_id, "field": "claim", "offset": 0}
            )
            self.assertEqual(result["success"], step_id != "stale")
        await runtime.collect_checker(invocation)

    async def test_feedback_read_pages_and_cannot_retain_another_request(self) -> None:
        tool = FeedbackReadTool()
        feedback = [
            {
                "check_id": f"check-{n}",
                "step_revision_id": f"step-{n}",
                "verdict": "objection",
                "reason": "r" * 6000,
            }
            for n in range(12)
        ]
        tool.bind(feedback)
        catalog = await tool.invoke({"check_id": "catalog", "offset": 0})
        self.assertEqual(
            json.loads(catalog["contentItems"][0]["text"])["next_offset"], 8
        )
        first = await tool.invoke({"check_id": "check-0", "offset": 0})
        payload = json.loads(first["contentItems"][0]["text"])
        self.assertIsNotNone(payload["next_offset"])
        self.assertLessEqual(len(first["contentItems"][0]["text"]), 7900)
        tool.bind(())
        self.assertFalse(
            (await tool.invoke({"check_id": "check-0", "offset": 0}))["success"]
        )

    async def test_real_source_tool_through_stdio_client_and_durable_audit(
        self,
    ) -> None:
        """Use the real tool and stdio client; only the remote model is a fixture."""
        library = self.methods()
        first = library.search("\\label", limit=1)["matches"][0]
        server_text = (
            ROOT / "src/derivation_runtime/test_app_server_fake.py"
        ).read_text()
        server_text = server_text.replace('"scientific_compute"', '"source_read"')
        server_text = server_text.replace(
            '"operation": "differentiate",\n                                "expression": "x**3",\n                                "variable": "x",',
            f'"source_id": {first["source_id"]!r},\n                                "start_line": {first["line"]},\n                                "line_count": 2,',
        )
        fake = self.directory / "provider_fixture.py"
        fake.write_text(server_text)
        audit = self.directory / "tool_calls.jsonl"
        client = AppServerClient(
            (sys.executable, str(fake), "dynamic_tool_valid"),
            cwd=self.directory,
            timeouts=ClientTimeouts(startup=2.0, request=1.0, close=0.3),
            dynamic_tool_audit_path=audit,
        )
        self.addAsyncCleanup(client.close)
        client.configure_dynamic_tools(library.tools())
        await client.start()
        await client.next_event(timeout=1.0)
        await client.thread_start(
            {
                "model": "gpt-5.5",
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "permissions": "strict_run_workspace",
                "runtimeWorkspaceRoots": [str(self.directory)],
                "cwd": str(self.directory),
                "ephemeral": False,
                "config": {"model_reasoning_effort": "high"},
                "dynamicTools": client.dynamic_tool_specs(["source_read"]),
            }
        )
        await client.turn_start(
            "thread-1", [{"type": "text", "text": "Read the allowed formula"}]
        )
        while (await client.next_event(timeout=1.0)).method != "turn/completed":
            pass
        response = (await client.account_read())["toolResponse"]["result"]
        self.assertTrue(response["success"])
        payload = json.loads(response["contentItems"][0]["text"])
        self.assertIn("\\label", payload["lines"][0]["text"])
        evidence = json.loads(audit.read_text())
        self.assertEqual(evidence["arguments"]["source_id"], first["source_id"])
        self.assertEqual(evidence["result"], response)


if __name__ == "__main__":
    unittest.main()
