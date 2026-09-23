import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { createFixtureApi, fixtureRun } from "./fixtures";
import type { DerivationClient } from "./api";
import type { DerivationRun, RunEvent, RunSummary } from "./types";

afterEach(() => window.history.replaceState({}, "", "/"));

function summary(run: DerivationRun, readOnly = false): RunSummary {
  return { id: run.id, question: run.question, phase: run.phase, status: run.status, updated_at: run.updated_at, created_at: run.created_at, step_count: run.steps.length, route_count: run.routes.length, read_only: readOnly };
}

const noCommands: DerivationRun["commands"] = {
  can_pause: false,
  can_resume: false,
  can_interrupt: false,
  branchable_step_revision_ids: [],
};

function authoritative(run: DerivationRun, commands: Partial<DerivationRun["commands"]>, readOnly = false): DerivationRun {
  return { ...run, read_only: readOnly, commands: { ...noCommands, ...commands } };
}

/** The reading desk heads with the run question; the route identity is the subtitle. */
function readerRouteLabel(): string | null | undefined {
  return document.querySelector(".reader-route-label")?.textContent;
}

async function renderApp(api: DerivationClient = createFixtureApi()) {
  const user = userEvent.setup();
  render(<App api={api} initialRunId="demo-run" />);
  await screen.findByRole("heading", { name: "Complete derivation tree" });
  return user;
}

