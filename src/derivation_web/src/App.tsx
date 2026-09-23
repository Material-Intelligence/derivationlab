import { useCallback, useEffect, useMemo, useState } from "react";
import type { DerivationClient } from "./api";
import { rememberChoice, rememberRoute, resolveRouteSelection } from "./reading/forks";
import { BranchDialog } from "./components/BranchDialog";
import { AccountGate } from "./components/AccountGate";
import { AccountRateLimitStatus } from "./components/AccountRateLimitStatus";
import { DetailsDrawer } from "./components/DetailsDrawer";
import { RouteReader } from "./components/RouteReader";
import { ReportExportDialog } from "./components/ReportExportDialog";
import { RunSidebar } from "./components/RunSidebar";
import { NewRunStart } from "./components/NewRunStart";
import { TreeCanvas } from "./components/TreeCanvas";
import { type LiveRole } from "./components/treeCanvasMessages";
import { roleOf } from "./live";
import "./components/RunHeaderLive.css";
import { WorkbenchSplitPane } from "./components/WorkbenchSplitPane";
import { browserHost, type ProductHost } from "./host";
import { useLocale } from "./i18n";
import { useDirectProblemMessages } from "./directProblemMessages";
import { useProductCapabilities } from "./hooks/useProductCapabilities";
import { useProductAccount } from "./hooks/useProductAccount";
import { useAccountRateLimits } from "./hooks/useAccountRateLimits";
import { useReportExport } from "./hooks/useReportExport";
import { useRunCatalog } from "./hooks/useRunCatalog";
import type { CreateIntakeSessionRequest, CreateRunRequest, DerivationStep } from "./types";
import { useDerivationRun } from "./useDerivationRun";

/** Phases in which the runtime is actively working, even before `status` flips to `running`. */
const LIVE_PHASES: readonly string[] = ["submitted", "autonomous_exploration", "human_expansion", "recovering"];

interface AppProps {
  api: DerivationClient;
  initialRunId?: string;
  host?: ProductHost;
}

