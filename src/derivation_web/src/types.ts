/**
 * Compatibility names for UI concepts backed directly by generated API DTOs.
 * No wire shape is declared in this file.
 */
import type {
  CreateBranchRequest,
  CreateRunRequest,
  EdgeView,
  FrozenRunConfig,
  IntakeAnswerInput as IntakeAnswerInputDto,
  IntakeDeclaredDefaultView,
  IntakePendingAnswerView,
  IntakePendingSubmissionView,
  IntakeQuestionView,
  RoleModelConfig,
  RouteView,
  RuntimeConfig,
  RunView,
  StepView,
} from "./api/generated";

export type {
  CapabilitiesView,
  CreateBranchRequest,
  CreateRunDefaultsView,
  CreateRunRequest,
  CreateIntakeSessionRequest,
  ErrorEnvelope,
  ExportReportRequest,
  FinalizeIntakeSessionRequest,
  FrozenProblemInput,
  FrozenRunConfig,
  IntakeAnswerInput,
  IntakeConvergenceView,
  IntakeDeclaredDefaultView,
  IntakeDecisionView,
  IntakeLadderRungView,
  IntakePendingAnswerView,
  IntakePendingSubmissionView,
  IntakeProblemSpecificationView,
  IntakeQuestionView,
  IntakeRevisionRequest,
  IntakeSessionView,
  ReportBundleView,
  RoleModelConfig,
  RouteView,
  RunEvent,
  RunSummary,
  RuntimeConfig,
  RuntimeOverlay,
  RunView,
  StepContent,
  StepView,
  SubmitIntakeRoundRequest,
} from "./api/generated";

export type DerivationRun = RunView;
export type DerivationStep = StepView;
export type DerivationEdge = EdgeView;
export type DerivationRoute = RouteView;
export type RunConfig = FrozenRunConfig;
export type RunRuntime = RuntimeConfig;
export type ModelRoleConfig = RoleModelConfig;
export type ProductModelRoleConfig = RoleModelConfig;
export type ProductCreateRunConfig = FrozenRunConfig;
export type ProductCreateRunRequest = CreateRunRequest;
export type IntakeQuestion = IntakeQuestionView;
export type IntakePendingSubmission = IntakePendingSubmissionView;
export type IntakePendingAnswer = IntakePendingAnswerView;
export type IntakeDecisionClass = IntakeDeclaredDefaultView["decision_class"];
export type AnswerStrategy = NonNullable<IntakeAnswerInputDto["strategy"]>;
export type RunPhase = RunView["phase"];
export type RunStatus = RunView["status"];
export type StepStatus = StepView["status"];
export type BranchKind = CreateBranchRequest["kind"];
export type BranchKindView = RunView["branches"][number]["kind"];
export type BranchStatus = RunView["branches"][number]["status"];
export type StatusHistoryItem = RouteView["status_history"][number];
export type StepChecks = StepView["checks"];
export type StepProvenance = NonNullable<StepView["provenance"]>;
