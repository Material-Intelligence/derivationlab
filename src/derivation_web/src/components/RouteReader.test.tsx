import { createElement } from "react";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureRun, stressRun } from "../fixtures";
import type { DerivationRoute, DerivationRun, DerivationStep } from "../types";
import type { LiveCall } from "../live";
import { RouteReader } from "./RouteReader";

/*
 * `ScientificText` is the expensive renderer on the desk (Markdown + KaTeX). The
 * wrapper below is deliberately *not* memoized, so it renders exactly when its
 * step article renders: counting it counts step re-renders.
 */
const renderLog = vi.hoisted(() => ({ text: [] as string[] }));
vi.mock("./ScientificText", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./ScientificText")>();
  return {
    ...actual,
    ScientificText: (props: { value: string; className?: string }) => {
      renderLog.text.push(props.value);
      return createElement(actual.ScientificText, props);
    },
  };
});

const routeOf = (run: DerivationRun, id: string): DerivationRoute => run.routes.find((route) => route.id === id)!;

function renderReader(run: DerivationRun, routeId: string, overrides: Partial<Parameters<typeof RouteReader>[0]> = {}) {
  const props = {
    route: routeOf(run, routeId),
    steps: run.steps,
    edges: run.edges,
    routes: run.routes,
    branchableStepRevisionIds: run.commands.branchable_step_revision_ids,
    onSelectStep: vi.fn(),
    onSelectRoute: vi.fn(),
    onOpenDetails: vi.fn(),
    onOpenBranch: vi.fn(),
    onCopyText: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<RouteReader {...props} />) };
}

describe("RouteReader as a continuous document", () => {
  it("renders every step of the route, not only the selected one", () => {
    const { container } = renderReader(fixtureRun, "route-a", { selectedNodeId: "conservation" });

    const steps = container.querySelectorAll(".route-step");
    expect(steps).toHaveLength(5);
    for (const step of steps) expect(step.querySelector(".route-step-body .scientific-text")).toBeInTheDocument();
    expect(container.querySelectorAll('.route-step[aria-current="step"]')).toHaveLength(1);
    expect(container.querySelector('.route-step[aria-current="step"]')).toHaveAttribute("id", "reader-step-conservation");
  });

  it("heads the document with the route identity, its size and the current path", () => {
    const { container } = renderReader(fixtureRun, "route-b");

    expect(screen.getByRole("heading", { level: 2, name: "Result B: validity boundary" })).toBeVisible();
    expect(container.querySelector(".route-status")).toHaveTextContent("Complete");
    expect(screen.getByText("5 steps")).toBeVisible();
    expect(screen.getByText(/Current path: Conservation-law route › Numerical counterexample check/)).toBeVisible();
    expect(screen.queryByText("Route reading")).not.toBeInTheDocument();
  });

  it("heads the document with the run question and demotes the route label", () => {
    const { container } = renderReader(fixtureRun, "route-b", { documentTitle: fixtureRun.question });

    expect(screen.getByRole("heading", { level: 2, name: fixtureRun.question })).toBeVisible();
    expect(container.querySelectorAll(".reader-document-title")).toHaveLength(1);
    expect(container.querySelector(".reader-document-meta .reader-route-label")).toHaveTextContent("Result B: validity boundary");
  });

  it("shows neither a picker nor a path for a single-route run", () => {
    const single: DerivationRun = { ...stressRun, routes: [routeOf(stressRun, "sroute-series")] };
    renderReader(single, "sroute-series");

    expect(screen.queryAllByTestId("branch-picker")).toHaveLength(0);
    expect(screen.queryByText(/Current path/)).not.toBeInTheDocument();
  });

  it("keeps the step context menu and the per-step actions reachable", async () => {
    const user = userEvent.setup();
    const { props } = renderReader(fixtureRun, "route-a", { selectedNodeId: "perturbation" });

    await user.click(screen.getByRole("button", { name: "View 5 details" }));
    expect(props.onOpenDetails).toHaveBeenCalledWith(expect.objectContaining({ id: "perturbation" }));

    await user.click(screen.getByRole("button", { name: "More actions for route step: question" }));
    expect(screen.getByRole("menuitem", { name: "Locate in tree" })).toBeVisible();
  });

  it("selects a step from its sequence number without moving the reader off the route", async () => {
    const user = userEvent.setup();
    const { props } = renderReader(fixtureRun, "route-a");

    await user.click(screen.getByRole("button", { name: "Select route step: Perturbative expansion" }));
    expect(props.onSelectStep).toHaveBeenCalledWith("perturbation");
  });
});

