from __future__ import annotations

import hashlib
import json
import unittest

from derivation_app.intake_handoff import build_intake_handoff
from derivation_app.intake_session import (
    ConvergenceState,
    ConversationEvent,
    DecisionClass,
    DeclaredDefault,
    IntakeSession,
    IntakeSessionStatus,
    LadderRung,
    ProblemSection,
    ProblemSpecification,
    ProblemSpecificationStatus,
    ThreadGeneration,
    ThreadGenerationStatus,
)


class IntakeHandoffTests(unittest.TestCase):
    def test_confirmed_session_exports_three_layers_and_hash_manifest(self) -> None:
        names = (
            "purpose",
            "scientific_target",
            "givens_and_starting_point",
            "notation_and_conventions",
            "assumptions_and_regime",
            "scope_and_non_goals",
            "required_output",
            "validation_criteria",
            "agent_discretion",
        )
        session = IntakeSession(
            session_id="intake_session_1",
            revision=3,
            status=IntakeSessionStatus.CONFIRMED,
            problem_specifications=(
                ProblemSpecification(
                    version=1,
                    status=ProblemSpecificationStatus.CONFIRMED,
                    sections=tuple(
                        ProblemSection(name=name, content=f"Resolved {name}")
                        for name in names
                    ),
                    critical_message_refs=("event_1",),
                ),
            ),
            decisions=(),
            frontier=(),
            thread_generations=(
                ThreadGeneration(
                    generation=1,
                    status=ThreadGenerationStatus.CLOSED,
                    app_server_thread_id="thread-intake",
                    prompt_version="intake-grill-v2",
                ),
            ),
        )
        events = (
            ConversationEvent("event_1", "user_message", {"text": "derive"}),
            ConversationEvent(
                "event_2",
                "session_confirmed",
                {"problem_specification_version": 1},
            ),
        )

        bundle = build_intake_handoff(session, events, run_id="run_1")

        self.assertEqual(
            set(bundle.files),
            {
                "problem_specification.json",
                "decision_log.json",
                "conversation.jsonl",
                "handoff_manifest.json",
            },
        )
        for name, expected in bundle.manifest["files"].items():
            self.assertEqual(hashlib.sha256(bundle.files[name]).hexdigest(), expected)
        manifest = json.loads(bundle.files["handoff_manifest.json"])
        self.assertEqual(manifest["intake_session_id"], "intake_session_1")
        self.assertEqual(manifest["schema_version"], "intake-handoff-v3")
        self.assertEqual(
            manifest["problem_specification"]["sha256"],
            manifest["files"]["problem_specification.json"],
        )
        self.assertEqual(
            manifest["decision_log"],
            {
                "revision": 3,
                "latest_decision_revisions": {},
                "sha256": manifest["files"]["decision_log.json"],
            },
        )
        terminal = manifest["conversation_archive"]["terminal_event"]
        self.assertEqual(terminal["event_id"], "event_2")
        self.assertEqual(terminal["kind"], "session_confirmed")
        self.assertEqual(
            manifest["conversation_archive"]["confirmation_event_id"],
            "event_2",
        )
        self.assertEqual(
            terminal["sha256"],
            hashlib.sha256(
                bundle.files["conversation.jsonl"].splitlines(keepends=True)[-1]
            ).hexdigest(),
        )
        self.assertEqual(
            manifest["thread_generations"][0]["thread_id"], "thread-intake"
        )
        self.assertEqual(bundle.files["conversation.jsonl"].count(b"\n"), 2)

    def test_unconfirmed_session_cannot_export(self) -> None:
        session = IntakeSession(
            session_id="intake_session_1",
            revision=0,
            status=IntakeSessionStatus.ACTIVE,
            problem_specifications=(),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        with self.assertRaisesRegex(ValueError, "confirmed"):
            build_intake_handoff(session, (), run_id="run_1")

    def test_confirmed_handoff_requires_terminal_confirmation_event(self) -> None:
        names = (
            "purpose",
            "scientific_target",
            "givens_and_starting_point",
            "notation_and_conventions",
            "assumptions_and_regime",
            "scope_and_non_goals",
            "required_output",
            "validation_criteria",
            "agent_discretion",
        )
        session = IntakeSession(
            session_id="intake_session_1",
            revision=2,
            status=IntakeSessionStatus.CONFIRMED,
            problem_specifications=(
                ProblemSpecification(
                    version=1,
                    status=ProblemSpecificationStatus.CONFIRMED,
                    sections=tuple(
                        ProblemSection(name=name, content=f"Resolved {name}")
                        for name in names
                    ),
                ),
            ),
            decisions=(),
            frontier=(),
            thread_generations=(),
        )
        with self.assertRaisesRegex(ValueError, "terminal confirmation"):
            build_intake_handoff(
                session,
                (ConversationEvent("event_1", "user_message", {"text": "derive"}),),
                run_id="run_1",
            )


V2_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "intake_session_id",
        "intake_revision",
        "run_id",
        "problem_specification_version",
        "problem_specification",
        "decision_log",
        "conversation_archive",
        "thread_generations",
        "files",
    }
)


