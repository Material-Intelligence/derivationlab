import type {
  AnswerStrategy,
  CreateIntakeSessionRequest,
  CreateRunDefaultsView,
  CreateRunRequest,
  FinalizeIntakeSessionRequest,
  IntakeConvergenceView,
  IntakeDecisionClass,
  IntakeProblemSpecificationView,
  IntakeQuestion,
  IntakeSessionView,
  RunConfig,
  SubmitIntakeRoundRequest,
} from "../types";
import {
  MAX_FRONTIER_ROUNDS,
  useProblemIntake,
  type RoleName,
} from "../hooks/useProblemIntake";
import { useLocale } from "../i18n";
import { IntakeTraceDetails } from "./IntakeTraceDetails";

export interface RunStartFormProps {
  loading: boolean;
  defaults: CreateRunDefaultsView;
  initialSessionId?: string;
  onCreate: (request: CreateIntakeSessionRequest) => Promise<IntakeSessionView>;
  onLoad: (sessionId: string) => Promise<IntakeSessionView>;
  onListActive: () => Promise<IntakeSessionView[]>;
  onRound: (
    sessionId: string,
    request: SubmitIntakeRoundRequest,
  ) => Promise<IntakeSessionView>;
  onFinalize: (
    sessionId: string,
    request: FinalizeIntakeSessionRequest,
  ) => Promise<IntakeSessionView>;
  onConfirm: (sessionId: string, baseRevision: number) => Promise<IntakeSessionView>;
  onCancel: (sessionId: string, baseRevision: number) => Promise<IntakeSessionView>;
  onSessionChange: (sessionId?: string) => void;
  onSubmit: (request: CreateRunRequest) => Promise<void> | void;
}

type Messages = ReturnType<typeof useLocale>["messages"];

const sectionLabel: Record<string, string> = {
  purpose: "Purpose",
  scientific_target: "Scientific target",
  givens_and_starting_point: "Givens and starting point",
  notation_and_conventions: "Notation and conventions",
  assumptions_and_regime: "Assumptions and regime",
  scope_and_non_goals: "Scope and non-goals",
  required_output: "Required output",
  validation_criteria: "Validation criteria",
  agent_discretion: "Agent discretion",
};

const declaredDefaultClasses: IntakeDecisionClass[] = [
  "problem",
  "convention",
  "approximation_level",
];

function decisionClassLabel(m: Messages, decisionClass: IntakeDecisionClass): string {
  if (decisionClass === "problem") return m.defaultsProblemClass;
  if (decisionClass === "convention") return m.defaultsConventionClass;
  return m.defaultsApproximationClass;
}

/** `reason` is a server constant; anything unexpected still gets plain words. */
function convergenceReasonCopy(m: Messages, reason: string | null | undefined): string {
  if (reason === "max_frontier_rounds") return m.convergenceReasonRounds;
  if (reason === "max_audit_rejections") return m.convergenceReasonAudits;
  return m.convergenceReasonUnknown;
}

function SpecificationSummary({
  specification,
}: {
  specification: IntakeProblemSpecificationView;
}) {
  const { messages: m } = useLocale();
  return (
    <details className="draft-summary">
      <summary>{m.viewFullPrompt}</summary>
      <div>
        {specification.sections.map((section) => (
          <section key={section.name}>
            <h3>{sectionLabel[section.name] ?? section.name}</h3>
            <p>
              {section.content ??
                `${m.explicitlyNone}: ${section.not_applicable_reason ?? ""}`}
            </p>
          </section>
        ))}
      </div>
    </details>
  );
}

/**
 * Every convention and approximation the agent settled by itself, grouped by
 * decision class. A problem-class default only exists after a finalize round,
 * so it is the agent deciding the question itself and is emphasized as such.
 */