describe("in-prose branch picker", () => {
  it("offers one radio per direction at each fork and checks the current one", async () => {
    const user = userEvent.setup();
    const { props, container } = renderReader(fixtureRun, "route-a");

    const pickers = screen.getAllByTestId("branch-picker");
    expect(pickers).toHaveLength(2);
    expect(container.querySelector('#reader-step-constraints [data-testid="branch-picker"]')).toBeInTheDocument();

    const atConstraints = pickers[0];
    const radios = within(atConstraints).getAllByRole("radio");
    expect(radios).toHaveLength(2);
    expect(radios[0]).toHaveAttribute("aria-checked", "true");
    expect(radios[1]).toHaveAttribute("aria-checked", "false");

    await user.click(radios[1]);
    expect(props.onSelectRoute).toHaveBeenCalledWith("route-c", "constraints");
  });

  it("shows five directions for a five-way fork and marks the failed and proposed ones", () => {
    renderReader(stressRun, "sroute-levels");

    const picker = document.querySelector('#reader-step-s-hamiltonian [data-testid="branch-picker"]') as HTMLElement;
    const radios = within(picker).getAllByRole("radio");
    expect(radios).toHaveLength(5);
    expect(radios.map((radio) => radio.dataset.status)).toEqual([
      "complete",
      "complete",
      "active",
      "failed",
      "proposed",
    ]);
    expect(within(picker).getByRole("radio", { name: /Failed/ })).toBeInTheDocument();
    expect(within(picker).getByRole("radio", { name: /Proposed/ })).toBeInTheDocument();
  });

  it("puts a human_revision under revisions, never under continue-from-here", () => {
    renderReader(stressRun, "sroute-thermo");

    const picker = document.querySelector('#reader-step-s-mean-energy [data-testid="branch-picker"]') as HTMLElement;
    const continueGroup = within(picker).getByRole("radiogroup", { name: "Continue from here" });
    const revisionGroup = within(picker).getByRole("radiogroup", { name: "Revisions of this step" });

    expect(within(continueGroup).getAllByRole("radio")).toHaveLength(1);
    expect(within(revisionGroup).getAllByRole("radio")).toHaveLength(1);
    expect(within(revisionGroup).getByRole("radio", { name: /revised/ })).toBeInTheDocument();
  });

  it("keeps a direct entry to a brand new branch on branchable forks", async () => {
    const user = userEvent.setup();
    const { props } = renderReader(fixtureRun, "route-a");

    const newBranch = screen.getAllByRole("button", { name: /New branch from here/ });
    expect(newBranch).toHaveLength(2);
    await user.click(newBranch[0]);
    expect(props.onOpenBranch).toHaveBeenCalledWith(expect.objectContaining({ id: "constraints" }));
  });

  it("hides the new-branch entry on a read-only run", () => {
    renderReader(fixtureRun, "route-a", { branchableStepRevisionIds: [] });

    expect(screen.queryByRole("button", { name: /New branch from here/ })).not.toBeInTheDocument();
    expect(screen.getAllByTestId("branch-picker")).toHaveLength(2);
  });
});

const liveCall = (overrides: Partial<LiveCall> = {}): LiveCall => ({
  callId: "call-1",
  branchId: "branch-a",
  fromStepId: "conservation",
  label: "Writer model call",
  ...overrides,
});

/** The same run with one more sealed step at the tip of `routeId`. */
function withExtraStep(run: DerivationRun, routeId: string): DerivationRun {
  const route = run.routes.find((candidate) => candidate.id === routeId)!;
  const tip = route.nodeIds[route.nodeIds.length - 1];
  const source = run.steps.find((step) => step.id === tip)!;
  const extra: DerivationStep = {
    ...source,
    id: "fresh-step",
    revisionId: "revision-fresh-step",
    order: run.steps.length,
    title: "A freshly sealed step",
  };
  return {
    ...run,
    steps: [...run.steps, extra],
    edges: [...run.edges, { id: "e-fresh", from: tip, to: extra.id, order: 0, kind: "continuation" }],
    routes: run.routes.map((candidate) => (candidate.id === routeId ? { ...candidate, nodeIds: [...candidate.nodeIds, extra.id] } : candidate)),
  };
}