describe("tree-first derivation UI", () => {
  it("labels a checker-off unlimited run without implying its physics passed", async () => {
    const value = structuredClone(fixtureRun);
    value.config = { ...value.config, checker_enabled: false, record_version: "1.1", max_model_calls: null };
    value.budget.maxSteps = null;
    value.steps.forEach((step) => { step.checks.physics = "not_requested"; });
    await renderApp(createFixtureApi(value));
    expect(screen.getByText("Checker off · segments remain unchecked")).toBeVisible();
    expect(screen.getByText(/no total limit/)).toBeVisible();
  });
  it("keeps an error run readable with safe text and a read-only reload", async () => {
    const api = createFixtureApi();
    api.getRun = vi.fn().mockResolvedValue({ ...fixtureRun, phase: "error", errorMessage: "private exception detail" });
    const resume = vi.spyOn(api, "resume");
    await renderApp(api);
    expect(screen.queryByText("private exception detail")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(`Run ID: ${fixtureRun.id}`);
    fireEvent.click(screen.getByRole("button", { name: "Reload tree" }));
    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("heading", { name: "Complete derivation tree" })).toBeVisible();
    expect(resume).not.toHaveBeenCalled();
  });

  it("shows safe formula diagnostics for a paused run", async () => {
    const api = createFixtureApi();
    api.getRun = vi.fn().mockResolvedValue({ ...fixtureRun, phase: "paused", status: "paused", errorMessage: "Formula validation paused.\nderivation formula 1: Unsupported TeX command: \\q" });
    await renderApp(api);
    expect(screen.getByRole("alert")).toHaveTextContent("derivation formula 1");
    expect(screen.getByRole("alert")).toHaveTextContent("\\q");
  });

  it("keeps the loaded tree on stream failure and offers safe reload", async () => {
    const api = createFixtureApi();
    let fail: (error: Error) => void = () => undefined;
    api.subscribe = vi.fn((_id, _event, error) => { fail = error; return () => undefined; });
    const read = vi.spyOn(api, "getRun");
    await renderApp(api);
    act(() => fail(new Error("private stream detail")));
    expect(screen.queryByText("private stream detail")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Complete derivation tree" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(`Run ID: ${fixtureRun.id}`);
    fireEvent.click(screen.getByRole("button", { name: "Reload tree" }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });

  it("offers safe detail reload after a failed read", async () => {
    const api = createFixtureApi();
    api.getRun = vi.fn().mockRejectedValueOnce(new Error("private server traceback"))
      .mockResolvedValue(fixtureRun);
    render(<App api={api} initialRunId="demo-run" />);
    const reload = await screen.findByRole("button", { name: "Reload tree" });
    expect(screen.getByText("Run ID: demo-run")).toBeVisible();
    expect(screen.queryByText("private server traceback")).not.toBeInTheDocument();
    fireEvent.click(reload);
    expect(await screen.findByRole("heading", { name: "Complete derivation tree" })).toBeVisible();
  });
  it("shows account quota throughout the signed-in product and refreshes on focus", async () => {
    const api = createFixtureApi();
    const getAccountRateLimits = vi.spyOn(api, "getAccountRateLimits");

    await renderApp(api);

    const quota = await screen.findByRole("contentinfo", { name: /Weekly 69% left/ });
    expect(quota).toBeVisible();
    expect(quota).toHaveTextContent("5-hour 82% left");
    expect(getAccountRateLimits).toHaveBeenCalledTimes(1);
    fireEvent.focus(window);
    await waitFor(() => expect(getAccountRateLimits).toHaveBeenCalledTimes(2));
  });

  it("does not request or show quota before ChatGPT sign-in", async () => {
    const api = createFixtureApi();
    api.getAccount = vi.fn(async () => ({ status: "signed_out" as const, credential_store: "file" as const, import_available: false, diagnostic: "source_auth_unavailable" as const }));
    const getAccountRateLimits = vi.spyOn(api, "getAccountRateLimits");

    render(<App api={api} />);

    expect(await screen.findByRole("heading", { name: "Connect your ChatGPT account" })).toBeVisible();
    expect(screen.queryByRole("contentinfo", { name: /Weekly/ })).not.toBeInTheDocument();
    expect(getAccountRateLimits).not.toHaveBeenCalled();
  });

  it("requires explicit confirmation before importing the fixed existing Codex login", async () => {
    const api = createFixtureApi();
    api.getAccount = vi.fn(async () => ({ status: "signed_out" as const, credential_store: "file" as const, import_available: true, diagnostic: "source_auth_available" as const }));
    const importExistingAccount = vi.spyOn(api, "importExistingAccount");
    const user = userEvent.setup();
    render(<App api={api} />);

    expect(await screen.findByRole("heading", { name: "Connect your ChatGPT account" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Import existing Codex sign-in" }));
    expect(screen.getByRole("heading", { name: "Import existing Codex sign-in?" })).toBeVisible();
    expect(importExistingAccount).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm and import" }));

    expect(await screen.findByRole("heading", { name: "Start from a complete problem" })).toBeVisible();
    expect(importExistingAccount).toHaveBeenCalledWith({ confirm_import: true });
  });

  it("replaces stale account failure during device login and clears recovered polling errors", async () => {
    const api = createFixtureApi();
    api.getAccount = vi.fn(async () => ({ status: "unavailable" as const, credential_store: "file" as const, import_available: false, diagnostic: "account_check_failed" as const }));
    api.getDeviceLogin = vi.fn().mockRejectedValueOnce(new Error("Temporary polling failure")).mockResolvedValue({ status: "pending", diagnostic: null });
    const user = userEvent.setup();
    render(<App api={api} />);
    await screen.findByText("Account service unavailable");
    await user.click(screen.getByRole("button", { name: "Sign in with a separate account" }));
    expect(screen.queryByText("Account service unavailable")).not.toBeInTheDocument();
    await screen.findByText("Temporary polling failure");
    await waitFor(() => expect(screen.queryByText("Temporary polling failure")).not.toBeInTheDocument(), { timeout: 3500 });
    expect(screen.getByText("Finish signing in in your browser")).toBeVisible();
  });

  it("opens the allowlisted device-login URL only after an explicit user click", async () => {
    const api = createFixtureApi();
    api.getAccount = vi.fn(async () => ({ status: "signed_out" as const, credential_store: "file" as const, import_available: false, diagnostic: "source_auth_unavailable" as const }));
    api.getDeviceLogin = vi.fn(async () => ({ status: "pending" as const, diagnostic: null }));
    const openExternal = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<App api={api} host={{ environment: "web", copyText: vi.fn(async () => undefined), openExternal }} />);

    await user.click(await screen.findByRole("button", { name: "Sign in with a separate account" }));
    expect(await screen.findByText("FIXTURE-CODE")).toBeVisible();
    expect(openExternal).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Open OpenAI sign-in page" }));
    expect(openExternal).toHaveBeenCalledWith("https://auth.openai.com/device");
  });

  it("heads the reading desk with the run question, not the route label", async () => {
    await renderApp();
    const title = document.querySelector(".reader-document-title");

    expect(title).toHaveTextContent(fixtureRun.question);
    expect(title?.tagName).toBe("H2");
    // The smoke typography contract binds one heading-scale element to this hook.
    expect(document.querySelectorAll(".reader-document-title")).toHaveLength(1);
    expect(readerRouteLabel()).toBe("Result A: weak coupling");
  });

  it("keeps the route reader visible and chooses the first route for a shared node", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Open: Define states and constraints" }));
    expect(readerRouteLabel()).toBe("Result A: weak coupling");
    expect(screen.getByRole("complementary", { name: "Route reader" })).toBeVisible();
    expect(screen.getByRole("article", { current: "step" })).toHaveTextContent("Define states and constraints");
  });

  it("selects the exclusive route from a node and expands the chosen step", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    expect(readerRouteLabel()).toBe("Result B: validity boundary");
    expect(screen.getByRole("article", { current: "step" })).toHaveTextContent("small-scale exact diagonalization");
  });

  it("selects the exclusive route from an edge", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Select matching route: Symmetry route → Representation decomposition" }));
    expect(readerRouteLabel()).toBe("Result C: symmetry closure");
    expect(screen.getByRole("article", { current: "step" })).toHaveTextContent("Representation decomposition");
  });

  it("switches route from the fork in the prose without rebuilding the shared prefix", async () => {
    const user = await renderApp();
    const shared = ["question", "constraints", "conservation"];
    const before = shared.map((id) => document.getElementById(`reader-step-${id}`));
    expect(document.getElementById("reader-step-perturbation")).toBeInTheDocument();

    const picker = screen
      .getAllByTestId("branch-picker")
      .find((element) => element.dataset.forkStep === "conservation")!;
    await user.click(within(picker).getByRole("radio", { name: /Numerical counterexample check/ }));

    expect(readerRouteLabel()).toBe("Result B: validity boundary");
    // Only the tail is replaced: the shared prefix keeps its DOM nodes.
    shared.forEach((id, index) => expect(document.getElementById(`reader-step-${id}`)).toBe(before[index]));
    expect(document.getElementById("reader-step-perturbation")).not.toBeInTheDocument();
    expect(document.getElementById("reader-step-boundary-result")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export PDF" })).toBeEnabled();
  });

  it("keeps the reader on route C when a shared upstream step is clicked", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Select matching route: Symmetry route → Representation decomposition" }));
    expect(readerRouteLabel()).toBe("Result C: symmetry closure");

    await user.click(screen.getByRole("button", { name: "Open: Research question" }));
    expect(readerRouteLabel()).toBe("Result C: symmetry closure");
    expect(screen.getByRole("article", { current: "step" })).toHaveTextContent("Research question");

    await user.click(screen.getByRole("button", { name: "Open: Define states and constraints" }));
    expect(readerRouteLabel()).toBe("Result C: symmetry closure");
    expect(screen.getByRole("button", { name: "Export PDF" })).toBeEnabled();
  });

  it("opens five-field details only on request and keeps audit collapsed", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    expect(screen.queryByRole("dialog", { name: "Numerical counterexample check" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View 5 details" }));
    expect(screen.getByRole("dialog", { name: "Numerical counterexample check" })).toBeVisible();
    expect(screen.getByText("1. Claim")).toBeVisible();
    expect(screen.getByText("5. Scope")).toBeVisible();
    expect(screen.getByText("Checks and audit").closest("details")).not.toHaveAttribute("open");
  });

  it("requires an explicit route confirmation before exporting a ReportBundle", async () => {
    const api = createFixtureApi();
    const exportReport = vi.spyOn(api, "exportReport");
    const copyText = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<App api={api} initialRunId="demo-run" host={{ environment: "web", copyText }} />);
    await screen.findByRole("heading", { name: "Complete derivation tree" });

    await user.click(screen.getByRole("button", { name: "Export PDF" }));
    expect(screen.getByRole("dialog", { name: "Export auditable PDF" })).toBeVisible();
    expect(exportReport).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /Confirm and export/ }));

    await screen.findByText("PDF export complete");
    expect(screen.getByRole("link", { name: "Download PDF" })).toHaveAttribute(
      "href",
      "/api/runs/demo-run/reports/fixture-report-001/report.pdf",
    );
    expect(exportReport).toHaveBeenCalledWith("demo-run", {
      selected_route_id: fixtureRun.routes[0].id,
      confirm_selected_route: true,
    });
    await user.click(screen.getByRole("button", { name: "Copy PDF path" }));
    expect(copyText).toHaveBeenCalledWith("runs/fixture/reports/demo-run/fixture-report-001/report.pdf");
  });

  it("explains failed PDF compilation safely and retries the same selected route", async () => {
    const api = createFixtureApi();
    const successful = await api.exportReport("demo-run", { selected_route_id: fixtureRun.routes[1].id, confirm_selected_route: true });
    let finishRetry!: (value: typeof successful) => void;
    const exportReport = vi.spyOn(api, "exportReport")
      .mockResolvedValueOnce({ ...successful, status: "failed", bundle_path: "/private/server/report", manifest: { failure: { code: "tectonic_timeout", message: "Failed at /private/server/secret" } } })
      .mockImplementationOnce(() => new Promise((resolve) => { finishRetry = resolve; }));
    const user = userEvent.setup();
    render(<App api={api} initialRunId="demo-run" />);
    await screen.findByRole("heading", { name: "Complete derivation tree" });
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "Export PDF" }));
    await user.click(screen.getByRole("button", { name: /Confirm and export/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("tectonic_timeout");
    expect(screen.getByRole("alert")).toHaveTextContent("timed out");
    expect(screen.queryByText(/\/private\/server/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download PDF" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry PDF export" }));
    expect(screen.queryByText("PDF compilation failed; evidence was preserved")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Compiling…" })).toBeDisabled();
    expect(exportReport).toHaveBeenNthCalledWith(2, "demo-run", { selected_route_id: fixtureRun.routes[1].id, confirm_selected_route: true });
    await act(async () => finishRetry(successful));
    expect(await screen.findByText("PDF export complete")).toBeVisible();
  });

  it("offers a branch dialog on sealed review nodes and enters human expansion", async () => {
    const user = await renderApp();
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "Start a new branch here" }));
    expect(screen.getByRole("dialog", { name: /Start a new branch from “Numerical counterexample check”/ })).toBeVisible();
    await user.type(screen.getByLabelText("Branch instruction"), "Re-expand along a non-perturbative route");
    await user.click(screen.getByRole("button", { name: "Create and run autonomously" }));
    await waitFor(() => expect(screen.getByText("Branch derivation running")).toBeVisible());
    expect(screen.queryByRole("button", { name: "Start a new branch here" })).not.toBeInTheDocument();
  });

  it("keeps the branch dialog open when branch creation fails", async () => {
    const api = createFixtureApi();
    api.createBranch = vi.fn(async () => { throw new Error("branch rejected"); });
    const user = await renderApp(api);
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "Start a new branch here" }));
    await user.type(screen.getByLabelText("Branch instruction"), "Try a new route");
    await user.click(screen.getByRole("button", { name: "Create and run autonomously" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The run could not be updated.");
    expect(screen.getByRole("dialog", { name: /Start a new branch from “Numerical counterexample check”/ })).toBeVisible();
  });

  it("serializes human revision as the exact five-field JSON instruction", async () => {
    const api = createFixtureApi();
    const createBranch = vi.spyOn(api, "createBranch");
    const user = await renderApp(api);
    const source = fixtureRun.steps.find((step) => step.id === "counterexample")!;
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "Start a new branch here" }));
    await user.click(screen.getByRole("radio", { name: "Revise this step" }));
    expect(screen.getByLabelText("Why")).toHaveValue(source.content!.why);
    await user.clear(screen.getByLabelText("Claim"));
    expect(screen.getByRole("button", { name: "Submit revision and continue" })).toBeDisabled();
    await user.type(screen.getByLabelText("Claim"), "Revised boundary result");
    await user.click(screen.getByRole("button", { name: "Submit revision and continue" }));
    await waitFor(() => expect(createBranch).toHaveBeenCalledWith("demo-run", {
      from_step_revision_id: source.revisionId,
      kind: "human_revision",
      instruction: JSON.stringify({ ...source.content!, claim: "Revised boundary result" }),
    }));
  });

  it("does not leak structured revision state into direction mode", async () => {
    const api = createFixtureApi();
    const createBranch = vi.spyOn(api, "createBranch");
    const user = await renderApp(api);
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "Start a new branch here" }));
    await user.click(screen.getByRole("radio", { name: "Revise this step" }));
    await user.clear(screen.getByLabelText("Claim"));
    await user.type(screen.getByLabelText("Claim"), "Must not leak into the direction instruction");
    await user.click(screen.getByRole("radio", { name: "New direction" }));
    expect(screen.getByLabelText("Branch instruction")).toHaveValue("");
    await user.type(screen.getByLabelText("Branch instruction"), "Explore only the non-perturbative direction");
    await user.click(screen.getByRole("button", { name: "Create and run autonomously" }));
    await waitFor(() => expect(createBranch).toHaveBeenCalledWith("demo-run", expect.objectContaining({
      kind: "human_direction",
      instruction: "Explore only the non-perturbative direction",
    })));
  });

  it("offers pause only during pausable autonomous phases", async () => {
    const running = authoritative({ ...structuredClone(fixtureRun), phase: "autonomous_exploration", status: "running" }, { can_pause: true });
    await renderApp(createFixtureApi(running));
    expect(screen.getByRole("button", { name: "Pause all" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Resume run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Interrupt/ })).not.toBeInTheDocument();
  });

  it("offers resume while paused", async () => {
    const paused = authoritative({ ...structuredClone(fixtureRun), phase: "paused", status: "paused", pause_requested: true }, { can_resume: true });
    const user = await renderApp(createFixtureApi(paused));
    expect(screen.queryByRole("button", { name: "Pause all" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume run" }));
    await waitFor(() => expect(screen.getByText("Deriving")).toBeVisible());
  });

  it("never treats recovering as pausable", async () => {
    const recovering = authoritative({ ...structuredClone(fixtureRun), phase: "recovering", status: "running" }, {});
    await renderApp(createFixtureApi(recovering));
    expect(screen.queryByRole("button", { name: "Pause all" })).not.toBeInTheDocument();
  });

  it("offers interrupt only when the SSE overlay reports an in-flight call", async () => {
    const active = authoritative({ ...structuredClone(fixtureRun), phase: "autonomous_exploration", status: "running" }, { can_pause: true });
    const api = createFixtureApi(active);
    const subscribe = api.subscribe.bind(api);
    api.subscribe = (runId, onEvent, onError, options) => {
      const stop = subscribe(runId, onEvent, onError, options);
      queueMicrotask(() => onEvent({
        event_id: 11,
        type: "run.updated",
        run_id: runId,
        occurred_at: "2026-08-29T15:04:00Z",
        run: authoritative(active, { can_pause: true, can_interrupt: true }),
        overlay: { hard_interrupt_requested: false, activeCalls: [{ callId: "call-1", branchId: "branch-a", fromStepId: "weak-result", label: "writer" }] },
      }));
      return stop;
    };
    await renderApp(api);
    expect(await screen.findByRole("button", { name: "Interrupt active calls" })).toBeVisible();
  });

  it("prevents duplicate control submissions while one command is pending", async () => {
    const running = authoritative({ ...structuredClone(fixtureRun), phase: "autonomous_exploration", status: "running" }, { can_pause: true });
    const api = createFixtureApi(running);
    let resolvePause!: (run: DerivationRun) => void;
    const pendingPause = new Promise<DerivationRun>((resolve) => { resolvePause = resolve; });
    api.pause = vi.fn(() => pendingPause);
    await renderApp(api);
    const button = screen.getByRole("button", { name: "Pause all" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.pause).toHaveBeenCalledTimes(1);
    resolvePause(authoritative({ ...running, phase: "paused", status: "paused", pause_requested: true }, { can_resume: true }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume run" })).toBeVisible());
  });

  it("discards a late command response after the user switches runs", async () => {
    const first = authoritative({ ...structuredClone(fixtureRun), phase: "autonomous_exploration", status: "running" }, { can_pause: true });
    const second = { ...structuredClone(fixtureRun), id: "second-run", question: "Second derivation", updated_at: "2026-08-30T16:00:00Z" };
    const api = createFixtureApi(first);
    api.listRuns = vi.fn(async () => [summary(second), summary(first)]);
    api.getRun = vi.fn(async (runId) => structuredClone(runId === second.id ? second : first));
    let resolvePause!: (run: DerivationRun) => void;
    api.pause = vi.fn(() => new Promise<DerivationRun>((resolve) => { resolvePause = resolve; }));
    const user = await renderApp(api);

    await user.click(screen.getByRole("button", { name: "Pause all" }));
    await user.click(screen.getByRole("button", { name: /Second derivation/ }));
    await waitFor(() => expect(new URL(window.location.href).searchParams.get("run")).toBe(second.id));
    expect(screen.getByRole("button", { name: /Second derivation/ })).toHaveAttribute("aria-current", "page");

    resolvePause(authoritative({ ...first, phase: "paused", status: "paused", pause_requested: true }, { can_resume: true }));

    await waitFor(() => expect(api.pause).toHaveBeenCalledWith(first.id));
    expect(screen.getByText("Budget reached · review needed")).toBeVisible();
    expect(new URL(window.location.href).searchParams.get("run")).toBe(second.id);
  });

  it("clears a transient SSE disconnect warning when the stream reopens", async () => {
    const api = createFixtureApi();
    let signalError!: (error: Error) => void;
    let signalOpen!: () => void;
    api.subscribe = (_runId, _onEvent, onError, options) => {
      signalError = onError;
      signalOpen = () => options?.onOpen?.();
      return () => undefined;
    };
    await renderApp(api);
    act(() => signalError(new Error("Run event stream disconnected")));
    expect(screen.getByRole("alert")).toHaveTextContent("The run could not be updated.");
    act(() => signalOpen());
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("keeps the run catalog visible and highlights the current run", async () => {
    await renderApp();
    const sidebar = screen.getByRole("complementary", { name: "Derivation navigation" });
    expect(sidebar).toBeVisible();
    expect(screen.getByRole("button", { name: "New derivation" })).toBeVisible();
    expect(screen.getByRole("button", { name: new RegExp(fixtureRun.question) })).toHaveAttribute("aria-current", "page");
  });

  it("switches runs, closes the previous SSE, clears transient panels, and updates the URL", async () => {
    const first = structuredClone(fixtureRun);
    const second = { ...structuredClone(fixtureRun), id: "second-run", question: "Second derivation", updated_at: "2026-08-30T16:00:00Z" };
    const api = createFixtureApi(first);
    api.listRuns = vi.fn(async () => [summary(second), summary(first)]);
    api.getRun = vi.fn(async (runId) => structuredClone(runId === second.id ? second : first));
    const stopFirst = vi.fn();
    const stopSecond = vi.fn();
    api.subscribe = vi.fn((runId, _onEvent, _onError, options) => { options?.onOpen?.(); return runId === first.id ? stopFirst : stopSecond; });
    const user = await renderApp(api);

    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    await user.click(screen.getByRole("button", { name: "View 5 details" }));
    expect(screen.getByRole("dialog", { name: "Numerical counterexample check" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Second derivation/ }));

    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith("second-run"));
    await waitFor(() => expect(stopFirst).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "Numerical counterexample check" })).not.toBeInTheDocument();
    expect(new URL(window.location.href).searchParams.get("run")).toBe("second-run");
    expect(screen.getByRole("button", { name: /Second derivation/ })).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("button", { name: "New derivation" }));
    expect(await screen.findByRole("heading", { name: "Start from a complete problem" })).toBeVisible();
    expect(new URL(window.location.href).searchParams.has("run")).toBe(false);
    await waitFor(() => expect(stopSecond).toHaveBeenCalledTimes(1));
  });

  it("hides mutable run controls for a read-only active validation", async () => {
    const active = authoritative({ ...structuredClone(fixtureRun), phase: "human_expansion" as const, status: "running" as const }, {}, true);
    const api = createFixtureApi(active);
    api.listRuns = vi.fn(async () => [summary(active, true)]);
    const subscribe = api.subscribe.bind(api);
    api.subscribe = (runId, onEvent, onError, options) => {
      const stop = subscribe(runId, onEvent, onError, options);
      queueMicrotask(() => onEvent({ event_id: 11, type: "run.updated", run_id: runId, occurred_at: active.updated_at, run: null, overlay: { hard_interrupt_requested: false, activeCalls: [{ callId: "call-ro", branchId: "branch-a", fromStepId: "weak-result", label: "writer" }] } }));
      return stop;
    };
    await renderApp(api);
    await screen.findByText("Read-only archive · the original run will not be changed");
    expect(screen.queryByRole("button", { name: "Pause all" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Interrupt active calls" })).not.toBeInTheDocument();
    expect(screen.getByText("Read-only archive · the original run will not be changed")).toBeVisible();
  });

  it("keeps RunView command authority when the catalog fails", async () => {
    const active = authoritative({ ...structuredClone(fixtureRun), phase: "autonomous_exploration" as const, status: "running" as const }, { can_pause: true });
    const api = createFixtureApi(active);
    api.listRuns = vi.fn(async () => { throw new Error("catalog unavailable"); });

    await renderApp(api);

    expect(screen.getByText("DerivationLab")).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause all" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("catalog unavailable");
  });

  it("hides branch creation for a read-only review run", async () => {
    const archived = authoritative(structuredClone(fixtureRun), {}, true);
    const api = createFixtureApi(archived);
    api.listRuns = vi.fn(async () => [summary(archived, true)]);
    const user = await renderApp(api);
    await screen.findByText("Read-only archive · the original run will not be changed");
    await user.click(screen.getByRole("button", { name: "Open: Numerical counterexample check" }));
    expect(screen.queryByRole("button", { name: "Start a new branch here" })).not.toBeInTheDocument();
  });

  it("hides resume for a read-only paused run", async () => {
    const paused = authoritative({ ...structuredClone(fixtureRun), phase: "paused" as const, status: "paused" as const, pause_requested: true }, {}, true);
    const api = createFixtureApi(paused);
    api.listRuns = vi.fn(async () => [summary(paused, true)]);
    await renderApp(api);
    await screen.findByText("Read-only archive · the original run will not be changed");
    expect(screen.queryByRole("button", { name: "Resume run" })).not.toBeInTheDocument();
  });
});

describe("run header live signals", () => {
  const activeCall = (callId: string, label: string) => ({ callId, branchId: "branch-a", fromStepId: "weak-result", label });

  it("shows the model-call budget as a progress bar and stays calm when nothing runs", async () => {
    await renderApp();

    const budget = screen.getByRole("progressbar", { name: "model calls" });
    expect(budget).toHaveAttribute("aria-valuenow", "10");
    expect(budget).toHaveAttribute("aria-valuemax", "10");
    expect(budget).toHaveAttribute("aria-valuemin", "0");
    expect(budget).toHaveTextContent("10/10");
    expect(document.querySelector(".run-phase.is-live")).toBeNull();
    expect(document.querySelector(".run-live-calls")).toBeNull();
  });

  it("pulses the phase pill and summarises the roles in flight", async () => {
    const api = createFixtureApi();
    const running: DerivationRun = { ...fixtureRun, phase: "autonomous_exploration", status: "running", budget: { usedSteps: 3, maxSteps: 8 } };
    api.getRun = vi.fn().mockResolvedValue(running);
    let emit: (event: RunEvent) => void = () => undefined;
    api.subscribe = vi.fn((_id, event) => { emit = event; return () => undefined; });
    await renderApp(api);

    expect(document.querySelector(".run-phase.is-live")).not.toBeNull();
    expect(screen.getByRole("progressbar", { name: "model calls" })).toHaveAttribute("aria-valuenow", "3");

    act(() => emit({
      event_id: 11,
      type: "run.updated",
      run_id: running.id,
      occurred_at: running.updated_at,
      run: running,
      overlay: {
        activeCalls: [activeCall("c1", "Writer model call"), activeCall("c2", "Checker model call"), activeCall("c3", "Checker model call")],
        hard_interrupt_requested: false,
      },
    }));

    const live = document.querySelector(".run-live-calls");
    expect(live).toHaveTextContent("3 active calls");
    // Roles are de-duplicated in overlay order, so two checkers read as one badge.
    expect(live).toHaveTextContent("Deriving · Checking");
    // The same overlay drives the map: one ghost per call, all hanging under the same sealed step.
    expect(document.querySelectorAll('.tree-ghost[data-ghost-for="weak-result"]')).toHaveLength(3);
  });
});
