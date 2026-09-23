import type { DerivationClient } from "./api";
import type { ProblemPresetsView } from "./api/generated";
import type { LiveCall } from "./live";
import type {
  CreateBranchRequest,
  CreateRunDefaultsView,
  DerivationRun,
  DerivationStep,
  IntakeDeclaredDefaultView,
  IntakeLadderRungView,
  IntakeProblemSpecificationView,
  IntakeQuestionView,
  IntakeSessionView,
  RunConfig,
  RunEvent,
  RunRuntime,
  RuntimeOverlay,
} from "./types";

export const fixtureConfig: RunConfig = {
  granularity: "one_claim",
  writer: { provider: "openai", model: "gpt-5.6-sol", effort: "high" },
  checker: { provider: "openai", model: "gpt-5.6-sol", effort: "medium" },
  judge: { provider: "openai", model: "gpt-5.6-sol", effort: "high" },
  backend: { name: "codex-app-server", version: "0.147.0" },
  max_model_calls: 10,
  max_active_branches: 3,
  reference_allowed: false,
  allowed_paths: [],
};

export const fixtureRuntime: RunRuntime = {
  auth_mode: "chatgpt",
  concurrency: 1,
  retries: 0,
  max_run_seconds: null,
  capability_profile: "benchmark_symbolic_v1",
  service_tier: "fast",
};

export const fixtureCreateRunDefaults: CreateRunDefaultsView = {
  config: { ...fixtureConfig, max_model_calls: 100 },
  runtime: fixtureRuntime,
  model_options: [
    {
      model: "gpt-5.6-sol",
      display_name: "GPT-5.6-Sol",
      is_default: true,
      default_effort: "low",
      supported_efforts: ["low", "medium", "high", "xhigh", "max", "ultra"],
      default_service_tier: "fast",
      supported_service_tiers: ["standard", "fast"],
    },
    {
      model: "gpt-5.5",
      display_name: "GPT-5.5",
      is_default: false,
      default_effort: "medium",
      supported_efforts: ["low", "medium", "high", "xhigh"],
      default_service_tier: "fast",
      supported_service_tiers: ["standard", "fast"],
    },
    {
      model: "gpt-5.4-mini",
      display_name: "GPT-5.4 Mini",
      is_default: false,
      default_effort: "medium",
      supported_efforts: ["low", "medium", "high", "xhigh"],
      default_service_tier: "standard",
      supported_service_tiers: ["standard"],
    },
  ],
  model_catalog_source: "static_fixture",
  model_catalog_refreshed_at: null,
  allowed_models: ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4-mini"],
  allowed_efforts: ["low", "medium", "high", "xhigh", "max", "ultra"],
};

const baseProvenance = (index: number) => ({
  model: "gpt-5-codex",
  threadId: `thread-demo-${Math.floor(index / 3) + 1}`,
  turnId: `turn-demo-${index}`,
  createdAt: `2026-08-29T${String(14 + Math.floor(index / 4)).padStart(2, "0")}:${String((index * 7) % 60).padStart(2, "0")}:00Z`,
});

const step = (
  id: string,
  order: number,
  title: string,
  input: string,
  reasoningSummary: string,
  output: string,
  status: DerivationStep["status"] = "sealed",
  branchId = "branch-root",
): DerivationStep => ({
  id,
  revisionId: `revision-${id}`,
  order,
  title,
  status,
  branch_id: branchId,
  content: status === "proposed" ? null : {
    claim: output,
    why: reasoningSummary,
    source: "The parent step, model calls and automatic check records of this derivation.",
    derivation: `${input} → ${reasoningSummary}`,
    scope: "Holds only within the constraints, approximations and validity bounds declared for this run.",
  },
  output_sha256: null,
  input,
  reasoningSummary,
  output,
  checks: {
    schema: status === "proposed" ? "pending" : "passed",
    physics: status === "proposed" ? "pending" : "passed",
    provenance: "passed",
  },
  provenance: baseProvenance(order),
});