describe("live activity block", () => {
  it("stays out of the document when nothing is running", () => {
    renderReader(fixtureRun, "route-a");
    expect(screen.queryByTestId("reader-activity")).not.toBeInTheDocument();
  });

  it("names the role, anchors the origin step and shows how long the call has run", () => {
    renderReader(fixtureRun, "route-a", {
      activeCalls: [liveCall(), liveCall({ callId: "call-2", fromStepId: "weak-result", label: "Checker model call" })],
    });

    const block = screen.getByTestId("reader-activity");
    expect(block).toHaveAttribute("aria-live", "off");
    expect(within(block).getByText("Deriving")).toBeVisible();
    expect(within(block).getByText("Checking")).toBeVisible();
    // `conservation` is third on route-a, `weak-result` fifth.
    expect(within(block).getByRole("button", { name: "Go to step 03" })).toHaveTextContent("STEP 03");
    expect(within(block).getByRole("button", { name: "Go to step 05" })).toBeVisible();
    expect(within(block).getAllByText("0:00")).toHaveLength(2);
  });

  it("scrolls to the origin step from its anchor", async () => {
    const user = userEvent.setup();
    renderReader(fixtureRun, "route-a", { activeCalls: [liveCall()] });

    const target = document.getElementById("reader-step-conservation")!;
    const scrollIntoView = vi.spyOn(target, "scrollIntoView");
    await user.click(screen.getByRole("button", { name: "Go to step 03" }));

    expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: "start" }));
  });

  it("shows a call rooted off the route as plain text, not as an anchor", () => {
    renderReader(fixtureRun, "route-a", { activeCalls: [liveCall({ fromStepId: "symmetry" })] });

    const block = screen.getByTestId("reader-activity");
    expect(within(block).getByText("symmetry")).toBeVisible();
    expect(within(block).queryByRole("button")).not.toBeInTheDocument();
  });

  it("tells a reader the first step is not sealed yet", () => {
    const empty: DerivationRun = { ...fixtureRun, routes: [{ ...fixtureRun.routes[0], nodeIds: [] }] };
    renderReader(empty, "route-a", { activeCalls: [], runPhase: "autonomous_exploration" });

    expect(screen.getByTestId("reader-activity")).toHaveTextContent("The first step has not been sealed yet.");
  });

  it("says nothing about a first step on a finished run", () => {
    const empty: DerivationRun = { ...fixtureRun, routes: [{ ...fixtureRun.routes[0], nodeIds: [] }] };
    renderReader(empty, "route-a", { activeCalls: [], runPhase: "review_ready" });

    expect(screen.queryByTestId("reader-activity")).not.toBeInTheDocument();
  });
});

describe("per-step checks", () => {
  it("marks each step with the worst of its three checks", () => {
    const run: DerivationRun = {
      ...fixtureRun,
      steps: fixtureRun.steps.map((step) => (step.id === "conservation"
        ? { ...step, checks: { ...step.checks, physics: "failed" as const } }
        : step.id === "perturbation"
          ? { ...step, checks: { ...step.checks, physics: "pending" as const } }
          : step)),
    };
    renderReader(run, "route-a");

    expect(screen.getByTestId("step-check-question")).toHaveClass("is-passed");
    expect(screen.getByTestId("step-check-conservation")).toHaveClass("is-failed");
    expect(screen.getByTestId("step-check-conservation")).toHaveAccessibleName("Checks: Failed");
    expect(screen.getByTestId("step-check-perturbation")).toHaveClass("is-pending");
  });
});

describe("a step sealed while the desk is open", () => {
  afterEach(() => vi.useRealTimers());

  it("marks the new step, announces it and lets the marker expire", () => {
    vi.useFakeTimers();
    const { props, container, rerender } = renderReader(fixtureRun, "route-a");
    expect(container.querySelector(".route-step.is-new")).toBeNull();

    const next = withExtraStep(fixtureRun, "route-a");
    rerender(<RouteReader {...props} route={next.routes[0]} steps={next.steps} edges={next.edges} routes={next.routes} />);

    expect(container.querySelector("#reader-step-fresh-step")).toHaveClass("is-new");
    expect(screen.getByTestId("reader-seal-announcement")).toHaveTextContent("New step sealed: step 06 — A freshly sealed step");

    act(() => vi.advanceTimersByTime(4_000));
    expect(container.querySelector("#reader-step-fresh-step")).not.toHaveClass("is-new");
    expect(screen.getByTestId("reader-seal-announcement")).toHaveTextContent("");
  });
});

