import type { FrozenProblemInput, IntakeSessionView } from "../types";

/**
 * Consume the backend-owned canonical Writer/Record projection.
 */
export function freezeIntakeSession(
  session: IntakeSessionView,
): FrozenProblemInput {
  if (session.status !== "confirmed") {
    throw new Error("IntakeSession must be confirmed before Run creation");
  }
  if (!session.frozen_problem) {
    throw new Error("Confirmed IntakeSession has no canonical frozen problem");
  }
  return structuredClone(session.frozen_problem);
}