export function App({ api, initialRunId, host = browserHost }: AppProps) {
  const { messages: m } = useLocale();
  const directMessages = useDirectProblemMessages();
  const statusLabel = {
    submitted: m.phaseSubmitted,
    autonomous_exploration: m.phaseExploring,
    review_ready: m.phaseReviewReady,
    review_ready_due_to_cap: m.phaseCapped,
    human_expansion: m.phaseExpansion,
    paused: m.phasePaused,
    recovering: m.phaseRecovering,
    interrupted: m.phaseInterrupted,
    error: m.phaseError,
  } as const;
  const { run, currentRunId, overlay, loading, error, pendingCommand, selectRun, reloadRun, createRun, pause, resume, interrupt, createBranch } = useDerivationRun(api, initialRunId);
  const { runs, loading: catalogLoading, error: catalogError, refresh: refreshCatalog } = useRunCatalog(api, run);
  const { capabilities, loading: capabilitiesLoading, error: capabilitiesError } = useProductCapabilities(api);
  const accountState = useProductAccount(api, !run);
  const quotaState = useAccountRateLimits(
    api,
    Boolean(run) || accountState.account?.status === "signed_in",
  );
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [intakeSessionId, setIntakeSessionId] = useState<string | undefined>(
    () => new URL(window.location.href).searchParams.get("intake") ?? undefined,
  );
  const [selectedRouteId, setSelectedRouteId] = useState<string>();
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [detailStep, setDetailStep] = useState<DerivationStep | null>(null);
  const [branchStep, setBranchStep] = useState<DerivationStep | null>(null);
  const [pendingReportRunId, setPendingReportRunId] = useState<string | null>(null);
  /** `remembered[forkStepId] = routeId` — which direction the reader took at each fork. */
  const [remembered, setRemembered] = useState<Record<string, string>>({});
  const routes = useMemo(() => run?.routes ?? [], [run?.routes]);
  /**
   * Which roles are working right now, de-duplicated and in overlay order.
   *
   * `ActiveCallOverlay` carries only a label ("Writer model call") today, so the shared `roleOf`
   * in `src/live.ts` parses the role from it (an explicit `role` field wins once the backend adds one).
   */
  const activeRoles = useMemo(() => {
    const labels: Record<LiveRole, string> = { writer: m.roleWriter, checker: m.roleChecker, judge: m.roleJudge, other: m.roleOther };
    const seen: string[] = [];
    for (const call of overlay.activeCalls) {
      const label = labels[roleOf(call)];
      if (!seen.includes(label)) seen.push(label);
    }
    return seen;
  }, [m, overlay.activeCalls]);
  const runIsLive = Boolean(run && (run.status === "running" || LIVE_PHASES.includes(run.phase)));
  const selectedRoute = routes.find((route) => route.id === selectedRouteId) ?? routes[0];
  const {
    open: reportOpen,
    exporting: reportExporting,
    result: reportResult,
    error: reportError,
    show: showReport,
    close: closeReport,
    reset: resetReport,
    confirm: confirmReport,
  } = useReportExport(api, run?.id, selectedRoute);

  useEffect(() => {
    if (routes.length === 0) return;
    if (!routes.some((route) => route.id === selectedRouteId)) setSelectedRouteId(routes[0].id);
  }, [routes, selectedRouteId]);

  const clearRunUi = useCallback(() => {
    setSelectedRouteId(undefined);
    setSelectedNodeId(undefined);
    setRemembered({});
    setDetailStep(null);
    setBranchStep(null);
    resetReport();
  }, [resetReport]);

  const navigateToRun = useCallback((runId?: string) => {
    clearRunUi();
    selectRun(runId);
    setSidebarOpen(false);
    const url = new URL(window.location.href);
    if (runId) url.searchParams.set("run", runId);
    else url.searchParams.delete("run");
    window.history.pushState({ runId: runId ?? null }, "", url);
  }, [clearRunUi, selectRun]);

  useEffect(() => {
    const onPopState = () => {
      const url = new URL(window.location.href);
      const runId = url.searchParams.get("run") ?? undefined;
      setIntakeSessionId(url.searchParams.get("intake") ?? undefined);
      clearRunUi();
      selectRun(runId);
      setSidebarOpen(false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [clearRunUi, selectRun]);

  const createAndRefresh = async (request: CreateRunRequest) => {
    const created = await createRun(request);
    if (created) {
      const url = new URL(window.location.href);
      url.searchParams.set("run", created.id);
      url.searchParams.delete("intake");
      window.history.replaceState({ runId: created.id }, "", url);
      setIntakeSessionId(undefined);
    }
    await refreshCatalog();
  };

  const changeIntakeSession = useCallback((sessionId?: string) => {
    setIntakeSessionId(sessionId);
    const url = new URL(window.location.href);
    if (sessionId) url.searchParams.set("intake", sessionId);
    else url.searchParams.delete("intake");
    window.history.replaceState({ intakeSessionId: sessionId ?? null }, "", url);
  }, []);

  const createIntake = useCallback(
    (request: CreateIntakeSessionRequest) => api.createIntakeSession(request),
    [api],
  );
  const loadProblemPresets = useCallback(() => api.getProblemPresets(), [api]);
  const loadIntake = useCallback(
    (sessionId: string) => api.getIntakeSession(sessionId),
    [api],
  );
  const listActiveIntakes = useCallback(
    () => api.listActiveIntakeSessions(),
    [api],
  );
  const submitIntakeRound = useCallback(
    (sessionId: string, request: Parameters<DerivationClient["submitIntakeRound"]>[1]) =>
      api.submitIntakeRound(sessionId, request),
    [api],
  );
  const finalizeIntake = useCallback(
    (sessionId: string, request: Parameters<DerivationClient["finalizeIntakeSession"]>[1]) =>
      api.finalizeIntakeSession(sessionId, request),
    [api],
  );
  const confirmIntake = useCallback(
    (sessionId: string, baseRevision: number) =>
      api.confirmIntakeSession(sessionId, { base_revision: baseRevision }),
    [api],
  );
  const cancelIntake = useCallback(
    (sessionId: string, baseRevision: number) =>
      api.cancelIntakeSession(sessionId, { base_revision: baseRevision }),
    [api],
  );

  const readOnly = run?.read_only ?? false;

  /** Record the chosen route at every fork it passes, so upstream clicks keep it. */
  const rememberRouteChoice = (routeId: string, forkStepId?: string) => {
    const chosen = routes.find((route) => route.id === routeId);
    if (!run || !chosen) return;
    setRemembered((current) =>
      rememberRoute(run, chosen, forkStepId ? rememberChoice(current, forkStepId, routeId) : current),
    );
  };

  // Selection keeps what the reader already chose downstream: clicking a shared
  // upstream step no longer drags a reader of route C back onto route A.
  const selectNode = (nodeId: string) => {
    const route = resolveRouteSelection(routes, { clickedNodeId: nodeId }, selectedRoute, remembered);
    if (route) setSelectedRouteId(route.id);
    setSelectedNodeId(nodeId);
  };

  const selectRouteFromReader = (routeId: string, forkStepId: string) => {
    setSelectedRouteId(routeId);
    rememberRouteChoice(routeId, forkStepId);
    const chosen = routes.find((route) => route.id === routeId);
    setSelectedNodeId((current) => (current && chosen && !chosen.nodeIds.includes(current) ? undefined : current));
  };
  const requestReportExport = useCallback((runId: string) => {
    if (runId === currentRunId) {
      showReport();
      return;
    }
    navigateToRun(runId);
    setPendingReportRunId(runId);
  }, [currentRunId, navigateToRun, showReport]);

  useEffect(() => {
    if (!pendingReportRunId || run?.id !== pendingReportRunId || routes.length === 0) return;
    showReport();
    setPendingReportRunId(null);
  }, [pendingReportRunId, routes.length, run?.id, showReport]);

  let content;
  if (loading && !run && currentRunId) {
    content = <main className="state-page" aria-live="polite"><div className="spinner" /><h1>{m.loadingTree}</h1></main>;
  } else if (!run && currentRunId && error) {
    content = <main className="state-page"><h1>{m.cannotLoadTree}</h1><div className="error-banner" role="alert">{m.loadTreeFailed}</div><p>{m.runIdentifier}: {currentRunId}</p><button type="button" onClick={reloadRun}>{m.reloadTree}</button></main>;
  } else if (!run && capabilitiesLoading) {
    content = <main className="state-page" aria-live="polite"><div className="spinner" /><h1>{m.loadingCapabilities}</h1></main>;
  } else if (!run && (!capabilities || capabilitiesError)) {
    content = <main className="state-page" aria-live="polite"><h1>{m.cannotCreate}</h1><div className="error-banner" role="alert">{m.capabilitiesUnavailable} {capabilitiesError ?? "unknown error"}</div></main>;
  } else if (!run && accountState.account?.status !== "signed_in") {
    content = <AccountGate accountState={accountState} host={host} />;
  } else if (!run) {
    content = <><NewRunStart loading={loading} defaults={capabilities!.create_run_defaults} initialSessionId={intakeSessionId} loadPresets={loadProblemPresets} onCreate={createIntake} onLoad={loadIntake} onListActive={listActiveIntakes} onRound={submitIntakeRound} onFinalize={finalizeIntake} onConfirm={confirmIntake} onCancel={cancelIntake} onSessionChange={changeIntakeSession} onSubmit={createAndRefresh} />{error && <div className="error-banner" role="alert">{error}</div>}</>;
  } else {
    const maxModelCalls = run.budget.maxSteps;
    const budgetPercent = maxModelCalls !== null && maxModelCalls > 0
      ? Math.min(100, Math.max(0, (run.budget.usedSteps / maxModelCalls) * 100))
      : 0;
    content = (
      <div className="app-shell">
        <header className="app-header">
          <button type="button" className="sidebar-toggle" aria-label={m.openNavigation} onClick={() => setSidebarOpen(true)}>
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" /></svg>
          </button>
          <div className="brand">
            <div>
              <strong title={run.question} aria-label={`${m.currentQuestion}: ${run.question}`}>{run.question}</strong>
              <small>{readOnly ? m.readOnlyNotice : "DerivationLab"}</small>
            </div>
          </div>
          <div className={`run-phase ${run.phase}${runIsLive ? " is-live" : ""}`}><i />{readOnly ? m.readOnly : statusLabel[run.phase]}</div>
          <div className="run-actions">
            <span>{routes.length} {m.routes} · {run.steps.length} {m.nodes}{overlay.hard_interrupt_requested ? ` · ${m.hardInterruptRequested}` : ""}</span>
            {/*
              * Budget bar. The two numbers do not share a unit yet: `usedSteps` counts sealed steps
              * while `maxSteps` is the model-call cap (projection.py). The bar only displays what
              * the contract returns; reconciling the units is a backend change.
              * A run with no cap has `maxSteps === null`, which has no bar to fill: it says so in
              * words instead of pretending the run is at 0%.
              * It is a `div`, not the sibling `span`, because narrow screens hide `.run-actions > span`.
              */}
            {maxModelCalls === null ? (
              <div className="run-progress is-unbounded">
                <span className="run-progress-text">{run.budget.usedSteps} {m.budgetCalls} · {directMessages.unlimitedShort}</span>
              </div>
            ) : (
              <div
                className="run-progress"
                role="progressbar"
                aria-valuemin={0}
                aria-valuenow={run.budget.usedSteps}
                aria-valuemax={maxModelCalls}
                aria-label={m.budgetCalls}
              >
                <span className="run-progress-track" aria-hidden="true">
                  <span className="run-progress-fill" style={{ width: `${budgetPercent}%` }} />
                </span>
                <span className="run-progress-text">{run.budget.usedSteps}/{maxModelCalls}</span>
              </div>
            )}
            {overlay.activeCalls.length > 0 && (
              <div className="run-live-calls">
                <i aria-hidden="true" />
                <span>{overlay.activeCalls.length} {m.activeCalls}</span>
                {activeRoles.length > 0 && <span className="run-live-roles">{activeRoles.join(" · ")}</span>}
              </div>
            )}
            {run.commands.can_pause && <button type="button" className="quiet-button" disabled={pendingCommand !== null} onClick={() => void pause()}>{m.pauseAll}</button>}
            {run.commands.can_resume && <button type="button" className="quiet-button" disabled={pendingCommand !== null} onClick={() => void resume()}>{m.resumeRun}</button>}
            {run.commands.can_interrupt && <button type="button" className="danger-button" disabled={pendingCommand !== null} onClick={() => void interrupt()}>{m.interruptCalls}</button>}
            <button type="button" className="quiet-button" disabled={!selectedRoute || reportExporting} onClick={showReport}>{m.exportPdf}</button>
          </div>
        </header>
        <div className="run-errors" aria-live="polite">
          {run.phase === "paused" && run.errorMessage?.startsWith("Formula validation paused.") && <div className="error-banner" role="alert">
            {run.errorMessage.split("\n").map((line, index) => <p key={index}>{line}</p>)}
          </div>}
          {run.config.checker_enabled === false && <p className="checker-mode-note">{directMessages.checkerOff}</p>}
          {(error || run.phase === "error") && <div className="error-banner" role="alert">
            <p>{run.phase === "error" ? m.runFailed : m.runUpdateFailed}</p>
            <p>{m.runIdentifier}: {run.id}</p>
            <p>{m.reloadTreeHint}</p>
            <button type="button" disabled={loading} onClick={reloadRun}>{m.reloadTree}</button>
          </div>}
        </div>

        <main className="derivation-workspace">
          <WorkbenchSplitPane
            tree={(
              <TreeCanvas
                run={run}
                routes={routes}
                selectedRouteId={selectedRoute?.id}
                selectedNodeId={selectedNodeId}
                onSelectNode={selectNode}
                onSelectRoute={(routeId) => { setSelectedRouteId(routeId); rememberRouteChoice(routeId); setSelectedNodeId(undefined); }}
                onSelectEdge={(edge) => {
                  const route = resolveRouteSelection(routes, { clickedEdge: edge }, selectedRoute, remembered);
                  if (route) setSelectedRouteId(route.id);
                  setSelectedNodeId(edge.to);
                }}
                onOpenDetails={(nodeId) => {
                  const step = run.steps.find((candidate) => candidate.id === nodeId);
                  if (step) setDetailStep(step);
                }}
                onOpenBranch={(nodeId) => {
                  const step = run.steps.find((candidate) => candidate.id === nodeId);
                  if (step) setBranchStep(step);
                }}
                onCopyText={host.copyText}
                canBranchNode={(nodeId) => {
                  const step = run.steps.find((candidate) => candidate.id === nodeId);
                  return Boolean(step && run.commands.branchable_step_revision_ids.includes(step.revisionId));
                }}
                activeCalls={overlay.activeCalls}
              />
            )}
            route={(
              <RouteReader
                route={selectedRoute}
                documentTitle={run.question}
                steps={run.steps}
                edges={run.edges}
                routes={routes}
                remembered={remembered}
                selectedNodeId={selectedNodeId}
                branchableStepRevisionIds={run.commands.branchable_step_revision_ids}
                onSelectRoute={selectRouteFromReader}
                onSelectStep={selectNode}
                onOpenDetails={setDetailStep}
                onOpenBranch={setBranchStep}
                onCopyText={host.copyText}
                activeCalls={overlay.activeCalls}
                runPhase={run.phase}
              />
            )}
          />
        </main>

        <DetailsDrawer step={detailStep} onClose={() => setDetailStep(null)} />
        {!readOnly && <BranchDialog
          step={branchStep}
          submitting={pendingCommand === "branch"}
          onClose={() => setBranchStep(null)}
          onSubmit={async (kind, instruction) => {
            if (!branchStep) return false;
            return Boolean(await createBranch(branchStep.revisionId, kind, instruction));
          }}
        />}
        {reportOpen && <ReportExportDialog
          route={selectedRoute}
          exporting={reportExporting}
          result={reportResult}
          error={reportError}
          host={host}
          pdfHref={reportResult?.status === "success" ? api.reportPdfHref(run.id, reportResult.export_id) : null}
          onClose={closeReport}
          onConfirm={confirmReport}
        />}
      </div>
    );
  }

  return (
    <div className="product-frame">
      <RunSidebar runs={runs} currentRunId={currentRunId} open={sidebarOpen} loading={catalogLoading} onClose={() => setSidebarOpen(false)} onNew={() => navigateToRun()} onSelect={navigateToRun} onExport={requestReportExport} host={host} />
      <section className="product-main">
        <div className="product-content">
          {!run && <div className="empty-window-chrome"><button type="button" className="sidebar-toggle start-sidebar-toggle" aria-label={m.openNavigation} onClick={() => setSidebarOpen(true)}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" /></svg></button></div>}
          {catalogError && <div className="error-banner catalog-error" role="alert">{m.cannotLoadRuns} {catalogError}</div>}
          {content}
        </div>
        <AccountRateLimitStatus value={quotaState.rateLimits} />
      </section>
    </div>
  );
}
