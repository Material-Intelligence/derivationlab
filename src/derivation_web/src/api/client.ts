import type {
  AccountRateLimitsView,
  AccountView,
  AdminCreateSiteAccountRequest,
  AdminResetSitePasswordRequest,
  AdminSetSiteAccountStatusRequest,
  BuildInfoView,
  CapabilitiesView,
  CreateBranchRequest,
  CreateIntakeSessionRequest,
  CreateRunRequest,
  DeviceLoginCancelView,
  DeviceLoginStartView,
  DeviceLoginStatusView,
  ExportReportRequest,
  FinalizeIntakeSessionRequest,
  ImportExistingAccountRequest,
  IntakeRevisionRequest,
  IntakeSessionView,
  ProblemPresetsView,
  QuitReadinessView,
  ReportBundleView,
  RunEvent,
  RunSummary,
  RunView,
  SiteLoginRequest,
  SiteModeView,
  SitePasswordChangeRequest,
  SiteAccountView,
  SiteSessionView,
  SubmitIntakeRoundRequest,
} from "./generated";

export interface SubscriptionOptions {
  lastEventId?: number;
  onOpen?: () => void;
}

export interface DerivationClient {
  getSiteMode(): Promise<SiteModeView>;
  getSiteSession(): Promise<SiteSessionView>;
  loginSite(request: SiteLoginRequest): Promise<SiteSessionView>;
  logoutSite(): Promise<void>;
  changeSitePassword(request: SitePasswordChangeRequest): Promise<void>;
  listSiteAccounts(): Promise<SiteAccountView[]>;
  createSiteAccount(request: AdminCreateSiteAccountRequest): Promise<SiteAccountView>;
  resetSiteAccountPassword(userId: string, request: AdminResetSitePasswordRequest): Promise<void>;
  setSiteAccountStatus(userId: string, request: AdminSetSiteAccountStatusRequest): Promise<SiteAccountView>;
  listAdminSiteAccountRuns(userId: string): Promise<RunSummary[]>;
  getAdminSiteAccountRun(userId: string, runId: string): Promise<RunView>;
  listAdminSiteAccountIntakes(userId: string): Promise<IntakeSessionView[]>;
  getAdminSiteAccountIntake(userId: string, sessionId: string): Promise<IntakeSessionView>;
  onSiteSessionInvalid(listener: () => void): () => void;
  getBuildInfo(): Promise<BuildInfoView>;
  getQuitReadiness(): Promise<QuitReadinessView>;
  getAccount(): Promise<AccountView>;
  getAccountRateLimits(): Promise<AccountRateLimitsView>;
  importExistingAccount(request: ImportExistingAccountRequest): Promise<AccountView>;
  startDeviceLogin(): Promise<DeviceLoginStartView>;
  getDeviceLogin(loginId: string): Promise<DeviceLoginStatusView>;
  cancelDeviceLogin(loginId: string): Promise<DeviceLoginCancelView>;
  getCapabilities(): Promise<CapabilitiesView>;
  getProblemPresets(): Promise<ProblemPresetsView>;
  listRuns(): Promise<RunSummary[]>;
  createIntakeSession(request: CreateIntakeSessionRequest): Promise<IntakeSessionView>;
  getIntakeSession(sessionId: string): Promise<IntakeSessionView>;
  listActiveIntakeSessions(): Promise<IntakeSessionView[]>;
  submitIntakeRound(sessionId: string, request: SubmitIntakeRoundRequest): Promise<IntakeSessionView>;
  finalizeIntakeSession(sessionId: string, request: FinalizeIntakeSessionRequest): Promise<IntakeSessionView>;
  confirmIntakeSession(sessionId: string, request: IntakeRevisionRequest): Promise<IntakeSessionView>;
  cancelIntakeSession(sessionId: string, request: IntakeRevisionRequest): Promise<IntakeSessionView>;
  createRun(request: CreateRunRequest): Promise<RunView>;
  getRun(runId: string): Promise<RunView>;
  subscribe(runId: string, onEvent: (event: RunEvent) => void, onError: (error: Error) => void, options?: SubscriptionOptions): () => void;
  pause(runId: string): Promise<RunView>;
  resume(runId: string): Promise<RunView>;
  interrupt(runId: string): Promise<RunView>;
  createBranch(runId: string, request: CreateBranchRequest): Promise<RunView>;
  exportReport(runId: string, request: ExportReportRequest): Promise<ReportBundleView>;
  reportPdfHref(runId: string, exportId: string): string;
}
