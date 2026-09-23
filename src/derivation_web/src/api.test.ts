import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, ContractMismatchError, createHttpClient } from "./api";
import { fixtureCreateRunDefaults, fixtureProblemPresets, fixtureRun } from "./fixtures";

class FakeEventSource {
  static urls: string[] = [];
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(public readonly url: string) {
    FakeEventSource.urls.push(url);
    FakeEventSource.instances.push(this);
  }
  close = vi.fn();
}

describe("HTTP event replay adapter", () => {
  it("bounds stalled run detail reads and aborts the fetch", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const result = expect(createHttpClient().getRun("demo-run")).rejects.toMatchObject({ code: "request_timeout" });
      await vi.advanceTimersByTimeAsync(20_000);
      await result;
      expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
  afterEach(() => {
    FakeEventSource.urls = [];
    FakeEventSource.instances = [];
    vi.unstubAllGlobals();
  });

  it("requests only the suffix after the canonical snapshot event", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const stop = createHttpClient("http://localhost:8000").subscribe(
      "run 1",
      () => undefined,
      () => undefined,
      { lastEventId: 42 },
    );
    expect(FakeEventSource.urls).toEqual(["http://localhost:8000/api/runs/run%201/events?after=42"]);
    stop();
  });

  it("reads the run catalog from the collection endpoint", async () => {
    const summaries = [{ id: "run-1", question: "Q", status: "running", phase: "submitted", updated_at: "2026-08-30T00:00:00Z", created_at: "2026-08-30T00:00:00Z", step_count: 0, route_count: 0, read_only: false }];
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(summaries), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(createHttpClient("http://localhost:8000").listRuns()).resolves.toEqual(summaries);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/runs", expect.objectContaining({ headers: expect.any(Object) }));
  });

  it("uses credentialed cookie requests for website login and logout", async () => {
    const session = {
      account: {
        user_id: "a".repeat(32),
        username: "alice",
        email: "alice@example.test",
        role: "user",
        status: "active",
        must_change_password: false,
      },
      idle_expires_at: "2027-01-15T08:00:00Z",
      absolute_expires_at: "2027-01-16T08:00:00Z",
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(session), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createHttpClient();

    await expect(client.loginSite({ identifier: "alice", password: "private-password" })).resolves.toEqual(session);
    await expect(client.logoutSite()).resolves.toBeUndefined();

    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST", credentials: "include" });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "DELETE", credentials: "include" });
  });

  it("notifies the application when a protected request loses its session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "site_session_invalid", message: "Sign in again", details: null },
      request_id: "request-session",
    }), { status: 401 })));
    const client = createHttpClient();
    const invalidated = vi.fn();
    client.onSiteSessionInvalid(invalidated);

    await expect(client.listRuns()).rejects.toMatchObject({ status: 401 });
    expect(invalidated).toHaveBeenCalledOnce();
  });

  it("reads backend-owned create-run defaults from capabilities", async () => {
    const payload = {
      api_version: "derivation-http-v1",
      command_transport: "http",
      event_transport: "sse",
      idempotency_header: "Idempotency-Key",
      sse_cursor_header: "Last-Event-ID",
      replay_query: "follow=false",
      branch_kinds: ["human_direction", "human_revision"],
      phases: ["submitted"],
      create_run_defaults: fixtureCreateRunDefaults,
      future_field: "tolerated",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })));
    await expect(createHttpClient().getCapabilities()).resolves.toMatchObject({
      create_run_defaults: fixtureCreateRunDefaults,
    });
  });

  it("loads direct problem presets and rejects fabricated human confirmation", async () => {
    const payload = fixtureProblemPresets();
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(createHttpClient().getProblemPresets()).resolves.toMatchObject(payload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/problem-presets");
    payload.presets[0].problem.confirmed_by_user = true;
    fetchMock.mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    await expect(createHttpClient().getProblemPresets()).rejects.toBeInstanceOf(ContractMismatchError);
  });

  it("validates account responses and imports with one retry-safe idempotency key", async () => {
    const account = { status: "signed_in", credential_store: "file", import_available: false, diagnostic: "ready" };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(account), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "33333333-3333-4333-8333-333333333333" });

    await expect(createHttpClient().importExistingAccount({ confirm_import: true })).resolves.toEqual(account);
    const [, init] = fetchMock.mock.calls[0];
    expect(init).toMatchObject({ method: "POST", body: JSON.stringify({ confirm_import: true }) });
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("33333333-3333-4333-8333-333333333333");
  });

  it("rejects an unsafe device-login URL at the client boundary", async () => {
    const login = { login_id: "login-1", verification_url: "https://evil.example/device", user_code: "SAFE-CODE", expires_at: "2026-09-01T00:00:00Z" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(login), { status: 200 })));
    await expect(createHttpClient().startDeviceLogin()).rejects.toMatchObject({
      name: "ContractMismatchError",
      issues: [expect.objectContaining({ path: "verification_url" })],
    });
  });

  it("rejects a malformed success DTO at the HTTP boundary", async () => {
    const malformed = [{ id: "run-1", question: "Q", status: "running", phase: "submitted" }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));
    await expect(createHttpClient().listRuns()).rejects.toMatchObject({
      name: "ContractMismatchError",
      endpoint: "/api/runs",
      schema: "RunSummary[]",
    });
  });

  it("rejects a semantically invalid route instead of deriving one", async () => {
    const malformed = structuredClone(fixtureRun);
    malformed.routes[0].nodeIds = [malformed.steps[0].id, "missing-node"];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));
    await expect(createHttpClient().getRun(malformed.id)).rejects.toBeInstanceOf(ContractMismatchError);
  });

  it("rejects a RunView for a different requested run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(fixtureRun), { status: 200 })));
    await expect(createHttpClient().getRun("different-run")).rejects.toMatchObject({
      name: "ContractMismatchError",
      issues: [expect.objectContaining({ path: "id" })],
    });
  });

  it("rejects contradictory command affordances", async () => {
    const malformed = structuredClone(fixtureRun);
    malformed.read_only = true;
    malformed.commands.can_pause = true;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));
    await expect(createHttpClient().getRun(malformed.id)).rejects.toMatchObject({
      name: "ContractMismatchError",
      issues: expect.arrayContaining([expect.objectContaining({ path: "commands" })]),
    });
  });

  it("rejects an unknown branchable step revision", async () => {
    const malformed = structuredClone(fixtureRun);
    malformed.commands.branchable_step_revision_ids = ["revision-missing"];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));
    await expect(createHttpClient().getRun(malformed.id)).rejects.toMatchObject({
      name: "ContractMismatchError",
      issues: expect.arrayContaining([expect.objectContaining({ path: "commands.branchable_step_revision_ids.0" })]),
    });
  });

  it("rejects a malformed SSE event before calling application state", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    const onError = vi.fn();
    createHttpClient().subscribe("demo-run", onEvent, onError);
    FakeEventSource.instances[0].onmessage?.(new MessageEvent("message", { data: JSON.stringify({ event_id: 1, type: "run.updated" }) }));
    expect(onEvent).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.any(ContractMismatchError));
  });

  it("rejects an SSE event for a different run before calling application state", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    const onError = vi.fn();
    createHttpClient().subscribe("demo-run", onEvent, onError);
    FakeEventSource.instances[0].onmessage?.(new MessageEvent("message", { data: JSON.stringify({
      event_id: 11,
      type: "run.updated",
      run_id: "different-run",
      occurred_at: "2026-08-31T12:00:00Z",
      run: fixtureRun,
      overlay: { hard_interrupt_requested: false, activeCalls: [] },
    }) }));
    expect(onEvent).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ name: "ContractMismatchError" }));
  });

  it("rejects malformed JSON error envelopes as contract mismatches", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: "missing code" } }), { status: 409 })));
    await expect(createHttpClient().resume("demo-run")).rejects.toBeInstanceOf(ContractMismatchError);
  });

  it("creates and advances one persistent intake session", async () => {
    const response = { session_id: "intake-1", revision: 1, status: "active", model: "gpt-5.4", effort: "low", problem_specifications: [], decisions: [], frontier: [], thread_generations: [], conversation: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "44444444-4444-4444-8444-444444444444" });
    const request = { initial_message: "Initial problem", model: "gpt-5.4", effort: "low" } as const;
    await expect(createHttpClient().createIntakeSession(request)).resolves.toMatchObject(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions", expect.objectContaining({ method: "POST", body: JSON.stringify(request) }));

    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(response), { status: 200, headers: { "Content-Type": "application/json" } }));
    const round = { base_revision: 1, answers: { convention: { selected_option_ids: [], custom_text: null, strategy: "simplest_first" as const } }, user_message: null };
    await expect(createHttpClient().submitIntakeRound("intake-1", round)).resolves.toMatchObject(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions/intake-1/rounds", expect.objectContaining({ method: "POST", body: JSON.stringify(round) }));
  });

  it("finalizes a stalled intake session under one idempotency key", async () => {
    const finalized = { session_id: "intake-1", revision: 3, status: "candidate_ready", model: "gpt-5.4", effort: "low", problem_specifications: [], decisions: [], frontier: [], pending_problem_questions: [], convergence: { rounds: 1, audit_rejections: 2, reason: "max_audit_rejections", finalized_by_user: true }, thread_generations: [], conversation: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(finalized), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "55555555-5555-4555-8555-555555555555" });
    const request = { base_revision: 2, answers: { regime: { selected_option_ids: ["direct-only"], custom_text: null } } };
    await expect(createHttpClient().finalizeIntakeSession("intake-1", request)).resolves.toMatchObject(finalized);
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions/intake-1/finalize", expect.objectContaining({ method: "POST", body: JSON.stringify(request) }));
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key")).toBe("55555555-5555-4555-8555-555555555555");
  });

  it("discovers active, stalled, and candidate-ready resumable intake sessions", async () => {
    const active = { session_id: "intake-active", revision: 1, status: "active", model: "gpt-5.4", effort: "low", problem_specifications: [], decisions: [], frontier: [], thread_generations: [], conversation: [] };
    const stalled = { ...active, session_id: "intake-stalled", status: "convergence_required" };
    const candidate = { ...active, session_id: "intake-candidate", status: "candidate_ready" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([active]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([stalled]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([candidate]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const sessions = await createHttpClient().listActiveIntakeSessions();
    expect(sessions.map((item) => item.session_id)).toEqual(["intake-active", "intake-stalled", "intake-candidate"]);
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions?status=active", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions?status=convergence_required", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/sessions?status=candidate_ready", expect.any(Object));
  });

  it("parses the canonical ErrorEnvelope into ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "invalid_phase", message: "Run is not paused", details: { phase: "review_ready" } },
      request_id: "request-17",
    }), { status: 409, headers: { "Content-Type": "application/json" } })));
    const error = await createHttpClient().resume("demo-run").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "invalid_phase", message: "Run is not paused", details: { phase: "review_ready" }, request_id: "request-17" });
  });

  it("does not expose an unstructured upstream error body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("gateway offline", { status: 502, headers: { "X-Request-ID": "proxy-2" } })));
    await expect(createHttpClient().getRun("demo-run")).rejects.toMatchObject({
      status: 502,
      code: "http_error",
      message: "Request failed (HTTP 502)",
      details: null,
      request_id: "proxy-2",
    });
  });

  it("reuses one idempotency key for the bounded network retry", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("network lost after send"))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixtureRun), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "11111111-1111-4111-8111-111111111111" });
    await createHttpClient().pause("demo-run");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const keys = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys).toEqual(["11111111-1111-4111-8111-111111111111", "11111111-1111-4111-8111-111111111111"]);
  });

  it("retries a network failure while reading a success body with the same key", async () => {
    const bodyFailure = { ok: true, json: vi.fn().mockRejectedValue(new TypeError("connection closed while reading body")) } as unknown as Response;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(bodyFailure)
      .mockResolvedValueOnce(new Response(JSON.stringify(fixtureRun), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "22222222-2222-4222-8222-222222222222" });
    await createHttpClient().pause("demo-run");
    const keys = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys).toEqual(["22222222-2222-4222-8222-222222222222", "22222222-2222-4222-8222-222222222222"]);
  });

  it("does not retry a received HTTP error", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "conflict", message: "Already complete", details: null },
      request_id: "request-18",
    }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(createHttpClient().pause("demo-run")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("exports one explicitly confirmed route without an ambiguous network retry", async () => {
    const report = {
      status: "success",
      run_id: "demo-run",
      selected_route_id: "route-a",
      export_id: "report-1",
      bundle_path: "runs/reports/report-1",
      files: ["report.pdf"],
      manifest: {},
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(report), { status: 201, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const request = { selected_route_id: "route-a", confirm_selected_route: true } as const;
    await expect(createHttpClient().exportReport("demo-run", request)).resolves.toEqual(report);
    expect(fetchMock).toHaveBeenCalledWith("/api/runs/demo-run/reports", expect.objectContaining({ method: "POST", body: JSON.stringify(request) }));
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has("Idempotency-Key")).toBe(false);
    expect(createHttpClient("http://127.0.0.1:8000").reportPdfHref("demo run", "export/1")).toBe(
      "http://127.0.0.1:8000/api/runs/demo%20run/reports/export%2F1/report.pdf",
    );
  });

  it("rejects a report bundle for a different run", async () => {
    const report = {
      status: "success",
      run_id: "different-run",
      selected_route_id: fixtureRun.routes[0].id,
      export_id: "report-1",
      bundle_path: "runs/reports/report-1",
      files: ["report.pdf"],
      manifest: {},
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(report), { status: 201 })));
    await expect(createHttpClient().exportReport("demo-run", {
      selected_route_id: fixtureRun.routes[0].id,
      confirm_selected_route: true,
    })).rejects.toBeInstanceOf(ContractMismatchError);
  });
});
