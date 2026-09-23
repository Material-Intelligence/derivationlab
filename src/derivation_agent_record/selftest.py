"""Offline acceptance tests for Derivation Agent Record V1.

The suite uses only the Python standard library. It verifies the committed
non-physics golden fixture, then rewrites and re-hashes copies of the event log
to prove that cross-event semantic violations are rejected independently of the
JSON Schema shape checks.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import re
import unittest
from typing import Any, Callable, Iterable, Mapping

from .model import (
    EVENT_TYPES,
    ContractError,
    canonical_json,
    compute_event_sha256,
    load_events,
    sha256_text,
)
from .replay import replay_events

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "derivation_agent_record"
FIXTURES = ROOT / "tests" / "fixtures"
EVENTS_PATH = FIXTURES / "golden_events.jsonl"
CANONICAL_PATH = FIXTURES / "golden_canonical.json"
EVENT_SCHEMA_PATH = ROOT / "docs/spec/derivation_agent_event_v1.schema.json"
CANONICAL_SCHEMA_PATH = ROOT / "docs/spec/derivation_agent_canonical_v1.schema.json"


def fresh_events() -> list[dict[str, Any]]:
    return copy.deepcopy(load_events(EVENTS_PATH))


def rehash(events: list[dict[str, Any]]) -> None:
    previous: str | None = None
    for seq, event in enumerate(events, start=1):
        event["seq"] = seq
        event["prev_event_sha256"] = previous
        event["event_sha256"] = compute_event_sha256(event)
        previous = event["event_sha256"]


def find_event(
    events: Iterable[dict[str, Any]],
    event_type: str,
    *,
    payload_key: str | None = None,
    payload_value: Any = None,
) -> dict[str, Any]:
    matches = []
    for event in events:
        if event["type"] != event_type:
            continue
        if payload_key is not None and event["payload"].get(payload_key) != payload_value:
            continue
        matches.append(event)
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {event_type} event matching {payload_key}={payload_value!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def append_event(
    events: list[dict[str, Any]],
    event_type: str,
    actor: Mapping[str, str],
    payload: Mapping[str, Any],
) -> None:
    seq = len(events) + 1
    event: dict[str, Any] = {
        "schema_version": events[0]["schema_version"],
        "run_id": events[0]["run_id"],
        "seq": seq,
        "event_id": f"evt_test_{seq:04d}",
        "recorded_at": "2026-08-25T23:59:59Z",
        "type": event_type,
        "actor": dict(actor),
        "prev_event_sha256": events[-1]["event_sha256"],
        "payload": copy.deepcopy(dict(payload)),
    }
    event["event_sha256"] = compute_event_sha256(event)
    events.append(event)


class RecordContractTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        # This module ships inside the wheel; the fixtures it reads do not, so on
        # an installed copy there is nothing here to test. That is a skip with a
        # reason, not a FileNotFoundError traceback the reader has to decode.
        if not FIXTURES.is_dir():
            raise unittest.SkipTest(
                f"no fixtures at {FIXTURES}: this suite reads tests/fixtures/, so it runs from a "
                "clone or an unpacked sdist, not from an installed wheel"
            )
        cls.events = fresh_events()
        cls.canonical = replay_events(cls.events).canonical
        cls.committed_canonical = json.loads(CANONICAL_PATH.read_text(encoding="utf-8"))

    def assertContractError(
        self,
        events: list[dict[str, Any]],
        contains: str | None = None,
    ) -> None:
        with self.assertRaises(ContractError) as captured:
            replay_events(events)
        if contains is not None:
            self.assertIn(contains, str(captured.exception))

    def semantic_mutation(
        self,
        mutate: Callable[[list[dict[str, Any]]], None],
    ) -> list[dict[str, Any]]:
        events = fresh_events()
        mutate(events)
        rehash(events)
        return events

    def test_01_golden_replay_matches_committed_canonical(self) -> None:
        self.assertEqual(self.canonical, self.committed_canonical)
        self.assertEqual(self.canonical["event_log"]["event_count"], 49)
        self.assertEqual(
            self.canonical["summary"],
            {
                "branch_count": 5,
                "step_revision_count": 4,
                "model_call_count": 9,
                "check_count": 4,
                "candidate_count": 2,
                "judgement_count": 1,
                "selection_count": 1,
                "candidate_status_counts": {
                    "provisional": 0,
                    "blocked": 0,
                    "rejected": 1,
                    "eligible": 1,
                },
                "selected_candidate_id": "cand_human_edit",
            },
        )

    def test_02_late_hard_defect_revokes_only_old_candidate(self) -> None:
        candidates = {item["candidate_id"]: item for item in self.canonical["candidates"]}
        self.assertEqual(candidates["cand_root"]["status"], "rejected")
        self.assertEqual(candidates["cand_human_edit"]["status"], "eligible")
        self.assertIn("step_root_2_r1", candidates["cand_root"]["transcript_step_revision_ids"])
        self.assertNotIn("step_root_2_r1", candidates["cand_human_edit"]["transcript_step_revision_ids"])
        self.assertIn("step_edit_2_r2", candidates["cand_human_edit"]["transcript_step_revision_ids"])

    def test_03_human_edit_forks_without_polluting_old_route(self) -> None:
        branches = {item["branch_id"]: item for item in self.canonical["branches"]}
        self.assertEqual(branches["br_root"]["provenance"]["content_class"], "model_only")
        self.assertFalse(branches["br_root"]["provenance"]["human_touched"])
        self.assertEqual(branches["br_human_direction"]["provenance"]["content_class"], "human_steered")
        self.assertEqual(branches["br_human_edit"]["provenance"]["content_class"], "human_edited")
        self.assertEqual(branches["br_human_edit"]["fork_mode"], "replace")
        self.assertEqual(branches["br_human_edit"]["inherited_step_revision_ids"], ["step_root_1_r1"])

    def test_04_instrument_failure_is_not_a_scientific_defect(self) -> None:
        branches = {item["branch_id"]: item for item in self.canonical["branches"]}
        calls = {item["model_call_id"]: item for item in self.canonical["model_calls"]}
        checks = {item["check_id"]: item for item in self.canonical["checks"]}
        self.assertEqual(branches["br_instrument"]["status"], "parked")
        self.assertEqual(calls["call_writer_zero_body"]["state"], "failed")
        self.assertEqual(calls["call_writer_zero_body"]["body_chars"], 0)
        self.assertEqual(calls["call_writer_zero_body"]["failure"]["kind"], "zero_body_instrument_failure")
        self.assertNotIn("step_revision_id", calls["call_writer_zero_body"])
        self.assertNotIn("call_writer_zero_body", {check["checker_call_id"] for check in checks.values()})

    def test_05_selection_is_hash_bound_to_eligible_passed_candidate(self) -> None:
        selection = self.canonical["selections"][0]
        candidate = next(item for item in self.canonical["candidates"] if item["candidate_id"] == selection["candidate_id"])
        judgement = next(item for item in self.canonical["judgements"] if item["judgement_id"] == selection["judgement_id"])
        self.assertEqual(candidate["status"], "eligible")
        self.assertEqual(judgement["verdict"], "pass")
        self.assertEqual(selection["candidate_transcript_sha256"], candidate["transcript_sha256"])
        self.assertTrue(selection["provenance"]["human_touched"])
        self.assertEqual(selection["provenance"]["content_class"], "human_edited")

    def test_06_run_configuration_is_explicit_and_has_no_hidden_branch_cap(self) -> None:
        configuration = self.canonical["run"]["configuration"]
        self.assertEqual(configuration["granularity"], "one_task")
        self.assertIsNone(configuration["max_active_branches"])
        self.assertEqual(set(configuration["models"]), {"writer", "checker", "judge"})
        for role in ("writer", "checker", "judge"):
            self.assertEqual(set(configuration["models"][role]), {"provider", "model", "effort"})

    def test_07_event_schema_covers_exact_runtime_event_set(self) -> None:
        schema = json.loads(EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))
        schema_types = {branch["properties"]["type"]["const"] for branch in schema["oneOf"]}
        self.assertEqual(schema_types, EVENT_TYPES)
        self.assertTrue(all(branch.get("additionalProperties") is False for branch in schema["oneOf"]))

    def test_08_canonical_schema_covers_exact_top_level_state(self) -> None:
        schema = json.loads(CANONICAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.canonical))
        self.assertEqual(set(schema["properties"]), set(self.canonical))

    def test_09_event_body_tamper_breaks_event_hash(self) -> None:
        events = fresh_events()
        event = find_event(events, "candidate_declared", payload_key="candidate_id", payload_value="cand_root")
        event["payload"]["reason"] = "tampered after recording"
        self.assertContractError(events, "event_sha256 mismatch")

    def test_10_prev_event_tamper_breaks_hash_chain(self) -> None:
        events = fresh_events()
        events[10]["prev_event_sha256"] = "0" * 64
        self.assertContractError(events, "prev_event_sha256 mismatch")

    def test_11_sequence_gap_is_rejected(self) -> None:
        events = fresh_events()
        events[8]["seq"] = 99
        self.assertContractError(events, "event sequence must be contiguous")

    def test_12_duplicate_step_revision_is_rejected(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "step_revision_sealed", payload_key="step_revision_id", payload_value="step_edit_2_r2")
            event["payload"]["step_revision_id"] = "step_root_2_r1"

        self.assertContractError(self.semantic_mutation(mutate), "duplicate step_revision_id")

    def test_13_human_revision_must_use_replace_fork(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "branch_created", payload_key="branch_id", payload_value="br_human_edit")
            event["payload"]["fork_mode"] = "after"

        self.assertContractError(self.semantic_mutation(mutate), "exact parent prefix")

    def test_14_check_completion_cannot_drift_to_another_hash(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "check_completed", payload_key="check_id", payload_value="check_edit_2")
            event["payload"]["target_output_sha256"] = sha256_text("different revision")

        self.assertContractError(self.semantic_mutation(mutate), "check completion hash mismatch")

    def test_15_hard_defect_requires_quoted_evidence(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "check_completed", payload_key="check_id", payload_value="check_root_2_late")
            event["payload"]["evidence"] = []

        self.assertContractError(self.semantic_mutation(mutate), "hard_defect requires quoted evidence")

    def test_16_five_column_step_cannot_omit_scope(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "step_revision_sealed", payload_key="step_revision_id", payload_value="step_edit_3_r1")
            event["payload"]["content"].pop("scope")
            event["payload"]["output_sha256"] = sha256_text(canonical_json(event["payload"]["content"]))
            call = find_event(events, "model_call_finished", payload_key="model_call_id", payload_value="call_writer_edit_3")
            call["payload"]["output_text"] = canonical_json(event["payload"]["content"])
            call["payload"]["output_sha256"] = event["payload"]["output_sha256"]
            call["payload"]["body_chars"] = len(call["payload"]["output_text"])

        self.assertContractError(self.semantic_mutation(mutate), "step content")

    def test_17_candidate_transcript_hash_is_immutable(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "candidate_declared", payload_key="candidate_id", payload_value="cand_human_edit")
            event["payload"]["transcript_sha256"] = sha256_text("wrong transcript")

        self.assertContractError(self.semantic_mutation(mutate), "candidate transcript hash mismatch")

    def test_18_candidate_cannot_omit_required_check(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "candidate_declared", payload_key="candidate_id", payload_value="cand_human_edit")
            event["payload"]["required_check_ids"].remove("check_edit_3")

        self.assertContractError(self.semantic_mutation(mutate), "candidate omitted or added required check")

    def test_19_required_check_cannot_be_added_after_candidate_snapshot(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "branch_created", payload_key="branch_id", payload_value="br_model_alt")
            event["type"] = "check_requested"
            event["actor"] = {"kind": "system", "id": "record-runtime"}
            step = find_event(events, "step_revision_sealed", payload_key="step_revision_id", payload_value="step_root_1_r1")
            event["payload"] = {
                "check_id": "check_added_too_late",
                "target_step_revision_id": "step_root_1_r1",
                "target_output_sha256": step["payload"]["output_sha256"],
                "required_for_candidate": True,
                "reason": "This mutation attempts to alter a frozen candidate checklist.",
            }

        self.assertContractError(self.semantic_mutation(mutate), "cannot add required check after candidate snapshot")

    def test_20_rejected_candidate_cannot_be_selected(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            selection = find_event(events, "selection_recorded", payload_key="selection_id", payload_value="selection_final")
            candidate = find_event(events, "candidate_declared", payload_key="candidate_id", payload_value="cand_root")
            selection["payload"]["candidate_id"] = "cand_root"
            selection["payload"]["candidate_transcript_sha256"] = candidate["payload"]["transcript_sha256"]

        self.assertContractError(self.semantic_mutation(mutate), "only eligible candidate may be selected")

    def test_21_near_pass_judgement_cannot_be_selected(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "judgement_completed", payload_key="judgement_id", payload_value="judge_edit_final")
            event["payload"]["verdict"] = "near_pass"

        self.assertContractError(self.semantic_mutation(mutate), "selection requires completed pass judgement")

    def test_22_in_flight_call_at_end_is_rejected(self) -> None:
        events = fresh_events()
        step = find_event(events, "step_revision_sealed", payload_key="step_revision_id", payload_value="step_edit_3_r1")
        append_event(
            events,
            "check_requested",
            {"kind": "system", "id": "record-runtime"},
            {
                "check_id": "check_left_in_flight",
                "target_step_revision_id": "step_edit_3_r1",
                "target_output_sha256": step["payload"]["output_sha256"],
                "required_for_candidate": False,
                "reason": "Non-required audit check used to exercise an in-flight call at end of record.",
            },
        )
        append_event(
            events,
            "model_call_started",
            {"kind": "checker", "id": "mock:checker"},
            {
                "model_call_id": "call_left_in_flight",
                "role": "checker",
                "provider": "mock",
                "model": "mock:checker",
                "effort": "fixture",
                "target": {"check_id": "check_left_in_flight"},
                "prompt_sha256": sha256_text("left in flight"),
            },
        )
        self.assertContractError(events, "still in flight at end of record")

    def test_23_terminal_call_actor_must_match_call_role(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "model_call_finished", payload_key="model_call_id", payload_value="call_writer_s1")
            event["actor"] = {"kind": "human", "id": "fixture-reviewer"}

        self.assertContractError(self.semantic_mutation(mutate), "actor kind does not match call role")

    def test_24_branch_check_and_judgement_actor_roles_are_enforced(self) -> None:
        mutations: list[tuple[Callable[[list[dict[str, Any]]], None], str]] = []

        def wrong_branch(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "branch_created", payload_key="branch_id", payload_value="br_model_alt")
            event["actor"] = {"kind": "system", "id": "record-runtime"}

        mutations.append((wrong_branch, "model_alternative branch must be created by model"))

        def wrong_check_request(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "check_requested", payload_key="check_id", payload_value="check_edit_2")
            event["actor"] = {"kind": "model", "id": "mock:writer"}

        mutations.append((wrong_check_request, "check_requested actor must be system"))

        def wrong_check_result(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "check_completed", payload_key="check_id", payload_value="check_edit_2")
            event["actor"] = {"kind": "system", "id": "record-runtime"}

        mutations.append((wrong_check_result, "check_completed actor must be checker"))

        def wrong_judgement_result(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "judgement_completed", payload_key="judgement_id", payload_value="judge_edit_final")
            event["actor"] = {"kind": "system", "id": "record-runtime"}

        mutations.append((wrong_judgement_result, "judgement_completed actor must be judge"))

        for mutate, message in mutations:
            with self.subTest(message=message):
                self.assertContractError(self.semantic_mutation(mutate), message)

    def test_25_model_call_must_match_frozen_run_configuration(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "model_call_started", payload_key="model_call_id", payload_value="call_writer_s1")
            event["payload"]["model"] = "mock:other-writer"

        self.assertContractError(self.semantic_mutation(mutate), "differs from run configuration")

    def test_26_human_action_target_shape_is_action_specific(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "human_action_recorded", payload_key="action_id", payload_value="act_direction")
            event["payload"]["target"] = {"branch_id": "br_root", "candidate_id": "cand_root"}

        self.assertContractError(self.semantic_mutation(mutate), "human action set_direction target")

    def test_27_hard_defect_evidence_must_come_from_checked_lineage(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = find_event(events, "check_completed", payload_key="check_id", payload_value="check_root_2_late")
            event["payload"]["evidence"] = [
                {
                    "kind": "ancestor_quote",
                    "source_id": "step_edit_2_r2",
                    "quote": "The invariant Q remains positive at the second checkpoint.",
                }
            ]

        self.assertContractError(self.semantic_mutation(mutate), "outside the checked transcript")

    def test_28_reference_path_requires_explicit_run_permission(self) -> None:
        def mutate(events: list[dict[str, Any]]) -> None:
            event = events[0]
            event["payload"]["input_policy"]["allowed_paths"] = ["reference/oldrepo/example.txt"]

        self.assertContractError(self.semantic_mutation(mutate), "reference path listed")

    def test_29_committed_view_is_the_one_the_renderer_produces(self) -> None:
        from .render import render_html

        self.assertEqual(
            render_html(self.canonical, self.events),
            (FIXTURES / "golden_view.html").read_text(encoding="utf-8"),
        )

    def test_30_package_has_no_provider_or_network_dependency(self) -> None:
        forbidden_import_roots = {"anthropic", "http", "openai", "requests", "socket", "urllib"}
        source_paths = sorted(PACKAGE.glob("*.py"))
        for path in source_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertTrue(roots.isdisjoint(forbidden_import_roots), f"{path}: forbidden import {roots}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".", 1)[0], forbidden_import_roots, str(path))
            if path.name != "selftest.py":
                self.assertNotIn("reference/", path.read_text(encoding="utf-8"), str(path))

    def test_31_new_artifacts_have_no_secret_shaped_literal(self) -> None:
        patterns = {
            "openai": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
            "anthropic": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
            "github": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
            "aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        }
        roots = [ROOT / "docs/spec", PACKAGE, FIXTURES, ROOT / "examples"]
        findings: list[str] = []
        for artifact_root in roots:
            for path in artifact_root.rglob("*"):
                if not path.is_file() or path.name == "selftest.py" or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for name, pattern in patterns.items():
                    if pattern.search(text):
                        findings.append(f"{name}: {path.relative_to(ROOT)}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