describe("jump to latest", () => {
  class FakeIntersectionObserver {
    static instances: FakeIntersectionObserver[] = [];
    constructor(readonly callback: IntersectionObserverCallback) {
      FakeIntersectionObserver.instances.push(this);
    }
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] { return []; }
  }

  const reportEnd = (isIntersecting: boolean) => {
    const observer = FakeIntersectionObserver.instances[FakeIntersectionObserver.instances.length - 1];
    act(() => observer.callback([{ isIntersecting } as IntersectionObserverEntry], observer as unknown as IntersectionObserver));
  };

  afterEach(() => {
    FakeIntersectionObserver.instances = [];
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("appears only once a step arrives while the reader is away from the end", () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    const { props, rerender } = renderReader(fixtureRun, "route-a");

    // Away from the end, but nothing new has arrived: reading is not interrupted.
    reportEnd(false);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).not.toBeInTheDocument();

    const next = withExtraStep(fixtureRun, "route-a");
    rerender(<RouteReader {...props} route={next.routes[0]} steps={next.steps} edges={next.edges} routes={next.routes} />);
    expect(screen.getByRole("button", { name: "Jump to latest" })).toBeVisible();

    // Back at the end: the affordance has nothing left to offer.
    reportEnd(true);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).not.toBeInTheDocument();
  });

  it("never scrolls the reader on its own", async () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    const user = userEvent.setup();
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const { props, rerender } = renderReader(fixtureRun, "route-a");

    reportEnd(false);
    const next = withExtraStep(fixtureRun, "route-a");
    rerender(<RouteReader {...props} route={next.routes[0]} steps={next.steps} edges={next.edges} routes={next.routes} />);
    expect(scrollIntoView).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    scrollIntoView.mockRestore();
  });
});

describe("snapshot churn", () => {
  it("does not re-render a sealed step when the run object is replaced", () => {
    const { props, rerender } = renderReader(fixtureRun, "route-a");
    expect(renderLog.text.length).toBeGreaterThan(0);

    // Exactly what an SSE event does: same content, every object identity new.
    const snapshot: DerivationRun = structuredClone(fixtureRun);
    renderLog.text.length = 0;
    rerender(
      <RouteReader
        {...props}
        route={snapshot.routes[0]}
        steps={snapshot.steps}
        edges={snapshot.edges}
        routes={snapshot.routes}
        branchableStepRevisionIds={snapshot.commands.branchable_step_revision_ids}
      />,
    );

    expect(renderLog.text).toEqual([]);
  });

  it("re-renders only the step whose revision changed", () => {
    const { props, rerender } = renderReader(fixtureRun, "route-a");

    const snapshot: DerivationRun = structuredClone(fixtureRun);
    const revised = snapshot.steps.map((step) => (step.id === "perturbation"
      ? { ...step, revisionId: "revision-perturbation-v2", reasoningSummary: "Rewritten reasoning summary." }
      : step));
    renderLog.text.length = 0;
    rerender(<RouteReader {...props} route={snapshot.routes[0]} steps={revised} edges={snapshot.edges} routes={snapshot.routes} />);

    // Only that step's article repaints — its summary and its result block.
    expect(renderLog.text).toEqual(["Rewritten reasoning summary.", "The leading correction comes from second-order virtual transitions."]);
  });

  it("repaints a step when the API starts serving it from a typeset layer", () => {
    const { props, rerender } = renderReader(fixtureRun, "route-a");

    // The layer is built after the route completes, so the same sealed
    // revision arrives a second time with repaired math. A memo keyed on the
    // revision alone would leave the broken formula on screen for ever.
    const snapshot: DerivationRun = structuredClone(fixtureRun);
    const typeset = snapshot.steps.map((step) => (step.id === "perturbation"
      ? { ...step, typeset: true, reasoningSummary: "Reasoning summary after the typesetting repair." }
      : step));
    renderLog.text.length = 0;
    rerender(<RouteReader {...props} route={snapshot.routes[0]} steps={typeset} edges={snapshot.edges} routes={snapshot.routes} />);

    expect(renderLog.text).toContain("Reasoning summary after the typesetting repair.");
  });

  it("keeps the scientific renderers memoized", async () => {
    const actual = await import("./ScientificText");
    // A memo component is an object with React's memo tag, never a bare function.
    expect(String((actual.ScientificInlineTitle as unknown as { $$typeof: symbol }).$$typeof)).toBe("Symbol(react.memo)");
  });
});

describe("typeset marker", () => {
  it("marks only the steps whose math came from the typeset layer", () => {
    const run: DerivationRun = structuredClone(fixtureRun);
    run.steps = run.steps.map((step) => (step.id === "perturbation" ? { ...step, typeset: true } : step));

    const { container } = renderReader(run, "route-a");

    const marked = container.querySelectorAll(".route-step-typeset");
    expect(marked).toHaveLength(1);
    expect(screen.getByTestId("step-typeset-perturbation")).toHaveAccessibleDescription(/sealed record text is unchanged/);
    expect(container.querySelector("#reader-step-perturbation .route-step-typeset")).toBeInTheDocument();
  });

  it("says nothing when the app is serving the sealed text", () => {
    const { container } = renderReader(fixtureRun, "route-a");

    expect(container.querySelectorAll(".route-step-typeset")).toHaveLength(0);
  });
});
