import type { ZodMiniType } from "zod/mini";
import {
  zAccountApiAccountGetResponse,
  zAccountRateLimitsApiAccountRateLimitsGetResponse,
  zCreateSiteAccountApiSiteAdminAccountsPostResponse,
  zBuildInfoApiBuildInfoGetResponse,
  zCancelDeviceLoginApiAccountDeviceLoginLoginIdCancelPostResponse,
  zCapabilitiesApiCapabilitiesGetResponse,
  zProblemPresetsView,
  zCancelIntakeSessionApiIntakeSessionsSessionIdCancelPostResponse,
  zConfirmIntakeSessionApiIntakeSessionsSessionIdConfirmPostResponse,
  zCreateBranchApiRunsRunIdBranchesPostResponse,
  zCreateIntakeSessionApiIntakeSessionsPostResponse,
  zCreateRunApiRunsPostResponse,
  zErrorEnvelope,
  zExportReportApiRunsRunIdReportsPostResponse,
  zFinalizeIntakeSessionApiIntakeSessionsSessionIdFinalizePostResponse,
  zGetDeviceLoginApiAccountDeviceLoginLoginIdGetResponse,
  zGetAdminSiteAccountIntakeApiSiteAdminAccountsUserIdIntakeSessionsSessionIdGetResponse,
  zGetAdminSiteAccountRunApiSiteAdminAccountsUserIdRunsRunIdGetResponse,
  zGetIntakeSessionApiIntakeSessionsSessionIdGetResponse,
  zGetRunApiRunsRunIdGetResponse,
  zImportExistingAccountApiAccountImportExistingPostResponse,
  zInterruptRunApiRunsRunIdInterruptPostResponse,
  zListRunsApiRunsGetResponse,
  zListAdminSiteAccountIntakesApiSiteAdminAccountsUserIdIntakeSessionsGetResponse,
  zListAdminSiteAccountRunsApiSiteAdminAccountsUserIdRunsGetResponse,
  zListSiteAccountsApiSiteAdminAccountsGetResponse,
  zListIntakeSessionsApiIntakeSessionsGetResponse,
  zPauseRunApiRunsRunIdPausePostResponse,
  zQuitReadinessApiDesktopQuitReadinessGetResponse,
  zResumeRunApiRunsRunIdResumePostResponse,
  zRunEvent,
  zSetSiteAccountStatusApiSiteAdminAccountsUserIdStatusPostResponse,
  zCreateSiteSessionApiSiteSessionPostResponse,
  zGetSiteSessionApiSiteSessionGetResponse,
  zSiteModeApiSiteModeGetResponse,
  zStartDeviceLoginApiAccountDeviceLoginStartPostResponse,
  zSubmitIntakeRoundApiIntakeSessionsSessionIdRoundsPostResponse,
} from "./generated/zod.gen";
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
  ErrorEnvelope,
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
  SiteAccountView,
  SitePasswordChangeRequest,
  SiteSessionView,
  SubmitIntakeRoundRequest,
} from "./generated";
import {
  assertDeviceLoginSemantics,
  assertReportSemantics,
  assertRunEventSemantics,
  assertRunSemantics,
  ContractMismatchError,
  parseContract,
} from "./contract";
import type { DerivationClient, SubscriptionOptions } from "./client";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown | null;
  readonly request_id: string;

  constructor(status: number, code: string, message: string, details: unknown | null, requestId: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.request_id = requestId;
  }
}

async function apiErrorFrom(response: Response, endpoint: string): Promise<ApiError> {
  const text = await response.text();
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return new ApiError(
      response.status,
      "http_error",
      `Request failed (HTTP ${response.status})`,
      null,
      response.headers.get("X-Request-ID") ?? "unknown",
    );
  }
  const envelope = parseContract<ErrorEnvelope>(zErrorEnvelope, value, endpoint, "ErrorEnvelope", {
    requestId: response.headers.get("X-Request-ID") ?? undefined,
  });
  return new ApiError(response.status, envelope.error.code, envelope.error.message, envelope.error.details, envelope.request_id);
}