function DeclaredDefaultsPanel({
  specification,
  open,
}: {
  specification: IntakeProblemSpecificationView;
  open: boolean;
}) {
  const { messages: m } = useLocale();
  const defaults = specification.declared_defaults ?? [];
  return (
    <details className="declared-defaults" open={open}>
      <summary>
        {m.declaredDefaults} ({defaults.length})
      </summary>
      <p className="declared-defaults-copy">{m.declaredDefaultsCopy}</p>
      {defaults.length === 0 ? (
        <p className="declared-defaults-empty">{m.declaredDefaultsEmpty}</p>
      ) : (
        declaredDefaultClasses
          .map((decisionClass) => ({
            decisionClass,
            items: defaults.filter((item) => item.decision_class === decisionClass),
          }))
          .filter((group) => group.items.length > 0)
          .map((group) => (
            <section
              key={group.decisionClass}
              className={`declared-default-group${group.decisionClass === "problem" ? " agent-decided-problem" : ""}`}
            >
              <h3>{decisionClassLabel(m, group.decisionClass)}</h3>
              <ul>
                {group.items.map((item) => (
                  <li key={item.default_id}>
                    <strong>{item.title}</strong>
                    <p>{item.statement}</p>
                    <small>
                      {m.defaultRationale}: {item.rationale}
                    </small>
                    {(item.alternatives ?? []).length > 0 && (
                      <small>
                        {m.defaultAlternatives}: {(item.alternatives ?? []).join(" · ")}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))
      )}
    </details>
  );
}

/** Rung 0 is fixed by contract to be the textbook-simplest comparable baseline. */
function RefinementLadderPreview({
  specification,
  decisionTitles,
  open,
}: {
  specification: IntakeProblemSpecificationView;
  decisionTitles: Record<string, string>;
  open: boolean;
}) {
  const { messages: m } = useLocale();
  const ladder = specification.refinement_ladder ?? [];
  const defaultTitles = Object.fromEntries(
    (specification.declared_defaults ?? []).map((item) => [item.default_id, item.title]),
  );
  return (
    <details className="refinement-ladder" open={open}>
      <summary>
        {m.refinementLadder} ({ladder.length})
      </summary>
      {ladder.length === 0 ? (
        <p className="refinement-ladder-empty">{m.refinementLadderEmpty}</p>
      ) : (
        <ol className="ladder-rungs">
          {ladder.map((rung) => (
            <li key={rung.rung} className={rung.rung === 0 ? "baseline-rung" : undefined}>
              <header>
                <strong>
                  {rung.rung} · {rung.name}
                </strong>
                {rung.parallel_branch && <span>{m.ladderParallelBranch}</span>}
              </header>
              {rung.rung === 0 && <p className="ladder-baseline">{m.ladderBaseline}</p>}
              <p>
                {m.ladderRelaxes}: {rung.relaxes}
              </p>
              {(rung.default_ids ?? []).length > 0 && (
                <p>
                  {m.ladderDefaults}:{" "}
                  {(rung.default_ids ?? [])
                    .map((id) => defaultTitles[id] ?? id)
                    .join(" · ")}
                </p>
              )}
              {(rung.decision_ids ?? []).length > 0 && (
                <p>
                  {m.ladderDecisions}:{" "}
                  {(rung.decision_ids ?? [])
                    .map((id) => decisionTitles[id] ?? id)
                    .join(" · ")}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}

/**
 * What just happened to the command the user pressed, rendered where they
 * pressed it. A failed Intake command used to report itself one small line
 * below a long page of questions, which reads as "nothing happened".
 */
function IntakeCommandFeedback({
  notice,
  error,
  answersKept = null,
}: {
  notice: string | null;
  error: string | null;
  /** The previous attempt failed and the server kept what was typed. */
  answersKept?: string | null;
}) {
  if (!notice && !error && !answersKept) return null;
  return (
    <div className="intake-command-feedback">
      {answersKept && (
        <p className="intake-answers-kept" role="status">
          {answersKept}
        </p>
      )}
      {notice && (
        <p className="intake-notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="intake-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

interface QuestionCardProps {
  question: IntakeQuestion;
  answer:
    | { selected_option_ids: string[]; custom_text: string | null; strategy: AnswerStrategy | null }
    | undefined;
  onSelect: (question: IntakeQuestion, optionId: string) => void;
  onCustom: (decisionId: string, value: string) => void;
  onStrategy: (decisionId: string, strategy: AnswerStrategy) => void;
}

function QuestionCard({ question, answer, onSelect, onCustom, onStrategy }: QuestionCardProps) {
  const { messages: m } = useLocale();
  const choiceQuestion = question.answer_mode !== "text" && question.options.length > 0;
  return (
    <fieldset className="frontier-card">
      <legend>{question.title}</legend>
      <p className="frontier-prompt">{question.prompt}</p>
      <p className="frontier-why">{question.why_needed}</p>
      <p className="frontier-matters">
        <strong>{m.whyItMatters}:</strong> {question.why_it_matters}
      </p>
      {question.grounded_in && (
        <p className="frontier-grounded">
          {m.auditorAsked}: {question.grounded_in}
        </p>
      )}
      {question.options.map((option) => {
        const selected = answer?.selected_option_ids.includes(option.option_id) ?? false;
        const recommended = question.recommended_option_ids.includes(option.option_id);
        return (
          <label className={`frontier-option${selected ? " selected" : ""}`} key={option.option_id}>
            <input
              type={question.answer_mode === "multi_choice" ? "checkbox" : "radio"}
              aria-label={option.label}
              name={question.question_id}
              checked={selected}
              onChange={() => onSelect(question, option.option_id)}
            />
            <span>
              <strong>{option.label}{recommended ? ` · ${m.recommended}` : ""}</strong>
              <small>{option.impact}</small>
            </span>
          </label>
        );
      })}
      {question.recommendation_reason && (
        <p className="recommendation-reason">{question.recommendation_reason}</p>
      )}
      {choiceQuestion && (
        <div className="frontier-strategies">
          <button
            type="button"
            className={`strategy-button${answer?.strategy === "simplest_first" ? " selected" : ""}`}
            aria-pressed={answer?.strategy === "simplest_first"}
            onClick={() => onStrategy(question.decision_id, "simplest_first")}
          >
            {m.strategySimplestFirst}
          </button>
          {question.options.length >= 2 && (
            <button
              type="button"
              className={`strategy-button${answer?.strategy === "both_routes" ? " selected" : ""}`}
              aria-pressed={answer?.strategy === "both_routes"}
              onClick={() => onStrategy(question.decision_id, "both_routes")}
            >
              {m.strategyBothRoutes}
            </button>
          )}
        </div>
      )}
      {question.allow_custom && (
        <label
          className="custom-answer"
          htmlFor={`custom-answer-${question.question_id}`}
        >
          {m.customAnswer}
          <textarea
            id={`custom-answer-${question.question_id}`}
            rows={2}
            value={answer?.custom_text ?? ""}
            onChange={(event) => onCustom(question.decision_id, event.target.value)}
            placeholder={m.customAnswerPlaceholder}
          />
        </label>
      )}
    </fieldset>
  );
}

function CatalogSelect({
  id,
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: readonly (string | { value: string; label: string })[];
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label htmlFor={id}>
      {label}
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((item) => (
          <option
            key={typeof item === "string" ? item : item.value}
            value={typeof item === "string" ? item : item.value}
          >
            {typeof item === "string" ? item : item.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function RoundsIndicator({ convergence }: { convergence: IntakeConvergenceView | null }) {
  const { messages: m } = useLocale();
  return (
    <span>
      {m.intakeRoundsUsed}: {convergence?.rounds ?? 0} / {MAX_FRONTIER_ROUNDS}
    </span>
  );
}

export function RunStartForm(props: RunStartFormProps) {
  const { messages: m } = useLocale();
  const {
    session,
    activeSessions,
    specification,
    ready,
    sessionConfirmed,
    convergenceRequired,
    convergence,
    pendingQuestions,
    message,
    setMessage,
    intakeModel,
    setIntakeModel,
    intakeEffort,
    setIntakeEffort,
    intakeEffortOptions,
    serviceTier,
    setServiceTier,
    serviceTierOptions,
    roleEffortOptions,
    answers,
    intakeBusy,
    intakeError,
    intakeNotice,
    intakeAnswersKept,
    confirmed,
    setConfirmed,
    correctionMode,
    setCorrectionMode,
    roles,
    granularity,
    setGranularity,
    maxActiveBranches,
    setMaxActiveBranches,
    retries,
    setRetries,
    maxModelCalls,
    setMaxModelCalls,
    configComplete,
    allBlockingAnswered,
    start,
    selectOption,
    setCustomAnswer,
    setStrategy,
    submitRound,
    submitCorrection,
    declareDefaultsAndStart,
    resumeSession,
    cancelSession,
    setRole,
    confirmAndStart,
  } = useProblemIntake(props);

  const resolvedCount =
    session?.decisions.filter((item) => item.status === "resolved").length ?? 0;
  const openCount =
    session?.decisions.filter((item) => item.status === "open").length ?? 0;
  const finalizedByUser = convergence?.finalized_by_user ?? false;
  const decisionTitles = Object.fromEntries(
    (session?.decisions ?? []).map((item) => [item.decision_id, item.question.title]),
  );

  return (
    <main className="start-shell">
      <form
        className="start-card ai-intake-card"
        onSubmit={(event) => event.preventDefault()}
      >
        <header className="intake-heading">
          <div className="brand-mark" aria-hidden="true">
            Φ
          </div>
          <div>
            <h1>
              {ready
                ? m.startReadyTitle
                : convergenceRequired
                  ? m.convergenceTitle
                  : m.startInitialTitle}
            </h1>
            <p>
              {sessionConfirmed
                ? m.startConfirmedCopy
                : ready
                  ? m.startReadyCopy
                  : convergenceRequired
                    ? m.convergenceHeaderCopy
                    : m.startInitialCopy}
            </p>
          </div>
        </header>

        {props.defaults.model_catalog_source === "last_known_good" && (
          <p className="catalog-source-notice" role="status">
            App Server is unavailable; using the last verified model catalog
            {props.defaults.model_catalog_refreshed_at
              ? ` from ${new Date(props.defaults.model_catalog_refreshed_at).toLocaleString()}`
              : ""}.
          </p>
        )}

        {!session && activeSessions.length > 0 && (
          <details className="active-intake-sessions">
            <summary>{m.resumeIntake} ({activeSessions.length})</summary>
            <div>
              {activeSessions.map((item) => (
                <button
                  type="button"
                  key={item.session_id}
                  disabled={intakeBusy}
                  onClick={() => void resumeSession(item.session_id)}
                >
                  <strong>{item.problem_specifications.at(-1)?.sections.find((section) => section.name === "scientific_target")?.content ?? item.session_id}</strong>
                  <span>{item.status} · r{item.revision}</span>
                </button>
              ))}
            </div>
          </details>
        )}

        {!session && (
          <div className="intake-conversation">
            <label className="intake-message-label" htmlFor="intake-message">
              {m.researchProblemLabel}
            </label>
            <textarea
              id="intake-message"
              rows={7}
              value={message}
              disabled={intakeBusy}
              onChange={(event) => setMessage(event.target.value)}
              placeholder={m.researchPlaceholder}
            />
            <div className="config-grid">
              <CatalogSelect
                id="intake-model"
                label="Model"
                value={intakeModel}
                options={props.defaults.model_options.map((option) => ({
                  value: option.model,
                  label: option.display_name,
                }))}
                disabled={intakeBusy}
                onChange={setIntakeModel}
              />
              <CatalogSelect
                id="intake-effort"
                label="Effort"
                value={intakeEffort}
                options={intakeEffortOptions}
                disabled={intakeBusy}
                onChange={setIntakeEffort}
              />
              <CatalogSelect
                id="intake-speed"
                label="Speed"
                value={serviceTier}
                options={serviceTierOptions.map((value) => ({
                  value,
                  label: value === "fast" ? "Fast" : "Standard",
                }))}
                disabled={intakeBusy}
                onChange={(value) =>
                  setServiceTier(value as "standard" | "fast")
                }
              />
            </div>
            <IntakeCommandFeedback notice={intakeNotice} error={intakeError} />
            <div className="intake-send-row">
              <span>{m.intakeRoundLimit}</span>
              <button
                type="button"
                className="primary-button"
                disabled={intakeBusy || !message.trim()}
                onClick={() => void start()}
              >
                {intakeBusy ? m.aiOrganizing : m.organizeProblem}
              </button>
            </div>
          </div>
        )}

        {session && !ready && !convergenceRequired && !correctionMode && (
          <div className="intake-frontier" aria-live="polite">
            <div className="intake-progress">
              <RoundsIndicator convergence={convergence} />
              <span>{m.resolvedDecisions}: {resolvedCount}</span>
              <span>{m.openDecisions}: {openCount}</span>
              <button
                type="button"
                className="quiet-button"
                disabled={intakeBusy}
                onClick={() => void cancelSession()}
              >
                {m.cancelIntake}
              </button>
            </div>
            {session.frontier.map((question) => (
              <QuestionCard
                key={question.question_id}
                question={question}
                answer={answers[question.decision_id]}
                onSelect={selectOption}
                onCustom={setCustomAnswer}
                onStrategy={setStrategy}
              />
            ))}
            <IntakeCommandFeedback
              notice={intakeNotice}
              error={intakeError}
              answersKept={intakeAnswersKept}
            />
            <div className="intake-send-row">
              <span>{m.answerWholeRound}</span>
              <button
                type="button"
                className="primary-button"
                disabled={intakeBusy || !allBlockingAnswered}
                onClick={() => void submitRound()}
              >
                {intakeBusy ? m.aiOrganizing : m.answerContinue}
              </button>
            </div>
          </div>
        )}

        {session && convergenceRequired && !correctionMode && (
          <div className="intake-convergence" aria-live="polite">
            <div className="intake-progress">
              <RoundsIndicator convergence={convergence} />
              <span>{m.resolvedDecisions}: {resolvedCount}</span>
              <span>{m.openDecisions}: {openCount}</span>
              <button
                type="button"
                className="quiet-button"
                disabled={intakeBusy}
                onClick={() => void cancelSession()}
              >
                {m.cancelIntake}
              </button>
            </div>
            <div className="convergence-notice">
              <p className="convergence-reason">
                {convergenceReasonCopy(m, convergence?.reason)}
              </p>
              <p>{m.convergenceCopy}</p>
            </div>
            {pendingQuestions.length > 0 && (
              <p className="pending-questions-label">
                {m.pendingProblemQuestions} ({pendingQuestions.length})
              </p>
            )}
            {pendingQuestions.map((question) => (
              <QuestionCard
                key={question.question_id}
                question={question}
                answer={answers[question.decision_id]}
                onSelect={selectOption}
                onCustom={setCustomAnswer}
                onStrategy={setStrategy}
              />
            ))}
            <IntakeCommandFeedback
              notice={intakeNotice}
              error={intakeError}
              answersKept={intakeAnswersKept}
            />
            <div className="intake-send-row">
              <span>{m.convergenceSendHint}</span>
              <button
                type="button"
                className="primary-button"
                disabled={intakeBusy || !allBlockingAnswered}
                onClick={() => void declareDefaultsAndStart()}
              >
                {intakeBusy ? m.aiOrganizing : m.declareDefaultsAndStart}
              </button>
            </div>
          </div>
        )}

        {session && correctionMode && (
          <div className="intake-conversation">
            <label className="intake-message-label" htmlFor="intake-correction">
              {m.correctionLabel}
            </label>
            <textarea
              id="intake-correction"
              rows={4}
              value={message}
              disabled={intakeBusy}
              onChange={(event) => setMessage(event.target.value)}
              placeholder={m.correctionPlaceholder}
            />
            <IntakeCommandFeedback notice={intakeNotice} error={intakeError} />
            <div className="intake-send-row">
              <button type="button" className="quiet-button" onClick={() => setCorrectionMode(false)}>{m.back}</button>
              <button type="button" className="primary-button" disabled={intakeBusy || !message.trim()} onClick={() => void submitCorrection()}>{m.submitCorrection}</button>
            </div>
          </div>
        )}

        {specification && <SpecificationSummary specification={specification} />}
        {specification && (
          <DeclaredDefaultsPanel
            specification={specification}
            open={convergenceRequired || finalizedByUser}
          />
        )}
        {specification && (
          <RefinementLadderPreview
            specification={specification}
            decisionTitles={decisionTitles}
            open={convergenceRequired || finalizedByUser}
          />
        )}
        {session && <IntakeTraceDetails session={session} />}

        {ready && !correctionMode && (
          <div className="intake-confirmation">
            {finalizedByUser && (
              <p className="convergence-finalized">
                {convergenceReasonCopy(m, convergence?.reason)} {m.finalizedByUser}
              </p>
            )}
            <div className="config-grid run-speed-config">
              <CatalogSelect
                id="run-speed"
                label="Speed"
                value={serviceTier}
                options={serviceTierOptions.map((value) => ({
                  value,
                  label: value === "fast" ? "Fast" : "Standard",
                }))}
                disabled={intakeBusy}
                onChange={(value) =>
                  setServiceTier(value as "standard" | "fast")
                }
              />
            </div>
            <div className="role-config-grid">
              {(["writer", "checker", "judge"] as RoleName[]).map((name) => (
                <fieldset key={name}>
                  <legend>{name}</legend>
                  <p className="fixed-field">Provider <strong>{roles[name].provider}</strong></p>
                  <CatalogSelect
                    id={`${name}-model`}
                    label="Model"
                    value={roles[name].model}
                    options={props.defaults.model_options.map((option) => ({
                      value: option.model,
                      label: option.display_name,
                    }))}
                    disabled={intakeBusy}
                    onChange={(value) => setRole(name, "model", value)}
                  />
                  <CatalogSelect
                    id={`${name}-effort`}
                    label="Effort"
                    value={roles[name].effort}
                    options={roleEffortOptions[name]}
                    disabled={intakeBusy}
                    onChange={(value) => setRole(name, "effort", value)}
                  />
                </fieldset>
              ))}
            </div>
            <details className="advanced-config">
              <summary>{m.advancedConfig}</summary>
              <p>{m.advancedConfigCopy}</p>
              <div className="config-grid">
                <label>Granularity<select value={granularity} onChange={(event) => setGranularity(event.target.value as RunConfig["granularity"])}><option value="one_task">one_task</option><option value="one_claim">one_claim</option></select></label>
                <label>Max model calls<input type="number" min={1} max={10000} value={maxModelCalls} onChange={(event) => setMaxModelCalls(event.target.value)} /></label>
                <label>Max active branches<input type="number" min={1} max={1000} value={maxActiveBranches} onChange={(event) => setMaxActiveBranches(event.target.value)} /></label>
                <label>Retries<select value={retries} onChange={(event) => setRetries(event.target.value as "0" | "1")}><option value="0">0</option><option value="1">1</option></select></label>
              </div>
            </details>
            {sessionConfirmed ? (
              <p className="confirmation-choice">{m.startConfirmedCopy}</p>
            ) : (
              <label className="confirmation-choice">
                <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
                {m.confirmPrompt}
              </label>
            )}
            <IntakeCommandFeedback notice={intakeNotice} error={intakeError} />
            <div className="intake-final-actions">
              {!sessionConfirmed && <button type="button" className="quiet-button" onClick={() => setCorrectionMode(true)}>{m.continueEditing}</button>}
              <button type="button" className="primary-button" disabled={(!sessionConfirmed && !confirmed) || !configComplete || props.loading || intakeBusy} onClick={() => void confirmAndStart()}>{props.loading ? m.creating : sessionConfirmed ? m.startConfirmed : m.confirmStart}</button>
            </div>
          </div>
        )}
      </form>
    </main>
  );
}
