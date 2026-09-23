import { describe, expect, it } from "vitest";
import type { IntakeSessionView } from "../types";
import { freezeIntakeSession } from "./problemProtocol";

const names = [
  "purpose",
  "scientific_target",
  "givens_and_starting_point",
  "notation_and_conventions",
  "assumptions_and_regime",
  "scope_and_non_goals",
  "required_output",
  "validation_criteria",
  "agent_discretion",
] as const;

function session(status: IntakeSessionView["status"] = "confirmed"): IntakeSessionView {
  return {
    session_id: "intake-projection",
    revision: 3,
    status,
    model: "gpt-5.4",
    effort: "low",
    problem_specifications: [
      {
        version: 3,
        status: status === "confirmed" ? "confirmed" : "candidate_ready",
        supersedes_version: 2,
        critical_message_refs: ["event-1"],
        sections: names.map((name) => ({
          name,
          content: `Content for ${name}`,
          not_applicable_reason: null,
        })),
      },
    ],
    decisions: [
      {
        decision_id: "scope",
        semantic_key: "scope",
        status: "superseded",
        revision: 1,
        supersedes_revision: null,
        reopen_reason: null,
        source_message_refs: ["event-old"],
        question: {
          question_id: "scope-old",
          decision_id: "scope",
          semantic_key: "scope",
          title: "Output scope",
          prompt: "Choose the old output scope.",
          why_needed: "It changes the result.",
          why_it_matters: "It changes which quantity the derivation delivers.",
          answer_mode: "single_choice",
          options: [{ option_id: "old", label: "Old", impact: "Old impact" }],
          recommended_option_ids: ["old"],
          recommendation_reason: "Old reason",
          allow_custom: true,
          depends_on: [],
          blocking: true,
        },
        answer: {
          selected_option_ids: ["old"],
          custom_text: "SUPERSEDED_SENTINEL",
          source_message_refs: ["event-old-answer"],
        },
      },
      {
        decision_id: "scope",
        semantic_key: "scope",
        status: "resolved",
        revision: 2,
        supersedes_revision: 1,
        reopen_reason: "The requested output changed.",
        source_message_refs: ["event-new"],
        question: {
          question_id: "scope-new",
          decision_id: "scope",
          semantic_key: "scope",
          title: "Output scope",
          prompt: "Choose the final output scope.",
          why_needed: "It changes the result.",
          why_it_matters: "It changes which quantity the derivation delivers.",
          answer_mode: "single_choice",
          options: [{ option_id: "full", label: "Full", impact: "Keep all terms" }],
          recommended_option_ids: ["full"],
          recommendation_reason: "Matches the target",
          allow_custom: true,
          depends_on: [],
          blocking: true,
        },
        answer: {
          selected_option_ids: ["full"],
          custom_text: "ACTIVE_DECISION_SENTINEL",
          source_message_refs: ["event-new-answer"],
        },
      },
    ],
    frontier: [],
    thread_generations: [],
    conversation: [],
    frozen_problem: status === "confirmed"
      ? {
          problem_id: "intake-projection",
          version: 3,
          supersedes_version: 2,
          objective: "Backend-owned objective",
          givens: ["Backend-owned givens"],
          assumptions: ["Backend-owned assumptions"],
          accepted_decisions: [
            "Output scope [scope]: ACTIVE_DECISION_SENTINEL",
          ],
          scope: "Backend-owned scope",
          deliverable: "Backend-owned deliverable",
          allowed_tools: ["scientific_compute"],
          allowed_references: [],
          success_criteria: ["Backend-owned validation"],
          source_pack: null,
          confirmed_by_user: true,
        }
      : null,
  };
}

describe("backend-owned Intake V2 frozen problem", () => {
  it("consumes the canonical backend projection without recomputing it", () => {
    const frozen = freezeIntakeSession(session());

    expect(frozen.problem_id).toBe("intake-projection");
    expect(frozen.objective).toBe("Backend-owned objective");
    expect(frozen.givens).toEqual(["Backend-owned givens"]);
    expect(frozen.accepted_decisions).toHaveLength(1);
    expect(frozen.accepted_decisions?.[0]).toContain("ACTIVE_DECISION_SENTINEL");
    expect(frozen.objective).not.toContain("Content for purpose");
  });

  it("refuses to create a Run from an unconfirmed candidate", () => {
    expect(() => freezeIntakeSession(session("candidate_ready"))).toThrow(
      "must be confirmed",
    );
  });
});