interface HttpClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
  eventSource?: typeof EventSource;
}

export class HttpDerivationClient implements DerivationClient {
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;
  readonly #EventSource: typeof EventSource;
  readonly #sessionInvalidListeners = new Set<() => void>();

  constructor({ baseUrl = "", fetch: fetchImpl = globalThis.fetch.bind(globalThis), eventSource = globalThis.EventSource }: HttpClientOptions = {}) {
    this.#baseUrl = baseUrl;
    this.#fetch = fetchImpl;
    this.#EventSource = eventSource;
  }

  async #requestJson<T>(path: string, schema: ZodMiniType<T>, schemaName: string, init?: RequestInit, networkRetries = 0): Promise<T> {
    const endpoint = `${this.#baseUrl}${path}`;
    for (let attempt = 0; ; attempt += 1) {
      try {
        const response = await this.#fetch(endpoint, {
          ...init,
          credentials: "include",
          headers: { "Content-Type": "application/json", ...init?.headers },
        });
        if (!response.ok) {
          const error = await apiErrorFrom(response, endpoint);
          if (error.status === 401) this.#notifySiteSessionInvalid();
          throw error;
        }
        const value: unknown = await response.json();
        return parseContract(schema, value, endpoint, schemaName, {
          requestId: response.headers.get("X-Request-ID") ?? undefined,
        });
      } catch (error) {
        if (!(error instanceof TypeError) || attempt >= networkRetries) throw error;
      }
    }
  }

  async #requestVoid(path: string, init: RequestInit): Promise<void> {
    const endpoint = `${this.#baseUrl}${path}`;
    const response = await this.#fetch(endpoint, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    if (!response.ok) {
      const error = await apiErrorFrom(response, endpoint);
      if (error.status === 401) this.#notifySiteSessionInvalid();
      throw error;
    }
  }

  #notifySiteSessionInvalid(): void {
    for (const listener of this.#sessionInvalidListeners) listener();
  }

  onSiteSessionInvalid(listener: () => void): () => void {
    this.#sessionInvalidListeners.add(listener);
    return () => this.#sessionInvalidListeners.delete(listener);
  }

  getSiteMode(): Promise<SiteModeView> {
    return this.#requestJson("/api/site/mode", zSiteModeApiSiteModeGetResponse, "SiteModeView");
  }

  getSiteSession(): Promise<SiteSessionView> {
    return this.#requestJson("/api/site/session", zGetSiteSessionApiSiteSessionGetResponse, "SiteSessionView");
  }

  loginSite(body: SiteLoginRequest): Promise<SiteSessionView> {
    return this.#requestJson("/api/site/session", zCreateSiteSessionApiSiteSessionPostResponse, "SiteSessionView", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  logoutSite(): Promise<void> {
    return this.#requestVoid("/api/site/session", { method: "DELETE" });
  }

  changeSitePassword(body: SitePasswordChangeRequest): Promise<void> {
    return this.#requestVoid("/api/site/password", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listSiteAccounts(): Promise<SiteAccountView[]> {
    return this.#requestJson("/api/site/admin/accounts", zListSiteAccountsApiSiteAdminAccountsGetResponse, "SiteAccountView[]");
  }

  createSiteAccount(body: AdminCreateSiteAccountRequest): Promise<SiteAccountView> {
    return this.#requestJson("/api/site/admin/accounts", zCreateSiteAccountApiSiteAdminAccountsPostResponse, "SiteAccountView", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  resetSiteAccountPassword(userId: string, body: AdminResetSitePasswordRequest): Promise<void> {
    return this.#requestVoid(`/api/site/admin/accounts/${encodeURIComponent(userId)}/password-reset`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  setSiteAccountStatus(userId: string, body: AdminSetSiteAccountStatusRequest): Promise<SiteAccountView> {
    const path = `/api/site/admin/accounts/${encodeURIComponent(userId)}/status`;
    return this.#requestJson(path, zSetSiteAccountStatusApiSiteAdminAccountsUserIdStatusPostResponse, "SiteAccountView", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  listAdminSiteAccountRuns(userId: string): Promise<RunSummary[]> {
    const path = `/api/site/admin/accounts/${encodeURIComponent(userId)}/runs`;
    return this.#requestJson(path, zListAdminSiteAccountRunsApiSiteAdminAccountsUserIdRunsGetResponse, "RunSummary[]");
  }

  async getAdminSiteAccountRun(userId: string, runId: string): Promise<RunView> {
    const path = `/api/site/admin/accounts/${encodeURIComponent(userId)}/runs/${encodeURIComponent(runId)}`;
    const run = await this.#requestJson(path, zGetAdminSiteAccountRunApiSiteAdminAccountsUserIdRunsRunIdGetResponse, "RunView");
    return assertRunSemantics(run, `${this.#baseUrl}${path}`, { runId });
  }

  listAdminSiteAccountIntakes(userId: string): Promise<IntakeSessionView[]> {
    const path = `/api/site/admin/accounts/${encodeURIComponent(userId)}/intake/sessions`;
    return this.#requestJson(path, zListAdminSiteAccountIntakesApiSiteAdminAccountsUserIdIntakeSessionsGetResponse, "IntakeSessionView[]");
  }

  getAdminSiteAccountIntake(userId: string, sessionId: string): Promise<IntakeSessionView> {
    const path = `/api/site/admin/accounts/${encodeURIComponent(userId)}/intake/sessions/${encodeURIComponent(sessionId)}`;
    return this.#requestJson(path, zGetAdminSiteAccountIntakeApiSiteAdminAccountsUserIdIntakeSessionsSessionIdGetResponse, "IntakeSessionView");
  }

  #commandJson<T>(path: string, schema: ZodMiniType<T>, schemaName: string, init: RequestInit): Promise<T> {
    const idempotencyKey = crypto.randomUUID();
    return this.#requestJson(path, schema, schemaName, {
      ...init,
      headers: { ...init.headers, "Idempotency-Key": idempotencyKey },
    }, 1);
  }

  getBuildInfo(): Promise<BuildInfoView> {
    return this.#requestJson("/api/build-info", zBuildInfoApiBuildInfoGetResponse, "BuildInfoView");
  }

  getQuitReadiness(): Promise<QuitReadinessView> {
    return this.#requestJson("/api/desktop/quit-readiness", zQuitReadinessApiDesktopQuitReadinessGetResponse, "QuitReadinessView");
  }

  getAccount(): Promise<AccountView> {
    return this.#requestJson("/api/account", zAccountApiAccountGetResponse, "AccountView");
  }

  getAccountRateLimits(): Promise<AccountRateLimitsView> {
    return this.#requestJson(
      "/api/account/rate-limits",
      zAccountRateLimitsApiAccountRateLimitsGetResponse,
      "AccountRateLimitsView",
    );
  }

  importExistingAccount(body: ImportExistingAccountRequest): Promise<AccountView> {
    return this.#commandJson("/api/account/import-existing", zImportExistingAccountApiAccountImportExistingPostResponse, "AccountView", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  startDeviceLogin(): Promise<DeviceLoginStartView> {
    const path = "/api/account/device-login/start";
    return this.#requestJson(path, zStartDeviceLoginApiAccountDeviceLoginStartPostResponse, "DeviceLoginStartView", { method: "POST" })
      .then((login) => assertDeviceLoginSemantics(login, `${this.#baseUrl}${path}`));
  }

  getDeviceLogin(loginId: string): Promise<DeviceLoginStatusView> {
    const path = `/api/account/device-login/${encodeURIComponent(loginId)}`;
    return this.#requestJson(path, zGetDeviceLoginApiAccountDeviceLoginLoginIdGetResponse, "DeviceLoginStatusView");
  }

  cancelDeviceLogin(loginId: string): Promise<DeviceLoginCancelView> {
    const path = `/api/account/device-login/${encodeURIComponent(loginId)}/cancel`;
    return this.#requestJson(path, zCancelDeviceLoginApiAccountDeviceLoginLoginIdCancelPostResponse, "DeviceLoginCancelView", { method: "POST" });
  }

  getCapabilities(): Promise<CapabilitiesView> {
    return this.#requestJson("/api/capabilities", zCapabilitiesApiCapabilitiesGetResponse, "CapabilitiesView");
  }

  getProblemPresets(): Promise<ProblemPresetsView> {
    const path = "/api/problem-presets";
    return this.#requestJson(path, zProblemPresetsView, "ProblemPresetsView").then((value) => {
      const issues = value.presets.flatMap((preset, index) =>
        preset.problem.origin !== "direct_spec" || preset.problem.confirmed_by_user !== false
          ? [{ path: `presets.${index}.problem`, message: "a prepared direct problem must declare direct_spec without human confirmation" }]
          : []);
      if (issues.length) throw new ContractMismatchError(`${this.#baseUrl}${path}`, "ProblemPresetsView semantics", issues);
      return value;
    });
  }

  listRuns(): Promise<RunSummary[]> {
    return this.#requestJson("/api/runs", zListRunsApiRunsGetResponse, "RunSummary[]");
  }

  createIntakeSession(body: CreateIntakeSessionRequest): Promise<IntakeSessionView> {
    return this.#commandJson("/api/intake/sessions", zCreateIntakeSessionApiIntakeSessionsPostResponse, "IntakeSessionView", { method: "POST", body: JSON.stringify(body) });
  }

  getIntakeSession(sessionId: string): Promise<IntakeSessionView> {
    const path = `/api/intake/sessions/${encodeURIComponent(sessionId)}`;
    return this.#requestJson(path, zGetIntakeSessionApiIntakeSessionsSessionIdGetResponse, "IntakeSessionView");
  }

  async listActiveIntakeSessions(): Promise<IntakeSessionView[]> {
    const resumable = await Promise.all(
      (["active", "convergence_required", "candidate_ready"] as const).map((status) =>
        this.#requestJson(`/api/intake/sessions?status=${status}`, zListIntakeSessionsApiIntakeSessionsGetResponse, "IntakeSessionView[]"),
      ),
    );
    return resumable.flat();
  }

  submitIntakeRound(sessionId: string, body: SubmitIntakeRoundRequest): Promise<IntakeSessionView> {
    const path = `/api/intake/sessions/${encodeURIComponent(sessionId)}/rounds`;
    return this.#commandJson(path, zSubmitIntakeRoundApiIntakeSessionsSessionIdRoundsPostResponse, "IntakeSessionView", { method: "POST", body: JSON.stringify(body) });
  }

  finalizeIntakeSession(sessionId: string, body: FinalizeIntakeSessionRequest): Promise<IntakeSessionView> {
    const path = `/api/intake/sessions/${encodeURIComponent(sessionId)}/finalize`;
    return this.#commandJson(path, zFinalizeIntakeSessionApiIntakeSessionsSessionIdFinalizePostResponse, "IntakeSessionView", { method: "POST", body: JSON.stringify(body) });
  }

  confirmIntakeSession(sessionId: string, body: IntakeRevisionRequest): Promise<IntakeSessionView> {
    const path = `/api/intake/sessions/${encodeURIComponent(sessionId)}/confirm`;
    return this.#commandJson(path, zConfirmIntakeSessionApiIntakeSessionsSessionIdConfirmPostResponse, "IntakeSessionView", { method: "POST", body: JSON.stringify(body) });
  }

  cancelIntakeSession(sessionId: string, body: IntakeRevisionRequest): Promise<IntakeSessionView> {
    const path = `/api/intake/sessions/${encodeURIComponent(sessionId)}/cancel`;
    return this.#commandJson(path, zCancelIntakeSessionApiIntakeSessionsSessionIdCancelPostResponse, "IntakeSessionView", { method: "POST", body: JSON.stringify(body) });
  }

  async createRun(body: CreateRunRequest): Promise<RunView> {
    const run = await this.#commandJson("/api/runs", zCreateRunApiRunsPostResponse, "RunView", { method: "POST", body: JSON.stringify(body) });
    return assertRunSemantics(run, `${this.#baseUrl}/api/runs`, { runId: run.id });
  }

  async getRun(runId: string): Promise<RunView> {
    const path = `/api/runs/${encodeURIComponent(runId)}`;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        reject(new ApiError(0, "request_timeout", "Loading the run timed out. Please reload.", null, "unknown"));
        controller.abort();
      }, 20_000);
    });
    try {
      const run = await Promise.race([
        this.#requestJson(path, zGetRunApiRunsRunIdGetResponse, "RunView", { signal: controller.signal }),
        timeout,
      ]);
      return assertRunSemantics(run, `${this.#baseUrl}${path}`, { runId });
    } finally {
      clearTimeout(timer);
    }
  }

  subscribe(runId: string, onEvent: (event: RunEvent) => void, onError: (error: Error) => void, options?: SubscriptionOptions): () => void {
    const after = options?.lastEventId === undefined ? "" : `?after=${encodeURIComponent(options.lastEventId)}`;
    const endpoint = `${this.#baseUrl}/api/runs/${encodeURIComponent(runId)}/events${after}`;
    const source = new this.#EventSource(endpoint, { withCredentials: true });
    source.onopen = () => options?.onOpen?.();
    source.onmessage = (message) => {
      try {
        const raw: unknown = JSON.parse(message.data);
        const event = parseContract(zRunEvent, raw, endpoint, "RunEvent", { runId, eventId: message.lastEventId || undefined });
        onEvent(assertRunEventSemantics(event, runId, endpoint, { runId, eventId: String(event.event_id) }));
      } catch (error) {
        if (error instanceof ContractMismatchError) onError(error);
        else onError(new ContractMismatchError(endpoint, "RunEvent JSON", [{ path: "$", message: "invalid JSON" }], { runId, eventId: message.lastEventId || undefined }));
      }
    };
    source.onerror = () => {
      void this.getSiteSession()
        .then(() => onError(new Error("Run event stream disconnected")))
        .catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) {
            source.close();
            return;
          }
          onError(new Error("Run event stream disconnected"));
        });
    };
    return () => source.close();
  }

  async pause(runId: string): Promise<RunView> {
    return this.#runCommand(runId, "pause", zPauseRunApiRunsRunIdPausePostResponse);
  }

  async resume(runId: string): Promise<RunView> {
    return this.#runCommand(runId, "resume", zResumeRunApiRunsRunIdResumePostResponse);
  }

  async interrupt(runId: string): Promise<RunView> {
    return this.#runCommand(runId, "interrupt", zInterruptRunApiRunsRunIdInterruptPostResponse);
  }

  async #runCommand(runId: string, command: string, schema: ZodMiniType<RunView>): Promise<RunView> {
    const path = `/api/runs/${encodeURIComponent(runId)}/${command}`;
    const run = await this.#commandJson(path, schema, "RunView", { method: "POST" });
    return assertRunSemantics(run, `${this.#baseUrl}${path}`, { runId });
  }

  async createBranch(runId: string, body: CreateBranchRequest): Promise<RunView> {
    const path = `/api/runs/${encodeURIComponent(runId)}/branches`;
    const run = await this.#commandJson(path, zCreateBranchApiRunsRunIdBranchesPostResponse, "RunView", { method: "POST", body: JSON.stringify(body) });
    return assertRunSemantics(run, `${this.#baseUrl}${path}`, { runId });
  }

  async exportReport(runId: string, body: ExportReportRequest): Promise<ReportBundleView> {
    const path = `/api/runs/${encodeURIComponent(runId)}/reports`;
    const report = await this.#requestJson(path, zExportReportApiRunsRunIdReportsPostResponse, "ReportBundleView", { method: "POST", body: JSON.stringify(body) });
    return assertReportSemantics(report, runId, `${this.#baseUrl}${path}`);
  }

  reportPdfHref(runId: string, exportId: string): string {
    return `${this.#baseUrl}/api/runs/${encodeURIComponent(runId)}/reports/${encodeURIComponent(exportId)}/report.pdf`;
  }
}

export function createHttpClient(baseUrl = ""): DerivationClient {
  return new HttpDerivationClient({ baseUrl });
}
