import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { createHttpClient } from "./api";
import { createFixtureApi, fixtureRun, type FixtureApi } from "./fixtures";
import { resolveProductHost } from "./host";
import { LocaleProvider } from "./i18n";
import { SiteAccessGate } from "./components/SiteAccessGate";
import { applyTheme, readStoredTheme } from "./theme";
import "./styles.css";

const params = new URL(window.location.href).searchParams;
const fixtureMode = import.meta.env.VITE_API_MODE === "fixture";
const liveMode = fixtureMode && params.get("live") === "1";
const api = fixtureMode ? createFixtureApi(fixtureRun, { live: liveMode }) : createHttpClient(import.meta.env.VITE_API_BASE_URL ?? "");
const initialRunId = params.get("empty") === "1" ? undefined : params.get("run") ?? (fixtureMode ? "demo-run" : undefined);
const host = resolveProductHost();

document.documentElement.dataset.productHost = host.environment;
applyTheme(readStoredTheme());
const updateWindowActivity = () => {
  document.documentElement.dataset.windowActive = String(document.hasFocus());
};
updateWindowActivity();
window.addEventListener("focus", updateWindowActivity);
window.addEventListener("blur", updateWindowActivity);

/*
 * `?live=1` (fixture mode only): a scripted derivation in progress, so the
 * reading desk's live surfaces can be reviewed — and captured by
 * `npm run smoke:fixture` — without a backend and without model calls.
 *
 * t+1s  a Writer call opens on the last sealed step
 * t+8s  a new step is sealed and a Checker call opens on it
 * t+14s the overlay empties
 */
if (liveMode) {
  const live = api as FixtureApi;
  const parentId = fixtureRun.routes[0].nodeIds[fixtureRun.routes[0].nodeIds.length - 1];
  const sealed = {
    ...fixtureRun.steps[fixtureRun.steps.length - 1],
    id: "live-step",
    revisionId: "revision-live-step",
    order: fixtureRun.steps.length,
    title: "Convergence check: bounding the remainder for $\\lambda \\ll 1$",
    input: "Result A: weak coupling",
    reasoningSummary: "Bound the remainder of the second-order term uniformly and confirm that the expansion converges for $\\lambda \\ll 1$.",
    output: "The remainder decays as $O(\\lambda^3)$, so the weak-coupling result holds in the declared interval.",
    checks: { schema: "passed", physics: "pending", provenance: "passed" },
  } as typeof fixtureRun.steps[number];

  let tick = 0;
  setInterval(() => {
    tick += 1;
    if (tick === 1) {
      live.__emitOverlay([{ callId: "live-writer", branchId: "branch-a", fromStepId: parentId, label: "Writer model call" }]);
    } else if (tick === 8) {
      live.__sealStep(sealed, parentId);
      live.__emitOverlay([{ callId: "live-checker", branchId: "branch-a", fromStepId: sealed.id, label: "Checker model call" }]);
    } else if (tick === 14) {
      live.__emitOverlay([]);
    }
  }, 1000);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LocaleProvider host={host}>
      {fixtureMode ? (
        <App api={api} initialRunId={initialRunId} host={host} />
      ) : (
        <SiteAccessGate api={api}>
          <App api={api} initialRunId={initialRunId} host={host} />
        </SiteAccessGate>
      )}
    </LocaleProvider>
  </StrictMode>,
);
