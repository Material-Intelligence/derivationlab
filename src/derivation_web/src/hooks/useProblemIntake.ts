import { useEffect, useMemo, useState } from "react";
import type {
  AnswerStrategy,
  CreateIntakeSessionRequest,
  CreateRunDefaultsView,
  CreateRunRequest,
  FinalizeIntakeSessionRequest,
  IntakeAnswerInput,
  IntakePendingAnswer,
  IntakePendingSubmission,
  IntakeQuestion,
  IntakeSessionView,
  ProductModelRoleConfig,
  RunConfig,
  SubmitIntakeRoundRequest,
} from "../types";
import { freezeIntakeSession } from "../intake/problemProtocol";
import { useLocale } from "../i18n";

export type RoleName = "writer" | "checker" | "judge";

/**
 * The server caps the grill; the cap itself is not on the wire, so the number
 * the user sees lives here and in the copy next to it.
 */
export const MAX_FRONTIER_ROUNDS = 3;

/**
 * HTTP status the API uses for every Intake command the authoritative state
 * refuses: a stale `base_revision`, a decision that is no longer open, and a
 * round the server could not apply. Reloading the session is how the client
 * tells those apart, because only the first kind moves the revision.
 */
const INTAKE_CONFLICT_STATUS = 409;

const isIntakeConflict = (reason: unknown): boolean =>
  typeof reason === "object" &&
  reason !== null &&
  (reason as { status?: unknown }).status === INTAKE_CONFLICT_STATUS;

/**
 * Whether a refused command is worth sending again.
 *
 * A 409 that left the revision where it was is not a race — the session
 * never moved, so the server has already refused this exact command against
 * this exact state and will refuse it identically. Retrying it costs the user
 * two more model calls of 50-90 s before the same error reaches the screen, so
 * the retry is reserved for a revision that actually advanced under the client.
 */
const revisionMoved = (
  sent: IntakeSessionView,
  reloaded: IntakeSessionView,
): boolean => reloaded.revision !== sent.revision;

/**
 * `question_id` pins the answer to the exact question it was typed against, so
 * a session reload can carry the answer over while a *reopened* decision — same
 * `decision_id`, new question — still starts from a blank answer.
 */
interface LocalIntakeAnswer {
  question_id: string;
  selected_option_ids: string[];
  custom_text: string | null;
  strategy: AnswerStrategy | null;
}

