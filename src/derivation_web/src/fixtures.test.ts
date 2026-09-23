import { describe, expect, expectTypeOf, it, vi } from "vitest";
import { createFixtureApi, fixtureRun, stressRun, stressRunSiblings } from "./fixtures";
import type { DerivationRun, DerivationStep, RunConfig, RunEvent, RunRuntime } from "./types";

describe("canonical fixture", () => {
  it("contains the full RunView/StepView response shape", () => {
    const exact: DerivationRun = fixtureRun;
    expect(exact).toMatchObject({
      status: "review_ready",
      canonical_event_id: 10,
      pause_requested: false,
      hard_interrupt_requested: false,
      errorMessage: null,
    });
    expect(exact.config.max_active_branches).toBe(3);
    expect(exact.runtime.max_run_seconds).toBeNull();
    expect(exact.steps.every((step) => "branch_id" in step && "output_sha256" in step && "provenance" in step)).toBe(true);
    expect(exact.branches.every((branch) => branch.status_history.every((item) => item.seq >= 1))).toBe(true);
    expect(exact.routes.map((route) => route.branch_id)).toEqual(["branch-a", "branch-b", "branch-c"]);
  });

  it("exposes only the implemented v1 runtime request values", () => {
    expectTypeOf<RunRuntime["auth_mode"]>().toEqualTypeOf<"chatgpt">();
    expectTypeOf<RunRuntime["concurrency"]>().toEqualTypeOf<1>();
    expectTypeOf<RunRuntime["max_run_seconds"]>().toEqualTypeOf<null>();
    expectTypeOf<RunConfig["backend"]["name"]>().toEqualTypeOf<string>();
    expectTypeOf<RunConfig["writer"]["provider"]>().toEqualTypeOf<string>();
  });
});

/*
 * The stress fixture is the one visual review is done against. These assertions
 * pin the failure modes it exists to expose; deleting one silently removes a
 * whole class of review coverage.
 */
describe("stress fixture", () => {
  it("carries a long question with same-prefix siblings in the catalog", async () => {
    expect(stressRun.question.length).toBeGreaterThanOrEqual(120);
    expect(stressRun.question).toMatch(/quantum harmonic oscillator/);
    expect(stressRunSiblings).toHaveLength(2);
    for (const sibling of stressRunSiblings) {
      expect(sibling.question.slice(0, 40)).toBe(stressRun.question.slice(0, 40));
      expect(sibling.question).not.toBe(stressRun.question);
    }

    const catalog = await createFixtureApi().listRuns();
    expect(catalog.map((entry) => entry.id)).toEqual(["demo-run", "stress-run", "stress-run-exact", "stress-run-grid"]);
  });

  it("holds three levels of branch-of-branch, a five-way fork and a revision edge", () => {
    expect(stressRun.steps.length).toBeGreaterThanOrEqual(24);
    expect(stressRun.routes.length).toBeGreaterThanOrEqual(5);

    const successors = new Set(stressRun.edges.filter((edge) => edge.from === "s-hamiltonian").map((edge) => edge.to));
    expect(successors.size).toBe(5);
    expect(stressRun.edges.some((edge) => edge.kind === "human_revision")).toBe(true);
    expect(stressRun.routes.some((route) => route.status === "failed")).toBe(true);
    expect(stressRun.routes.some((route) => route.status === "proposed")).toBe(true);

    const depthOf = (branchId: string): number => {
      const branch = stressRun.branches.find((candidate) => candidate.branch_id === branchId)!;
      return branch.parent_branch_id ? depthOf(branch.parent_branch_id) + 1 : 0;
    };
    expect(Math.max(...stressRun.branches.map((branch) => depthOf(branch.branch_id)))).toBeGreaterThanOrEqual(3);
  });

  it("holds a production-scale body and a formula in a step title", () => {
    const longest = Math.max(...stressRun.steps.map((step) => step.reasoningSummary.length));
    expect(longest).toBeGreaterThanOrEqual(20_000);

    const heavy = stressRun.steps.find((step) => step.reasoningSummary.length >= 20_000)!;
    expect(heavy.reasoningSummary.match(/\$\$/g)!.length).toBeGreaterThanOrEqual(6);
    expect(stressRun.steps.some((step) => step.title.includes("$\\psi_n(x)$"))).toBe(true);
  });
});
/*
 * Live-run emitter.
 *
 * Fixture mode is the only surface that can drive the reading desk's live
 * behaviour without a backend and without model calls, so the two handles are
 * covered as carefully as the fixture data itself.
 */
describe("fixture live handles", () => {
  const subscribe = (api: ReturnType<typeof createFixtureApi>, lastEventId: number) => {
    const events: RunEvent[] = [];
    const unsubscribe = api.subscribe("demo-run", (event) => events.push(event), vi.fn(), { lastEventId });
    return { events, unsubscribe };
  };

  it("pushes an overlay without replacing the run or advancing the cursor", async () => {
    const api = createFixtureApi();
    const run = await api.getRun("demo-run");
    const { events, unsubscribe } = subscribe(api, run.canonical_event_id);

    api.__emitOverlay([{ callId: "c1", branchId: "branch-a", fromStepId: "weak-result", label: "Writer model call" }]);

    expect(events).toHaveLength(1);
    // `run: null` is what keeps the client's `RunView` object identity stable.
    expect(events[0].run).toBeNull();
    expect(events[0].event_id).toBe(run.canonical_event_id);
    expect(events[0].overlay?.activeCalls).toEqual([
      { callId: "c1", branchId: "branch-a", fromStepId: "weak-result", label: "Writer model call" },
    ]);

    api.__emitOverlay([]);
    expect(events[1].overlay?.activeCalls).toEqual([]);
    unsubscribe();
  });

  it("seals a step into the run, its edges and the first route", async () => {
    const api = createFixtureApi();
    const run = await api.getRun("demo-run");
    const { events, unsubscribe } = subscribe(api, run.canonical_event_id);
    const sealed: DerivationStep = { ...run.steps[run.steps.length - 1], id: "live-step", revisionId: "revision-live-step" };

    const updated = api.__sealStep(sealed, "weak-result");

    expect(updated.steps.map((step) => step.id)).toContain("live-step");
    expect(updated.edges.some((edge) => edge.from === "weak-result" && edge.to === "live-step")).toBe(true);
    expect(updated.routes[0].nodeIds[updated.routes[0].nodeIds.length - 1]).toBe("live-step");
    expect(updated.canonical_event_id).toBe(run.canonical_event_id + 1);

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("step.sealed");
    expect(events[0].run?.steps.map((step) => step.id)).toContain("live-step");
    expect((await api.getRun("demo-run")).steps.map((step) => step.id)).toContain("live-step");
    unsubscribe();
  });

  it("starts the active run in a running phase when live mode is requested", async () => {
    const live = await createFixtureApi(fixtureRun, { live: true }).getRun("demo-run");
    expect(live.phase).toBe("autonomous_exploration");
    expect(live.status).toBe("running");
    expect((await createFixtureApi().getRun("demo-run")).status).toBe("review_ready");
  });
});
