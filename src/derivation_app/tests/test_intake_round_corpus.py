"""Regression corpus of real Intake round payloads captured from live models.

Every file in ``data/intake_rounds`` is a structured-output payload a model
actually returned through the codex app-server, wrapped with the round mode the
host asked for. The model's text is kept; the session file name, the session
and event ids and the timestamps were replaced by stand-ins (``intake_corpus_*``,
``event_corpus_*``, ``rollout-synthetic-*``), consistently within each session. The contract this file asserts is deliberately narrow:
a payload a real model produced must never destroy the round. Bookkeeping
violations (a ladder rung citing an undeclared default, a declared default whose
class the round is not allowed to settle) are the parser's to repair and record,
not to reject.

The corpus is evidence, so it grows by capture, never by invention. Files whose
``source`` says ``synthetic`` are hand-written variants and are labelled as such
in the file itself.

The published corpus keeps only captured rounds on textbook problems. None of
them triggers a repair, so here they guard the plain parse path; the repair
paths are exercised by the hand-built payloads in
``test_intake_contract_hardening.py``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from derivation_app.app_server_intake_session import (
    IntakeRoundMode,
    parse_intake_round_proposal,
)
from derivation_runtime.app_server_structured_turn import StructuredTurnResult

CORPUS = Path(__file__).resolve().parent / "data" / "intake_rounds"

REQUIRED_PAYLOAD_KEYS = frozenset(
    {
        "public_summary",
        "specification_sections",
        "critical_message_refs",
        "questions",
        "declared_defaults",
        "refinement_ladder",
        "reopen_requests",
        "candidate_ready",
    }
)


def _corpus_files() -> list[Path]:
    if not CORPUS.is_dir():
        return []
    return sorted(CORPUS.glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class IntakeRoundCorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = _corpus_files()
        # The published corpus has seven rounds; an export that lost the
        # directory must fail here, not pass by skipping.
        self.assertGreaterEqual(len(self.files), 7, f"captured rounds missing in {CORPUS}")

    def test_every_wrapper_declares_its_round(self) -> None:
        for path in self.files:
            with self.subTest(round=path.name):
                doc = _load(path)
                self.assertIn(doc.get("mode"), {"advance", "finalize"})
                self.assertTrue(str(doc.get("source", "")).strip())
                self.assertTrue(str(doc.get("captured", "")).strip())
                payload = doc.get("payload")
                self.assertIsInstance(payload, dict)
                self.assertEqual(set(payload), REQUIRED_PAYLOAD_KEYS)

    def test_every_captured_round_parses(self) -> None:
        """A real model payload never destroys the round it belongs to.

        The captured rounds guard the plain parse path; none of them needs a
        repair. The repair paths (a problem-class default declared in an
        advance round, a ladder citing an undeclared default) are covered by
        the hand-built payloads in ``test_intake_contract_hardening.py``.
        """

        for path in self.files:
            with self.subTest(round=path.name):
                doc = _load(path)
                payload = doc["payload"]
                result = StructuredTurnResult(
                    payload=payload,
                    thread_id="thread_intake_corpus",
                    turn_id="turn_intake_corpus",
                    created_thread=False,
                )
                proposal = parse_intake_round_proposal(
                    payload,
                    specification_version=1,
                    supersedes_version=None,
                    result=result,
                    mode=IntakeRoundMode(doc["mode"]),
                    inherited_declared_defaults=(),
                    session_id=doc.get("provenance", {}).get("intake_session_id"),
                )
                self.assertTrue(proposal.public_summary)
                # candidate_ready is deliberately not asserted: a repair may
                # hand a decision back to the user and downgrade the round.

    def test_repaired_ladder_never_cites_an_undeclared_default(self) -> None:
        """Whatever the repair does, the parsed ladder must stay self-consistent."""

        for path in self.files:
            with self.subTest(round=path.name):
                doc = _load(path)
                payload = doc["payload"]
                result = StructuredTurnResult(
                    payload=payload,
                    thread_id="thread_intake_corpus",
                    turn_id="turn_intake_corpus",
                    created_thread=False,
                )
                try:
                    proposal = parse_intake_round_proposal(
                        payload,
                        specification_version=1,
                        supersedes_version=None,
                        result=result,
                        mode=IntakeRoundMode(doc["mode"]),
                        inherited_declared_defaults=(),
                        session_id=doc.get("provenance", {}).get("intake_session_id"),
                    )
                except ValueError:
                    self.skipTest("round does not parse yet; see test_every_captured_round_parses")
                declared = {
                    item.default_id
                    for item in proposal.problem_specification.declared_defaults
                }
                cited = {
                    default_id
                    for rung in proposal.problem_specification.refinement_ladder
                    for default_id in rung.default_ids
                }
                self.assertEqual(cited - declared, set())


if __name__ == "__main__":
    unittest.main()