interface UseProblemIntakeOptions {
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

const emptyAnswer = (questionId: string): LocalIntakeAnswer => ({
  question_id: questionId,
  selected_option_ids: [],
  custom_text: null,
  strategy: null,
});

/**
 * The answer a failed round left on the server, put back on its question.
 *
 * The server holds a submission against the revision it was typed at and stops
 * reporting it once the session moves, so whatever arrives here was typed
 * against exactly the questions now on screen. The cast is the OpenAPI
 * pattern-properties map, which generates as `unknown | IntakePendingAnswerView`.
 */
const heldAnswer = (
  question: IntakeQuestion,
  held: IntakePendingSubmission | null,
): LocalIntakeAnswer | null => {
  const answer = held?.answers?.[question.decision_id] as
    | IntakePendingAnswer
    | undefined;
  if (!answer) return null;
  return {
    question_id: question.question_id,
    selected_option_ids: [...(answer.selected_option_ids ?? [])],
    custom_text: answer.custom_text ?? null,
    strategy: answer.strategy ?? null,
  };
};

/** The questions the session is waiting on, whichever list the status uses. */
const openQuestionsOf = (
  session: IntakeSessionView,
): readonly IntakeQuestion[] =>
  session.status === "convergence_required"
    ? (session.pending_problem_questions ?? [])
    : session.frontier;

const requiredQuestionsOf = (
  session: IntakeSessionView,
): readonly IntakeQuestion[] =>
  openQuestionsOf(session).filter(
    (question) => session.status === "convergence_required" || question.blocking,
  );

const catalogModel = (defaults: CreateRunDefaultsView, preferred: string): string =>
  defaults.model_options.some((option) => option.model === preferred)
    ? preferred
    : (defaults.model_options.find((option) => option.is_default)?.model ??
      defaults.model_options[0]?.model ?? "");

const effortsFor = (defaults: CreateRunDefaultsView, model: string): readonly string[] =>
  defaults.model_options.find((option) => option.model === model)?.supported_efforts ?? [];

const serviceTiersFor = (
  defaults: CreateRunDefaultsView,
  model: string,
): readonly ("standard" | "fast")[] =>
  defaults.model_options.find((option) => option.model === model)
    ?.supported_service_tiers ?? ["standard"];

const catalogEffort = (
  defaults: CreateRunDefaultsView,
  model: string,
  preferred: string,
): string => {
  const option = defaults.model_options.find((item) => item.model === model);
  if (!option) return "";
  return option.supported_efforts.includes(preferred)
    ? preferred
    : option.default_effort;
};

const catalogServiceTier = (
  defaults: CreateRunDefaultsView,
  model: string,
  preferred: "standard" | "fast",
): "standard" | "fast" => {
  const option = defaults.model_options.find((item) => item.model === model);
  if (!option) return "standard";
  return (option.supported_service_tiers ?? ["standard"]).includes(preferred)
    ? preferred
    : (option.default_service_tier ?? "standard");
};

const answered = (answer: LocalIntakeAnswer | undefined): boolean =>
  Boolean(
    answer &&
      (answer.selected_option_ids.length > 0 ||
        answer.custom_text?.trim() ||
        answer.strategy),
  );

/**
 * Carry the answers the user already typed onto the questions a freshly loaded
 * session still asks.
 *
 * An answer survives when its decision is still open *and* still asks the same
 * question. Anything else is dropped, and the caller says so instead of
 * retrying a command the server will refuse again.
 */
const remapAnswers = (
  previous: Record<string, LocalIntakeAnswer>,
  session: IntakeSessionView,
): {
  answers: Record<string, LocalIntakeAnswer>;
  droppedDecisionIds: string[];
  complete: boolean;
} => {
  const next: Record<string, LocalIntakeAnswer> = {};
  for (const question of openQuestionsOf(session)) {
    const carried = previous[question.decision_id];
    next[question.decision_id] =
      carried && carried.question_id === question.question_id
        ? carried
        : emptyAnswer(question.question_id);
  }
  const droppedDecisionIds = Object.entries(previous)
    .filter(
      ([decisionId, answer]) =>
        answered(answer) && next[decisionId] !== answer,
    )
    .map(([decisionId]) => decisionId);
  const required = requiredQuestionsOf(session);
  return {
    answers: next,
    droppedDecisionIds,
    complete:
      required.length > 0 &&
      required.every((question) => answered(next[question.decision_id])),
  };
};

/**
 * A ladder strategy replaces an explicit option selection but may carry
 * clarifying text the user typed alongside it.
 */
const wireAnswers = (
  source: Record<string, LocalIntakeAnswer>,
): Record<string, IntakeAnswerInput> =>
  Object.fromEntries(
    Object.entries(source).map(([decisionId, answer]) => [
      decisionId,
      answer.strategy
        ? {
            selected_option_ids: [],
            custom_text: answer.custom_text,
            strategy: answer.strategy,
          }
        : {
            selected_option_ids: answer.selected_option_ids,
            custom_text: answer.custom_text,
          },
    ]),
  );

export function useProblemIntake({
  defaults,
  initialSessionId,
  onCreate,
  onLoad,
  onListActive,
  onRound,
  onFinalize,
  onConfirm,
  onCancel,
  onSessionChange,
  onSubmit,
}: UseProblemIntakeOptions) {
  const { messages: m } = useLocale();
  const [session, setSession] = useState<IntakeSessionView | null>(null);
  const [activeSessions, setActiveSessions] = useState<IntakeSessionView[]>([]);
  const [message, setMessage] = useState("");
  const [intakeModel, setIntakeModel] = useState(() =>
    catalogModel(defaults, defaults.config.writer.model),
  );
  const [intakeEffort, setIntakeEffort] = useState(() =>
    catalogEffort(
      defaults,
      catalogModel(defaults, defaults.config.writer.model),
      defaults.config.writer.effort,
    ),
  );
  const [serviceTier, setServiceTier] = useState<"standard" | "fast">(
    defaults.runtime.service_tier ?? "standard",
  );
  const [answers, setAnswers] = useState<Record<string, LocalIntakeAnswer>>({});
  const [intakeBusy, setIntakeBusy] = useState(false);
  const [intakeError, setIntakeError] = useState<string | null>(null);
  /** What the session changed under the user while they were answering. */
  const [intakeNotice, setIntakeNotice] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [correctionMode, setCorrectionMode] = useState(false);
  const [roles, setRoles] = useState<Record<RoleName, ProductModelRoleConfig>>(
    () => ({
      writer: structuredClone(defaults.config.writer),
      checker: structuredClone(defaults.config.checker),
      judge: structuredClone(defaults.config.judge),
    }),
  );
  const [granularity, setGranularity] = useState<RunConfig["granularity"]>(
    defaults.config.granularity,
  );
  const [maxActiveBranches, setMaxActiveBranches] = useState(
    defaults.config.max_active_branches === null
      ? ""
      : String(defaults.config.max_active_branches),
  );
  const [retries, setRetries] = useState<"0" | "1">(
    String(defaults.runtime.retries) as "0" | "1",
  );
  const [maxModelCalls, setMaxModelCalls] = useState(
    String(defaults.config.max_model_calls),
  );

  useEffect(() => {
    let active = true;
    void onListActive()
      .then((items) => {
        if (active) setActiveSessions(items);
      })
      .catch(() => undefined);
    if (initialSessionId) {
      setIntakeBusy(true);
      void onLoad(initialSessionId)
        .then((value) => {
          if (active) {
            setSession(value);
            setServiceTier(value.service_tier ?? "standard");
          }
        })
        .catch((reason: unknown) => {
          if (active) {
            setIntakeError(reason instanceof Error ? reason.message : m.intakeFailed);
          }
        })
        .finally(() => {
          if (active) setIntakeBusy(false);
        });
    }
    return () => {
      active = false;
    };
  }, [initialSessionId, m.intakeFailed, onListActive, onLoad]);

  const convergenceRequired = session?.status === "convergence_required";
  const pendingQuestions = useMemo(
    () => session?.pending_problem_questions ?? [],
    [session?.pending_problem_questions],
  );
  /**
   * `convergence_required` empties the frontier by contract; the open decisions
   * move to `pending_problem_questions` and all of them must be answered.
   */
  const openQuestions = useMemo(
    () => (convergenceRequired ? pendingQuestions : (session?.frontier ?? [])),
    [convergenceRequired, pendingQuestions, session?.frontier],
  );
  const convergence = session?.convergence ?? null;
  /**
   * What a previous attempt submitted and never got committed: the round it
   * paid for failed after the answers were already typed. The server keeps it
   * only while it still fits the session's current revision.
   */
  const pendingSubmission = useMemo(
    () => session?.pending_submission ?? null,
    [session?.pending_submission],
  );

  /**
   * Re-seed the answer sheet whenever the session moves. Answers are kept per
   * `question_id`, so a reload that returns the same questions (the conflict
   * recovery below) does not throw away what the user already typed, while a
   * new or reopened question still starts blank — except where the server is
   * still holding what the user submitted, which seeds the blank one.
   */
  useEffect(() => {
    setAnswers((current) =>
      Object.fromEntries(
        openQuestions.map((question) => {
          const carried = current[question.decision_id];
          return [
            question.decision_id,
            carried && carried.question_id === question.question_id
              ? carried
              : (heldAnswer(question, pendingSubmission) ??
                emptyAnswer(question.question_id)),
          ];
        }),
      ),
    );
    setConfirmed(session?.status === "confirmed");
  }, [openQuestions, pendingSubmission, session?.revision, session?.status]);

  const sessionConfirmed = session?.status === "confirmed";
  const ready = session?.status === "candidate_ready" || sessionConfirmed;
  const specification = session?.problem_specifications.at(-1) ?? null;
  const configComplete =
    Object.values(roles).every((role) => role.model.trim() && role.effort.trim()) &&
    Object.values(roles).every((role) =>
      serviceTiersFor(defaults, role.model).includes(serviceTier),
    ) &&
    Number(maxModelCalls) > 0 &&
    Number.isFinite(Number(maxModelCalls)) &&
    (maxActiveBranches === "" ||
      (Number(maxActiveBranches) > 0 &&
        Number.isFinite(Number(maxActiveBranches))));
  const allBlockingAnswered = useMemo(
    () =>
      openQuestions
        .filter((question) => convergenceRequired || question.blocking)
        .every((question) => answered(answers[question.decision_id])),
    [answers, convergenceRequired, openQuestions],
  );

  const runBusy = async (operation: () => Promise<void>) => {
    if (intakeBusy) return;
    setIntakeBusy(true);
    setIntakeError(null);
    setIntakeNotice(null);
    try {
      await operation();
    } catch (reason) {
      setIntakeError(reason instanceof Error ? reason.message : m.intakeFailed);
    } finally {
      setIntakeBusy(false);
    }
  };

  const questionTitles = (
    source: IntakeSessionView,
    decisionIds: readonly string[],
  ): string[] =>
    decisionIds.map(
      (decisionId) =>
        openQuestionsOf(source).find(
          (question) => question.decision_id === decisionId,
        )?.title ?? decisionId,
    );

  /**
   * Reload the session a refused command was aimed at.
   *
   * When the reload itself fails there is nothing to recover onto, so the
   * command's own refusal — the actionable one — is re-raised and the reload
   * failure is reported beside it rather than dropped.
   */
  const reloadForRecovery = async (
    sessionId: string,
    refusal: unknown,
  ): Promise<IntakeSessionView> => {
    try {
      return await onLoad(sessionId);
    } catch (loadFailure) {
      setIntakeNotice(
        loadFailure instanceof Error ? loadFailure.message : m.intakeFailed,
      );
      throw refusal;
    }
  };

  /**
   * Send one answer-carrying command, and recover from the authoritative state
   * refusing it.
   *
   * A 409 may mean this client is arguing with a session that has moved: the
   * revision, the open decisions, or both. Reload the session, keep the answers
   * whose decision still asks the same question, and send the command once more
   * against the fresh revision with a new idempotency key. The retry is skipped
   * — and the server's own message surfaced — when the reload shows the
   * session never moved, or leaves the command unanswerable, so a doomed
   * command is never sent twice.
   */
  const sendAnswerCommand = async (
    current: IntakeSessionView,
    localAnswers: Record<string, LocalIntakeAnswer>,
    send: (
      session: IntakeSessionView,
      wire: Record<string, IntakeAnswerInput>,
    ) => Promise<IntakeSessionView>,
  ) => {
    try {
      setSession(await send(current, wireAnswers(localAnswers)));
      return;
    } catch (reason) {
      if (!isIntakeConflict(reason)) throw reason;
      const refreshed = await reloadForRecovery(current.session_id, reason);
      const remapped = remapAnswers(localAnswers, refreshed);
      setSession(refreshed);
      setAnswers(remapped.answers);
      if (remapped.droppedDecisionIds.length > 0) {
        setIntakeNotice(
          `${m.intakeAnswersDropped} ${questionTitles(
            current,
            remapped.droppedDecisionIds,
          ).join(" / ")}`,
        );
      }
      if (!revisionMoved(current, refreshed)) throw reason;
      if (!remapped.complete || refreshed.status !== current.status) throw reason;
      setSession(await send(refreshed, wireAnswers(remapped.answers)));
    }
  };

  const start = () =>
    runBusy(async () => {
      if (!message.trim()) return;
      const created = await onCreate({
        initial_message: message.trim(),
        model: intakeModel,
        effort: intakeEffort,
        service_tier: serviceTier,
      });
      setSession(created);
      setMessage("");
      onSessionChange(created.session_id);
      setActiveSessions((items) => [
        created,
        ...items.filter((item) => item.session_id !== created.session_id),
      ]);
    });

  /** The question an answer belongs to; the effect above seeds it, this is the guard. */
  const questionIdFor = (decisionId: string): string =>
    openQuestions.find((question) => question.decision_id === decisionId)
      ?.question_id ?? decisionId;

  const selectOption = (question: IntakeQuestion, optionId: string) => {
    setAnswers((current) => {
      const answer =
        current[question.decision_id] ?? emptyAnswer(question.question_id);
      const selected = answer.selected_option_ids.includes(optionId);
      const nextSelected =
        question.answer_mode === "multi_choice"
          ? selected
            ? answer.selected_option_ids.filter((item) => item !== optionId)
            : [...answer.selected_option_ids, optionId]
          : selected
            ? []
            : [optionId];
      return {
        ...current,
        [question.decision_id]: {
          ...answer,
          selected_option_ids: nextSelected,
          strategy: null,
        },
      };
    });
  };

  const setCustomAnswer = (decisionId: string, value: string) => {
    setAnswers((current) => ({
      ...current,
      [decisionId]: {
        ...(current[decisionId] ?? emptyAnswer(questionIdFor(decisionId))),
        custom_text: value.trim() ? value : null,
      },
    }));
  };

  /**
   * Picking a strategy replaces the explicit selection rather than adding to
   * it: `both_routes` without a selection means every option the question
   * offered, which is exactly what the button promises. Custom text survives
   * strategy changes because it is part of the user's answer.
   */
  const setStrategy = (decisionId: string, strategy: AnswerStrategy) => {
    setAnswers((current) => {
      const answer =
        current[decisionId] ?? emptyAnswer(questionIdFor(decisionId));
      const next = answer.strategy === strategy ? null : strategy;
      return {
        ...current,
        [decisionId]: {
          ...answer,
          selected_option_ids: next ? [] : answer.selected_option_ids,
          strategy: next,
        },
      };
    });
  };

  const submitRound = () => {
    const current = session;
    const typed = answers;
    return runBusy(async () => {
      if (!current || convergenceRequired || !allBlockingAnswered) return;
      await sendAnswerCommand(current, typed, (view, wire) =>
        onRound(view.session_id, {
          base_revision: view.revision,
          answers: wire,
          user_message: null,
        }),
      );
    });
  };

  const submitCorrection = () => {
    const current = session;
    return runBusy(async () => {
      if (!current || convergenceRequired || !message.trim()) return;
      const correction = message.trim();
      const send = (view: IntakeSessionView) =>
        onRound(view.session_id, {
          base_revision: view.revision,
          answers: {},
          user_message: correction,
        });
      try {
        setSession(await send(current));
      } catch (reason) {
        if (!isIntakeConflict(reason)) throw reason;
        const refreshed = await reloadForRecovery(current.session_id, reason);
        setSession(refreshed);
        // A correction round is the same two model calls as an answered one,
        // so it gets the same rule: retry a race, never a flat refusal.
        if (!revisionMoved(current, refreshed)) throw reason;
        if (refreshed.status !== current.status) throw reason;
        setSession(await send(refreshed));
      }
      // Only a committed correction clears the box the user typed it into.
      setMessage("");
      setCorrectionMode(false);
    });
  };

  const declareDefaultsAndStart = () => {
    const current = session;
    const typed = answers;
    return runBusy(async () => {
      if (!current || !convergenceRequired || !allBlockingAnswered) return;
      await sendAnswerCommand(current, typed, (view, wire) =>
        onFinalize(view.session_id, {
          base_revision: view.revision,
          answers: wire,
        }),
      );
    });
  };

  const resumeSession = (sessionId: string) =>
    runBusy(async () => {
      const resumed = await onLoad(sessionId);
      setSession(resumed);
      setServiceTier(resumed.service_tier ?? "standard");
      setCorrectionMode(false);
      onSessionChange(sessionId);
    });

  const cancelSession = () =>
    runBusy(async () => {
      if (!session) return;
      await onCancel(session.session_id, session.revision);
      setActiveSessions((items) =>
        items.filter((item) => item.session_id !== session.session_id),
      );
      setSession(null);
      setCorrectionMode(false);
      onSessionChange(undefined);
    });

  const setRole = (
    name: RoleName,
    field: "model" | "effort",
    value: string,
  ) => {
    setRoles((current) => {
      if (field === "model") {
        setServiceTier((currentTier) =>
          serviceTiersFor(defaults, value).includes(currentTier)
            ? currentTier
            : "standard",
        );
        return {
          ...current,
          [name]: {
            ...current[name],
            model: value,
            effort: catalogEffort(defaults, value, current[name].effort),
          },
        };
      }
      return { ...current, [name]: { ...current[name], effort: value } };
    });
  };

  const changeIntakeModel = (value: string) => {
    setIntakeModel(value);
    setIntakeEffort((current) => catalogEffort(defaults, value, current));
    setServiceTier((current) =>
      catalogServiceTier(defaults, value, current),
    );
  };

  const confirmAndStart = () =>
    runBusy(async () => {
      if (
        !session ||
        !ready ||
        (!sessionConfirmed && !confirmed) ||
        !configComplete
      ) return;
      const freeze = async (): Promise<IntakeSessionView> => {
        if (sessionConfirmed) return session;
        try {
          return await onConfirm(session.session_id, session.revision);
        } catch (reason) {
          if (!isIntakeConflict(reason)) throw reason;
          const refreshed = await reloadForRecovery(session.session_id, reason);
          setSession(refreshed);
          if (refreshed.status === "confirmed") return refreshed;
          if (refreshed.status !== "candidate_ready") throw reason;
          return await onConfirm(refreshed.session_id, refreshed.revision);
        }
      };
      const frozen = await freeze();
      setSession(frozen);
      await onSubmit({
        problem: freezeIntakeSession(frozen),
        config: {
          ...structuredClone(defaults.config),
          ...roles,
          granularity,
          max_model_calls: Number(maxModelCalls),
          max_active_branches:
            maxActiveBranches === "" ? null : Number(maxActiveBranches),
        },
        runtime: {
          ...structuredClone(defaults.runtime),
          retries: Number(retries) as 0 | 1,
          service_tier: serviceTier,
        },
      });
      onSessionChange(undefined);
    });

  return {
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
    setIntakeModel: changeIntakeModel,
    intakeEffort,
    setIntakeEffort,
    intakeEffortOptions: effortsFor(defaults, intakeModel),
    serviceTier,
    setServiceTier,
    serviceTierOptions: ready
      ? (["standard", "fast"] as const).filter((tier) =>
          Object.values(roles).every((role) =>
            serviceTiersFor(defaults, role.model).includes(tier),
          ),
        )
      : serviceTiersFor(defaults, intakeModel),
    roleEffortOptions: Object.fromEntries(
      (Object.keys(roles) as RoleName[]).map((name) => [
        name,
        effortsFor(defaults, roles[name].model),
      ]),
    ) as Record<RoleName, readonly string[]>,
    answers,
    intakeBusy,
    intakeError,
    intakeNotice,
    /**
     * Shown above the submit button, beside the 409 notice rather than in
     * place of it: the answers on screen are the ones the failed attempt kept.
     * A correction-only submission pre-fills nothing, so it says nothing.
     *
     * Only a row that carries a failure reason says the round failed. The row
     * is written *before* the 50-90 s model call and is served throughout it,
     * so a reload mid-round finds one with no reason yet — and telling that
     * user their submission already failed invites them to send it again into
     * the round that is still running. The pre-fill is deliberately not gated
     * the same way: a process killed mid-round never writes a reason either,
     * and the answers still have to come back.
     */
    intakeAnswersKept:
      pendingSubmission?.failure_reason &&
      Object.keys(pendingSubmission.answers ?? {}).length > 0
        ? m.intakeAnswersKept
        : null,
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
  };
}
