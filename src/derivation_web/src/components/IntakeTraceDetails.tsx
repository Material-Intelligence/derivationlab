import type { IntakeDecisionView, IntakeSessionView } from "../types";
import { useLocale } from "../i18n";

type Messages = ReturnType<typeof useLocale>["messages"];

/** `selected` carries no extra meaning; only a ladder strategy is worth a word. */
function strategyLabel(
  m: Messages,
  strategy: NonNullable<IntakeDecisionView["answer"]>["strategy"],
): string[] {
  if (strategy === "simplest_first") return [m.strategySimplestFirstShort];
  if (strategy === "both_routes") return [m.strategyBothRoutesShort];
  return [];
}

function payloadText(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function eventSummary(
  event: IntakeSessionView["conversation"][number],
  submittedDecisionAnswers: string,
): string {
  if (event.kind === "user_message") {
    return payloadText(event.payload, "text") ?? event.kind;
  }
  if (event.kind === "user_answers") {
    return payloadText(event.payload, "message") ?? submittedDecisionAnswers;
  }
  return payloadText(event.payload, "summary") ?? event.kind;
}

export function IntakeTraceDetails({ session }: { session: IntakeSessionView }) {
  const { messages: m } = useLocale();
  return (
    <details className="intake-trace-details">
      <summary>{m.viewIntakeTrace}</summary>
      <div className="intake-trace-grid">
        <section>
          <h3>{m.decisionLog}</h3>
          <ol className="intake-decision-history">
            {session.decisions.map((decision) => {
              const selectedLabels = decision.answer?.selected_option_ids.map(
                (optionId) =>
                  decision.question.options.find(
                    (option) => option.option_id === optionId,
                  )?.label ?? optionId,
              );
              return (
                <li key={`${decision.decision_id}-${decision.revision}`}>
                  <header>
                    <strong>{decision.question.title}</strong>
                    <span>{decision.status} · r{decision.revision}</span>
                  </header>
                  <p>{decision.question.prompt}</p>
                  <ul>
                    {decision.question.options.map((option) => (
                      <li key={option.option_id}>
                        <strong>{option.label}</strong>: {option.impact}
                      </li>
                    ))}
                  </ul>
                  {decision.question.recommendation_reason && (
                    <small>{m.recommended}: {decision.question.recommendation_reason}</small>
                  )}
                  {decision.answer && (
                    <p>
                      {m.recordedAnswer}: {[
                        ...strategyLabel(m, decision.answer.strategy),
                        ...(selectedLabels ?? []),
                        ...(decision.answer.custom_text
                          ? [decision.answer.custom_text]
                          : []),
                      ].join(" · ")}
                    </p>
                  )}
                  {decision.reopen_reason && (
                    <p>{m.reopenReason}: {decision.reopen_reason}</p>
                  )}
                  {decision.source_message_refs.length > 0 && (
                    <p>{m.sourceMessages}: {decision.source_message_refs.join(" · ")}</p>
                  )}
                </li>
              );
            })}
          </ol>
        </section>
        <section>
          <h3>{m.conversationArchive}</h3>
          <ol className="intake-event-history">
            {session.conversation.map((event) => (
              <li key={event.event_id}>
                <header>
                  <strong>{event.kind}</strong>
                  <span>{event.event_id}</span>
                </header>
                <p>{eventSummary(event, m.submittedDecisionAnswers)}</p>
                <details>
                  <summary>{m.rawEventDetails}</summary>
                  <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </details>
  );
}