class IntakeHandoffLadderTests(unittest.TestCase):
    """v3 records the ladder and the convergence budget without dropping v2."""

    def _confirmed_session(self) -> IntakeSession:
        names = (
            "purpose",
            "scientific_target",
            "givens_and_starting_point",
            "notation_and_conventions",
            "assumptions_and_regime",
            "scope_and_non_goals",
            "required_output",
            "validation_criteria",
            "agent_discretion",
        )
        return IntakeSession(
            session_id="intake_session_ladder",
            revision=5,
            status=IntakeSessionStatus.CONFIRMED,
            problem_specifications=(
                ProblemSpecification(
                    version=1,
                    status=ProblemSpecificationStatus.CONFIRMED,
                    sections=tuple(
                        ProblemSection(name=name, content=f"Resolved {name}")
                        for name in names
                    ),
                    critical_message_refs=("event_1",),
                    declared_defaults=(
                        DeclaredDefault(
                            default_id="unit-system",
                            decision_class=DecisionClass.CONVENTION,
                            title="Unit system",
                            statement="Work in SI units throughout.",
                            rationale="Interchangeable with Gaussian units.",
                            alternatives=("Gaussian units",),
                        ),
                    ),
                    refinement_ladder=(
                        LadderRung(
                            rung=0,
                            name="Textbook-simplest baseline",
                            relaxes="Nothing; this is the comparable baseline.",
                            default_ids=("unit-system",),
                        ),
                        LadderRung(
                            rung=1,
                            name="Required deliverable",
                            relaxes="Replaces the ideal line shape with broadening.",
                        ),
                    ),
                ),
            ),
            decisions=(),
            frontier=(),
            thread_generations=(),
            convergence=ConvergenceState(
                rounds=3,
                audit_rejections=2,
                reason="max_audit_rejections",
                finalized_by_user=True,
            ),
        )

    def test_manifest_v3_only_adds_to_v2(self) -> None:
        events = (
            ConversationEvent("event_1", "user_message", {"text": "derive"}),
            ConversationEvent(
                "event_2",
                "session_confirmed",
                {"problem_specification_version": 1},
            ),
        )

        bundle = build_intake_handoff(
            self._confirmed_session(), events, run_id="run_ladder"
        )
        manifest = json.loads(bundle.files["handoff_manifest.json"])

        assert V2_MANIFEST_KEYS < set(manifest)
        assert set(manifest) - V2_MANIFEST_KEYS == {"convergence"}
        assert manifest["schema_version"] == "intake-handoff-v3"
        assert manifest["convergence"] == {
            "rounds": 3,
            "audit_rejections": 2,
            "reason": "max_audit_rejections",
            "finalized_by_user": True,
        }
        assert manifest["problem_specification"]["declared_default_ids"] == [
            "unit-system"
        ]
        assert manifest["problem_specification"]["refinement_ladder_rungs"] == 2
        assert (
            manifest["problem_specification"]["sha256"]
            == (manifest["files"]["problem_specification.json"])
        )

        specification = json.loads(bundle.files["problem_specification.json"])
        assert [item["rung"] for item in specification["refinement_ladder"]] == [0, 1]
        assert specification["declared_defaults"][0]["decision_class"] == "convention"


if __name__ == "__main__":
    unittest.main()