export const fixtureRun: DerivationRun = {
  id: "demo-run",
  question: "Starting from locality, conservation laws and symmetry constraints, construct a low-energy effective theory for a two-dimensional quantum model.",
  phase: "review_ready_due_to_cap",
  status: "review_ready",
  config: fixtureConfig,
  runtime: fixtureRuntime,
  canonical_event_id: 10,
  read_only: false,
  commands: {
    can_pause: false,
    can_resume: false,
    can_interrupt: false,
    branchable_step_revision_ids: [
      "revision-question",
      "revision-constraints",
      "revision-conservation",
      "revision-symmetry",
      "revision-perturbation",
      "revision-counterexample",
      "revision-representation",
      "revision-weak-result",
      "revision-boundary-result",
      "revision-symmetry-result",
    ],
  },
  rootStepId: "question",
  budget: { usedSteps: 10, maxSteps: 10 },
  steps: [
    step("question", 0, "Research question", "Original research question", "Narrow the natural-language goal to a checkable derivation task.", "The goal is a low-energy effective theory that respects locality and symmetry."),
    step("constraints", 1, "Define states and constraints", "Research question", "Separate hard constraints, adjustable assumptions and approximation bounds.", "Four hard constraints are fixed; adjustable assumptions are recorded separately from them."),
    step("conservation", 2, "Conservation-law route", "States and constraints", "Collect the couplings allowed by the continuity equation.", "A set of candidate actions ordered by power counting.", "sealed", "branch-a"),
    step("symmetry", 3, "Symmetry route", "States and constraints", "Decompose the candidates by space-group and time-reversal representations.", "Three candidates with the wrong transformation properties are excluded.", "sealed", "branch-c"),
    step("perturbation", 4, "Perturbative expansion", "Conservation-law route", "Expand to second order around the non-interacting limit.", "The leading correction comes from second-order virtual transitions.", "sealed", "branch-a"),
    step("counterexample", 5, "Numerical counterexample check", "Conservation-law route", "Test the limits of the analytic assumptions with small-scale exact diagonalization.", "Deviations appear at strong coupling, so the validity range must enter the result.", "sealed", "branch-b"),
    step("representation", 6, "Representation decomposition", "Symmetry route", "Project the low-energy subspace onto irreducible representations.", "The only surviving scalar channel is compatible with the conservation-law route.", "sealed", "branch-c"),
    step("weak-result", 7, "Result A: weak coupling", "Perturbative expansion", "Combine the perturbative expansion with the constraint checks.", "A closed-form effective Hamiltonian at weak coupling.", "sealed", "branch-a"),
    step("boundary-result", 8, "Result B: validity boundary", "Numerical counterexample check", "Turn the numerical counterexample into an explicit validity interval.", "The result stands, but only while the dimensionless coupling stays below a threshold.", "sealed", "branch-b"),
    step("symmetry-result", 9, "Result C: symmetry closure", "Representation decomposition", "Check that the representation decomposition closes on the low-energy couplings.", "A result equivalent to route A, supported by independent evidence.", "sealed", "branch-c"),
  ],
  edges: [
    { id: "e0", from: "question", to: "constraints", order: 0, kind: "continuation" },
    { id: "e1", from: "constraints", to: "conservation", order: 0, kind: "model_fork" },
    { id: "e2", from: "constraints", to: "symmetry", order: 1, kind: "model_fork" },
    { id: "e3", from: "conservation", to: "perturbation", order: 0, kind: "model_fork" },
    { id: "e4", from: "conservation", to: "counterexample", order: 1, kind: "model_fork" },
    { id: "e5", from: "symmetry", to: "representation", order: 0, kind: "model_fork" },
    { id: "e7", from: "perturbation", to: "weak-result", order: 0, kind: "continuation" },
    { id: "e8", from: "counterexample", to: "boundary-result", order: 0, kind: "continuation" },
    { id: "e9", from: "representation", to: "symmetry-result", order: 0, kind: "continuation" },
  ],
  branches: [
    { branch_id: "branch-root", parent_branch_id: null, anchor_step_revision_id: null, kind: "root", status: "completed", status_history: [{ seq: 1, status: "active" }, { seq: 10, status: "completed" }], step_revision_ids: ["revision-question", "revision-constraints"] },
    { branch_id: "branch-a", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-constraints", kind: "model_fork", status: "completed", status_history: [{ seq: 2, status: "active" }, { seq: 8, status: "completed" }], step_revision_ids: ["revision-conservation", "revision-perturbation", "revision-weak-result"] },
    { branch_id: "branch-b", parent_branch_id: "branch-a", anchor_step_revision_id: "revision-conservation", kind: "model_fork", status: "completed", status_history: [{ seq: 3, status: "active" }, { seq: 9, status: "completed" }], step_revision_ids: ["revision-counterexample", "revision-boundary-result"] },
    { branch_id: "branch-c", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-constraints", kind: "model_fork", status: "parked", status_history: [{ seq: 4, status: "active" }, { seq: 10, status: "parked" }], step_revision_ids: ["revision-symmetry", "revision-representation", "revision-symmetry-result"] },
  ],
  routes: [
    { id: "route-a", label: "Result A: weak coupling", nodeIds: ["question", "constraints", "conservation", "perturbation", "weak-result"], status: "complete", branch_id: "branch-a", status_history: [{ seq: 2, status: "active" }, { seq: 8, status: "completed" }] },
    { id: "route-b", label: "Result B: validity boundary", nodeIds: ["question", "constraints", "conservation", "counterexample", "boundary-result"], status: "complete", branch_id: "branch-b", status_history: [{ seq: 3, status: "active" }, { seq: 9, status: "completed" }] },
    { id: "route-c", label: "Result C: symmetry closure", nodeIds: ["question", "constraints", "symmetry", "representation", "symmetry-result"], status: "proposed", branch_id: "branch-c", status_history: [{ seq: 4, status: "active" }, { seq: 10, status: "parked" }] },
  ],
  pause_requested: false,
  hard_interrupt_requested: false,
  created_at: "2026-08-29T14:00:00Z",
  updated_at: "2026-08-29T15:03:00Z",
  errorMessage: null,
};

/**
 * Intake decision-ladder fixtures.
 *
 * These mirror `FakeDerivationService` in `src/derivation_api` so a web test
 * drives the same deterministic path the API fixture serves: two frontier
 * questions, one stalled `convergence_required` round with a single pending
 * problem question, then a finalize into `candidate_ready`.
 */
const intakeSectionNames = [
  "purpose",
  "scientific_target",
  "givens_and_starting_point",
  "notation_and_conventions",
  "assumptions_and_regime",
  "scope_and_non_goals",
  "required_output",
  "validation_criteria",
  "agent_discretion",
];

export const fixtureIntakeDeclaredDefaults: IntakeDeclaredDefaultView[] = [
  {
    default_id: "unit-system",
    decision_class: "convention",
    title: "Unit system",
    statement: "Work in SI units throughout.",
    rationale: "Interchangeable with Gaussian units; declared to stay explicit.",
    alternatives: ["Gaussian units"],
  },
  {
    default_id: "line-shape",
    decision_class: "approximation_level",
    title: "Line shape",
    statement: "Start from an ideal delta-function line shape.",
    rationale: "The simplest baseline; broadening is a later ladder rung.",
    alternatives: ["Constant broadening", "State-dependent lifetime"],
  },
];

/** A problem-class default only exists once the user said go. */
export const fixtureIntakeFinalizedDefault: IntakeDeclaredDefaultView = {
  default_id: "target-quantity",
  decision_class: "problem",
  title: "Target quantity",
  statement: "Deliver the direct-transition absorption coefficient only.",
  rationale: "Declared on your behalf when you ended the questioning.",
  alternatives: ["Include phonon-assisted absorption"],
};

export const fixtureIntakeRefinementLadder: IntakeLadderRungView[] = [
  {
    rung: 0,
    name: "Textbook-simplest baseline",
    relaxes: "Nothing; this is the comparable baseline.",
    default_ids: ["unit-system", "line-shape"],
    decision_ids: [],
    parallel_branch: false,
  },
  {
    rung: 1,
    name: "Required deliverable",
    relaxes: "Replaces the ideal line shape with a finite broadening.",
    default_ids: ["line-shape"],
    decision_ids: ["output-form"],
    parallel_branch: true,
  },
];

const intakeQuestion = (
  decisionId: string,
  title: string,
  optionIds: [string, string],
): IntakeQuestionView => ({
  question_id: `question-${decisionId}`,
  decision_id: decisionId,
  semantic_key: `semantic-${decisionId}`,
  title,
  prompt: `Choose ${title.toLowerCase()}.`,
  why_needed: "This choice changes the scientific interpretation.",
  why_it_matters: `Answering ${title.toLowerCase()} changes which quantity the derivation delivers.`,
  answer_mode: "single_choice",
  decision_class: "problem",
  options: [
    { option_id: optionIds[0], label: `Use ${title}`, impact: "Apply it consistently." },
    { option_id: optionIds[1], label: `Avoid ${title}`, impact: "Drop it from the target." },
  ],
  recommended_option_ids: [optionIds[0]],
  recommendation_reason: "It matches the supplied starting point.",
  allow_custom: true,
  depends_on: [],
  blocking: true,
});

export const fixtureIntakeFrontier: IntakeQuestionView[] = [
  intakeQuestion("convention", "Fourier convention", ["minus", "plus"]),
  intakeQuestion("output-form", "output form", ["full-chain", "symbolic"]),
];

/** The independent auditor raised this one, so it quotes what it read. */
export const fixtureIntakePendingQuestion: IntakeQuestionView = {
  ...intakeQuestion("regime", "Included physics", ["direct-only", "with-phonons"]),
  prompt: "Should phonon-assisted transitions be inside the target?",
  answer_mode: "choice_with_text",
  why_it_matters: "Including phonons changes the target quantity itself.",
  grounded_in: "absorption coefficient versus photon energy",
  options: [
    {
      option_id: "direct-only",
      label: "Direct transitions only",
      impact: "Keep the vertical-transition target.",
    },
    {
      option_id: "with-phonons",
      label: "Include phonon-assisted transitions",
      impact: "Widen the target to indirect absorption.",
    },
  ],
  recommended_option_ids: ["direct-only"],
};

function intakeSpecification(
  status: IntakeProblemSpecificationView["status"],
  ladder: boolean,
): IntakeProblemSpecificationView {
  return {
    version: status === "confirmed" ? 3 : 2,
    status,
    supersedes_version: status === "confirmed" ? 2 : 1,
    critical_message_refs: ["event-user"],
    declared_defaults: ladder ? fixtureIntakeDeclaredDefaults : [],
    refinement_ladder: ladder ? fixtureIntakeRefinementLadder : [],
    sections: intakeSectionNames.map((name) => ({
      name,
      content:
        name === "scientific_target"
          ? "Derive the bounded response tensor."
          : `Resolved ${name}`,
      not_applicable_reason: null,
    })),
  };
}

export interface FixtureIntakeSessionOptions {
  /** Renders the post-finalize screen: user-declared defaults, problem class included. */
  finalized?: boolean;
  /** Drops the ladder and defaults so the empty states can be exercised. */
  withoutLadder?: boolean;
  /** Stalls on the round budget with no pending user decision. */
  withoutPending?: boolean;
  /**
   * Renders the screen a failed round leaves behind: the session never moved,
   * and the server is still holding the answers the user already submitted.
   *
   * ``"running"`` is the same row before its round reported back: what a reload
   * finds mid-round, with no failure reason yet.
   */
  heldSubmission?: boolean | "running";
}

export function fixtureIntakeSession(
  status: IntakeSessionView["status"] = "active",
  {
    finalized = false,
    withoutLadder = false,
    withoutPending = false,
    heldSubmission = false,
  }: FixtureIntakeSessionOptions = {},
): IntakeSessionView {
  const ready = status === "candidate_ready" || status === "confirmed";
  const stalled = status === "convergence_required";
  const pending = stalled && !withoutPending;
  const ladder = !withoutLadder && (ready || stalled);
  const specification = ready
    ? intakeSpecification(status === "confirmed" ? "confirmed" : "candidate_ready", ladder)
    : stalled
      ? intakeSpecification("draft", ladder)
      : { ...intakeSpecification("draft", false), version: 1, supersedes_version: null };
  if (finalized && specification.declared_defaults) {
    specification.declared_defaults = [
      fixtureIntakeFinalizedDefault,
      ...specification.declared_defaults,
    ];
  }
  const decisions: IntakeSessionView["decisions"] = fixtureIntakeFrontier.map((item) => ({
    decision_id: item.decision_id,
    semantic_key: item.semantic_key,
    status: ready || stalled ? "resolved" : "open",
    question: item,
    revision: ready || stalled ? 2 : 1,
    supersedes_revision: ready || stalled ? 1 : null,
    answer:
      ready || stalled
        ? {
            selected_option_ids: stalled ? [] : [item.options[0].option_id],
            custom_text: null,
            source_message_refs: ["event-answer"],
            strategy: stalled ? "simplest_first" : null,
          }
        : null,
    reopen_reason: null,
    source_message_refs: [ready || stalled ? "event-answer" : "event-user"],
  }));
  if (pending) {
    decisions.push({
      decision_id: fixtureIntakePendingQuestion.decision_id,
      semantic_key: fixtureIntakePendingQuestion.semantic_key,
      status: "open",
      question: fixtureIntakePendingQuestion,
      revision: 1,
      supersedes_revision: null,
      answer: null,
      reopen_reason: null,
      source_message_refs: ["event-answer"],
    });
  }
  const revision = finalized ? 3 : ready || stalled ? 2 : 1;
  return {
    session_id: "intake-ui",
    revision,
    status,
    model: "gpt-5.4",
    effort: "low",
    service_tier: "fast",
    problem_specifications: [specification],
    decisions,
    frontier: ready || stalled ? [] : fixtureIntakeFrontier,
    pending_problem_questions: pending ? [fixtureIntakePendingQuestion] : [],
    pending_submission: heldSubmission
      ? {
          kind: stalled ? "finalize" : "round",
          base_revision: revision,
          submitted_at: "2026-09-18T09:12:00Z",
          answers: stalled
            ? {
                regime: {
                  selected_option_ids: ["with-phonons"],
                  custom_text: "Keep the phonon-assisted channel.",
                  source_message_refs: ["event-answer"],
                  strategy: null,
                },
              }
            : {
                convention: {
                  selected_option_ids: ["plus"],
                  custom_text: "Use the retarded response.",
                  source_message_refs: ["event-user"],
                  strategy: null,
                },
                "output-form": {
                  selected_option_ids: ["symbolic"],
                  custom_text: null,
                  source_message_refs: ["event-user"],
                  strategy: null,
                },
              },
          user_message: null,
          failure_reason:
            heldSubmission === "running"
              ? null
              : "ValueError: the model declared an undeclared default",
        }
      : null,
    convergence: {
      rounds: status === "active" ? 0 : withoutPending ? 3 : 1,
      audit_rejections: (stalled || finalized) && !withoutPending ? 2 : 0,
      reason: withoutPending
        ? "max_frontier_rounds"
        : stalled || finalized
          ? "max_audit_rejections"
          : null,
      finalized_by_user: finalized,
    },
    thread_generations: [
      {
        generation: 1,
        status: status === "confirmed" ? "closed" : "active",
        app_server_thread_id: "thread-intake-ui",
        prompt_version: "intake-grill-v3",
        replaced_generation: null,
        replacement_reason: null,
      },
    ],
    conversation: [
      {
        event_id: "event-user",
        kind: "user_message",
        payload: { text: "Derive the response tensor from the supplied model." },
      },
      {
        event_id: "event-round",
        kind: "assistant_round",
        payload: {
          summary: "Two independent decisions were presented together.",
          questions: fixtureIntakeFrontier,
        },
      },
    ],
    frozen_problem:
      status === "confirmed"
        ? {
            problem_id: "intake-ui",
            version: 2,
            supersedes_version: 1,
            objective: "Canonical frozen objective",
            givens: ["Canonical givens"],
            assumptions: ["Canonical assumptions"],
            accepted_decisions: ["Canonical decisions"],
            scope: "Canonical scope",
            deliverable: "Canonical deliverable",
            allowed_tools: ["scientific_compute"],
            allowed_references: [],
            success_criteria: ["Canonical success criterion"],
            source_pack: null,
            confirmed_by_user: true,
            declared_defaults: specification.declared_defaults ?? [],
            refinement_ladder: specification.refinement_ladder ?? [],
          }
        : null,
  };
}

/*
 * Stress fixture — open with `?run=stress-run` in fixture mode.
 *
 * Visual review is done against this fixture, never against demo-run: demo-run's
 * ten short titles and one-sentence bodies hide every failure mode a real run
 * shows. It deliberately carries a >120-character question with two same-prefix
 * siblings in the catalog, a title containing `$\psi_n(x)$`, a step with five
 * successors, three levels of branch-of-branch (25 steps), a >20k-character body
 * with several display formulas, a `human_revision` edge, and both a `failed`
 * and a `proposed` route.
 *
 * The subject is a textbook one: the one-dimensional quantum harmonic
 * oscillator, its spectrum and its thermodynamics.
 */
const STRESS_QUESTION_PREFIX =
  "For a one-dimensional quantum harmonic oscillator of mass m and angular frequency omega, derive the energy levels E_n and the normalized eigenfunctions psi_n(x), then use them to obtain the canonical partition function Z(beta), the mean energy and the heat capacity C(T) in closed form";

/** A textbook ladder-operator derivation, used to give the stress body real weight. */
const stressExcerpt = String.raw`With the length scale $\ell\equiv\sqrt{\hbar/(m\omega)}$ and the dimensionless coordinate $\xi\equiv x/\ell$, the Hamiltonian $H=p^2/(2m)+\tfrac12 m\omega^2x^2$ takes the form

$$
H=\frac{\hbar\omega}{2}\left(-\frac{d^2}{d\xi^2}+\xi^2\right).
$$

Define the lowering and raising operators $a=(\xi+d/d\xi)/\sqrt2$ and $a^\dagger=(\xi-d/d\xi)/\sqrt2$. Acting on any smooth test function, $[d/d\xi,\xi]=1$, so the canonical commutator becomes

$$
[a,a^\dagger]=1,\qquad H=\hbar\omega\left(a^\dagger a+\tfrac12\right).
$$

The number operator $N=a^\dagger a$ is Hermitian and non-negative, because $\langle\phi|N|\phi\rangle=\|a\phi\|^2\ge0$. From the commutator, $[N,a]=-a$ and $[N,a^\dagger]=a^\dagger$, so $a$ lowers an eigenvalue of $N$ by one and $a^\dagger$ raises it by one. A chain of lowerings must stop before the eigenvalue turns negative, which forces a state with $a\psi_0=0$; in the coordinate representation this is the first-order equation $(\xi+d/d\xi)\psi_0=0$, whose normalized solution is

$$
\psi_0(x)=\left(\frac{1}{\pi\ell^2}\right)^{1/4}e^{-x^2/(2\ell^2)}.
$$

Applying the raising operator $n$ times and normalizing with $a^\dagger|n\rangle=\sqrt{n+1}\,|n+1\rangle$ gives the whole spectrum,

$$
E_n=\hbar\omega\left(n+\tfrac12\right),\qquad \psi_n(x)=\frac{1}{\sqrt{2^n n!}}\left(\frac{1}{\pi\ell^2}\right)^{1/4}H_n(x/\ell)\,e^{-x^2/(2\ell^2)},
$$

with $H_n$ the physicists' Hermite polynomials. Every level is non-degenerate, the spacing is exactly $\hbar\omega$, and the ground state keeps the zero-point energy $\hbar\omega/2$. Summing the Boltzmann factors is a geometric series,

$$
Z(\beta)=\sum_{n=0}^{\infty}e^{-\beta\hbar\omega(n+1/2)}=\frac{1}{2\sinh(\beta\hbar\omega/2)},
$$

from which $\langle E\rangle=-\partial_\beta\ln Z=\tfrac{\hbar\omega}{2}\coth(\beta\hbar\omega/2)$ and the heat capacity

$$
C(T)=k_B\left(\frac{\hbar\omega}{k_BT}\right)^2\frac{e^{\hbar\omega/k_BT}}{\left(e^{\hbar\omega/k_BT}-1\right)^2}.
$$

It tends to $k_B$ for $k_BT\gg\hbar\omega$ and vanishes exponentially for $k_BT\ll\hbar\omega$, which is where the validity discussion of the thermodynamics step comes from.`;

/** >20k characters: the body scale a long multi-step run reaches. */
const stressLongBody = [
  "The complete derivation record of this step (one textbook derivation, repeated as several sections to stress the reader).",
  ...Array.from({ length: 12 }, (_, index) => `### ${index + 1}. Ladder-operator derivation, pass ${index + 1}\n\n${stressExcerpt}`),
].join("\n\n");

const stressStep = (
  id: string,
  order: number,
  title: string,
  summary: string,
  output: string,
  branchId: string,
  status: DerivationStep["status"] = "sealed",
): DerivationStep => step(id, order, title, "Result of the previous step", summary, output, status, branchId);

export const stressRun: DerivationRun = {
  id: "stress-run",
  question: `${STRESS_QUESTION_PREFIX}; also check the high- and low-temperature limits of C(T), compare the exact levels with the WKB quantization condition, and state where a classical treatment breaks down.`,
  phase: "review_ready_due_to_cap",
  status: "review_ready",
  config: fixtureConfig,
  runtime: fixtureRuntime,
  canonical_event_id: 46,
  read_only: false,
  commands: {
    can_pause: false,
    can_resume: false,
    can_interrupt: false,
    branchable_step_revision_ids: [
      "revision-s-hamiltonian",
      "revision-s-spectrum",
      "revision-s-mean-energy",
      "revision-s-commutator",
    ],
  },
  rootStepId: "s-question",
  budget: { usedSteps: 25, maxSteps: 48 },
  steps: [
    stressStep("s-question", 0, "Research question: spectrum and thermodynamics of the quantum harmonic oscillator", "Turn the request into a derivation with checkable intermediate results.", String.raw`Targets: the levels $E_n$, the eigenfunctions and the heat capacity $C(T)$.`, "branch-root"),
    stressStep("s-units", 1, String.raw`Fix units and scales: $\ell=\sqrt{\hbar/(m\omega)}$, $\xi=x/\ell$, energies in units of $\hbar\omega$`, "Choose the natural length and energy scales so every later formula is dimensionless.", String.raw`All branches measure lengths in $\ell$ and energies in $\hbar\omega$.`, "branch-root"),
    stressStep("s-hamiltonian", 2, "Write the Hamiltonian as a kinetic term plus a quadratic potential", String.raw`State $H=p^2/(2m)+\tfrac12 m\omega^2x^2$ on $L^2(\mathbb R)$ as the common starting point of five treatments.`, "One self-adjoint Hamiltonian; five routes to its spectrum start here.", "branch-root"),

    stressStep("s-ladder", 3, "Introduce lowering and raising operators", String.raw`Factor the Hamiltonian with $a=(\xi+d/d\xi)/\sqrt2$ and its adjoint.`, String.raw`$H=\hbar\omega(a^\dagger a+\tfrac12)$.`, "branch-alg"),
    { ...stressStep("s-commutator", 4, String.raw`Derive $[a,a^\dagger]=1$ and the action of $a$ and $a^\dagger$ on eigenstates of $N$`, stressLongBody, stressLongBody, "branch-alg"), reasoningSummary: stressLongBody, output: stressLongBody },
    stressStep("s-spectrum", 5, String.raw`Spectrum $E_n=\hbar\omega(n+\tfrac12)$ from the lowering chain`, "The chain of lowerings must stop at a state annihilated by a; that fixes the ladder.", "Non-degenerate, equally spaced levels starting at the zero-point energy.", "branch-alg"),

    stressStep("s-ground", 6, String.raw`Ground state from $a\psi_0=0$`, "Solve the first-order equation in the coordinate representation.", String.raw`A Gaussian of width $\ell$.`, "branch-alg1"),
    stressStep("s-excited", 7, String.raw`Excited states $\psi_n(x)$ by repeated raising`, "Apply the raising operator and normalize at each step.", "Hermite polynomials times the ground-state Gaussian.", "branch-alg1"),
    stressStep("s-orthonormal", 8, "Orthonormality and parity of the eigenfunctions", "Check the overlaps with the Hermite generating function.", String.raw`$\langle m|n\rangle=\delta_{mn}$ and $\psi_n(-x)=(-1)^n\psi_n(x)$ on a 400-point quadrature to $10^{-12}$.`, "branch-alg1"),
    stressStep("s-result-a", 9, "Result A: levels and normalized eigenfunctions", "Collect the spectrum and the eigenfunctions.", String.raw`Closed forms for $E_n$ and $\psi_n(x)$, with the quadrature check attached.`, "branch-alg1"),

    stressStep("s-partition", 10, "Canonical partition function as a geometric series", "Sum the Boltzmann factors of the exact levels.", String.raw`$Z(\beta)=1/[2\sinh(\beta\hbar\omega/2)]$.`, "branch-alg2"),
    stressStep("s-mean-energy", 11, String.raw`Mean energy $\langle E\rangle=-\partial_\beta\ln Z$`, "Differentiate the logarithm of the partition function.", String.raw`$\langle E\rangle=\tfrac{\hbar\omega}{2}\coth(\beta\hbar\omega/2)$.`, "branch-alg2"),
    stressStep("s-result-b", 12, "Result B: heat capacity and its two limits", "Differentiate the mean energy with respect to temperature.", String.raw`$C\to k_B$ at high temperature and $C\sim k_B(\hbar\omega/k_BT)^2e^{-\hbar\omega/k_BT}$ at low temperature.`, "branch-alg2"),

    stressStep("s-mean-energy-v2", 13, "Mean energy (revised: the zero-point term is kept explicitly)", "Human revision: an earlier draft of this step dropped the constant ħω/2 before differentiating, which hid it from the energy check.", String.raw`$\langle E\rangle=\tfrac{\hbar\omega}{2}+\hbar\omega/(e^{\beta\hbar\omega}-1)$; the heat capacity is unchanged.`, "branch-alg2r"),
    stressStep("s-result-b2", 14, "Result B, revised: thermodynamics with the zero-point energy shown", "Redo the result on the revised mean energy.", "Same heat capacity; the ground-state energy now appears in the mean energy.", "branch-alg2r"),

    stressStep("s-series", 15, "Hermite equation from the Schrödinger equation", String.raw`Factor out $e^{-\xi^2/2}$ and write the remaining equation for $h(\xi)$.`, String.raw`$h''-2\xi h'+(\epsilon-1)h=0$ with $\epsilon=2E/(\hbar\omega)$.`, "branch-series"),
    stressStep("s-recursion", 16, "Power-series recursion and its termination", "Insert a power series and read off the two-term recursion.", "An infinite series grows like a Gaussian of the wrong sign, so it must terminate.", "branch-series"),
    stressStep("s-result-c", 17, "Result C: quantization from series termination", "Termination forces ε to be an odd integer.", "The same levels as the ladder route; the normalization is still being written up.", "branch-series", "running"),

    stressStep("s-wkb", 18, "WKB route: Bohr–Sommerfeld condition with the Maslov correction", String.raw`Impose $\oint p\,dx=2\pi\hbar(n+\tfrac12)$ on the classical orbit.`, "For a quadratic well the WKB condition gives the exact levels.", "branch-wkb"),
    stressStep("s-result-d", 19, "Result D: WKB is exact for the quadratic well", "Compare with the ladder spectrum.", "Identical levels; the wavefunctions differ from the exact ones near the turning points.", "branch-wkb"),

    stressStep("s-grid", 20, "Numerical route: finite-difference Hamiltonian on a box", "Discretize the Hamiltonian on a uniform grid with hard walls far outside the classical region.", "A sparse symmetric matrix whose lowest eigenvalues approximate the spectrum.", "branch-num", "proposed"),
    stressStep("s-grid-scan", 21, "Convergence in the box size and the grid spacing", "Scan the grid spacing at fixed box size, then the box size.", String.raw`The lowest ten levels agree with $n+\tfrac12$ to about $3\times10^{-4}$ so far; the highest levels still feel the walls.`, "branch-num", "proposed"),
    stressStep("s-result-e", 22, "Result E (pending): numerical cross-check of the spectrum", "Planned as an independent check of the analytic levels.", "Usable once the upper levels are converged.", "branch-num", "proposed"),

    stressStep("s-classical", 23, "Classical route: equipartition for a single oscillator", "Treat x and p as classical variables and integrate over phase space.", String.raw`$Z_{\rm cl}=k_BT/(\hbar\omega)$ and $C=k_B$ at every temperature.`, "branch-classical", "failed"),
    stressStep("s-classical-limit", 24, "Where the classical route fails", "Compare with the quantum heat capacity at low temperature.", "The classical result misses the freeze-out below ħω/k_B and has no zero-point energy; the route is rejected.", "branch-classical", "failed"),
  ],
  edges: [
    { id: "se0", from: "s-question", to: "s-units", order: 0, kind: "continuation" },
    { id: "se1", from: "s-units", to: "s-hamiltonian", order: 1, kind: "continuation" },

    { id: "se2", from: "s-hamiltonian", to: "s-ladder", order: 2, kind: "model_fork" },
    { id: "se3", from: "s-hamiltonian", to: "s-wkb", order: 3, kind: "model_fork" },
    { id: "se4", from: "s-hamiltonian", to: "s-series", order: 4, kind: "model_fork" },
    // Parallel edge for the same (from, to) pair: projection.py can emit these,
    // and the picker must show four-plus-one directions, not six.
    { id: "se4b", from: "s-hamiltonian", to: "s-series", order: 5, kind: "human_direction" },
    { id: "se5", from: "s-hamiltonian", to: "s-classical", order: 6, kind: "model_fork" },
    { id: "se6", from: "s-hamiltonian", to: "s-grid", order: 7, kind: "human_direction" },

    { id: "se7", from: "s-ladder", to: "s-commutator", order: 8, kind: "continuation" },
    { id: "se8", from: "s-commutator", to: "s-spectrum", order: 9, kind: "continuation" },
    { id: "se9", from: "s-spectrum", to: "s-ground", order: 10, kind: "model_fork" },
    { id: "se10", from: "s-spectrum", to: "s-partition", order: 11, kind: "model_fork" },

    { id: "se11", from: "s-ground", to: "s-excited", order: 12, kind: "continuation" },
    { id: "se12", from: "s-excited", to: "s-orthonormal", order: 13, kind: "continuation" },
    { id: "se13", from: "s-orthonormal", to: "s-result-a", order: 14, kind: "continuation" },

    { id: "se14", from: "s-partition", to: "s-mean-energy", order: 15, kind: "continuation" },
    { id: "se15", from: "s-mean-energy", to: "s-result-b", order: 16, kind: "continuation" },
    { id: "se16", from: "s-mean-energy", to: "s-mean-energy-v2", order: 17, kind: "human_revision" },
    { id: "se17", from: "s-mean-energy-v2", to: "s-result-b2", order: 18, kind: "continuation" },

    { id: "se18", from: "s-series", to: "s-recursion", order: 19, kind: "continuation" },
    { id: "se19", from: "s-recursion", to: "s-result-c", order: 20, kind: "continuation" },

    { id: "se20", from: "s-wkb", to: "s-result-d", order: 21, kind: "continuation" },

    { id: "se21", from: "s-grid", to: "s-grid-scan", order: 22, kind: "continuation" },
    { id: "se22", from: "s-grid-scan", to: "s-result-e", order: 23, kind: "continuation" },

    { id: "se23", from: "s-classical", to: "s-classical-limit", order: 24, kind: "continuation" },
  ],
  branches: [
    { branch_id: "branch-root", parent_branch_id: null, anchor_step_revision_id: null, kind: "root", status: "completed", status_history: [{ seq: 1, status: "active" }, { seq: 46, status: "completed" }], step_revision_ids: ["revision-s-question", "revision-s-units", "revision-s-hamiltonian"] },
    { branch_id: "branch-alg", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-s-hamiltonian", kind: "model_fork", status: "completed", status_history: [{ seq: 2, status: "active" }, { seq: 38, status: "completed" }], step_revision_ids: ["revision-s-ladder", "revision-s-commutator", "revision-s-spectrum"] },
    { branch_id: "branch-alg1", parent_branch_id: "branch-alg", anchor_step_revision_id: "revision-s-spectrum", kind: "model_fork", status: "completed", status_history: [{ seq: 11, status: "active" }, { seq: 39, status: "completed" }], step_revision_ids: ["revision-s-ground", "revision-s-excited", "revision-s-orthonormal", "revision-s-result-a"] },
    { branch_id: "branch-alg2", parent_branch_id: "branch-alg", anchor_step_revision_id: "revision-s-spectrum", kind: "model_fork", status: "completed", status_history: [{ seq: 12, status: "active" }, { seq: 40, status: "completed" }], step_revision_ids: ["revision-s-partition", "revision-s-mean-energy", "revision-s-result-b"] },
    { branch_id: "branch-alg2r", parent_branch_id: "branch-alg2", anchor_step_revision_id: "revision-s-mean-energy", kind: "human_revision", status: "completed", status_history: [{ seq: 21, status: "active" }, { seq: 41, status: "completed" }], step_revision_ids: ["revision-s-mean-energy-v2", "revision-s-result-b2"] },
    { branch_id: "branch-series", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-s-hamiltonian", kind: "model_fork", status: "active", status_history: [{ seq: 4, status: "active" }], step_revision_ids: ["revision-s-series", "revision-s-recursion", "revision-s-result-c"] },
    { branch_id: "branch-wkb", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-s-hamiltonian", kind: "model_fork", status: "completed", status_history: [{ seq: 3, status: "active" }, { seq: 29, status: "completed" }], step_revision_ids: ["revision-s-wkb", "revision-s-result-d"] },
    { branch_id: "branch-num", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-s-hamiltonian", kind: "human_direction", status: "parked", status_history: [{ seq: 6, status: "active" }, { seq: 43, status: "parked" }], step_revision_ids: ["revision-s-grid", "revision-s-grid-scan", "revision-s-result-e"] },
    { branch_id: "branch-classical", parent_branch_id: "branch-root", anchor_step_revision_id: "revision-s-hamiltonian", kind: "model_fork", status: "killed", status_history: [{ seq: 5, status: "active" }, { seq: 17, status: "killed" }], step_revision_ids: ["revision-s-classical", "revision-s-classical-limit"] },
  ],
  routes: [
    { id: "sroute-levels", label: "Result A: levels and eigenfunctions from ladder operators", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-ladder", "s-commutator", "s-spectrum", "s-ground", "s-excited", "s-orthonormal", "s-result-a"], status: "complete", branch_id: "branch-alg1", status_history: [{ seq: 11, status: "active" }, { seq: 39, status: "completed" }] },
    { id: "sroute-thermo", label: "Result B: partition function and heat capacity", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-ladder", "s-commutator", "s-spectrum", "s-partition", "s-mean-energy", "s-result-b"], status: "complete", branch_id: "branch-alg2", status_history: [{ seq: 12, status: "active" }, { seq: 40, status: "completed" }] },
    { id: "sroute-thermo-v2", label: "Result B, revised: zero-point energy kept", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-ladder", "s-commutator", "s-spectrum", "s-partition", "s-mean-energy", "s-mean-energy-v2", "s-result-b2"], status: "complete", branch_id: "branch-alg2r", status_history: [{ seq: 21, status: "active" }, { seq: 41, status: "completed" }] },
    { id: "sroute-wkb", label: "Result D: WKB quantization", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-wkb", "s-result-d"], status: "complete", branch_id: "branch-wkb", status_history: [{ seq: 3, status: "active" }, { seq: 29, status: "completed" }] },
    { id: "sroute-series", label: "Result C: Hermite series termination", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-series", "s-recursion", "s-result-c"], status: "active", branch_id: "branch-series", status_history: [{ seq: 4, status: "active" }] },
    { id: "sroute-classical", label: "Classical equipartition (rejected)", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-classical", "s-classical-limit"], status: "failed", branch_id: "branch-classical", status_history: [{ seq: 5, status: "active" }, { seq: 17, status: "killed" }] },
    { id: "sroute-grid", label: "Result E (pending): grid diagonalization", nodeIds: ["s-question", "s-units", "s-hamiltonian", "s-grid", "s-grid-scan", "s-result-e"], status: "proposed", branch_id: "branch-num", status_history: [{ seq: 6, status: "active" }, { seq: 43, status: "parked" }] },
  ],
  pause_requested: false,
  hard_interrupt_requested: false,
  created_at: "2026-09-08T14:00:00Z",
  updated_at: "2026-09-08T17:25:00Z",
  errorMessage: null,
};

/** Same-prefix siblings: run titles in the catalog must stay distinguishable. */
const stressSibling = (id: string, suffix: string, updatedAt: string): DerivationRun => ({
  ...structuredClone(stressRun),
  id,
  question: `${STRESS_QUESTION_PREFIX}${suffix}`,
  updated_at: updatedAt,
});

export const stressRunSiblings: DerivationRun[] = [
  stressSibling("stress-run-exact", "; derive the levels by the ladder operators only and compare them with the WKB condition.", "2026-09-08T17:50:00Z"),
  stressSibling("stress-run-grid", "; focus on the convergence of the grid diagonalization in the box size and the grid spacing.", "2026-09-08T18:15:00Z"),
];

/** Runs the fixture catalog always offers in addition to the active one. */
export const fixtureRunLibrary: DerivationRun[] = [stressRun, ...stressRunSiblings];

const cloneRun = (run: DerivationRun): DerivationRun => structuredClone(run);

export function fixtureProblemPresets(): ProblemPresetsView {
  const base = fixtureIntakeSession("confirmed").frozen_problem!;
  const pack = { pack_id: "fixture-methods", version: "1", sha256: "a".repeat(64) };
  return {
    presets: ["first", "second"].map((kind) => ({
      id: `fixture_${kind}`,
      title: kind === "first" ? "First example" : "Second example",
      description: "Interface fixture only; not a validated scientific benchmark.",
      problem: { ...structuredClone(base), problem_id: `fixture_${kind}`, origin: "direct_spec" as const, confirmed_by_user: false, source_pack: pack },
    })),
    method_source_pack: pack,
    method_references: ["Fixture method A", "Fixture method B"],
    capability_profile: "source_reading_v1",
  };
}

const runSummary = (run: DerivationRun) => ({
  id: run.id,
  question: run.question,
  phase: run.phase,
  status: run.status,
  updated_at: run.updated_at,
  created_at: run.created_at,
  step_count: run.steps.length,
  route_count: run.routes.length,
  read_only: false,
});

/**
 * Scripted live-run handles.
 *
 * Fixture mode is the only place the reading desk's live surfaces can be driven
 * without burning model calls, so the emitter is part of the fixture API rather
 * than of the client contract: `DerivationClient` is unchanged and the handles
 * ride on an intersection type.
 */
export interface FixtureLiveHandles {
  /**
   * Push a new overlay without touching the run.
   *
   * The event carries `run: null` (which `useDerivationRun` tolerates) and does
   * not advance `canonical_event_id`, so it models an overlay-only ticker the
   * backend does not send yet: the client keeps the `RunView` object it already
   * has, and only `overlay` changes identity.
   */
  __emitOverlay: (calls: readonly LiveCall[]) => void;
  /** Seal `step` under `parentId`: appended to steps, edges and the first route. */
  __sealStep: (step: DerivationStep, parentId: string) => DerivationRun;
}

export type FixtureApi = DerivationClient & FixtureLiveHandles;

/**
 * `options.live` starts the active run in a running phase, so a scripted
 * derivation looks like one: the canonical fixture is `review_ready_due_to_cap`,
 * which no live surface would ever render.
 */
export function createFixtureApi(initial: DerivationRun = fixtureRun, options: { live?: boolean } = {}): FixtureApi {
  let run = cloneRun(options.live
    ? {
      ...initial,
      phase: "autonomous_exploration",
      status: "running",
      commands: { ...initial.commands, can_pause: true, can_resume: false, can_interrupt: true },
    }
    : initial);
  let eventId = run.canonical_event_id;
  // Read-only companions in the catalog (stress-run and its two same-prefix
  // siblings). Opening one makes it the active, mutable run.
  const library = new Map<string, DerivationRun>(
    fixtureRunLibrary.filter((candidate) => candidate.id !== initial.id).map((candidate) => [candidate.id, cloneRun(candidate)]),
  );
  const listeners = new Set<{ lastEventId: number; send: (event: RunEvent) => void }>();
  const update = (next: DerivationRun) => {
    eventId += 1;
    run = cloneRun({ ...next, canonical_event_id: eventId });
    for (const listener of listeners) {
      if (eventId > listener.lastEventId) listener.send({ event_id: eventId, type: "run.updated", run_id: run.id, occurred_at: run.updated_at, run: cloneRun(run), overlay: { activeCalls: [], hard_interrupt_requested: run.hard_interrupt_requested } });
    }
    return cloneRun(run);
  };

  const overlayOf = (calls: readonly LiveCall[]): RuntimeOverlay => ({
    activeCalls: calls.map((call) => ({ ...call })),
    hard_interrupt_requested: run.hard_interrupt_requested,
  });

  const emitOverlay = (calls: readonly LiveCall[]) => {
    const overlay = overlayOf(calls);
    // No `eventId` bump and no replay gate: an overlay tick is not a Record event.
    for (const listener of listeners) {
      listener.send({ event_id: eventId, type: "run.updated", run_id: run.id, occurred_at: new Date().toISOString(), run: null, overlay });
    }
  };

  const sealStep = (step: DerivationStep, parentId: string) => {
    eventId += 1;
    const occurredAt = new Date().toISOString();
    run = cloneRun({
      ...run,
      steps: [...run.steps, step],
      edges: [...run.edges, { id: `live-edge-${step.id}`, from: parentId, to: step.id, order: run.edges.length, kind: "continuation" }],
      routes: run.routes.map((route, index) => (index === 0 ? { ...route, nodeIds: [...route.nodeIds, step.id] } : route)),
      canonical_event_id: eventId,
      updated_at: occurredAt,
    });
    const overlay = overlayOf([]);
    for (const listener of listeners) {
      if (eventId > listener.lastEventId) listener.send({ event_id: eventId, type: "step.sealed", run_id: run.id, occurred_at: occurredAt, run: cloneRun(run), overlay });
    }
    return cloneRun(run);
  };

  return {
    __emitOverlay: emitOverlay,
    __sealStep: sealStep,

    async getSiteMode() {
      return { mode: "desktop" as const, channel: "development" as const };
    },
    async getSiteSession() {
      return {
        account: {
          user_id: "fixture-user",
          username: "fixture",
          email: "fixture@example.test",
          role: "user" as const,
          status: "active" as const,
          must_change_password: false,
        },
        idle_expires_at: "2099-12-31T00:00:00Z",
        absolute_expires_at: "2099-12-31T00:00:00Z",
      };
    },
    async loginSite() {
      return this.getSiteSession();
    },
    async logoutSite() {},
    async changeSitePassword() {},
    async listSiteAccounts() {
      return [(await this.getSiteSession()).account];
    },
    async createSiteAccount(request) {
      return {
        user_id: "b".repeat(32),
        username: request.username,
        email: request.email,
        role: request.role ?? "user",
        status: "active" as const,
        must_change_password: false,
      };
    },
    async resetSiteAccountPassword() {},
    async setSiteAccountStatus(userId, request) {
      return {
        user_id: userId,
        username: "fixture",
        email: "fixture@example.test",
        role: "user" as const,
        status: request.status,
        must_change_password: false,
      };
    },
    async listAdminSiteAccountRuns() {
      return this.listRuns();
    },
    async getAdminSiteAccountRun(_userId, runId) {
      return this.getRun(runId);
    },
    async listAdminSiteAccountIntakes() {
      return this.listActiveIntakeSessions();
    },
    async getAdminSiteAccountIntake(_userId, sessionId) {
      return this.getIntakeSession(sessionId);
    },
    onSiteSessionInvalid() {
      return () => undefined;
    },
    async getBuildInfo() {
      return {
        schema_version: "derivationlab-build-info-v1" as const,
        version: "dev",
        build_number: "0",
        release_id: "fixture",
        commit: "0".repeat(40),
        openapi_sha256: "0".repeat(64),
        product_mode: "development" as const,
      };
    },
    async getQuitReadiness() {
      return { safe_to_quit: true, active_run_count: 0 };
    },
    async getAccount() {
      return { status: "signed_in" as const, credential_store: "file" as const, import_available: false, diagnostic: "ready" as const };
    },
    async getAccountRateLimits() {
      return {
        status: "available" as const,
        plan_type: "plus",
        windows: [
          {
            kind: "five_hour" as const,
            used_percent: 18,
            remaining_percent: 82,
            window_duration_mins: 300,
            resets_at: "2026-09-04T20:00:00Z",
          },
          {
            kind: "weekly" as const,
            used_percent: 31,
            remaining_percent: 69,
            window_duration_mins: 10_080,
            resets_at: "2026-09-07T19:21:00Z",
          },
        ],
        observed_at: "2026-09-04T16:00:00Z",
        stale: false,
        diagnostic: "ready" as const,
      };
    },
    async importExistingAccount() {
      return { status: "signed_in" as const, credential_store: "file" as const, import_available: false, diagnostic: "ready" as const };
    },
    async startDeviceLogin() {
      return {
        login_id: "fixture-login",
        verification_url: "https://auth.openai.com/device",
        user_code: "FIXTURE-CODE",
        expires_at: "2099-01-01T00:00:00Z",
      };
    },
    async getDeviceLogin() {
      return { status: "signed_in" as const, diagnostic: null };
    },
    async cancelDeviceLogin() {
      return { status: "canceled" as const };
    },
    async getProblemPresets() {
      return fixtureProblemPresets();
    },
    async getCapabilities() {
      return {
        api_version: "derivation-http-v1",
        command_transport: "http",
        event_transport: "sse",
        idempotency_header: "Idempotency-Key",
        sse_cursor_header: "Last-Event-ID",
        replay_query: "follow=false",
        branch_kinds: ["human_direction", "human_revision"],
        phases: ["submitted", "autonomous_exploration", "human_expansion", "paused", "recovering", "review_ready", "review_ready_due_to_cap", "interrupted", "error"],
        create_run_defaults: structuredClone(fixtureCreateRunDefaults),
      };
    },
    async listRuns() {
      return [runSummary(run), ...[...library.values()].filter((candidate) => candidate.id !== run.id).map(runSummary)];
    },
    async createIntakeSession() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async getIntakeSession() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async listActiveIntakeSessions() {
      return [];
    },
    async submitIntakeRound() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async finalizeIntakeSession() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async confirmIntakeSession() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async cancelIntakeSession() {
      throw new Error("Fixture mode does not simulate AI Problem Intake");
    },
    async createRun(request) {
      return update({ ...cloneRun(fixtureRun), id: `fixture-${Date.now()}`, question: request.problem.objective, config: request.config, runtime: request.runtime, phase: "autonomous_exploration", status: "running", commands: { can_pause: true, can_resume: false, can_interrupt: false, branchable_step_revision_ids: [] } });
    },
    async getRun(runId) {
      if (runId === run.id || runId === "demo-run") return cloneRun(run);
      const target = library.get(runId);
      if (!target) throw new Error(`Fixture run not found: ${runId}`);
      library.set(run.id, cloneRun(run));
      run = cloneRun(target);
      eventId = run.canonical_event_id;
      return cloneRun(run);
    },
    subscribe(_runId, onEvent, _onError, options) {
      const listener = { lastEventId: options?.lastEventId ?? 0, send: onEvent };
      listeners.add(listener);
      options?.onOpen?.();
      return () => {
        listeners.delete(listener);
      };
    },
    async pause() {
      return update({ ...run, phase: "paused", status: "paused", pause_requested: true, commands: { can_pause: false, can_resume: true, can_interrupt: false, branchable_step_revision_ids: [] } });
    },
    async resume() {
      return update({ ...run, phase: "autonomous_exploration", status: "running", pause_requested: false, commands: { can_pause: true, can_resume: false, can_interrupt: true, branchable_step_revision_ids: [] } });
    },
    async interrupt() {
      return update({ ...run, phase: "interrupted", status: "interrupted", hard_interrupt_requested: true, commands: { can_pause: false, can_resume: false, can_interrupt: false, branchable_step_revision_ids: [] } });
    },
    async createBranch(_runId: string, request: CreateBranchRequest) {
      const source = run.steps.find((candidate) => candidate.revisionId === request.from_step_revision_id);
      if (!source || source.status !== "sealed") throw new Error("Branches require a sealed step revision");
      return update({ ...run, phase: "human_expansion", status: "running", commands: { can_pause: true, can_resume: false, can_interrupt: true, branchable_step_revision_ids: [] } });
    },
    async exportReport(runId, request) {
      if (runId !== run.id && runId !== "demo-run") throw new Error(`Fixture run not found: ${runId}`);
      if (!run.routes.some((route) => route.id === request.selected_route_id)) throw new Error("Report route not found");
      return {
        status: "success",
        run_id: run.id,
        selected_route_id: request.selected_route_id,
        export_id: "fixture-report-001",
        bundle_path: `runs/fixture/reports/${run.id}/fixture-report-001`,
        files: ["assets", "compile.log", "manifest.json", "report.pdf", "report.tex"],
        manifest: { schema_version: "derivationlab-report-bundle-v1" },
      };
    },
    reportPdfHref: (runId, exportId) => `/api/runs/${encodeURIComponent(runId)}/reports/${encodeURIComponent(exportId)}/report.pdf`,
  };
}
